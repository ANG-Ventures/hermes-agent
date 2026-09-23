"""Tests for the append-only kanban mutation journal and its replay tool.

Card t_357330bf. The headline test is :func:`test_incident_replay_recovers_the
_comment_threads`, which reproduces the 2026-09-21 loss shape end to end:
snapshot the board, keep writing (including comments), destroy the board, then
restore snapshot + journal and assert the post-snapshot comment BODIES come
back. That is the exact thing that was unrecoverable in the real incident.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture()
def journal_env(tmp_path, monkeypatch):
    """Isolate the journal directory; never touch the live one."""
    jdir = tmp_path / "journal"
    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", str(jdir))
    monkeypatch.delenv("HERMES_KANBAN_JOURNAL", raising=False)
    from hermes_cli import kanban_journal

    return kanban_journal, jdir


def test_append_writes_one_json_line_per_mutation(journal_env):
    kanban_journal, jdir = journal_env
    assert kanban_journal.append("b1", "t_a", "created", {"title": "x"}) is True
    assert kanban_journal.append("b1", "t_a", "commented", {"author": "ace"}) is True

    path = jdir / "b1.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["board"] == "b1"
    assert first["task_id"] == "t_a"
    assert first["kind"] == "created"
    assert first["payload"] == {"title": "x"}
    assert isinstance(first["ts"], float)


def test_journal_lives_outside_the_kanban_home(tmp_path, monkeypatch):
    """The deleter took ~/.hermes/kanban wholesale; the journal must not be in it."""
    monkeypatch.delenv("HERMES_KANBAN_JOURNAL_DIR", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import kanban_journal

    d = kanban_journal.journal_dir().resolve()
    kanban_home = (Path(tmp_path) / "kanban").resolve()
    assert kanban_home not in d.parents and d != kanban_home, (
        "journal must not live under the kanban home: " + str(d))
    assert "state" in d.parts


def test_a_journal_failure_never_raises(journal_env, monkeypatch):
    """A safety net that can break the thing it protects is a liability."""
    kanban_journal, jdir = journal_env
    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", "/proc/nonexistent-cannot-write")
    assert kanban_journal.append("b1", "t_a", "created", {}) is False
    health = kanban_journal.journal_health()
    assert health["failed"] >= 1
    assert health["last_error"]


def test_off_switch(journal_env, monkeypatch):
    kanban_journal, jdir = journal_env
    monkeypatch.setenv("HERMES_KANBAN_JOURNAL", "off")
    assert kanban_journal.append("b1", "t_a", "created", {}) is False
    assert not (jdir / "b1.jsonl").exists()


def test_reader_skips_a_torn_trailing_line(journal_env):
    """A crash mid-write leaves a partial line; that is expected, not corruption."""
    kanban_journal, jdir = journal_env
    kanban_journal.append("b1", "t_a", "created", {"n": 1})
    path = jdir / "b1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1.0, "kind": "trunc')  # torn, no newline
    entries = list(kanban_journal.read_entries("b1"))
    assert len(entries) == 1
    assert entries[0]["payload"] == {"n": 1}


def test_append_is_concurrency_safe(journal_env):
    """O_APPEND: separate writers interleave whole lines, never shred each other."""
    import threading

    kanban_journal, jdir = journal_env
    errors = []

    def writer(n):
        try:
            for i in range(25):
                kanban_journal.append("b1", "t_" + str(n), "tick", {"i": i})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    lines = (jdir / "b1.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 100
    for line in lines:
        json.loads(line)  # every line is whole


# ---------------------------------------------------------------- replay


def _make_board_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT,
                            created_at INTEGER);
        CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id TEXT, author TEXT, body TEXT, run_id INTEGER,
                            session_ref TEXT, created_at INTEGER);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            task_id TEXT, run_id INTEGER, kind TEXT,
                            payload TEXT, created_at INTEGER);
        """
    )
    conn.commit()
    return conn


