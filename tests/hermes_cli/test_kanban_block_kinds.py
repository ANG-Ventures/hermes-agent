"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking —
  unless no parent is open, in which case the wait can never be satisfied
  and the block is recorded as ``needs_input`` (sticky, loop-counted).
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t", parents=()):
    """Create a task (linked under ``parents`` first) and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    for parent in parents:
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
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
    with kbc.connect_closing() as conn:
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
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(
            conn,
            parent_id=parent,
            child_id=child,
            expected_child_run_id=kb.get_task(conn, child).current_run_id,
        )
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
def test_dependency_without_open_blocking_parent_is_refused(
    kanban_home: Path, parent_state: str,
) -> None:
    """A ``dependency`` block no open ``blocks`` parent can satisfy is refused
    with the two-option message and leaves the card untouched (it used to be
    re-kinded to needs_input silently and paged every 2 h, t_1e0609f4)."""
    with kb.connect_closing() as conn:
        # Build the edge BEFORE the child runs: a running child cannot be gated retroactively
        # (link_tasks rejects it without the owning run id, upstream b95513df7c4).
        parent = None
        if parent_state != "absent":
            parent = _running_task(conn, title="parent")
            if parent_state != "derived-from":
                assert kb.complete_task(conn, parent, result="done")
                if parent_state == "archived":
                    assert kb.archive_task(conn, parent)
        child = kb.create_task(conn, title="waiting on external PR", assignee="worker")
        if parent is not None:
            kb.link_tasks(
                conn, parent_id=parent, child_id=child,
                kind="derived-from" if parent_state == "derived-from" else "blocks",
            )
        _make_running_again(conn, child)
        before = [e.kind for e in kb.list_events(conn, child)]
        with pytest.raises(kb.BlockRefused) as exc:
            kb.block_task(conn, child, reason="PR not merged", kind="dependency")
        assert str(exc.value) == kb.DEPENDENCY_NO_PARENT_REFUSAL
        assert "--kind deferred --until" in str(exc.value) and "--kind needs_input" in str(exc.value)
        task = kb.get_task(conn, child)
        assert (task.status, task.block_kind) == ("running", None)
        assert [e.kind for e in kb.list_events(conn, child)] == before


def test_cli_dependency_without_parent_is_refused_with_both_options(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """`hermes kanban block <id> --kind dependency` with no parent exits 1,
    names both options on stderr, and writes no BLOCKED comment."""
    with kbc.connect_closing() as conn:
        child = _running_task(conn, title="no-parent")
        args = argparse.Namespace(task_id=child, ids=None, reason=["waiting", "on", "upstream"],
                                  kind="dependency", until=None)
        assert kanban_cli._cmd_block(args) == 1
        err = capsys.readouterr().err
        assert kb.DEPENDENCY_NO_PARENT_REFUSAL in err
        assert kb.get_task(conn, child).status == "running"
        assert not [c for c in kb.list_comments(conn, child) if c.body.startswith("BLOCKED:")]


@pytest.mark.parametrize(("kind", "until"), [("deferred", None), ("needs_input", 2_000_000_000)])
def test_deferred_and_until_must_come_together(kanban_home: Path, kind, until) -> None:
    with kb.connect_closing() as conn:
        child = _running_task(conn)
        with pytest.raises(kb.BlockRefused):
            kb.block_task(conn, child, reason="x", kind=kind, until=until)
        assert kb.get_task(conn, child).status == "running"


def test_deferred_block_parks_scheduled_and_wakes_at_until(kanban_home: Path) -> None:
    """``--kind deferred --until T`` parks in ``scheduled`` (never ``blocked``),
    stamps the wake, and the dispatcher's timed-wake pass returns it to
    ``ready`` at T, not before."""
    with kb.connect_closing() as conn:
        child = _running_task(conn, title="deferred")
        now = 1_800_000_000
        until = kb.parse_wake_at("+1m", now=now)
        assert until == now + 60
        assert kb.block_task(conn, child, reason="DEFERRED until the 24h window",
                             kind="deferred", until=until)
        task = kb.get_task(conn, child)
        assert (task.status, task.block_kind, task.next_eligible_at) == ("scheduled", "deferred", until)
        assert task.current_run_id is None
        kinds = [e.kind for e in kb.list_events(conn, child)]
        assert "blocked" not in kinds and "dependency_wait" not in kinds
        parked = [e for e in kb.list_events(conn, child) if e.kind == "scheduled"][-1].payload
        assert (parked["kind"], parked["until"]) == ("deferred", until)
        assert kb.wake_due_scheduled(conn, now=until - 1) == []
        assert kb.get_task(conn, child).status == "scheduled"
        assert kb.wake_due_scheduled(conn, now=until) == [child]
        woke = kb.get_task(conn, child)
        assert (woke.status, woke.next_eligible_at) == ("ready", None)
        assert kb.claim_task(conn, child, claimer="worker") is not None


@pytest.mark.parametrize("status", ["done", "archived", "scheduled", "todo"])
def test_parentless_dependency_on_unblockable_card_returns_false(kanban_home: Path, status) -> None:
    """The refusal only fires where a block could land; elsewhere block_task
    keeps its ``False`` contract instead of raising (Prism 4f545435d98e)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        assert kb.block_task(conn, tid, reason="x", kind="dependency") is False
        assert kb.get_task(conn, tid).status == status


