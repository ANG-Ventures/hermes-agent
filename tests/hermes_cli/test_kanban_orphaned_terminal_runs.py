"""Runs left ``running`` on a done/archived card are ended by the reaper.

Every other reclaim path selects ``tasks WHERE status='running'``, so a run
whose card went terminal without closing it stayed open forever (4 live rows,
card t_cb91bfc4). ``end_orphaned_terminal_runs`` closes them when the pid is
dead or the heartbeat is older than max_runtime; a run on a live card, or a
live-and-fresh worker on a terminal card, is untouched.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c


def _dead_pid() -> int:
    p = subprocess.Popen(["true"], stdin=subprocess.DEVNULL)
    p.wait()
    return p.pid


def _open_run(conn, tid, *, task_status, pid, heartbeat, current=True):
    now = int(time.time())
    host = kb._claimer_id().split(":", 1)[0]
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, worker_pid, "
        "last_heartbeat_at, started_at) VALUES (?, 'w', 'running', ?, ?, ?, ?)",
        (tid, f"{host}:1", pid, heartbeat, now - 7200),
    )
    run_id = cur.lastrowid
    conn.execute(
        "UPDATE tasks SET status = ?, current_run_id = ? WHERE id = ?",
        (task_status, run_id if current else None, tid),
    )
    conn.commit()
    return run_id


def _run(conn, run_id):
    return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def test_archived_card_dead_pid_run_is_ended_in_one_sweep(conn):
    tid = kb.create_task(conn, title="archived", assignee="w")
    rid = _open_run(conn, tid, task_status="archived", pid=_dead_pid(),
                    heartbeat=int(time.time()), current=False)

    assert kb.end_orphaned_terminal_runs(conn) == [rid]

    row = _run(conn, rid)
    assert row["ended_at"] is not None
    assert row["status"] == "reclaimed"
    assert row["outcome"] == "orphaned_terminal_task"
    assert row["worker_pid"] is None and row["claim_lock"] is None
    ev = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'orphaned_terminal_run_ended'", (tid, rid),
    ).fetchone()
    assert ev is not None and json.loads(ev["payload"])["pid_dead"] is True
    # Idempotent: a second sweep finds nothing.
    assert kb.end_orphaned_terminal_runs(conn) == []


def test_done_card_stale_heartbeat_ends_even_with_live_pid(conn):
    import os
    tid = kb.create_task(conn, title="done-stale", assignee="w")
    rid = _open_run(conn, tid, task_status="done", pid=os.getpid(),
                    heartbeat=int(time.time()) - 2 * kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS)

    assert kb.end_orphaned_terminal_runs(conn) == [rid]
    assert kb.connect  # noqa
    assert conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
    ).fetchone()["current_run_id"] is None


def test_live_fresh_worker_on_done_card_is_left_alone(conn):
    import os
    tid = kb.create_task(conn, title="finalising", assignee="w")
    rid = _open_run(conn, tid, task_status="done", pid=os.getpid(),
                    heartbeat=int(time.time()))

    assert kb.end_orphaned_terminal_runs(conn) == []
    assert _run(conn, rid)["ended_at"] is None


def test_running_run_on_live_card_is_untouched(conn):
    tid = kb.create_task(conn, title="live", assignee="w")
    rid = _open_run(conn, tid, task_status="running", pid=_dead_pid(),
                    heartbeat=int(time.time()) - 10 ** 6)

    assert kb.end_orphaned_terminal_runs(conn) == []
    row = _run(conn, rid)
    assert row["ended_at"] is None and row["status"] == "running"


def test_dispatch_once_runs_the_sweep(conn):
    tid = kb.create_task(conn, title="archived", assignee="w")
    rid = _open_run(conn, tid, task_status="archived", pid=_dead_pid(),
                    heartbeat=int(time.time()), current=False)

    res = kb.dispatch_once(conn, dry_run=True)

    assert res.ended_terminal_runs == [rid]
    assert _run(conn, rid)["outcome"] == "orphaned_terminal_task"
