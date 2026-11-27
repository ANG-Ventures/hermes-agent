"""The notifier's board probes must not reopen an unchanged board, and must never answer stale.

t_2cfa2782: every gateway's kanban notifier reopened every idle board every tick
(count_notify_subs + list_boards' _board_db_is_empty), and each reopen rewrote the board's -shm on
disk (~37 KB per open on APFS). sqlite_quiet_memo answers an unchanged board from memory.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import sqlite_quiet_memo as memo


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.delenv("HERMES_SQLITE_QUIET_MEMO", raising=False)
    memo.clear()
    yield
    memo.clear()


def _settle(path) -> None:
    old = time.time() - 3600
    os.utime(path, (old, old))


def test_unchanged_board_is_opened_once_by_the_notifier_probes(tmp_path, monkeypatch):
    db_path = tmp_path / "board.db"
    kb.init_db(db_path)
    _settle(db_path)
    opens = []
    real_connect = sqlite3.connect

    def counting_connect(*a, **k):
        opens.append(a[0] if a else k.get("database"))
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    assert [kbn.count_notify_subs(db_path=db_path) for _ in range(4)] == [0] * 4
    assert [kb._board_db_is_empty(db_path) for _ in range(4)] == [True] * 4
    assert len(opens) == 2, opens


def test_any_change_to_the_database_is_read_not_remembered(tmp_path):
    db = tmp_path / "a.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(3)])
    conn.commit()
    conn.close()
    _settle(db)

    def count():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            c.close()

    def read():
        return memo.read(db, "n", count)

    assert read() == 3 and read() == 3
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO t VALUES (99)")
    writer.commit()  # frames sit in the -wal; the main file is untouched
    assert read() == 4
    writer.close()  # last connection checkpoints into the main file and empties the -wal
    assert read() == 4
    _settle(db)
    assert read() == 4 and read() == 4  # remembered again
    size = os.stat(db).st_size
    writer = sqlite3.connect(db)
    writer.execute("DELETE FROM t WHERE x = 99")  # same-size page rewrite after checkpoint
    writer.commit()
    writer.close()
    assert os.stat(db).st_size == size
    assert read() == 3
    other = tmp_path / "b.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE t (x INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(7)])
    c.commit()
    c.close()
    os.replace(other, db)
    _settle(db)
    assert read() == 7
