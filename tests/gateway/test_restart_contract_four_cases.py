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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner

CALLER = "agent:main:telegram:dm:CALLER"
BUSY = "agent:main:discord:group:BUSY:9"
STOPPED_DURING = "agent:main:discord:group:STOPPED_DURING:9"
STOPPED_BEFORE = "agent:main:discord:group:STOPPED_BEFORE:9"
IDLE = "agent:main:telegram:dm:IDLE"


def _wire_store(runner, release_after_mark=()):
    """The runner's ``async_session_store`` is a read-only facade over
    ``session_store`` (AsyncSessionStore delegates to the sync methods), so one
    sync mock records BOTH the pre-drain marks and the drain-end clears.

    ``release_after_mark``: session keys whose running slot is released during
    the drain window — modelling a turn that finishes (or a user ``/stop``)
    after the pre-drain hedge marked it.

    The release is driven by TWO ORDERING EVENTS, never a wall clock:

    1. the hedge ``mark_resume_pending(key, ...)`` for that key must have
       happened (``_queued``), and
    2. ``stop()`` must have reached the drain — witnessed by the drain's own
       forced ``_update_runtime_status("draining")``, which
       ``_drain_active_agents`` emits after the hedge loop and before it waits.

    Both halves are load-bearing. A ``loop.call_later(0.05, ...)`` release (and
    before it an ``asyncio.sleep(0.05)`` task) is a stopwatch racing the 0.5s
    drain deadline from the other side: on a CFS-capped merge-queue runner the
    release landed BEFORE stop() reached the hedge loop, so the caller was never
    marked at all and the test died on ``KeyError: ...:CALLER`` (3 of 3
    merge_group runs for #746, slice 15/16, 2026-09-20). Keying on the two
    events makes the interleaving the contract needs independent of scheduling.

    ``store.release_order`` records the marks and releases in the order they
    actually happened, so a regression back to release-before-mark is an
    assertable fact rather than an intermittent KeyError.
    """
    store = MagicMock()
    store._entries = {}
    queued: set[str] = set()
    released: set[str] = set()
    store.release_order = []

    def _release_queued() -> None:
        """Drop every queued-and-marked slot. Called from the drain's own tick."""
        for key in sorted(queued - released):
            released.add(key)
            if key in runner._running_agents:
                del runner._running_agents[key]
                store.release_order.append(f"release:{key}")

    def _mark(key, reason, *a, **k):
        store.release_order.append(f"mark:{key}")
        if key in release_after_mark:
            queued.add(key)
        return True

    def _status(state=None, *a, **k):
        # The drain emits this once (force=True) after the hedge loop and before
        # it starts waiting — the exact window a mid-drain completion lands in.
        if state == "draining":
            _release_queued()

    store.mark_resume_pending = MagicMock(side_effect=_mark)
    store.clear_resume_pending = MagicMock(return_value=True)
    runner.session_store = store
    runner._update_runtime_status = MagicMock(side_effect=_status)
    return store, None


def _marks(store):
    """{key: [reasons...]} in call order."""
    out: dict[str, list[str]] = {}
    for call in store.mark_resume_pending.call_args_list:
        out.setdefault(call.args[0], []).append(call.args[1])
    return out


def _clears(store, _astore=None):
    return {c.args[0] for c in store.clear_resume_pending.call_args_list}



@pytest.mark.asyncio
async def test_four_cases_through_the_real_drain():
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.5          # BUSY never finishes -> times out
    # the caller's reply lands, and a user /stop releases another slot, mid-drain
    store, astore = _wire_store(runner, release_after_mark=(CALLER, STOPPED_DURING))

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
        await runner.stop()

    marks = _marks(store)
    cleared = _clears(store, astore)

    # (0) NON-VACUITY / ordering witness: the interleaving every assertion below
    #     depends on actually happened — both mid-drain slots were hedged BEFORE
    #     they were released. The load flake this replaces was exactly the
    #     opposite order (release landed first, the key was never marked), which
    #     surfaced as an opaque KeyError instead of a named failure.
    for _key in (CALLER, STOPPED_DURING):
        assert f"mark:{_key}" in store.release_order, store.release_order
        assert f"release:{_key}" in store.release_order, store.release_order
        assert store.release_order.index(f"mark:{_key}") < store.release_order.index(
            f"release:{_key}"
        ), f"{_key} released before it was hedged: {store.release_order}"
    assert f"release:{BUSY}" not in store.release_order, store.release_order

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
    store, astore = _wire_store(runner, release_after_mark=(BUSY, STOPPED_DURING))
    runner._running_agents = {BUSY: MagicMock(), STOPPED_DURING: MagicMock()}
    store._entries = {k: MagicMock(resume_reason=None) for k in (BUSY, STOPPED_DURING, IDLE)}
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()
    marks = _marks(store)
    assert all(v == ["shutdown_timeout"] for v in marks.values()), marks
    assert _clears(store, astore) == {BUSY, STOPPED_DURING}
    assert IDLE not in marks
