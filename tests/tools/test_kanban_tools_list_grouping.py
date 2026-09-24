"""kanban_list (tool) is session-first, like ``hermes kanban list``.

With a caller session: THIS session's cards come back in full under
``tasks``; every other session's card (and unhomed cards) collapses to
``{id, status, title}`` under ``other_sessions``. ``all=true`` (or no caller
session at all) is the old flat view.
"""
from __future__ import annotations

import json

import pytest

MINE = "20260924_000000_mine"
THEIRS = "20260924_000000_theirs"


@pytest.fixture(autouse=True)
def _reset_session_id_contextvar():
    from gateway.session_context import _SESSION_ID, _UNSET
    _SESSION_ID.set(_UNSET)
    yield
    _SESSION_ID.set(_UNSET)


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        mine = kb.create_task(conn, title="my card", assignee="w", session_id=MINE)
        theirs = kb.create_task(conn, title="their card", assignee="w", session_id=THEIRS)
        orphan = kb.create_task(conn, title="cron card", assignee="w")
        stamp = conn.execute(
            "SELECT session_id FROM tasks WHERE id = ?", (orphan,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert stamp == kb.UNHOMED_SESSION
    return {"mine": mine, "theirs": theirs, "orphan": orphan}


def test_tool_list_groups_this_session_first(monkeypatch, board):
    monkeypatch.setenv("HERMES_SESSION_ID", MINE)
    from tools import kanban_tools as kt

    d = json.loads(kt._handle_list({}))
    assert d["grouped"] is True
    assert [t["id"] for t in d["tasks"]] == [board["mine"]]
    assert d["this_session_count"] == 1
    # Full summary shape for my card only.
    assert "parent_count" in d["tasks"][0]
    others = {o["id"]: o for o in d["other_sessions"]}
    assert set(others) == {board["theirs"], board["orphan"]}
    # Collapsed to one line of facts per foreign card.
    assert set(others[board["theirs"]]) == {"id", "status", "title"}
    assert others[board["orphan"]]["unhomed"] is True
    assert d["count"] == 3


@pytest.mark.parametrize("flag", [True, "true"])
def test_tool_list_all_is_flat(monkeypatch, board, flag):
    monkeypatch.setenv("HERMES_SESSION_ID", MINE)
    from tools import kanban_tools as kt

    d = json.loads(kt._handle_list({"all": flag}))
    assert "grouped" not in d and "other_sessions" not in d
    assert {t["id"] for t in d["tasks"]} == set(board.values())


def test_tool_list_without_caller_session_is_flat(board):
    from tools import kanban_tools as kt

    d = json.loads(kt._handle_list({}))
    assert "grouped" not in d
    assert {t["id"] for t in d["tasks"]} == set(board.values())
