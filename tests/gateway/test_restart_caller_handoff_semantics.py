"""Safe-restart resume semantics (Ace ruling 2026-10-03 01:43, card t_04247008).

1. CALLER (the session that invoked the restart): ALWAYS gets a new turn driven
   by its handoff when the gateway returns, mid-turn or already finished. No
   unfinished-work check.
2. SIBLING cut mid-turn: resumes where it left off, and the resume note says a
   gateway restart happened (one-line system notice, not a new user turn).
3. SIBLING already idle: nothing. No turn, no notice.

Incident: deploy-window-1003-b15 (2026-10-03 00:08). The calling session had
finished its turn, the boot logged ``boot_resume_skipped ... kind=None
cause=no_unfinished_work`` and the window's HANDOFF was dropped.

The four cases run through the real dropbox sweep, the real finished-work check
and the real scheduler. Mutant: make the sweep stamp every request as a sibling
and ``test_caller_finished_turn_still_gets_the_handoff_turn`` goes RED.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from gateway import resume_requests as rr
from gateway.run import _SIBLING_RESTART_NOTICE, _build_resume_pending_message
from tests.gateway.test_boot_resume_skips_finished_sessions import (
    _COMPLETED_TAIL,
    _INTERRUPTED_TAIL,
    _runner,
    _seed,
    _source,
)

HANDOFF = "deploy-window-test: post live-evidence on t_x, then run ang-closeout"


def _sweep(runner) -> None:
    runner._draining = False
    runner._shutdown_event = asyncio.Event()


async def _boot(runner) -> int:
    """The real startup sequence: prepare (dropbox sweep + finished-work check
    + auto classification) then schedule."""
    await runner._prepare_auto_resume_decisions()
    return runner._schedule_resume_pending_sessions()


def _scheduled_keys(messages: list[str]) -> list[str]:
    out = []
    for m in messages:
        if "PHASE=boot_resume_scheduled" in m:
            out.append(m.split("key=", 1)[1].split()[0])
    return out


@pytest.mark.asyncio
async def test_caller_finished_turn_still_gets_the_handoff_turn(
    tmp_path, monkeypatch, caplog
):
    """The b15 case: the caller's turn had ENDED before the bounce."""
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "always")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    caller = runner.session_store.get_or_create_session(_source("caller"))
    _seed(db, caller, _COMPLETED_TAIL)
    # What the boot path also does on an unclean exit: a hedge mark, kind=None.
    assert runner.session_store.mark_resume_pending(caller.session_key, "restart_interrupted")

    rr.submit_resume_request(tmp_path, caller.session_key, handoff=HANDOFF, role=rr.ROLE_CALLER)
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        _sweep(runner)
        assert await _boot(runner) == 1

    entry = runner.session_store._entries[caller.session_key]
    assert entry.resume_kind == "self"
    assert entry.resume_handoff == HANDOFF
    assert _scheduled_keys(caplog.messages) == [caller.session_key]
    assert not any("cause=no_unfinished_work" in m for m in caplog.messages)
    assert any("role=caller" in m for m in caplog.messages)
    # The new turn's prompt IS the handoff.
    note, ask = _build_resume_pending_message(
        agent_history=_COMPLETED_TAIL, message="", reason_phrase="a gateway restart",
        resume_mode="auto", resume_kind=entry.resume_kind, resume_handoff=entry.resume_handoff,
    )
    assert HANDOFF in note and ask is False
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_caller_mid_turn_gets_the_handoff_turn_not_a_sibling_replay(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "always")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    caller = runner.session_store.get_or_create_session(_source("caller"))
    _seed(db, caller, _INTERRUPTED_TAIL)
    # The drain's hedge mark, then the caller's request on top of it.
    assert runner.session_store.mark_resume_pending(caller.session_key, "shutdown_timeout")
    rr.submit_resume_request(tmp_path, caller.session_key, handoff=HANDOFF, role=rr.ROLE_CALLER)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        _sweep(runner)
        assert await _boot(runner) == 1

    entry = runner.session_store._entries[caller.session_key]
    assert entry.resume_kind == "self"
    assert entry.resume_handoff == HANDOFF
    assert _scheduled_keys(caplog.messages) == [caller.session_key]
    assert any("kind=self mode=auto" in m for m in caplog.messages)
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_sibling_mid_turn_resumes_and_is_told_a_restart_happened(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "always")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    caller = runner.session_store.get_or_create_session(_source("caller"))
    _seed(db, caller, _COMPLETED_TAIL)
    sibling = runner.session_store.get_or_create_session(_source("busy-sibling"))
    _seed(db, sibling, _INTERRUPTED_TAIL)
    assert runner.session_store.mark_resume_pending(sibling.session_key, "shutdown_timeout")
    rr.submit_resume_request(tmp_path, caller.session_key, handoff=HANDOFF, role=rr.ROLE_CALLER)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        _sweep(runner)
        assert await _boot(runner) == 2

    assert sorted(_scheduled_keys(caplog.messages)) == sorted(
        [caller.session_key, sibling.session_key]
    )
    sib = runner.session_store._entries[sibling.session_key]
    assert sib.resume_kind is None and sib.resume_handoff is None  # never gets the caller's handoff
    mode = runner._startup_resume_modes[sibling.session_key]["mode"]
    assert mode == "auto"
    note, ask = _build_resume_pending_message(
        agent_history=_INTERRUPTED_TAIL, message="", reason_phrase="a gateway shutdown",
        resume_mode=mode, resume_kind=sib.resume_kind,
    )
    assert _SIBLING_RESTART_NOTICE in note
    assert HANDOFF not in note
    assert ask is False
    await asyncio.gather(*runner._background_tasks)
    db.close()


