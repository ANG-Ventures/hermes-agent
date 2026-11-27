"""state.db writers do not run SQLite's inline autocheckpoint inside commit (t_b6742fb1).

Live 2026-11-10: the write-lock ledger showed holds of 9.8 s, 18.2 s and 283.9 s spent entirely in
``conn.commit()`` at ~0 CPU. SQLite's ``wal_autocheckpoint`` runs inside COMMIT (the WAL hook), after
the WAL write lock is already released but while ``SessionDB._execute_write`` still holds
``self._lock``, so every other writer in the process (the /stop marker included) waited out the
checkpoint's I/O. Writer connections now set ``wal_autocheckpoint=0``; the side-thread PASSIVE
checkpointer (``_try_wal_checkpoint``, its own connection, outside ``_lock``) does the backfill.
``database.writer_autocheckpoint: true`` restores the inline checkpoint (kill switch, read once).

The tell for "the checkpoint ran inside commit" is the main db file: a checkpoint copies WAL frames
into it, so it grows. Without an inline checkpoint the pages stay in the WAL until the side pass.
"""

import os

import pytest

import hermes_state_wal
from hermes_state import SessionDB

_BIG_WRITE_BYTES = 8 * 1024 * 1024  # ~2,000 pages: well past the default 1,000-page autocheckpoint


@pytest.fixture()
def knob(monkeypatch):
    def set_knob(value):
        import hermes_cli.config as config_mod
        cfg = {"database": {} if value is None else {"writer_autocheckpoint": value}}
        monkeypatch.setattr(config_mod, "load_config_readonly", lambda *a, **k: cfg)
        monkeypatch.setattr(hermes_state_wal, "_writer_autocheckpoint_cached", None, raising=False)
    return set_knob


def _big_write(db):
    blob = os.urandom(_BIG_WRITE_BYTES).hex()[:_BIG_WRITE_BYTES]
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO sessions (id, source, started_at, title) VALUES ('big', 'test', 0, ?)", (blob,)))


def _main_size(db):
    return os.path.getsize(db.db_path)


def test_a_commit_does_not_checkpoint_under_the_writer_lock(tmp_path, knob):
    """Red on base: the commit ran the 1,000-page autocheckpoint inline, so the main file grew by
    the whole write before _execute_write released self._lock."""
    knob(None)  # default = new behaviour
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert db._wal_active
        assert db._conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
        before = _main_size(db)
        _big_write(db)
        assert _main_size(db) - before < _BIG_WRITE_BYTES // 4, "the commit checkpointed inline"
        # The side checkpointer (own connection, outside _lock) still backfills the WAL.
        db._try_wal_checkpoint()
        assert _main_size(db) - before >= _BIG_WRITE_BYTES // 2, "the side checkpoint did not backfill"
    finally:
        db.close()


def test_a_writer_waiting_on_the_lock_does_not_wait_for_a_checkpoint(tmp_path, knob):
    """The /stop marker shape: a one-row write queued on self._lock behind a big write. Red on base:
    by the time the marker got the lock, the big write's commit had already checkpointed its pages
    into the main file, i.e. the marker waited out that checkpoint."""
    import threading
    import time
    knob(None)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        before = _main_size(db)
        seen = {}

        def marker_fn(conn):
            seen["main"] = _main_size(db)
            conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('stop', 'test', 0)")

        marker = threading.Thread(target=lambda: db._execute_write(marker_fn))
        blob = os.urandom(_BIG_WRITE_BYTES).hex()[:_BIG_WRITE_BYTES]

        def big_fn(conn):
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, title) VALUES ('big', 'test', 0, ?)", (blob,))
            marker.start()  # queues on db._lock, which this write holds through its commit
            time.sleep(0.2)

        db._execute_write(big_fn)
        marker.join(10)
        assert "main" in seen
        assert seen["main"] - before < _BIG_WRITE_BYTES // 4, "the marker waited out an inline checkpoint"
    finally:
        db.close()


def test_kill_switch_restores_the_inline_autocheckpoint(tmp_path, knob):
    knob(True)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert db._conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
        before = _main_size(db)
        _big_write(db)
        assert _main_size(db) - before >= _BIG_WRITE_BYTES // 2
    finally:
        db.close()


def test_knob_is_read_once(monkeypatch, knob):
    knob(True)
    assert hermes_state_wal.writer_autocheckpoint_enabled() is True
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda *a, **k: {"database": {}})
    assert hermes_state_wal.writer_autocheckpoint_enabled() is True
