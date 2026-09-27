# fork-only: upstream AGENTS.md forbids source-reading tests; the AST-contract
# half of this file does not port. The loop-lag half is behavioural and does.
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

These tests drive the real seams.  ``test_loop_lag_*`` is the acceptance gate:
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


class LoopLagMonitor:
    """Records the longest interval the event loop failed to make progress.

    A coroutine that re-schedules itself every ``interval`` and records the
    delta between successive wakeups.  Any synchronous call blocking the loop
    shows up directly as an oversized delta -- this measures the LOOP, not the
    wall clock of the work under test.
    """

    def __init__(self, interval: float = 0.002) -> None:
        self.interval = interval
        self.max_gap_ms = 0.0
        self.samples: list[float] = []
        self._stop = False
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop:
            await asyncio.sleep(self.interval)
            now = time.perf_counter()
            gap_ms = (now - last - self.interval) * 1000.0
            if gap_ms > 0:
                self.samples.append(gap_ms)
                self.max_gap_ms = max(self.max_gap_ms, gap_ms)
            last = now

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
        f"lock nor do filesystem/psutil work on the loop. Samples: "
        f"{[round(g, 1) for g in sorted(over_budget, reverse=True)[:10]]}"
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


# ---------------------------------------------------------------------------
# The mechanism: the read fast path must not take the config lock.
# ---------------------------------------------------------------------------


def test_cached_config_read_does_not_block_on_a_held_config_lock(sandbox_home):
    """A cache-HIT read must complete while another thread holds _CONFIG_LOCK.

    This is the root cause in one assertion: the py-spy MainThread frames sat
    in ``_load_config_impl`` for >=6s while worker thread ``asyncio_5`` was in
    the same function.  A cached read costs ~0.025ms, so the only thing the
    loop could have been doing is waiting for the lock.
    """
    from hermes_cli import config as cfgmod

    cfgmod.load_config_readonly()  # prime

    holder_has_lock = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with cfgmod._CONFIG_LOCK:
            holder_has_lock.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    try:
        assert holder_has_lock.wait(timeout=5), "lock holder never started"
        t0 = time.perf_counter()
        cfg = cfgmod.load_config_readonly()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        release.set()
        thread.join(timeout=5)

    assert isinstance(cfg, dict) and cfg, "readonly load returned nothing useful"
    assert elapsed_ms < MAX_LOOP_BLOCK_MS, (
        f"a CACHED load_config_readonly() took {elapsed_ms:.1f}ms while another "
        "thread held _CONFIG_LOCK. The cache-hit path must be lock-free so the "
        "event loop never waits on a background config writer."
    )


def test_cached_read_still_sees_a_changed_config_file(sandbox_home):
    """The lock-free fast path must not sacrifice freshness."""
    from hermes_cli import config as cfgmod

    first = cfgmod.load_config_readonly()
    assert first["agent"]["max_turns"] == 500

    path = sandbox_home / "config.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "  max_turns: 500", "  max_turns: 123"
    )
    # Force a distinct (mtime_ns, size) -- same length, so bump mtime explicitly.
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    second = cfgmod.load_config_readonly()
    assert second["agent"]["max_turns"] == 123, (
        "lock-free fast path served a stale config after the file changed"
    )


def test_readonly_and_deepcopy_paths_agree_after_the_fast_path(sandbox_home):
    from hermes_cli import config as cfgmod

    ro = cfgmod.load_config_readonly()
    full = cfgmod.load_config()
    assert full["agent"]["max_turns"] == ro["agent"]["max_turns"]
    # load_config() must still hand back an isolated object.
    full["agent"]["max_turns"] = -1
    assert cfgmod.load_config_readonly()["agent"]["max_turns"] != -1


# ---------------------------------------------------------------------------
# Hook-timeout resolution must not re-read config on every message.
# ---------------------------------------------------------------------------


