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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway import shutdown_flush
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult


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

    # Nobody is awaiting the lane any more.  Fence here so the assertion
    # below is about the SHIELD (did the queued work survive cancellation)
    # and not about the exit fence.  Whether the real exit path runs that
    # fence is a separate property, gated across a real process boundary by
    # test_the_production_exit_funnel_drains_the_lane below -- this
    # in-process call deliberately does not stand in for it.
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
    """The sync form swallows per-session failures; the async form must too.

    NOTE the seam this does NOT cover -- ``flush_pending_to_file`` catches
    per-session exceptions itself, so a raising ``_write_payload`` never
    reaches the async wrapper's own handler.  Measured: making that wrapper
    ``raise`` instead of returning 0 left this whole file at 20/20 green.
    ``test_a_failing_lane_submission_is_best_effort`` below gates the
    wrapper's handler directly.
    """

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


def test_a_failing_lane_submission_is_best_effort(flush_home, monkeypatch):
    """The wrapper's OWN failure must not break shutdown either.

    The lane can fail where the sync form cannot: a rejected ``submit``
    (interpreter shutting down, lane already shut), or the worker raising
    out of ``_run_on_flush_lane`` rather than inside the per-session loop.
    Both surface at the wrapper, and the caller is ``cancel_background_tasks``
    inside a bare ``except Exception: pass`` -- so an escaping exception would
    silently skip the rest of adapter teardown.
    """
    class _RejectingLane:
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(
        shutdown_flush, "_get_flush_lane", lambda: _RejectingLane(), raising=True
    )

    assert asyncio.run(
        shutdown_flush.flush_pending_to_file_async(
            {"sess-a": "x"}, reason="adapter_shutdown"
        )
    ) == 0

    # A rejected submit must not strand the fence: nothing was queued, so a
    # later drain has to return immediately rather than wait out its timeout
    # on a completion that can never arrive.
    t0 = time.monotonic()
    assert shutdown_flush.fence_flush_lane(timeout=5.0)
    assert time.monotonic() - t0 < 1.0, (
        "a rejected lane submission left the submitted/completed counters "
        "unbalanced; every later fence now blocks for its full timeout"
    )


def test_a_worker_exception_reaches_the_wrapper_as_best_effort(
    flush_home, monkeypatch
):
    """An exception raised out of the LANE BODY must be swallowed, not raised.

    Unlike ``_write_payload``, this one is not caught by the sync form's
    per-session loop, so it is the wrapper's handler that has to hold.
    """
    def _boom(*a, **kw):
        raise RuntimeError("lane body failed")

    monkeypatch.setattr(
        shutdown_flush, "flush_pending_to_file", _boom, raising=True
    )

    assert asyncio.run(
        shutdown_flush.flush_pending_to_file_async(
            {"sess-a": "x"}, reason="adapter_shutdown"
        )
    ) == 0
    assert shutdown_flush.fence_flush_lane(timeout=5.0)


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


# ---------------------------------------------------------------------------
# 5. The exit fence -- across a REAL process boundary.
# ---------------------------------------------------------------------------
#
# The shield above deliberately lets the awaiter unwind while the write is
# still queued, so SOMETHING must wait for that write before the process dies
# or the payload is lost outright.  ``shutdown_flush`` registers ``atexit``
# fences, but the gateway does not exit through ``atexit``: every graceful
# exit funnels into ``gateway.run._exit_after_graceful_shutdown``, which ends
# in ``os._exit`` (#53107, so a wedged non-daemon thread cannot hang teardown)
# and therefore hand-rolls each cleanup that would otherwise be an ``atexit``
# handler -- PID file, runtime lock, lifecycle sentinel, log-queue drain.  The
# lane drain is one of those.
#
# An in-process ``fence_flush_lane()`` call cannot gate this: it passes
# identically whether or not the exit path runs the fence (pytest itself exits
# via ``sys.exit``, i.e. the arm where ``atexit`` DOES fire).  So these tests
# spawn a child, exit it through the production funnel, and count files on
# disk after the process is dead.  Measured without the explicit drain:
# 0/5 payloads survived at every hold >= 0.1s on the production arm, against
# 5/5 on a ``sys.exit`` control.

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
)))

