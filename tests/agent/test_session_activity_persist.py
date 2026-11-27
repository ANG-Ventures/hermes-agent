"""Durable session activity projection from AIAgent._touch_activity (#72016)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import run_agent
from agent.session_activity import (
    ActivityProvenance,
    SESSION_ACTIVITY_PERSIST_NEVER,
    reset_session_activity_persist_window,
)


def _agent_with_db(session_id: str = "sess-1"):
    agent = SimpleNamespace(
        session_id=session_id,
        _session_db=MagicMock(),
        _last_activity_ts=0.0,
        _last_activity_desc="",
        _last_activity_provenance=ActivityProvenance.UNKNOWN,
        _session_activity_last_persist_mono=SESSION_ACTIVITY_PERSIST_NEVER,
        _current_tool=None,
        _api_call_count=0,
        max_iterations=10,
        iteration_budget=SimpleNamespace(used=0, max_total=10),
    )
    agent._touch_activity = run_agent.AIAgent._touch_activity.__get__(agent, SimpleNamespace)
    agent._persist_session_activity_if_due = (
        run_agent.AIAgent._persist_session_activity_if_due.__get__(agent, SimpleNamespace)
    )
    agent._reset_activity_labels_after_turn = (
        run_agent.AIAgent._reset_activity_labels_after_turn.__get__(agent, SimpleNamespace)
    )
    agent.get_activity_summary = run_agent.AIAgent.get_activity_summary.__get__(
        agent, SimpleNamespace
    )
    return agent




def test_touch_activity_skips_persist_without_session_db(monkeypatch):
    agent = _agent_with_db()
    agent._session_db = None
    monkeypatch.setattr(run_agent.time, "time", lambda: 1.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 1.0)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity("starting API call #1")
    assert agent._last_activity_desc == "starting API call #1"
    assert agent._last_activity_provenance is ActivityProvenance.UNKNOWN


def test_touch_activity_accepts_named_provenance(monkeypatch):
    agent = _agent_with_db()
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 1000.0)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity(
        "compressing context",
        provenance=ActivityProvenance.AGENT_COMPRESSION,
    )
    assert agent._last_activity_provenance is ActivityProvenance.AGENT_COMPRESSION
    agent._session_db.touch_session_activity.assert_called_once_with(
        "sess-1",
        1_700_000_000.0,
        description="compressing context",
        provenance=ActivityProvenance.AGENT_COMPRESSION,
    )

    agent._session_db.touch_session_activity.reset_mock()
    reset_session_activity_persist_window(agent)
    agent._touch_activity("starting API call #1")
    assert agent._last_activity_provenance is ActivityProvenance.UNKNOWN
    agent._session_db.touch_session_activity.assert_called_once_with(
        "sess-1",
        1_700_000_000.0,
        description="starting API call #1",
        provenance=ActivityProvenance.UNKNOWN,
    )


def test_touch_activity_persists_in_first_minute_after_host_boot(monkeypatch):
    """monotonic() counts from boot: a fresh CI VM / login-started gateway reads
    < 60s. The first stamp and a force_persist terminal stamp must still write
    (heavy-ci run 36328699837 flake: 0.0 sentinel read as 'persisted just now')."""
    agent = _agent_with_db()
    del agent._session_activity_last_persist_mono  # never-initialised path too
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 5.0)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity("first stamp after boot")
    assert agent._session_db.touch_session_activity.call_count == 1

    agent._touch_activity("inside the window")
    assert agent._session_db.touch_session_activity.call_count == 1  # rate limit holds

    reset_session_activity_persist_window(agent)
    agent._touch_activity("context compression completed")
    assert agent._session_db.touch_session_activity.call_count == 2


def test_touch_activity_persist_errors_are_swallowed(monkeypatch):
    agent = _agent_with_db()
    agent._session_db.touch_session_activity.side_effect = RuntimeError("db locked")
    monkeypatch.setattr(run_agent.time, "time", lambda: 1.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 1.0)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity("tool completed: terminal (1.0s)")
    assert agent._last_activity_desc == "tool completed: terminal (1.0s)"






def test_heartbeat_respects_cadence_constant(monkeypatch):
    """The rate limiter must key off the shared cadence constant."""
    from agent.session_activity import (
        SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS as INTERVAL,
    )

    agent = _agent_with_db()
    mono = {"t": 1000.0}
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: mono["t"])
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    agent._touch_activity("first")
    assert agent._session_db.touch_session_activity.call_count == 1

    # Just inside the window: no write.
    mono["t"] = 1000.0 + INTERVAL - 0.5
    agent._touch_activity("inside window")
    assert agent._session_db.touch_session_activity.call_count == 1

    # Just past the window: write.
    mono["t"] = 1000.0 + INTERVAL + 0.5
    agent._touch_activity("past window")
    assert agent._session_db.touch_session_activity.call_count == 2


def test_get_activity_summary_exposes_shared_activity_contract(monkeypatch):
    agent = _agent_with_db()
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_010.0)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent._last_activity_ts = 1_700_000_000.0
    agent._last_activity_desc = "executing tool: terminal"
    agent._last_activity_provenance = ActivityProvenance.UNKNOWN

    summary = agent.get_activity_summary()
    assert summary["last_activity_at"] == 1_700_000_000.0
    assert summary["last_activity_description"] == "executing tool: terminal"
    assert summary["last_activity_provenance"] == "unknown"
    assert summary["seconds_since_activity"] == 10.0
    assert summary["last_activity_ts"] == 1_700_000_000.0
    assert summary["last_activity_desc"] == "executing tool: terminal"
    assert "phase" not in summary
    assert "last_progress_at" not in summary


def test_reset_activity_labels_after_turn_keeps_ts_and_clears_labels():
    """Turn-end cleanup must not bump ts (watchdog continuity) but must
    clear mid-turn description/provenance and force a durable label clear.
    """
    agent = _agent_with_db()
    agent._last_activity_ts = 1_700_000_000.0
    agent._last_activity_desc = "compressing context"
    agent._last_activity_provenance = ActivityProvenance.AGENT_COMPRESSION
    # Still inside the 60s persist window from a prior heartbeat — label
    # clear must bypass that rate limit via clear_session_activity_labels.
    agent._session_activity_last_persist_mono = 1_000.0

    agent._reset_activity_labels_after_turn()

    assert agent._last_activity_ts == 1_700_000_000.0
    assert agent._last_activity_desc == ""
    assert agent._last_activity_provenance is ActivityProvenance.UNKNOWN
    agent._session_db.clear_session_activity_labels.assert_called_once_with("sess-1")
    agent._session_db.touch_session_activity.assert_not_called()


def test_reset_activity_labels_after_turn_skips_db_without_session():
    agent = _agent_with_db()
    agent.session_id = None
    agent._last_activity_ts = 42.0
    agent._last_activity_desc = "executing tool: terminal"
    agent._last_activity_provenance = ActivityProvenance.AGENT_COMPRESSION

    agent._reset_activity_labels_after_turn()

    assert agent._last_activity_ts == 42.0
    assert agent._last_activity_desc == ""
    assert agent._last_activity_provenance is ActivityProvenance.UNKNOWN
    agent._session_db.clear_session_activity_labels.assert_not_called()


def test_reset_activity_labels_after_turn_swallows_db_errors():
    agent = _agent_with_db()
    agent._last_activity_ts = 99.0
    agent._last_activity_desc = "starting API call #1"
    agent._last_activity_provenance = ActivityProvenance.UNKNOWN
    agent._session_db.clear_session_activity_labels.side_effect = RuntimeError("db locked")

    agent._reset_activity_labels_after_turn()

    assert agent._last_activity_ts == 99.0
    assert agent._last_activity_desc == ""
    assert agent._last_activity_provenance is ActivityProvenance.UNKNOWN


def test_warn_context_overflow_blocked_stamps_compression_cooldown(monkeypatch):
    agent = _agent_with_db()
    agent._last_ctx_overflow_warn = None
    agent._emit_warning = MagicMock()
    agent._touch_activity = run_agent.AIAgent._touch_activity.__get__(
        agent, SimpleNamespace
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_100.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 2000.0)

    run_agent.AIAgent._warn_context_overflow_blocked(
        agent, "cooldown: 30s remaining", 80_000, 40_000
    )

    assert agent._last_activity_provenance is ActivityProvenance.AGENT_COMPRESSION_COOLDOWN
    assert "compression blocked" in agent._last_activity_desc
    agent._emit_warning.assert_called_once()

    # Deduped re-entry must not re-touch or re-emit.
    agent._session_db.touch_session_activity.reset_mock()
    prev_desc = agent._last_activity_desc
    run_agent.AIAgent._warn_context_overflow_blocked(
        agent, "cooldown: 29s remaining", 80_000, 40_000
    )
    assert agent._last_activity_desc == prev_desc
    agent._emit_warning.assert_called_once()


def test_warn_context_overflow_blocked_stamps_cooldown_for_ineffective(monkeypatch):
    agent = _agent_with_db()
    agent._last_ctx_overflow_warn = None
    agent._emit_warning = MagicMock()
    agent._touch_activity = run_agent.AIAgent._touch_activity.__get__(
        agent, SimpleNamespace
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_100.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 2000.0)

    run_agent.AIAgent._warn_context_overflow_blocked(
        agent, "ineffective: last pass saved 0 tokens", 80_000, 40_000
    )

    assert agent._last_activity_provenance is ActivityProvenance.AGENT_COMPRESSION_COOLDOWN
    agent._emit_warning.assert_called_once()


def test_warn_context_overflow_blocked_names_damaged_store(monkeypatch):
    """t_5be6185c: when the last attempt failed on a damaged store, /compress cannot help; say so."""
    agent = _agent_with_db()
    agent._last_ctx_overflow_warn = None
    agent._emit_warning = MagicMock()
    agent.context_compressor = SimpleNamespace(last_compress_store_unavailable=True)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    run_agent.AIAgent._warn_context_overflow_blocked(agent, "attempts_exhausted:3", 730_000, 650_000)

    agent._emit_warning.assert_called_once()
    text = agent._emit_warning.call_args.args[0]
    assert "compaction store is damaged" in text
    assert "/compress uses the same store and will not help" in text
    assert "/new" in text
    assert "attempts_exhausted" not in text
    assert "730,000" in text and "650,000" in text


def test_warn_context_overflow_blocked_keeps_text_for_other_causes(monkeypatch):
    agent = _agent_with_db()
    agent._last_ctx_overflow_warn = None
    agent._emit_warning = MagicMock()
    agent.context_compressor = SimpleNamespace(last_compress_store_unavailable=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    run_agent.AIAgent._warn_context_overflow_blocked(agent, "attempts_exhausted:3", 730_000, 650_000)

    text = agent._emit_warning.call_args.args[0]
    assert "compression is currently blocked (attempts_exhausted:3)" in text
    assert "damaged" not in text


def test_warn_context_overflow_blocked_store_down_keeps_cooldown_activity(monkeypatch):
    """Prism 4c48c2a9c268: the store warning must not drop the cooldown activity touch."""
    agent = _agent_with_db()
    agent._last_ctx_overflow_warn = None
    agent._emit_warning = MagicMock()
    agent._touch_activity = MagicMock()
    agent.context_compressor = SimpleNamespace(last_compress_store_unavailable=True)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    run_agent.AIAgent._warn_context_overflow_blocked(agent, "cooldown:30s", 730_000, 650_000)

    agent._touch_activity.assert_called_once()
    assert "compression blocked (cooldown:30s)" in agent._touch_activity.call_args.args[0]
    assert "compaction store is damaged" in agent._emit_warning.call_args.args[0]


def test_compression_transition_provenances_surface_in_activity_summary(monkeypatch):
    """Compaction / timeout / cooldown publish through get_activity_summary."""
    agent = _agent_with_db()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(run_agent.time, "time", lambda: 1_700_000_200.0)
    monkeypatch.setattr(run_agent.time, "monotonic", lambda: 3000.0)

    transitions = (
        (
            ActivityProvenance.AGENT_COMPRESSION,
            "context compression in progress",
        ),
        (
            ActivityProvenance.AGENT_COMPRESSION_TIMEOUT,
            "context compression timed out",
        ),
        (
            ActivityProvenance.AGENT_COMPRESSION_COOLDOWN,
            "compression blocked (cooldown: 30s remaining)",
        ),
    )
    for provenance, desc in transitions:
        agent._touch_activity(desc, provenance=provenance)
        summary = agent.get_activity_summary()
        assert summary["last_activity_provenance"] == provenance.value
        assert summary["provenance"] == provenance.value
        assert summary["last_activity_description"] == desc
        assert summary["last_activity_desc"] == desc
