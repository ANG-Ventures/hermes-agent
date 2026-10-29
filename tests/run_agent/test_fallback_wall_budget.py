"""Per-turn no-first-byte wall budget across the fallback chain (t_452c6ead).

Chain alr -> dtlr -> btpr -> bpr (all claude-fable-5-1) -> cpa (gpt). Since the
2026-10-12 ruling (t_778185f3) a spent budget cuts the attempt and the native
walker takes the NEXT lane in chain order, same model or not: no same-model skip.
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
from agent.fork_ext import relay_headers as _relay_headers

_REAL_FETCH_RELAY_STAGE = _relay_headers.fetch_relay_stage


@pytest.fixture(autouse=True)
def _no_live_relay_stage_read(monkeypatch):
    """Several agents here point at 127.0.0.1:18810, the live apr relay's port on the
    Studio. The t_9a635fbd stage read must never reach it from a test; the stage tests
    below put the real function back against their own loopback stub."""
    monkeypatch.setattr(_relay_headers, "fetch_relay_stage", lambda *a, **k: None)


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


def test_budget_cut_on_alr_walks_to_dtlr_on_the_same_model():
    """2026-10-11 23:26 (t_778185f3): alr held a fable turn 600 s with no byte and the
    wall budget cut it. The next lane in the chain is dtlr, on the SAME model: it is
    tried. The old same-model skip passed dtlr, btpr and bpr and landed on cpa/gpt."""
    from agent.fallback_wall_budget import FallbackWallBudgetExceeded
    agent = _agent([])
    script = [(601.0, FallbackWallBudgetExceeded("cut")), (3.0, _response("served"))]
    result, called = _drive(agent, script, {})  # the shipped default budget
    assert result["final_response"] == "served"
    assert called == ["claude-alr", "claude-dtlr"]


def test_incident_timeline_keeps_the_chain_order_after_the_budget_is_spent():
    """The 01:59-02:34 cascade: dtlr+btpr burn the default budget, and bpr (same model,
    next in the chain) is still tried before cpa."""
    agent = _agent([])
    result, called = _drive(agent, INCIDENT, {})
    assert result["final_response"] == "served"
    assert called == ["claude-alr", "claude-dtlr", "claude-btpr", "claude-bpr"]


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


# ── unit: clock, cut rule, notices ─────────────────────────────────────────


def _bare(model=FABLE, chain=CHAIN, index=1):
    return SimpleNamespace(model=model, provider="claude-dtlr", _fallback_chain=chain,
                           _fallback_index=index, _wall_budget=None)


def test_cut_fires_with_only_same_model_lanes_ahead():
    """t_778185f3: the cut no longer waits for a lane on a DIFFERENT model."""
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"no_first_byte_budget_seconds": 180}}):
        only_fable = _bare(chain=CHAIN[:3])
        wb.arm(only_fable, now=0.0)
        assert not wb.should_cut_attempt(only_fable, now=179.0)
        assert wb.should_cut_attempt(only_fable, now=181.0)
        assert not wb.should_cut_attempt(only_fable, now=500.0), "fires once per wait"


def test_cut_needs_a_usable_lane_ahead():
    """Nothing usable left in the chain: the native per-lane timers keep the attempt."""
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"no_first_byte_budget_seconds": 180}}):
        last = _bare(index=len(CHAIN))
        wb.arm(last, now=0.0)
        assert not wb.should_cut_attempt(last, now=500.0)
        blocked = _bare(chain=CHAIN[3:], index=0)
        blocked._quota_gate_skipped_providers = {"cpa"}
        wb.arm(blocked, now=0.0)
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
        a.provider = "openai-codex"  # non-relay lane: relay lanes get the t_e35032e2 wording
        wb.arm(a, now=0.0)
        assert wb.notice_due(a, now=119.0) is None
        wb.note_lane_switch(a)
        text = wb.notice_due(a, now=121.0)
        assert text and "121s" in text and "2 lanes tried" in text and "openai-codex" in text
        # Ace 2026-10-16: the chain is his own config; the notice no longer repeats it.
        assert "ahead:" not in text and "claude-btpr" not in text
        assert wb.notice_due(a, now=200.0) is None
        assert wb.notice_due(a, now=241.0) is not None


def test_relay_lane_notice_says_turn_in_progress_not_no_first_reply():
    """t_e35032e2: the claude relays buffer the whole upstream body, so the harness sees no
    byte until the turn ends. On alr 2026-10-15 06:40-06:50 every turn had upstream ttfb
    1-7 s and ran 90-257 s; "still waiting for a first reply" was wrong on almost every one."""
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"still_waiting_notice_seconds": 120,
                                          "no_first_byte_budget_seconds": 600}}):
        a = _bare(index=0)
        a.provider = "claude-alr"
        wb.arm(a, now=0.0)
        text = wb.notice_due(a, now=120.5)
    assert text == (
        "⏳ turn in progress on claude-alr/claude-fable-5-1 (buffering lane, first byte not "
        "visible until the turn ends): 120s elapsed; long outputs run 2–4 min; cut at 600s")


def test_relay_lane_notice_cut_number_is_the_live_budget():
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"still_waiting_notice_seconds": 120,
                                          "no_first_byte_budget_seconds": 900}}):
        a = _bare()  # claude-dtlr, a relay face
        wb.arm(a, now=0.0)
        text = wb.notice_due(a, now=121.0)
    assert text and "cut at 900s" in text and "cut at 600s" not in text


def test_non_relay_lane_notice_keeps_the_original_wording():
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"still_waiting_notice_seconds": 120}}):
        for provider in ("cpa", "openai-codex", "xai-oauth"):
            a = _bare(model="gpt-6.1-sol", index=4)
            a.provider = provider
            wb.arm(a, now=0.0)
            assert wb.notice_due(a, now=121.0) == (
                f"⏳ still waiting for a first reply: 121s, 1 lane tried "
                f"(now on {provider}/gpt-6.1-sol); nothing left in the fallback chain")


def test_relay_wording_changes_no_clock_or_cut_behaviour():
    """Wording only: the same wait on a relay lane and a non-relay lane gives identical
    elapsed/spent/should_cut_attempt answers and the same notice cadence."""
    from agent import fallback_wall_budget as wb
    cfg = {"fallback": {"still_waiting_notice_seconds": 120, "no_first_byte_budget_seconds": 600}}
    probes = [0.0, 119.0, 121.0, 200.0, 241.0, 599.0, 601.0, 700.0]

    def trace(provider):
        a = _bare()
        a.provider = provider
        out = []
        with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            wb.arm(a, now=0.0)
            for t in probes:
                out.append((wb.elapsed(a, now=t), wb.spent(a, now=t),
                            wb.should_cut_attempt(a, now=t), wb.notice_due(a, now=t) is None))
        return out, {k: v for k, v in a._wall_budget.items()}

    assert trace("claude-alr") == trace("cpa")


def test_relay_drain_wait_is_credited_not_counted():
    """2026-10-11 16:37 PT: merging claude-pool#249 drained the alr relay for 126 s
    (deploy_drain_refused x47). The harness waited in place as designed, and the wall
    clock posted "still waiting for a first reply: 124s" into a healthy BIOS session.
    A wait the relay ASKED for ("draining-for-deploy", Retry-After) is not provider
    silence: it must not count toward the notice or the budget."""
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"still_waiting_notice_seconds": 120,
                                          "no_first_byte_budget_seconds": 600}}):
        a = _bare()
        wb.arm(a, now=0.0)
        # 126 s of deploy-drain wait, reported by the retry loop as it sleeps.
        wb.note_excused_wait(a, 126.0)
        assert wb.elapsed(a, now=130.0) == pytest.approx(4.0)
        assert wb.notice_due(a, now=130.0) is None, "drain wait must not trigger the notice"
        # Real silence after the drain still counts from where the drain left off.
        assert wb.notice_due(a, now=126.0 + 121.0) is not None
        # The budget is credited the same way: 600 s of provider silence, not 600 s wall.
        wb.note_excused_wait(a, 300.0)
        assert not wb.spent(a, now=126.0 + 300.0 + 599.0)
        assert wb.spent(a, now=126.0 + 300.0 + 600.0)


def test_excused_wait_without_an_armed_clock_is_a_noop():
    from agent import fallback_wall_budget as wb
    a = _bare()
    wb.note_excused_wait(a, 50.0)  # nothing armed: must not create state or raise
    assert wb.elapsed(a) is None


def test_turn_recovery_drain_sleep_credits_the_wall_clock_for_the_real_wait():
    """Drive the real relay_draining branch (same rig as test_relay_deploy_drain): four
    drain 503s with Retry-After 0.05 s, then a served response. The wall clock must be
    credited for the seconds actually slept in that branch, not left running (the
    16:37 PT notice) and not credited a made-up number (a mutant that credits 0 or the
    hint must fail)."""
    from agent import fallback_wall_budget as wb
    from tests.run_agent.test_relay_deploy_drain import _make_agent, _run, _drain_error, _mock_response
    agent = _make_agent()
    credits: list[float] = []
    real = wb.note_excused_wait

    def _spy(a, seconds):
        credits.append(float(seconds))
        real(a, seconds)

    calls = []

    def fake_api_call(api_kwargs):
        calls.append(1)
        if len(calls) <= 4:
            raise _drain_error(retry_after="0.05")
        return _mock_response("served after the drain")

    with patch("agent.turn_recovery.note_excused_wait", _spy, create=True), \
         patch("agent.fallback_wall_budget.note_excused_wait", _spy):
        result, _ = _run(agent, fake_api_call, drain_wait_s=30.0)
    assert result["completed"] is True
    assert len(credits) == 4, f"one credit per drain sleep, got {credits}"
    # Each credit is the REAL slept span: ~0.05 s (the Retry-After), never 0, never the
    # 30 s window or any other constant.
    for c in credits:
        assert 0.03 <= c <= 1.0, f"credit must be the measured sleep, got {c}"


def test_budget_cut_error_falls_over_on_first_hit():
    from agent.fallback_wall_budget import FallbackWallBudgetExceeded
    c = classify_api_error(FallbackWallBudgetExceeded("cut"), provider="claude-dtlr", model=FABLE)
    assert c.reason is FailoverReason.pool_stalled
    assert c.retryable is False and c.should_fallback is True


# ── banner: the cut is classified, not "unclassified error" (t_778185f3) ────


def _budget_cut_rider(exc):
    import datetime as dt

    from agent import fallback_events as fbe
    from agent import fallback_policy as fp
    a = SimpleNamespace(_pending_fallback_error=None, session_id="s", _current_turn_id="s:1")
    fbe.stash_api_error(a, exc, None, None, elapsed_s=600.0)
    row = fbe.build_row(a, "failover", from_provider="claude-alr", from_model=FABLE,
                        to_provider="claude-dtlr", to_model=FABLE, reason="pool_stalled")
    row["first_err_ts"] = dt.datetime(2026, 10, 11, 23, 26, 32, tzinfo=dt.timezone.utc).timestamp()
    floors: list = []
    return row, fp.format_cause_rider(row, tz=dt.timezone.utc, floors=floors), floors


def test_budget_cut_banner_names_lane_elapsed_and_seat():
    from agent.fallback_wall_budget import FallbackWallBudgetExceeded
    exc = FallbackWallBudgetExceeded("cut", lane="claude-alr", elapsed_s=600.2,
                                     pool_headers={"x-pool-served-by": "sub-vps-5",
                                                   "x-pool-route-id": "65a4c896ecf1"})
    row, rider, floors = _budget_cut_rider(exc)
    assert (row["trigger_class"], row["class_source"]) == ("no_first_byte_budget", "harness")
    assert row["seat"] == "sub-vps-5" and row["route_id"] == "65a4c896ecf1"
    assert rider == "no reply in 600 s on claude-alr (sub-vps-5), 23:26:32"
    assert floors == [], "a classified cut is not a dead-letter floor"


def test_budget_cut_before_headers_says_so():
    from agent.fallback_wall_budget import FallbackWallBudgetExceeded
    exc = FallbackWallBudgetExceeded("cut", lane="claude-alr", elapsed_s=600.0, pool_headers={})
    _row, rider, _floors = _budget_cut_rider(exc)
    assert rider == \
        "no reply in 600 s on claude-alr (relay sent no headers before the cut), 23:26:32"
    assert "unclassified" not in rider and "unknown" not in rider


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
        with pytest.raises(FallbackWallBudgetExceeded) as cut:
            agent._interruptible_streaming_api_call({"model": FABLE})
    assert any("still waiting for a first reply" in str(m) for _k, m in statuses)
    # The cut carries its banner evidence (t_778185f3): lane, budget clock, and the
    # attempt's relay headers (the mock stream response answers every header name).
    assert cut.value.lane == agent.provider
    assert cut.value.elapsed_s is not None and cut.value.elapsed_s >= 0.6
    assert "x-pool-served-by" in cut.value.pool_headers


def test_notice_omits_the_chain_but_still_says_when_nothing_is_left():
    """Ace 2026-10-16 00:29: "omit the fallback chain, I know what it is". The chain text
    is gone from both wordings; the one state that changes what the cut DOES (nothing to
    fall to -> the turn ends) is still named."""
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly",
               return_value={"fallback": {"still_waiting_notice_seconds": 120,
                                          "no_first_byte_budget_seconds": 600}}):
        a = _bare(index=0)
        a.provider = "claude-alr"
        wb.arm(a, now=0.0)
        text = wb.notice_due(a, now=121.0)
        assert text and "ahead" not in text and "[claude-fable-5-1]" not in text
        assert "nothing left" not in text
        last = _bare(model="gpt-6.1-sol", index=4)
        last.provider = "cpa"
        wb.arm(last, now=0.0)
        tail = wb.notice_due(last, now=121.0)
        assert tail and tail.endswith("; nothing left in the fallback chain")



# ── t_b0a117bd: the notice names the upstream cause, or says what happens next ──
# Ace 2026-10-28 17:58 PT: 47 bare "still waiting for a first reply: 120s, 1 lane
# tried" lines in one hour while the same waits had seen 502/529/resets.

_OVERLOADED_529 = RelayError(529, {"type": "error", "error": {"type": "overloaded_error",
                                                              "message": "Overloaded"}},
                             {"x-relay-seat": "sub-vps-5"})
_NOTICE_CFG = {"fallback": {"still_waiting_notice_seconds": 120,
                            "no_first_byte_budget_seconds": 600}}


def _wait_agent(provider="claude-dtlr"):
    a = _bare(index=1)
    a.provider = provider
    a.base_url = "http://127.0.0.1:18810/v1"
    a._pending_fallback_error = None
    a.session_id, a._current_turn_id = "s", "s:t1"
    a._buffer_diagnostic_status = lambda *_a, **_k: None
    a._touch_activity = lambda *_a, **_k: None
    return a


def test_notice_names_the_529_seen_earlier_in_the_same_wait():
    """RED on base: two 529s on alr, the failover consumes the cause and the wait goes
    on on dtlr; the next notice used to be the bare lane count."""
    from agent import fallback_cause, fallback_events as fbe
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly", return_value=_NOTICE_CFG):
        a = _wait_agent(provider="claude-alr")
        wb.arm(a, now=0.0)
        fbe.stash_api_error(a, _OVERLOADED_529, 529, None, elapsed_s=1.0)
        fbe.stash_api_error(a, _OVERLOADED_529, 529, None, elapsed_s=2.0)
        fallback_cause.take(a)          # the failover row consumed the cause
        a.provider = "claude-dtlr"      # the walker moved the same wait on
        wb.note_lane_switch(a)
        with patch("agent.fallback_wall_budget._buffering_lane", return_value=False):
            text = wb.notice_due(a, now=241.0)
    # No x-relay-error-hop on this 529: the status is named, its sender is not.
    assert text == ("⏳ still waiting for a first reply: 241s on claude-dtlr/claude-fable-5-1, "
                    "2 lanes tried; last failure: HTTP 529 overloaded ×2 on sub-vps-5, retrying"), text
    assert "upstream" not in text and "Anthropic" not in text, text


def test_notice_names_a_stream_reset_retried_inside_the_stream_call():
    """RED on base: a connection reset retried by the stream loop never reaches
    stash_api_error; the wall clock still saw it."""
    from agent import fallback_wall_budget as wb
    from agent.stream_diag import emit_stream_drop
    with patch("hermes_cli.config.load_config_readonly", return_value=_NOTICE_CFG):
        a = _wait_agent()
        wb.arm(a, now=0.0)
        err = httpx.ReadError("reset")
        err.__cause__ = ConnectionResetError(104, "Connection reset by peer")
        emit_stream_drop(a, error=err, attempt=1, max_attempts=3, mid_tool_call=False,
                         diag={"http_status": 200, "started_at": 0.0})
        text = wb.notice_due(a, now=121.0)
    assert text and "last failure: connection reset" in text and "retrying" in text, text
    assert "upstream" not in text and "Anthropic" not in text, text
    assert "buffering lane" not in text, text


def test_silent_wait_notice_says_when_the_cut_comes():
    """D2: nothing failed yet, keep the wording and add the next step on the same line."""
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly", return_value=_NOTICE_CFG):
        a = _wait_agent(provider="openai-codex")
        a.model = "gpt-6.1-sol"
        wb.arm(a, now=0.0)
        text = wb.notice_due(a, now=121.0)
    assert text == ("⏳ still waiting for a first reply: 121s, 1 lane tried "
                    "(now on openai-codex/gpt-6.1-sol); cut to next lane at 600s"), text
    assert "ahead" not in text and "claude-btpr" not in text


def test_failure_memory_is_per_wait():
    """A served byte disarms the clock; the next wait starts with no failures."""
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly", return_value=_NOTICE_CFG):
        a = _wait_agent(provider="openai-codex")
        if hasattr(wb, "note_upstream_failure"):
            wb.note_upstream_failure(a, status=529)    # nothing armed: not a wait, no-op
        wb.arm(a, now=0.0)
        if hasattr(wb, "note_upstream_failure"):
            wb.note_upstream_failure(a, status=502)
        wb.disarm(a)
        wb.arm(a, now=0.0)
        text = wb.notice_due(a, now=121.0)
    assert "last failure" not in text and "502" not in text, text


# ── t_b0a117bd send-back (Apollo 2026-10-28 20:2x): name the STAGE, never blame ──
# the provider for our own path. 17:00-20:00 the slow stage was relay_send (our
# uplink, p90 80-117 s) while upstream_ttfb p90 stayed 13-19 s, and the 502s were
# ECONNRESET between relay and box. Each case below is RED on 58dc308d4b.

def _failed_once(err, status, provider="claude-alr"):
    from agent import fallback_events as fbe
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly", return_value=_NOTICE_CFG):
        a = _wait_agent(provider=provider)
        wb.arm(a, now=0.0)
        fbe.stash_api_error(a, err, status, None, elapsed_s=1.0)
        with patch("agent.fallback_wall_budget._buffering_lane", return_value=False), \
                patch("agent.fallback_cause.wait_notice", return_value=None):
            return wb.notice_due(a, now=121.0)


def test_r2_econnreset_502_reads_as_a_connection_fault_between_relay_and_box():
    """The relay's 502 for an ECONNRESET on its box hop (class conn, hop relay->bridge)."""
    err = RelayError(502, {"error": "upstream unreachable"},
                     {"x-relay-error-class": "conn", "x-relay-error-hop": "relay->bridge",
                      "x-relay-seat": "sub-vps-14"},
                     text='HTTP 502: read ECONNRESET {"error":"upstream unreachable"}')
    text = _failed_once(err, 502)
    assert "last failure: connection reset between relay and box (502) on sub-vps-14" in text, text
    assert "Anthropic" not in text and "upstream" not in text, text


