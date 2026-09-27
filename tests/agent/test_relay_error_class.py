"""Harness side of the relay error classes (fallback spec 2026-09-25 D2,
Phase 1b).

* a relay-stated ``503 + conn`` retries in place and does not fall back
  (real ``run_conversation`` against a scripted provider);
* the in-stream ``relay_error_class`` is parsed (Anthropic SDK body = whole
  SSE event; OpenAI SDK body = the inner ``error`` object);
* an upstream "Fable limit" with no relay class is ``quota_seat`` and, on a
  pool relay, neither benches the model nor runs ``apply_quota_gate``;
* ``x-hermes-accepts: error-class-v2`` rides every claude-apr AND claude-bpr
  request, and never a direct pin or third party.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import fallback_events as fbe
from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason, classify_api_error
from agent.fork_ext.relay_headers import (
    _pool_capability_headers,
    merge_pool_capability_headers,
)
from tests.run_agent.test_pool_capacity_503_retry import _make_agent, _response, _run


class RelayError(Exception):
    """SDK-shaped error: status, headers on ``.response``, parsed ``.body``."""

    def __init__(self, status, body, headers=None, text=None):
        super().__init__(text or f"Error code: {status} - {body}")
        self.status_code = status
        self.body = body
        self.response = SimpleNamespace(status_code=status,
                                        headers=dict(headers or {}),
                                        json=lambda: body)


def _conn_503():
    return RelayError(503, {"error": "upstream connect timed out"},
                      {"x-relay-error-class": "conn",
                       "x-relay-error-hop": "relay->bridge",
                       "x-relay-seat": "sub-vps-9", "x-relay-seats-tried": "2",
                       "x-relay-eligible": "7"})


def _legacy_conn_429():
    """Today's bytes for the same failure (relay flag off / not negotiated)."""
    return RelayError(429, {"error": "upstream connect timed out"})


FABLE_TEXT = ("Claude Code returned an error result: You've reached your Fable "
              "limit. Switch to another model to continue.")


def _fable_429_no_class():
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": FABLE_TEXT}}
    return RelayError(429, body, {"content-type": "application/json"},
                      text=f"Error code: 429 - {body}")


# --------------------------------------------------------------------------- #
# classifier: the relay class beats status and text
# --------------------------------------------------------------------------- #
def test_conn_class_is_retry_in_place_not_fallback():
    c = classify_api_error(_conn_503(), provider="claude-apr", model="claude-fable-5-1")
    assert c.reason is FailoverReason.timeout
    assert c.retryable is True and c.should_fallback is False
    assert c.should_rotate_credential is False


def test_conn_class_wins_over_a_lying_429_status():
    err = RelayError(429, {"error": "upstream connect timed out"},
                     {"x-relay-error-class": "conn"})
    c = classify_api_error(err, provider="claude-bpr")
    assert c.reason is FailoverReason.timeout and c.should_fallback is False


def test_legacy_connect_timeout_429_is_still_rate_limit():
    """Negative control: without the relay class the same body is today's
    rate_limit (the eager-fallback path) — the class is what changes it."""
    c = classify_api_error(_legacy_conn_429(), provider="claude-apr")
    assert c.reason is FailoverReason.rate_limit


@pytest.mark.parametrize("cls,reason,fallback", [
    ("pool_pressure", FailoverReason.overloaded, False),
    ("quota_model", FailoverReason.pool_exhausted, True),
    ("quota_seat", FailoverReason.rate_limit, True),
    ("rate_upstream", FailoverReason.rate_limit, True),
])
def test_relay_classes_map_to_recovery(cls, reason, fallback):
    err = RelayError(429, {"error": "x"}, {"x-relay-error-class": cls})
    c = classify_api_error(err, provider="claude-apr")
    assert c.reason is reason and c.should_fallback is fallback


def test_passthrough_and_unknown_class_use_the_normal_pipeline():
    for cls in ("upstream_passthrough", "martian"):
        err = RelayError(400, {"type": "error", "error": {
            "type": "invalid_request_error", "message": "messages: bad"}},
            {"x-relay-error-class": cls})
        c = classify_api_error(err, provider="claude-apr")
        assert c.reason not in (FailoverReason.timeout, FailoverReason.pool_exhausted)


