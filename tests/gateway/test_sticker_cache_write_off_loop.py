"""The Telegram sticker-description cache write must not block the event loop.

``gateway/sticker_cache.py``'s ``_save_cache`` ends in ``os.fsync`` +
``os.replace``, whose duration is unbounded under filesystem pressure -- the
exact tail that held the Apollo MainThread for >= 30s on 2026-09-20, four
plain-``def`` frames below a coroutine.

The reachability ratchet froze this route as

    plugins/platforms/telegram/adapter.py _handle_sticker -> os.fsync
      via _handle_sticker>cache_sticker_description>_save_cache

``_handle_sticker`` is an inbound-message coroutine: every sticker that misses
the cache paid the rename inline on the loop, stalling every other adapter and
every in-flight turn in the process.

These tests pin the fix WITHOUT wall-clock thresholds.  The rename is held open
on a real barrier for as long as the assertion needs, and the loop must advance
a sibling task anyway; a companion gate-proof forces the pre-fix inline shape
and asserts the very same barrier DOES starve the loop, so the liveness test
cannot pass vacuously.

They also pin the property the off-loop move would otherwise silently break:
moving the write to a worker thread removes the accidental serialization the
event loop used to provide, so the load/mutate/save read-modify-write can now
interleave and lose an entry.  ``atomic_replace`` makes each write atomic; it
does not make the triple atomic.
"""
import ast
import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from gateway import sticker_cache


@pytest.fixture()
def cache_file(tmp_path, monkeypatch):
    """Point the module's cache at an isolated path."""
    path = tmp_path / "sticker_cache.json"
    monkeypatch.setattr(sticker_cache, "CACHE_PATH", path, raising=True)
    return path


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

    def release(self):
        self._gate.set()


def test_the_cache_write_does_not_block_the_loop(cache_file, monkeypatch):
    """A stalled rename must not stop the loop from running other tasks.

    No stopwatch.  The witness is ORDERING, not elapsed time: the rename is
    held open on a barrier that only a background timer releases, and the
    assertion is that the sibling task ticked BEFORE that release.  On the
    pre-fix inline shape the loop is stuck inside the rename, so the sibling
    cannot possibly tick until the release happens -- which is exactly what
    ``test_gate_proof_the_sync_form_does_block_the_loop`` demonstrates with
    the identical barrier.
    """

    async def scenario():
        held = _HeldReplace(monkeypatch)
        released = threading.Event()
        order: dict[str, bool] = {}

        async def sibling():
            await asyncio.sleep(0)
            # Captured AT TICK TIME: was the rename still being held?
            order["ticked_before_release"] = not released.is_set()

        write = asyncio.create_task(
            sticker_cache.cache_sticker_description_async(
                "uid_loop", "A cat waving", emoji="🐱", set_name="Cats"
            )
        )
        sibling_task = asyncio.create_task(sibling())

        def _release_later():
            released.set()
            held.release()

        releaser = threading.Timer(1.0, _release_later)
        releaser.start()
        try:
            await asyncio.wait_for(write, timeout=10.0)
        finally:
            releaser.cancel()

        await sibling_task
        assert held.wait_until_entered(), "the write never reached os.replace"
        assert "ticked_before_release" in order, "the sibling task never ran"
        assert order["ticked_before_release"], (
            "the loop did not advance the sibling task until the held rename "
            "was released -- the write is still blocking the event loop"
        )

        # Durability is preserved, not traded away for liveness.
        record = sticker_cache.get_cached_description("uid_loop")
        assert record["description"] == "A cat waving"
        assert record["emoji"] == "🐱"
        assert record["set_name"] == "Cats"
        assert json.loads(cache_file.read_text())["uid_loop"] == record

    asyncio.run(scenario())


