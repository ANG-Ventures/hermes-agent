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


def _move_out(tid: str, delete: bool = True) -> Path:
    """What R8 does to one card: copy its rows (all columns + moved_at) to the archive, then delete them.
    ``delete=False`` stops after the archive commit (the window before R8 deletes the live rows)."""
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
            if delete:
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


def test_archive_path_with_uri_characters(tmp_path):
    from hermes_cli import kanban_archive_read as kar

    directory = tmp_path / "board?#% name"
    directory.mkdir()
    conn = sqlite3.connect(directory / "kanban.db")
    arch = sqlite3.connect(directory / kar.ARCHIVE_NAME)
    arch.execute("CREATE TABLE task_comments (id INTEGER, task_id TEXT, author TEXT, body TEXT, created_at INTEGER, run_id INTEGER)")
    arch.execute("INSERT INTO task_comments VALUES (1, 't_old', 'apollo', 'preserved', 1, NULL)")
    arch.commit()
    arch.close()
    try:
        assert kar.archived_rows(conn, "t_old")["comments"][0]["body"] == "preserved"
    finally:
        conn.close()


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


def test_locked_archive_is_surfaced_not_empty(kanban_home, monkeypatch):
    """A failed/locked archive read must not look like a card with no archived history."""
    from hermes_cli import kanban_archive_read as kar
    from tools import kanban_tools as kt

    tid = _done_card()
    arch = _move_out(tid)
    monkeypatch.setattr(kar, "_TIMEOUT_S", 0.2, raising=False)
    holder = sqlite3.connect(arch, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")   # held past the reader's timeout
    try:
        text = kc.run_slash(f"show {tid}")
        out = json.loads(kc.run_slash(f"show {tid} --json"))
        monkeypatch.setenv("HERMES_PROFILE", "daedalus")
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        shown = json.loads(kt._handle_show({"task_id": tid}))
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "archived history unreadable: OperationalError" in text
    assert out["archived"]["error"].startswith("OperationalError")
    assert out["archived"]["comments"] == [] and out["archived"]["runs"] == []
    assert shown["archived"]["error"].startswith("OperationalError")


def test_state_filter_applies_to_archived_runs(kanban_home):
    tid = _done_card()
    arch = _move_out(tid)
    a = sqlite3.connect(arch)
    cols = [r[1] for r in a.execute("PRAGMA table_info(task_runs)") if r[1] != "id"]
    a.execute("INSERT INTO task_runs (%s) SELECT %s FROM task_runs WHERE task_id=?"
              % (", ".join(cols), ", ".join(cols)), (tid,))
    a.execute("UPDATE task_runs SET outcome='crashed', summary='died' WHERE id=(SELECT max(id) FROM task_runs)")
    a.commit()
    assert sorted(r[0] for r in a.execute("SELECT outcome FROM task_runs WHERE task_id=?", (tid,))) == \
        ["completed", "crashed"]
    a.close()
    out = json.loads(kc.run_slash(f"show {tid} --state-type outcome --state-name crashed --json"))
    assert [r["outcome"] for r in out["archived"]["runs"]] == ["crashed"]
    text = kc.run_slash(f"show {tid} --state-type outcome --state-name crashed")
    assert "Archived runs (1, kanban-archive.db):" in text and "shipped the thing" not in text
    allruns = json.loads(kc.run_slash(f"show {tid} --json"))
    assert sorted(r["outcome"] for r in allruns["archived"]["runs"]) == ["completed", "crashed"]


# ---- live/archive race (t_f06682ef, Prism c17963c19b03): live rows are read first, the archive after,
# deduped by id, so a move committing between the reads can neither empty nor double the history.

def test_move_between_reads_keeps_the_run(kanban_home, monkeypatch):
    """R8 commits right after the live events read (between the comment and run reads)."""
    tid = _done_card()
    real = kb.list_events
    fired = []

    def list_events_then_move(conn, task_id, *a, **k):
        out = real(conn, task_id, *a, **k)
        if task_id == tid and not fired:
            fired.append(1)
            _move_out(tid)
        return out
    monkeypatch.setattr(kb, "list_events", list_events_then_move)
    out = json.loads(kc.run_slash(f"show {tid} --json"))
    assert fired
    runs = [r["summary"] for r in out["runs"]] + [r["summary"] for r in out.get("archived", {}).get("runs", [])]
    assert runs == ["shipped the thing in o/r#7"]
    comments = [c["body"] for c in out["comments"]] + \
        [c["body"] for c in out.get("archived", {}).get("comments", [])]
    assert comments == ["review_coverage: approve, moved later"]


def test_row_in_live_and_archive_listed_once_cli(kanban_home):
    tid = _done_card()
    _move_out(tid, delete=False)
    out = json.loads(kc.run_slash(f"show {tid} --json"))
    assert [c["body"] for c in out["comments"]] == ["review_coverage: approve, moved later"]
    assert [r["summary"] for r in out["runs"]] == ["shipped the thing in o/r#7"]
    assert "archived" not in out
    assert "Archived" not in kc.run_slash(f"show {tid}")


def test_row_in_live_and_archive_listed_once_tool(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    tid = _done_card()
    _move_out(tid, delete=False)
    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    shown = json.loads(kt._handle_show({"task_id": tid}))
    assert len(shown["comments"]) == 1 and len(shown["runs"]) == 1
    assert "archived" not in shown