def test_r2_relay_unreachable_without_errno_is_a_connection_failure_on_our_path():
    err = RelayError(502, {"error": "upstream unreachable"},
                     {"x-relay-error-class": "conn", "x-relay-error-hop": "relay->bridge",
                      "x-relay-seat": "sub-vps-7"})
    text = _failed_once(err, 502)
    assert "last failure: connection failed between relay and box (502) on sub-vps-7" in text, text
    assert "Anthropic" not in text and "upstream" not in text, text


def test_r2_408_is_a_connection_fault_not_a_provider_status():
    err = RelayError(408, {"error": "request timeout"}, {"x-relay-seat": "sub-vps-3"})
    text = _failed_once(err, 408)
    assert "last failure: request timed out (408) on sub-vps-3" in text, text
    assert "Anthropic" not in text and "upstream" not in text, text


def test_r1_relay_stage_refusal_names_our_relay():
    err = RelayError(503, {"error": "pool at capacity"},
                     {"x-relay-error-class": "pool_pressure", "x-relay-error-hop": "relay",
                      "x-relay-seat": "none"})
    text = _failed_once(err, 503)
    assert "last failure: 503 unavailable at our relay (pool at capacity)" in text, text
    assert "Anthropic" not in text and "upstream" not in text, text


def test_r1_box_stage_status_names_the_relay_to_box_hop():
    err = RelayError(504, {"error": "upstream attempt timed out"},
                     {"x-relay-error-class": "pool_stalled", "x-relay-error-hop": "relay->bridge",
                      "x-relay-seat": "sub-vps-9"})
    text = _failed_once(err, 504)
    assert "last failure: 504 gateway timeout between relay and box on sub-vps-9" in text, text
    assert "Anthropic" not in text and "upstream" not in text, text


