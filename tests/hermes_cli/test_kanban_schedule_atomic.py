"""Scheduling must never commit an unwakeable intermediate state."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def test_schedule_requires_wake_and_commits_it_atomically(tmp_path: Path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tid = kb.create_task(conn, title="Timed work", assignee="builder")
        with pytest.raises(ValueError, match="wake"):
            kb.schedule_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.schedule_task(conn, tid, wake_at=2_000_000_000)
        task = kb.get_task(conn, tid)
        assert (task.status, task.next_eligible_at) == ("scheduled", 2_000_000_000)
        assert kb.wake_due_scheduled(conn, now=1_999_999_999) == []
        assert kb.wake_due_scheduled(conn, now=2_000_000_000) == [tid]


def test_raw_writer_cannot_create_or_clear_scheduled_wake(tmp_path: Path):
    import sqlite3

    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tid = kb.create_task(conn, title="Raw writer", assignee="builder")
        with pytest.raises(sqlite3.IntegrityError, match="wake"):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        assert kb.schedule_task(conn, tid, wake_at=2_000_000_000)
        with pytest.raises(sqlite3.IntegrityError, match="wake"):
            conn.execute("UPDATE tasks SET next_eligible_at=NULL WHERE id=?", (tid,))


def test_named_event_is_an_explicit_schedule_exit(tmp_path: Path):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        tid = kb.create_task(conn, title="Parts arrival", assignee="builder",
                             body="unblock_when: event:parts-arrived")
        assert kb.schedule_task(conn, tid)
        assert kb.get_task(conn, tid).next_eligible_at is None
        assert kb.wake_due_scheduled(conn, now=2_000_000_000) == []