_EXIT_CHILD = '''
import os, sys, threading
os.environ["HERMES_HOME"] = sys.argv[3]
sys.path.insert(0, sys.argv[4])
from gateway import shutdown_flush

LANE = sys.argv[1]           # "flush" | "spool"
HOLD = float(sys.argv[2])

release = threading.Event()
if LANE == "flush":
    lane_get, submit = shutdown_flush._get_flush_lane, None
else:
    lane_get, submit = shutdown_flush._get_spool_lane, None

# Occupy the single worker so the payload below sits QUEUED, never started.
started = threading.Event()
def _hold():
    started.set()
    release.wait(30.0)
lane_get().submit(_hold)
assert started.wait(5.0), "lane occupant never started"

if LANE == "flush":
    import asyncio
    pending = {"sess-a": "the only surviving copy"}
    async def scenario():
        async def teardown():
            await shutdown_flush.flush_pending_to_file_async(
                pending, reason="adapter_shutdown", drain=True
            )
        task = asyncio.create_task(teardown())
        await asyncio.sleep(0.1)
        task.cancel()            # the per-adapter deadline
        try:
            await task
        except asyncio.CancelledError:
            return "CancelledError"
        return "completed"
    print("AWAITER_OUTCOME:", asyncio.run(scenario()), flush=True)
else:
    shutdown_flush._submit_spool_write(
        {"session_key": "sess-a", "reason": "transcript_cap_drop",
         "ts": 0, "data": {"text": "the only surviving copy"}}
    )

flush_dir = shutdown_flush._get_flush_dir()
print("FILES_AT_EXIT_CALL:",
      len(list(flush_dir.glob("pending-*.json"))), flush=True)

# Release only AFTER the exit call is under way, so the queued write genuinely
# outlives it -- the one window the fence exists for.
threading.Timer(HOLD, release.set).start()

from gateway.run import _exit_after_graceful_shutdown
_exit_after_graceful_shutdown(0)
'''


