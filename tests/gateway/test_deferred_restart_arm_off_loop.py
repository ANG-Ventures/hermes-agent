"""Behavioural contract: the deferred-restart arm must not pin the event loop.

The ratchet (``test_no_atomic_write_reachable_from_loop``) proves the shape is
gone from the call graph.  These tests prove the BEHAVIOUR that shape existed
to protect, driving the real ``DeferredRestartCoordinator`` against a stalled
``os.replace``:

* a coroutine concurrent with the arm keeps making progress, and
* the arm's durable effect still lands, and
* ``_run_as_leader``'s claim/commit writes do not pin the loop either.

Each asserts on observable INTERLEAVING (did the peer coroutine advance while
the write was blocked?), not on wall-clock thresholds, so they do not flake on
a loaded CI box.  Every one of them fails on the pre-fix inline shape: with the
write on the loop thread the peer cannot run at all until the replace returns.
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

import gateway.deferred_restart as deferred_restart
from gateway.deferred_restart import (
    DeferredRestartCoordinator,
    DeferredRestartRequest,
    submit_deferred_restart,
)

BOOT_ID = "boot-off-loop-test"
SESSION_KEY = "agent:main:telegram:dm:99"


class _StalledReplace:
    """Block the FIRST ``os.replace`` until explicitly released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._real = os.replace
        self._armed = True
        self.thread_names: list[str] = []
        self.stalled_dst: str | None = None

    def __call__(self, src, dst):
        self.thread_names.append(f"{threading.current_thread().name}:{dst}")
        # Only the CAS lifecycle rename (``<id>.<state>.json``) is the site
        # under test; the payload refresh's mkstemp rename is a different call.
        is_cas = ".armed." in str(dst) or ".claimed." in str(dst)
        if self._armed and is_cas:
            self._armed = False
            self.stalled_dst = str(dst)
            self.entered.set()
            # Bounded so a genuine deadlock fails the test instead of hanging
            # the suite; the passing path releases this immediately.
            self.release.wait(timeout=10.0)
        return self._real(src, dst)


@pytest.fixture()
def stalled_replace(monkeypatch):
    stall = _StalledReplace()
    monkeypatch.setattr(deferred_restart.os, "replace", stall)
    return stall


async def _peer_progress_while(stall: _StalledReplace, work):
    """Run ``work`` and count peer-coroutine ticks while the write is blocked.

    Returns ``(result, ticks_during_stall)``.  ``ticks_during_stall`` counts
    only iterations observed AFTER the blocking replace was entered and BEFORE
    it was released, so it is zero exactly when the loop was pinned.
    """
    ticks = 0
    stop = asyncio.Event()

    async def peer():
        nonlocal ticks
        while not stop.is_set():
            if stall.entered.is_set() and not stall.release.is_set():
                ticks += 1
                if ticks >= 5:
                    # Progress proven; let the write finish.
                    stall.release.set()
            await asyncio.sleep(0.001)

    async def watchdog():
        # If the loop IS pinned the peer never runs, so nothing would ever
        # release the stall. Release from a worker thread after a bounded wait
        # so the pre-fix shape FAILS on the assertion rather than hanging.
        await asyncio.to_thread(stall.entered.wait, 10.0)
        await asyncio.to_thread(stall.release.wait, 1.0)
        stall.release.set()

    peer_task = asyncio.create_task(peer())
    dog = asyncio.create_task(watchdog())
    try:
        result = await work()
    finally:
        stop.set()
        dog.cancel()
        await asyncio.gather(peer_task, dog, return_exceptions=True)
    return result, ticks


