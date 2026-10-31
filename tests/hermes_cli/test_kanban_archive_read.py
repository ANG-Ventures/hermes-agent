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


def test_new_note_after_archiving_keeps_the_archived_history(board):
    tid = make_card()
    before = _strip(tool_show(tid))
    archive(board, tid)
    with kb.connect() as conn:
        kb.add_comment(conn, tid, "apollo", "fresh note after archiving")
    after = tool_show(tid)
    assert [c["body"] for c in after["comments"]] == ["ruling: ship it", "fresh note after archiving"]
    kinds = [e["kind"] for e in after["events"]]
    assert kinds[:len(before["events"])] == [e["kind"] for e in before["events"]] and kinds[-1] == "commented"
    assert after["runs"] == before["runs"]


def test_board_row_wins_on_a_duplicate_id(board):
    tid = make_card()
    archive(board, tid)
    ca = sqlite3.connect(os.path.join(os.path.dirname(board), "kanban-archive.db"))
    cid = ca.execute("SELECT id FROM archived_comments WHERE task_id=?", (tid,)).fetchone()[0]
    ca.close()
    with kb.connect() as conn:
        conn.execute("INSERT INTO task_comments (id, task_id, author, body, created_at) VALUES (?,?,?,?,?)",
                     (cid, tid, "apollo", "board copy", 1))
        conn.commit()
    assert [c["body"] for c in tool_show(tid)["comments"]] == ["board copy"]


def test_latest_summary_comes_from_archived_runs(board, capsys):
    tid = make_card()
    with kb.connect() as conn:
        conn.execute("UPDATE task_runs SET summary=? WHERE task_id=?", ("headline\nfull handoff details", tid))
        conn.commit()
        assert kb.get_task(conn, tid).result is None
    before = show_json(tid)["latest_summary"]
    assert before == "headline\nfull handoff details"
    archive(board, tid)
    assert show_json(tid)["latest_summary"] == before
    assert "full handoff details" in kc.run_slash(f"show {tid}")


def test_archived_run_projection_carries_worker_started_at_and_resources(board):
    from hermes_cli import kanban_archive_read as kar
    tid = make_card()
    with kb.connect() as conn:
        conn.execute("UPDATE task_runs SET worker_started_at=123, resources=? WHERE task_id=?",
                     ('{"cpu":2}', tid))
        conn.commit()
    archive(board, tid)
    (row,) = kar.archived_rows(os.path.dirname(board), "task_runs", tid)
    assert row["worker_started_at"] == 123 and row["resources"] == '{"cpu":2}'
    with kb.connect() as conn:
        board_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    assert board_cols <= set(row.keys())


@pytest.mark.parametrize("name", ["board%23one", "board#one", "a?b", "pct%41"])
def test_archive_path_with_uri_special_characters(tmp_path, name):
    from hermes_cli import kanban_archive_read as kar
    d = tmp_path / name
    d.mkdir()
    ca = sqlite3.connect(str(d / "kanban-archive.db"))
    ca.executescript(CARD_DDL)
    ca.execute("INSERT INTO archived_comments VALUES (1, 't_x', 'apollo', ?, 5, NULL, NULL, 9)", (_z("hi"),))
    ca.commit()
    ca.close()
    rows = kar.archived_rows(str(d), "task_comments", "t_x")
    assert [r["body"] for r in rows] == ["hi"]


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


def test_board_run_wins_even_when_run_filters_drop_it(board):
    from hermes_cli import kanban_archive_read as kar
    tid = make_card()
    archive(board, tid)
    ca = sqlite3.connect(os.path.join(os.path.dirname(board), "kanban-archive.db"))
    rid = ca.execute("SELECT id FROM archived_runs WHERE task_id=?", (tid,)).fetchone()[0]
    ca.close()
    with kb.connect() as conn:
        conn.execute("INSERT INTO task_runs (id, task_id, status, started_at, ended_at) VALUES (?,?,?,?,?)",
                     (rid, tid, "failed", 1, 2))
        conn.commit()
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid, state_type="status", state_name="done")
        assert runs == []
        _c, runs, _e = kar.fill(kb, conn, task, [], runs, [], state_type="status", state_name="done")
    assert runs == []


def test_latest_review_reviewer_comes_from_archived_events(board):
    tid = make_card()
    with kb.connect() as conn:
        rid = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        conn.execute("UPDATE task_runs SET outcome='changes_requested' WHERE id=?", (rid,))
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                     (tid, rid, "changes_requested", json.dumps({"reviewer": "apollo"}), 5))
        conn.commit()
    assert tool_show(tid)["latest_review"]["reviewer"] == "apollo"
    archive(board, tid)
    out = tool_show(tid)
    assert out["latest_review"]["run_id"] == rid and out["latest_review"]["reviewer"] == "apollo"