def test_hook_timeout_is_resolved_without_re_reading_config_every_call(sandbox_home):
    """``_resolve_hook_callback_timeout`` runs once per hook invocation, which
    is at least once per inbound message.  It must be memoized against the
    config generation, not re-derived from a full config read each time."""
    from hermes_cli import config as cfgmod
    from hermes_cli import plugins as pluginsmod

    pluginsmod._reset_hook_callback_timeout_cache()
    assert pluginsmod._resolve_hook_callback_timeout() == 30.0

    calls = {"n": 0}
    real = cfgmod.load_config_readonly

    def counting():
        calls["n"] += 1
        return real()

    pluginsmod_config_reader = getattr(cfgmod, "load_config_readonly")
    assert pluginsmod_config_reader is real  # sanity

    cfgmod.load_config_readonly = counting  # type: ignore[assignment]
    try:
        for _ in range(100):
            pluginsmod._resolve_hook_callback_timeout()
    finally:
        cfgmod.load_config_readonly = real  # type: ignore[assignment]

    assert calls["n"] == 0, (
        f"_resolve_hook_callback_timeout() read the config {calls['n']}x across "
        "100 hook invocations; it must be resolved once and reused until the "
        "config changes."
    )


def test_hook_timeout_cache_invalidates_when_config_changes(sandbox_home):
    from hermes_cli import plugins as pluginsmod

    pluginsmod._reset_hook_callback_timeout_cache()
    assert pluginsmod._resolve_hook_callback_timeout() == 30.0

    path = sandbox_home / "config.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "  hook_callback_timeout: 30", "  hook_callback_timeout: 45"
    )
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert pluginsmod._resolve_hook_callback_timeout() == 45.0, (
        "hook-timeout memo did not pick up a changed config.yaml"
    )


