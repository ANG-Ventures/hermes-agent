# fork-only: behavioural companion to the reachability ratchet.
"""The platform-lock acquire must not block the event loop.

``BasePlatformAdapter._acquire_platform_lock`` is the single choke point every
adapter's ``connect()`` goes through to claim its credential lock.  It is a
plain ``def`` that, on the explicit ``--replace`` takeover path, performs:

    _acquire_platform_lock
      > take_over_scoped_lock_holder
        > _terminate_scoped_lock_owner_once
          > write_takeover_marker > _write_json_file -> atomic_json_write
          > _wait_for_scoped_lock_owner_exit -> time.sleep(0.5) x20, then
                                                time.sleep(0.25) x20

That is an atomic write plus up to ~15s of ``time.sleep`` executed inline on
whatever thread calls it.  Eight ``async def connect``/``open`` coroutines call
it directly, so on the running loop it is a hard stall of the entire gateway --
every other adapter's polling, every in-flight turn, every heartbeat.

The reachability ratchet froze four of those coroutines
(``gateway/platforms/signal.py connect``, ``gateway/platforms/weixin.py
connect``, ``gateway/platforms/yuanbao.py open`` and
``plugins/platforms/telegram/adapter.py connect``) because they are the ones
whose DFS reports the takeover-marker sink first.  The other four reach the
same blocking body; they are simply shadowed by a different first sink.

The fix is a choke-point one: ``_acquire_platform_lock_async`` offloads the
whole sync method to a worker thread, and every coroutine call site awaits it.
The sync method keeps its exact contract so the ~28 existing callers/tests that
drive it directly are unaffected.

These tests are about OBSERVABLE BEHAVIOUR -- can the loop keep running while
the acquire is in its slow path -- not about which API was called.
"""

from __future__ import annotations

import ast
import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from gateway import status
from gateway.platforms.base import BasePlatformAdapter


class _StubAdapter(BasePlatformAdapter):
    """Minimal concrete subclass; mirrors test_stale_platform_lock_retryable."""

    platform = MagicMock(value="telegram")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {}


def _adapter() -> _StubAdapter:
    obj = _StubAdapter.__new__(_StubAdapter)
    obj._running = True
    obj._fatal_error_code = None
    obj._fatal_error_message = None
    obj._fatal_error_retryable = True
    obj._fatal_error_handler = None
    obj._platform_lock_scope = None
    obj._platform_lock_identity = None
    obj._platform_lock_takeover_allowed = False
    obj._platform_lock_takeover_attempted = False
    obj._status_write_logged = None
    return obj


@pytest.fixture()
def adapter() -> _StubAdapter:
    return _adapter()


@pytest.mark.asyncio
async def test_loop_keeps_running_while_the_lock_acquire_is_stalled(
    adapter, monkeypatch
):
    """The loop must make progress while the acquire sits in its slow path.

    No wall-clock threshold: the acquire is held on an ``threading.Event``
    until the loop has demonstrably ticked, and the assertion is on the tick
    COUNT.  On the blocking shape the ticker cannot run at all, so the count is
    0 and the release never happens -- which is why the harness releases from a
    watchdog thread rather than from the loop.
    """
    entered = threading.Event()
    release = threading.Event()

    def _slow_acquire(scope: str, identity: str, resource_desc: str) -> bool:
        entered.set()
        # Stand in for write_takeover_marker + _wait_for_scoped_lock_owner_exit.
        release.wait(timeout=10)
        return True

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _slow_acquire)

    ticks = 0
    stop = False

    async def _ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.005)

    # Watchdog: release once the loop has proven it can still tick.  If the
    # acquire is inline on the loop, `ticks` stays frozen and the timeout fires,
    # which surfaces as the assertion below rather than a hang.
    def _watchdog() -> None:
        entered.wait(timeout=5)
        deadline = threading.Event()
        for _ in range(200):
            if ticks >= 5:
                break
            deadline.wait(0.01)
        release.set()

    watcher = threading.Thread(target=_watchdog, daemon=True)
    watcher.start()

    ticker = asyncio.ensure_future(_ticker())
    try:
        result = await adapter._acquire_platform_lock_async(
            "telegram-bot-token", "tok", "Telegram bot token"
        )
    finally:
        stop = True
        release.set()
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        watcher.join(timeout=5)

    assert result is True
    assert ticks >= 5, (
        "the event loop did not tick while the platform-lock acquire was in "
        f"its slow path (ticks={ticks}); the acquire is running inline on the "
        "loop, which is the ~15s connect stall this gate exists to prevent"
    )


@pytest.mark.asyncio
async def test_acquire_runs_off_the_loop_thread(adapter, monkeypatch):
    """The sync body must execute on a worker thread, not the loop thread."""
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _record(scope: str, identity: str, resource_desc: str) -> bool:
        seen["thread"] = threading.get_ident()
        return True

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _record)

    assert await adapter._acquire_platform_lock_async("s", "i", "d") is True
    assert seen["thread"] != loop_thread, (
        "the platform-lock acquire ran on the event-loop thread"
    )


