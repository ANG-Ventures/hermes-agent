"""The per-turn sessions.json mirror write must not run on the event loop.

Measured on a live gateway (2026-09-20, py-spy, 14 consecutive dumps 2s apart,
all threads): the MainThread sat in ``os.replace`` for >= 30 seconds, reached
per turn from the routing-index save:

    atomic_replace                <- os.replace(tmp, real_path)
      atomic_json_write
        _save_sessions_json       (gateway/session_persistence.py)
        _persist_routing_data
        _save
        clear_resume_pending      <- post-turn resume gate
        _handle_message_with_agent

The loop-liveness watchdog then killed the process ("missed 3 consecutive
liveness probes; exiting with code 75") and every platform adapter timed out on
the relaunch.  Host load was 27 on 32 cores, so this was not CPU starvation.

Size is not the cause and making the write faster cannot fix it.  A 562 KB /
316-session sessions.json measures, uncontended (40 samples per arm):

    same directory as the live file   median 0.322ms  p95 12.1ms  max 13.7ms
    outside the platform index scope  median 0.207ms  p95  2.4ms  max  8.9ms
    + a concurrent fsync writer       median 0.093ms             max  0.2ms
    + a full directory metadata scan  median 0.096ms             max  1.7ms

Five orders of magnitude below the observed stall.  The rename's own work is
trivial; what varies is how long it WAITS.  An unbounded-tail syscall must not
sit on the loop thread, so the fix is to stop calling it there rather than to
optimise it.

These tests drive the REAL seam with a slow ``atomic_replace`` and measure the
LOOP's longest blocking interval, never a wall-clock sleep as an assertion.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest


# Injected per-write delay: large enough to dwarf scheduler noise on a loaded
# CI runner, small enough that the whole gate runs in a couple of seconds.
INJECTED_REPLACE_DELAY_S = 0.5

# The loop must never be blocked longer than this by a mirror write.  One
# injected write alone is 500ms, so a pre-fix run cannot hide under this.
MAX_LOOP_BLOCK_MS = 150.0


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


class _FakeDB:
    """Accepts the routing replace so ``db_saved`` is True.

    This matters: the mirror is only deferred once state.db has ALREADY
    committed.  With no DB the mirror is the primary copy and must stay
    synchronous (covered by its own test below).
    """

    def __init__(self) -> None:
        self.rows: dict = {}

    def replace_gateway_routing_entries(self, entries, *, scope=None, **kw):
        self.rows = dict(entries)

    def save_gateway_routing_entry(self, key, entry_json, *, scope=None, **kw):
        self.rows[key] = entry_json

    def close(self) -> None:
        pass


def _make_store(tmp_path: Path, monkeypatch, *, with_db: bool = True):
    """A REAL SessionStore over a throwaway sessions dir."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.session import SessionStore

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    class _Cfg:
        write_sessions_json = True

    store = object.__new__(SessionStore)
    SessionStore.__init__(store, sessions_dir, _Cfg())
    store._db = _FakeDB() if with_db else None
    return store


def _seed_entries(store, keys: int = 40) -> str:
    """Populate the routing index; return one key carrying resume_pending."""
    from gateway.session import SessionEntry

    # A fixed timestamp keeps the byte-comparison test deterministic.
    now = datetime(2026, 9, 20, 12, 26, 25)
    first = ""
    for i in range(keys):
        key = f"agent:main:discord:chan{i}"
        entry = SessionEntry(
            session_key=key,
            session_id=f"2026_{i:08d}_deadbeef",
            created_at=now,
            updated_at=now,
        )
        entry.resume_pending = True
        entry.resume_reason = "test"
        store._entries[key] = entry
        if not first:
            first = key
    store._loaded = True
    store._routing_db_loaded = True
    return first


def _drain(store, timeout: float = 20.0) -> None:
    drain = getattr(store, "drain_sessions_json_writes", None)
    if callable(drain):
        drain(timeout=timeout)


