# QUIET LANE (t_44dcc531): this file lives under tests/e2e so it runs in the e2e job (3 files at a
# time, its own runner), not on the shared unit slices. On a 4-CPU slice beside 7 busy workers the
# strict gate (only the loop thread's own run-queue wait subtracted) read 11-20 gaps over budget in
# 3/3 runs with the fix in place, against 0 in 3/3 runs alone: there it measures ci-box's scheduler,
# not the loop. The known-bad tree still fails it in the e2e lane.
"""The event loop must not stall on per-inbound-message synchronous work.

Measured on the live Apollo gateway 2026-09-20 with py-spy (14 dumps, 2s apart,
all threads, triggered by a Discord ``latency_exceeded`` warning):

  episode-20260920-103334  MainThread top frames
    dumps 1-3 (>=6s)  _load_config_impl (hermes_cli/config.py:3801)
                      <- load_config_readonly <- _resolve_hook_callback_timeout
                      <- invoke_hook <- _handle_message (gateway/run.py:21256)
    dump 9            _joinrealpath <- _canonical_hermes_home
                      <- _build_pid_record <- write_runtime_status
                      <- _persist_active_agents <- _handle_message (run.py:22749)
    dump 10           atomic_replace <- atomic_json_write <- _write_json_file
                      <- write_runtime_status <- _persist_active_agents
    dump 11           psutil _get_kinfo_proc <- create_time <- _compute_boot_id
                      <- _build_pid_record <- write_runtime_status

  episode-20260920-101605  worker thread ``asyncio_5`` was ALSO inside
                      _load_config_impl at the same time.

``_load_config_impl`` takes ``_CONFIG_LOCK``, and a cache HIT costs ~0.025ms
(measured on this box).  So the loop was not computing anything -- it was
BLOCKED ON THE LOCK held by a background writer.  Measured lock hold times:

    load_config_readonly() cache HIT                      median  0.026ms
    _load_config_impl() cache MISS (parse+merge+expand)    median  5.479ms
    save_config() (holds lock across atomic_yaml_write)    median  5.514ms
                                                          p95    33.259ms
                                                          max    55.521ms

and the loop-side cost of that same cache HIT while a background thread writes
config in a loop:

    loop load_config_readonly() under a writer   median 22.673ms
                                                 p95    62.113ms
                                                 max  4032.320ms
                                                 >20ms: 400/400 calls

These tests drive the real seams (the mechanism tests live in
tests/gateway/test_no_sync_work_per_inbound_message.py).  ``test_loop_lag_*`` is the gate:
it measures the loop's LONGEST BLOCKING INTERVAL via a lag-monitor coroutine
while N synthetic inbound messages are handled and a background thread writes
config -- never a wall-clock sleep as an assertion.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest


# The loop must never be blocked longer than this by per-message work.  Chosen
# from the measurement above: pre-fix every one of 400 calls exceeded 20ms and
# the worst exceeded 4s, so 150ms is far below the defect and far above the
# scheduler noise of a loaded CI box.
MAX_LOOP_BLOCK_MS = 150.0

# How many over-budget intervals are tolerated across the whole run.  Measured
# separation on this box (4 runs each, identical harness):
#     parent commit : 11, 36, 21, 25 intervals over budget
#     with the fix  :  0,  0,  0,  0
# 3 leaves headroom for isolated OS-scheduling outliers on a loaded CI runner
# while sitting an order of magnitude below the defect's floor.
MAX_OVER_BUDGET_INTERVALS = 3


# ---------------------------------------------------------------------------
# Lag monitor -- the house instrument (see test_startup_offloads_blocking_work).
# ---------------------------------------------------------------------------


class _RunQueueClock:
    """CPU run-queue time (ms) of the CALLING thread: time it spent RUNNABLE BUT NOT RUNNING
    (``/proc/self/task/<tid>/schedstat`` field 2; Linux only, else None).

    Only the loop thread's own wait is subtracted from a loop gap: that time is starvation
    of the loop itself by construction.  Other threads' run-queue time is never subtracted,
    because it need not have delayed the loop (the loop blocked in sync I/O while a starved
    worker queued would be hidden; Prism 77dc6042a683).  A lock or I/O wait leaves the loop
    thread SLEEPING, not runnable, so it is never subtracted either
    (``test_loop_lag_monitor_counts_a_lock_wait_on_the_loop``,
    ``test_loop_lag_monitor_counts_sync_work_beside_starved_threads``).

    Read ON the loop, so the read must not release the GIL: every release hands the GIL to
    the CPU-bound config writer for a switch interval, and an ``open``/``read`` per 2 ms
    tick made the instrument itself block the loop (29-31 gaps over budget, alone,
    measured).  So the file stays open and is ``pread`` through ``ctypes.PyDLL``, which
    holds the GIL.
    """

    def __init__(self) -> None:
        self._fd = -1
        self._buf = None
        self._libc = None
        if not os.path.isdir("/proc/self/task"):
            return
        import ctypes

        self._libc = ctypes.PyDLL(None)  # PyDLL: calls keep the GIL
        self._libc.pread.restype = ctypes.c_ssize_t
        self._libc.pread.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_long]
        self._buf = ctypes.create_string_buffer(128)
        self._fd = os.open(f"/proc/self/task/{threading.get_native_id()}/schedstat", os.O_RDONLY)

    def read(self) -> float | None:
        if self._fd < 0:
            return None
        n = self._libc.pread(self._fd, self._buf, 127, 0)
        if n <= 0:
            return None
        return int(self._buf.raw[:n].split()[1]) / 1e6

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class LoopLagMonitor:
    """Records the longest interval the event loop failed to make progress.

    A coroutine that re-schedules itself every ``interval`` and records the
    delta between successive wakeups.  Any synchronous call blocking the loop
    shows up directly as an oversized delta -- this measures the LOOP, not the
    wall clock of the work under test.  Where the kernel reports it, the loop
    thread's own CPU run-queue time inside each interval is subtracted
    (``_RunQueueClock``): a loop waiting for a CPU is not on-loop work.
    """

    def __init__(self, interval: float = 0.002) -> None:
        self.interval = interval
        self.max_gap_ms = 0.0
        self.samples: list[float] = []
        self.raw_samples: list[float] = []
        self._stop = False
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        clock = _RunQueueClock()
        try:
            last, queued = time.perf_counter(), clock.read()
            while not self._stop:
                await asyncio.sleep(self.interval)
                now, queued_now = time.perf_counter(), clock.read()
                raw_ms = (now - last - self.interval) * 1000.0
                starved_ms = queued_now - queued if queued is not None and queued_now is not None else 0.0
                gap_ms = raw_ms - max(starved_ms, 0.0)
                if raw_ms > 0:
                    self.raw_samples.append(raw_ms)
                if gap_ms > 0:
                    self.samples.append(gap_ms)
                    self.max_gap_ms = max(self.max_gap_ms, gap_ms)
                last, queued = now, queued_now
        finally:
            clock.close()

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()


@pytest.fixture()
def sandbox_home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME with a realistically-sized config.yaml."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    lines = ["plugins:", "  hook_callback_timeout: 30", "agent:", "  max_turns: 500"]
    # ~400 sections: comparable to the live Apollo config, and enough that a
    # writer's parse+merge+expand under the lock is measurable (the defect
    # scales with config size, which is exactly why it bites on a real install).
    for i in range(400):
        lines += [f"section_{i}:", f"  key_a: value_{i}", "  key_b: [1, 2, 3, 4, 5]"]
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import config as cfgmod

    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()
    yield home
    # Drain the module-global runtime-status lane before the next test.  A test
    # that queues status writes from a running loop (the loop-lag gate queues
    # 200) returns long before the single lane worker has run them; the backlog
    # would otherwise keep writing -- and calling psutil/realpath -- while the
    # NEXT test runs, under the next test's HERMES_HOME.
    from gateway import status as gwstatus

    gwstatus._fence_runtime_status_lane()
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()


# ---------------------------------------------------------------------------
# ACCEPTANCE GATE: loop lag under a concurrent config writer.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_loop_lag_stays_bounded_while_handling_messages_under_a_config_writer(
    sandbox_home,
):
    """The regression this whole change exists to prevent.

    N synthetic inbound messages run the REAL per-message config read
    (``_resolve_hook_callback_timeout`` -> ``load_config_readonly``) ON the
    loop, while a background thread does the thing the live gateway does
    (``load_config`` + ``save_config``, which holds ``_CONFIG_LOCK`` across an
    atomic YAML write).

    Pre-fix the loop's longest blocking interval exceeded MAX_LOOP_BLOCK_MS by
    orders of magnitude, because the cache-HIT read still had to acquire the
    lock the writer was holding.
    """
    from hermes_cli import config as cfgmod
    from hermes_cli.plugins import _resolve_hook_callback_timeout
    import gateway.run as run

    # Prime the cache so every on-loop read below is a cache HIT -- the exact
    # situation py-spy caught (a hit costs ~0.025ms; the stall is the lock).
    cfgmod.load_config_readonly()

    # The real per-turn seam, built the way the bare-runner tests do.
    runner = object.__new__(run.GatewayRunner)
    runner._running_agents = {"synthetic": object()}
    runner._running_agent_tasks = {}

    stop = threading.Event()
    writer_error: list[BaseException] = []

    def _writer() -> None:
        try:
            while not stop.is_set():
                cfg = cfgmod.load_config()
                cfg.setdefault("agent", {})["max_turns"] = 500
                cfgmod.save_config(cfg)
                # Paced: a real gateway writes config occasionally (a setup
                # flow, a dashboard save, an LKG snapshot), not in a hot spin.
                # Without the pace this thread's CPU-bound YAML parsing also
                # starves the loop through the GIL, which would let the test
                # pass/fail on GIL scheduling rather than on the lock contention
                # it is meant to measure.
                time.sleep(0.02)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            writer_error.append(exc)

    async def main() -> LoopLagMonitor:
        # One message before measuring: its first-call costs (lazy imports, the status
        # lane's executor thread start) run once per PROCESS, not per message, and each
        # cost 400-1000 ms on a 4-CPU CI slice (2-5 gaps over budget with no other load,
        # stacks in importlib and ThreadPoolExecutor._adjust_thread_count; t_44dcc531).
        # The defect is per message, so warming does not hide it.
        from gateway import status as gwstatus

        _resolve_hook_callback_timeout()
        runner._persist_active_agents()
        await asyncio.to_thread(gwstatus._fence_runtime_status_lane)

        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)  # let the monitor establish a baseline

        thread = threading.Thread(target=_writer, daemon=True)
        thread.start()
        try:
            # 200 synthetic inbound messages.  Each does exactly what the live
            # per-message path does and py-spy caught on the loop:
            #   1. the hook-invocation timeout resolution (config read), and
            #   2. the per-turn runtime-status persist (realpath + psutil +
            #      read-modify-atomic-write of gateway_state.json).
            for _ in range(200):
                _resolve_hook_callback_timeout()
                runner._persist_active_agents()
                await asyncio.sleep(0)
        finally:
            # Stop measuring BEFORE tearing the writer down: `thread.join()` is
            # itself a synchronous block, and joining on the loop thread would
            # be recorded as loop lag caused by the test harness rather than by
            # the code under test.
            await monitor.stop()
            stop.set()
            await asyncio.to_thread(thread.join, 10)
        return monitor

    monitor = asyncio.run(main())

    assert not writer_error, f"background config writer raised: {writer_error[0]!r}"
    assert monitor.samples, "lag monitor never sampled -- the instrument is broken"

    # Assert on the COUNT of over-budget intervals, not on the single worst
    # sample.  The defect produces SUSTAINED blocking (measured on the parent
    # commit: 11, 36, 21 and 25 intervals over budget across four runs, worst
    # samples 286ms / 11267ms / 478ms / 8145ms).  A loaded CI box can produce
    # one isolated outlier from OS scheduling alone, which is not this bug --
    # so a single spike must not turn the gate red, while the defect's
    # persistent contention cannot hide under this threshold.
    # With the fix, the same four runs recorded ZERO over-budget intervals.
    over_budget = [g for g in monitor.samples if g > MAX_LOOP_BLOCK_MS]
    assert len(over_budget) <= MAX_OVER_BUDGET_INTERVALS, (
        f"event loop was blocked >{MAX_LOOP_BLOCK_MS:.0f}ms on "
        f"{len(over_budget)} occasions (worst {monitor.max_gap_ms:.1f}ms) while "
        "handling 200 synthetic inbound messages with a background thread "
        "writing config. The per-message path must not contend on the config "
        f"lock nor do filesystem/psutil work on the loop. Samples (net of CPU run-queue "
        f"wait): {[round(g, 1) for g in sorted(over_budget, reverse=True)[:10]]}; raw: "
        f"{[round(g, 1) for g in sorted(monitor.raw_samples, reverse=True)[:10]]}"
    )


@pytest.mark.timeout(60)
def test_loop_lag_monitor_actually_detects_a_blocked_loop(sandbox_home):
    """Control arm: the instrument must FAIL on a knowingly-blocked loop.

    Without this, a green acceptance gate could mean 'the monitor never fires'.
    """

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)
        # A deliberate synchronous block, far above the budget.
        time.sleep(0.4)  # noqa: sync-on-loop control arm: proves the monitor bites
        await asyncio.sleep(0.05)
        await monitor.stop()
        return monitor

    monitor = asyncio.run(main())
    assert monitor.max_gap_ms > MAX_LOOP_BLOCK_MS, (
        f"monitor only saw {monitor.max_gap_ms:.1f}ms of lag across a deliberate "
        "400ms block -- the instrument does not work"
    )


@pytest.mark.timeout(60)
def test_loop_lag_monitor_counts_sync_work_beside_starved_threads(sandbox_home):
    """Control arm (Prism 77dc6042a683): other threads starving for a CPU while the loop
    blocks in synchronous work must not be subtracted from the loop's gap.

    16 threads hash a 4 MiB buffer in a loop (``hashlib`` drops the GIL for large inputs,
    so they really run in parallel and, on a CPU-quota'd CI slice, queue for a CPU) while
    the loop sleeps synchronously for 1 s (sleeping, not runnable, like a blocking I/O
    call).  Their run-queue time sums to seconds; none of it may come off the loop's gap.
    """
    import hashlib

    stop = threading.Event()
    payload = b"x" * (4 << 20)

    def _spin() -> None:
        while not stop.is_set():
            hashlib.sha256(payload).digest()

    spinners = [threading.Thread(target=_spin, daemon=True) for _ in range(16)]

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)
        for t in spinners:
            t.start()
        time.sleep(1.0)  # noqa: sync-on-loop control arm: sync work while other threads queue
        await asyncio.sleep(0.05)
        await monitor.stop()
        stop.set()
        for t in spinners:
            await asyncio.to_thread(t.join, 5)
        return monitor

    monitor = asyncio.run(main())
    assert monitor.max_gap_ms > MAX_LOOP_BLOCK_MS, (
        f"monitor saw only {monitor.max_gap_ms:.1f}ms net of run-queue wait across a 1s "
        f"synchronous block beside {len(spinners)} CPU-bound threads (raw "
        f"{max(monitor.raw_samples, default=0):.1f}ms) -- other threads' starvation is being "
        "charged to the loop"
    )


@pytest.mark.timeout(60)
def test_loop_lag_monitor_counts_a_lock_wait_on_the_loop(sandbox_home):
    """Control arm for the run-queue correction: the loop WAITING on a lock a thread holds
    (the defect's shape) is counted in full, because a lock wait is not run-queue time.

    A busy thread holds the lock (and the GIL most of the time) for 1 s while the loop
    blocks on it: the net gap must still be over budget.
    """
    lock = threading.Lock()
    held = threading.Event()

    def _holder() -> None:
        with lock:
            held.set()
            end = time.perf_counter() + 1.0
            while time.perf_counter() < end:
                pass  # CPU work under the lock, like a YAML parse in save_config

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)
        thread = threading.Thread(target=_holder, daemon=True)
        thread.start()
        await asyncio.to_thread(held.wait, 5)
        with lock:  # noqa: sync-on-loop control arm: the loop blocks on a held lock
            pass
        await asyncio.sleep(0.05)
        await monitor.stop()
        await asyncio.to_thread(thread.join, 5)
        return monitor

    monitor = asyncio.run(main())
    assert monitor.max_gap_ms > MAX_LOOP_BLOCK_MS, (
        f"monitor saw only {monitor.max_gap_ms:.1f}ms net of run-queue wait across a 1s "
        f"lock wait on the loop (raw {max(monitor.raw_samples, default=0):.1f}ms) -- the "
        "starvation correction is hiding the defect class"
    )