@pytest.mark.asyncio
async def test_async_wrapper_preserves_the_sync_contract(adapter, monkeypatch):
    """Arguments, return value, and exceptions pass through unchanged.

    The sync method is the one the ~28 existing direct callers/tests drive; the
    wrapper must be a transport, not a reinterpretation.
    """
    calls: list[tuple] = []

    def _ok(scope: str, identity: str, resource_desc: str) -> bool:
        calls.append((scope, identity, resource_desc))
        return False

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _ok)
    assert await adapter._acquire_platform_lock_async("sc", "id", "rd") is False
    assert calls == [("sc", "id", "rd")]

    boom = RuntimeError("lock backend exploded")

    def _raise(scope: str, identity: str, resource_desc: str) -> bool:
        raise boom

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _raise)
    with pytest.raises(RuntimeError) as excinfo:
        await adapter._acquire_platform_lock_async("sc", "id", "rd")
    assert excinfo.value is boom, (
        "the wrapper must propagate the original exception object; adapters "
        "wrap this call in try/except and treat the failure as non-fatal"
    )


@pytest.mark.asyncio
async def test_fatal_error_state_set_from_the_worker_thread_is_visible(
    adapter, monkeypatch
):
    """State the sync body mutates must be visible after the await.

    ``_acquire_platform_lock`` failure calls ``_set_fatal_error``, which the
    runner reads to decide whether the connect failure is retryable.  Running
    the body on a worker thread must not lose that.
    """
    def _fail(scope: str, identity: str, resource_desc: str) -> bool:
        adapter._set_fatal_error(
            f"{scope}_lock", "already in use", retryable=True
        )
        return False

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _fail)
    monkeypatch.setattr(adapter, "_write_runtime_status_safe", lambda *a, **k: None)

    assert await adapter._acquire_platform_lock_async("sig", "acct", "rd") is False
    assert adapter._fatal_error_code == "sig_lock"
    assert adapter._fatal_error_retryable is True
    assert adapter._running is False


# ---------------------------------------------------------------------------
# Class sweep: enforce the invariant, do not enumerate the sites.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _adapter_source_files() -> list[Path]:
    repo = _repo_root()
    out: list[Path] = []
    for root in ("gateway", "plugins"):
        out.extend(sorted((repo / root).rglob("*.py")))
    return out


class _CoroutineCallVisitor(ast.NodeVisitor):
    """Collect ``_acquire_platform_lock`` calls made from inside a coroutine.

    Tracks the enclosing function so a hit inside a plain ``def`` (which is
    allowed to block) is not reported.  Nested plain ``def``s inside a
    coroutine are treated as non-coroutine context, matching the runtime: such
    a helper is only blocking when something calls it, and that call site is
    itself visited.

    Also collects every call to the ASYNC form and whether it is awaited.  An
    un-awaited ``_acquire_platform_lock_async(...)`` returns a coroutine
    object, which is always truthy -- so the production shape
    ``if not await self._acquire_platform_lock_async(...)`` silently becomes
    ``if not <coroutine>``, i.e. never taken.  The adapter would then connect
    with NO credential lock held and no error, which is worse than the
    blocking call this gate replaced.
    """

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.hits: list[str] = []
        self.async_calls: list[str] = []
        self.unawaited_async_calls: list[str] = []
        self._depth = 0
        self._awaited: set[int] = set()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        saved, self._depth = self._depth, 0
        self.generic_visit(node)
        self._depth = saved

    def visit_Await(self, node: ast.Await) -> None:
        self._awaited.add(id(node.value))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            self._depth > 0
            and isinstance(func, ast.Attribute)
            and func.attr == "_acquire_platform_lock"
        ):
            self.hits.append(f"{self.rel}:{node.lineno}")
        if isinstance(func, ast.Attribute) and func.attr == "_acquire_platform_lock_async":
            where = f"{self.rel}:{node.lineno}"
            self.async_calls.append(where)
            if id(node) not in self._awaited:
                self.unawaited_async_calls.append(where)
        self.generic_visit(node)