@pytest.mark.asyncio
async def test_arm_does_not_pin_the_loop_and_still_arms(tmp_path, stalled_replace):
    """The arm's CAS rename runs off-loop; the durable effect still lands."""
    submit_deferred_restart(
        tmp_path, session_key=SESSION_KEY, handoff="h", boot_id=BOOT_ID
    )
    coordinator = DeferredRestartCoordinator(tmp_path, boot_id=BOOT_ID)

    async def arm():
        return await asyncio.to_thread(
            coordinator.arm_for_session,
            SESSION_KEY,
            consume_breadcrumb=lambda _key: True,
        )

    state, ticks = await _peer_progress_while(stalled_replace, arm)

    assert state == "armed", state
    assert ticks >= 5, (
        "a peer coroutine made no progress while the arm's os.replace was "
        "blocked -- the event loop was pinned by the durable arm"
    )
    # The durable effect is real, not skipped: the request file is renamed.
    armed = [r for r in coordinator.scan() if r.state == "armed"]
    assert [r.session_key for r in armed] == [SESSION_KEY]
    # And the blocking call did not execute on the loop thread.
    on_loop = [
        name
        for name in stalled_replace.thread_names
        if name.startswith("MainThread")
        and (".armed." in name or ".claimed." in name or ".coalesce_pending." in name)
    ]
    assert not on_loop, f"a lifecycle CAS rename ran on the loop thread: {on_loop}"


@pytest.mark.asyncio
async def test_leader_claim_transition_does_not_pin_the_loop(
    tmp_path, stalled_replace
):
    """``_run_as_leader``'s claim rename is off-loop too."""
    request = submit_deferred_restart(
        tmp_path, session_key=SESSION_KEY, handoff="h", boot_id=BOOT_ID
    )
    coordinator = DeferredRestartCoordinator(tmp_path, boot_id=BOOT_ID)
    # Arm it without tripping the stall (the stall is one-shot and we want it
    # to fire on the LEADER's claim).
    real = stalled_replace._real
    armed_path = request.path.with_name(f"{request.request_id}.armed.json")
    real(request.path, armed_path)
    armed = DeferredRestartRequest.load(armed_path)
    # The leader waits for every same-boot armed/claimed peer to have passed
    # its delivery barrier before committing. This request IS that peer.
    coordinator._delivery_ready.add(armed.request_id)

    signalled: list[bool] = []

    async def lead():
        return await coordinator._run_as_leader(
            armed,
            record_replay=lambda _r: None,
            mark_self=lambda _r: True,
            signal_restart=lambda: signalled.append(True),
            checkpoint=None,
        )

    outcome, ticks = await _peer_progress_while(stalled_replace, lead)

    assert outcome == "signaled", outcome
    assert signalled == [True]
    assert ticks >= 5, (
        "a peer coroutine made no progress while the leader's claim "
        "os.replace was blocked -- _run_as_leader pinned the event loop"
    )
    on_loop = [
        name
        for name in stalled_replace.thread_names
        if name.startswith("MainThread")
        and (".armed." in name or ".claimed." in name or ".coalesce_pending." in name)
    ]
    assert not on_loop, f"a lifecycle CAS rename ran on the loop thread: {on_loop}"