@pytest.mark.parametrize("lane", ["flush", "spool"])
def test_the_production_exit_funnel_drains_the_lane(tmp_path, lane):
    """A queued write must survive the gateway's ``os._exit`` funnel.

    Both lanes, because both are backed by an ``atexit`` fence the funnel
    bypasses -- ``_fence_flush_lane_at_exit`` and ``_fence_spool_lane_at_exit``
    have the identical defect, so they get the identical gate.
    """
    import subprocess

    child = tmp_path / "exit_child.py"
    child.write_text(_EXIT_CHILD)
    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT
    proc = subprocess.run(
        [sys.executable, str(child), lane, "0.5", str(home), _REPO_ROOT],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert "FILES_AT_EXIT_CALL: 0" in proc.stdout, (
        "the write was not still queued when the exit funnel ran, so this "
        f"test would be vacuous: {proc.stdout!r} {proc.stderr[-2000:]!r}"
    )
    survived = _payload_files(home)
    assert len(survived) == 1, (
        f"the {lane} lane's queued payload died with the process: the exit "
        "funnel uses os._exit, which never runs the atexit fence -- it must "
        "drain the lanes explicitly (fence_lanes_for_hard_exit). "
        f"stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
    )


def test_every_atexit_fence_in_shutdown_flush_has_a_hard_exit_counterpart():
    """Class sweep: no durability fence may rest on ``atexit`` alone.

    ``gateway/shutdown_flush.py`` is the only module in ``gateway/`` whose
    ``atexit`` handlers guard UNRECOVERABLE data (a stranded PID file or
    control socket is recovered on the next bind; a lost payload is not), so
    every one of them must also be reachable from the hard-exit funnel.  This
    fails if someone adds a third lane with an ``atexit`` fence and forgets
    to wire it into ``fence_lanes_for_hard_exit``.
    """
    import inspect

    source = inspect.getsource(shutdown_flush)
    registered = {
        line.split("atexit.register(")[1].split(")")[0].strip()
        for line in source.splitlines()
        if line.startswith("atexit.register(")
    }
    assert registered, "no atexit fences found; this sweep would be vacuous"

    hard_exit = inspect.getsource(shutdown_flush.fence_lanes_for_hard_exit)
    for handler in sorted(registered):
        # "_fence_<name>_lane_at_exit" is backed by "fence_<name>_lane".
        fence = handler.removeprefix("_").removesuffix("_at_exit")
        assert f"{fence}(" in hard_exit, (
            f"{handler} guards unrecoverable data but fence_lanes_for_hard_exit "
            f"never calls {fence}(); the gateway exits via os._exit, so the "
            "atexit registration alone does not run"
        )


def test_the_hard_exit_funnel_calls_the_lane_fence():
    """The funnel itself must carry the call, not just the module.

    Source-level, because the behavioural test above exercises one exit code
    path; this pins that the wiring lives in the single funnel every graceful
    exit passes through, beside the other hand-rolled atexit replacements.
    """
    import inspect

    from gateway.run import _exit_after_graceful_shutdown

    body = inspect.getsource(_exit_after_graceful_shutdown)
    assert "fence_lanes_for_hard_exit" in body, (
        "_exit_after_graceful_shutdown ends in os._exit and hand-rolls every "
        "other atexit replacement (PID file, runtime lock, log drain); the "
        "durability lane drain must be one of them"
    )


def test_the_hard_exit_fence_budget_is_shared_not_per_lane(flush_home):
    """The funnel's budget is for the CALL, not for each lane in turn.

    ``_exit_after_graceful_shutdown`` exists to be wedge-proof (#53107): it
    hard-exits precisely so a stuck thread cannot hold shutdown open.  The two
    fences run in sequence, so handing each the full timeout makes the real
    worst case ``2 * timeout`` -- measured 4.01s for a 2.0s request before the
    shared deadline.  Wedge BOTH lanes and assert the call still returns
    inside one budget.
    """
    release = threading.Event()
    started = threading.Event()
    seen = []

    def _wedge():
        seen.append(1)
        if len(seen) == 2:
            started.set()
        release.wait(60.0)

    shutdown_flush._get_flush_lane().submit(_wedge)
    shutdown_flush._get_spool_lane().submit(_wedge)
    try:
        assert started.wait(5.0), "lane occupants never started"

        # Give each fence something outstanding to wait on.  Restored in the
        # finally below: the lanes are process-wide singletons, so leaving a
        # submitted-but-never-completed count behind wedges every later
        # fence_*_lane() call in the session.
        with shutdown_flush._FLUSH_PROGRESS:
            shutdown_flush._FLUSH_SUBMITTED += 1
        with shutdown_flush._SPOOL_PROGRESS:
            shutdown_flush._SPOOL_SUBMITTED += 1

        budget = 1.0
        t0 = time.monotonic()
        drained = shutdown_flush.fence_lanes_for_hard_exit(timeout=budget)
        elapsed = time.monotonic() - t0
    finally:
        release.set()
        with shutdown_flush._FLUSH_PROGRESS:
            shutdown_flush._FLUSH_SUBMITTED -= 1
            shutdown_flush._FLUSH_PROGRESS.notify_all()
        with shutdown_flush._SPOOL_PROGRESS:
            shutdown_flush._SPOOL_SUBMITTED -= 1
            shutdown_flush._SPOOL_PROGRESS.notify_all()

    assert drained is False, (
        "both lanes were wedged, so this must report a failed drain; if it "
        "returns True the fence is not actually waiting and the test is vacuous"
    )
    assert elapsed < budget * 1.8, (
        f"fence_lanes_for_hard_exit took {elapsed:.2f}s for a {budget}s budget: "
        "the per-lane timeouts are serial, so the exit funnel's real worst case "
        "is 2x what it asks for.  Share one deadline across both fences."
    )


# ---------------------------------------------------------------------------
# 6. The await point must not eat a late arrival.
# ---------------------------------------------------------------------------


def test_a_message_arriving_during_the_flush_is_not_destroyed(flush_home):
    """The offload's await is a window the synchronous form never had.

    ``cancel_background_tasks()`` flushes then clears ``_pending_messages``.
    A message arriving during the await is not in the snapshot, so clearing
    AFTER the await destroys it -- not on disk and not in the slot.  Late
    arrivals during teardown are a real shape: the adapter teardown re-queues one
    inside that very teardown loop, and the runner and adapter paths
    also assign into the slot.

    Pre-fix (synchronous) the late arrival stayed in the slot, where a later
    flush could still take it.  The fix keeps that property by making the
    snapshot and the clear atomic inside ``flush_pending_to_file_async``
    (``drain=True``), before any await.
    """
    release = threading.Event()
    # Occupy the lane so the await genuinely blocks; without this the flush
    # completes in microseconds, the late arrival lands after the clear in
    # every arm, and the test is vacuous.
    _occupy_lane(release)

    pending = {"sess-a": "early message"}
    late = "late message arriving during teardown"

    async def scenario():
        async def teardown():
            # The exact shape of cancel_background_tasks()'s tail.
            try:
                await shutdown_flush.flush_pending_to_file_async(
                    pending, reason="adapter_shutdown", drain=True
                )
            except Exception:
                pass

        async def late_arrival():
            await asyncio.sleep(0.1)
            pending["sess-late"] = late
            release.set()

        await asyncio.gather(teardown(), late_arrival())

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert shutdown_flush.fence_flush_lane(timeout=10.0)

    on_disk = any(
        late in json.dumps(json.loads(p.read_text()))
        for p in _payload_files(flush_home)
    )
    assert pending.get("sess-late") == late or on_disk, (
        "a message that arrived during the flush was destroyed: it is "
        "neither on disk nor in the pending slot.  Snapshot and clear must "
        "be atomic with respect to the await (drain=True)."
    )
    # And the message that WAS in the snapshot still reached disk.
    assert any(
        "early message" in json.dumps(json.loads(p.read_text()))
        for p in _payload_files(flush_home)
    ), "the snapshotted payload never landed; the flush itself regressed"


def test_drain_clears_the_slot_it_snapshotted(flush_home):
    """``drain=True`` owns the clear, so the call sites must not re-clear.

    Both production callers dropped their trailing ``.clear()`` when they
    moved to ``drain=True``.  If the callee ever stopped clearing, those
    sessions would be re-flushed on the next pass -- duplicate recovery
    payloads for the same messages.
    """
    pending = {"sess-a": "x", "sess-b": "y"}
    flushed = asyncio.run(
        shutdown_flush.flush_pending_to_file_async(
            pending, reason="adapter_shutdown", drain=True
        )
    )
    assert flushed == 2
    assert dict(pending) == {}, (
        "drain=True must clear the slots it snapshotted; the call sites no "
        "longer clear after the await"
    )
    assert len(_payload_files(flush_home)) == 2


def test_drain_defaults_off_so_the_caller_keeps_the_dict(flush_home):
    """Without ``drain``, the dict is untouched -- the pre-existing contract."""
    pending = {"sess-a": "x"}
    assert asyncio.run(
        shutdown_flush.flush_pending_to_file_async(
            pending, reason="adapter_shutdown"
        )
    ) == 1
    assert dict(pending) == {"sess-a": "x"}


# ---------------------------------------------------------------------------
# 7. The PRODUCTION callers -- at the call site, not the callee.
# ---------------------------------------------------------------------------
#
# ``test_a_message_arriving_during_the_flush_is_not_destroyed`` above drives
# ``flush_pending_to_file_async(..., drain=True)`` directly, so it proves the
# callee's snapshot-and-clear is atomic -- but it passes identically if a
# caller reverts to the defective shape (no ``drain``, ``.clear()`` after the
# await).  Measured: reverting ``cancel_background_tasks`` to that shape left
# the whole file at 16/16 green.  These two tests close that gap by driving
# the real production coroutines, and the AST sweep below makes it a class
# rule rather than a two-site inventory.


class _FlushStubAdapter(BasePlatformAdapter):
    """Minimal concrete adapter; only the teardown tail is exercised."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def test_adapter_teardown_does_not_destroy_a_message_arriving_mid_flush(flush_home):
    """``cancel_background_tasks`` is the real caller; drive it, not the callee.

    the adapter teardown re-queues into ``_pending_messages`` inside this very
    teardown loop, so a late arrival during the offload's await is a real
    production shape.  Pre-fix (synchronous flush) there was no await point,
    so the arrival either made the snapshot or stayed in the slot for a later
    flush.  If this caller clears after the await, the arrival is destroyed --
    neither on disk nor in the slot.
    """
    release = threading.Event()
    _occupy_lane(release)          # the await must genuinely block

    adapter = _FlushStubAdapter()
    adapter._pending_messages["sess-a"] = MessageEvent(text="early message")
    late = "late message arriving during teardown"

    async def scenario():
        async def late_arrival():
            await asyncio.sleep(0.1)
            adapter._pending_messages["sess-late"] = MessageEvent(text=late)
            release.set()

        await asyncio.gather(adapter.cancel_background_tasks(), late_arrival())

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert shutdown_flush.fence_flush_lane(timeout=10.0)

    on_disk = any(
        late in json.dumps(json.loads(p.read_text()))
        for p in _payload_files(flush_home)
    )
    still_in_slot = adapter._pending_messages.get("sess-late")
    assert (still_in_slot is not None and still_in_slot.text == late) or on_disk, (
        "cancel_background_tasks destroyed a message that arrived during its "
        "flush await: not on disk and not in the pending slot.  The caller "
        "must hand the live slot to flush_pending_to_file_async(drain=True) "
        "instead of clearing after the await."
    )
    assert any(
        "early message" in json.dumps(json.loads(p.read_text()))
        for p in _payload_files(flush_home)
    ), "the snapshotted payload never landed; the flush itself regressed"


def test_runner_stop_does_not_destroy_a_message_arriving_mid_flush(flush_home):
    """The runner's ``_stop_impl_body`` tail has the identical shape.

    the runner assigns into the slot during teardown, so the same
    window exists here.  Driving the whole runner shutdown would drag in the
    entire gateway, so this exercises the exact three statements of that tail
    against a live slot -- the shape the AST sweep below then pins for every
    caller.
    """
    release = threading.Event()
    _occupy_lane(release)

    pending = {"sess-a": "early message"}
    late = "late message arriving during teardown"

    async def scenario():
        async def stop_tail():
            # gateway/run.py _stop_impl_body, verbatim in shape.
            try:
                from gateway.shutdown_flush import flush_pending_to_file_async
                await flush_pending_to_file_async(
                    pending, reason="shutdown", drain=True
                )
            except Exception:
                pass

        async def late_arrival():
            await asyncio.sleep(0.1)
            pending["sess-late"] = late
            release.set()

        await asyncio.gather(stop_tail(), late_arrival())

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert shutdown_flush.fence_flush_lane(timeout=10.0)

    on_disk = any(
        late in json.dumps(json.loads(p.read_text()))
        for p in _payload_files(flush_home)
    )
    assert pending.get("sess-late") == late or on_disk, (
        "the runner stop tail destroyed a message that arrived during its "
        "flush await"
    )


def test_no_caller_may_clear_the_pending_slot_after_the_flush_await():
    """CLASS SWEEP -- the rule, not the two sites that motivated it.

    The defect is structural: ``await`` is a yield point the synchronous
    ``flush_pending_to_file`` never had, so ANY caller that snapshots via the
    await and then clears destroys whatever arrived in between.  An inventory
    of the two current callers would not stop a third adapter from
    reintroducing it, so this walks the AST of every ``gateway/`` and
    ``plugins/`` module and requires each ``flush_pending_to_file_async``
    call to pass ``drain=True`` and to have no ``.clear()`` on the same slot
    later in the enclosing function.
    """
    import ast

    roots = [
        os.path.join(_REPO_ROOT, "gateway"),
        os.path.join(_REPO_ROOT, "plugins"),
    ]
    call_sites = []
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read(), path)
                except SyntaxError:      # pragma: no cover - not our concern
                    continue
                for func in ast.walk(tree):
                    if not isinstance(func, (ast.AsyncFunctionDef, ast.FunctionDef)):
                        continue
                    for node in ast.walk(func):
                        if not isinstance(node, ast.Call):
                            continue
                        name = getattr(node.func, "id", None) or getattr(
                            node.func, "attr", None
                        )
                        if name != "flush_pending_to_file_async":
                            continue
                        call_sites.append((path, func, node))

    assert call_sites, (
        "no flush_pending_to_file_async call sites found; this sweep would be "
        "vacuous -- did the function move or get renamed?"
    )

    def _slot_repr(node):
        """Text of the first positional arg, e.g. ``self._pending_messages``."""
        arg = node.args[0] if node.args else None
        if arg is None:
            return None
        try:
            return ast.unparse(arg)
        except Exception:                # pragma: no cover
            return None

    failures = []
    for path, func, node in call_sites:
        rel = os.path.relpath(path, _REPO_ROOT)
        where = f"{rel}::{func.name} (line {node.lineno})"

        drain = next(
            (kw for kw in node.keywords if kw.arg == "drain"), None
        )
        if drain is None or not (
            isinstance(drain.value, ast.Constant) and drain.value.value is True
        ):
            failures.append(
                f"{where}: must pass drain=True.  The await is a yield point; "
                "only a snapshot-and-clear taken together before it keeps a "
                "message that arrives during the flush from being lost."
            )

        slot = _slot_repr(node)
        if slot is None:
            failures.append(f"{where}: could not resolve the pending slot argument")
            continue
        # A copy defeats drain=True: the callee would clear the copy and the
        # live slot would keep entries that are already on disk.
        if slot.startswith(("dict(", "copy.", "{")) or slot.endswith(".copy()"):
            failures.append(
                f"{where}: passes a COPY ({slot}); drain=True must clear the "
                "live slot, or the flushed sessions are re-flushed later."
            )
        for later in ast.walk(func):
            if not isinstance(later, ast.Call):
                continue
            if getattr(later.func, "attr", None) != "clear":
                continue
            if later.lineno <= node.lineno:
                continue
            try:
                target = ast.unparse(later.func.value)
            except Exception:            # pragma: no cover
                continue
            if target == slot:
                failures.append(
                    f"{where}: clears {slot} at line {later.lineno}, AFTER the "
                    "flush await.  A message arriving during the await is not "
                    "in the snapshot and is destroyed by that clear -- pass "
                    "drain=True and drop the clear."
                )

    assert not failures, "\n".join(failures)
