"""t_8d085477: the gateway shutdown's cron interrupt-mark must not deadlock
against a cron job that holds its own fire fence while waiting on the loop.

Incident shape (4 of 8 shutdown-watchdog force-exits, 2026-09-24/25): the
event loop thread was parked in ``_fire_job_lock`` under
``mark_running_jobs_interrupted`` while the cron job thread held that fence
across delivery, and the delivery was ``run_coroutine_threadsafe(...,
loop).result(timeout=60)`` — waiting on the very loop that was blocked.
"""

import asyncio
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()


def _claimed_running_job(tmp_path):
    """A real job in a real store, claimed and registered as in flight."""
    import cron.jobs as jobs
    import cron.scheduler as sched

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    with jobs.use_cron_store(profile_home):
        created = jobs.create_job(prompt="x", schedule="every 5m", name="fenced")
        claimed = jobs.claim_job_for_fire(created["id"], force=True, return_job=True)
    assert isinstance(claimed, dict)
    owner = claimed["fire_claim"]["by"]
    sched._running_job_ids.add(created["id"])
    sched._running_fire_owners[created["id"]] = {object(): (owner, profile_home)}
    return created["id"], owner, profile_home


def _hold_fence(job_id, owner, profile_home, *, release: threading.Event,
                held: threading.Event, before_release=None):
    import cron.jobs as jobs

    with jobs.use_cron_store(profile_home):
        with jobs.fire_claim_fence(job_id, expected_owner=owner) as owns:
            assert owns
            held.set()
            if before_release is not None:
                before_release()
            release.wait(10)


def test_bounded_mark_fails_closed_fast_while_fence_is_held(tmp_path):
    import cron.scheduler as sched

    job_id, owner, home = _claimed_running_job(tmp_path)
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(
        target=_hold_fence, args=(job_id, owner, home),
        kwargs={"release": release, "held": held}, daemon=True,
    )
    t.start()
    assert held.wait(5)
    try:
        started = time.monotonic()
        marked = sched.mark_running_jobs_interrupted("shutdown", lock_timeout=0.3)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        t.join(5)

    assert marked == []
    assert elapsed < 2.0, elapsed
    # The in-memory flag is still recorded: run_one_job must never report
    # the truncated run as a success even though the persisted write failed.
    assert sched._interrupted_job_ids


def test_unbounded_mark_keeps_legacy_wait_until_release(tmp_path):
    import cron.scheduler as sched

    job_id, owner, home = _claimed_running_job(tmp_path)
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(
        target=_hold_fence, args=(job_id, owner, home),
        kwargs={"release": release, "held": held}, daemon=True,
    )
    t.start()
    assert held.wait(5)
    threading.Timer(0.5, release.set).start()
    started = time.monotonic()
    marked = sched.mark_running_jobs_interrupted("shutdown")
    elapsed = time.monotonic() - started
    t.join(5)

    assert marked == [job_id]
    assert elapsed >= 0.4, elapsed


def test_fence_holder_waiting_on_loop_does_not_deadlock_off_loop_mark(tmp_path):
    """The gateway shape: the holder's delivery needs the loop. Marking from
    a worker thread (asyncio.to_thread) leaves the loop free, the delivery
    completes, the fence is released and the mark SUCCEEDS. The same call
    made synchronously on the loop can only fail closed at its timeout."""
    import cron.scheduler as sched

    job_id, owner, home = _claimed_running_job(tmp_path)

    async def _scenario(off_loop: bool):
        loop = asyncio.get_running_loop()
        held, release = threading.Event(), threading.Event()

        async def _send():
            await asyncio.sleep(0.2)
            return "sent"

        def _deliver_on_loop():
            fut = asyncio.run_coroutine_threadsafe(_send(), loop)
            try:
                fut.result(timeout=3)
            except Exception:
                pass
            release.set()

        t = threading.Thread(
            target=_hold_fence, args=(job_id, owner, home),
            kwargs={"release": release, "held": held,
                    "before_release": _deliver_on_loop},
            daemon=True,
        )
        t.start()
        while not held.is_set():
            await asyncio.sleep(0.01)
        started = time.monotonic()
        if off_loop:
            marked = await asyncio.to_thread(
                sched.mark_running_jobs_interrupted, "shutdown", lock_timeout=2.0
            )
        else:
            marked = sched.mark_running_jobs_interrupted(
                "shutdown", lock_timeout=0.5
            )
        elapsed = time.monotonic() - started
        await asyncio.to_thread(t.join, 5)
        return marked, elapsed

    on_loop_marked, on_loop_elapsed = asyncio.run(_scenario(off_loop=False))
    assert on_loop_marked == []  # deadlock shape: bounded, but cannot succeed
    assert on_loop_elapsed < 2.0

    sched._interrupted_job_ids.clear()
    off_loop_marked, off_loop_elapsed = asyncio.run(_scenario(off_loop=True))
    assert off_loop_marked == [job_id]
    assert off_loop_elapsed < 2.0
