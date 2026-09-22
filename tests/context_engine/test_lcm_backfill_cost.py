"""The search_content backfill must be O(1) on a healthy DB and restart-safe.

2026-09-22: on the Mac Studio the default gateway sat with all 8 turn slots parked
for ~21 minutes per boot because ``_backfill_search_content`` ran
``WHERE search_content IS NULL`` as a full-table scan (10.9 GB, 2.5 M rows, zero
NULLs) under the process-wide engine-load lock. These tests pin the contract:
a partial index exists, the probe uses it, and real work commits in batches.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from plugins.context_engine.lcm.store import MessageStore


class _SpyConn:
    """Proxy a live sqlite3.Connection, recording SQL and commits."""

    def __init__(self, conn):
        self._c = conn
        self.sql: list[str] = []
        self.commits = 0

    def execute(self, sql, *a, **k):
        self.sql.append(sql)
        return self._c.execute(sql, *a, **k)

    def commit(self):
        self.commits += 1
        return self._c.commit()

    def __getattr__(self, name):
        return getattr(self._c, name)


def _store(tmp_path: Path) -> MessageStore:
    return MessageStore(db_path=str(tmp_path / "lcm.db"))


def test_partial_null_index_exists_and_probe_uses_it(tmp_path):
    st = _store(tmp_path)
    conn = st._conn
    assert conn is not None
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_msg_search_content_null'"
    ).fetchone()
    assert row and "WHERE search_content IS NULL" in row[0]
    plan = " ".join(
        r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM messages WHERE search_content IS NULL LIMIT 1"
        ).fetchall()
    )
    assert "idx_msg_search_content_null" in plan, plan
    # a plain table scan reads "SCAN messages" with NO index clause
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


def test_backfill_populates_null_rows_in_committed_batches(tmp_path, monkeypatch):
    st = _store(tmp_path)
    conn = st._conn
    assert conn is not None
    monkeypatch.setattr(MessageStore, "BACKFILL_BATCH_ROWS", 3)
    # the default-fill trigger populates search_content on INSERT; null it
    # afterwards to model legacy rows (the exact shape the backfill exists for)
    for i in range(7):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s', 'user', ?, ?)",
            (st._cipher.encrypt_text(f"hello {i}", field="content"), float(i)),
        )
    conn.execute("UPDATE messages SET search_content = NULL")
    conn.commit()
    assert conn.execute("SELECT count(*) FROM messages WHERE search_content IS NULL").fetchone()[0] == 7
    spy = _SpyConn(conn)
    st._conn = spy  # type: ignore[assignment]
    st._backfill_search_content()
    assert conn.execute("SELECT count(*) FROM messages WHERE search_content IS NULL").fetchone()[0] == 0
    assert spy.commits >= 3, spy.commits   # 7 rows / batch 3 -> at least 3 batch commits
    assert conn.execute(
        "SELECT search_content FROM messages ORDER BY store_id LIMIT 1"
    ).fetchone()[0] == "hello 0"


def test_backfill_with_nothing_pending_issues_no_update(tmp_path):
    st = _store(tmp_path)
    conn = st._conn
    assert conn is not None
    spy = _SpyConn(conn)
    st._conn = spy  # type: ignore[assignment]
    st._backfill_search_content()
    assert not any(s.lstrip().upper().startswith("UPDATE") for s in spy.sql), spy.sql
    assert not any("LIMIT ?" in s for s in spy.sql), "batch SELECT must not run when nothing is pending"
    assert spy.commits == 0
