"""``agent.resume_interrupted_turns: always`` — continue an interrupted SIBLING
turn unattended even when its persisted tail is an incomplete mutating call.

Operator ruling (2026-09-19): a gateway restart must not turn every sibling
that happened to be mid-``terminal`` into a "may I continue?" prompt — those
turns should pick up where they left off. ``auto`` deliberately fails closed
there (the mechanical mutation gate); ``always`` drops ONLY that
tail-classification veto and keeps every structural guard:

* messaging surfaces only,
* a stable assistant rowid (structural transcript integrity),
* the once-ever-per-interrupted-turn credit (loop bound),
* the finished-work skip (no bystander resumes).

Reuses the t7 fixture from ``test_auto_continue_interrupted_turns`` so the
seeded transcript is the exact shape ``auto`` refuses.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from gateway.run import (
    _RESUME_UNATTENDED_MODES,
    _resume_interrupted_turns_mode,
)
from gateway.fork_ext.restart_policy import _bridge_agent_config_to_env
from tests.gateway.test_auto_continue_interrupted_turns import (
    _entry,
    _mark_pending,
    _runner,
    _seed_session,
    _tool_call,
)

_INCOMPLETE_TERMINAL_TAIL = [
    {"role": "user", "content": "run the migration"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [_tool_call("terminal", arguments='{"command":"./migrate.sh"}')],
    },
    {
        "role": "assistant",
        "content": "Operation interrupted.",
        "finish_reason": "interrupt_close",
    },
]


def test_mode_enum_accepts_always_and_bridge_carries_it(monkeypatch):
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    assert _resume_interrupted_turns_mode() == "prompt"
    _bridge_agent_config_to_env({"resume_interrupted_turns": "always"})
    assert _resume_interrupted_turns_mode() == "always"
    assert "always" in _RESUME_UNATTENDED_MODES and "auto" in _RESUME_UNATTENDED_MODES
    assert "prompt" not in _RESUME_UNATTENDED_MODES
    _bridge_agent_config_to_env({"resume_interrupted_turns": "ALWAYS "})
    assert _resume_interrupted_turns_mode() == "always"  # normalized like the others
    _bridge_agent_config_to_env({"resume_interrupted_turns": "nope"})
    assert _resume_interrupted_turns_mode() == "prompt"   # invalid still fails closed


@pytest.mark.asyncio
async def test_always_continues_past_incomplete_mutating_tail(tmp_path, monkeypatch, caplog):
    """The behavior contract vs ``auto``: same transcript, auto→prompt, always→auto,
    and the once-ever credit IS consumed (the loop bound stays armed)."""
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "always")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = _entry(runner)
    _seed_session(db, entry, _INCOMPLETE_TERMINAL_TAIL)
    _mark_pending(runner, entry)

    assert await runner._prepare_auto_resume_decisions() == 1
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions() == 1
    decision = runner._startup_resume_modes[entry.session_key]
    assert decision["mode"] == "auto", decision
    assert decision["reason"] is None
    logs = [r.getMessage() for r in caplog.records if "PHASE=boot_resume_scheduled" in r.getMessage()]
    assert len(logs) == 1
    assert "mode=auto" in logs[0]
    # the override is NAMED in the audit line — an operator can see the tail was vetoed
    assert "tail_override=incomplete mutating tool call: terminal" in logs[0]
    # once-ever credit consumed (unlike the auto->prompt fallback, which burns nothing)
    assert (tmp_path / "state" / "auto_resume_attempts.json").exists()
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_auto_still_prompts_on_the_same_tail(tmp_path, monkeypatch):
    """Negative control: ``auto`` is unchanged by this feature."""
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "auto")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = _entry(runner)
    _seed_session(db, entry, _INCOMPLETE_TERMINAL_TAIL)
    _mark_pending(runner, entry)
    assert await runner._prepare_auto_resume_decisions() == 1
    assert runner._schedule_resume_pending_sessions() == 1
    decision = runner._startup_resume_modes[entry.session_key]
    assert decision["mode"] == "prompt"
    assert "terminal" in decision["reason"]
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_always_is_still_once_ever_per_interrupted_turn(tmp_path, monkeypatch):
    """The loop bound survives ``always``: a second boot on the SAME interrupted turn
    falls back to prompt with the once-ever reason."""
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "always")
    first, _adapter, first_db = _runner(tmp_path, monkeypatch)
    entry = _entry(first)
    _seed_session(first_db, entry, _INCOMPLETE_TERMINAL_TAIL)
    _mark_pending(first, entry)
    assert await first._prepare_auto_resume_decisions() == 1
    assert first._schedule_resume_pending_sessions() == 1
    assert first._startup_resume_modes[entry.session_key]["mode"] == "auto"
    await asyncio.gather(*first._background_tasks)
    store_path = tmp_path / "state" / "auto_resume_attempts.json"
    assert store_path.exists(), "the once-ever credit must be recorded under always"

    # The next BOOT (same shape as test_t4_real_attempt_store_and_rowid_survive_double_restart):
    # the continuation turn was itself re-interrupted, a fresh runner comes up on the same
    # store — the ORIGINAL interrupted turn's credit is already spent.
    first_db.append_message(entry.session_id, "user", "")
    first_db.append_message(
        entry.session_id, "assistant", "Operation interrupted.", finish_reason="interrupt_close",
    )
    first_db.close()
    second, _adapter2, second_db = _runner(tmp_path, monkeypatch)
    second.session_store._ensure_loaded()
    assert await second._prepare_auto_resume_decisions() == 1
    assert second._schedule_resume_pending_sessions() == 1
    decision = second._startup_resume_modes[entry.session_key]
    assert decision["mode"] == "prompt"
    assert "already auto-continued once" in decision["reason"]
    await asyncio.gather(*second._background_tasks)
    second_db.close()


@pytest.mark.asyncio
async def test_always_still_requires_a_stable_rowid(tmp_path, monkeypatch):
    """Structural guard kept: a tail with no persisted assistant rowid is transcript
    damage, not a policy question — prompt, even under ``always``."""
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "always")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = _entry(runner)
    # user-only tail: nothing assessable, no assistant row => turn_rowid None
    _seed_session(db, entry, [{"role": "user", "content": "hello?"}])
    _mark_pending(runner, entry)
    await runner._prepare_auto_resume_decisions()
    runner._schedule_resume_pending_sessions()
    decision = runner._startup_resume_modes.get(entry.session_key)
    if decision is not None:  # (a finished-work skip may drop it entirely — also correct)
        assert decision["mode"] == "prompt"
    await asyncio.gather(*runner._background_tasks)
    db.close()
