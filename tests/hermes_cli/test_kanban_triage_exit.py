"""Tests for the ``triage`` column's exit verb and stranded-subtree reporting.

``triage`` is where the unblock-loop breaker in ``block_task`` parks a task
that an automated unblocker has already failed to clear
``BLOCK_RECURRENCE_LIMIT`` times. That escalation is deliberate and correct —
it is the only thing stopping a cron-unblock ↔ worker-re-block spin.

Two defects sat on top of it, both observed on 2026-08-08:

1. **No supported exit.** ``unblock_task`` no-ops on a triaged card,
   ``promote_task`` refuses ("promote only applies to 'todo' or 'blocked'"),
   ``complete_task`` refuses. A status that REQUIRES a human offered the human
   no command, so operators ran raw SQL against a live board.
2. **Silent downstream stranding.** A child is held in ``todo`` until its
   parent is ``done``. One triaged parent therefore freezes an entire subtree
   while ``dispatch`` prints ``Spawned: 0`` and every other bucket is empty —
   byte-identical to an idle board. A deploy card sat idle behind a triaged
   parent and nobody was told.

``triage_resolve_task`` is the exit; ``find_stranded_by_triage`` /
``DispatchResult.stranded_by_triage`` make the stranding loud. The escalation
itself is unchanged, and the tests below pin that too — an "exit" that
auto-requeued would re-arm the exact loop the breaker exists to stop.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # ``kanban_db_path`` honours HERMES_KANBAN_DB above HERMES_HOME (the
    # dispatcher→worker handoff pins it). A test inheriting it from a worker
    # env would write to the REAL board, so clear it explicitly.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _triaged_parent_with_todo_child(conn) -> tuple[str, str]:
    """Build the exact 2026-08-08 shape via the REAL escalation path.

    Deliberately does NOT hand-set ``status='triage'``: driving
    block → unblock → re-block is what proves the fix is anchored to the
    condition the breaker actually produces.
    """
    parent = kb.create_task(conn, title="parent hit the unblock loop", assignee="worker")
    child = kb.create_task(
        conn, title="DEPLOY the fix", parents=[parent], assignee="worker",
    )
    assert kb.get_task(conn, child).status == "todo"

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
    assert kb.claim_task(conn, parent, claimer="worker") is not None
    assert kb.block_task(conn, parent, reason="needs a human", kind="needs_input")
    assert kb.unblock_task(conn, parent)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
    assert kb.block_task(conn, parent, reason="still needs a human", kind="needs_input")

    assert kb.get_task(conn, parent).status == "triage"
    assert kb.get_task(conn, child).status == "todo"
    return parent, child


# ---------------------------------------------------------------------------
# The 2026-08-08 shape: triaged parent + todo child + Spawned: 0
# ---------------------------------------------------------------------------


def test_triaged_parent_strands_child_and_dispatch_names_it(kanban_home: Path) -> None:
    """The regression shape, end to end.

    Pre-fix this tick returned ``spawned == []`` with EVERY bucket empty —
    indistinguishable from "nothing to do". The stranded pair must be named.
    """
    with kb.connect_closing() as conn:
        parent, child = _triaged_parent_with_todo_child(conn)

        res = kb.dispatch_once(
            conn, dry_run=True, spawn_fn=lambda *a, **k: 1,
        )
        assert res.spawned == []
        assert res.stranded_by_triage == [(child, parent)]
        # Guard against the false-negative class directly: a zero-spawn tick
        # on this board must NOT look empty.
        assert any(
            getattr(res, name)
            for name in ("stranded_by_triage",)
        ), "zero-spawn tick reported nothing — the silent-stranding bug is back"


def test_find_stranded_ignores_parents_that_will_clear_themselves(
    kanban_home: Path,
) -> None:
    """Only human-gated parents count.

    A child behind a ``ready``/``running`` parent is waiting on work in
    flight, not on a person — flagging it would train the operator to ignore
    the signal.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="busy parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", parents=[parent], assignee="worker",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        assert kb.find_stranded_by_triage(conn) == []

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (parent,))
        assert kb.find_stranded_by_triage(conn) == [(child, parent)]

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (parent,))
        assert kb.find_stranded_by_triage(conn) == [(child, parent)]


