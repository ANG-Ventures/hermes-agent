"""Integration proof: the journal fires from the REAL kanban_db write path.

The unit tests exercise kanban_journal directly. This asserts the WIRING --
that a normal create/comment/complete through kanban_db's public API lands in
the journal, with the comment BODY, without anyone calling the journal by hand.
An unwired journal passes every unit test and saves nothing.

Runs entirely inside a sandboxed HERMES_HOME (HERMES_KANBAN_SANDBOX=1), so it
never touches the live board.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith("HERMES_KANBAN")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", str(tmp_path / "state" / "kanban-journal"))
    monkeypatch.delenv("HERMES_KANBAN_JOURNAL", raising=False)
    return tmp_path


def _entries(root: Path, board: str = "default"):
    path = root / "state" / "kanban-journal" / (board + ".jsonl")
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def test_real_write_path_populates_the_journal(sandbox):
    from hermes_cli import kanban_db

    conn = kanban_db.connect()
    task_id = kanban_db.create_task(
        conn, title="journal wiring probe", assignee="daedalus",
    )
    if isinstance(task_id, dict):  # tolerate a richer return shape
        task_id = task_id.get("id") or task_id.get("task_id")

    kanban_db.add_comment(
        conn, task_id, "apollo", "the body that must survive a wipe",
        run_id=None, session_ref=None,
    )

    entries = _entries(sandbox)
    assert entries, "the journal is EMPTY -- the hook is not wired"

    kinds = [e["kind"] for e in entries]
    assert "created" in kinds, kinds
    assert "commented" in kinds, kinds

    # The content hook: the BODY, not just the fact of a comment. This is the
    # specific thing that was unrecoverable on 2026-09-21.
    bodies = [
        e["payload"].get("body")
        for e in entries
        if e["kind"] == "comment_body"
    ]
    assert "the body that must survive a wipe" in bodies, entries

    for entry in entries:
        assert entry["task_id"] in (task_id, None)
        assert isinstance(entry["ts"], float)


def test_the_event_journal_alone_would_not_have_saved_the_threads(sandbox):
    """Why the content hook exists, pinned as a test.

    The ``commented`` EVENT carries author + length and no text. If someone
    later 'simplifies' by dropping the comment_body hook, this fails.
    """
    from hermes_cli import kanban_db

    conn = kanban_db.connect()
    task_id = kanban_db.create_task(conn, title="x", assignee="daedalus")
    if isinstance(task_id, dict):
        task_id = task_id.get("id") or task_id.get("task_id")
    secret = "irreplaceable thread content"
    kanban_db.add_comment(conn, task_id, "ace", secret, run_id=None, session_ref=None)

    entries = _entries(sandbox)
    commented = [e for e in entries if e["kind"] == "commented"]
    assert commented, "expected a commented event"
    assert all(secret not in json.dumps(e["payload"]) for e in commented), (
        "the event payload now carries the body; the comment_body hook may be "
        "redundant -- re-check before removing either")

    content = [e for e in entries if e["kind"] == "comment_body"]
    assert content and secret in json.dumps(content[0]["payload"])


def test_a_broken_journal_does_not_break_a_board_write(sandbox, monkeypatch):
    """The net must never become a gate."""
    from hermes_cli import kanban_db

    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", "/proc/cannot-write-here")
    conn = kanban_db.connect()
    task_id = kanban_db.create_task(conn, title="still works", assignee="daedalus")
    if isinstance(task_id, dict):
        task_id = task_id.get("id") or task_id.get("task_id")
    assert task_id
    kanban_db.add_comment(conn, task_id, "ace", "written anyway",
                          run_id=None, session_ref=None)
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchall()
    assert [r[0] for r in rows] == ["written anyway"]


def test_journal_off_switch_is_honoured_end_to_end(sandbox, monkeypatch):
    from hermes_cli import kanban_db

    monkeypatch.setenv("HERMES_KANBAN_JOURNAL", "off")
    conn = kanban_db.connect()
    task_id = kanban_db.create_task(conn, title="quiet", assignee="daedalus")
    if isinstance(task_id, dict):
        task_id = task_id.get("id") or task_id.get("task_id")
    kanban_db.add_comment(conn, task_id, "ace", "no journal please",
                          run_id=None, session_ref=None)
    assert _entries(sandbox) == []
