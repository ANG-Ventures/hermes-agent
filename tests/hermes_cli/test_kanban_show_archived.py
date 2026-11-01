"""``kanban show`` lists rows the retention job moved to ``kanban-archive.db`` (t_d1b5fc83).

The fleet retention job (hermes-home R8) moves the comments/runs/events of long-done cards into
``kanban-archive.db`` beside the board: same table names and columns, plus ``moved_at``. The tasks
row stays. ``show`` (CLI text + JSON, and the ``kanban_show`` tool) is the one reader that lists
them, labelled archived; with no archive file nothing changes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _db_path(conn) -> Path:
    return Path(next(p for _s, n, p in conn.execute("PRAGMA database_list") if n == "main"))


def _move_out(tid: str) -> Path:
    """What R8 does to one card: copy its rows (all columns + moved_at) to the archive, then delete them."""
    conn = kbc.connect()
    try:
        arch = _db_path(conn).parent / "kanban-archive.db"
        conn.execute("ATTACH DATABASE ? AS march", (str(arch),))
        for tbl in ("task_comments", "task_runs", "task_events"):
            cols = [r[1] for r in conn.execute("PRAGMA main.table_info(%s)" % tbl)]
            conn.execute("CREATE TABLE IF NOT EXISTS march.%s (id INTEGER PRIMARY KEY, %s, moved_at INTEGER NOT NULL)"
                         % (tbl, ", ".join(c for c in cols if c != "id")))
            cl = ", ".join(cols)
            conn.execute("INSERT INTO march.%s (%s, moved_at) SELECT %s, 1 FROM main.%s WHERE task_id=?"
                         % (tbl, cl, cl, tbl), (tid,))
            conn.execute("DELETE FROM main.%s WHERE task_id=?" % tbl, (tid,))
        conn.commit()
        conn.execute("DETACH DATABASE march")
        return arch
    finally:
        conn.close()


def _done_card() -> str:
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="old done card", assignee="daedalus")
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, "apollo", "review_coverage: approve, moved later")
        kb.complete_task(conn, tid, summary="shipped the thing in o/r#7")
    finally:
        conn.close()
    return tid


def test_show_lists_archived_rows_labelled(kanban_home):
    tid = _done_card()
    _move_out(tid)
    text = kc.run_slash(f"show {tid}")
    assert "Archived comments (1, kanban-archive.db):" in text
    assert "[archived] apollo: review_coverage: approve, moved later" in text
    assert "Archived runs (1, kanban-archive.db):" in text
    assert "shipped the thing in o/r#7" in text
    out = json.loads(kc.run_slash(f"show {tid} --json"))
    assert out["comments"] == [] and out["runs"] == []   # the live board holds none of them
    assert [c["body"] for c in out["archived"]["comments"]] == ["review_coverage: approve, moved later"]
    assert out["archived"]["runs"][0]["summary"] == "shipped the thing in o/r#7"
    assert out["task"]["status"] == "done"                # the tasks row stayed


def test_no_archive_file_changes_nothing(kanban_home):
    tid = _done_card()
    assert "Archived" not in kc.run_slash(f"show {tid}")
    assert "archived" not in json.loads(kc.run_slash(f"show {tid} --json"))


def test_archive_without_this_card_changes_nothing(kanban_home):
    _move_out(_done_card())
    other = _done_card()
    assert "Archived" not in kc.run_slash(f"show {other}")
    assert "archived" not in json.loads(kc.run_slash(f"show {other} --json"))


def test_kanban_show_tool_reports_archived_counts(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    tid = _done_card()
    _move_out(tid)
    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    shown = json.loads(kt._handle_show({"task_id": tid}))
    arch = shown["archived"]
    assert (arch["comments"], arch["runs"]) == (1, 1)
    assert arch["newest_comment"]["body"] == "review_coverage: approve, moved later"
    assert arch["newest_run"]["summary"] == "shipped the thing in o/r#7"
    assert f"hermes kanban show {tid}" in arch["note"]