def test_incident_replay_recovers_the_comment_threads(journal_env, tmp_path):
    """The 2026-09-21 loss shape, end to end.

    Snapshot -> keep working (comments!) -> board destroyed -> restore snapshot
    + replay journal -> the post-snapshot comment BODIES are back. In the real
    incident these threads were the only unrecoverable part.
    """
    kanban_journal, _ = journal_env
    from hermes_cli import kanban_journal_replay

    live = tmp_path / "live.db"
    conn = _make_board_db(live)
    base = int(time.time()) - 1000
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?)",
        ("t_8388f4f6", "PR #827 review", "running", base),
    )
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("t_8388f4f6", "apollo", "pre-snapshot note", base + 1),
    )
    conn.commit()

    # --- the snapshot the hourly backup would have taken
    snapshot = tmp_path / "snapshot.db"
    snapshot.write_bytes(live.read_bytes())
    snap_watermark = base + 1

    # --- work continues AFTER the snapshot, and is journaled
    later = snap_watermark + 10
    lost_threads = [
        ("apollo", "review round 1: the fallback is wrong", later),
        ("daedalus", "agreed, re-pinned the oracle", later + 5),
        ("ace", "ship it", later + 9),
    ]
    for author, body, when in lost_threads:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t_8388f4f6", author, body, when),
        )
        kanban_journal.append(
            "subs-ace", "t_8388f4f6", "comment_body",
            {"author": author, "body": body, "created_at": when},
        )
    kanban_journal.append(
        "subs-ace", "t_8388f4f6", "status_changed",
        {"to": "completed"},
    )
    conn.commit()
    conn.close()

    # --- THE WIPE
    live.unlink()
    assert not live.exists()

    # --- recovery: snapshot + journal
    recovered = tmp_path / "recovered.db"
    result = kanban_journal_replay.replay_into("subs-ace", snapshot, recovered)

    assert result.comments_restored == 3, result.as_dict()
    assert result.events_restored == 1, result.as_dict()
    assert result.orphan_events == 0

    rc = sqlite3.connect(str(recovered))
    bodies = [r[0] for r in rc.execute(
        "SELECT body FROM task_comments ORDER BY created_at").fetchall()]
    rc.close()
    assert bodies == [
        "pre-snapshot note",
        "review round 1: the fallback is wrong",
        "agreed, re-pinned the oracle",
        "ship it",
    ]


def test_replay_is_idempotent(journal_env, tmp_path):
    kanban_journal, _ = journal_env
    from hermes_cli import kanban_journal_replay

    snapshot = tmp_path / "snap.db"
    conn = _make_board_db(snapshot)
    base = int(time.time()) - 500
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("t_x", "t", "running", base))
    conn.commit()
    conn.close()

    kanban_journal.append(
        "b2", "t_x", "comment_body",
        {"author": "ace", "body": "only once", "created_at": base + 50},
    )

    out = tmp_path / "out.db"
    first = kanban_journal_replay.replay_into("b2", snapshot, out)
    second = kanban_journal_replay.replay_into("b2", snapshot, out)
    assert first.comments_restored == 1
    assert second.comments_restored == 1  # fresh copy of the snapshot each time

    rc = sqlite3.connect(str(out))
    n = rc.execute("SELECT count(*) FROM task_comments").fetchone()[0]
    rc.close()
    assert n == 1, "replaying twice must not duplicate a comment"


def test_replay_reports_orphans_instead_of_inventing_tasks(journal_env, tmp_path):
    """An event does not carry a full task row; a fabricated one is worse than a gap."""
    kanban_journal, _ = journal_env
    from hermes_cli import kanban_journal_replay

    snapshot = tmp_path / "snap.db"
    conn = _make_board_db(snapshot)
    conn.commit()
    conn.close()

    kanban_journal.append(
        "b3", "t_never_seen", "comment_body",
        {"author": "ace", "body": "orphan", "created_at": int(time.time())},
    )
    out = tmp_path / "out.db"
    result = kanban_journal_replay.replay_into("b3", snapshot, out)
    assert result.orphan_events == 1
    assert result.orphan_task_ids == {"t_never_seen"}
    assert result.comments_restored == 0

    rc = sqlite3.connect(str(out))
    assert rc.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    rc.close()


def test_replay_skips_entries_already_in_the_snapshot(journal_env, tmp_path):
    kanban_journal, _ = journal_env
    from hermes_cli import kanban_journal_replay

    snapshot = tmp_path / "snap.db"
    conn = _make_board_db(snapshot)
    base = int(time.time()) - 200
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("t_y", "t", "running", base))
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?,?,?,?)", ("t_y", "ace", "already here", base + 10))
    conn.commit()
    conn.close()

    # Journaled BEFORE the snapshot watermark -> must not be replayed again.
    kanban_journal.append(
        "b4", "t_y", "comment_body",
        {"author": "ace", "body": "already here", "created_at": base + 10},
    )
    out = tmp_path / "out.db"
    result = kanban_journal_replay.replay_into("b4", snapshot, out, since=base + 10)
    assert result.comments_restored == 0

    rc = sqlite3.connect(str(out))
    n = rc.execute("SELECT count(*) FROM task_comments").fetchone()[0]
    rc.close()
    assert n == 1