@pytest.mark.asyncio
async def test_runner_arm_dispatch_does_not_pin_the_loop(tmp_path, stalled_replace):
    """The PRODUCTION entry point (`GatewayRunner._arm_deferred_restart_after_release`).

    The prior two tests prove the coordinator is safe to call from a worker
    thread; this one proves the runner actually DOES so.  Every caller of
    ``_release_running_agent_state`` reaches the arm through this method, so a
    regression that puts the arm back inline is caught here even if the
    coordinator itself stays thread-safe.
    """
    import gateway.run as run

    submit_deferred_restart(
        tmp_path, session_key=SESSION_KEY, handoff="h", boot_id=BOOT_ID
    )
    coordinator = DeferredRestartCoordinator(tmp_path, boot_id=BOOT_ID)

    runner = object.__new__(run.GatewayRunner)
    runner._deferred_restart_coordinator = coordinator
    runner._background_tasks = set()
    runner._consume_restart_initiated_breadcrumb = lambda _key: True
    runner._get_deferred_restart_coordinator = lambda: coordinator
    runner._adapter_for_source = lambda _source: None
    runner._record_restart_replay_mark = lambda *a, **k: False
    runner.request_restart = lambda **k: None

    class _Store:
        _entries: dict = {}

        def mark_resume_pending(self, *a, **k):
            return True

    runner.session_store = _Store()

    scheduled: list[str] = []
    real_schedule = coordinator.schedule_armed

    def _schedule_spy(session_key, **kwargs):
        scheduled.append(session_key)
        return real_schedule(session_key, **kwargs)

    coordinator.schedule_armed = _schedule_spy  # type: ignore[method-assign]

    async def arm():
        runner._arm_deferred_restart_after_release(SESSION_KEY, generation=None)
        # The dispatch is fire-and-forget; wait for its task to finish.
        for _ in range(2000):
            if scheduled:
                break
            await asyncio.sleep(0.005)
        return "done"

    _outcome, ticks = await _peer_progress_while(stalled_replace, arm)

    assert scheduled == [SESSION_KEY], (
        f"the runner never scheduled the armed request: {scheduled}"
    )
    assert ticks >= 5, (
        "a peer coroutine made no progress while the runner's arm was "
        "blocked -- _arm_deferred_restart_after_release pinned the event loop"
    )
    loop_thread_calls = [
        name
        for name in stalled_replace.thread_names
        if name.startswith("MainThread")
        and (".armed." in name or ".claimed." in name or ".coalesce_pending." in name)
    ]
    assert not loop_thread_calls, (
        f"a lifecycle CAS rename ran on the loop thread: {loop_thread_calls}"
    )


@pytest.mark.asyncio
async def test_delivery_barrier_is_registered_before_the_arm_returns(tmp_path):
    """Moving the arm off-loop must not move the barrier registration with it.

    ``acknowledge_response_delivery`` is a ONE-SHOT lookup fired right after
    the turn's final send.  If the callback is only registered once the
    worker-thread arm completes, a fast ack finds no callback, is dropped, and
    the armed task then sits on its 30s delivery barrier waiting for a
    delivery that already happened.  This regression pins that the barrier is
    in place by the time ``_arm_deferred_restart_after_release`` RETURNS --
    i.e. before the caller can send and ack.
    """
    import gateway.run as run

    submit_deferred_restart(
        tmp_path, session_key=SESSION_KEY, handoff="h", boot_id=BOOT_ID
    )
    coordinator = DeferredRestartCoordinator(tmp_path, boot_id=BOOT_ID)

    registered: list[str] = []
    slow_arm_entered = threading.Event()
    release_arm = threading.Event()

    class _Adapter:
        def register_delivery_ack_callback(self, session_key, _cb, *, generation=None):
            registered.append(session_key)

        def cancel_delivery_ack_callback(self, session_key):
            return False

    real_arm = coordinator.arm_for_session

    def _slow_arm(*args, **kwargs):
        slow_arm_entered.set()
        release_arm.wait(timeout=10.0)
        return real_arm(*args, **kwargs)

    coordinator.arm_for_session = _slow_arm  # type: ignore[method-assign]

    runner = object.__new__(run.GatewayRunner)
    runner._background_tasks = set()
    runner._consume_restart_initiated_breadcrumb = lambda _key: True
    runner._get_deferred_restart_coordinator = lambda: coordinator
    runner._adapter_for_source = lambda _source: _Adapter()
    runner._record_restart_replay_mark = lambda *a, **k: False
    runner.request_restart = lambda **k: None

    class _Entry:
        origin = object()

    class _Store:
        _entries = {SESSION_KEY: _Entry()}

        def mark_resume_pending(self, *a, **k):
            return True

    runner.session_store = _Store()

    runner._arm_deferred_restart_after_release(SESSION_KEY, generation=None)

    # The arm has NOT completed yet (it is parked on release_arm), but the
    # barrier must already be registered.
    assert registered == [SESSION_KEY], (
        "the delivery barrier was not registered before the arm returned -- a "
        "final-send ack fired now would be dropped and the armed task would "
        "block on its 30s barrier"
    )

    release_arm.set()
    await asyncio.gather(*runner._background_tasks, return_exceptions=True)
