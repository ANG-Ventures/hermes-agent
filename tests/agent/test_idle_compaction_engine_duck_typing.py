"""Idle-triggered compaction must not assume a ContextCompressor.

Regression for the fleet incident of 2026-09-26: after #1124 made the
gateway idle gap real, every session idle >= ``idle_compact_after_seconds``
on a profile using the ``context_engine`` plugin died at the top of the turn
with::

    AttributeError: 'LCMEngine' object has no attribute 'summary_target_ratio'

(``agent/turn_context.py`` idle floor computation). The user saw
"Sorry, I encountered an unexpected error" on every retry, because the idle
gap only grows while the turn keeps failing.

Two seams are pinned here:

1. ``build_turn_context`` computes the idle floor duck-typed — a compressor
   that lacks ``summary_target_ratio`` (any context-engine plugin) must not
   raise; it falls back to ``_config.target_ratio`` then the 0.20 default.
2. ``LCMEngine`` itself exposes ``summary_target_ratio`` mirroring the
   configured ``compression.target_ratio`` so it satisfies the same
   compressor surface ``ContextCompressor`` does.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from hermes_state import SessionDB

from tests.agent.test_idle_compaction_lock_and_guards import (
    _history,
    _prep_idle_agent,
    _run_prologue,
)


def _engine_like_compressor(*, target_ratio: float | None = 0.30) -> MagicMock:
    """A compressor that, like LCMEngine pre-fix, has NO summary_target_ratio.

    ``spec`` pins the attribute surface so ``getattr(..., "summary_target_ratio")``
    genuinely raises/returns-default instead of auto-vivifying a MagicMock.
    """
    allowed = [
        "threshold_tokens",
        "protect_first_n",
        "protect_last_n",
        "get_active_compression_failure_cooldown",
        "compress",
        "context_length",
    ]
    if target_ratio is not None:
        allowed.append("_config")
    comp = MagicMock(spec=allowed)
    comp.threshold_tokens = 100_000
    comp.protect_first_n = 2
    comp.protect_last_n = 2
    comp.context_length = 200_000
    comp.get_active_compression_failure_cooldown = lambda *a, **k: None
    comp.compress.return_value = ([{"role": "user", "content": "compacted"}], None)
    if target_ratio is not None:
        comp._config = MagicMock(spec=["target_ratio"])
        comp._config.target_ratio = target_ratio
    assert not hasattr(comp, "summary_target_ratio")
    return comp


def _prep_engine_agent(tmp_path: Path, sid: str, **kw):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(sid, source="cli")
    agent = _prep_idle_agent(db, sid)
    agent.context_compressor = _engine_like_compressor(**kw)
    # Route the idle path's compress call through the mock like the base
    # fixture does; only the floor computation is under test here.
    agent._compress_context = MagicMock(
        side_effect=lambda messages, *a, **k: (messages, "SYSTEM")
    )
    return agent


def test_idle_floor_does_not_require_summary_target_ratio(tmp_path: Path) -> None:
    """Pre-fix this raised AttributeError before any turn work ran."""
    agent = _prep_engine_agent(tmp_path, "IDLE_ENGINE_CFG", target_ratio=0.30)
    _run_prologue(agent, _history())  # must not raise
    # The idle branch was actually reached (gap 3600s >= 60s, tokens 999_999
    # > floor) and routed into the compressor, not short-circuited by a guard.
    agent._compress_context.assert_called_once()
    assert agent._compress_context.call_args.kwargs.get("trigger_reason") == "idle_resume"


def test_idle_floor_falls_back_to_default_without_any_ratio(tmp_path: Path) -> None:
    """No summary_target_ratio AND no _config: the 0.20 default floor applies."""
    agent = _prep_engine_agent(tmp_path, "IDLE_ENGINE_BARE", target_ratio=None)
    _run_prologue(agent, _history())  # must not raise
    agent._compress_context.assert_called_once()


def test_lcm_engine_exposes_summary_target_ratio(tmp_path: Path) -> None:
    """LCMEngine mirrors compression.target_ratio on the compressor surface."""
    from plugins.context_engine.lcm.config import LCMConfig
    from plugins.context_engine.lcm.engine import LCMEngine

    cfg = LCMConfig(database_path=str(tmp_path / "lcm.db"), target_ratio=0.35)
    eng = LCMEngine(config=cfg, hermes_home=str(tmp_path))
    assert eng.summary_target_ratio == 0.35
    # Same shape the idle floor computes for a ContextCompressor.
    assert isinstance(eng.summary_target_ratio, float)
    assert hasattr(eng, "threshold_tokens")