# --------------------------------------------------------------------------- #
# in-stream class
# --------------------------------------------------------------------------- #
def test_in_stream_class_anthropic_sdk_body_is_the_whole_event():
    body = {"type": "error", "error": {"type": "rate_limit_error",
                                       "message": "Fable limit"},
            "relay_error_class": "quota_model", "relay_error_hop": "bridge->anthropic"}
    err = RelayError(200, body)
    assert fbe.relay_error_class(err) == ("quota_model", "relay_stream")
    assert classify_api_error(err, provider="claude-apr").reason is FailoverReason.pool_exhausted
    assert fbe.classify_trigger(body=body, http_status=200) == ("quota_model", "relay_stream")


def test_in_stream_class_openai_sdk_body_is_the_inner_error():
    # openai._streaming raises APIError(body=data["error"]): only the inner
    # object survives, so the relay puts the class there too.
    inner = {"message": "connection reset", "type": "server_error", "code": 502,
             "relay_error_class": "conn"}
    err = RelayError(200, inner)
    assert fbe.relay_error_class(err) == ("conn", "relay_stream")
    c = classify_api_error(err, provider="claude-bpr")
    assert c.reason is FailoverReason.timeout and c.should_fallback is False


def test_header_beats_stream_field():
    err = RelayError(200, {"relay_error_class": "quota_model"},
                     {"x-relay-error-class": "conn"})
    assert fbe.relay_error_class(err) == ("conn", "relay_header")


# --------------------------------------------------------------------------- #
# the loop: 503 + conn retries in place
# --------------------------------------------------------------------------- #
def test_503_conn_retries_in_place_and_does_not_fall_back():
    statuses: list = []
    agent = _make_agent(statuses)
    sleeps: list = []
    result, activate, call = _run(agent, [_conn_503(), _response("served")], sleeps)
    assert result["completed"] is True and result["final_response"] == "served"
    activate.assert_not_called()
    assert agent._fallback_activated is False
    assert agent.provider == "claude-apr"
    assert call.call_count == 2
    assert not getattr(agent, "_rate_limited_until", 0)


def test_legacy_connect_timeout_429_falls_back_on_the_first_hit():
    """Negative control on the SAME rig: today's 429 bytes eagerly fall back
    after one attempt. The relay class is what keeps the session in place."""
    statuses: list = []
    agent = _make_agent(statuses)
    sleeps: list = []
    result, activate, call = _run(agent, [_legacy_conn_429(), _response("fb")], sleeps)
    assert activate.called
    assert activate.call_args.kwargs.get("reason") is FailoverReason.rate_limit
    assert call.call_count == 2          # one primary attempt, then the fallback


def test_persistent_conn_falls_back_only_after_in_place_retries():
    statuses: list = []
    agent = _make_agent(statuses)
    sleeps: list = []
    err = _conn_503()
    result, activate, call = _run(agent, [err, err, err, err, _response("fb")], sleeps)
    assert activate.called
    first = activate.call_args_list[0]
    assert first.kwargs.get("reason") is FailoverReason.timeout
    # >= 2 same-provider attempts before the chain was walked
    assert call.call_count >= 3


# --------------------------------------------------------------------------- #
# Fable limit with no relay class: quota_seat, no bench, no quota gate
# --------------------------------------------------------------------------- #
def _stashed_agent(provider, err):
    agent = SimpleNamespace(provider=provider, model="claude-fable-5-1",
                            _pending_fallback_error=None)
    fbe.stash_api_error(agent, err, err.status_code, {"message": FABLE_TEXT})
    return agent


def test_fable_limit_without_relay_class_is_quota_seat():
    agent = _stashed_agent("claude-bpr", _fable_429_no_class())
    assert fbe.pending_trigger_class(agent) == "quota_seat"
    assert fbe.quota_seat_on_relay(agent) is True
    # peek only: the ledger row still gets the evidence
    assert agent._pending_fallback_error is not None


def test_direct_pin_fable_limit_is_not_relay_quota_seat():
    agent = _stashed_agent("claude-bpx-3", _fable_429_no_class())
    assert fbe.quota_seat_on_relay(agent) is False


class _Stop(Exception):
    pass


