"""Per-turn no-first-byte wall budget across the fallback chain (t_452c6ead).

Acceptance fixture: the 2026-10-11 01:59-02:34 cascade (t_f2ddf6d1). Chain
alr -> dtlr -> btpr -> bpr (all claude-fable-5-1) -> cpa (gpt). alr failed in
seconds, dtlr and btpr each held the turn 420 s with no byte and 504'd, and the
walk went on to bpr on the same model: 34 min of dead air. With the DEFAULT budget
(600 s) spent once btpr fails, the walker skips the remaining fable face and lands
on the first lane on another model.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from tests.agent.test_relay_error_class import RelayError
from tests.run_agent.test_pool_capacity_503_retry import _FakeClock, _fallback_client, _response
from run_agent import AIAgent

FABLE = "claude-fable-5-1"
CHAIN = [
    {"provider": "claude-dtlr", "model": FABLE},
    {"provider": "claude-btpr", "model": FABLE},
    {"provider": "claude-bpr", "model": FABLE},
    {"provider": "cpa", "model": "gpt-6.1-sol"},
]
# Relay 504 after per_attempt_timeout_s (bytes verbatim, see test_relay_stall_capacity_fallback).
STALL_504 = {"error": "upstream attempt timed out"}


def _agent(statuses):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="k", base_url="http://127.0.0.1:18810/v1", provider="claude-alr",
            model=FABLE, quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=[dict(e) for e in CHAIN],
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )
    agent.api_mode = "chat_completions"
    agent._api_max_retries = 3
    return agent


def _drive(agent, script, fallback_cfg):
    """``script``: list of (seconds_the_call_takes, outcome) consumed per provider call.
    Returns (result, providers_called)."""
    seq = list(script)
    called: list[str] = []
    clock = _FakeClock([])

    def _fake_call(*_a, **_k):
        called.append(agent.provider)
        secs, outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        clock._offset += secs
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    agent._disable_streaming = True
    cfg = {"fallback": fallback_cfg}
    with (
        patch("agent.chat_completion_helpers.should_use_direct_api_call", return_value=True),
        patch.object(agent, "_interruptible_api_call", side_effect=_fake_call),
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(_fallback_client(), "m")),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, _p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("hermes_cli.config.read_raw_config", return_value={"model": {"announce_route_change": True}}),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.turn_recovery.time", clock),
        patch("agent.fallback_wall_budget.time", clock, create=True),
    ):
        result = agent.run_conversation("hello")
    return result, called


INCIDENT = [
    (2.0, RelayError(504, STALL_504)),     # alr: fails fast (01:59:43)
    (420.0, RelayError(504, STALL_504)),   # dtlr: 420 s silent, relay 504 (02:06:44)
    (420.0, RelayError(504, STALL_504)),   # btpr: another 420 s silent seat; 842 s spent
    (3.0, _response("served")),           # whichever lane comes next answers
]


def test_incident_timeline_skips_remaining_fable_lanes_once_default_budget_is_spent():
    agent = _agent([])
    result, called = _drive(agent, INCIDENT, {})  # pins the shipped default, not a test value
    assert result["final_response"] == "served"
    # dtlr+btpr burned the budget; bpr is another face of the same stalled model: go to cpa.
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr", "cpa"]


def test_healthy_slow_lane_inside_default_budget_is_not_skipped():
    """A 500 s silent dtlr then a healthy btpr: the default budget is not spent, so the
    chain order holds and the turn stays on fable (no mid-conversation model change)."""
    agent = _agent([])
    script = [(2.0, RelayError(504, STALL_504)), (500.0, RelayError(504, STALL_504)), (3.0, _response("served"))]
    result, called = _drive(agent, script, {})
    assert result["final_response"] == "served"
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr"]


def test_budget_not_spent_keeps_the_chain_order():
    """Negative control: dtlr fails inside the budget, so btpr (same model) still gets its turn."""
    agent = _agent([])
    script = [(2.0, RelayError(504, STALL_504)), (30.0, RelayError(504, STALL_504)), (3.0, _response("served"))]
    result, called = _drive(agent, script, {"no_first_byte_budget_seconds": 180})
    assert result["final_response"] == "served"
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr"]


def test_zero_disables_the_budget():
    agent = _agent([])
    result, called = _drive(agent, INCIDENT, {"no_first_byte_budget_seconds": 0})
    assert result["final_response"] == "served"
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr", "claude-bpr"]


def test_unusable_other_model_lane_does_not_strand_the_turn():
    """Prism a54a5cb796b0: the only other-model lane is unusable, so the spent budget must
    not skip the remaining fable face into an exhausted chain."""
    agent = _agent([])
    agent._unavailable_fallback_keys = {("cpa", "gpt-6.1-sol", "")}  # e.g. a 401 earlier this session
    result, called = _drive(agent, INCIDENT, {})
    assert result["final_response"] == "served"
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr", "claude-bpr"]


def test_walker_revisits_budget_skipped_lanes_when_the_other_model_is_refused_late():
    """A filter the budget cannot see ahead of time (a card pin refusing cpa): the walk
    comes back to the skipped fable face instead of ending the turn with no reply."""
    agent = _agent([])
    with patch("hermes_cli.kanban_worker_route.refuse_runtime_failover",
               side_effect=lambda _a, prov, _m, _r=None: prov == "cpa"):
        result, called = _drive(agent, INCIDENT, {})
    assert result["final_response"] == "served"
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr", "claude-bpr"]


# ── unit: clock, skip rule, notices ─────────────────────────────────────────


def _bare(model=FABLE, chain=CHAIN, index=1):
    return SimpleNamespace(model=model, provider="claude-dtlr", _fallback_chain=chain,
                           _fallback_index=index, _wall_budget=None)


def test_skip_lane_only_for_the_stalled_model_and_only_with_another_model_ahead():
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"no_first_byte_budget_seconds": 180}}):
        a = _bare()
        wb.arm(a, now=0.0)
        assert not wb.skip_lane(a, FABLE, now=179.0)
        assert wb.skip_lane(a, FABLE, now=181.0)
        assert not wb.skip_lane(a, "gpt-6.1-sol", now=181.0)
        # Nothing on another model ahead: never skip into an empty chain.
        only_fable = _bare(chain=CHAIN[:3])
        wb.arm(only_fable, now=0.0)
        assert not wb.skip_lane(only_fable, FABLE, now=500.0)
        # The only other-model lane is unusable: same answer.
        blocked = _bare()
        blocked._quota_gate_skipped_providers = {"cpa"}
        wb.arm(blocked, now=0.0)
        assert not wb.skip_lane(blocked, FABLE, now=500.0)
        assert not wb.should_cut_attempt(blocked, now=500.0)


def test_default_budget_does_not_cut_a_healthy_slow_attempt():
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        a = _bare()
        wb.arm(a, now=0.0)
        assert not wb.should_cut_attempt(a, now=500.0)
        assert wb.should_cut_attempt(a, now=wb.DEFAULT_BUDGET_S + 1)


def test_first_real_byte_disarms():
    from agent import fallback_wall_budget as wb
    a = _bare()
    wb.arm(a, now=0.0)
    wb.disarm(a)
    assert not wb.spent(a, now=10_000.0)


def test_still_waiting_notice_once_per_interval_names_lanes():
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"still_waiting_notice_seconds": 120}}):
        a = _bare()
        wb.arm(a, now=0.0)
        assert wb.notice_due(a, now=119.0) is None
        wb.note_lane_switch(a)
        text = wb.notice_due(a, now=121.0)
        assert text and "121s" in text and "2 lanes tried" in text and "claude-dtlr" in text
        # It names what is left: the fable faces ahead and the other model behind them.
        assert "ahead: claude-btpr, claude-bpr [claude-fable-5-1] + cpa [gpt-6.1-sol]" in text
        assert wb.notice_due(a, now=200.0) is None
        assert wb.notice_due(a, now=241.0) is not None


def test_budget_cut_error_falls_over_on_first_hit():
    from agent.fallback_wall_budget import FallbackWallBudgetExceeded
    c = classify_api_error(FallbackWallBudgetExceeded("cut"), provider="claude-dtlr", model=FABLE)
    assert c.reason is FailoverReason.pool_stalled
    assert c.retryable is False and c.should_fallback is True


# ── streaming: the monitor cuts a silent attempt and posts the notice ──────


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_streaming_monitor_cuts_silent_attempt_and_posts_notice(monkeypatch):
    from agent.fallback_wall_budget import FallbackWallBudgetExceeded

    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "60")
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    statuses: list = []
    agent = AIAgent(api_key="k", base_url="https://example.com/v1", model=FABLE, quiet_mode=True,
                    skip_context_files=True, skip_memory=True,
                    status_callback=lambda kind, message: statuses.append((kind, message)))
    agent.api_mode = "anthropic_messages"
    agent._anthropic_client = MagicMock()
    agent._anthropic_api_key = "k"
    agent._create_request_anthropic_client = lambda *a, **k: agent._anthropic_client
    agent._fallback_chain = [dict(e) for e in CHAIN]
    agent._fallback_index = 1
    unblock = threading.Event()
    agent._abort_request_anthropic_client = lambda *a, **k: unblock.set()

    def _silent():
        unblock.wait(timeout=10.0)
        raise httpx.ReadError("socket shut down")
        yield

    def _stream(*_a, **_k):
        cm, stream = MagicMock(), MagicMock()
        stream.__iter__ = MagicMock(return_value=_silent())
        cm.__enter__ = MagicMock(return_value=stream)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    agent._anthropic_client.messages.stream.side_effect = _stream
    cfg = {"fallback": {"no_first_byte_budget_seconds": 0.6, "still_waiting_notice_seconds": 0.3}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        from agent.fallback_wall_budget import arm
        arm(agent)  # the turn loop arms it in build_api_request; this drives the call directly
        with pytest.raises(FallbackWallBudgetExceeded):
            agent._interruptible_streaming_api_call({"model": FABLE})
    assert any("still waiting for a first reply" in str(m) for _k, m in statuses)
