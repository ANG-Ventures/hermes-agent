"""A writable SessionDB still alive at interpreter teardown must not checkpoint (Prism 24f888f2a0c1).

#2379 made ``SessionDB.__del__`` skip ``close()`` while the interpreter finalizes (close()'s lazy
imports hang on the import lock there). The connections are still finalized afterwards, and sqlite3's
destructor runs SQLite's last-connection checkpoint: a writer whose WAL generation was taken away
then writes its retired frames over the newer generation at the path. Teardown must leave every
connection unable to checkpoint, import-free: SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE where sqlite3 has
``setconfig`` (3.12+), else the pre-bound C-reference pin that ``close()`` uses (< 3.12).
"""

import sqlite3

import pytest

from tests.hermes_state._wal_generation_harness import (
    gateway_writer, integrity_ok_path, lose_sidecars, message_count, pin_wal, write_second_generation,
)
from hermes_state import SessionDB

_FLAG = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)


def _writer_with_ckpt_conn(tmp_path, monkeypatch) -> SessionDB:
    pin_wal(monkeypatch)
    db = SessionDB(db_path=tmp_path / "state.db")
    db._try_wal_checkpoint()  # opens the dedicated checkpoint connection too
    assert db._ckpt_conn is not None
    return db


@pytest.mark.skipif(_FLAG is None or not hasattr(sqlite3.Connection, "setconfig"), reason="needs setconfig (3.12+)")
def test_finalization_switches_off_the_close_time_checkpoint(tmp_path, monkeypatch):
    db = _writer_with_ckpt_conn(tmp_path, monkeypatch)
    conns = (db._conn, db._ckpt_conn)
    try:
        db.__del__(_finalizing=lambda: True)
        assert [c.getconfig(_FLAG) for c in conns] == [True, True]
    finally:
        db.close()


def test_finalization_pins_where_setconfig_is_missing(tmp_path, monkeypatch):
    """< 3.12 path, driven on any runtime: no flag bound, so the pre-bound retirement pin is used."""
    db = _writer_with_ckpt_conn(tmp_path, monkeypatch)
    conns = [db._conn, db._ckpt_conn]
    pins = []
    db._retire_connection = pins.append
    try:
        db.__del__(_finalizing=lambda: True, _no_ckpt_on_close=None)
        assert pins == conns and db._connection_pinned
        db.__del__(_finalizing=lambda: True, _no_ckpt_on_close=None)
        assert pins == conns + [conns[1]], "the writer connection must not be pinned twice"
    finally:
        db._retire_connection = None
        db.close()


def test_read_only_handle_is_left_alone_at_finalization(tmp_path, monkeypatch):
    pin_wal(monkeypatch)
    SessionDB(db_path=tmp_path / "state.db").close()
    db = SessionDB(db_path=tmp_path / "state.db", read_only=True)
    try:
        db.__del__(_finalizing=lambda: True, _no_ckpt_on_close=None)
        assert not db._connection_pinned
    finally:
        db.close()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("halt", [False, True], ids=["loss-unobserved", "loss-captured-at-halt"])
def test_unclosed_writer_at_exit_does_not_overwrite_the_newer_generation(tmp_path, halt):
    """The Prism repro: a writer retained to interpreter teardown after its sidecars were taken away and a
    newer generation was checkpointed through the path. The OFD lock guard is off in the child: it refuses
    the close-time checkpoint by itself and would hide whether retirement does."""
    with gateway_writer(tmp_path, lockguard=False) as gw:
        path = gw.path
        gw.next_event("ready")  # 4 rows live only in the writer's WAL
        lose_sidecars(path, rename=False)
        expected = write_second_generation(path, n_rows=400)
        if halt:
            gw.send("write")
            assert gw.next_event("write")["refused"] is True
        gw.send("quit")  # no close(): the SessionDB is a __main__ global, finalized at teardown
        assert gw.wait_exit(timeout=20) == 0, gw.stderr_text()

    assert integrity_ok_path(path)
    assert message_count(path) == expected, "teardown checkpointed retired frames over the newer generation"
