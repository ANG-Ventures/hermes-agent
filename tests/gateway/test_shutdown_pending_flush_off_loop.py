"""The shutdown pending-message flush must not block the event loop.

``BasePlatformAdapter.cancel_background_tasks`` and the runner's
``_stop_impl_body`` both called ``flush_pending_to_file`` inline, immediately
before ``_pending_messages.clear()``.  That call runs one ``_write_payload``
per pending session, each ending in the ``mkstemp`` + ``fsync`` +
``os.replace`` tail of the atomic writer, whose duration is unbounded under
filesystem pressure -- and both callers are coroutines.  Measured on the
pre-fix shape with ``os.replace`` held for 0.30s, the loop ticked **0** times
while the rename was held; on the off-loop lane, 31.

That is the class the reachability ratchet
(``tests/gateway/test_no_atomic_write_reachable_from_loop.py``) froze as
``gateway/platforms/base.py cancel_background_tasks -> atomic_json_write``.

These tests pin the fix without wall-clock thresholds -- the rename is held on
a real barrier and the loop must make progress anyway -- and, just as
importantly, they pin the two durability properties the offload must not
trade away.  The payloads here are the ONLY surviving copy of user messages
the DB could not persist (#72680) and the caller clears the dict the instant
the await returns, so "moved off the loop" is worthless if the write can be
dropped:

* the flush must not be queued behind the loop's *shared default* executor
  (what ``asyncio.to_thread`` would use), because the shutdown path is when
  that pool is busiest;
* a deadline cancellation must still surface as ``CancelledError`` -- the
  shutdown deadline depends on it -- while the snapshotted payload still
  reaches disk.
"""
import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway import shutdown_flush