@pytest.mark.asyncio
async def test_idle_sibling_gets_nothing(tmp_path, monkeypatch, caplog):
    """Both shapes of an idle sibling: a leftover hedge mark, and an explicit
    role=sibling dropbox request. Neither may spawn a turn."""
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "always")
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    caller = runner.session_store.get_or_create_session(_source("caller"))
    _seed(db, caller, _COMPLETED_TAIL)
    hedged = runner.session_store.get_or_create_session(_source("idle-hedged"))
    _seed(db, hedged, _COMPLETED_TAIL)
    assert runner.session_store.mark_resume_pending(hedged.session_key, "restart_interrupted")
    requested = runner.session_store.get_or_create_session(_source("idle-requested"))
    _seed(db, requested, _COMPLETED_TAIL)
    rr.submit_resume_request(tmp_path, requested.session_key, role=rr.ROLE_SIBLING)
    rr.submit_resume_request(tmp_path, caller.session_key, handoff=HANDOFF, role=rr.ROLE_CALLER)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        _sweep(runner)
        assert await _boot(runner) == 1

    assert _scheduled_keys(caplog.messages) == [caller.session_key]
    for idle in (hedged, requested):
        assert runner._is_session_running(idle.session_key) is False
        assert runner.session_store._entries[idle.session_key].resume_pending is False
        assert any(
            "PHASE=boot_resume_skipped" in m and idle.session_key in m
            and "cause=no_unfinished_work" in m
            for m in caplog.messages
        )
    await asyncio.gather(*runner._background_tasks)
    db.close()


def test_role_rides_the_dropbox_and_legacy_payload_reads_as_caller(tmp_path):
    rr.submit_resume_request(tmp_path, "agent:main:discord:group:1:1", role=rr.ROLE_SIBLING)
    assert rr.sweep_resume_requests(tmp_path) == [
        ("agent:main:discord:group:1:1", "restart_interrupted", None, "sibling")
    ]
    # Watchers that predate the role key wrote requests only for their own caller.
    d = rr.dropbox_dir(tmp_path)
    (d / "legacy.json").write_text(
        '{"session_key": "agent:main:discord:group:2:2", "reason": "restart_interrupted", '
        '"requested_at": %f, "handoff": "h"}' % __import__("time").time()
    )
    assert rr.sweep_resume_requests(tmp_path) == [
        ("agent:main:discord:group:2:2", "restart_interrupted", "h", "caller")
    ]
    with pytest.raises(ValueError):
        rr.submit_resume_request(tmp_path, "k", role="bystander")
