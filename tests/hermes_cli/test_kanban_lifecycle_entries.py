"""Lifecycle entry contracts apply to raw writers as well as kernel verbs."""
import json
import sqlite3
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_lifecycle_guards import install


@pytest.fixture
def conn(tmp_path: Path):
    with kb.connect_closing(tmp_path / "kanban.db") as db:
        yield db


def test_current_guard_install_is_read_only_across_connections(conn):
    conn.commit()
    with sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2]) as fresh:
        statements = []
        fresh.set_trace_callback(statements.append)
        fresh.execute("PRAGMA query_only=ON")
        install(fresh)
        assert not any(sql.lstrip().upper().startswith(("CREATE", "DROP", "ALTER"))
                       for sql in statements)
        assert not fresh.in_transaction


@pytest.mark.parametrize("damage", ["missing", "changed", "create_denied"])
def test_guard_install_repairs_definitions_without_committing(conn, damage):
    expected = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    names = [name for name in expected if name.startswith((
        "tasks_scheduled_wake_", "tasks_review_handoff", "tasks_human_ready_"))]
    conn.commit()
    conn.execute("BEGIN")
    for name in names:
        conn.execute(f'DROP TRIGGER "{name}"')
        if damage != "missing":
            conn.execute(f'CREATE TRIGGER "{name}" AFTER INSERT ON tasks BEGIN SELECT 1; END')
    damaged = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    if damage == "create_denied":
        conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                install(conn)
        finally:
            conn.set_authorizer(None)
        assert dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")) == damaged
    else:
        install(conn)
        assert dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")) == expected
        statements = []
        conn.set_trace_callback(statements.append)
        install(conn)
        conn.set_trace_callback(None)
        assert not any(sql.lstrip().upper().startswith(("CREATE", "DROP", "ALTER"))
                       for sql in statements)
    assert conn.in_transaction
    conn.rollback()
    assert dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")) == expected


@pytest.mark.parametrize("intervening", [False, True])
@pytest.mark.parametrize("current_run", [False, True])
def test_review_entry_binds_implementer_before_any_exit(conn, current_run, intervening):
    tid = kb.create_task(conn, title="Recovered review", assignee="builder")
    if current_run:
        conn.execute("INSERT INTO task_runs(task_id,profile,status,outcome,started_at) VALUES(?,'old-builder','completed','completed',1)", (tid,))
        kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET status='review',assignee='human:apollo' WHERE id=?", (tid,))
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert event is not None
    assert json.loads(event[0])["implementer"] == "builder"
    if intervening:
        kb._append_event(conn, tid, "unrelated", {"note": "between entry and handoff"})
    kb._append_event(conn, tid, "review_requested", {"implementer": "builder", "reviewer": "human:apollo", "summary": "verified"})
    handoffs = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested'", (tid,)).fetchall()
    assert len(handoffs) == 1
    assert json.loads(handoffs[0][0])["summary"] == "verified"

    # A second raw entry must bind a new episode even though the first has
    # complete provenance. An unrelated event must not split either episode.
    conn.execute("UPDATE tasks SET status='running',assignee='builder' WHERE id=?", (tid,))
    conn.execute("UPDATE tasks SET status='review',assignee='human:apollo' WHERE id=?", (tid,))
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='review_requested'", (tid,)).fetchone()[0] == 2
    if intervening:
        kb._append_event(conn, tid, "unrelated")
    kb._append_event(conn, tid, "review_requested", {"summary": "round 2"})
    handoffs = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id", (tid,)).fetchall()
    assert len(handoffs) == 2
    assert [json.loads(row[0])["summary"] for row in handoffs] == ["verified", "round 2"]
    assert json.loads(handoffs[0][0])["review_exit_id"] != json.loads(handoffs[1][0])["review_exit_id"]

    # A new raw INSERT has no prior run/assignee to recover.
    conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('raw-review','Raw review','review',1)")
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id='raw-review' AND kind='review_requested'").fetchone()[0]
    assert json.loads(payload)["implementer"] == "human:operator"
    kb._append_event(conn, "raw-review", "review_requested", {"summary": "verified"})
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id='raw-review' AND kind='review_requested'").fetchone()[0]
    assert json.loads(payload)["implementer"] == "human:operator"


def test_review_run_returning_to_review_keeps_the_implementer(conn):
    """A REVIEW claim released back to review (unbound-claim release, TTL reclaim) is the same episode:
    no repair handoff naming the reviewer as implementer (t_c2a562c9; #177 wrote one, so a later
    request-changes routed the card to ``human`` and parked it in todo)."""
    tid = kb.create_task(conn, title="Review claim released", assignee="builder")
    impl = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                             expected_run_id=impl.current_run_id)
    assert kb.claim_review_task(conn, tid, session_ref=None) is not None
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested' "
                        "ORDER BY id", (tid,)).fetchall()
    assert [json.loads(r[0])["implementer"] for r in rows] == ["builder"]