def test_find_stranded_reports_every_child_of_a_triaged_parent(
    kanban_home: Path,
) -> None:
    """One triaged parent freezes the WHOLE subtree — report all of it."""
    with kb.connect_closing() as conn:
        parent, first = _triaged_parent_with_todo_child(conn)
        second = kb.create_task(
            conn, title="sibling deploy", parents=[parent], assignee="worker",
        )
        done_child = kb.create_task(
            conn, title="already finished", parents=[parent], assignee="worker",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (done_child,))

        stranded = kb.find_stranded_by_triage(conn)
        assert sorted(stranded) == sorted([(first, parent), (second, parent)])
        # A terminal child is not stranded — it already landed.
        assert done_child not in [c for c, _p in stranded]


def test_find_stranded_reports_only_the_human_gated_parent(
    kanban_home: Path,
) -> None:
    """A child with mixed parents names the one that actually needs a human.

    Reporting the ``done`` parent too would send the operator to a card that
    has nothing to decide.
    """
    with kb.connect_closing() as conn:
        triaged = kb.create_task(conn, title="triaged", assignee="worker")
        finished = kb.create_task(conn, title="finished", assignee="worker")
        child = kb.create_task(
            conn, title="child", parents=[triaged, finished], assignee="worker",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (triaged,))
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (finished,))

        assert kb.find_stranded_by_triage(conn) == [(child, triaged)]

        # A grandchild behind the (todo) child is waiting on ordinary parent
        # gating, not on a person — it must not be reported.
        grandchild = kb.create_task(
            conn, title="grandchild", parents=[child], assignee="worker",
        )
        stranded = kb.find_stranded_by_triage(conn)
        assert (grandchild, child) not in stranded
        assert stranded == [(child, triaged)]


def test_escalation_records_the_stranded_subtree_at_the_moment_it_happens(
    kanban_home: Path,
) -> None:
    """Requirement D: the escalation must not strand a subtree silently."""
    with kb.connect_closing() as conn:
        parent, child = _triaged_parent_with_todo_child(conn)
        kinds = {e.kind for e in kb.list_events(conn, parent)}
        assert "block_loop_detected" in kinds, "escalation semantics changed"
        assert "triage_stranded_subtree" in kinds
        payload = next(
            e.payload for e in kb.list_events(conn, parent)
            if e.kind == "triage_stranded_subtree"
        )
        assert payload["stranded"] == [child]
        assert payload["count"] == 1


def test_escalation_event_excludes_provenance_only_children(
    kanban_home: Path,
) -> None:
    """The escalation event names only descendants the parent really gates."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="looping parent", assignee="worker")
        blocked_child = kb.create_task(
            conn, title="blocked child", parents=[parent], assignee="worker",
        )
        provenance_child = kb.create_task(
            conn,
            title="provenance child",
            parents=[parent],
            parents_kind="derived-from",
            assignee="worker",
        )
        blocked = kb.get_task(conn, blocked_child)
        provenance = kb.get_task(conn, provenance_child)
        assert blocked is not None and blocked.status == "todo"
        assert provenance is not None and provenance.status == "ready"

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        assert kb.block_task(conn, parent, reason="needs human", kind="needs_input")
        assert kb.unblock_task(conn, parent)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        assert kb.block_task(
            conn, parent, reason="still needs human", kind="needs_input",
        )

        payload = next(
            e.payload for e in kb.list_events(conn, parent)
            if e.kind == "triage_stranded_subtree"
        )
        assert payload == {"stranded": [blocked_child], "count": 1}


def test_escalation_without_children_emits_no_stranding_event(
    kanban_home: Path,
) -> None:
    """No subtree, no warning — the signal must stay meaningful."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="lonely", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="r", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        assert kb.block_task(conn, tid, reason="r", kind="needs_input")

        assert kb.get_task(conn, tid).status == "triage"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "triage_stranded_subtree" not in kinds


# ---------------------------------------------------------------------------
# The exit verb
# ---------------------------------------------------------------------------


def test_the_old_exits_still_refuse_a_triaged_card(kanban_home: Path) -> None:
    """Pins the bug this verb exists for — and that we did NOT widen the others.

    Loosening ``unblock``/``promote``/``complete`` to accept ``triage`` would
    let the same automation that caused the loop clear the escalation.
    """
    with kb.connect_closing() as conn:
        parent, _child = _triaged_parent_with_todo_child(conn)

        assert kb.unblock_task(conn, parent) is False
        ok, err = kb.promote_task(conn, parent, actor="ace", reason="go")
        assert ok is False and "promote only applies" in err
        assert kb.complete_task(conn, parent, result="by hand") is False
        assert kb.get_task(conn, parent).status == "triage"


