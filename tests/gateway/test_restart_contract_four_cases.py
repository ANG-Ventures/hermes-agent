"""The restart-continuity CONTRACT, per session role, driven through the real
``GatewayRunner.stop()`` drain (no LLM, no adapters, hermetic).

Operator ruling (2026-09-19), four cases:

  1. CALLER   — the session that initiated the restart is re-prompted after the
                boot (its shutdown mark is ``restart_consumed`` /
                ``restart_consumed_interrupted``; the re-prompt itself rides the
                SELF dropbox request, covered by test_deferred_restart_taxonomy).
  2. BUSY     — a sibling turn still running when the drain times out is marked
                ``shutdown_timeout`` and the mark SURVIVES the stop, so the next
                boot resumes it (auto / always).
  3. STOPPED  — a sibling the user ``/stop``ped stays stopped: released before the
                drain => never marked; released DURING the drain => the pre-drain
                hedge is CLEARED at drain end. Either way nothing resumes.
  4. IDLE     — a session with no running turn is never touched.

Every assertion is on the session_store calls the real stop() makes, so a merge
that drops the pre-drain hedge, the clean-drain clear pass, the timeout mark, or
the caller-reason resolution turns this file red.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner

CALLER = "agent:main:telegram:dm:CALLER"
BUSY = "agent:main:discord:group:BUSY:9"
STOPPED_DURING = "agent:main:discord:group:STOPPED_DURING:9"
STOPPED_BEFORE = "agent:main:discord:group:STOPPED_BEFORE:9"
IDLE = "agent:main:telegram:dm:IDLE"


def _wire_store(runner):
    """The runner's ``async_session_store`` is a read-only facade over
    ``session_store`` (AsyncSessionStore delegates to the sync methods), so one
    sync mock records BOTH the pre-drain marks and the drain-end clears."""
    store = MagicMock()
    store._entries = {}
    store.mark_resume_pending = MagicMock(return_value=True)
    store.clear_resume_pending = MagicMock(return_value=True)
    runner.session_store = store
    return store, None


def _marks(store):
    """{key: [reasons...]} in call order."""
    out: dict[str, list[str]] = {}
    for call in store.mark_resume_pending.call_args_list:
        out.setdefault(call.args[0], []).append(call.args[1])
    return out


def _clears(store, _astore=None):
    return {c.args[0] for c in store.clear_resume_pending.call_args_list}


async def _release_after(runner, key, delay):
    """Model a turn ending mid-drain: the caller finishing its reply, or a user
    ``/stop`` (which releases the running-agent slot)."""
    await asyncio.sleep(delay)
    del runner._running_agents[key]


@pytest.mark.asyncio
async def test_four_cases_through_the_real_drain():
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.5          # BUSY never finishes -> times out
    store, astore = _wire_store(runner)

    runner._running_agents = {
        CALLER: MagicMock(),
        BUSY: MagicMock(),
        STOPPED_DURING: MagicMock(),
    }
    runner._session_initiated_restart = {CALLER: True}
    # STOPPED_BEFORE and IDLE exist as sessions but have no running turn.
    store._entries = {k: MagicMock(resume_reason=None) for k in
                      (CALLER, BUSY, STOPPED_DURING, STOPPED_BEFORE, IDLE)}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        stop_task = asyncio.ensure_future(runner.stop())
        # the caller's reply lands, and a user /stop releases another slot, mid-drain
        await asyncio.gather(
            _release_after(runner, CALLER, 0.05),
            _release_after(runner, STOPPED_DURING, 0.1),
        )
        await stop_task

    marks = _marks(store)
    cleared = _clears(store, astore)

    # (1) CALLER: hedged with the CALLER reason (never a sibling reason), and since
    #     its turn finished cleanly during the drain the hedge is cleared — the
    #     re-prompt is the watcher's SELF dropbox request, not a stale hedge.
    assert marks[CALLER] == ["restart_consumed"], marks
    assert CALLER in cleared

    # (2) BUSY: hedged, then marked INTERRUPTED at timeout with the sibling reason,
    #     and NOT cleared — this is the mark the next boot resumes on.
    assert marks[BUSY][0] == "shutdown_timeout" and marks[BUSY][-1] == "shutdown_timeout"
    assert len(marks[BUSY]) >= 2, "timeout path must re-mark the still-running sibling"
    assert BUSY not in cleared

    # (3) STOPPED: /stop during the drain -> hedge cleared; /stop before -> never marked.
    assert marks[STOPPED_DURING] == ["shutdown_timeout"]
    assert STOPPED_DURING in cleared
    assert STOPPED_BEFORE not in marks and STOPPED_BEFORE not in cleared

    # (4) IDLE: untouched.
    assert IDLE not in marks and IDLE not in cleared


@pytest.mark.asyncio
async def test_caller_still_running_at_timeout_gets_the_interrupted_caller_reason():
    """Case 1 variant: the initiating turn was cut by the drain. It must carry
    ``restart_consumed_interrupted`` (in _AUTO_RESUME_REASONS, so it auto-resumes)
    and never the bare ``restart_consumed`` (deliberately NOT in the allow-list,
    to break the restart->resume->restart cascade)."""
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.2
    store, astore = _wire_store(runner)
    runner._running_agents = {CALLER: MagicMock()}
    runner._session_initiated_restart = {CALLER: True}
    store._entries = {CALLER: MagicMock(resume_reason=None)}
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()
    marks = _marks(store)
    assert marks[CALLER][0] == "restart_consumed"
    assert marks[CALLER][-1] == "restart_consumed_interrupted"
    assert CALLER not in _clears(store, astore)


@pytest.mark.asyncio
async def test_clean_drain_clears_every_hedge_and_marks_nothing_interrupted():
    """All siblings finish inside the budget: every pre-drain hedge is released,
    no session carries an interrupted mark into the next boot (nothing resumes)."""
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.5
    store, astore = _wire_store(runner)
    runner._running_agents = {BUSY: MagicMock(), STOPPED_DURING: MagicMock()}
    store._entries = {k: MagicMock(resume_reason=None) for k in (BUSY, STOPPED_DURING, IDLE)}
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        stop_task = asyncio.ensure_future(runner.stop())
        await asyncio.gather(
            _release_after(runner, BUSY, 0.05),
            _release_after(runner, STOPPED_DURING, 0.05),
        )
        await stop_task
    marks = _marks(store)
    assert all(v == ["shutdown_timeout"] for v in marks.values()), marks
    assert _clears(store, astore) == {BUSY, STOPPED_DURING}
    assert IDLE not in marks
