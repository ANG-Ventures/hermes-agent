"""Tests for SessionDB WAL checkpoint strategy (issue #45383).

Verifies that ALL checkpoints on the shared state.db use PASSIVE mode:
periodic, close(), and pre-VACUUM. TRUNCATE fires a full WAL reset, and
transient per-cron-run connections closing many times an hour would race
the live gateway writer and corrupt B-tree pages (#45383).
"""

import logging
import sqlite3
from unittest.mock import MagicMock

import pytest

from hermes_state import SessionDB


class TrackingConnection:
    """sqlite3.Connection proxy that records executed SQL strings."""

    def __init__(self, conn):
        self._conn = conn
        self.execute_calls = []

    def execute(self, sql, *args, **kwargs):
        self.execute_calls.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


class TestTryWalCheckpointPassive:
    """_try_wal_checkpoint() should use PASSIVE mode for periodic use."""

    def test_checkpoint_uses_passive_mode(self, db):
        """PASSIVE checkpoint does not require exclusive lock — safe for large DBs."""
        # Capture the real connection's execute before mocking
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        # sqlite3.Connection.execute is read-only (C extension) — replace _conn
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        mock_conn.fetchone.return_value = None
        db._ckpt_conn = mock_conn  # the periodic checkpoint runs on its own connection

        db._try_wal_checkpoint()
        db._ckpt_conn = None

        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(passive_calls) == 1, (
            f"Expected 1 PASSIVE checkpoint call, got {len(passive_calls)}"
        )
        assert len(truncate_calls) == 0, (
            "Periodic checkpoint should NOT use TRUNCATE"
        )

    def test_failed_checkpoint_never_raises_and_is_not_silent(self, db, caplog):
        """The periodic checkpoint runs from the write path: a failing PASSIVE
        checkpoint must not raise into the caller, and must leave a WARNING."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        db._ckpt_conn = mock_conn
        try:
            with caplog.at_level(logging.WARNING, logger="hermes_state"):
                db._try_wal_checkpoint()
        finally:
            db._ckpt_conn = None
        assert any(r.levelno >= logging.WARNING for r in caplog.records)




class TestCloseUsesPassive:
    """close() must use PASSIVE. Transient per-cron-run SessionDB connections
    close many times an hour; a TRUNCATE reset there races the live gateway
    writer on the large WAL DB and corrupts B-tree pages (#45383)."""

    def test_close_uses_passive_mode(self, db):
        """close() checkpoints PASSIVE, never TRUNCATE."""
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        db.close()

        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        assert len(truncate_calls) == 0, (
            "close() must NOT TRUNCATE (races the live gateway writer, #45383)"
        )
        assert len(passive_calls) == 1, (
            f"Expected 1 PASSIVE checkpoint at close, got {len(passive_calls)}"
        )

    def test_failed_checkpoint_at_close_still_releases_the_handle(self, db):
        """close() is best-effort about the checkpoint: a failing PASSIVE at
        close must neither raise nor leave the handle open."""
        real_conn = db._conn
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        db._conn = mock_conn
        try:
            db.close()
        finally:
            real_conn.close()

        assert db._conn is None
        mock_conn.close.assert_called()



class TestVacuumUsesPassive:
    """Manual vacuum paths must checkpoint PASSIVE, never TRUNCATE."""

    def test_vacuum_uses_passive_before_vacuum(self, db):
        """SessionDB.vacuum() checkpoints PASSIVE before VACUUM.

        A TRUNCATE checkpoint AFTER the VACUUM is expected: VACUUM rewrites
        every page through the WAL, so without it a 3 GB database leaves a
        3 GB state.db-wal behind and `sessions optimize` becomes a net disk
        LOSS. The #45383 tearing concern is about truncating while another
        writer is mid-transaction — the pre-VACUUM checkpoint stays PASSIVE
        for that reason; the post-VACUUM truncate runs under the same
        exclusive-lock window the VACUUM itself required.
        """
        real_conn = db._conn
        tracking_conn = TrackingConnection(real_conn)
        db._conn = tracking_conn

        db.vacuum()

        checkpoint_calls = [
            c for c in tracking_conn.execute_calls if "wal_checkpoint" in c.lower()
        ]
        truncate_calls = [c for c in checkpoint_calls if "TRUNCATE" in c]
        passive_calls = [c for c in checkpoint_calls if "PASSIVE" in c]
        vacuum_calls = [
            c for c in tracking_conn.execute_calls if c.strip().upper() == "VACUUM"
        ]
        assert passive_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
        assert vacuum_calls == ["VACUUM"]
        assert truncate_calls == ["PRAGMA wal_checkpoint(TRUNCATE)"]
        vacuum_index = tracking_conn.execute_calls.index(vacuum_calls[0])
        assert tracking_conn.execute_calls.index(passive_calls[0]) < vacuum_index
        # TRUNCATE must come only AFTER the VACUUM (never before it).
        assert tracking_conn.execute_calls.index(truncate_calls[0]) > vacuum_index

    def test_optimize_storage_uses_passive_after_vacuum(self, db):
        """optimize_fts_storage() checkpoints PASSIVE after its VACUUM."""
        real_conn = db._conn
        tracking_conn = TrackingConnection(real_conn)
        db._conn = tracking_conn

        result = db.optimize_fts_storage(vacuum=True)

        checkpoint_calls = [
            c for c in tracking_conn.execute_calls if "wal_checkpoint" in c.lower()
        ]
        truncate_calls = [c for c in checkpoint_calls if "TRUNCATE" in c]
        passive_calls = [c for c in checkpoint_calls if "PASSIVE" in c]
        vacuum_calls = [
            c for c in tracking_conn.execute_calls if c.strip().upper() == "VACUUM"
        ]
        assert result["ok"] is True
        assert result["vacuumed"] is True
        assert truncate_calls == []
        assert passive_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
        assert vacuum_calls == ["VACUUM"]
        assert tracking_conn.execute_calls.index(
            vacuum_calls[0]
        ) < tracking_conn.execute_calls.index(passive_calls[0])


class TestCheckpointFrequency:
    """Checkpoint triggers every N writes."""

    def test_checkpoint_triggers_at_interval(self, db):
        """_try_wal_checkpoint is called every _CHECKPOINT_EVERY_N_WRITES writes."""
        call_count = [0]
        original = db._try_wal_checkpoint

        def counting_checkpoint():
            call_count[0] += 1
            original()

        db._try_wal_checkpoint = counting_checkpoint

        # Write exactly _CHECKPOINT_EVERY_N_WRITES sessions to trigger one checkpoint
        n = db._CHECKPOINT_EVERY_N_WRITES
        import time as _time
        for i in range(n):
            db._execute_write(lambda conn, _i=i: conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (f"sess_{_i}", "test", _time.time()),
            ))

        if db._ckpt_thread is not None:
            db._ckpt_thread.join(10)  # the periodic checkpoint now runs in the background
        assert call_count[0] == 1, (
            f"Expected 1 checkpoint after {n} writes, got {call_count[0]}"
        )


