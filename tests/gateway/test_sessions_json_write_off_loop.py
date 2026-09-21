# fork-only: upstream AGENTS.md forbids source-reading tests; the AST-contract
# half of the sibling ratchet does not port. This file is behavioural and does.
"""The per-turn sessions.json mirror write must not run on the event loop.

MEASURED INCIDENT (live Apollo gateway, pid 89635, 2026-09-20):

    gateway.log 12:26:14 / 12:26:24
      PHASE=event_loop_blocked platform=discord seconds=10/20
      site=utils.py:230 atomic_replace

    py-spy episode-20260920-122625, MainThread top frame for 14 CONSECUTIVE
    2s dumps (12:26:25 -> 12:26:55, >= 30s):

      atomic_replace (utils.py:230)          <- os.replace(tmp, real_path)
        _write_sessions_json_unlocked (gateway/session.py:2349)
        _save_sessions_json           (gateway/session.py:2313)
        _persist_routing_data         (gateway/session.py:2276)
        _save                         (gateway/session.py:2152)
        clear_resume_pending          (gateway/session.py:4058)
        _apply_post_turn_resume_gate  (gateway/run.py:13948)
        _handle_message_with_agent    (gateway/run.py:25593)

    gateway/shutdown_watchdog then logged "missed 3 consecutive liveness
    probes; exiting with code 75" at 12:27:33, launchd relaunched at 12:30:54,
    all four adapters timed out -- Apollo was deaf ~5 minutes.

Size was not the cause: ~/.hermes/sessions/sessions.json is 562 KB / 316
sessions, and an uncontended copy+fsync+rename of that file measures 0.32 ms
median / 13.7 ms max on this box (40 samples).  The 30 s was contention on the
rename itself, i.e. exactly the kind of tail no amount of making the write
*faster* can bound.  The only sound fix is to stop doing it on the loop thread.

The acceptance gate below therefore does NOT measure how long the write takes.
It monkeypatches the REAL ``utils.atomic_replace`` to sleep, drives the REAL
per-turn seam (``clear_resume_pending`` -> ``_save`` -> ... -> atomic_replace)
on a live event loop, and measures the loop's longest blocking interval with
the house lag monitor.  Pre-fix the loop stalls for the injected delay; post-fix
it does not stall at all.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest


# Injected per-write delay. Large enough to dwarf scheduler noise on a loaded
# CI runner, small enough that the whole gate runs in a couple of seconds.
INJECTED_REPLACE_DELAY_S = 0.5

# The loop must never be blocked longer than this by a mirror write.  One
# injected write alone is 500ms, so a pre-fix run cannot hide under this.
MAX_LOOP_BLOCK_MS = 150.0


class LoopLagMonitor:
    """Records the longest interval the event loop failed to make progress.

    House instrument, same shape as
    ``tests/gateway/test_no_sync_work_per_inbound_message.py``: a coroutine
    that re-schedules itself every ``interval`` and records the delta between
    successive wakeups.  This measures the LOOP, never the wall clock of the
    work under test.
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


