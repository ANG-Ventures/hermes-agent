"""Idle compaction must see the TRUE idle gap on the gateway's agent paths.

Before the fix, both gateway agent paths zeroed ``_last_activity_ts`` before
``build_turn_context`` measured the idle gap:

- cached agent: ``GatewayRunner._init_cached_agent_for_turn`` resets the clock
  to "now" (watchdog, #9051);
- evicted/rebuilt agent: the idle sweep evicts at the same 1h the idle trigger
  uses, and a rebuilt agent's clock is its construction time.

So ``idle_compact_after_seconds`` never fired in the gateway (Blackbox: 0 fires
over 46 eligible turns in 23h). These tests drive the real gateway helpers
against a real ``SessionDB`` transcript and a real ``AIAgent`` prologue and
assert the idle predicate receives the real gap and compaction runs.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from hermes_state import SessionDB

import agent.turn_context as turn_context
from tests.agent.test_idle_compaction_lock_and_guards import (
    _prep_idle_agent,
    _run_prologue,
)

IDLE_AFTER = 3600
GAP = 7200.0


def _seed_transcript(db: SessionDB, sid: str, last_ts: float) -> list:
    """Persist a transcript whose newest row is at ``last_ts``; load it the
    way ``SessionStore.load_transcript`` does."""
    db.create_session(sid, source="discord")
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        db.append_message(
            session_id=sid, role=role, content=f"m{i}",
            timestamp=last_ts - (9 - i) * 5,
        )
    history = db.get_messages_as_conversation(
        sid, include_timestamp=True, repair_alternation=True
    )
    assert history and max(m.get("timestamp") or 0 for m in history) == last_ts
    return history


def _run_and_capture_gap(agent, history):
    seen = {}
    real = turn_context._should_idle_compact

    def _spy(**kw):
        seen["gap"] = kw["idle_gap_seconds"]
        return real(**kw)

    with patch("agent.turn_context._should_idle_compact", side_effect=_spy):
        _run_prologue(agent, [{"role": m["role"], "content": m["content"]}
                              for m in history])
    return seen.get("gap")


def test_cached_agent_sees_true_idle_gap(tmp_path: Path) -> None:
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_CACHED"
    last = time.time() - GAP
    history = _seed_transcript(db, sid, last)
    agent = _prep_idle_agent(db, sid, idle_after=IDLE_AFTER, idle_gap=GAP)
    # Cached agent: its clock was last touched when the previous turn ended.
    agent._last_activity_ts = last

    # The real gateway run_sync order: reset per-turn state, then anchor.
    GatewayRunner._init_cached_agent_for_turn(agent, interrupt_depth=0)
    assert time.time() - agent._last_activity_ts < 5  # watchdog reset intact
    GatewayRunner._stamp_idle_gap_anchor(agent, history, 0)

    gap = _run_and_capture_gap(agent, history)

    assert gap is not None and gap >= GAP - 5, f"idle gap not reached: {gap}"
    agent.context_compressor.compress.assert_called_once()
    assert agent._blackbox_compaction.get("idle_compaction_fired") is True
    # Anchor is one-shot: consumed by the prologue.
    assert agent._idle_gap_anchor_ts is None


def test_evicted_rebuilt_agent_sees_true_idle_gap(tmp_path: Path) -> None:
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_EVICTED"
    last = time.time() - GAP
    history = _seed_transcript(db, sid, last)
    agent = _prep_idle_agent(db, sid, idle_after=IDLE_AFTER, idle_gap=GAP)
    # Freshly constructed after eviction/restart: clock == construction time.
    agent._last_activity_ts = time.time()

    GatewayRunner._stamp_idle_gap_anchor(agent, history, 0)

    gap = _run_and_capture_gap(agent, history)

    assert gap is not None and gap >= GAP - 5, f"idle gap not reached: {gap}"
    agent.context_compressor.compress.assert_called_once()
    assert agent._blackbox_compaction.get("idle_compaction_fired") is True


def test_recent_activity_does_not_fire(tmp_path: Path) -> None:
    """Control: a session active 60s ago must not idle-compact."""
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "IDLE_RECENT"
    last = time.time() - 60
    history = _seed_transcript(db, sid, last)
    agent = _prep_idle_agent(db, sid, idle_after=IDLE_AFTER, idle_gap=60)
    agent._last_activity_ts = last
    GatewayRunner._init_cached_agent_for_turn(agent, interrupt_depth=0)
    GatewayRunner._stamp_idle_gap_anchor(agent, history, 0)

    _run_and_capture_gap(agent, history)

    agent.context_compressor.compress.assert_not_called()


def test_interrupt_recursive_turn_gets_no_anchor() -> None:
    from types import SimpleNamespace
    from gateway.run import GatewayRunner

    agent = SimpleNamespace(_idle_gap_anchor_ts=123.0)
    GatewayRunner._stamp_idle_gap_anchor(
        agent, [{"role": "user", "content": "x", "timestamp": 1.0}], 1
    )
    assert agent._idle_gap_anchor_ts is None


def test_run_sync_stamps_anchor_from_raw_transcript() -> None:
    """Wiring: run_sync stamps the anchor from ctx.history (raw rows keep
    timestamps) before converting history for the agent."""
    import inspect
    from gateway import run as gw_run

    src = inspect.getsource(gw_run)
    stamp = src.index("_stamp_idle_gap_anchor(agent, ctx.history, ctx._interrupt_depth)")
    build = src.index("_build_gateway_agent_history(\n            ctx.history,")
    assert stamp < build
