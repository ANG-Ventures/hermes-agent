"""Deferred boot-resume turns must never be admitted DURING the next drain.

Incident 2026-09-20 (Apollo, gateway/run.py). Boot marked 4 sessions resumable
at 09:55:09; Discord could not connect until 10:02:26, so the boot resumes were
only SCHEDULED at 10:02:45. Their turn bodies did not begin until 10:12:36-46 —
~590s later, and 30s INTO a shutdown drain that had started at 10:12:05. Result:

    drain done at +51.44s (drain took 50.09s, timed_out=True,
                           active_at_start=0, active_now=5 ...)

Five turns ADMITTED during the drain, interrupted at the cap, .clean_shutdown
skipped, and the next boot resumed all five AGAIN as reason=shutdown_timeout
kind=sibling. Every affected channel got the resume machinery twice.

The contract these tests lock:

(a) a boot resume SCHEDULED but not STARTED at shutdown is CANCELLED and
    RE-MARKED resumable under its ORIGINAL reason, so the next boot resumes it
    exactly once;
(b) a boot resume whose turn ALREADY STARTED is drained like normal work;
(c) a turn admitted while shutdown is in progress emits
    ``PHASE=drain_admission key=<key> reason=<why>``.

Hermetic: drives the real GatewayRunner methods in-process. No gateway process
is ever spawned (the fleet watchdog reaps ``hermes_cli.main gateway run``).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.fork_ext.drain_resume import (
    DISPOSITION_CANCEL,
    DISPOSITION_DRAIN,
    DISPOSITION_GONE,
    BootResumeRegistration,
    classify_boot_resume_at_shutdown,
    drain_admission_reason,
)
from gateway.run import _AGENT_PENDING_SENTINEL, GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner

_KEY = "agent:main:telegram:dm:123456:u1"


# --------------------------------------------------------------------------
# Pure predicate (no runner needed)
# --------------------------------------------------------------------------


def _registration(reason: str = "restart_interrupted") -> BootResumeRegistration:
    return BootResumeRegistration(
        session_key=_KEY, resume_reason=reason, scheduled_at=0.0
    )


def test_classify_pending_sentinel_is_cancelled():
    """The incident shape: slot still holds the sentinel => turn never began."""
    assert (
        classify_boot_resume_at_shutdown(
            _registration(),
            slot_value=_AGENT_PENDING_SENTINEL,
            pending_sentinel=_AGENT_PENDING_SENTINEL,
            task_done=False,
        )
        == DISPOSITION_CANCEL
    )


def test_classify_real_agent_is_drained():
    """A resume turn that genuinely started is ordinary in-flight work."""
    assert (
        classify_boot_resume_at_shutdown(
            _registration(),
            slot_value=object(),
            pending_sentinel=_AGENT_PENDING_SENTINEL,
            task_done=False,
        )
        == DISPOSITION_DRAIN
    )


def test_classify_empty_slot_is_already_finished():
    assert (
        classify_boot_resume_at_shutdown(
            _registration(),
            slot_value=None,
            pending_sentinel=_AGENT_PENDING_SENTINEL,
            task_done=True,
        )
        == DISPOSITION_GONE
    )


def test_drain_admission_reason_names_boot_resume():
    assert drain_admission_reason(is_boot_resume=True, is_internal=True) == "boot_resume"
    assert (
        drain_admission_reason(is_boot_resume=False, is_internal=True)
        == "internal_event"
    )
    assert (
        drain_admission_reason(is_boot_resume=False, is_internal=False)
        == "user_message"
    )


# --------------------------------------------------------------------------
# Runner-level contract
# --------------------------------------------------------------------------


def _runner_with_registry(monkeypatch):
    runner, adapter = make_restart_runner()
    marks: list[tuple[str, str]] = []

    def _mark(session_key, reason):
        marks.append((session_key, reason))
        return True

    runner.session_store.mark_resume_pending = _mark
    runner._pending_boot_resumes = {}
    runner._startup_resume_active = set()
    for name in (
        "_register_pending_boot_resume",
        "_clear_pending_boot_resume",
        "_session_has_pending_boot_resume",
        "_session_in_startup_resume",
        "_cancel_pending_boot_resumes_for_shutdown",
        "_log_drain_admission",
        "_session_state",
        "_peek_session_state",
        "_release_running_agent_state",
        "_adapter_for_source",
    ):
        setattr(runner, name, getattr(GatewayRunner, name).__get__(runner, GatewayRunner))
    return runner, adapter, marks


@pytest.mark.asyncio
async def test_pending_resume_is_cancelled_and_remarked_at_shutdown(monkeypatch):
    """(a) scheduled-but-unstarted resume => cancelled + re-marked ONCE.

    The re-mark must carry the ORIGINAL reason. In the incident the sessions
    came back on the next boot as ``shutdown_timeout kind=sibling`` — a
    second-generation mark — precisely because the pending resume was allowed
    to be interrupted by the drain instead of being deferred cleanly.
    """
    runner, _adapter, marks = _runner_with_registry(monkeypatch)

    async def _never_runs():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_never_runs())
    # Exactly what _schedule_resume_pending_sessions does: claim the slot with
    # the pending sentinel, then register.
    runner._session_state(_KEY).turn.agent = _AGENT_PENDING_SENTINEL
    runner._register_pending_boot_resume(_KEY, "restart_interrupted", task)
    assert runner._session_has_pending_boot_resume(_KEY)
    assert _KEY in runner._running_agents  # the drain WOULD have waited on this

    cancelled = await runner._cancel_pending_boot_resumes_for_shutdown()

    assert cancelled == 1
    # slot released -> the drain no longer counts it
    assert _KEY not in runner._running_agents
    # re-marked exactly once, under the ORIGINAL boot reason
    assert marks == [(_KEY, "restart_interrupted")]
    # registration consumed; a second shutdown pass must not double-mark
    assert not runner._session_has_pending_boot_resume(_KEY)
    assert await runner._cancel_pending_boot_resumes_for_shutdown() == 0
    assert marks == [(_KEY, "restart_interrupted")]
    # the wrapper task is cancelled (let the cancellation propagate)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_running_resume_turn_is_drained_not_cancelled(monkeypatch):
    """(b) an ALREADY-RUNNING resume turn is left alone for the normal drain."""
    runner, _adapter, marks = _runner_with_registry(monkeypatch)

    async def _still_running():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_still_running())
    runner._session_state(_KEY).turn.agent = _AGENT_PENDING_SENTINEL
    runner._register_pending_boot_resume(_KEY, "restart_interrupted", task)

    # The turn body starts: the chokepoint clears the registration and the
    # sentinel is promoted to a real agent.
    runner._clear_pending_boot_resume(_KEY)
    live_agent = SimpleNamespace(name="live-resume-agent")
    runner._session_state(_KEY).turn.agent = live_agent

    cancelled = await runner._cancel_pending_boot_resumes_for_shutdown()

    assert cancelled == 0
    # still in-flight: the drain must wait on it like any other turn
    assert runner._running_agents.get(_KEY) is live_agent
    assert marks == []  # NOT re-marked — the shutdown_mark path owns it
    assert not task.cancelled()
    task.cancel()


@pytest.mark.asyncio
async def test_registered_but_promoted_slot_is_drained(monkeypatch):
    """Belt-and-braces: a real agent in the slot wins even if the registry
    entry was never cleared (e.g. the chokepoint was bypassed)."""
    runner, _adapter, marks = _runner_with_registry(monkeypatch)

    async def _running():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_running())
    runner._register_pending_boot_resume(_KEY, "shutdown_timeout", task)
    runner._session_state(_KEY).turn.agent = SimpleNamespace(name="agent")

    assert await runner._cancel_pending_boot_resumes_for_shutdown() == 0
    assert marks == []
    task.cancel()


def test_drain_admission_line_fires_for_turn_started_during_drain(
    monkeypatch, caplog
):
    """(c) the detector names a turn admitted while shutdown is in progress."""
    runner, _adapter, _marks = _runner_with_registry(monkeypatch)
    runner._draining = True
    runner._startup_resume_active.add(_KEY)
    event = SimpleNamespace(internal=True)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        runner._log_drain_admission(_KEY, event)

    lines = [
        r.getMessage()
        for r in caplog.records
        if "PHASE=drain_admission" in r.getMessage()
    ]
    assert len(lines) == 1, lines
    assert f"key={_KEY}" in lines[0]
    assert "reason=boot_resume" in lines[0]


def test_drain_admission_line_silent_when_not_draining(monkeypatch, caplog):
    """No noise on the normal path — the line means 'shutdown was underway'."""
    runner, _adapter, _marks = _runner_with_registry(monkeypatch)
    runner._draining = False

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        runner._log_drain_admission(_KEY, SimpleNamespace(internal=True))

    assert not [
        r for r in caplog.records if "PHASE=drain_admission" in r.getMessage()
    ]


def test_drain_admission_classifies_a_user_message(monkeypatch, caplog):
    runner, _adapter, _marks = _runner_with_registry(monkeypatch)
    runner._draining = True

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        runner._log_drain_admission("agent:main:telegram:dm:999:u9",
                                    SimpleNamespace(internal=False))

    lines = [
        r.getMessage()
        for r in caplog.records
        if "PHASE=drain_admission" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "reason=user_message" in lines[0]


@pytest.mark.asyncio
async def test_cancelled_resume_is_recovered_exactly_once_on_next_boot(
    monkeypatch,
):
    """End of the loop: after cancel+re-mark, the NEXT boot's resume-candidate
    scan sees the session exactly once, with the original reason intact.

    This is the half that distinguishes 'deferred' from 'dropped'. A cancel
    without a durable re-mark would silently lose the interrupted work.
    """
    runner, _adapter, marks = _runner_with_registry(monkeypatch)

    async def _never():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_never())
    runner._session_state(_KEY).turn.agent = _AGENT_PENDING_SENTINEL
    runner._register_pending_boot_resume(_KEY, "restart_interrupted", task)
    await runner._cancel_pending_boot_resumes_for_shutdown()

    # Simulate the next boot reading the durable mark back.
    assert len(marks) == 1
    key, reason = marks[0]
    assert key == _KEY
    # NOT re-derived as shutdown_timeout — that re-derivation is exactly what
    # made every affected channel run the resume machinery a second time.
    assert reason == "restart_interrupted"