# ---------------------------------------------------------------------------
# A real SessionStore over a temp sessions dir.
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path, monkeypatch, *, with_db: bool = True):
    """Build a REAL SessionStore over a temp sessions dir.

    ``with_db=True`` pins a minimal in-memory stand-in for SessionDB that
    accepts the routing UPSERT.  That matters: the mirror is only deferred
    when state.db has ALREADY committed (see ``_dispatch_sessions_json_save``)
    -- with no DB the mirror is the primary copy and must stay synchronous.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.session import SessionStore

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    class _Cfg:
        write_sessions_json = True

    class _FakeDB:
        """Accepts the routing replace so ``db_saved`` is True."""

        def __init__(self) -> None:
            self.rows: dict = {}

        def replace_gateway_routing_entries(self, entries, *, scope=None, **kw):
            self.rows = dict(entries)

        def save_gateway_routing_entry(self, key, entry_json, *, scope=None, **kw):
            self.rows[key] = entry_json

        def close(self) -> None:
            pass

    store = object.__new__(SessionStore)
    SessionStore.__init__(store, sessions_dir, _Cfg())
    store._db = _FakeDB() if with_db else None
    return store


def _seed_entries(store, keys: int = 40) -> str:
    """Populate the routing index and return one session key with resume_pending."""
    from datetime import datetime

    from gateway.session import SessionEntry

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


def _slow_atomic_replace(delay: float):
    """A stand-in for ``utils.atomic_replace`` that blocks the calling thread."""
    import utils as utils_mod

    real = utils_mod.atomic_replace
    calls: list[str] = []

    def slow(tmp_path, target):
        calls.append(str(target))
        time.sleep(delay)
        return real(tmp_path, target)

    return slow, calls


def _drain(store, timeout: float = 20.0) -> None:
    """Drain the deferred writer if this build has one.

    Deliberately tolerant: on the PARENT commit the helper does not exist, and
    a RED proof must fail on the behavioural assertion (the loop was blocked /
    the write happened on the loop thread), not on an AttributeError from the
    harness.  A missing helper simply means there is nothing to drain.
    """
    drain = getattr(store, "drain_sessions_json_writes", None)
    if callable(drain):
        drain(timeout=timeout)


# ---------------------------------------------------------------------------
# ACCEPTANCE GATE
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_loop_lag_stays_bounded_across_per_turn_session_saves(tmp_path, monkeypatch):
    """The regression this change exists to prevent.

    Drives the REAL per-turn seam -- ``clear_resume_pending`` -> ``_save`` ->
    ``_persist_routing_data`` -> ``_save_sessions_json`` ->
    ``_write_sessions_json_unlocked`` -> ``utils.atomic_replace`` -- from a
    coroutine on a live loop, with ``atomic_replace`` monkeypatched to block
    for 500ms per call.

    On the parent commit the loop stalls for ~500ms per turn boundary (the
    measured incident was 30s from real filesystem contention).  With the fix
    the write is handed to the mirror writer thread, so the loop never blocks.
    """
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store)

    slow, calls = _slow_atomic_replace(INJECTED_REPLACE_DELAY_S)
    monkeypatch.setattr(utils_mod, "atomic_replace", slow)
    # session.py imports the symbol directly; patch the bound name too.
    import gateway.session as sessmod

    monkeypatch.setattr(sessmod, "atomic_replace", slow, raising=False)

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)  # let the monitor establish a baseline
        try:
            # 5 synthetic turn boundaries.  Each is the real call the gateway
            # makes from _apply_post_turn_resume_gate.
            for _ in range(5):
                store._entries[key].resume_pending = True
                store.clear_resume_pending(key)
                await asyncio.sleep(0.01)
            # Let any deferred write settle without asserting on wall clock.
            for _ in range(200):
                if calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            await monitor.stop()
        return monitor

    monitor = asyncio.run(main())
    # Drain off-loop so the assertions below see a settled writer.
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
    """Control arm: the instrument must FAIL on a knowingly-blocked loop.

    Without this, a green acceptance gate could mean 'the monitor never fires'.
    """

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)
        time.sleep(INJECTED_REPLACE_DELAY_S)  # noqa: sync-on-loop control arm: proves the monitor bites
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
    import gateway.session as sessmod
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

    monkeypatch.setattr(utils_mod, "atomic_replace", probe)
    monkeypatch.setattr(sessmod, "atomic_replace", probe, raising=False)

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
    """Synchronous callers (CLI, tests, shutdown) keep the old behavior.

    The offload is loop-conditional, not mandatory: with no running loop the
    write must happen inline on the calling thread, not be deferred or dropped.
    """
    import gateway.session as sessmod
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store, keys=5)

    seen: dict = {}
    real = utils_mod.atomic_replace

    def probe(tmp_p, target):
        seen["thread"] = threading.current_thread()
        return real(tmp_p, target)

    monkeypatch.setattr(utils_mod, "atomic_replace", probe)
    monkeypatch.setattr(sessmod, "atomic_replace", probe, raising=False)

    store.clear_resume_pending(key)

    assert seen.get("thread") is threading.current_thread(), (
        "with no running loop the mirror write must happen inline on the "
        "calling thread, not be deferred"
    )
    assert (store.sessions_dir / "sessions.json").exists()


# ---------------------------------------------------------------------------
# Coalescing: N back-to-back saves collapse to fewer renames.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_back_to_back_saves_coalesce_into_fewer_renames(tmp_path, monkeypatch):
    """N per-turn saves in a burst must not produce N renames.

    The writer keeps only the NEWEST pending snapshot, so a burst of turn
    boundaries collapses.  This is the part that makes the off-loop write
    cheap in aggregate rather than merely invisible.
    """
    import gateway.session as sessmod
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

    monkeypatch.setattr(utils_mod, "atomic_replace", probe)
    monkeypatch.setattr(sessmod, "atomic_replace", probe, raising=False)

    async def main():
        for _ in range(20):
            store._entries[key].resume_pending = True
            store.clear_resume_pending(key)
        # Everything is queued; release the writer.
        gate.set()
        await asyncio.sleep(0.05)

    asyncio.run(main())
    _drain(store)

    assert renames, "no rename happened at all"
    assert len(renames) < 20, (
        f"{len(renames)} renames for 20 back-to-back saves; the writer must "
        "coalesce a burst into the newest snapshot only"
    )


# ---------------------------------------------------------------------------
# Durability + format: the on-disk file must be byte-identical in shape.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_deferred_write_produces_the_same_on_disk_bytes_as_the_inline_write(
    tmp_path, monkeypatch
):
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
    assert _drain(store_async) is None
    deferred_bytes = (store_async.sessions_dir / "sessions.json").read_bytes()

    assert deferred_bytes == inline_bytes, (
        "the deferred mirror write produced different on-disk bytes than the "
        "inline write; the format must be unchanged"
    )
    # And it must still be the documented shape.
    parsed = json.loads(deferred_bytes)
    assert "_README" in parsed
    assert any(k.startswith("agent:main:discord:") for k in parsed)


@pytest.mark.timeout(60)
def test_flush_waits_for_the_deferred_write(tmp_path, monkeypatch):
    """``flush()`` promises durability; it must not return before the rename."""
    import gateway.session as sessmod
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch)
    _seed_entries(store, keys=5)

    real = utils_mod.atomic_replace
    written = threading.Event()

    def probe(tmp_p, target):
        time.sleep(0.2)
        out = real(tmp_p, target)
        written.set()
        return out

    monkeypatch.setattr(utils_mod, "atomic_replace", probe)
    monkeypatch.setattr(sessmod, "atomic_replace", probe, raising=False)

    async def main():
        await asyncio.to_thread(store.flush)

    asyncio.run(main())
    assert written.is_set(), "flush() returned before the deferred write landed"
    assert (store.sessions_dir / "sessions.json").exists()


@pytest.mark.timeout(60)
def test_mirror_stays_inline_when_state_db_did_not_commit(tmp_path, monkeypatch):
    """When the mirror is the PRIMARY copy, its failure must reach the caller.

    ``_persist_routing_data`` only tolerates a mirror failure after state.db
    committed.  With no DB commit the mirror is the only durable copy, so the
    write stays synchronous even on a loop thread and raises.
    """
    import gateway.session as sessmod
    import utils as utils_mod

    store = _make_store(tmp_path, monkeypatch, with_db=False)
    key = _seed_entries(store, keys=3)

    def boom(tmp_p, target):
        raise OSError("disk on fire")

    monkeypatch.setattr(utils_mod, "atomic_replace", boom)
    monkeypatch.setattr(sessmod, "atomic_replace", boom, raising=False)

    async def main():
        with pytest.raises(OSError):
            store.clear_resume_pending(key)

    asyncio.run(main())


@pytest.mark.timeout(60)
def test_writer_thread_retires_on_shutdown(tmp_path, monkeypatch):
    """``close_all_db_handles`` (the shutdown path) must drain and stop it."""
    store = _make_store(tmp_path, monkeypatch)
    key = _seed_entries(store, keys=5)

    async def main():
        store.clear_resume_pending(key)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert store.stop_sessions_json_writer(timeout=20.0)
    assert store._sessions_json_writer is None
    assert (store.sessions_dir / "sessions.json").exists()


# ---------------------------------------------------------------------------
# The ALIAS-MIGRATION mirror writes (#773).
#
# Two call sites still reached ``_save_sessions_json`` DIRECTLY, bypassing
# ``_dispatch_sessions_json_save``:
#
#   gateway/session.py::_redirect_legacy_alias_routes_locked   (startup)
#   gateway/session.py::migrate_discord_session_keys           (adapter-driven)
#
# Both fire only when ongoing mirroring is DISABLED
# (``gateway.write_sessions_json: false``) and a legacy sessions.json still
# exists -- they retire alias keys from that file so they cannot resurrect.
# That is a narrow condition, but the write is the same unbounded-tail
# mkstemp + fsync + ``os.replace``, and ``migrate_discord_session_keys`` is
# called from the Discord adapter's connect path on a live loop.
#
# These were invisible to the #782 ratchet only because
# ``_persist_routing_data`` reached the same sink first; closing that chain
# exposed them (the ratchet named ``_interrupt_and_clear_session`` and
# ``_run_startup_resume_event``, whose route runs
# ``_ensure_loaded_locked -> _redirect_legacy_alias_routes_locked``).
# ---------------------------------------------------------------------------


def _make_mirror_disabled_store(tmp_path, monkeypatch):
    """A real store with ongoing mirroring OFF and a legacy sessions.json present.

    That is exactly the condition under which the alias-retirement writes fire.
    """
    store = _make_store(tmp_path, monkeypatch)
    store._write_sessions_json = False
    # ``migrate_discord_session_keys`` rebuilds keys through
    # ``build_session_key``, which reads these two from config.  The shared
    # ``_Cfg`` stub above only carries ``write_sessions_json``.
    store.config.group_sessions_per_user = False
    store.config.thread_sessions_per_user = False
    # A legacy file must exist, or the retirement branch is skipped entirely.
    (store.sessions_dir / "sessions.json").write_text("{}", encoding="utf-8")
    return store


def _seed_discord_aliases(store, pairs: int = 20):
    """Seed ``channel``-typed Discord keys that collapse onto ``group`` keys."""
    from datetime import datetime

    from gateway.session import SessionEntry

    now = datetime(2026, 9, 20, 12, 26, 25)
    chat_types = {}
    for i in range(pairs):
        chat_id = f"chan{i}"
        key = f"agent:main:discord:channel:{chat_id}"
        store._entries[key] = SessionEntry(
            session_key=key,
            session_id=f"2026_{i:08d}_aliasfeed",
            created_at=now,
            updated_at=now,
        )
        chat_types[chat_id] = "group"
    store._loaded = True
    store._routing_db_loaded = True
    return chat_types


@pytest.mark.timeout(120)
def test_loop_lag_stays_bounded_across_discord_alias_migration(tmp_path, monkeypatch):
    """``migrate_discord_session_keys`` must not rename sessions.json on the loop.

    The Discord adapter calls this from its connect path, i.e. on the event
    loop thread. With mirroring disabled and a legacy file present it retires
    the alias keys with a full mkstemp + fsync + ``os.replace``. Before #773
    that went straight to ``_save_sessions_json``; now it goes through
    ``_dispatch_sessions_json_save`` like every other whole-index write.
    """
    import gateway.session as sessmod
    import utils as utils_mod

    store = _make_mirror_disabled_store(tmp_path, monkeypatch)
    chat_types = _seed_discord_aliases(store)

    slow, calls = _slow_atomic_replace(INJECTED_REPLACE_DELAY_S)
    monkeypatch.setattr(utils_mod, "atomic_replace", slow)
    monkeypatch.setattr(sessmod, "atomic_replace", slow, raising=False)

    merged_holder: dict = {}

    async def main() -> LoopLagMonitor:
        monitor = LoopLagMonitor()
        monitor.start()
        await asyncio.sleep(0.05)
        try:
            merged_holder["n"] = store.migrate_discord_session_keys(chat_types)
            for _ in range(200):
                if calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            await monitor.stop()
        return monitor

    monitor = asyncio.run(main())
    _drain(store)

    assert merged_holder.get("n"), (
        "no aliases were merged -- the seam under test never ran, so a green "
        "result here would be vacuous"
    )
    assert monitor.samples, "lag monitor never sampled -- the instrument is broken"
    assert calls, "no sessions.json write happened at all -- the seam is not wired"

    over_budget = [g for g in monitor.samples if g > MAX_LOOP_BLOCK_MS]
    assert not over_budget, (
        f"event loop blocked >{MAX_LOOP_BLOCK_MS:.0f}ms on {len(over_budget)} "
        f"occasions (worst {monitor.max_gap_ms:.1f}ms) during the Discord "
        "alias migration with a 500ms atomic_replace. The alias-retirement "
        "mirror write must go through _dispatch_sessions_json_save. Samples: "
        f"{[round(g, 1) for g in sorted(over_budget, reverse=True)[:5]]}"
    )


@pytest.mark.timeout(60)
def test_discord_alias_migration_write_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """Mechanism arm: WHICH THREAD renames the file during alias migration."""
    import gateway.session as sessmod
    import utils as utils_mod

    store = _make_mirror_disabled_store(tmp_path, monkeypatch)
    chat_types = _seed_discord_aliases(store, pairs=5)

    seen: dict = {}
    done = threading.Event()
    real = utils_mod.atomic_replace

    def probe(tmp_p, target):
        seen["thread"] = threading.current_thread()
        done.set()
        return real(tmp_p, target)

    monkeypatch.setattr(utils_mod, "atomic_replace", probe)
    monkeypatch.setattr(sessmod, "atomic_replace", probe, raising=False)

    async def main():
        seen["loop_thread"] = threading.current_thread()
        assert store.migrate_discord_session_keys(chat_types)
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    _drain(store)

    assert done.is_set(), "the alias migration never wrote sessions.json"
    assert seen["thread"] is not seen["loop_thread"], (
        "sessions.json was renamed ON the event loop thread during the "
        "Discord alias migration; it must be handed to the writer thread."
    )


@pytest.mark.timeout(60)
def test_startup_alias_redirect_write_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """The startup route (``_redirect_legacy_alias_routes_locked``) too.

    This is the chain the #782 ratchet actually named: a coroutine reaching
    ``_ensure_loaded_locked`` runs the one-time legacy alias redirect, which
    retired keys with an inline rename.
    """
    from datetime import datetime

    import gateway.session as sessmod
    import utils as utils_mod

    from gateway.session import SessionEntry

    store = _make_mirror_disabled_store(tmp_path, monkeypatch)

    # ``channel`` is a shape-only alias of ``group`` for discord, so the
    # redirect rewrites it without needing the channel object.
    now = datetime(2026, 9, 20, 12, 26, 25)
    for i in range(5):
        key = f"agent:main:discord:channel:chan{i}"
        store._entries[key] = SessionEntry(
            session_key=key,
            session_id=f"2026_{i:08d}_startupal",
            created_at=now,
            updated_at=now,
        )
    store._loaded = True
    store._routing_db_loaded = True

    seen: dict = {}
    done = threading.Event()
    real = utils_mod.atomic_replace

    def probe(tmp_p, target):
        seen["thread"] = threading.current_thread()
        done.set()
        return real(tmp_p, target)

    monkeypatch.setattr(utils_mod, "atomic_replace", probe)
    monkeypatch.setattr(sessmod, "atomic_replace", probe, raising=False)

    async def main():
        seen["loop_thread"] = threading.current_thread()
        with store._lock:
            merged = store._redirect_legacy_alias_routes_locked()
        assert merged, "the redirect merged nothing -- the arm is vacuous"
        for _ in range(200):
            if done.is_set():
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    _drain(store)

    assert done.is_set(), "the startup alias redirect never wrote sessions.json"
    assert seen["thread"] is not seen["loop_thread"], (
        "sessions.json was renamed ON the event loop thread during the "
        "startup legacy-alias redirect; it must be handed to the writer thread."
    )
