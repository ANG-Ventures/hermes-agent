"""Relay/bridge read-phase stalls and capacity refusals walk ``fallback_providers`` fast
(t_bbb0dec5, measured on claude-dlr 2026-10-03).

Two shapes stranded workers for the 600 s card limit:

* The relay's own 504 ``{"error":"upstream attempt timed out"}`` (and ``pool deadline
  exceeded``) on a lane that negotiated error-class-v2 (claude-alr / claude-bpr): the relay
  stamps ``x-relay-error-class: conn`` on it, and the stated class beat the exact body, so the
  stall classified ``timeout`` and was retried in place (2 x per_attempt_timeout_s=420 s).
* The tui bridge's 504 ``error.code=tui_turn_timeout`` passed through the dlr relay (161 hits
  on 2026-10-03): it fell to the 5xx floor (``server_error``) and was retried up to 3 x 600 s.

Both mean a box accepted the turn and stalled past its deadline; re-sending re-enters the same
stall. They classify ``pool_stalled`` and fail over on the first hit, with or without the v2
header. The bridge's ``tui_capacity`` 503 keeps at most one same-provider retry.

Bytes are verbatim from claude_pool_relay.py (claude-pool a42b818) and claude-bpx
bridge/src/tuiRunner.js TUI_ERRORS.
"""

from __future__ import annotations

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from tests.agent.test_relay_error_class import RelayError
from tests.run_agent.test_pool_capacity_503_retry import _make_agent, _response, _run

_V2_CONN = {"x-relay-error-class": "conn", "x-relay-error-hop": "relay->bridge",
            "x-relay-seat": "sub-vps-18", "x-relay-seats-tried": "1", "x-relay-eligible": "2"}


def _tui(code, status, typ, message):
    return {"error": {"type": typ, "code": code, "message": message}}


TUI_TURN_TIMEOUT = _tui("tui_turn_timeout", 504, "api_error",
                        "the interactive turn on this box did not complete before the turn deadline (600 s)")
TUI_CAPACITY = _tui("tui_capacity", 503, "overloaded_error",
                    "this box has no free interactive session slot; place the session on another box (6/6 slots in use)")

STALLS = {
    "relay_attempt_timeout_v2": lambda: RelayError(504, {"error": "upstream attempt timed out"}, _V2_CONN),
    "relay_pool_deadline_v2": lambda: RelayError(504, {"error": "pool deadline exceeded"}, _V2_CONN),
    "relay_attempt_timeout_legacy": lambda: RelayError(504, {"error": "upstream attempt timed out"}),
    "bridge_tui_turn_timeout_legacy": lambda: RelayError(504, TUI_TURN_TIMEOUT),
    "bridge_tui_turn_timeout_v2": lambda: RelayError(504, TUI_TURN_TIMEOUT, _V2_CONN),
}


@pytest.mark.parametrize("shape", sorted(STALLS))
@pytest.mark.parametrize("provider", ["claude-dtlr", "claude-alr", "claude-bpr"])
def test_read_phase_stall_classifies_pool_stalled(shape, provider):
    c = classify_api_error(STALLS[shape](), provider=provider, model="claude-opus-5-5")
    assert c.reason is FailoverReason.pool_stalled
    assert c.retryable is False
    assert c.should_fallback is True
    assert c.should_rotate_credential is False


@pytest.mark.parametrize("shape", sorted(STALLS))
def test_read_phase_stall_falls_back_on_the_first_hit(shape):
    """One primary call, then the fallback rung; no same-provider retry, no sleep."""
    agent = _make_agent([])
    sleeps: list[float] = []
    result, activate, call = _run(agent, [STALLS[shape](), _response("via fallback")], sleeps)
    assert result["final_response"] == "via fallback"
    activate.assert_called_once()
    assert activate.call_args.kwargs["reason"] is FailoverReason.pool_stalled
    assert call.call_count == 2
    assert sleeps == []


DRAINED = {"error": "no eligible sub: every subscription that could serve this request is "
                    "drained for maintenance; retry shortly"}
MODE = {"error": "no eligible sub: no subscription that serves the requested bridge mode is "
                 "available; retry shortly or drop the mode"}


@pytest.mark.parametrize("body", [DRAINED, MODE], ids=["drained", "mode"])
def test_no_eligible_pool_pressure_bodies_match_their_v2_class(body):
    """Legacy bytes classify as the relay's v2 ``pool_pressure``, not the quota capacity wait."""
    legacy = classify_api_error(RelayError(503, body), provider="claude-dtlr", model="m")
    v2 = classify_api_error(RelayError(503, body, {"x-relay-error-class": "pool_pressure"}),
                            provider="claude-alr", model="m")
    assert legacy.reason is v2.reason is FailoverReason.overloaded


@pytest.mark.parametrize("body", [DRAINED, MODE], ids=["drained", "mode"])
def test_no_eligible_pool_pressure_falls_back_after_one_retry(body):
    agent = _make_agent([])
    err = RelayError(503, body)
    result, activate, call = _run(agent, [err, err, _response("via fallback")], [])
    assert result["final_response"] == "via fallback"
    activate.assert_called_once()
    assert call.call_count == 3  # first try + one retry, then the fallback call


def test_quota_no_eligible_sub_keeps_the_capacity_wait():
    """Negative control: the bare / model-scoped quota body stays ``pool_exhausted``."""
    for body in ({"error": "no eligible sub"},
                 {"error": "no eligible sub for the requested model; this model's budget is capped"}):
        c = classify_api_error(RelayError(503, body), provider="claude-dtlr", model="m")
        assert c.reason is FailoverReason.pool_exhausted


def test_v2_conn_connect_timeout_still_retries_in_place():
    """Negative control: a relay-stated ``conn`` with a CONNECT-phase body is not a stall."""
    e = RelayError(503, {"error": "upstream connect timed out"}, _V2_CONN)
    c = classify_api_error(e, provider="claude-alr", model="claude-opus-5-5")
    assert c.reason is FailoverReason.timeout
    assert c.should_fallback is False


def test_other_bridge_504_code_is_not_a_stall():
    """Negative control: the stall rule keys on the exact machine code, not on 504 or 'timeout'."""
    e = RelayError(504, _tui("tui_other", 504, "api_error", "the interactive turn timed out"))
    c = classify_api_error(e, provider="claude-dtlr", model="claude-opus-5-5")
    assert c.reason is not FailoverReason.pool_stalled


@pytest.mark.parametrize("headers", [None, _V2_CONN], ids=["legacy", "v2"])
def test_tui_capacity_gets_at_most_one_same_provider_retry(headers):
    """Regression pin (already true on fork/main): the transport branch falls back after
    one same-provider retry. Must stay that way once tui_capacity is relay-tagged."""
    agent = _make_agent([])
    err = RelayError(503, TUI_CAPACITY, headers)
    result, activate, call = _run(agent, [err, err, _response("via fallback")], [])
    assert result["final_response"] == "via fallback"
    activate.assert_called_once()
    assert call.call_count == 3  # first try + one retry, then the fallback call