def _patch_replace(monkeypatch, fn):
    """Patch ``atomic_replace`` at every binding the write path can reach."""
    import utils as utils_mod

    monkeypatch.setattr(utils_mod, "atomic_replace", fn)
    for modname in ("gateway.session_persistence", "gateway.session"):
        try:
            mod = __import__(modname, fromlist=["x"])
        except ImportError:
            continue
        if hasattr(mod, "atomic_replace"):
            monkeypatch.setattr(mod, "atomic_replace", fn)


# ---------------------------------------------------------------------------
# ACCEPTANCE GATE
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_loop_lag_stays_bounded_across_per_turn_session_saves(tmp_path, monkeypatch):
    """The regression this change exists to prevent.

    Drives the real per-turn seam -- ``clear_resume_pending`` -> ``_save`` ->
    ``_persist_routing_data`` -> ``_save_sessions_json`` -> ``atomic_json_write``
    -> ``atomic_replace`` -- from a coroutine on a live loop, with the rename
    blocking 500ms per call.
    """
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store)

    real = utils_mod.atomic_replace
    calls: list[str] = []

    def slow(tmp_p, target):
        calls.append(str(target))
        time.sleep(INJECTED_REPLACE_DELAY_S)
        return real(tmp_p, target)

    _patch_replace(monkeypatch, slow)

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)  # let the monitor establish a baseline
        try:
            for _ in range(5):
                store._entries[key].resume_pending = True
                store.clear_resume_pending(key)
                await asyncio.sleep(0.01)
            for _ in range(200):
                if calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            await monitor.stop()
        return monitor

    monitor = asyncio.run(main())
    _drain(store)

    assert monitor.samples, "lag monitor never sampled -- the instrument is broken"
    assert calls, "no sessions.json write happened at all -- the seam is not wired"

    over_budget = [g for g in monitor.samples if g > MAX_LOOP_BLOCK_MS]
    assert not over_budget, (
        f"event loop blocked >{MAX_LOOP_BLOCK_MS:.0f}ms on {len(over_budget)} "
        f"occasions (worst {monitor.max_gap_ms:.1f}ms) across 5 per-turn "
        "sessions.json saves with a 500ms atomic_replace. The per-turn mirror "
        "write must not run on the loop thread. Samples: "
        f"{[round(g, 1) for g in sorted(over_budget, reverse=True)[:5]]}"
    )


@pytest.mark.timeout(60)
def test_loop_lag_monitor_actually_detects_a_blocked_loop():
    """Control arm: a green gate must not be able to mean 'the monitor never fires'."""

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)
        time.sleep(INJECTED_REPLACE_DELAY_S)  # deliberate synchronous block
        await asyncio.sleep(0.05)
        await monitor.stop()
        return monitor

    monitor = asyncio.run(main())
    assert monitor.max_gap_ms > MAX_LOOP_BLOCK_MS, (
        f"monitor only saw {monitor.max_gap_ms:.1f}ms of lag across a deliberate "
        f"{INJECTED_REPLACE_DELAY_S * 1000:.0f}ms block -- the instrument does not work"
    )


# ---------------------------------------------------------------------------
# Mechanism: WHICH THREAD does the write.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_mirror_write_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """The decisive assertion: the rename does not happen on the loop thread."""
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store, keys=5)

    seen: dict = {}
    done = threading.Event()
    real = utils_mod.atomic_replace

    def probe(tmp_p, target):
        seen["thread"] = threading.current_thread()
        done.set()
        return real(tmp_p, target)

    _patch_replace(monkeypatch, probe)

    async def main():
        seen["loop_thread"] = threading.current_thread()
        store.clear_resume_pending(key)
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    _drain(store)

    assert done.is_set(), "the per-turn save never wrote sessions.json"
    assert seen["thread"] is not seen["loop_thread"], (
        "sessions.json was renamed ON the event loop thread; the per-turn "
        "mirror write must be handed to the writer thread."
    )


