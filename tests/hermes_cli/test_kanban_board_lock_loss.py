"""A raw open()/close() of a board must never run while this process holds a connection to it,
tracked or not (t_54b8e6cc).

``close()`` on any descriptor cancels every POSIX lock the process holds on that inode, including
SQLite's SHARED lock on the database. On 2026-11-17 the default board took two zero-page
corruptions, each opened by ``disk I/O error`` in every gateway at once; a scratch stress harness
reproduced that cross-process ``SQLITE_IOERR_SHORT_READ`` storm only in the arm where one process
raw-read the board while it held connections. The raw-read guards trusted a registry that plain
``sqlite3.connect`` callers (the board anchor among them) never enter.

The lock state is read the way another process would see it: a child asks the kernel for an
exclusive lock on SQLite's SHARED byte range.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.sqlite_safe_read import connect_tracked, process_holds_open_fd

pytestmark = pytest.mark.platforms("posix")

# os_unix.c: PENDING_BYTE 0x40000000, SHARED_FIRST = PENDING_BYTE + 2, SHARED_SIZE 510.
_PROBE = """
import fcntl, sys
with open(sys.argv[1], "r+b") as fh:
    try:
        fcntl.lockf(fh, fcntl.LOCK_EX | fcntl.LOCK_NB, 510, 0x40000002)
    except OSError:
        print("held")
    else:
        print("free")
"""


def _shared_lock_state(db: Path) -> str:
    out = subprocess.run([sys.executable, "-c", _PROBE, str(db)], capture_output=True, text=True,
                         check=True, timeout=60)
    return out.stdout.strip()


def _untracked_wal_connection(db: Path) -> sqlite3.Connection:
    """A plain ``sqlite3.connect`` like the board anchor's: invisible to the tracked registry."""
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t(v)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("SELECT count(*) FROM t").fetchone()
    return conn


def test_header_probe_keeps_an_untracked_connections_lock(tmp_path):
    db = tmp_path / "kanban.db"
    conn = _untracked_wal_connection(db)
    try:
        assert _shared_lock_state(db) == "held"
        assert process_holds_open_fd(db)
        kbc._validate_sqlite_header(db)  # the first-connect probe in connect()
        assert _shared_lock_state(db) == "held"
    finally:
        conn.close()
    assert not process_holds_open_fd(db)


def test_quarantine_refuses_while_an_untracked_connection_is_open(tmp_path):
    db = tmp_path / "kanban.db"
    conn = _untracked_wal_connection(db)
    try:
        assert kbc._backup_corrupt_db(db) is None
        assert _shared_lock_state(db) == "held"
    finally:
        conn.close()
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


def test_quarantine_holds_the_lifecycle_lock_against_a_concurrent_tracked_open(tmp_path, monkeypatch):
    """A tracked connection opened between the open-handle check and the raw copy would lose its
    POSIX locks to the copy's close(). The open must wait until the copy is done (t_b8456297)."""
    db = tmp_path / "kanban.db"
    seed = sqlite3.connect(db)
    seed.execute("CREATE TABLE t(v)")
    seed.commit()
    seed.close()
    opened = threading.Event()
    conns: list = []
    state: dict = {}

    def _open():
        conns.append(connect_tracked(db, check_same_thread=False))
        opened.set()

    real_fingerprint = kbc._db_fingerprint

    def _fingerprint_then_race(path):  # runs after the checks, before the raw read and copy
        state["thread"] = threading.Thread(target=_open, daemon=True)
        state["thread"].start()
        state["opened_before_copy"] = opened.wait(1.0)
        return real_fingerprint(path)

    monkeypatch.setattr(kbc, "_db_fingerprint", _fingerprint_then_race)
    try:
        backup = kbc._backup_corrupt_db(db)
        state["thread"].join(30)
        assert opened.is_set()
        assert not state["opened_before_copy"] or backup is None
    finally:
        for conn in conns:
            conn.close()


