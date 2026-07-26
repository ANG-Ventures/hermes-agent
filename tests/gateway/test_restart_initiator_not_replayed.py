"""A turn that ASKED for the restart must not be auto-replayed after it.

The restart-cascade loop
------------------------
1. A user turn runs ``/restart`` (or otherwise reaches ``request_restart``).
2. ``stop()`` drains.  That turn is still running — it IS the turn that asked
   for the restart — so the drain-timeout path marks it ``resume_pending`` with
   reason ``restart_timeout``.
3. ``restart_timeout`` is in ``_AUTO_RESUME_REASONS``, so on the next boot
   ``_schedule_resume_pending_sessions`` synthesizes a continuation turn for it.
4. The replayed turn re-runs the same work — including the restart request.
5. Go to 1.

Each iteration also emits a shutdown notification, so the user sees a stream of
"Gateway restarting" banners.

The distinguishing signal already exists in the runner: ``request_restart``
knows a restart was requested, and ``_restart_command_source`` records WHICH
session's command asked for it.  Nothing consults it at the drain-mark, so a
restart-initiating turn is marked with the same reason as an innocent
bystander turn that merely happened to be running.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


def _entry(session_key: str, source) -> SessionEntry:
    return SessionEntry(
        session_key=session_key,
        session_id="sid-" + session_key,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )


class _RecordingStore:
    """Minimal session store that records mark_resume_pending reasons."""

    def __init__(self):
        self._entries = {}
        self.marks = []

    def mark_resume_pending(self, session_key, reason="restart_timeout"):
        self.marks.append((session_key, reason))
        entry = self._entries.get(session_key)
        if entry is None:
            return False
        entry.resume_pending = True
        entry.resume_reason = reason
        entry.last_resume_marked_at = datetime.now()
        return True

    def clear_resume_pending(self, session_key):
        entry = self._entries.get(session_key)
        if entry is not None:
            entry.resume_pending = False
        return True

    def _save(self):
        return None


def _prepare(runner, initiator_key, bystander_key):
    source = make_restart_source(chat_id="initiator")
    other = make_restart_source(chat_id="bystander")
    store = _RecordingStore()
    store._entries = {
        initiator_key: _entry(initiator_key, source),
        bystander_key: _entry(bystander_key, other),
    }
    runner.session_store = store
    return store


@pytest.mark.asyncio
async def test_restart_initiating_turn_is_not_marked_auto_resumable():
    """The turn that requested the restart must get a NON-auto-resume reason.

    Without this the same turn is replayed on the next boot, re-runs the
    restart request, and the gateway restarts forever.
    """
    runner, _adapter = make_restart_runner()
    initiator = "agent:main:telegram:dm:initiator"
    bystander = "agent:main:telegram:dm:bystander"
    store = _prepare(runner, initiator, bystander)

    runner._restart_requested = True
    runner._session_initiated_restart = {initiator: True}

    reason = runner._resume_reason_for_shutdown_mark(initiator)

    assert reason not in GatewayRunner._AUTO_RESUME_REASONS, (
        f"the restart-initiating turn was marked {reason!r}, which IS an "
        "auto-resume reason — the next boot replays it, it re-fires the "
        "restart, and the gateway cascades"
    )


@pytest.mark.asyncio
async def test_bystander_turn_still_auto_resumes():
    """Feature preserved: an innocent interrupted turn still auto-resumes.

    The fix must be surgical — only the turn that CAUSED the restart loses
    its replay, not every session that happened to be running.
    """
    runner, _adapter = make_restart_runner()
    initiator = "agent:main:telegram:dm:initiator"
    bystander = "agent:main:telegram:dm:bystander"
    _prepare(runner, initiator, bystander)

    runner._restart_requested = True
    runner._session_initiated_restart = {initiator: True}

    reason = runner._resume_reason_for_shutdown_mark(bystander)

    assert reason in GatewayRunner._AUTO_RESUME_REASONS, (
        "a bystander turn lost its auto-resume — the fix over-reached"
    )


@pytest.mark.asyncio
async def test_restart_consumed_reason_survives_across_boots():
    """A relapse must not overwrite the mark back to an auto-resume reason.

    If boot N marks the initiator ``restart_consumed`` but boot N+1 (where the
    in-memory ``_session_initiated_restart`` flag is gone, the process being
    new) re-marks it ``restart_timeout``, the loop simply resumes one boot
    later. The durable session entry has to be consulted too.
    """
    runner, _adapter = make_restart_runner()
    initiator = "agent:main:telegram:dm:initiator"
    bystander = "agent:main:telegram:dm:bystander"
    store = _prepare(runner, initiator, bystander)

    # Boot N: in-memory flag present.
    runner._restart_requested = True
    runner._session_initiated_restart = {initiator: True}
    first = runner._resume_reason_for_shutdown_mark(initiator)
    store.mark_resume_pending(initiator, first)

    # Boot N+1: fresh process — no in-memory flag, only the durable entry.
    runner._session_initiated_restart = {}
    second = runner._resume_reason_for_shutdown_mark(initiator)

    assert second not in GatewayRunner._AUTO_RESUME_REASONS, (
        f"the cross-boot mark decayed to {second!r} — the cascade resumes one "
        "boot later instead of being broken"
    )


@pytest.mark.asyncio
async def test_scheduler_skips_a_restart_consumed_session():
    """E2E: the non-auto-resume reason actually suppresses the replay."""
    runner, _adapter = make_restart_runner()
    initiator = "agent:main:telegram:dm:initiator"
    bystander = "agent:main:telegram:dm:bystander"
    store = _prepare(runner, initiator, bystander)

    runner._restart_requested = True
    runner._session_initiated_restart = {initiator: True}

    # Mark BOTH as the drain path would.
    store.mark_resume_pending(
        initiator, runner._resume_reason_for_shutdown_mark(initiator)
    )
    store.mark_resume_pending(
        bystander, runner._resume_reason_for_shutdown_mark(bystander)
    )

    replayed = [
        key
        for key, entry in store._entries.items()
        if entry.resume_pending
        and entry.resume_reason in GatewayRunner._AUTO_RESUME_REASONS
    ]

    assert initiator not in replayed, (
        "the restart-initiating session is still scheduled for replay — the "
        "cascade is intact"
    )
    assert bystander in replayed, "the bystander lost its legitimate resume"


def test_request_restart_records_the_initiating_session():
    """``request_restart`` must record WHICH session asked.

    Without this breadcrumb the drain-mark (which runs later, on the shutdown
    path, with no reference to the originating turn) has no way to tell the
    initiator apart from a bystander.
    """
    runner, _adapter = make_restart_runner()
    initiator = "agent:main:telegram:dm:initiator"

    from gateway.session_context import set_session_vars

    async def _drive():
        set_session_vars(session_key=initiator)
        runner.request_restart(detached=False, via_service=True)

    asyncio.run(_drive())

    assert getattr(runner, "_session_initiated_restart", {}).get(initiator), (
        "request_restart did not record the initiating session, so the "
        "drain-mark cannot distinguish it from an interrupted bystander"
    )


def test_clean_turn_clears_the_initiator_breadcrumb():
    """Forward progress must release the suppression.

    A session that ran ``/restart`` once must not lose auto-resume forever.
    Once it completes a clean turn, a LATER unrelated restart that happens to
    interrupt it should be marked normally and still auto-resume.
    """
    runner, _adapter = make_restart_runner()
    initiator = "agent:main:telegram:dm:initiator"
    bystander = "agent:main:telegram:dm:bystander"
    _prepare(runner, initiator, bystander)

    runner._restart_requested = True
    runner._session_initiated_restart = {initiator: True}
    assert (
        runner._resume_reason_for_shutdown_mark(initiator)
        not in GatewayRunner._AUTO_RESUME_REASONS
    )

    # A clean turn completes for that session (what the post-turn hook does).
    runner._session_initiated_restart.pop(initiator, None)
    # Its durable entry no longer carries the consumed reason either.
    runner.session_store._entries[initiator].resume_reason = None

    assert (
        runner._resume_reason_for_shutdown_mark(initiator)
        in GatewayRunner._AUTO_RESUME_REASONS
    ), "the session lost auto-resume permanently after one /restart"


def test_restart_consumed_is_not_an_auto_resume_reason():
    """Invariant: the two sets must stay disjoint.

    If a future edit adds ``restart_consumed`` to ``_AUTO_RESUME_REASONS`` the
    whole fix silently becomes a no-op, so pin the relationship rather than
    the literal.
    """
    assert (
        GatewayRunner._RESTART_CONSUMED_REASON
        not in GatewayRunner._AUTO_RESUME_REASONS
    )