def test_triage_resolve_to_todo_clears_the_card_and_the_loop_counter(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        parent, child = _triaged_parent_with_todo_child(conn)
        before = kb.get_task(conn, parent)
        assert before.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT
        assert before.block_kind == "needs_input"

        ok, err = kb.triage_resolve_task(
            conn, parent, to="todo", reason="dep landed; re-queue", actor="ace",
        )
        assert (ok, err) == (True, None)

        after = kb.get_task(conn, parent)
        # 'todo' runs through recompute_ready, so a parent-free card lands in
        # 'ready'. What matters is that it left triage and is workable again.
        assert after.status in ("todo", "ready")
        # The counter MUST reset: a human decision starts the task over. Left
        # at the limit, the very next same-cause block bounces it straight
        # back to triage and the human's decision buys nothing.
        assert after.block_recurrences == 0
        assert after.block_kind is None
        assert after.claim_lock is None
        assert kb.get_task(conn, child).status == "todo"


def test_triage_resolve_to_done_unstrands_the_subtree(kanban_home: Path) -> None:
    """The 2026-08-08 remedy: resolving the parent releases the deploy card."""
    with kb.connect_closing() as conn:
        parent, child = _triaged_parent_with_todo_child(conn)
        assert kb.find_stranded_by_triage(conn) == [(child, parent)]

        ok, err = kb.triage_resolve_task(
            conn, parent, to="done", reason="obsolete; the fix shipped elsewhere",
            actor="ace",
        )
        assert (ok, err) == (True, None)
        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, parent).completed_at is not None
        # Child is free now, and the dispatcher says so.
        assert kb.get_task(conn, child).status == "ready"
        assert kb.find_stranded_by_triage(conn) == []
        res = kb.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1)
        assert res.stranded_by_triage == []