def test_quarantine_copy_does_not_stall_another_databases_open_and_close(tmp_path, monkeypatch):
    """The copy holds only ITS board out of service: an open/close of database B in this process
    returns at once while board A's copy is in progress (t_b8456297 send-back: a process-wide lock
    across a 537 MB copy stalled the gateway's SessionDB open)."""
    board_a, other_b = tmp_path / "a" / "kanban.db", tmp_path / "b" / "state.db"
    for db in (board_a, other_b):
        db.parent.mkdir()
        seed = sqlite3.connect(db)
        seed.execute("CREATE TABLE t(v)")
        seed.commit()
        seed.close()
    done = threading.Event()
    state: dict = {}

    def _open_close_b():
        connect_tracked(other_b, check_same_thread=False).close()
        done.set()

    real_fingerprint = kbc._db_fingerprint

    def _fingerprint_while_b(path):  # board A's copy is in progress here
        state["thread"] = threading.Thread(target=_open_close_b, daemon=True)
        state["thread"].start()
        state["b_returned_during_copy"] = done.wait(5.0)
        return real_fingerprint(path)

    monkeypatch.setattr(kbc, "_db_fingerprint", _fingerprint_while_b)
    backup = kbc._backup_corrupt_db(board_a)
    state["thread"].join(30)
    assert backup is not None
    assert state["b_returned_during_copy"]


def test_quarantine_copies_once_per_episode_until_a_clean_verdict(tmp_path):
    db = tmp_path / "kanban.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 8176)
    first = kbc._backup_corrupt_db(db)
    assert first is not None and first.is_file()

    for i in range(5):  # a broken live board keeps changing under open connections
        with db.open("r+b") as fh:
            fh.seek(4096 + i)
            fh.write(b"\xab")
        assert kbc._backup_corrupt_db(db) == first
    assert sorted(tmp_path.glob("kanban.db.corrupt.*.bak")) == [first]

    resolved = db.resolve()
    kbc._write_integrity_stamp(resolved, kbc._db_fingerprint(resolved), time.time())
    with db.open("r+b") as fh:
        fh.seek(5000)
        fh.write(b"\xcd")
    second = kbc._backup_corrupt_db(db)
    assert second is not None and second != first


def test_first_ioerr_row_names_a_deleted_wal_under_a_live_connection(tmp_path, caplog):
    from hermes_cli import kanban_db_ioerr

    db = tmp_path / "kanban.db"
    conn = _untracked_wal_connection(db)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO t VALUES (2)")
    try:
        os.unlink(f"{db}-wal")  # what another process's last-close does to a live board
        exc = sqlite3.OperationalError("disk I/O error")
        exc.sqlite_errorcode = 522
        exc.sqlite_errorname = "SQLITE_IOERR_SHORT_READ"
        kanban_db_ioerr._SEEN.discard(os.path.realpath(db))
        with caplog.at_level("WARNING", logger=kanban_db_ioerr.__name__):
            row = kanban_db_ioerr.note_board_ioerr(exc, where="notifier", db_path=db)
            again = kanban_db_ioerr.note_board_ioerr(exc, where="notifier", db_path=db)
    finally:
        conn.close()
    assert again is None
    assert row["pid"] == os.getpid() and row["errorname"] == "SQLITE_IOERR_SHORT_READ"
    assert row["files"]["-wal"] is None
    stale_wal = [fd for fd in row["own_fds"] if fd["file"] == "-wal"]
    assert stale_wal and all(fd["stale"] for fd in stale_wal)
    logged = [r for r in caplog.records if "first in this process" in r.getMessage()]
    assert len(logged) == 1
    assert json.loads(logged[0].getMessage().split(": ", 1)[1])["own_fds"] == row["own_fds"]
    assert kanban_db_ioerr.note_board_ioerr(sqlite3.OperationalError("database is locked"),
                                           where="notifier", db_path=tmp_path / "other.db") is None


def test_notifier_cursor_write_ioerr_names_its_board_and_connection(monkeypatch):
    """The 23:05 damage was in kanban_notify_subs: the notifier's cursor write must report the board
    it failed on and the connection it used, not the default board with no connection."""
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db_ioerr, kanban_db_notify

    conn = sqlite3.connect(":memory:")
    calls = []
    monkeypatch.setattr(kbc, "connect", lambda board=None: conn)
    exc = sqlite3.OperationalError("disk I/O error")

    def _fail(*_a, **_k):
        raise exc

    monkeypatch.setattr(kanban_db_notify, "advance_notify_cursor", _fail)
    monkeypatch.setattr(kanban_db_ioerr, "note_board_ioerr", lambda e, **kw: calls.append((e, kw)))
    sub = {"task_id": "t_x", "platform": "discord", "chat_id": "1"}
    with pytest.raises(sqlite3.OperationalError):
        GatewayKanbanWatchersMixin.__new__(GatewayKanbanWatchersMixin)._kanban_advance(sub, 7, "proj")
    assert calls == [(exc, {"where": "notifier:advance_notify_cursor", "board": "proj", "conn": conn})]
