"""Q15=B (t_8ee219f5): the ``tasks`` status-change audit trigger.

AFTER UPDATE OF status, current_run_id, worker_pid on ``tasks`` inserts a
``task_status_audit`` row. Rows written through a kanban_db connection carry
``kanban_db:<pid>``; any other client leaves ``unknown`` (the signal).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home

def _rows(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM task_status_audit WHERE task_id = ? ORDER BY id", (task_id,)
    )]


def _running_task(conn):
    tid = kb.create_task(conn, title="audit fixture", assignee="w")
    assert kb.claim_task(conn, tid) is not None
    return tid


def test_trigger_fires_on_status_run_id_and_pid_changes(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="audit fixture", assignee="w")
        assert _rows(conn, tid) == []
        assert kb.claim_task(conn, tid) is not None          # ready -> running + run id
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert kb._set_worker_pid(conn, tid, 4242, run_id=run_id)  # pid
        rows = _rows(conn, tid)
    # claim_task writes status and current_run_id in separate UPDATEs: a row each
    assert any(r["old_status"] == "ready" and r["new_status"] == "running" for r in rows), rows
    assert any(r["old_run_id"] is None and r["new_run_id"] == run_id for r in rows), rows
    assert rows[-1]["old_worker_pid"] is None and rows[-1]["new_worker_pid"] == 4242
    assert all(r["source"] == f"kanban_db:{os.getpid()}" for r in rows), rows


def test_untracked_column_updates_write_no_row(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        n = len(_rows(conn, tid))
        conn.execute("UPDATE tasks SET title='x', claim_expires=claim_expires+5 WHERE id=?", (tid,))
        # an UPDATE that sets a tracked column to the SAME value is not a change
        conn.execute("UPDATE tasks SET status=status WHERE id=?", (tid,))
        assert len(_rows(conn, tid)) == n


def test_raw_client_restore_before_exit_leaves_two_unknown_rows(kanban_home):
    """Arm A6-6: direct SQL to done, then back to running. Row is clean; audit is not."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        start = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_status_audit").fetchone()[0]
    db = kb.kanban_db_path()
    raw = sqlite3.connect(str(db))          # plain client: no kanban_db marker
    raw.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,)); raw.commit()
    raw.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,)); raw.commit()
    raw.close()
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"
        rows = [r for r in _rows(conn, tid) if r["id"] > start]
    assert [(r["old_status"], r["new_status"], r["source"]) for r in rows] == [
        ("running", "done", "unknown"), ("done", "running", "unknown")]


def test_marker_is_per_connection_not_per_database(kanban_home):
    """A kanban_db connection being open must not stamp a raw client's write."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        raw = sqlite3.connect(str(kb.kanban_db_path()))
        raw.execute("UPDATE tasks SET worker_pid=77 WHERE id=?", (tid,)); raw.commit(); raw.close()
        assert _rows(conn, tid)[-1]["source"] == "unknown"


def test_gc_prunes_audit_rows_older_than_retention(kanban_home):
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        old = int(time.time()) - 31 * 24 * 3600
        conn.execute("UPDATE task_status_audit SET changed_at=? WHERE task_id=?", (old, tid))
        conn.execute("UPDATE tasks SET worker_pid=5 WHERE id=?", (tid,))   # one fresh row
        removed = kb.gc_status_audit(conn, older_than_seconds=30 * 24 * 3600)
        assert removed >= 1
        assert [r["new_worker_pid"] for r in _rows(conn, tid)] == [5]


def test_one_run_lifecycle_row_count_and_attribution(kanban_home):
    """claim -> spawn -> complete through kanban_db: every row is this process's.
    The count is the per-run audit volume the retention estimate uses."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert kb._set_worker_pid(conn, tid, 4243, run_id=run_id)
        assert kb.complete_task(conn, tid, result="ok")
        rows = _rows(conn, tid)
    print(f"audit rows per run lifecycle: {len(rows)}")
    assert rows[-1]["new_status"] == "done"
    assert {r["source"] for r in rows} == {f"kanban_db:{os.getpid()}"}
