"""Hot listing queries must not sort whole rows into SQLite temp files (t_d24713d3).

On the Studio, ``kanban list`` sorted every task row (bodies, results): 198 MB of etilqs_* temp files per
call; the gateway's channel directory sorted every keyed session row with its ~100 KB resolved system
prompt: 117 MB per call. Both now rank (keys, id) in a subquery and join the full rows by primary key, so
no top-level step of the statement may be a temp B-tree or an automatic index.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._fixtures import sqlite_plan_guard as guard
from hermes_cli import kanban_db as kb
from hermes_state import SessionDB


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_list_tasks_sorts_keys_not_rows(kanban_home):
    with kb.connect() as conn:
        low = kb.create_task(conn, title="low", body="x" * 5000, priority=1)
        high = kb.create_task(conn, title="high", body="y" * 5000, priority=9)
        mid = kb.create_task(conn, title="mid", priority=5)
        rec = guard.Recorder(conn)
        assert [t.id for t in kb.list_tasks(rec)] == [high, mid, low]
        assert [t.id for t in kb.list_tasks(rec, order_by="title")] == [high, low, mid]
        assert [t.id for t in kb.list_tasks(rec, limit=2)] == [high, mid]
        assert kb.list_tasks(rec, order_by="priority-desc")[0].body.endswith("x" * 5000)
        for order in kb.VALID_SORT_ORDERS:
            kb.list_tasks(rec, order_by=order)
        listing = [s for s in rec.statements if "FROM tasks" in s[0]]
        assert len(listing) == 4 + len(kb.VALID_SORT_ORDERS)
        assert guard.spills(conn, listing, outer_only=True) == []


def test_list_tasks_title_sort_has_no_sql_sort(kanban_home):
    """--sort title sorted (title, id) in SQL: long titles spilled 4.1 MB per call on the Studio board even
    as bare keys (t_ba5a93d0). It now sorts in Python, with SQLite's BINARY order for the same rows."""
    titles = ["Fix forward main red mt-1: " + "x" * 300 + "b", "Fix forward main red mt-1: " + "x" * 300 + "a",
              "Ärger", "Zeta", "zeta", "émoji 🚀", "Fix forward main red mt-1: " + "x" * 300 + "a"]
    with kb.connect() as conn:
        for t in titles:
            kb.create_task(conn, title=t, body="b" * 2000)
        rec = guard.Recorder(conn)
        got = [t.id for t in kb.list_tasks(rec, order_by="title")]
        want = [r[0] for r in conn.execute(
            "SELECT id FROM tasks WHERE status != 'archived' ORDER BY title ASC, id ASC")]
        assert got == want
        assert [t.id for t in kb.list_tasks(rec, order_by="title", limit=3)] == want[:3]
        assert guard.spills(conn, rec.statements) == []


def test_list_gateway_sessions_sorts_keys_not_rows(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for sid, src in (("gw-a", "discord"), ("gw-b", "discord"), ("gw-c", "telegram")):
            db.create_session(sid, src, session_key=f"agent:main:{src}:dm:{sid}", chat_id=sid, chat_type="dm",
                              system_prompt="p" * 20000)
        db._conn.execute("UPDATE sessions SET last_activity_at = 100 WHERE id = 'gw-a'")
        db._conn.execute("UPDATE sessions SET last_activity_at = 300 WHERE id = 'gw-b'")
        db._conn.execute("UPDATE sessions SET last_activity_at = 200 WHERE id = 'gw-c'")
        db._conn.commit()
        recorded = []
        real_read_all = db._read_all

        def _recording_read_all(sql, params=()):
            recorded.append((sql, tuple(params)))
            return real_read_all(sql, params)

        db._read_all = _recording_read_all
        rows = db.list_gateway_sessions(active_only=False)
        assert [r["id"] for r in rows] == ["gw-b", "gw-c", "gw-a"]
        assert rows[0]["system_prompt"] == "p" * 20000
        assert "_gateway_list_rn" not in rows[0]
        assert [r["id"] for r in db.list_gateway_sessions(platform="discord", active_only=False)] == ["gw-b", "gw-a"]
        listing = [s for s in recorded if "system_prompts" in s[0]]
        assert len(listing) == 2
        assert guard.spills(db._conn, listing, outer_only=True) == []
    finally:
        db.close()
