"""list_sessions_rich(order_by_last_active=True) pages by the indexed
``sessions.effective_last_active`` column instead of walking every session's
compression chain on each call.

The column is a cache of the recursive-CTE ordering key, so every test here is
a differential: the indexed page must equal the CTE page row-for-row, after
writes made through the public API AND through raw SQL (the triggers, not the
call sites, keep the cache honest).
"""
import random
import sqlite3
import time

import pytest

from hermes_state import SessionDB

T0 = time.time() - 10_000


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(tmp_path / "state.db")
    yield handle
    handle.close()


def _page(db, *, indexed, **kwargs):
    kwargs.setdefault("limit", 200)
    if not indexed:
        db._session_recency_current = lambda: False
    try:
        return db.list_sessions_rich(order_by_last_active=True, **kwargs)
    finally:
        db.__dict__.pop("_session_recency_current", None)


LIST_VARIANTS = [
    {},
    {"compact_rows": True},
    {"limit": 3},
    {"limit": 3, "offset": 2},
    {"include_children": True},
    {"include_pinned": True, "limit": 2},
    {"include_archived": True},
    {"min_message_count": 1},
    {"project_compression_tips": False},
]


def _assert_indexed_matches_cte(db):
    for variant in LIST_VARIANTS:
        indexed = _page(db, indexed=True, **variant)
        oracle = _page(db, indexed=False, **variant)
        assert indexed == oracle, variant
    raw = db._conn.execute(
        "SELECT COUNT(*) FROM session_recency_dirty"
    ).fetchone()[0]
    assert raw == 0, "an indexed read must leave the recency queue drained"


def _msg(db, sid, ts, text="hi"):
    db.append_message(session_id=sid, role="user", content=text, timestamp=ts)


def _compress(db, parent, child, ts):
    db.end_session(parent, "compression")
    db.create_session(session_id=child, source="cli", parent_session_id=parent)
    _msg(db, child, ts, f"continued {child}")


def test_indexed_page_uses_the_recency_index(db):
    db.create_session(session_id="a", source="cli")
    db.list_sessions_rich(order_by_last_active=True)
    plan = " ".join(
        str(row[-1]) for row in db._conn.execute(
            "EXPLAIN QUERY PLAN SELECT s.id FROM sessions s WHERE s.archived = 0 "
            "ORDER BY s.effective_last_active DESC, s.started_at DESC, s.id DESC LIMIT 20"
        ).fetchall()
    )
    assert "idx_sessions_effective_last_active" in plan
    assert "TEMP B-TREE" not in plan


def test_compressed_root_surfaces_by_its_live_tip(db):
    db.create_session(session_id="old", source="cli")
    _msg(db, "old", T0)
    db.create_session(session_id="mid", source="cli")
    _msg(db, "mid", T0 + 100)
    _compress(db, "old", "old-tip", T0 + 500)

    ids = [row["id"] for row in _page(db, indexed=True, project_compression_tips=False)]
    assert ids[:2] == ["old", "mid"]
    stored = db._conn.execute(
        "SELECT effective_last_active FROM sessions WHERE id = 'old'"
    ).fetchone()[0]
    assert stored == pytest.approx(T0 + 500)
    _assert_indexed_matches_cte(db)


def test_raw_sql_writes_are_tracked_by_triggers(db):
    for sid, ts in (("a", T0), ("b", T0 + 10), ("c", T0 + 20)):
        db.create_session(session_id=sid, source="cli")
        _msg(db, sid, ts)
    _compress(db, "a", "a2", T0 + 30)
    _assert_indexed_matches_cte(db)

    with db._lock:
        conn = db._conn
        # A message timestamp moved forward, a tip's rows deleted, a chain edge cut.
        conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = 'b'", (T0 + 900,))
        conn.execute("DELETE FROM messages WHERE session_id = 'a2'")
        conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = 'c'", (T0 + 800,))
        conn.commit()
    _assert_indexed_matches_cte(db)

    with db._lock:
        db._conn.execute("UPDATE sessions SET parent_session_id = NULL WHERE id = 'a2'")
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('a2', 'user', 'x', ?)",
            (T0 + 2000,),
        )
        db._conn.commit()
    _assert_indexed_matches_cte(db)
    stored = dict(db._conn.execute("SELECT id, effective_last_active FROM sessions").fetchall())
    assert stored["a"] == pytest.approx(T0)  # edge cut: a2's activity no longer lifts its old root


