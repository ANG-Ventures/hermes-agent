# fork-only: upstream AGENTS.md forbids source-reading tests; do not port.
"""Sync file I/O residue on the gateway event-loop thread (t_1fd05a3a).

Follow-up of t_620ba53d (#1628).  Each GIL-releasing syscall on the loop
thread waits out the 5ms switch interval whenever another thread holds the
GIL (kanban dispatcher tick, psutil sweep), so small file I/O on the loop is
amplified ~1000x.  PHASE=event_loop_blocked dumps 2026-10-01 23:00 ->
2026-10-02 02:07 named these sites, all reached from a coroutine:

1. ``BasePlatformAdapter._set_fatal_error`` -> ``_write_runtime_status_safe``
   -> ``gateway.status.write_runtime_status`` -> ``utils.atomic_json_write``
   (utils.py:387) -- Discord liveness sampler, Telegram polling-error handler.
2. ``gateway/status.py`` ``release_scoped_lock`` (unlink) -- adapter
   ``disconnect()`` -> ``_release_platform_lock``.
3. ``gateway/drain_control.py`` ``read_drain_request`` -- the 1s
   ``_drain_control_watcher`` tick.
4. ``hermes_cli/plugins.py`` ``_plugin_home_key`` ``Path.resolve()`` -- every
   ``has_hook`` from a Discord event handler.

One test per site proves the I/O leaves the loop thread; AST guards pin the
shape so a refactor cannot silently put it back.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from gateway import status
from gateway.platforms.base import BasePlatformAdapter


class _StubAdapter(BasePlatformAdapter):
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


def _calls_named(fn, name: str) -> list[ast.Call]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                out.append(node)
    return out


# ---------------------------------------------------------------------------
# Site 1: adapter runtime-status write (_set_fatal_error and friends)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_fatal_error_on_loop_writes_status_off_the_loop_thread(monkeypatch):
    loop_thread = threading.get_ident()
    seen: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        status,
        "write_runtime_status",
        lambda **kw: seen.append((threading.get_ident(), kw)),
    )
    adapter = _adapter()

    adapter._set_fatal_error("telegram_network", "boom", retryable=True)

    # State the runner reads is set synchronously, before any await.
    assert adapter._fatal_error_code == "telegram_network"
    assert adapter._running is False
    status.drain_runtime_status_lane()
    assert len(seen) == 1
    tid, kw = seen[0]
    assert tid != loop_thread, "runtime-status write ran on the event-loop thread"
    assert kw["platform"] == "telegram"
    assert kw["platform_state"] == "fatal"
    assert kw["error_code"] == "telegram_network"


@pytest.mark.asyncio
async def test_slow_adapter_status_write_does_not_stall_the_loop(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(
        status, "write_runtime_status", lambda **kw: gate.wait(5)
    )
    adapter = _adapter()

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    adapter._mark_connected()
    await asyncio.sleep(0)
    assert loop.time() - t0 < 1.0, "the loop waited on the status write"
    gate.set()
    status.drain_runtime_status_lane()


def test_adapter_status_write_without_a_loop_stays_inline(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(
        status, "write_runtime_status", lambda **kw: seen.append(threading.get_ident())
    )
    _adapter()._mark_disconnected()
    assert seen == [threading.get_ident()]


@pytest.mark.asyncio
async def test_adapter_status_write_failure_is_still_logged(monkeypatch, caplog):
    def _boom(**_kw):
        raise OSError("ENOSPC")

    monkeypatch.setattr(status, "write_runtime_status", _boom)
    adapter = _adapter()
    with caplog.at_level("WARNING"):
        adapter._set_fatal_error("x", "y", retryable=False)
        status.drain_runtime_status_lane()
    assert "Failed to write runtime status (fatal)" in caplog.text


@pytest.mark.asyncio
async def test_adapter_status_write_lands_on_disk_in_order(tmp_path, monkeypatch):
    """Real write path (temp HERMES_HOME): fatal then connected, last wins."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    status._reset_identity_caches()
    adapter = _adapter()
    adapter._set_fatal_error("c", "m", retryable=True)
    adapter._mark_connected()
    status.drain_runtime_status_lane()
    state = status.read_runtime_status()
    assert state["platforms"]["telegram"]["state"] == "connected"


def test_write_runtime_status_safe_routes_through_the_ordered_lane():
    calls = _calls_named(BasePlatformAdapter._write_runtime_status_safe, "submit_runtime_status_job")
    assert calls, "_write_runtime_status_safe must queue on the ordered status lane"


# ---------------------------------------------------------------------------
# Site 2: scoped-lock release from adapter teardown
# ---------------------------------------------------------------------------