def test_hook_timeout_clamps_and_defaults_survive_memoization(sandbox_home):
    """The memo must not swallow the existing validation contract."""
    from hermes_cli import plugins as pluginsmod

    path = sandbox_home / "config.yaml"

    def _set(raw: str) -> float:
        text = path.read_text(encoding="utf-8")
        import re

        text = re.sub(r"  hook_callback_timeout: .*", f"  hook_callback_timeout: {raw}", text)
        path.write_text(text, encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        pluginsmod._reset_hook_callback_timeout_cache()
        return pluginsmod._resolve_hook_callback_timeout()

    assert _set("9999") == pluginsmod._MAX_HOOK_CALLBACK_TIMEOUT_SECS
    assert _set("-5") == pluginsmod._HOOK_CALLBACK_TIMEOUT_SECS
    assert _set("0") == 0.0
    assert _set("'not-a-number'") == pluginsmod._HOOK_CALLBACK_TIMEOUT_SECS


# ---------------------------------------------------------------------------
# Per-message filesystem / psutil work.
# ---------------------------------------------------------------------------


def test_boot_id_is_computed_once_per_process_not_per_message(sandbox_home):
    """``_compute_boot_id`` hits psutil's ``_get_kinfo_proc`` (py-spy dump 11).

    A process's kernel create_time is IMMUTABLE for the life of the process, so
    calling it on every status write is pure waste on the loop.

    Only THIS thread's calls are counted.  The psutil patch is process-global,
    and the ``gateway-runtime-status`` lane worker also reaches
    ``_compute_boot_id`` (via ``_build_pid_record``) for any write still queued;
    if it misses the just-reset cache at the same moment as the test's first
    call, both compute once.  That duplicate is a benign race on an idempotent
    cache fill, not a per-message recompute, and it made this test fail with
    "called 2x" on a loaded CI runner (run 36069213079, slice 12).
    """
    import psutil

    from gateway import status as gwstatus

    gwstatus._fence_runtime_status_lane()
    gwstatus._reset_identity_caches()
    calls = {"n": 0}
    real_create_time = psutil.Process.create_time
    test_thread = threading.get_ident()

    def counting(self):
        if threading.get_ident() == test_thread:
            calls["n"] += 1
        return real_create_time(self)

    psutil.Process.create_time = counting  # type: ignore[assignment]
    try:
        first = gwstatus._compute_boot_id(os.getpid())
        for _ in range(200):
            assert gwstatus._compute_boot_id(os.getpid()) == first
    finally:
        psutil.Process.create_time = real_create_time  # type: ignore[assignment]

    assert calls["n"] == 1, (
        f"psutil create_time() was called {calls['n']}x for 201 boot-id "
        "computations; it is immutable per process and must be cached."
    )


def test_canonical_hermes_home_does_not_realpath_every_call(sandbox_home):
    """``_canonical_hermes_home`` -> ``Path.resolve()`` -> ``_joinrealpath``
    was the MainThread top frame in py-spy dump 9, reached per inbound message
    via ``_persist_active_agents``."""
    import os.path as ospath

    from gateway import status as gwstatus

    gwstatus._reset_identity_caches()
    calls = {"n": 0}
    real_realpath = ospath.realpath

    def counting(path, **kwargs):
        calls["n"] += 1
        return real_realpath(path, **kwargs)

    ospath.realpath = counting  # type: ignore[assignment]
    try:
        first = gwstatus._canonical_hermes_home(sandbox_home)
        for _ in range(200):
            assert gwstatus._canonical_hermes_home(sandbox_home) == first
    finally:
        ospath.realpath = real_realpath  # type: ignore[assignment]

    assert calls["n"] <= 1, (
        f"realpath() ran {calls['n']}x for 201 canonicalizations of the same "
        "path; the result is stable and must be cached off the hot path."
    )


def test_canonical_hermes_home_still_distinguishes_different_paths(tmp_path):
    from gateway import status as gwstatus

    gwstatus._reset_identity_caches()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert gwstatus._canonical_hermes_home(a) != gwstatus._canonical_hermes_home(b)
    assert gwstatus._canonical_hermes_home(a).is_absolute()


def test_persist_active_agents_offloads_the_status_write(sandbox_home):
    """The per-turn ``_persist_active_agents`` does a read-modify-atomic-write
    of gateway_state.json ON the loop (py-spy dumps 9/10/11).  Measured
    ``write_runtime_status`` median 0.903ms / max 7.666ms on an IDLE box with a
    small file -- it must not run on the loop thread at all.
    """
    import gateway.run as run

    runner = object.__new__(run.GatewayRunner)
    runner._running_agents = {}
    runner._running_agent_tasks = {}

    seen: dict = {}
    done = threading.Event()

    def fake_write(**kwargs):
        seen["thread"] = threading.current_thread()
        done.set()

    async def main():
        seen["loop_thread"] = threading.current_thread()
        runner._persist_active_agents()
        # Give the offload a chance to land without asserting on wall clock:
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)

    import gateway.status as gwstatus

    real = gwstatus.write_runtime_status
    gwstatus.write_runtime_status = fake_write  # type: ignore[assignment]
    try:
        asyncio.run(main())
    finally:
        gwstatus.write_runtime_status = real  # type: ignore[assignment]

    assert done.is_set(), "_persist_active_agents never performed the status write"
    assert seen["thread"] is not seen["loop_thread"], (
        "gateway_state.json was written ON the event loop thread; the "
        "read-modify-atomic-write must be offloaded with asyncio.to_thread."
    )


def test_persist_active_agents_still_works_with_no_running_loop(sandbox_home):
    """Sync callers (startup, shutdown, tests built via object.__new__) must
    keep working -- the offload is loop-conditional, not mandatory."""
    import gateway.run as run
    import gateway.status as gwstatus

    runner = object.__new__(run.GatewayRunner)
    runner._running_agents = {}
    runner._running_agent_tasks = {}

    seen: dict = {}

    def fake_write(**kwargs):
        seen["thread"] = threading.current_thread()

    real = gwstatus.write_runtime_status
    gwstatus.write_runtime_status = fake_write  # type: ignore[assignment]
    try:
        runner._persist_active_agents()
    finally:
        gwstatus.write_runtime_status = real  # type: ignore[assignment]

    assert seen.get("thread") is threading.current_thread(), (
        "with no running loop the write must happen inline, not be dropped"
    )