def test_normal_request_review_has_one_complete_handoff(conn):
    tid = kb.create_task(conn, title="Canonical handoff", assignee="builder")
    assert kb.request_review(conn, tid, summary="verified", reviewer="human:apollo")
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested'", (tid,)).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["implementer"] == "builder"
    assert payload["reviewer"] == "human:apollo"
    assert payload["summary"] == "verified"
    assert "source" not in payload


def test_human_ready_is_operator_todo_and_cannot_promote(conn):
    tid = kb.create_task(conn, title="Operator action", assignee="human:apollo")
    assert kb.get_task(conn, tid).status == "todo"
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "todo"
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.get_task(conn, tid).status == "todo"
    events = conn.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? AND kind='needs_operator'", (tid,),
    ).fetchone()[0]
    assert events == 1
    assert kb.assign_task(conn, tid, "builder")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "ready"
    conn.execute("UPDATE tasks SET status='todo',assignee='human:apollo' WHERE id=?", (tid,))
    kb._append_event(conn, tid, "dependency_wait", {"source_status": "review", "kind": "dependency"})
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "review"


def test_untyped_block_requires_a_recovery_predicate(conn):
    tid = kb.create_task(conn, title="Missing recovery", assignee="builder")
    with pytest.raises(kb.BlockRefused, match="kind|unblock_when"):
        kb.block_task(conn, tid, reason="stuck")
    assert kb.get_task(conn, tid).status == "ready"


def test_legacy_repair_is_dry_run_first_and_idempotent(conn):
    from hermes_cli.kanban_lifecycle_repair import repair

    conn.execute("DROP TRIGGER tasks_human_ready_insert")
    conn.execute("DROP TRIGGER tasks_human_ready_update")
    tid = kb.create_task(conn, title="Legacy operator", assignee="human:apollo")
    conn.commit()
    assert repair(conn)["before"] == 1
    assert kb.get_task(conn, tid).status == "ready"
    report = repair(conn, apply=True)
    assert (report["before"], report["after"]) == (1, 0)
    assert kb.get_task(conn, tid).status == "todo"
    assert repair(conn, apply=True)["before"] == 0
    held = kb.create_task(conn, title="Legacy review hold", assignee="reviewer")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (held,))
    kb._append_event(conn, held, "blocked", {"source_status": "review"})
    conn.commit()
    repair(conn, apply=True)
    assert kb._resume_status_from_events(conn, held) == "review"


@pytest.mark.parametrize("legacy_source", [False, True])
def test_legacy_repair_preserves_creation_dependency_release(conn, legacy_source):
    from hermes_cli.kanban_lifecycle_repair import repair

    parent = kb.create_task(conn, title="Prerequisite", assignee="builder")
    child = kb.create_task(conn, title="Creation hold", assignee="builder",
                           parents=[parent], initial_status="blocked")
    if legacy_source:
        conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='blocked'",
                     (json.dumps({"reason": "created with initial_status=blocked"}), child))
    explicit = kb.create_task(conn, title="Explicit hold", assignee="builder", parents=[parent])
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (explicit,))
    kb._append_event(conn, explicit, "blocked", {"reason": "operator decision"})
    conn.commit()
    repair(conn, apply=True)
    assert kb._latest_block_source(conn, child) == "initial_status"
    assert kb._latest_block_source(conn, explicit) == "explicit"
    assert kb.get_task(conn, child).status == "blocked"
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"
    assert kb.get_task(conn, explicit).status == "blocked"
    assert repair(conn, apply=True)["before"] == 0


def test_operator_todo_card_completes_and_promote_says_why(conn):
    """t_554b6dc1 (2026-11-10): an operator card (human:*) is held in todo by the lifecycle guard, so
    complete must accept todo for it, and promote must not report a promotion the trigger reverts."""
    tid = kb.create_task(conn, title="Operator closeout", assignee="human:apollo")
    assert kb.get_task(conn, tid).status == "todo"
    ok, why = kb.promote_task(conn, tid, actor="apollo")
    assert not ok and "operator card stays 'todo' by design" in why
    assert kb.get_task(conn, tid).status == "todo"
    assert not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='promoted_manual'", (tid,)).fetchone()
    assert kb.complete_task(conn, tid, result="merged and deployed", summary="merged and deployed")
    assert kb.get_task(conn, tid).status == "done"


def test_operator_todo_card_with_an_open_parent_still_refuses_complete(conn):
    parent = kb.create_task(conn, title="Parent", assignee="builder")
    tid = kb.create_task(conn, title="Operator child", assignee="human:apollo", parents=[parent])
    assert kb.get_task(conn, tid).status == "todo"
    assert not kb.complete_task(conn, tid, result="x", summary="x")
    assert kb.get_task(conn, tid).status == "todo"


def test_worker_todo_card_still_refuses_complete(conn):
    parent = kb.create_task(conn, title="Parent", assignee="builder")
    tid = kb.create_task(conn, title="Worker child", assignee="builder", parents=[parent])
    assert kb.get_task(conn, tid).status == "todo"
    assert not kb.complete_task(conn, tid, result="x", summary="x")
    assert "complete needs running/ready/blocked/review" in kb.explain_complete_refusal(conn, tid)
