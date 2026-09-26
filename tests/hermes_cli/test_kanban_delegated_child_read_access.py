"""delegate_task children: read-only Kanban verbs + append-only comments (t_70fcc2c3).

A child used to be refused by EVERY ``hermes kanban`` verb, because the
mutation guard fired inside ``init_db``'s migration pass. Reads must work,
``comment`` must append (author marked ``(subagent)``), and status/ownership
mutations must stay parent-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _run(home: Path, *args: str, child: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_TASK"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if child:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60,
    )


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    home = tmp_path_factory.mktemp("hermes")
    created = _run(home, "create", "child access probe", "--json", child=False)
    assert created.returncode == 0, created.stderr
    return home, json.loads(created.stdout)["id"]


def _task(home: Path, task_id: str) -> dict:
    shown = _run(home, "show", task_id, "--json", child=False)
    assert shown.returncode == 0, shown.stderr
    return json.loads(shown.stdout)


@pytest.mark.parametrize(
    "argv",
    [
        ("show", "{id}"),
        ("show", "{id}", "--json"),
        ("list",),
        ("ls", "--json"),
        ("notify-list",),
        ("runs", "{id}"),
        ("context", "{id}"),
        ("stats",),
    ],
)
def test_child_read_verbs_succeed(board, argv):
    home, tid = board
    res = _run(home, *(a.format(id=tid) for a in argv), child=True)
    assert res.returncode == 0, res.stderr
    assert "cannot mutate" not in res.stderr
    assert "could not initialize database" not in res.stderr


def test_child_log_verb_reaches_handler(board):
    home, tid = board
    res = _run(home, "log", tid, child=True)
    # No worker ever ran, so the handler reports "no log" (rc 1) -- the point
    # is that it got past DB init instead of being refused there.
    assert "could not initialize database" not in res.stderr
    assert "cannot mutate" not in res.stderr
    assert "no log for" in (res.stdout + res.stderr)


def test_child_comment_appends_with_subagent_marker(board):
    home, tid = board
    res = _run(home, "comment", tid, "note from a child", "--author", "apollo", child=True)
    assert res.returncode == 0, res.stderr
    comments = _task(home, tid)["comments"]
    mine = [c for c in comments if c["body"] == "note from a child"]
    assert len(mine) == 1
    assert mine[0]["author"] == "apollo (subagent)"


@pytest.mark.parametrize(
    "argv",
    [
        ("complete", "{id}"),
        ("block", "{id}", "nope"),
        ("unblock", "{id}"),
        ("assign", "{id}", "someone"),
        ("create", "child-made card"),
        ("archive", "{id}"),
    ],
)
def test_child_status_and_ownership_mutations_stay_refused(board, argv):
    home, tid = board
    before = _task(home, tid)["task"]
    res = _run(home, *(a.format(id=tid) for a in argv), child=True)
    assert res.returncode == 1
    assert "delegate_task child contexts cannot mutate" in res.stderr
    after = _task(home, tid)["task"]
    assert (after["status"], after["assignee"]) == (before["status"], before["assignee"])


def test_child_cannot_initialize_missing_board(tmp_path):
    res = _run(tmp_path, "list", child=True)
    assert res.returncode == 1
    assert "cannot initialize a Kanban board" in res.stderr
    assert not (tmp_path / "kanban.db").exists()


def test_child_connection_is_query_only_even_after_comment(monkeypatch, tmp_path):
    """DB layer: a child cannot bypass write_txn with a raw UPDATE."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    from hermes_cli import kanban_db as kb
    from agent.delegation_context import delegated_child_context

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t")

    with delegated_child_context():
        with kb.connect_closing() as conn:
            kb.add_comment(conn, tid, "apollo (subagent)", "x")  # no double marker
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            with pytest.raises(PermissionError):
                kb.complete_task(conn, tid)

    with kb.connect_closing() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        authors = [c.author for c in kb.list_comments(conn, tid)]
    assert row["status"] != "done"
    assert authors == ["apollo (subagent)"]