def test_no_coroutine_calls_the_blocking_platform_lock_acquire():
    """No ``async def`` may call the BLOCKING acquire directly.

    This is the invariant, not a list: a newly added adapter that calls the
    sync form from its ``connect()`` fails here without anyone remembering to
    update an inventory.  The sync method itself stays public for the non-loop
    callers, so it cannot simply be deleted.

    SCOPE, stated exactly.  This is a LEXICAL sweep: it flags a call written
    inside an ``async def`` body.  A coroutine that reaches the blocking
    acquire INDIRECTLY -- through a plain ``def`` helper that itself calls it
    -- is not flagged here.  That transitive class is the job of the
    reachability ratchet in
    ``tests/gateway/test_no_atomic_write_reachable_from_loop.py``, which walks
    the call graph; the two gates are complementary and neither subsumes the
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
        visitor = _CoroutineCallVisitor(str(path.relative_to(repo)))
        visitor.visit(tree)
        hits.extend(visitor.hits)
        unawaited.extend(visitor.unawaited_async_calls)

    assert not hits, (
        "coroutine(s) call the BLOCKING _acquire_platform_lock directly. On "
        "the --replace takeover path that writes a marker and then sleeps for "
        "up to ~15s inline on the event loop, stalling every adapter and every "
        "in-flight turn. Use `await self._acquire_platform_lock_async(...)`.\n"
        + "\n".join(f"  {h}" for h in hits)
    )

    assert not unawaited, (
        "call(s) to _acquire_platform_lock_async are NOT awaited. The call "
        "then evaluates to a coroutine object — always truthy — so the "
        "production guard `if not await self._acquire...` becomes `if not "
        "<coroutine>` and never fires: the adapter connects with NO credential "
        "lock held, silently, and the acquire never even runs. That is worse "
        "than the blocking call this gate replaced, and the sweep above cannot "
        "see it because the blocking form is gone from the call site.\n"
        + "\n".join(f"  {h}" for h in unawaited)
    )


def test_the_sweep_is_not_vacuous():
    """The sweep must actually parse adapter code and see the async form.

    A green sweep over an empty file list, or over a tree where the async form
    does not exist, proves nothing.
    """
    repo = _repo_root()
    files = _adapter_source_files()
    assert len(files) >= 20, f"scanned only {len(files)} files; the glob is broken"

    async_call_sites = 0
    for path in files:
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        async_call_sites += src.count("_acquire_platform_lock_async(")

    # 8 production call sites (one per adapter) + the `async def` declaration
    # in base.py.  Measured, not assumed: the body of the wrapper calls the
    # SYNC `_acquire_platform_lock`, so it does not carry this token.
    assert async_call_sites >= 9, (
        f"found only {async_call_sites} references to the async acquire; the "
        "off-loop form is not actually in use, so the sweep above is green "
        "for the wrong reason"
    )


# ---------------------------------------------------------------------------
# Cancellation: the await point this change INTRODUCES must not leak the lock.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_the_acquire_does_not_leak_the_scoped_lock(monkeypatch):
    """A cancelled connect() must not leave the scoped lock held by nobody.

    Moving the body to a worker thread adds an await point where the blocking
    call had none, so ``connect()`` is now cancellable (shutdown, reconnect
    supervisor, adapter teardown) WHILE the acquire is in flight.
    ``asyncio.to_thread`` cannot interrupt the thread -- it runs on and can take
    the lock after its awaiter is gone.  The scoped lock is machine-global, so
    an orphaned acquire makes every later connect fail "already in use" until
    the process restarts.

    THE TEARDOWN CASE.  The cancellation almost always comes FROM adapter
    teardown, and teardown calls ``_release_platform_lock()`` -- 18 sites across
    signal/yuanbao/qqbot/weixin/discord/telegram/slack/whatsapp -- which clears
    ``_platform_lock_identity``.  Any cancel-path release that re-derives the
    identity from the adapter is therefore a silent no-op exactly when it
    matters.  This test runs that teardown between the worker taking the lock
    and the cancellation landing.

    The oracle is the RESOURCE -- ``gateway.status``'s scoped-lock registry --
    not which adapter method was called.  Regression for FleetReview P1
    "Orphaned lock on cancel" (round 2) and its round-4 teardown variant.
    """
    held: set[tuple[str, str]] = set()
    entered = threading.Event()
    proceed = threading.Event()
    worker_finished = threading.Event()

    def _fake_acquire(scope: str, identity: str, metadata=None):
        entered.set()
        proceed.wait(timeout=10)
        held.add((scope, identity))
        worker_finished.set()
        return True, None

    def _fake_release(scope: str, identity: str) -> None:
        held.discard((scope, identity))

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(status, "release_scoped_lock", _fake_release)

    adapter = _adapter()
    task = asyncio.ensure_future(
        adapter._acquire_platform_lock_async("telegram-bot-token", "tok", "d")
    )
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    # Adapter teardown, exactly as the production sites do it: this clears
    # _platform_lock_identity while the acquire is still in flight.
    adapter._release_platform_lock()
    assert adapter._platform_lock_identity is None

    task.cancel()
    # Release the worker so it completes its acquire during the cancellation.
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Join the worker before asserting.  On the broken (abandoned-drain)
    # shape the worker OUTLIVES the await, so a bare assertion here passes
    # vacuously — the lock has simply not been taken yet.
    assert await asyncio.get_running_loop().run_in_executor(
        None, worker_finished.wait, 10
    ), "the acquire worker never finished"

    assert held == set(), (
        "a cancelled acquire left the machine-global scoped lock held by an "
        f"orphaned worker thread: {held}. Every later connect will fail "
        "'already in use' until the process restarts."
    )


@pytest.mark.asyncio
async def test_cancellation_still_propagates(adapter, monkeypatch):
    """Draining the orphaned worker must not swallow the cancellation.

    The caller asked to be cancelled; holding the lock correctly is not a
    licence to return normally and let connect() proceed.
    """
    def _acquire(scope: str, identity: str, resource_desc: str) -> bool:
        return False

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _acquire)

    task = asyncio.ensure_future(
        adapter._acquire_platform_lock_async("s", "i", "d")
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_failing_worker_still_surfaces_as_cancellation(monkeypatch):
    """A worker exception must not REPLACE the caller's CancelledError.

    The drain re-raises whatever the worker thread produced if it is written
    as ``except Exception: raise``.  A takeover-marker write failure would then
    reach ``connect()``'s caller as a ``RuntimeError`` instead of
    ``CancelledError``, so the caller treats the task as "failed, keep going"
    rather than "unwinding" -- and proceeds after teardown has already run.

    Regression for FleetReview P1 at base.py:3822 (round 4).
    """
    entered = threading.Event()
    proceed = threading.Event()

    def _fake_acquire(scope: str, identity: str, metadata=None):
        entered.set()
        proceed.wait(timeout=10)
        raise RuntimeError("takeover marker write failed")

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(status, "release_scoped_lock", lambda scope, identity: None)

    adapter = _adapter()
    task = asyncio.ensure_future(adapter._acquire_platform_lock_async("s", "i", "d"))
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    task.cancel()
    proceed.set()

    with pytest.raises(asyncio.CancelledError):
        await task



@pytest.mark.asyncio
async def test_the_orphan_drain_does_not_delete_a_retry_connects_live_lock(
    monkeypatch,
):
    """The drain must not release a lock a RETRY connect already re-acquired.

    The scoped lock is ONE machine-global file per ``(scope, identity)``, and
    the cancel-path release goes by that pair -- so it deletes whoever holds
    it, not "our" acquisition.  While the drain waits for the orphaned worker,
    the reconnect supervisor routinely brings a fresh connect up for the same
    credential; that connect is LIVE and using the lock.  Releasing it there
    is the same orphaning failure this PR exists to prevent, pointed the other
    way: a running adapter loses its credential lock and the next connect
    anywhere on the machine can steal it.

    Oracle is the resource.  Regression for the FleetReview P1 at
    base.py:3832 on record ``7a21d08c3f`` ("Orphan-drain release can delete a
    scoped lock that a concurrent retry connect already re-acquired").
    """
    held: set[tuple[str, str]] = set()
    entered = threading.Event()
    proceed = threading.Event()
    slow_done = threading.Event()

    def _fake_acquire(scope: str, identity: str, metadata=None):
        # Only the first (soon-orphaned) acquire is slow; the retry connect
        # that follows must be able to take the lock immediately.
        if not slow_done.is_set():
            slow_done.set()
            entered.set()
            proceed.wait(timeout=10)
        # Model the real contract: the second element is the PRE-EXISTING
        # record, so it is non-None exactly when somebody already held the pair.
        # `acquire_scoped_lock` self-reacquires for the calling PID, so both of
        # these acquires succeed.
        existing = {"pid": os.getpid()} if (scope, identity) in held else None
        held.add((scope, identity))
        return True, existing

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(
        status, "release_scoped_lock", lambda s, i: held.discard((s, i))
    )

    adapter = _adapter()
    task = asyncio.ensure_future(
        adapter._acquire_platform_lock_async("telegram-bot-token", "tok", "d")
    )
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    adapter._release_platform_lock()
    task.cancel()

    # The reconnect supervisor's retry connect succeeds and now holds the lock.
    retry = _adapter()
    assert retry._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    assert ("telegram-bot-token", "tok") in held

    # Now let the orphaned worker finish and the drain run its release.
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ("telegram-bot-token", "tok") in held, (
        "the cancelled acquire's drain released the machine-global scoped "
        "lock that a live retry connect had already re-acquired; that adapter "
        "is now running without its credential lock."
    )


@pytest.mark.asyncio
async def test_a_second_cancel_during_the_drain_still_does_not_orphan_the_lock(
    monkeypatch,
):
    """Repeat cancellations must not abandon the drain.

    Teardown routinely cancels the same connect task more than once -- the
    reconnect supervisor, the adapter's own teardown and gateway shutdown all
    target it.  If the second cancel breaks the drain, the worker thread runs
    on, takes the machine-global lock, and nobody is left to release it: the
    exact orphan this drain exists to close, just one cancel later.

    Regression for the FleetReview P1 at
    ``tests/gateway/test_platform_lock_acquire_off_loop.py:344`` on record
    ``0e350d48b7`` ("Cancellation test only covers a single cancel").
    """
    held: set[tuple[str, str]] = set()
    entered = threading.Event()
    proceed = threading.Event()
    worker_finished = threading.Event()

    def _fake_acquire(scope: str, identity: str, metadata=None):
        entered.set()
        proceed.wait(timeout=10)
        held.add((scope, identity))
        worker_finished.set()
        return True, None

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(
        status, "release_scoped_lock", lambda s, i: held.discard((s, i))
    )

    adapter = _adapter()
    task = asyncio.ensure_future(
        adapter._acquire_platform_lock_async("telegram-bot-token", "tok", "d")
    )
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    adapter._release_platform_lock()
    task.cancel()
    await asyncio.sleep(0)
    # Second cancel, delivered while the drain is awaiting the worker.
    task.cancel()
    await asyncio.sleep(0)

    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The worker thread OUTLIVES an abandoned drain, so asserting straight
    # after the await would be vacuously green on the broken shape: the lock
    # simply has not been taken yet.  Join the worker first, then assert.
    assert await asyncio.get_running_loop().run_in_executor(
        None, worker_finished.wait, 10
    ), "the acquire worker never finished"

    assert held == set(), (
        "a second cancellation abandoned the drain, so the worker thread took "
        f"the machine-global scoped lock with no owner left to release it: {held}"
    )


@pytest.mark.asyncio
async def test_the_drain_does_not_release_while_a_sibling_acquisition_is_live(
    monkeypatch,
):
    """Refcount, not "am I the only record".

    ``acquire_scoped_lock`` SELF-REACQUIRES for the calling PID, so an orphaned
    worker and a live retry connect can BOTH hold the same pair.  The drain must
    decide on whether any OTHER live acquisition remains -- not on a count, and
    not on being the newest.  This pins the case where the drain drops its own
    token and a sibling's acquisition is still recorded: the lock must survive.
    """
    from gateway.platforms import base as base_mod

    held: set[tuple[str, str]] = set()
    entered = threading.Event()
    proceed = threading.Event()
    worker_finished = threading.Event()

    def _fake_acquire(scope: str, identity: str, metadata=None):
        existing = {"pid": os.getpid()} if (scope, identity) in held else None
        held.add((scope, identity))
        return True, existing

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(
        status, "release_scoped_lock", lambda s, i: held.discard((s, i))
    )

    adapter = _adapter()
    real_acquire = adapter._acquire_platform_lock

    def _slow(scope: str, identity: str, resource_desc: str) -> bool:
        entered.set()
        proceed.wait(timeout=10)
        try:
            return real_acquire(scope, identity, resource_desc)
        finally:
            worker_finished.set()

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _slow)

    task = asyncio.ensure_future(
        adapter._acquire_platform_lock_async("telegram-bot-token", "tok", "d")
    )
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    # A retry connect comes up for the same credential and is STILL LIVE when
    # our orphaned worker finishes.
    retry = _adapter()
    assert retry._acquire_platform_lock("telegram-bot-token", "tok", "d") is True

    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.get_running_loop().run_in_executor(
        None, worker_finished.wait, 10
    ), "the acquire worker never finished"
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert held == {("telegram-bot-token", "tok")}, (
        "the drain deleted the machine-global lock while a live retry connect "
        "was still holding it"
    )
    # ...and the drain must have dropped ITS OWN acquisition, or the pair stays
    # pinned forever and the retry's own teardown can never release it.
    assert base_mod._platform_lock_holder_count("telegram-bot-token", "tok") == 1, (
        "the cancelled acquire leaked its acquisition record; the pair is now "
        "permanently pinned and no later release of it can ever fire"
    )
    retry._release_platform_lock()
    assert held == set(), (
        "the live holder's own teardown could not release the lock, because a "
        "drained acquisition was still on record"
    )


def test_the_ownership_token_is_recorded_by_the_sync_choke_point(monkeypatch):
    """The ownership guard must be driven by real acquisitions, not by the drain.

    Non-vacuity floor for the cancel-path tests above: if
    ``_note_platform_lock_acquired`` stopped being called from
    :meth:`_acquire_platform_lock`, no acquisition would ever be recorded, the
    drain's token would never match, and the cancel-path release would silently
    become the no-op that orphans the lock.
    """
    from gateway.platforms import base as base_mod

    monkeypatch.setattr(
        status, "acquire_scoped_lock", lambda s, i, metadata=None: (True, None)
    )

    adapter = _adapter()
    assert adapter._acquire_platform_lock("telegram-bot-token", "tok", "d") is True

    token = adapter._platform_lock_token
    assert token is not None, (
        "a successful acquire recorded no ownership token; the cancel-path and "
        "teardown release guards have nothing to match against"
    )
    assert base_mod._platform_lock_holder_count("telegram-bot-token", "tok") == 1, (
        "the acquiring adapter is not on record as a live holder of the pair "
        "it just acquired"
    )


def test_a_dropped_adapter_does_not_keep_ownership_forever(monkeypatch):
    """A record whose adapter is gone must not keep the pair pinned.

    Adapters are dropped without teardown (a failed connect, a GC'd retry).  If
    the acquisition record outlived them, the pair would look permanently held
    by a live sibling and no later release would ever fire -- turning the orphan
    bug back on permanently.
    """
    import gc

    from gateway.platforms import base as base_mod

    monkeypatch.setattr(
        status, "acquire_scoped_lock", lambda s, i, metadata=None: (True, None)
    )

    doomed = _adapter()
    assert doomed._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    assert base_mod._platform_lock_holder_count("telegram-bot-token", "tok") == 1

    del doomed
    gc.collect()

    assert base_mod._platform_lock_holder_count("telegram-bot-token", "tok") == 0, (
        "a dropped adapter's acquisition record kept the pair pinned; no later "
        "release of that lock can ever fire"
    )


@pytest.mark.asyncio
async def test_the_drain_releases_even_when_an_unrelated_connect_churned_the_pair(
    monkeypatch,
):
    """Ownership is not a counter delta.

    The previous guard released only when the acquisition GENERATION advanced by
    exactly one while the drain ran.  That is a proxy, and an unrelated connect
    that acquires and RELEASES the same ``(scope, identity)`` while our worker is
    blocked advances it by two -- so the drain refuses to release a lock our own
    worker really does hold, and it is orphaned: every later connect anywhere on
    the machine fails "already in use" until the process restarts.

    Oracle is the resource.  Regression for the FleetReview P1 at
    ``base.py:3929``/``:3878`` on record ``401cd4c23e``.
    """
    held: set[tuple[str, str]] = set()
    entered = threading.Event()
    proceed = threading.Event()
    worker_finished = threading.Event()
    slow_done = threading.Event()

    def _fake_acquire(scope: str, identity: str, metadata=None):
        # Models the real contract: the second element is the PRE-EXISTING
        # record, and `acquire_scoped_lock` self-reacquires for the calling PID.
        existing = {"pid": os.getpid()} if (scope, identity) in held else None
        held.add((scope, identity))
        return True, existing

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(
        status, "release_scoped_lock", lambda s, i: held.discard((s, i))
    )

    adapter = _adapter()
    real_acquire = adapter._acquire_platform_lock

    def _slow(scope: str, identity: str, resource_desc: str) -> bool:
        if not slow_done.is_set():
            slow_done.set()
            entered.set()
            proceed.wait(timeout=10)
        try:
            return real_acquire(scope, identity, resource_desc)
        finally:
            worker_finished.set()

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _slow)

    task = asyncio.ensure_future(
        adapter._acquire_platform_lock_async("telegram-bot-token", "tok", "d")
    )
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    # An UNRELATED connect for the same credential acquires and releases while
    # our worker is still blocked: +2 on any counter, and gone by the time we
    # finish.
    other = _adapter()
    assert other._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    other._release_platform_lock()
    assert held == set()

    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.get_running_loop().run_in_executor(
        None, worker_finished.wait, 10
    ), "the acquire worker never finished"
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert held == set(), (
        "the drain refused to release a lock its own worker took, because an "
        f"unrelated connect had churned the same pair: {held}. Every later "
        "connect will fail 'already in use' until the process restarts."
    )


@pytest.mark.asyncio
async def test_teardown_after_a_cancelled_acquire_does_not_release_twice(
    monkeypatch,
):
    """Teardown must not delete a retry connect's live lock.

    The sync acquire sets ``_platform_lock_identity`` BEFORE it acquires, and
    the cancel drain releases without clearing it -- so the adapter still points
    at the pair after its lock is gone.  Teardown always runs, so an
    identity-only release fires a SECOND time and deletes whatever retry connect
    has since legitimately claimed that machine-global pair.

    Regression for the FleetReview P1 at ``base.py:3940`` on record
    ``401cd4c23e``.
    """
    held: set[tuple[str, str]] = set()
    entered = threading.Event()
    proceed = threading.Event()
    worker_finished = threading.Event()

    def _fake_acquire(scope: str, identity: str, metadata=None):
        # Models the real contract: the second element is the PRE-EXISTING
        # record, and `acquire_scoped_lock` self-reacquires for the calling PID.
        existing = {"pid": os.getpid()} if (scope, identity) in held else None
        held.add((scope, identity))
        return True, existing

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(
        status, "release_scoped_lock", lambda s, i: held.discard((s, i))
    )

    adapter = _adapter()
    real_acquire = adapter._acquire_platform_lock

    def _slow(scope: str, identity: str, resource_desc: str) -> bool:
        entered.set()
        proceed.wait(timeout=10)
        try:
            return real_acquire(scope, identity, resource_desc)
        finally:
            worker_finished.set()

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _slow)

    task = asyncio.ensure_future(
        adapter._acquire_platform_lock_async("telegram-bot-token", "tok", "d")
    )
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.get_running_loop().run_in_executor(
        None, worker_finished.wait, 10
    ), "the acquire worker never finished"
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert held == set(), "the drain should have released its own lock"

    # A retry connect now legitimately holds the machine-global pair ...
    retry = _adapter()
    assert retry._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    assert held == {("telegram-bot-token", "tok")}

    # ... and the cancelled adapter's teardown runs, as it always does.
    adapter._release_platform_lock()

    assert held == {("telegram-bot-token", "tok")}, (
        "the cancelled adapter's teardown released the pair a second time and "
        "deleted a LIVE retry connect's machine-global lock; that adapter is "
        "now running without its credential lock."
    )


# ---------------------------------------------------------------------------
# Round-5 regressions: the ownership bookkeeping and the RESOURCE operation it
# authorises must be ONE critical section per (scope, identity) pair.
#
# FleetReview record ``576840bc1b`` raised three P1s that are the same class
# seen from three sides -- a decision taken against registry state observed in
# a DIFFERENT critical section than the one that acts on the lock file:
#
#   base.py:118  stale-holder wipe race       (acquire vs acquire)
#   base.py:145  unlock race / double unlock  (release vs acquire)
#   base.py:4188 teardown uses the overwritten token (reconnect on one adapter)
#
# Every oracle below is the RESOURCE -- which ``(scope, identity)`` pairs are
# held in the scoped-lock registry -- never which method was called.
# ---------------------------------------------------------------------------


def _held_registry(monkeypatch, held: "set[tuple[str, str]]"):
    """Install a scoped-lock fake that mirrors the real acquire contract.

    ``acquire_scoped_lock`` returns ``(acquired, pre_existing_record)``: the
    second element is non-None exactly when somebody already held the pair, and
    an acquire by the CALLING pid always succeeds (self-reacquire).  Both
    properties are load-bearing for these tests.
    """
    guard = threading.Lock()

    def _acquire(scope: str, identity: str, metadata=None):
        with guard:
            existing = {"pid": os.getpid()} if (scope, identity) in held else None
            held.add((scope, identity))
        return True, existing

    def _release(scope: str, identity: str) -> None:
        with guard:
            held.discard((scope, identity))

    monkeypatch.setattr(status, "acquire_scoped_lock", _acquire)
    monkeypatch.setattr(status, "release_scoped_lock", _release)
    return _acquire, _release


def test_a_concurrent_acquire_cannot_wipe_a_live_holders_record(monkeypatch):
    """Two concurrent acquires of one pair: neither may prune the other.

    ``_note_platform_lock_acquired`` prunes every recorded acquisition when its
    caller observed ``had_prior_holder=False``.  That observation comes from
    ``acquire_scoped_lock`` and is only valid while nothing else acquires the
    pair -- so if the acquire and the record are separate critical sections, a
    sibling acquire completing in the window records with a STALE
    ``had_prior_holder=False`` and wipes the live holder.

    The consequence is asserted on the resource: with its record gone, the live
    adapter is invisible to the ownership guard, so the other adapter's routine
    teardown deletes the machine-global lock that live adapter is using.

    Regression for FleetReview P1 ``gateway/platforms/base.py:118`` on record
    ``576840bc1b``.
    """
    from gateway.platforms import base as base_mod

    held: set[tuple[str, str]] = set()
    guard = threading.Lock()
    observed = threading.Event()
    may_return = threading.Event()
    parked = {"done": False}

    def _acquire(scope: str, identity: str, metadata=None):
        with guard:
            existing = {"pid": os.getpid()} if (scope, identity) in held else None
            held.add((scope, identity))
        if not parked["done"]:
            # The FIRST acquire has read the pre-state; park it before it
            # returns so the second can complete an entire acquire+record in
            # the window its observation is already stale for.
            parked["done"] = True
            observed.set()
            may_return.wait(timeout=10)
        return True, existing

    monkeypatch.setattr(status, "acquire_scoped_lock", _acquire)
    monkeypatch.setattr(
        status, "release_scoped_lock", lambda s, i: held.discard((s, i))
    )

    parked_adapter = _adapter()
    live = _adapter()

    parked_thread = threading.Thread(
        target=parked_adapter._acquire_platform_lock,
        args=("telegram-bot-token", "tok", "d"),
        daemon=True,
    )
    parked_thread.start()
    assert observed.wait(timeout=5), "the first acquire never ran"

    live_result: list[bool] = []
    live_thread = threading.Thread(
        target=lambda: live_result.append(
            live._acquire_platform_lock("telegram-bot-token", "tok", "d")
        ),
        daemon=True,
    )
    live_thread.start()
    # Give the second acquire a real chance to land inside the window.  Under
    # the fix it simply waits on the pair lock, which is the whole point.
    live_thread.join(timeout=1.0)

    may_return.set()
    parked_thread.join(timeout=10)
    live_thread.join(timeout=10)
    assert live_result == [True], "the live adapter never completed its acquire"

    live_token = live._platform_lock_token
    holders = base_mod._PLATFORM_LOCK_OWNERS.get(("telegram-bot-token", "tok"), {})
    assert live_token in holders, (
        "a concurrent acquire wiped a LIVE holder's ownership record; the "
        "ownership guard can no longer see it, so any other release deletes "
        "its machine-global lock"
    )

    # The consequence, on the resource: routine teardown of the other adapter.
    parked_adapter._release_platform_lock()
    assert held == {("telegram-bot-token", "tok")}, (
        "a live, connected adapter lost its machine-global credential lock to "
        "another adapter's routine teardown"
    )


def test_a_retry_connect_in_the_release_window_keeps_its_lock(monkeypatch):
    """The ownership claim and the release it authorises are one section.

    ``_claim_platform_lock_release`` answers "no other live acquisition
    remains, you may release".  That answer is only valid until somebody
    acquires, so if the claim and ``release_scoped_lock`` are separate critical
    sections a retry connect landing in between -- the reconnect supervisor
    does exactly this -- has taken the machine-global lock and then loses it.

    Regression for FleetReview P1s ``gateway/platforms/base.py:145`` ("Unlock
    race") and ``:4054`` ("Double unlock") on record ``576840bc1b``.
    """
    held: set[tuple[str, str]] = set()
    _held_registry(monkeypatch, held)

    leaving = _adapter()
    assert leaving._acquire_platform_lock("telegram-bot-token", "tok", "d") is True

    in_window = threading.Event()
    retry_done = threading.Event()
    original = type(leaving)._release_platform_lock_identity

    def _slow_release_identity(self, scope: str, identity: str) -> None:
        # Widen the real (small) window between the ownership claim and the
        # release, deterministically rather than by racing.
        in_window.set()
        retry_done.wait(timeout=10)
        original(self, scope, identity)

    monkeypatch.setattr(
        type(leaving), "_release_platform_lock_identity", _slow_release_identity
    )

    teardown = threading.Thread(target=leaving._release_platform_lock, daemon=True)
    teardown.start()
    assert in_window.wait(timeout=5), "teardown never reached the release window"

    retry = _adapter()
    retry_result: list[bool] = []
    retry_thread = threading.Thread(
        target=lambda: retry_result.append(
            retry._acquire_platform_lock("telegram-bot-token", "tok", "d")
        ),
        daemon=True,
    )
    retry_thread.start()
    # Under the fix the retry blocks on the pair lock until the release
    # completes; on the broken shape it acquires inside the window.
    retry_thread.join(timeout=1.0)

    retry_done.set()
    teardown.join(timeout=10)
    retry_thread.join(timeout=10)
    assert retry_result == [True], "the retry connect never completed its acquire"

    assert held == {("telegram-bot-token", "tok")}, (
        "a live retry connect lost the machine-global credential lock it had "
        "just acquired, because a concurrent teardown released the pair on a "
        "stale ownership claim"
    )


def test_a_reconnect_on_one_adapter_does_not_strand_its_first_token(monkeypatch):
    """A re-acquire on the SAME adapter must retire its previous token.

    The reconnect supervisor drives the same adapter object through
    ``connect()`` again, so ``_note_acquired_platform_lock`` overwrites
    ``_platform_lock_token``.  The superseded token stays in the pair's
    live-holder set forever -- nothing drops it, and the weakref prune cannot
    help because the adapter is the same live object.  The pair then looks
    permanently held by a live sibling, so no later release ever fires and
    every later connect on the machine fails "already in use" until the
    process restarts.

    Regression for FleetReview P1 ``gateway/platforms/base.py:4188``
    ("Teardown uses overwritten token") on record ``576840bc1b``.
    """
    from gateway.platforms import base as base_mod

    held: set[tuple[str, str]] = set()
    _held_registry(monkeypatch, held)

    adapter = _adapter()
    assert adapter._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    first_token = adapter._platform_lock_token

    # The reconnect: same adapter, same credential.  acquire_scoped_lock
    # self-reacquires for this pid, so it succeeds.
    assert adapter._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    assert adapter._platform_lock_token != first_token

    assert base_mod._platform_lock_holder_count("telegram-bot-token", "tok") == 1, (
        "a reconnect on one adapter left its superseded acquisition on record; "
        "the pair now looks held by a phantom live sibling"
    )

    adapter._release_platform_lock()
    assert held == set(), (
        "the sole adapter holding this credential could not release its "
        "machine-global lock on teardown, because its own stranded token made "
        "the pair look held by somebody else -- every later connect fails "
        "'already in use' until the process restarts"
    )
