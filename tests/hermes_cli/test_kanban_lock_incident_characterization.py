"""Characterize the two different dispatcher locks before changing ownership.

These are baseline probes for t_6b68232f, not a claim to reproduce the incident.
"""
import asyncio
import gc
import os
from pathlib import Path
import subprocess
import sys

import pytest

from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock
from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    path = tmp_path / "kanban.db"
    kb.init_db(path)
    assert Path(kb.__file__).resolve() == Path(__file__).resolve().parents[2] / "hermes_cli/kanban_db.py"
    return path


def assert_tick_available(board):
    with kb._dispatch_tick_lock(board) as held:
        assert held is True


def test_global_leadership_does_not_block_cli_dispatch(board):
    handle, state = _acquire_singleton_lock(board.parent / "kanban/.dispatcher.lock")
    assert state == "held"
    try:
        assert not os.get_inheritable(handle.fileno())
        with kb.connect_closing(board) as conn:
            result = kb.dispatch_once(conn, dry_run=True)
        assert result.skipped_locked is False
        contender, state = _acquire_singleton_lock(board.parent / "kanban/.dispatcher.lock")
        assert contender is None
        assert state == "contended"
    finally:
        _release_singleton_lock(handle)


@pytest.mark.parametrize("error", [KeyboardInterrupt, GeneratorExit, asyncio.CancelledError])
def test_tick_released_after_base_exception(board, error):
    with pytest.raises(error):
        with kb._dispatch_tick_lock(board) as held:
            assert held is True
            raise error()
    assert_tick_available(board)


def test_abandoned_context_manager_releases_after_collection(board):
    manager = kb._dispatch_tick_lock(board)
    assert manager.__enter__() is True
    del manager
    gc.collect()
    assert_tick_available(board)


@pytest.mark.asyncio
async def test_cancelled_task_releases_tick_lock(board):
    entered = asyncio.Event()

    async def owner():
        with kb._dispatch_tick_lock(board) as held:
            assert held is True
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(owner())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_tick_available(board)


@pytest.mark.skipif(os.name != "posix", reason="fcntl lock characterization")
def test_exec_child_does_not_inherit_tick_descriptor(board):
    import fcntl

    # Deliberately disable subprocess's close_fds protection: prove the opened
    # lock descriptor itself has close-on-exec, independently of spawn defaults.
    manager = kb._dispatch_tick_lock(board)
    with manager as held:
        assert held is True
        descriptor = manager.gen.gi_frame.f_locals["handle"].fileno()
        assert fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import os, sys\n"
             "try:\n"
             "    s = os.fstat(int(sys.argv[1]))\n"
             "    inherited = (s.st_dev, s.st_ino) == tuple(map(int, sys.argv[2:]))\n"
             "except OSError:\n"
             "    inherited = False\n"
             "print('inherited' if inherited else 'closed', flush=True)\n"
             "sys.stdin.read()\n",
             str(descriptor), str(os.fstat(descriptor).st_dev), str(os.fstat(descriptor).st_ino)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=False,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline() == b"closed\n"
        except BaseException:
            child.kill()
            child.communicate(timeout=5)
            raise
    try:
        # The parent context has exited while the exec child is still alive.
        # Verify the lock is available independently of the child's lifetime.
        with board.with_name(board.name + ".dispatch.lock").open("a+b") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        child.communicate(timeout=5)
