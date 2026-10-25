"""Private-descriptor WAL lock guard (t_aeae3fe7).

``hold()`` used to find SQLite's own descriptors with ``listdir('/dev/fd')`` + one ``fstat`` per fd
under ``SessionDB._lock``; under GIL load that held the write lock 16-28 s and delayed Discord
replies by minutes. The guard now OFD-locks descriptors it opens itself. OFD locks conflict by inode
range, so the foreign-EXCLUSIVE refusal (the reason the guard exists) must be unchanged, including
after a stray in-process open()/close() (howtocorrupt §2.2). Linux-only: OFD locks + ``/proc``.
"""

import importlib
import logging
import os
import subprocess
import sys
import time

import pytest

import hermes_state
import hermes_state_lockguard as lg
import hermes_state_lockowners as lockowners
from tests.hermes_state._wal_generation_harness import make_db, pin_wal, require_wal

pytestmark = pytest.mark.platforms("linux")

_SHARED = (0x40000002, 510)
_DMS = (128, 1)


def _foreign_exclusive(path: str, start: int, length: int) -> str:
    """A CHILD process tries F_OFD_SETLK F_WRLCK on (start, length): 'ACQUIRED' or 'REFUSED'."""
    code = (
        "import fcntl, os, struct\n"
        f"fd = os.open({path!r}, os.O_RDWR)\n"
        f"lk = struct.pack('@hhqqi', fcntl.F_WRLCK, 0, {start}, {length}, 0)\n"
        "try:\n    fcntl.fcntl(fd, 37, lk); print('ACQUIRED')\n"
        "except BlockingIOError:\n    print('REFUSED')\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()


def test_private_hold_refuses_foreign_exclusive_until_released(tmp_path):
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    shm = tmp_path / "state.db-shm"
    shm.write_bytes(b"\0" * 32768)
    held = lg.hold(db)
    try:
        assert set(held) == {(os.stat(db).st_dev, os.stat(db).st_ino), (os.stat(shm).st_dev, os.stat(shm).st_ino)}
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
        assert _foreign_exclusive(str(shm), *_DMS) == "REFUSED"
        # howtocorrupt §2.2: a stray open()/close() of both files in THIS process.
        for path in (db, shm):
            os.close(os.open(path, os.O_RDONLY))
        assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
        assert _foreign_exclusive(str(shm), *_DMS) == "REFUSED"
    finally:
        released = lg.release(held)
    lg.retire(released)
    assert _foreign_exclusive(str(db), *_SHARED) == "ACQUIRED"
    assert _foreign_exclusive(str(shm), *_DMS) == "ACQUIRED"
    assert not lg._HANDLES and not lg._FDS, "retire() must close the private descriptors"


def test_missing_shm_is_tolerated(tmp_path):
    if not lg.supported():
        pytest.skip("no OFD locks")
    db = tmp_path / "state.db"
    db.write_bytes(b"\0" * 4096)
    held = lg.hold(db)  # never raises; arms the main file only
    assert list(held) == [(os.stat(db).st_dev, os.stat(db).st_ino)]
    lg.retire(lg.release(held))
    assert lg.hold(tmp_path / "absent.db") == {}


def test_late_shm_is_armed_by_checkpoint_with_one_stat_and_no_listdir(tmp_path, monkeypatch):
    """-shm absent at open, created later: the next _try_wal_checkpoint arms it with exactly one
    os.stat (the -shm) and zero os.listdir calls."""
    pin_wal(monkeypatch)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(sdb)
    try:
        shm = f"{sdb.db_path}-shm"
        shm_ident = (os.stat(shm).st_dev, os.stat(shm).st_ino)
        # Simulate "-shm minted after open": drop it from this handle's guard record.
        lg.release({shm_ident: sdb._wal_lock_guard.pop(shm_ident)})
        assert shm_ident not in sdb._wal_lock_guard
        calls = {"stat": 0, "listdir": 0}
        real_stat, real_listdir = os.stat, os.listdir

        def _stat(path, *a, **k):
            if os.fspath(path) == shm:
                calls["stat"] += 1
            return real_stat(path, *a, **k)

        def _listdir(*a, **k):
            calls["listdir"] += 1
            return real_listdir(*a, **k)

        monkeypatch.setattr(lg.os, "stat", _stat)
        monkeypatch.setattr(lg.os, "listdir", _listdir)
        sdb._try_wal_checkpoint()
        monkeypatch.setattr(lg.os, "stat", real_stat)
        monkeypatch.setattr(lg.os, "listdir", real_listdir)
        assert shm_ident in sdb._wal_lock_guard, "checkpoint did not arm the new -shm"
        assert calls == {"stat": 1, "listdir": 0}, calls
        # Already guarded: the next checkpoint is one stat and nothing else.
        calls.update(stat=0, listdir=0)
        monkeypatch.setattr(lg.os, "stat", _stat)
        monkeypatch.setattr(lg.os, "listdir", _listdir)
        sdb._try_wal_checkpoint()
        assert calls == {"stat": 1, "listdir": 0}, calls
    finally:
        monkeypatch.undo()
        sdb.close()


def test_write_path_never_reaches_the_fd_table_scan(tmp_path, monkeypatch):
    """I2: open, write, checkpoint, reopen and close never call _own_fds_for in private mode."""
    pin_wal(monkeypatch)

    def _boom(*_a, **_k):
        raise AssertionError("fd-table scan on the write path")

    monkeypatch.setattr(lg, "_own_fds_for", _boom)
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    require_wal(sdb)
    sdb._try_wal_checkpoint()
    sdb.close()
    sdb.append_message("s", role="user", content="after close")  # reopen path
    assert sdb._wal_lock_guard
    sdb.close()
    assert not lg._HANDLES and not lg._FDS


class _SlowWrite:
    def __init__(self, seconds):
        self.seconds = seconds

    def __call__(self, conn):
        time.sleep(self.seconds)
        return None


@pytest.mark.parametrize("seconds,expect", [(1.2, True), (0.5, False)])
def test_lock_hold_gauge(tmp_path, monkeypatch, caplog, seconds, expect):
    pin_wal(monkeypatch)
    notes = []
    monkeypatch.setattr(lockowners, "note_lock_hold", lambda db, op, held: notes.append((op, held)))
    sdb = make_db(tmp_path / "state.db", "s", "seed")
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_state"):
            caplog.clear()
            sdb._execute_write(_SlowWrite(seconds))
        warned = [r for r in caplog.records if "SessionDB._lock held" in r.getMessage()]
        assert bool(warned) is expect and bool(notes) is expect
        if expect:
            assert notes[0][0].startswith("_lock:") and notes[0][1] >= 1.2
            assert hermes_state._LOCK_HOLD_WARN_S == 1.0
    finally:
        sdb.close()


def test_lock_hold_note_writes_a_released_ledger_row(tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"")
    lockowners.note_lock_hold(db, "_lock:append_messages_batch", 1.7)
    import json
    rows = [json.loads(p.read_text()) for p in (tmp_path / "state.db.lockholders").glob("*.json")]
    assert rows and rows[0]["op"] == "_lock:append_messages_batch"
    assert rows[0]["state"] == "released" and rows[0]["held_s"] == 1.7


@pytest.mark.parametrize("value,mode,armed", [
    (None, "private", True), ("scan", "scan", True), ("off", "off", False), ("bogus", "private", True)])
def test_mode_kill_switch_is_read_at_import(monkeypatch, tmp_path, value, mode, armed):
    """D5: HERMES_STATE_LOCKGUARD_MODE=private|scan|off, read once at import; unknown -> private."""
    if value is None:
        monkeypatch.delenv("HERMES_STATE_LOCKGUARD_MODE", raising=False)
    else:
        monkeypatch.setenv("HERMES_STATE_LOCKGUARD_MODE", value)
    monkeypatch.delitem(sys.modules, "hermes_state_lockguard", raising=False)
    try:
        guard = importlib.import_module("hermes_state_lockguard")
        assert guard._MODE == mode and guard.supported() is armed
        db = tmp_path / "state.db"
        db.write_bytes(b"\0" * 4096)
        fd = os.open(db, os.O_RDONLY)  # scan mode guards descriptors the process already has
        try:
            held = guard.hold(db)
            assert bool(held) is armed
            if armed:
                assert _foreign_exclusive(str(db), *_SHARED) == "REFUSED"
            guard.retire(guard.release(held))
        finally:
            os.close(fd)
    finally:
        monkeypatch.delitem(sys.modules, "hermes_state_lockguard", raising=False)
        sys.modules["hermes_state_lockguard"] = lg
