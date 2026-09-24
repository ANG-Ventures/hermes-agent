"""REGRESSION: a structural FTS rebuild must never run on the engine-load thread.

Residual of the 2026-09-24 Apollo freeze #3 (card t_1e04c4bd). #971 made the
repair ONE transaction, but when the engine-load path (``throttle=True``) found
genuine structural damage (a missing FTS5 shadow table, say) it still rebuilt
INLINE, holding _LOAD_LOCK and the SQLite write lock for the whole O(rows)
rebuild: 1696 s on an 11.4 GB fleet snapshot. Every turn and every ingest waited.

Contract now: the load thread issues no ``'rebuild'``. It drops the (unwritable)
FTS triggers so ingest keeps working, flags ``fts_integrity_failed``, and returns.
Search falls back to LIKE. A daemon thread rebuilds atomically and recreates the
triggers; rows ingested during the window end up indexed.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

import plugins.context_engine.lcm.db_bootstrap as B
from plugins.context_engine.lcm.db_bootstrap import (
    join_background_integrity_scans,
    load_integrity_failed,
    repair_external_content_fts,
)
from plugins.context_engine.lcm.store import MessageStore, build_message_fts_spec

# Above the inline bound, so the load path must defer.
N_ROWS = getattr(B, "INLINE_STRUCTURAL_REBUILD_MAX_ROWS", 1_000) + 200


def _damaged_db(db: Path) -> None:
    st = MessageStore(db_path=str(db))
    conn = st._conn
    assert conn is not None
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, search_content, timestamp) "
        "VALUES (?,?,?,?,?)",
        [
            (
                "s",
                "user",
                st._cipher.encrypt_text("hello world %d" % i, field="content"),
                "hello world %d" % i,
                float(i),
            )
            for i in range(N_ROWS)
        ],
    )
    conn.commit()
    conn.execute("DROP TABLE messages_fts_config")
    conn.commit()
    st.close()


class _Recorder:
    """Wrap sqlite3.connect so every connection records (thread, sql)."""

    def __init__(self, monkeypatch, gate: threading.Event | None = None):
        self.statements: list[tuple[int, str]] = []
        self._lock = threading.Lock()
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            if gate is not None and threading.current_thread().name.startswith(
                "lcm-fts-rebuild-"
            ):
                # Hold the rebuild BEFORE it takes the write lock, so the test can
                # observe the degraded window.
                assert gate.wait(30), "test never released the background rebuild"
            conn = real_connect(*args, **kwargs)

            def trace(sql: str) -> None:
                with self._lock:
                    self.statements.append((threading.get_ident(), sql))

            conn.set_trace_callback(trace)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect)

    def rebuilds(self, *, on_thread: int | None = None, off_thread: int | None = None):
        with self._lock:
            rows = list(self.statements)
        return [
            sql
            for ident, sql in rows
            if "'rebuild'" in sql
            and (on_thread is None or ident == on_thread)
            and (off_thread is None or ident != off_thread)
        ]


def test_engine_load_defers_structural_rebuild_to_background(tmp_path, monkeypatch):
    monkeypatch.delenv(B.BACKGROUND_INTEGRITY_ENV, raising=False)
    db = tmp_path / "lcm.db"
    _damaged_db(db)

    # Hold the background rebuild until the degraded window has been checked.
    gate = threading.Event()
    rec = _Recorder(monkeypatch, gate=gate)
    loader = threading.get_ident()

    st = MessageStore(db_path=str(db))
    try:
        assert rec.rebuilds(on_thread=loader) == [], (
            "engine construction issued the O(rows) FTS 'rebuild' on the loading thread"
        )
        spec = build_message_fts_spec()
        flag = load_integrity_failed(st._conn, spec)
        assert flag is not None and "structural" in flag["detail"]

        # Degraded window: ingest must still work (triggers dropped) and search
        # must answer via the LIKE fallback.
        st.append("s", {"role": "user", "content": "window message zebra"})
        hits = st.search("zebra")
        assert any("zebra" in (h.get("content") or "") for h in hits)

        gate.set()
        join_background_integrity_scans(timeout=60.0)
        assert rec.rebuilds(off_thread=loader), "background thread never rebuilt the index"

        check = sqlite3.connect(str(db))
        try:
            total = check.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            docs = check.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0]
            assert docs == total == N_ROWS + 1, "rows ingested during the window were not indexed"
            assert not B._fts_needs_rebuild_structural(check, spec)
            assert not B._fts_missing_triggers(check, spec)
            matched = check.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'zebra'"
            ).fetchone()[0]
            assert matched == 1
            assert load_integrity_failed(check, spec) is None
        finally:
            check.close()
    finally:
        gate.set()
        join_background_integrity_scans(timeout=60.0)
        st.close()


def test_kill_switch_keeps_inline_rebuild(tmp_path, monkeypatch):
    monkeypatch.setenv(B.BACKGROUND_INTEGRITY_ENV, "false")
    db = tmp_path / "lcm.db"
    _damaged_db(db)
    rec = _Recorder(monkeypatch)
    st = MessageStore(db_path=str(db))
    try:
        assert rec.rebuilds(on_thread=threading.get_ident())
        assert not B._fts_needs_rebuild_structural(st._conn, build_message_fts_spec())
    finally:
        st.close()


def test_explicit_repair_stays_synchronous(tmp_path, monkeypatch):
    monkeypatch.delenv(B.BACKGROUND_INTEGRITY_ENV, raising=False)
    db = tmp_path / "lcm.db"
    _damaged_db(db)
    conn = sqlite3.connect(str(db))
    try:
        result = repair_external_content_fts(conn, build_message_fts_spec(), throttle=False)
        assert result["rebuilt"] is True
        assert "rebuild_deferred" not in result
        docs = conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0]
        assert docs == N_ROWS
    finally:
        conn.close()