def test_r1_provider_origin_status_is_the_only_one_that_names_anthropic():
    """hop bridge->upstream = the relay marks the answer as the provider's own."""
    err = RelayError(529, {"type": "error", "error": {"type": "overloaded_error",
                                                       "message": "Overloaded"}},
                     {"x-relay-error-class": "pool_pressure",
                      "x-relay-error-hop": "bridge->upstream", "x-relay-seat": "sub-vps-5"})
    text = _failed_once(err, 529)
    assert "last failure: Anthropic 529 overloaded on sub-vps-5" in text, text


def test_r1_silent_wait_never_says_upstream():
    from agent import fallback_wall_budget as wb
    with patch("hermes_cli.config.load_config_readonly", return_value=_NOTICE_CFG):
        a = _wait_agent()
        wb.arm(a, now=0.0)
        with patch("agent.fallback_wall_budget._buffering_lane", return_value=False):
            text = wb.notice_due(a, now=121.0)
    assert text.startswith("⏳ still waiting for a first reply: 121s"), text
    assert "upstream" not in text and "Anthropic" not in text, text


# ── t_9a635fbd: the notice names the slow hop from the relay's GET /inflight ──


def _inflight_relay(reply=None, *, status=200, delay_s=0.0):
    """A loopback 'relay' answering GET /inflight. Returns (base_url, seen_paths, stop)."""
    import json
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            time.sleep(delay_s)
            body = json.dumps(reply if status == 200 else {"error": "x"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    return f"http://127.0.0.1:{srv.server_address[1]}/v1", seen, stop


_STAGE_CFG = {"fallback": {"still_waiting_notice_seconds": 120, "no_first_byte_budget_seconds": 600}}


def _stage_notice(base_url, provider="claude-alr", session="sess-abc"):
    from agent import fallback_wall_budget as wb
    a = _bare(index=0)
    a.provider, a.base_url, a.session_id = provider, base_url, session
    with patch("hermes_cli.config.load_config_readonly", return_value=_STAGE_CFG), \
         patch.object(_relay_headers, "fetch_relay_stage", _REAL_FETCH_RELAY_STAGE):
        wb.arm(a, now=0.0)
        return wb.notice_due(a, now=121.0)


@pytest.mark.parametrize("stage,want", [
    ("relay_send", "⏳ request still uploading to sub-vps-7 (our uplink) for 95s: 121s elapsed; cut at 600s"),
    ("box_headers", "⏳ sub-vps-7 connected, provider has not sent a first token for 95s: 121s elapsed; cut at 600s"),
    ("upstream_ttfb", "⏳ sub-vps-7 connected, provider has not sent a first token for 95s: 121s elapsed; cut at 600s"),
    ("queued", "⏳ queued on sub-vps-7 for 95s: 121s elapsed; cut at 600s"),
])
def test_relay_stage_notice_names_the_hop(stage, want):
    base, seen, stop = _inflight_relay({"call": "sess-abc", "stage": stage, "seat": "sub-vps-7",
                                        "since_ms": 95_400.0, "attempt_ms": 96_000.0})
    try:
        text = _stage_notice(base)
    finally:
        stop()
    assert text == want
    assert seen == ["/inflight?call=sess-abc"], "keyed by the session the request carries"


_OLD_RELAY_WORDING = (
    "⏳ turn in progress on claude-alr/claude-fable-5-1 (buffering lane, first byte not "
    "visible until the turn ends): 121s elapsed; long outputs run 2–4 min; cut at 600s")


@pytest.mark.parametrize("stage", ["streaming", "box_body", "box_done", "something-new"])
def test_stage_that_says_nothing_new_keeps_todays_wording(stage):
    base, _seen, stop = _inflight_relay({"stage": stage, "seat": "sub-vps-7", "since_ms": 1.0})
    try:
        assert _stage_notice(base) == _OLD_RELAY_WORDING
    finally:
        stop()


def test_stage_read_failure_keeps_todays_wording():
    """404 (nothing in flight), a relay slower than 300 ms, a dead port, garbage: all
    fall back to the exact pre-change line, and none costs more than the 300 ms budget."""
    import socket
    import time
    base404, _s, stop404 = _inflight_relay(status=404)
    slow, _s2, stop_slow = _inflight_relay({"stage": "relay_send", "seat": "s"}, delay_s=1.5)
    junk, _s3, stop_junk = _inflight_relay({"no": "stage"})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{sock.getsockname()[1]}/v1"
    sock.close()
    try:
        for base in (base404, junk, dead):
            assert _stage_notice(base) == _OLD_RELAY_WORDING, base
        t0 = time.monotonic()
        assert _stage_notice(slow) == _OLD_RELAY_WORDING
        assert time.monotonic() - t0 < 1.0, "the stage read is bounded at 300 ms"
    finally:
        stop404()
        stop_slow()
        stop_junk()


def test_stage_is_never_read_off_a_non_relay_or_non_loopback_lane():
    base, seen, stop = _inflight_relay({"stage": "relay_send", "seat": "s", "since_ms": 1.0})
    try:
        text = _stage_notice(base, provider="cpa")
        assert text and text.startswith("⏳ still waiting for a first reply")
        assert seen == []
    finally:
        stop()
    opened: list = []
    assert _REAL_FETCH_RELAY_STAGE("https://api.example.com/v1", "s", opener=opened.append) is None
    assert opened == []