def test_triage_resolve_to_archived(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent, child = _triaged_parent_with_todo_child(conn)
        ok, err = kb.triage_resolve_task(
            conn, parent, to="archived", reason="wontfix", actor="ace",
        )
        assert (ok, err) == (True, None)
        assert kb.get_task(conn, parent).status == "archived"
        # ``archived`` parents don't gate children (same as ``done``).
        assert kb.get_task(conn, child).status == "ready"


def test_triage_resolve_refuses_ready_target(kanban_home: Path) -> None:
    """PRESERVE THE ESCALATION: no path from triage straight into the pool.

    ``ready`` would bypass parent gating and hand the card back to the same
    automation that spun it — re-arming the loop the breaker exists to stop.
    """
    with kb.connect_closing() as conn:
        parent, _child = _triaged_parent_with_todo_child(conn)
        ok, err = kb.triage_resolve_task(
            conn, parent, to="ready", reason="just run it",
        )
        assert ok is False
        assert "invalid target" in err
        assert kb.get_task(conn, parent).status == "triage"
        assert "ready" not in kb.TRIAGE_RESOLVE_TARGETS


@pytest.mark.parametrize("bad_reason", ["", "   ", None])
def test_triage_resolve_requires_a_reason(kanban_home: Path, bad_reason) -> None:
    """The audit trail is the point — a blank reason is not a decision."""
    with kb.connect_closing() as conn:
        parent, _child = _triaged_parent_with_todo_child(conn)
        ok, err = kb.triage_resolve_task(
            conn, parent, to="todo", reason=bad_reason,
        )
        assert ok is False
        assert "reason is required" in err
        assert kb.get_task(conn, parent).status == "triage"


def test_triage_resolve_refuses_a_card_that_is_not_in_triage(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="ordinary", assignee="worker")
        ok, err = kb.triage_resolve_task(
            conn, tid, to="done", reason="nope",
        )
        assert ok is False
        assert "triage-resolve only applies to 'triage'" in err

        ok, err = kb.triage_resolve_task(
            conn, "t_nosuchcard", to="done", reason="nope",
        )
        assert ok is False and "not found" in err


def test_triage_resolve_records_who_and_why(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent, _child = _triaged_parent_with_todo_child(conn)
        ok, _ = kb.triage_resolve_task(
            conn, parent, to="todo", reason="talked to the author; retry is fine",
            actor="ace",
        )
        assert ok

        events = [e for e in kb.list_events(conn, parent) if e.kind == "triage_resolved"]
        assert len(events) == 1
        assert events[0].payload == {
            "to": "todo",
            "reason": "talked to the author; retry is fine",
            "actor": "ace",
        }
        bodies = [c.body for c in kb.list_comments(conn, parent)]
        assert any(b.startswith("TRIAGE-RESOLVE -> todo:") for b in bodies)


def test_resolved_card_can_still_re_escalate(kanban_home: Path) -> None:
    """The loop breaker must remain armed after a human resolve.

    Counter reset gives the task a genuine fresh start — it must NOT disarm
    the escalation permanently.
    """
    with kb.connect_closing() as conn:
        parent, _child = _triaged_parent_with_todo_child(conn)
        assert kb.triage_resolve_task(
            conn, parent, to="todo", reason="retry once", actor="ace",
        )[0]

        # Two same-cause blocks again → back to triage.
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
            assert kb.claim_task(conn, parent, claimer="worker") is not None
            kb.block_task(conn, parent, reason="same wall", kind="needs_input")
            if kb.get_task(conn, parent).status == "blocked":
                kb.unblock_task(conn, parent)
        assert kb.get_task(conn, parent).status == "triage"


# ---------------------------------------------------------------------------
# CLI surfaces
# ---------------------------------------------------------------------------


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_cli_triage_resolve_happy_path(kanban_home: Path, capsys) -> None:
    with kb.connect_closing() as conn:
        parent, _child = _triaged_parent_with_todo_child(conn)

    rc = kb_cli._cmd_triage_resolve(
        _args(task_id=parent, to="todo", reason="reviewed; re-queue", json=True),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resolved"] is True
    assert payload["to"] == "todo"
    assert payload["status"] in ("todo", "ready")
    assert payload["error"] is None


def test_cli_triage_resolve_reports_failure(kanban_home: Path, capsys) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="not triaged", assignee="worker")

    rc = kb_cli._cmd_triage_resolve(
        _args(task_id=tid, to="todo", reason="x", json=False),
    )
    assert rc == 1
    assert "cannot triage-resolve" in capsys.readouterr().err


def test_cli_dispatch_report_names_the_stranded_subtree(capsys) -> None:
    """``Spawned: 0`` must never again read as a healthy idle board."""
    kb_cli._print_stranded_by_triage([("t_child1", "t_parent"), ("t_child2", "t_parent")])
    out = capsys.readouterr().out
    assert "STRANDED: 2 task(s)" in out
    assert "1 triaged/blocked parent(s)" in out
    # Naming the ids is the requirement — a bare count sends the operator hunting.
    assert "t_parent" in out and "t_child1" in out and "t_child2" in out
    assert "triage-resolve" in out


def test_cli_dispatch_report_is_silent_when_nothing_is_stranded(capsys) -> None:
    kb_cli._print_stranded_by_triage([])
    assert capsys.readouterr().out == ""


def test_cli_list_banner_surfaces_triage_and_its_victims(capsys) -> None:
    kb_cli._print_triage_banner(["t_parent"], [("t_child", "t_parent")])
    out = capsys.readouterr().out
    assert "TRIAGE: 1 card(s) need a human decision" in out
    assert "t_parent" in out
    assert "stranding 1 downstream task(s)" in out
    assert "t_child" in out


def test_cli_list_banner_is_silent_with_no_triage(capsys) -> None:
    kb_cli._print_triage_banner([], [])
    assert capsys.readouterr().out == ""


def test_cli_stats_flags_the_triage_bucket(kanban_home: Path, capsys) -> None:
    with kb.connect_closing() as conn:
        _triaged_parent_with_todo_child(conn)

    rc = kb_cli._cmd_stats(_args(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    triage_line = next(ln for ln in out.splitlines() if ln.strip().startswith("triage"))
    assert "needs a human" in triage_line
    assert "triage-resolve" in out


def test_cli_stats_does_not_nag_when_triage_is_empty(
    kanban_home: Path, capsys,
) -> None:
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="ordinary", assignee="worker")

    assert kb_cli._cmd_stats(_args(json=False)) == 0
    out = capsys.readouterr().out
    triage_line = next(ln for ln in out.splitlines() if ln.strip().startswith("triage"))
    assert "needs a human" not in triage_line
    assert "triage-resolve" not in out
