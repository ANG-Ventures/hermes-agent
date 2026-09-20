"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------










def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        assert kb.get_task(conn, child).block_recurrences == 0
        for _ in range(5):
            kb.recompute_ready(conn)
            assert kb.get_task(conn, child).status == "todo"
        assert not any(e.kind == "blocked" for e in kb.list_events(conn, child))
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, child).block_recurrences == 0


@pytest.mark.parametrize("parent_state", ["done", "archived", "absent", "derived-from"])
def test_dependency_without_open_blocking_parent_stays_blocked(
    kanban_home: Path, parent_state: str,
) -> None:
    """An external wait must not re-spawn on every dispatcher tick."""
    with kb.connect_closing() as conn:
        child = _running_task(conn, title="waiting on external PR")
        if parent_state != "absent":
            parent = _running_task(conn, title="parent")
            kb.link_tasks(
                conn, parent_id=parent, child_id=child,
                kind="derived-from" if parent_state == "derived-from" else "blocks",
            )
            if parent_state != "derived-from":
                assert kb.complete_task(conn, parent, result="done")
                if parent_state == "archived":
                    assert kb.archive_task(conn, parent)
        before = [e.kind for e in kb.list_events(conn, child)]
        for _ in range(10):
            kb.block_task(conn, child, reason="PR not merged", kind="dependency")
            kb.recompute_ready(conn)
            # Exercise the real claim gate, without launching a worker process.
            assert kb.claim_task(conn, child, claimer="worker") is None
        task = kb.get_task(conn, child)
        assert task.status == "blocked"
        assert task.block_kind == "dependency"
        assert task.block_recurrences == 1
        after = [e.kind for e in kb.list_events(conn, child)]
        assert after.count("promoted") == before.count("promoted")
        assert after.count("claimed") == before.count("claimed")
        assert after.count("blocked") == 1


def test_dependency_without_parent_escalates_after_manual_unblocks(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        child = _running_task(conn)
        for attempt in range(1, kb.BLOCK_RECURRENCE_LIMIT + 1):
            assert kb.block_task(conn, child, reason="external wait", kind="dependency")
            task = kb.get_task(conn, child)
            assert task.block_recurrences == attempt
            if attempt < kb.BLOCK_RECURRENCE_LIMIT:
                assert task.status == "blocked"
                assert kb.unblock_task(conn, child)
                _make_running_again(conn, child)
        assert task.status == "triage"
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, child, claimer="worker") is None


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