@pytest.mark.timeout(60)
def test_mirror_write_is_inline_with_no_running_loop(tmp_path, monkeypatch):
    """The offload is loop-conditional, not mandatory.

    Synchronous callers (CLI, tests, shutdown) must still get an inline write --
    not a deferred one, and not a dropped one.
    """
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store, keys=5)

    seen: dict = {}
    real = utils_mod.atomic_replace

    def probe(tmp_p, target):
        seen["thread"] = threading.current_thread()
        return real(tmp_p, target)

    _patch_replace(monkeypatch, probe)
    store.clear_resume_pending(key)

    assert seen.get("thread") is threading.current_thread(), (
        "with no running loop the mirror write must happen inline on the "
        "calling thread, not be deferred"
    )
    assert (store.sessions_dir / "sessions.json").exists()


@pytest.mark.timeout(60)
def test_mirror_stays_inline_when_state_db_did_not_commit(tmp_path, monkeypatch):
    """When the mirror is the PRIMARY copy, its failure must reach the caller.

    ``_persist_routing_data`` only tolerates a mirror failure after state.db
    committed.  With no DB the mirror is the only durable copy, so the write
    stays synchronous even on a loop thread and raises.
    """
    store = _make_store(tmp_path, monkeypatch, with_db=False)
    key = _seed_entries(store, keys=3)

    def boom(tmp_p, target):
        raise OSError("disk on fire")

    _patch_replace(monkeypatch, boom)

    async def main():
        with pytest.raises(OSError):
            store.clear_resume_pending(key)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Coalescing, durability, format.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_back_to_back_saves_coalesce_into_fewer_renames(tmp_path, monkeypatch):
    """N per-turn saves in a burst must not produce N renames."""
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store, keys=10)

    renames: list[float] = []
    real = utils_mod.atomic_replace
    gate = threading.Event()

    def probe(tmp_p, target):
        # Hold the first write so the rest of the burst queues behind it.
        gate.wait(timeout=10)
        renames.append(time.perf_counter())
        return real(tmp_p, target)

    _patch_replace(monkeypatch, probe)

    async def main():
        for _ in range(20):
            store._entries[key].resume_pending = True
            store.clear_resume_pending(key)
        gate.set()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    _drain(store)

    assert renames, "no rename happened at all"
    assert len(renames) < 20, (
        f"{len(renames)} renames for 20 back-to-back saves; the writer must "
        "coalesce a burst into the newest snapshot only"
    )


@pytest.mark.timeout(60)
def test_deferred_write_produces_the_same_on_disk_bytes_as_the_inline_write(
        tmp_path, monkeypatch):
    """The off-loop path must not change the file format by one byte."""
    store_sync = _make_store(tmp_path / "a", monkeypatch)
    key_sync = _seed_entries(store_sync, keys=7)
    store_sync.clear_resume_pending(key_sync)
    inline_bytes = (store_sync.sessions_dir / "sessions.json").read_bytes()

    store_async = _make_store(tmp_path / "b", monkeypatch)
    key_async = _seed_entries(store_async, keys=7)

    async def main():
        store_async.clear_resume_pending(key_async)
        await asyncio.sleep(0)

    asyncio.run(main())
    _drain(store_async)
    deferred_bytes = (store_async.sessions_dir / "sessions.json").read_bytes()

    assert deferred_bytes == inline_bytes, (
        "the deferred mirror write produced different on-disk bytes than the "
        "inline write; the format must be unchanged"
    )
    parsed = json.loads(deferred_bytes)
    assert "_README" in parsed
    assert any(k.startswith("agent:main:discord:") for k in parsed)


@pytest.mark.timeout(60)
def test_writer_thread_retires_on_shutdown(tmp_path, monkeypatch):
    """The shutdown path must drain the queue and stop the thread."""
    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store, keys=5)

    async def main():
        store.clear_resume_pending(key)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert store.stop_sessions_json_writer(timeout=20.0)
    assert getattr(store, "_sessions_json_writer", None) is None
    assert (store.sessions_dir / "sessions.json").exists()
