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

The reachability ratchet froze three of those coroutines
(``gateway/platforms/signal.py connect``, ``gateway/platforms/weixin.py
connect``, ``gateway/platforms/yuanbao.py open``) because they are the ones
whose DFS reports the takeover-marker sink first.  The other five reach the
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
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

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
    """

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.hits: list[str] = []
        self._depth = 0

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        saved, self._depth = self._depth, 0
        self.generic_visit(node)
        self._depth = saved

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            self._depth > 0
            and isinstance(func, ast.Attribute)
            and func.attr == "_acquire_platform_lock"
        ):
            self.hits.append(f"{self.rel}:{node.lineno}")
        self.generic_visit(node)


def test_no_coroutine_calls_the_blocking_platform_lock_acquire():
    """No ``async def`` may call the BLOCKING acquire directly.

    This is the invariant, not a list: a newly added adapter that calls the
    sync form from its ``connect()`` fails here without anyone remembering to
    update an inventory.  The sync method itself stays public for the non-loop
    callers, so it cannot simply be deleted.
    """
    repo = _repo_root()
    hits: list[str] = []
    for path in _adapter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        visitor = _CoroutineCallVisitor(str(path.relative_to(repo)))
        visitor.visit(tree)
        hits.extend(visitor.hits)

    assert not hits, (
        "coroutine(s) call the BLOCKING _acquire_platform_lock directly. On "
        "the --replace takeover path that writes a marker and then sleeps for "
        "up to ~15s inline on the event loop, stalling every adapter and every "
        "in-flight turn. Use `await self._acquire_platform_lock_async(...)`.\n"
        + "\n".join(f"  {h}" for h in hits)
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

    # 8 production call sites + the definition + the to_thread body reference.
    assert async_call_sites >= 8, (
        f"found only {async_call_sites} references to the async acquire; the "
        "off-loop form is not actually in use, so the sweep above is green "
        "for the wrong reason"
    )