@pytest.fixture()
def flush_home(tmp_path, monkeypatch):
    """Point the pending-message flush dir at an isolated HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_constants
    monkeypatch.setattr(
        hermes_constants, "get_hermes_home", lambda: tmp_path, raising=True
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _drain_lane_between_tests():
    """No test may leave a shielded write queued for the next one."""
    yield
    shutdown_flush.fence_flush_lane(timeout=10.0)


def _payload_files(home):
    d = home / "pending_messages"
    return sorted(d.glob("pending-*.json")) if d.exists() else []


class _HeldReplace:
    """Replace ``os.replace`` with one that blocks until released."""

    def __init__(self, monkeypatch):
        self._gate = threading.Event()
        self._entered = threading.Event()
        self._real = os.replace
        monkeypatch.setattr(os, "replace", self._blocking, raising=True)

    def _blocking(self, src, dst, *a, **kw):
        self._entered.set()
        self._gate.wait(timeout=10.0)
        return self._real(src, dst, *a, **kw)

    def wait_until_entered(self, timeout=5.0):
        return self._entered.wait(timeout)

    @property
    def entered(self):
        return self._entered.is_set()

    def release(self):
        self._gate.set()


def _occupy_lane(release: threading.Event) -> threading.Event:
    """Fill the single-worker lane so the next submit sits QUEUED.

    Returns an event set once the occupant is genuinely running, so callers
    can be sure the lane is busy rather than racing it.
    """
    started = threading.Event()

    def _hold():
        started.set()
        release.wait(timeout=30.0)

    shutdown_flush._get_flush_lane().submit(_hold)
    assert started.wait(timeout=5.0), "lane occupant never started"
    return started


# ---------------------------------------------------------------------------
# 1. Loop liveness -- the reason the fix exists.
# ---------------------------------------------------------------------------


def test_flush_does_not_block_the_loop(flush_home, monkeypatch):
    """A stalled rename must not stop the loop from running other tasks.

    No stopwatch threshold: the rename is held on a barrier and released
    from an INDEPENDENT OS thread (never from the loop, which the pre-fix
    shape would have stalled), and the loop must advance a sibling task
    while the hold is in force.  Measured: 0 ticks inline, 31+ on the lane.
    """
    held = _HeldReplace(monkeypatch)

    async def scenario():
        ticks = 0
        running = True

        async def ticker():
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0.01)

        sib = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        before = ticks

        # The release is driven off-loop: if the loop were blocked, nothing
        # on it could ever let the rename finish.
        def _release_after_hold():
            held.wait_until_entered(timeout=5.0)
            time.sleep(0.3)
            held.release()

        threading.Thread(target=_release_after_hold, daemon=True).start()

        flushed = await shutdown_flush.flush_pending_to_file_async(
            {"sess-a": "unpersisted text"}, reason="adapter_shutdown"
        )
        during = ticks - before
        running = False
        sib.cancel()
        return flushed, during

    flushed, during = asyncio.run(scenario())
    assert held.entered, "the rename was never reached; this test is vacuous"
    assert flushed == 1
    assert during > 5, (
        f"the loop ticked {during} times while the rename was held: the "
        "flush is running ON the event loop"
    )
    assert len(_payload_files(flush_home)) == 1


# ---------------------------------------------------------------------------
# 2. Durability -- what the offload must not trade away.
# ---------------------------------------------------------------------------


def test_payload_is_durable_before_the_caller_resumes(flush_home):
    """Both callers ``.clear()`` the dict the instant the await returns."""

    pending = {"sess-a": "unpersisted text", "sess-b": "more"}

    async def scenario():
        flushed = await shutdown_flush.flush_pending_to_file_async(
            pending, reason="adapter_shutdown"
        )
        # The contract the callers rely on: by the time control comes back,
        # the payloads are on disk, so clearing is safe.
        on_disk = len(_payload_files(flush_home))
        pending.clear()
        return flushed, on_disk

    flushed, on_disk = asyncio.run(scenario())
    assert flushed == 2
    assert on_disk == 2, (
        "the caller resumed before the payloads were durable; "
        "clear() would have destroyed the only surviving copy"
    )


def test_caller_mutation_while_the_flush_is_queued_cannot_corrupt_the_payload(
    flush_home
):
    """The worker must own a snapshot, not a reference to the caller's dict.

    ``base.py`` passes the live ``self._pending_messages`` and clears it the
    instant the await returns.  The dangerous window is while the work is
    QUEUED: a lane body holding a reference would serialise whatever the
    dict contains when it finally runs, which by then is empty.  So the
    lane is deliberately occupied first -- with the work already running,
    serialisation has happened and the bug is invisible.
    """
    release = threading.Event()
    _occupy_lane(release)
    pending = {"sess-a": "unpersisted text"}

    async def scenario():
        task = asyncio.create_task(
            shutdown_flush.flush_pending_to_file_async(
                pending, reason="adapter_shutdown"
            )
        )
        # Let the coroutine reach its submit, then mutate while it is queued.
        await asyncio.sleep(0.1)
        pending.clear()
        release.set()
        return await asyncio.wait_for(task, timeout=10.0)

    try:
        flushed = asyncio.run(scenario())
    finally:
        release.set()

    assert flushed == 1, (
        "the queued flush read the caller's live dict after it was cleared; "
        "it must capture a snapshot at submit time"
    )
    files = _payload_files(flush_home)
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["session_key"] == "sess-a"
    assert payload["data"] == {"text": "unpersisted text"}


def test_flush_is_not_queued_behind_the_shared_default_executor(flush_home):
    """The lane must be isolated from the loop's default executor.

    ``asyncio.to_thread`` dispatches onto the loop's *default*
    ``ThreadPoolExecutor``, shared with every other ``to_thread`` caller in
    the process -- and the shutdown path is exactly when that pool is
    busiest.  With it saturated, a ``to_thread``-based flush had not even
    STARTED when the per-adapter deadline fired, and the payload never
    reached disk.  Regression for that shape.
    """
    release = threading.Event()

    def _hog():
        release.wait(timeout=30.0)

    async def scenario():
        loop = asyncio.get_running_loop()
        saturated = ThreadPoolExecutor(max_workers=2)
        loop.set_default_executor(saturated)
        for _ in range(2):
            saturated.submit(_hog)
        # Let both hogs genuinely occupy their threads.
        await asyncio.sleep(0.2)

        try:
            return await asyncio.wait_for(
                shutdown_flush.flush_pending_to_file_async(
                    {"sess-a": "unpersisted text"}, reason="adapter_shutdown"
                ),
                timeout=5.0,
            )
        finally:
            release.set()
            saturated.shutdown(wait=True)

    flushed = asyncio.run(scenario())
    assert flushed == 1
    assert len(_payload_files(flush_home)) == 1, (
        "the flush was starved by unrelated work on the shared default "
        "executor; it must own an isolated lane"
    )


# ---------------------------------------------------------------------------
# 3. Cancellation -- the await point the synchronous form never had.
# ---------------------------------------------------------------------------


def test_deadline_cancel_still_lands_a_queued_payload(flush_home):
    """``cancel_background_tasks()`` is cancelled at the adapter deadline.

    ``_bounded_adapter_teardown`` awaits it through
    ``_await_adapter_cleanup_with_timeout``, which ``task.cancel()``s the
    coroutine when the per-adapter budget expires.  Pre-fix the flush was a
    plain synchronous call with no await point inside it, so the payloads
    were always on disk before cancellation could be observed; the offload
    introduces that await point, and this data has no second copy (#72680).

    The lane is occupied first so the flush is cancelled while still
    QUEUED -- the only window in which an unshielded await would actually
    cancel the pending future and discard the write.  Once the lane body
    has started, cancelling the awaiter cannot stop the thread either way,
    so testing that window would be vacuous.
    """
    release = threading.Event()
    _occupy_lane(release)
    pending = {"sess-a": "the only surviving copy"}

    async def scenario():
        async def teardown():
            # The shape of cancel_background_tasks()'s flush-then-clear tail.
            try:
                await shutdown_flush.flush_pending_to_file_async(
                    pending, reason="adapter_shutdown"
                )
            finally:
                pending.clear()

        task = asyncio.create_task(teardown())
        # Let it submit and start awaiting, then fire the deadline while the
        # work is still sitting in the lane's queue.
        await asyncio.sleep(0.1)
        task.cancel()
        outcome = "completed"
        try:
            await task
        except asyncio.CancelledError:
            outcome = "CancelledError"
        except BaseException as exc:  # noqa: BLE001
            outcome = type(exc).__name__
        return outcome

    try:
        outcome = asyncio.run(scenario())
    finally:
        release.set()

    # Nobody is awaiting the lane any more; the exit fence is what
    # guarantees the write lands, so use it here.
    assert shutdown_flush.fence_flush_lane(timeout=10.0)

    assert outcome == "CancelledError", (
        "cancellation must propagate unchanged as CancelledError, or the "
        "shutdown deadline stops behaving as it did pre-offload"
    )
    assert len(_payload_files(flush_home)) == 1, (
        "a cancelled flush dropped a queued payload; these messages have "
        "no second copy (#72680) -- the lane work must be shielded"
    )


def test_cancellation_propagates_as_cancelled_error_not_the_worker_error(
    flush_home, monkeypatch
):
    """A cancelled awaiter must never see the worker's own exception.

    Replacing ``CancelledError`` with e.g. a ``RuntimeError`` makes
    ``cancel_background_tasks()``'s caller proceed through teardown instead
    of unwinding.
    """
    def _boom(*a, **kw):
        raise RuntimeError("payload write failed")

    monkeypatch.setattr(shutdown_flush, "_write_payload", _boom, raising=True)

    async def scenario():
        task = asyncio.create_task(
            shutdown_flush.flush_pending_to_file_async(
                {"sess-a": "x"}, reason="adapter_shutdown"
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
            return "completed"
        except asyncio.CancelledError:
            return "CancelledError"
        except BaseException as exc:  # noqa: BLE001
            return type(exc).__name__

    assert asyncio.run(scenario()) == "CancelledError"


def test_a_failing_write_is_best_effort_and_never_breaks_shutdown(
    flush_home, monkeypatch
):
    """The sync form swallows per-session failures; the async form must too."""

    def _boom(*a, **kw):
        raise RuntimeError("payload write failed")

    monkeypatch.setattr(shutdown_flush, "_write_payload", _boom, raising=True)

    flushed = asyncio.run(
        shutdown_flush.flush_pending_to_file_async(
            {"sess-a": "x"}, reason="adapter_shutdown"
        )
    )
    assert flushed == 0
    assert _payload_files(flush_home) == []


def test_empty_pending_is_a_noop(flush_home):
    assert asyncio.run(shutdown_flush.flush_pending_to_file_async({})) == 0
    assert _payload_files(flush_home) == []


# ---------------------------------------------------------------------------
# 4. Non-vacuity: the lane must actually be a lane.
# ---------------------------------------------------------------------------


def test_flush_runs_on_its_own_named_lane_not_the_caller_thread(flush_home):
    """Pins that the write really left the calling thread, and where it went.

    Without this, every assertion above would still pass if the offload
    silently degraded back to running inline.
    """
    seen = []
    real = shutdown_flush._write_payload

    def _record(flush_dir, payload):
        seen.append(threading.current_thread().name)
        return real(flush_dir, payload)

    async def scenario():
        caller = threading.current_thread().name
        shutdown_flush._write_payload = _record
        try:
            await shutdown_flush.flush_pending_to_file_async(
                {"sess-a": "x"}, reason="adapter_shutdown"
            )
        finally:
            shutdown_flush._write_payload = real
        return caller

    caller = asyncio.run(scenario())
    assert seen, "the write never happened; this test would be vacuous"
    assert all(name != caller for name in seen), (
        f"the write ran on the calling thread {caller!r}: the offload "
        "silently degraded back to inline"
    )
    assert all(name.startswith("shutdown-flush") for name in seen), (
        f"the write ran off-loop but not on the dedicated lane: {seen}"
    )