def _fallback_agent(provider):
    err = _fable_429_no_class()
    agent = SimpleNamespace(
        provider=provider, model="claude-fable-5-1", _fallback_activated=False,
        _primary_runtime={"provider": provider, "model": "claude-fable-5-1"},
        _rate_limit_backoff_count=0, _rate_limited_until=0,
        _fallback_chain=[], _fallback_index=0, _pending_fallback_error=None,
        _pending_stream_error_reason=None)
    fbe.stash_api_error(agent, err, 429, {"message": FABLE_TEXT})
    return agent


@pytest.mark.parametrize("provider,benched", [
    ("claude-apr", False), ("claude-bpr", False),   # relay: the relay rotates seats
    ("claude-bpx-3", True),                          # direct pin: seat == provider
])
def test_quota_seat_on_relay_neither_benches_nor_runs_quota_gate(provider, benched):
    agent = _fallback_agent(provider)
    agent._fallback_chain = [{"provider": "openrouter", "model": "x"}]
    agent._fallback_index = 1                          # tail empty: no client built
    with patch("agent.quota_registry_gate.load_registry_snapshot",
               return_value={}) as snap:
        ok = try_activate_fallback(agent, reason=FailoverReason.rate_limit)
    assert ok is False
    # the gate ran (marked the turn) only for the direct pin
    assert bool(getattr(agent, "_quota_gate_applied", False)) is benched
    snap.assert_not_called()                           # empty tail either way
    armed = (agent._rate_limited_until or 0) > time.monotonic()
    assert armed is benched
    assert agent._rate_limit_backoff_count == (1 if benched else 0)


def test_quota_seat_through_the_classifier_path_on_the_loop():
    """End to end on the real loop: an un-classed Fable-limit 429 on the relay
    falls back (fallback flow unchanged) but arms no bench and no gate."""
    statuses: list = []
    agent = _make_agent(statuses)
    sleeps: list = []
    with patch("agent.quota_registry_gate.prune_exhausted_entries") as prune:
        result, activate, call = _run(agent, [_fable_429_no_class(), _response("fb")], sleeps)
    assert activate.called
    prune.assert_not_called()                     # neither gate caller pruned
    assert not getattr(agent, "_quota_gate_applied", False)
    assert not ((getattr(agent, "_rate_limited_until", 0) or 0) > time.monotonic())


# --------------------------------------------------------------------------- #
# per-request negotiation header
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider,want", [
    ("claude-apr", True), ("claude-bpr", True), ("Claude-BPR", True),
    ("claude-bpx-3", False), ("claude-apx-2", False), ("anthropic", False),
    ("openai-codex", False), ("", False)])
def test_capability_header_is_pool_relay_scoped(provider, want):
    h = _pool_capability_headers(SimpleNamespace(provider=provider))
    assert (h == {"x-hermes-accepts": "error-class-v2"}) is want


def test_merge_keeps_existing_extra_headers():
    kw = {"extra_headers": {"x-hermes-session": "s"}}
    merge_pool_capability_headers(SimpleNamespace(provider="claude-apr"), kw)
    assert kw["extra_headers"] == {"x-hermes-session": "s",
                                   "x-hermes-accepts": "error-class-v2"}
    kw2 = {"model": "m"}
    merge_pool_capability_headers(SimpleNamespace(provider="claude-bpx-3"), kw2)
    assert "extra_headers" not in kw2


def _kwargs(agent):
    return agent._build_api_kwargs([{"role": "user", "content": "hi"}])


def test_build_api_kwargs_chat_completions_bpr_carries_accepts():
    agent = _make_agent([])
    agent.provider = "claude-bpr"
    kw = _kwargs(agent)
    assert kw["extra_headers"]["x-hermes-accepts"] == "error-class-v2"


def test_build_api_kwargs_chat_completions_direct_pin_does_not():
    agent = _make_agent([])
    agent.provider = "claude-bpx-3"
    kw = _kwargs(agent)
    assert "x-hermes-accepts" not in (kw.get("extra_headers") or {})


def test_build_api_kwargs_anthropic_messages_apr_carries_accepts_and_session():
    agent = _make_agent([])
    agent.api_mode = "anthropic_messages"
    agent.session_id = "20260926_000000_abcdef"
    kw = _kwargs(agent)
    eh = kw.get("extra_headers") or {}
    assert eh.get("x-hermes-accepts") == "error-class-v2"
    assert eh.get("x-hermes-session") == "20260926_000000_abcdef"