class TestCheckpointNeverHoldsTheWriteLock:
    """2026-10-25, live gateway: the periodic PASSIVE checkpoint ran under SessionDB._lock and held
    19-23 queued writers for >=42 s on a 6.25 GB state.db (py-spy: _try_wal_checkpoint -> PRAGMA at the
    lock holder). PASSIVE itself never blocks writers; the Python lock around it did."""

    def _blocking_ckpt(self, db):
        import threading
        entered, release = threading.Event(), threading.Event()
        real = db._checkpoint_conn_locked()

        class Slow:
            def execute(self, sql, *a, **k):
                if "wal_checkpoint" in sql:
                    entered.set()
                    release.wait(10)
                return real.execute(sql, *a, **k)

        db._ckpt_conn = Slow()
        return entered, release, real

    def test_a_write_completes_while_a_checkpoint_is_running(self, db):
        """Red on base: the PRAGMA ran inside `with self._lock`, so this write waited for it."""
        import threading
        import time as _time
        entered, release, real = self._blocking_ckpt(db)
        t = threading.Thread(target=db._try_wal_checkpoint)
        t.start()
        try:
            assert entered.wait(5), "checkpoint never started"
            started = _time.monotonic()
            db._execute_write(lambda conn: conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES ('w', 'test', 0)"))
            assert _time.monotonic() - started < 2.0, "a writer waited on the running checkpoint"
        finally:
            release.set()
            t.join(10)
            db._ckpt_conn = real

    def test_the_interval_writer_does_not_wait_for_the_checkpoint(self, db):
        """Red on base: the Nth write ran the checkpoint inline before returning."""
        import time as _time
        entered, release, real = self._blocking_ckpt(db)
        try:
            n = db._CHECKPOINT_EVERY_N_WRITES
            started = _time.monotonic()
            for i in range(n):
                db._execute_write(lambda conn, _i=i: conn.execute(
                    "INSERT INTO sessions (id, source, started_at) VALUES (?, 'test', 0)", (f"k{_i}",)))
            assert entered.wait(5), "the interval write did not start a checkpoint"
            assert _time.monotonic() - started < 5.0, "the interval writer waited for the checkpoint"
        finally:
            release.set()
            if db._ckpt_thread is not None:
                db._ckpt_thread.join(10)
            db._ckpt_conn = real

    def test_close_closes_the_checkpoint_connection(self, db):
        db._try_wal_checkpoint()
        assert db._ckpt_conn is not None
        conn = db._ckpt_conn
        db.close()
        assert db._ckpt_conn is None
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
