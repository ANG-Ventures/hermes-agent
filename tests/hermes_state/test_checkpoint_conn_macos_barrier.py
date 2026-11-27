"""The dedicated state.db checkpoint connection carries the macOS crash barrier (t_e8c62e86).

``PRAGMA checkpoint_fullfsync`` and ``synchronous`` are per-connection. Since t_b881920f every periodic
state.db checkpoint runs on ``SessionDB._ckpt_conn`` (opened by ``_checkpoint_conn_locked``), not on the
writer, so the barrier the writer gets from ``apply_wal_with_fallback`` never reached the connection that
actually copies WAL frames into the main file. On macOS a plain ``fsync`` does not flush the drive cache:
pages a checkpoint wrote can be lost by a panic or power cut after the WAL was reset (the 2026-11-11 lcm.db
tear, t_6314cc2f).

``sys.platform`` is pinned to ``darwin`` only around the checkpoint (the barrier helpers read it at call
time), so this runs on Linux CI too.
"""

import sqlite3
import sys

from hermes_cli import backup_restore
from hermes_state import SessionDB


def test_dedicated_checkpoint_conn_has_fullfsync_barrier(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert db._ckpt_conn is None
        monkeypatch.setattr(sys, "platform", "darwin")  # os-marker: ok — pins the darwin branch as data so it runs on every lane
        db._try_wal_checkpoint()
        monkeypatch.undo()
        ckpt = db._ckpt_conn
        assert ckpt is not None, "checkpoint did not open the dedicated connection"
        assert ckpt is not db._conn
        assert ckpt.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1, (
            "dedicated checkpoint connection checkpoints with a plain fsync on macOS"
        )
        assert ckpt.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        db.close()


def test_dedicated_checkpoint_conn_no_darwin_pragmas_off_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db._try_wal_checkpoint()
        assert db._ckpt_conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 0
    finally:
        db.close()


def test_safe_restore_dst_conn_has_barrier_before_checkpoint(tmp_path, monkeypatch):
    """``_safe_restore_db`` checkpoints and page-copies into a live state.db on its own connection."""
    src, dst = tmp_path / "snap.db", tmp_path / "state.db"
    for path in (src, dst):
        with sqlite3.connect(path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE t (x)")
        c.close()

    class _Tracing(sqlite3.Connection):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.executed = []

        def execute(self, sql, *a):
            self.executed.append(sql)
            return super().execute(sql, *a)

    seen = []
    real_connect = sqlite3.connect

    def connect(database, *a, **kw):
        if str(database) == str(dst):
            kw["factory"] = _Tracing
            conn = real_connect(database, *a, **kw)
            seen.append(conn)
            return conn
        return real_connect(database, *a, **kw)

    monkeypatch.setattr(backup_restore.sqlite3, "connect", connect)
    monkeypatch.setattr(sys, "platform", "darwin")  # os-marker: ok — pins the darwin branch as data so it runs on every lane
    assert backup_restore._safe_restore_db(src, dst) is True
    monkeypatch.undo()
    assert seen, "restore did not open the destination"
    sql = seen[0].executed
    barrier = [i for i, s in enumerate(sql) if "checkpoint_fullfsync=1" in s]
    ckpt = [i for i, s in enumerate(sql) if "wal_checkpoint" in s]
    assert barrier and ckpt and barrier[0] < ckpt[0], sql
