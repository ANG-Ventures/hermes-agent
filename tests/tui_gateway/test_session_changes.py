from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB
from tui_gateway import server


def _call(method: str, params: dict) -> dict:
    return server._methods[method](1, params)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: session_db)
    yield session_db
    session_db.close()


def test_session_changes_returns_ordered_rows_after_cursor_and_uses_index(db):
    db.create_session("s1", source="desktop")
    first_id = db.append_message("s1", "user", "one", timestamp=1.0)
    second_id = db.append_message("s1", "assistant", "two", timestamp=2.0)
    third_id = db.append_message("s1", "user", "three", timestamp=3.0)
    db.create_session("other", source="desktop")
    db.append_message("other", "user", "ignore me", timestamp=4.0)

    envelope = _call(
        "session.changes",
        {"session_id": "s1", "since_message_id": first_id},
    )

    assert "error" not in envelope
    result = envelope["result"]
    assert [message["id"] for message in result["messages"]] == [second_id, third_id]
    assert [message["text"] for message in result["messages"]] == ["two", "three"]
    assert [message["timestamp"] for message in result["messages"]] == [2.0, 3.0]
    assert result["last_id"] == third_id

    plan_rows = db._conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT * FROM messages
        WHERE session_id = ? AND id > ?
        ORDER BY id
        """,
        ("s1", first_id),
    ).fetchall()
    plan = "\n".join(str(tuple(row)) for row in plan_rows)
    assert "USING INDEX idx_messages_session_id" in plan


def test_session_changes_unknown_session_returns_clean_json_rpc_error(db):
    envelope = _call(
        "session.changes",
        {"session_id": "missing", "since_message_id": 0},
    )

    assert envelope["error"]["code"] == 4044
    assert "session not found" in envelope["error"]["message"]
    assert "traceback" not in str(envelope).lower()
