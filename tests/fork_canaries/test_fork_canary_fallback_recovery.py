"""Fork canary: fallback / quota recovery fixes of 2026-10-11..13 (#1890 #1894 #1920 #1977 #1979).

Surfaces: ``agent/fallback_wall_budget.py`` (cross-lane no-first-byte budget),
``agent/chat_completion_helpers.py`` (chain walker), ``agent/fallback_policy.py``
(sticky classes), ``agent/fallback_events.py`` (banner class),
``agent/turn_recovery.py`` (drain credit), ``agent/fork_ext/relay_quota.py`` +
``agent/turn_api_error.py`` (long relay quota hands off instead of sleeping).

None of these modules exist upstream; ``turn_api_error.py``, ``turn_recovery.py`` and
``chat_completion_helpers.py`` do, and a parity merge that resolves them toward
upstream drops the fork call sites with no import error. Each test names the
merge-shaped edit that turns it red. Rigs are imported from the shipped tests so
the canary drives the same real turn loop (``run_conversation`` /
``try_activate_fallback``) with only the wire, resolver and clock faked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent import fallback_wall_budget as wb
from agent.error_classifier import FailoverReason
from agent.fallback_wall_budget import FallbackWallBudgetExceeded
from tests.agent.test_fallback_budget_cut_sticky import _cut
from tests.agent.test_fallback_policy import (  # noqa: F401  (wired is a fixture)
    OPUS, _restore, _state, _wired_agent, wired,
)
from tests.agent.test_relay_error_class import RelayError
from tests.agent.test_relay_quota_retry_handoff import make_agent, quota_error, run
from tests.run_agent.test_fallback_wall_budget import STALL_504, _agent, _drive
from tests.run_agent.test_pool_capacity_503_retry import _response

FABLE = "claude-fable-5-1"


# ── #1979: confirmed long relay quota -> next lane, no sleep ────────────────


def test_a_long_relay_quota_429_hands_off_without_sleeping():
    """ALR 2026-10-12/13: a quota 429 with Retry-After 1783 s slept the 600 s cap
    and retried the exhausted lane while dtlr/btpr/bpr sat untouched.

    RED-PROVABLE: agent/turn_api_error.py ~L161 resolve to upstream, i.e.
    ``_relay_quota_exhausted = relay_quota_outlasts_retry_window(...)`` ->
    ``_relay_quota_exhausted = False`` (the sleep comes back, same lane retried)."""
    agent = make_agent([])
    sleeps: list = []
    result, activate, call, attempts = run(agent, quota_error(status=429), sleeps)
    assert result["completed"] is True
    assert sum(sleeps) == 0, f"slept {sum(sleeps)} s on a quota that outlasts the wait policy"
    assert [p for p, _ in attempts] == ["claude-alr", "claude-dtlr"]
    assert activate.called


@pytest.mark.parametrize("error,wait", [
    (quota_error(retry_after="30"), 30),  # short structured quota: recovers in place
    # "quota" text + long Retry-After but no relay class and no rejected window header: unproven
    (RelayError(429, {"error": "quota exhausted"}, {"retry-after": "1783"}), 600),
])
def test_b_short_or_unproven_wait_keeps_in_place_retry(error, wait):
    """The fail-open half: only a PROVEN long quota leaves the lane.

    RED-PROVABLE: agent/fork_ext/relay_quota.py ~L23 widen the class:
    ``not retry_after > RETRY_AFTER_CAP_RATE_LIMIT_S`` -> ``not retry_after > 0`` (short arm),
    or ~L31 ``if relay_class not in (None, "upstream_passthrough") ...`` -> ``return True``
    for anything left (unproven arm)."""
    agent = make_agent([])
    sleeps: list = []
    result, activate, call, attempts = run(agent, error, sleeps)
    assert result["completed"] is True
    activate.assert_not_called()
    assert [p for p, _ in attempts] == ["claude-alr", "claude-alr"]
    assert sum(sleeps) == pytest.approx(wait, abs=0.21)


def test_c_long_quota_with_no_fallback_fails_loud_without_retry():
    """Nothing to hand off to: the turn ends with an error the user sees, after ONE call.

    RED-PROVABLE: agent/turn_api_error.py resolve the #1979 hunk to upstream
    (~L161 ``_relay_quota_exhausted = False``: sleep, then the exhausted lane again), or
    drop BOTH exhaustion pins (~L174 ``retry_count = max_retries - 1`` -> ``0`` AND the
    ~L233 ``if _relay_quota_exhausted and _ce.action == "fallthrough"`` block). Either pin
    alone keeps it green: they are redundant on this path (measured 2026-10-14)."""
    agent = make_agent([], fallback=False)
    sleeps: list = []
    result, activate, call, attempts = run(agent, quota_error(status=429), sleeps)
    assert result.get("failed") is True
    assert result.get("completed") is not True
    assert result["final_response"], "a failed turn must say so"
    assert call.call_count == 1
    assert sum(sleeps) == 0


# ── #1920: the same-model skip stays dead ───────────────────────────────────


def test_d_budget_cut_walks_every_lane_in_chain_order():
    """2026-10-11 23:26 PT: the cut on alr skipped dtlr, btpr and bpr (same model) and
    sent a 378k-token fable turn to cpa/gpt at 2x price.

    RED-PROVABLE: agent/chat_completion_helpers.py ~L3729 (try_activate_fallback), after
    the ``_should_skip_fallback_candidate`` ``continue``, re-add the skip:
    ``if _wb.spent(agent) and fb_model == agent.model and any(e.get("model") != fb_model
    for e in agent._fallback_chain[agent._fallback_index:]): continue``."""
    agent = _agent([])  # alr/fable, chain [dtlr/fable, btpr/fable, bpr/fable, cpa/gpt]
    script = [
        (601.0, FallbackWallBudgetExceeded("cut", lane="claude-alr", elapsed_s=601.0)),
        (2.0, RelayError(504, STALL_504)),
        (2.0, RelayError(504, STALL_504)),
        (3.0, _response("served")),
    ]
    result, called = _drive(agent, script, {})
    assert result["final_response"] == "served"
    # Every same-model lane, in chain order, before the other model is considered.
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr", "claude-bpr"]
    assert "cpa" not in called


def test_d_cut_fires_with_only_same_model_lanes_ahead():
    """The cut rule must not wait for a lane on a different model.

    RED-PROVABLE: agent/fallback_wall_budget.py ``_lane_ahead`` ~L190 restore
    has_other_model_ahead's filter: ``_viable(agent, fb)`` ->
    ``_viable(agent, fb) and fb.get("model") != agent.model``."""
    only_fable = SimpleNamespace(model=FABLE, provider="claude-alr", _wall_budget=None,
                                 _fallback_index=0,
                                 _fallback_chain=[{"provider": p, "model": FABLE}
                                                  for p in ("claude-dtlr", "claude-btpr", "claude-bpr")])
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"no_first_byte_budget_seconds": 600}}):
        wb.arm(only_fable, now=0.0)
        assert not wb.should_cut_attempt(only_fable, now=599.0)
        assert wb.should_cut_attempt(only_fable, now=601.0)


# ── #1977: a budget cut arms the sticky episode ─────────────────────────────


def test_e_budget_cut_arms_sticky_and_next_turn_stays(wired):
    """2026-10-13 11:47-12:01 PT: alr held a turn 606 s, the cut moved it to dtlr, and the
    next turn restored alr in the same second.

    RED-PROVABLE: agent/fallback_policy.py ~L58 drop the arm:
    ``STICKY_CLASSES = frozenset({"conn", "pool_pressure", "quota_seat", "quota_model"})``."""
    a = _wired_agent()
    assert _cut(a) is True
    st = _state(a)
    assert st is not None and st.active and st.cls == fp.NO_FIRST_BYTE_BUDGET_CLASS
    assert _restore(a) is False, "the cut lane must not be restored on the next turn"
    assert (a.provider, a.model) == OPUS


# ── #1894: relay drain sleep is credited against the wall clock ─────────────


def test_f_excused_wait_subtracts_from_the_wall_clock():
    """2026-10-11 16:37 PT: a 126 s deploy drain posted "still waiting for a first reply"
    into a healthy session.

    RED-PROVABLE: agent/fallback_wall_budget.py ``note_excused_wait`` ~L139 ignore the
    credit: ``st["started"] += credit`` -> ``pass``."""
    a = SimpleNamespace(model=FABLE, provider="claude-alr", _wall_budget=None,
                        _fallback_index=0, _fallback_chain=[{"provider": "cpa", "model": "gpt"}])
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"no_first_byte_budget_seconds": 600,
                                          "still_waiting_notice_seconds": 120}}):
        wb.arm(a, now=0.0)
        wb.note_excused_wait(a, 126.0)
        assert wb.elapsed(a, now=130.0) == pytest.approx(4.0)
        assert wb.notice_due(a, now=130.0) is None
        assert not wb.spent(a, now=126.0 + 599.0)
        assert wb.spent(a, now=126.0 + 600.0)


def test_f_drain_branch_credits_the_real_sleep():
    """The call site: turn_recovery's relay_draining branch credits each real sleep.

    RED-PROVABLE: agent/turn_recovery.py ~L2113 delete
    ``note_excused_wait(agent, time.monotonic() - _drain_sleep_t0)``."""
    from tests.run_agent.test_relay_deploy_drain import _drain_error, _make_agent, _mock_response, _run
    agent = _make_agent()
    credits: list = []
    real = wb.note_excused_wait

    def _spy(ag, seconds):
        credits.append(float(seconds))
        real(ag, seconds)

    calls: list = []

    def fake_api_call(api_kwargs):
        calls.append(1)
        if len(calls) <= 2:
            raise _drain_error(retry_after="0.05")
        return _mock_response("served after the drain")

    with patch("agent.fallback_wall_budget.note_excused_wait", _spy):
        result, _ = _run(agent, fake_api_call, drain_wait_s=30.0)
    assert result["completed"] is True
    assert len(credits) == 2
    assert all(0.03 <= c <= 1.0 for c in credits), credits


# ── #1920: the cut carries its evidence and has its own banner class ────────


def test_g_budget_cut_carries_lane_elapsed_headers_and_banner_class():
    """Before #1920 the cut rendered as "unclassified error" with no lane or seat.

    RED-PROVABLE: agent/fallback_wall_budget.py FallbackWallBudgetExceeded.__init__ ~L64
    drop a field (``self.elapsed_s = elapsed_s`` deleted), or agent/fallback_events.py
    ``classify_trigger`` ~L281 delete the ``if exc_name == _BUDGET_CUT_EXC`` branch."""
    exc = FallbackWallBudgetExceeded("cut", lane="claude-alr", elapsed_s=600.2,
                                     pool_headers={"x-pool-served-by": "sub-vps-5"})
    assert exc.lane == "claude-alr"
    assert exc.elapsed_s == pytest.approx(600.2)
    assert exc.pool_headers == {"x-pool-served-by": "sub-vps-5"}
    assert fbe.classify_trigger(exc_name=type(exc).__name__) == ("no_first_byte_budget", "harness")
    a = SimpleNamespace(_pending_fallback_error=None, session_id="s", _current_turn_id="s:1")
    fbe.stash_api_error(a, exc, None, None, elapsed_s=600.2)
    row = fbe.build_row(a, "failover", from_provider="claude-alr", from_model=FABLE,
                        to_provider="claude-dtlr", to_model=FABLE,
                        reason=FailoverReason.pool_stalled.value)
    assert row["trigger_class"] == fp.NO_FIRST_BYTE_BUDGET_CLASS
    assert row["budget_cut"]["lane"] == "claude-alr"
    assert row["seat"] == "sub-vps-5"