# Nanosecond epoch: fits SQLite's integer range, overflows time.localtime().
_UNRENDERABLE_WAKE = 1_781_100_000_000_000_000


def test_deferred_block_refuses_unrenderable_wake_before_persisting(kanban_home: Path) -> None:
    """A wake ``time.localtime`` cannot render is refused before the park,
    so the card stays running and list/show never meet it (Prism 106d60b244a4)."""
    with kb.connect_closing() as conn:
        child = _running_task(conn)
        with pytest.raises(ValueError):
            kb.parse_wake_at(str(_UNRENDERABLE_WAKE))
        with pytest.raises(kb.BlockRefused):
            kb.block_task(conn, child, reason="x", kind="deferred", until=_UNRENDERABLE_WAKE)
        task = kb.get_task(conn, child)
        assert (task.status, task.next_eligible_at) == ("running", None)
        assert "scheduled" not in [e.kind for e in kb.list_events(conn, child)]


def test_schedule_wake_refuses_unrenderable_wake(kanban_home: Path) -> None:
    """Same class on the ``schedule --at`` writer."""
    with kb.connect_closing() as conn:
        child = _running_task(conn)
        assert kb.schedule_task(conn, child, reason="gate")
        ok, err = kb.set_schedule_wake(conn, child, wake_at=_UNRENDERABLE_WAKE, actor="t")
        assert not ok and "representable" in err
        assert kb.get_task(conn, child).next_eligible_at is None


def test_format_deferred_until_survives_a_stored_unrenderable_wake() -> None:
    """A row written before validation still renders instead of raising."""
    assert str(_UNRENDERABLE_WAKE) in kb.format_deferred_until(_UNRENDERABLE_WAKE)


def _claimed_review(conn) -> str:
    tid = kb.create_task(conn, title="r", assignee="builder")
    worker = kb.claim_task(conn, tid)
    assert worker is not None
    assert kb.request_review(conn, tid, reviewer="argus", expected_run_id=worker.current_run_id)
    assert kb.claim_review_task(conn, tid) is not None
    return tid


@pytest.mark.parametrize("park", ["deferred_block", "schedule"])
def test_parking_a_claimed_review_wakes_back_into_review(kanban_home: Path, park) -> None:
    """A reviewer that parks on time resumes REVIEW at the wake, never an
    implementation run under the reviewer (Prism 2eb3a143f7f5)."""
    with kb.connect_closing() as conn:
        tid = _claimed_review(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        if park == "deferred_block":
            assert kb.block_task(conn, tid, reason="wait", kind="deferred",
                                 until=2_000_000_000, expected_run_id=run_id)
        else:
            assert kb.schedule_task(conn, tid, reason="wait", expected_run_id=run_id)
            ok, _ = kb.set_schedule_wake(conn, tid, wake_at=2_000_000_000, actor="t")
            assert ok
        parked = [e for e in kb.list_events(conn, tid) if e.kind == "scheduled"][-1].payload
        assert parked["source_status"] == "review"
        assert kb.wake_due_scheduled(conn, now=2_000_000_000) == [tid]
        assert kb.get_task(conn, tid).status == "review"


def test_blocked_review_converted_to_deferred_wakes_back_into_review(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _claimed_review(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.block_task(conn, tid, reason="human", kind="needs_input", expected_run_id=run_id)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.block_task(conn, tid, reason="time", kind="deferred", until=2_000_000_000)
        assert kb.wake_due_scheduled(conn, now=2_000_000_000) == [tid]
        assert kb.get_task(conn, tid).status == "review"


def test_dependency_block_with_open_parent_stays_parked_across_dispatch_tick(
    kanban_home: Path, all_assignees_spawnable,
) -> None:
    """Control: a genuine wait on an incomplete parent parks in ``todo``
    without counting a recurrence, survives a dispatch tick unspawned, and
    resumes once the parent finishes."""
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open-parent", assignee="alice")
        # Linked while todo (a running child cannot be gated retroactively); the parent then stays
        # open while the child runs — the reopened-parent shape, forced the same way the loop below does.
        child = kb.create_task(conn, title="waiter", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            assert kb.block_task(conn, child, reason="wait", kind="dependency")
            parked = kb.get_task(conn, child)
            assert (parked.status, parked.block_kind, parked.block_recurrences) == ("todo", "dependency", 0)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        assert kb.block_task(conn, child, reason="wait", kind="dependency")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert kb.get_task(conn, child).status == "todo"
        assert child not in spawns
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="alice")
        kb.complete_task(conn, parent, result="done")
        res2 = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert child in [row[0] for row in res2.spawned]


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


