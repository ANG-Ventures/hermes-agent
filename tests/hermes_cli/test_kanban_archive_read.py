"""``kanban show`` / ``kanban_show`` read an old card's rows from the board's archive files (t_3be5b45f).

hermes-home kanban-events-retention.py (rule A1, #5971) moves comments + runs of done/archived cards into
<board dir>/kanban-archive.db and their events (R4) into <board dir>/kanban-events-archive.db; the tasks row
stays. The archive here is written with the retention job's own DDL and zlib encoding.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zlib
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

CARD_DDL = """CREATE TABLE archived_comments (
    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, author TEXT NOT NULL, body_z BLOB, created_at INTEGER NOT NULL,
    run_id INTEGER, session_ref TEXT, archived_at INTEGER NOT NULL);
CREATE TABLE archived_runs (
    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, step_key TEXT, status TEXT NOT NULL,
    claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, summary_z BLOB,
    metadata_z BLOB, error_z BLOB, worker_started_at INTEGER, resources TEXT, host TEXT,
    dropped_keys TEXT, archived_at INTEGER NOT NULL);"""
EVENTS_DDL = """CREATE TABLE archived_events (
    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,
    payload_z BLOB, created_at INTEGER NOT NULL, actor_profile TEXT, actor_session_id TEXT,
    rule TEXT, archived_at INTEGER NOT NULL);"""
RUN_COLS = ("id", "task_id", "profile", "step_key", "status", "claim_lock", "claim_expires", "worker_pid",
            "max_runtime_seconds", "last_heartbeat_at", "started_at", "ended_at", "outcome", "summary",
            "metadata", "error", "worker_started_at", "resources", "host")


def _z(v):
    return None if v is None else zlib.compress(v.encode("utf-8"), 6)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN")]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    path = kb.kanban_db_path()
    assert str(path).startswith(str(tmp_path))
    kb.init_db()
    return path


def make_card(status="done"):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="old finished card", assignee="daedalus")
        kb.add_comment(conn, tid, "apollo", "ruling: ship it")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute("UPDATE task_runs SET ended_at=started_at+60, status='done', outcome='completed', "
                     "summary='shipped the thing', metadata='{\"pr\": 7}' WHERE id=?", (run_id,))
        conn.execute("UPDATE tasks SET status=?, completed_at=1, current_run_id=NULL WHERE id=?", (status, tid))
        conn.commit()
    return tid


def archive(board_path, tid, *, noise_event=True):
    """Move tid's comments/runs/events off the board the way retention rule A1/R4 does."""
    d = os.path.dirname(board_path)
    b = sqlite3.connect(board_path)
    b.row_factory = sqlite3.Row
    ca = sqlite3.connect(os.path.join(d, "kanban-archive.db"))
    ea = sqlite3.connect(os.path.join(d, "kanban-events-archive.db"))
    ca.executescript(CARD_DDL)
    ea.executescript(EVENTS_DDL)
    for c in b.execute("SELECT * FROM task_comments WHERE task_id=?", (tid,)):
        ca.execute("INSERT INTO archived_comments VALUES (?,?,?,?,?,?,?,9)",
                   (c["id"], c["task_id"], c["author"], _z(c["body"]), c["created_at"], c["run_id"], c["session_ref"]))
    rcols = {r[1] for r in b.execute("PRAGMA table_info(task_runs)")}
    for r in b.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,)):
        vals = [(_z(r[k]) if k in ("summary", "metadata", "error") else r[k]) if k in rcols else None
                for k in RUN_COLS]
        ca.execute("INSERT INTO archived_runs (%s, dropped_keys, archived_at) VALUES (%s, NULL, 9)"
                   % (", ".join(k + "_z" if k in ("summary", "metadata", "error") else k for k in RUN_COLS),
                      ", ".join("?" * len(RUN_COLS))), vals)
    for e in b.execute("SELECT * FROM task_events WHERE task_id=?", (tid,)):
        ea.execute("INSERT INTO archived_events VALUES (?,?,?,?,?,?,?,?,'R4_terminal_full',9)",
                   (e["id"], e["task_id"], e["run_id"], e["kind"], _z(e["payload"]), e["created_at"],
                    e["actor_profile"], e["actor_session_id"]))
    if noise_event:  # an R1 heartbeat the retention job dropped on purpose: must stay hidden
        ea.execute("INSERT INTO archived_events VALUES (999999, ?, NULL, 'heartbeat', NULL, 2, NULL, NULL, "
                   "'R1_heartbeat', 9)", (tid,))
    for t in ("task_comments", "task_runs", "task_events"):
        b.execute("DELETE FROM %s WHERE task_id=?" % t, (tid,))
    for c in (b, ca, ea):
        c.commit()
        c.close()


def show_json(tid):
    out = json.loads(kc.run_slash(f"show {tid} --json"))
    return out


def tool_show(tid):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_show({"task_id": tid}))


def _strip(d):
    return {k: d[k] for k in ("comments", "events", "runs")}


def test_archived_card_shows_its_comments_runs_and_events(board):
    tid = make_card()
    before_cli, before_tool = _strip(show_json(tid)), _strip(tool_show(tid))
    assert before_cli["comments"] and before_cli["runs"] and before_cli["events"]
    archive(board, tid)
    with kb.connect() as conn:  # the board really is empty for this card now
        assert kb.list_comments(conn, tid) == [] and kb.list_runs(conn, tid) == [] and kb.list_events(conn, tid) == []
    assert _strip(show_json(tid)) == before_cli
    assert _strip(tool_show(tid)) == before_tool
    assert tool_show(tid)["runs"][0]["summary"] == "shipped the thing"
    assert "heartbeat" not in [e["kind"] for e in tool_show(tid)["events"]]


def test_live_card_never_reads_the_archive(board):
    tid = make_card(status="running")
    archive(board, tid)
    out = tool_show(tid)
    assert out["comments"] == [] and out["runs"] == [] and out["events"] == []


def test_board_rows_win_over_archive_rows(board):
    tid = make_card()
    archive(board, tid)
    with kb.connect() as conn:
        kb.add_comment(conn, tid, "apollo", "fresh note after archiving")
    bodies = [c["body"] for c in tool_show(tid)["comments"]]
    assert bodies == ["fresh note after archiving"]


def test_no_archive_file_is_a_noop(board):
    tid = make_card()
    before = _strip(tool_show(tid))
    assert not (Path(board).parent / "kanban-archive.db").exists()
    assert _strip(tool_show(tid)) == before


def test_show_does_not_attach_anything_to_the_board_connection(board):
    from hermes_cli import kanban_archive_read as kar
    tid = make_card()
    archive(board, tid)
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        dbs = [row[1] for row in conn.execute("PRAGMA database_list")]
        temp = conn.execute("SELECT type, name FROM sqlite_temp_master").fetchall()
        c, r, e = kar.fill(kb, conn, task, [], [], [])
        assert c and r and e
        assert [row[1] for row in conn.execute("PRAGMA database_list")] == dbs
        assert "karch" not in dbs and "kevarch" not in dbs
        assert conn.execute("SELECT type, name FROM sqlite_temp_master").fetchall() == temp