def test_gate_proof_the_sync_form_does_block_the_loop(cache_file, monkeypatch):
    """The liveness test above is not vacuous.

    Call the SYNC form -- the pre-fix shape at the ``_handle_sticker`` call
    site -- from inside a coroutine and the very same barrier starves the loop:
    the sibling task cannot tick until the rename is released.
    """

    async def scenario():
        held = _HeldReplace(monkeypatch)
        ticked = asyncio.Event()
        released_at_tick = threading.Event()

        async def sibling():
            await asyncio.sleep(0)
            ticked.set()

        asyncio.create_task(sibling())

        def _release_later():
            released_at_tick.set()
            held.release()

        releaser = threading.Timer(0.5, _release_later)
        releaser.start()
        try:
            sticker_cache.cache_sticker_description(
                "uid_inline", "A cat waving", emoji="🐱"
            )
        finally:
            releaser.cancel()

        # The loop was blocked for the whole rename: the sibling had no chance
        # to run, so the release necessarily happened FIRST.
        assert released_at_tick.is_set(), (
            "the inline write returned without the rename having been "
            "released -- the barrier did not actually hold, so the liveness "
            "test's guarantee is not being demonstrated"
        )
        assert not ticked.is_set(), (
            "the sibling task ran while the inline rename was held. The "
            "inline call is supposed to starve the loop; if it no longer "
            "does, the liveness test above proves nothing."
        )

    asyncio.run(scenario())


def test_the_write_runs_off_the_loop_thread(cache_file):
    """The rename must execute on a worker thread, not the loop thread."""
    seen: dict[str, int] = {}
    real_save = sticker_cache._save_cache

    def _record(cache):
        seen["thread"] = threading.get_ident()
        return real_save(cache)

    async def scenario():
        sticker_cache._save_cache = _record
        try:
            seen["loop"] = threading.get_ident()
            await sticker_cache.cache_sticker_description_async(
                "uid_thread", "A dog"
            )
        finally:
            sticker_cache._save_cache = real_save

    asyncio.run(scenario())
    assert seen["thread"] != seen["loop"], (
        "the cache write ran on the event-loop thread; the off-loop dispatch "
        "is not in effect"
    )


def test_the_async_wrapper_preserves_the_sync_contract(cache_file):
    """Same stored record either way -- the sync form is still the contract."""
    sticker_cache.cache_sticker_description(
        "uid_sync", "A happy dog", emoji="🐕", set_name="Dogs"
    )
    sync_record = dict(sticker_cache.get_cached_description("uid_sync"))

    asyncio.run(
        sticker_cache.cache_sticker_description_async(
            "uid_async", "A happy dog", emoji="🐕", set_name="Dogs"
        )
    )
    async_record = dict(sticker_cache.get_cached_description("uid_async"))

    sync_record.pop("cached_at")
    async_record.pop("cached_at")
    assert sync_record == async_record


def test_concurrent_writes_do_not_lose_an_entry(cache_file):
    """Off-loop dispatch removed the loop's accidental serialization.

    ``cache_sticker_description`` is a read-modify-write:
    ``_load_cache`` -> mutate -> ``_save_cache``.  ``os.replace`` makes each
    WRITE atomic; it does not make the TRIPLE atomic.  While the call was
    inline on the loop, the loop serialized every caller and the race could not
    be observed.  Moving it to a worker thread introduces real concurrency, so
    the lock must be introduced by the SAME change -- otherwise two stickers
    described at once silently drop one of the two descriptions.

    Asserts on the DURABLE FILE, not on the API.
    """
    # 2.0s, not longer: with the lock in place (the fixed shape) the second
    # caller cannot reach the barrier, so the first one MUST time out here.
    # That timeout is the green path's cost, so keep it small.
    entered = threading.Barrier(2, timeout=2.0)
    real_load = sticker_cache._load_cache
    first = threading.Event()

    def _interleaving_load():
        data = real_load()
        # Force both callers to read the SAME pre-state, which is what a real
        # lost update requires.  Only the first two callers rendezvous.
        if not first.is_set():
            try:
                entered.wait()
            except threading.BrokenBarrierError:
                pass
        return data

    async def scenario():
        sticker_cache._load_cache = _interleaving_load
        try:
            await asyncio.gather(
                sticker_cache.cache_sticker_description_async("uid_a", "A"),
                sticker_cache.cache_sticker_description_async("uid_b", "B"),
            )
        finally:
            first.set()
            sticker_cache._load_cache = real_load

    asyncio.run(scenario())

    durable = json.loads(cache_file.read_text())
    assert sorted(durable) == ["uid_a", "uid_b"], (
        "a concurrent read-modify-write lost an entry from the durable cache "
        f"file: keys = {sorted(durable)}. os.replace makes each write atomic, "
        "not the load/mutate/save triple."
    )