def test_randomized_writes_keep_index_equal_to_cte(db):
    rng = random.Random(4242)
    sessions = []
    clock = [T0]

    def tick():
        clock[0] += rng.uniform(1, 50)
        return clock[0]

    def new_root():
        sid = f"s{len(sessions)}"
        db.create_session(session_id=sid, source=rng.choice(["cli", "telegram", "tui"]))
        sessions.append(sid)
        return sid

    for _ in range(4):
        new_root()
    for step in range(160):
        op = rng.choice([
            "root", "msg", "msg", "old_msg", "compress", "branch", "delegate",
            "tool_child", "touch", "archive", "pin", "delete", "reopen",
        ])
        sid = rng.choice(sessions)
        if op == "root":
            new_root()
        elif op == "msg":
            _msg(db, sid, tick())
        elif op == "old_msg":
            _msg(db, sid, T0 - rng.uniform(1, 5000))
        elif op == "compress":
            child = f"{sid}-c{step}"
            _compress(db, sid, child, tick())
            sessions.append(child)
        elif op in ("branch", "delegate", "tool_child"):
            child = f"{sid}-{op}{step}"
            marker = {"branch": {"_branched_from": sid}, "delegate": {"_delegate_from": sid}}.get(op)
            db.create_session(
                session_id=child, source="tool" if op == "tool_child" else "cli",
                parent_session_id=sid, model_config=marker,
            )
            _msg(db, child, tick())
            sessions.append(child)
        elif op == "touch":
            db.touch_session_activity(sid, tick())
        elif op == "archive":
            db.set_session_archived(sid, rng.random() < 0.5)
        elif op == "pin":
            with db._lock:
                db._conn.execute("UPDATE sessions SET pinned = 1 WHERE id = ?", (sid,))
                db._conn.commit()
        elif op == "delete" and len(sessions) > 3:
            db.delete_session(sid)  # may cascade delegate children
            sessions[:] = [row[0] for row in db._conn.execute("SELECT id FROM sessions ORDER BY rowid")]
        elif op == "reopen":
            db.reopen_session(sid)
        if step % 8 == 0:
            _assert_indexed_matches_cte(db)
    _assert_indexed_matches_cte(db)


def test_read_only_handle_with_queued_rows_falls_back_to_cte(db, tmp_path):
    for sid, ts in (("a", T0), ("b", T0 + 10)):
        db.create_session(session_id=sid, source="cli")
        _msg(db, sid, ts)
    db.list_sessions_rich(order_by_last_active=True)  # drain
    _msg(db, "a", T0 + 99)  # queued, not drained

    reader = SessionDB(tmp_path / "state.db", read_only=True)
    try:
        assert reader._session_recency_current() is False
        ids = [row["id"] for row in reader.list_sessions_rich(order_by_last_active=True)]
    finally:
        reader.close()
    assert ids == ["a", "b"]


def test_existing_store_is_seeded_on_open(tmp_path):
    path = tmp_path / "state.db"
    first = SessionDB(path)
    for sid, ts in (("a", T0), ("b", T0 + 10)):
        first.create_session(session_id=sid, source="cli")
        _msg(first, sid, ts)
    _compress(first, "a", "a2", T0 + 50)
    with first._lock:
        # Simulate a store written before the recency cache existed.
        first._conn.execute("UPDATE sessions SET effective_last_active = NULL")
        first._conn.execute("DELETE FROM session_recency_dirty")
        first._conn.execute("DELETE FROM state_meta WHERE key = 'session_recency_seeded'")
        first._conn.commit()
    first.close()

    reopened = SessionDB(path)
    try:
        ids = [row["id"] for row in _page(reopened, indexed=True, project_compression_tips=False)]
        assert ids == ["a", "b"]
        _assert_indexed_matches_cte(reopened)
    finally:
        reopened.close()


def test_search_and_id_queries_keep_the_chain_cte(db):
    db.create_session(session_id="alpha", source="cli")
    _msg(db, "alpha", T0)
    _compress(db, "alpha", "alpha-tip", T0 + 5)
    calls = []
    db._session_recency_current = lambda: calls.append(1) or True
    try:
        rows = db.list_sessions_rich(order_by_last_active=True, id_query="alpha-tip")
    finally:
        db.__dict__.pop("_session_recency_current", None)
    assert calls == []
    assert [row["id"] for row in rows] == ["alpha-tip"]