def _held_registry(monkeypatch, held: set, release_threads: list):
    def _fake_acquire(scope, identity, metadata=None):
        existing = {"pid": 1} if (scope, identity) in held else None
        held.add((scope, identity))
        return True, existing

    def _fake_release(scope, identity):
        release_threads.append(threading.get_ident())
        held.discard((scope, identity))

    monkeypatch.setattr(status, "acquire_scoped_lock", _fake_acquire)
    monkeypatch.setattr(status, "release_scoped_lock", _fake_release)


@pytest.mark.asyncio
async def test_teardown_release_runs_off_the_loop_thread(monkeypatch):
    held: set = set()
    threads: list[int] = []
    _held_registry(monkeypatch, held, threads)
    adapter = _adapter()
    assert adapter._acquire_platform_lock("telegram-bot-token", "tok", "d") is True

    adapter._release_platform_lock()
    status.drain_runtime_status_lane()

    assert held == set()
    assert threads and threads[0] != threading.get_ident(), (
        "release_scoped_lock ran on the event-loop thread"
    )


@pytest.mark.asyncio
async def test_queued_release_does_not_delete_a_retry_connects_lock(monkeypatch):
    """A retry connect that acquires before the queued unlink runs keeps its lock."""
    held: set = set()
    threads: list[int] = []
    _held_registry(monkeypatch, held, threads)
    gate = threading.Event()
    # Park the lane so the release is still queued when the retry acquires.
    status.submit_runtime_status_job(lambda: gate.wait(5))

    first = _adapter()
    assert first._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    first._release_platform_lock()
    retry = _adapter()
    assert retry._acquire_platform_lock("telegram-bot-token", "tok", "d") is True

    gate.set()
    status.drain_runtime_status_lane()
    assert held == {("telegram-bot-token", "tok")}, (
        "the queued teardown unlink deleted a live retry connect's lock"
    )
    retry._release_platform_lock()
    status.drain_runtime_status_lane()
    assert held == set()


def test_release_without_a_loop_stays_inline(monkeypatch):
    held: set = set()
    threads: list[int] = []
    _held_registry(monkeypatch, held, threads)
    adapter = _adapter()
    assert adapter._acquire_platform_lock("telegram-bot-token", "tok", "d") is True
    adapter._release_platform_lock()
    assert held == set()
    assert threads == [threading.get_ident()]


# ---------------------------------------------------------------------------
# Site 3: drain-control watcher marker read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_watcher_reads_the_marker_off_the_loop_thread(monkeypatch):
    from gateway.run import GatewayRunner
    import gateway.drain_control as dc

    loop_thread = threading.get_ident()
    seen: list[int] = []

    runner = object.__new__(GatewayRunner)
    runner._running = True

    def _fake_requested(*_a, **_kw):
        seen.append(threading.get_ident())
        runner._running = False
        return False

    monkeypatch.setattr(dc, "drain_requested", _fake_requested)
    runner._exit_external_drain = lambda: None
    await asyncio.wait_for(runner._drain_control_watcher(interval=0.01), 5)
    assert seen and seen[0] != loop_thread, "drain marker read ran on the loop"


def test_drain_watcher_awaits_to_thread_for_the_marker_read():
    from gateway.run import GatewayRunner

    calls = [
        c
        for c in _calls_named(GatewayRunner._drain_control_watcher, "to_thread")
        if c.args and isinstance(c.args[0], ast.Name) and c.args[0].id == "drain_requested"
    ]
    assert calls, "_drain_control_watcher must call drain_requested via asyncio.to_thread"
    bare = [
        c
        for c in _calls_named(GatewayRunner._drain_control_watcher, "drain_requested")
    ]
    assert not bare, "drain_requested() is called directly on the loop"


# ---------------------------------------------------------------------------
# Site 4: plugin home key realpath on every has_hook
# ---------------------------------------------------------------------------


def test_plugin_home_key_does_not_resolve_on_every_call(tmp_path, monkeypatch):
    from hermes_cli import plugins

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    plugins._resolve_plugin_home.cache_clear()

    real_resolve = Path.resolve
    calls: list[str] = []

    def counting(self, *a, **kw):
        calls.append(str(self))
        return real_resolve(self, *a, **kw)

    with patch.object(Path, "resolve", counting):
        keys = {plugins._plugin_home_key() for _ in range(50)}
    assert keys == {real_resolve(home)}
    assert len(calls) <= 1, calls


def test_plugin_home_key_still_follows_a_home_switch(tmp_path, monkeypatch):
    from hermes_cli import plugins

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "link"
    link.symlink_to(a)
    monkeypatch.setenv("HERMES_HOME", str(link))
    assert plugins._plugin_home_key() == a.resolve()
    monkeypatch.setenv("HERMES_HOME", str(b))
    assert plugins._plugin_home_key() == b.resolve()