# ---------------------------------------------------------------------------
# Class sweep: the invariant, not an inventory.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class _BlockingCacheCallVisitor(ast.NodeVisitor):
    """Collect calls to the BLOCKING cache write inside ``async def`` bodies."""

    def __init__(self, rel: str):
        self.rel = rel
        self.hits: list[str] = []
        self.unawaited: list[str] = []
        self._depth = 0
        self._awaited: set[int] = set()

    def visit_FunctionDef(self, node):
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_Await(self, node):
        if isinstance(node.value, ast.Call):
            self._awaited.add(id(node.value))
        self.generic_visit(node)

    def visit_Call(self, node):
        if self._depth:
            name = node.func.attr if isinstance(
                node.func, ast.Attribute
            ) else getattr(node.func, "id", None)
            if name == "cache_sticker_description":
                self.hits.append(f"{self.rel}:{node.lineno}")
            elif name == "cache_sticker_description_async":
                if id(node) not in self._awaited:
                    self.unawaited.append(f"{self.rel}:{node.lineno}")
        self.generic_visit(node)


def _adapter_source_files() -> list[Path]:
    repo = _repo_root()
    files = sorted((repo / "plugins" / "platforms").rglob("*.py"))
    files += sorted((repo / "gateway" / "platforms").rglob("*.py"))
    return files


def test_no_coroutine_calls_the_blocking_sticker_cache_write():
    """No ``async def`` may call the BLOCKING cache write directly.

    This is the invariant, not a list: a second adapter that starts describing
    stickers fails here without anyone remembering to update an inventory.
    The sync form stays public for the non-loop callers, so it cannot simply
    be deleted.

    SCOPE, stated exactly.  This is a LEXICAL sweep -- it flags a call written
    inside an ``async def`` body.  A coroutine that reaches the blocking write
    INDIRECTLY, through a plain ``def`` helper, is not flagged here; that
    transitive class is the job of the reachability ratchet in
    ``tests/gateway/test_no_atomic_write_reachable_from_loop.py``, which walks
    the call graph.  The two gates are complementary and neither subsumes the
    other.
    """
    repo = _repo_root()
    hits: list[str] = []
    unawaited: list[str] = []
    for path in _adapter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        visitor = _BlockingCacheCallVisitor(str(path.relative_to(repo)))
        visitor.visit(tree)
        hits.extend(visitor.hits)
        unawaited.extend(visitor.unawaited)

    assert not hits, (
        "coroutine(s) call the BLOCKING cache_sticker_description directly. "
        "It ends in os.fsync + os.replace, which stalls every adapter and "
        "every in-flight turn for the duration of the rename. Use "
        "`await cache_sticker_description_async(...)`.\n"
        + "\n".join(f"  {h}" for h in hits)
    )
    assert not unawaited, (
        "call(s) to cache_sticker_description_async are NOT awaited, so the "
        "coroutine is never scheduled and the description is silently never "
        "cached -- every sticker re-pays a vision call forever.\n"
        + "\n".join(f"  {h}" for h in unawaited)
    )


def test_the_sweep_is_not_vacuous():
    """A green sweep over an empty file list proves nothing."""
    files = _adapter_source_files()
    assert len(files) >= 20, f"scanned only {len(files)} files; glob is broken"

    # Measured, not assumed: exactly the Telegram adapter's `_handle_sticker`
    # call site awaits the async form today.
    call_sites = sum(
        path.read_text(encoding="utf-8", errors="ignore").count(
            "cache_sticker_description_async("
        )
        for path in files
    )
    assert call_sites >= 1, (
        f"found only {call_sites} references to the async cache write in the "
        "adapter tree; the sweep is scanning the wrong files and would stay "
        "green even if the blocking form came back"
    )
