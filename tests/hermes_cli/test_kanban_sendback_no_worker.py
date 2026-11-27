"""Send-backs release operator-only flags; stale flags remain observable."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect() as conn:
        yield conn


@pytest.mark.parametrize("sendback", [False, True])
def test_sendback_releases_no_worker(board, sendback):
    tid = kb.create_task(board, title="rework", assignee="builder")
    run = kb.claim_task(board, tid)
    assert kb.request_review(board, tid, summary="ready", reviewer="human",
                             expected_run_id=run.current_run_id)
    assert kb.set_no_worker(board, tid, True)
    if sendback:
        ok, detail = kb.request_sendback(board, tid, payload={
            "v": 1, "key": "example/repo#1@" + "a" * 40 + ":ci-red:test",
            "repo": "example/repo", "pr": 1, "head": "a" * 40,
            "source": "ci-red", "reason": "ci-red:test", "retry": "after_change",
            "summary": "fix the failing test", "evidence": {}, "round": 1,
        })
    else:
        import json
        from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES

        ok, detail = kb.request_changes(board, tid, reason="rework", claimer="apollo",
            coverage=json.dumps({"verdict": "CHANGES_REQUESTED", "head_sha": "a" * 40,
                "lenses": {k: "done" for k in REQUIRED_REVIEW_LENSES},
                "findings": 1, "items": ["fix the test"], "review_minutes": 4,
                "batch_id": "no-worker-regression"}))
    assert ok, detail
    task = kb.get_task(board, tid)
    assert (task.status, task.assignee, task.no_worker) == ("ready", "builder", False)
    events = [e for e in kb.list_events(board, tid) if e.kind == "no_worker_cleared"]
    assert len(events) == 1
    assert events[0].payload["source"] == ("sendback" if sendback else "request-changes")
    preview = dispatch.dispatch_once(board, dry_run=True)
    assert tid not in preview.skipped_no_worker
    assert kb.claim_task(board, tid) is not None


_SENDBACK = {
    "v": 1, "key": "example/repo#1@" + "a" * 40 + ":ci-red:test",
    "repo": "example/repo", "pr": 1, "head": "a" * 40,
    "source": "ci-red", "reason": "ci-red:test", "retry": "after_change",
    "summary": "fix the failing test", "evidence": {}, "round": 1,
}


def test_ready_sendback_without_reassignment_keeps_operator_hold(board):
    """Prism 3e4739f2dcfa: a sendback that leaves the assignee alone is not a release."""
    tid = kb.create_task(board, title="held", assignee="builder")
    assert kb.set_no_worker(board, tid, True)
    assert kb.request_sendback(board, tid, payload=dict(_SENDBACK)) == (True, "requeued")
    task = kb.get_task(board, tid)
    assert (task.status, task.assignee, task.no_worker) == ("ready", "builder", True)
    assert not [e for e in kb.list_events(board, tid) if e.kind == "no_worker_cleared"]


def test_demoted_sendback_that_restores_worker_releases_hold(board):
    """Control: a todo sendback that moves the card from its reviewer back to the worker releases."""
    parent = kb.create_task(board, title="parent", assignee="planner")
    assert kb.complete_task(board, parent, result="done")
    tid = kb.create_task(board, title="demoted", assignee="builder", parents=[parent])
    run = kb.claim_task(board, tid)
    assert kb.request_review(board, tid, summary="ready", reviewer="argus",
                             expected_run_id=run.current_run_id)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
    assert kb.claim_review_task(board, tid) is None
    assert kb.get_task(board, tid).assignee == "argus"
    assert kb.set_no_worker(board, tid, True)
    assert kb.request_sendback(board, tid, payload=dict(_SENDBACK)) == (True, "requeued")
    task = kb.get_task(board, tid)
    assert (task.assignee, task.no_worker) == ("builder", False)


@pytest.mark.parametrize("target", ["human", None])
def test_composed_sendback_preserves_nonworker_hold(board, target):
    tid = kb.create_task(board, title="operator rework", assignee="builder")
    run = kb.claim_task(board, tid)
    assert kb.request_review(board, tid, summary="ready", reviewer="human",
                             expected_run_id=run.current_run_id)
    assert kb.set_no_worker(board, tid, True)
    with kb.event_actor("apollo"):
        assert kb.assign_task(board, tid, target, operator="apollo",
                              request_changes_reason="operator will handle it")
    task = kb.get_task(board, tid)
    assert task.assignee == target
    assert task.no_worker
    assert not [e for e in kb.list_events(board, tid) if e.kind == "no_worker_cleared"]


def test_stranded_flag_is_stale_and_once_per_episode(board):
    tid = kb.create_task(board, title="stranded", assignee="builder")
    assert kb.set_no_worker(board, tid, True)
    now = board.execute("SELECT created_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    from hermes_cli.kanban_no_worker import record_stranded

    assert record_stranded(board, now=now + 1800) == []
    assert record_stranded(board, now=now + 1801) == [tid]
    assert record_stranded(board, now=now + 3600) == []
    assert len([e for e in kb.list_events(board, tid) if e.kind == "stranded_no_worker"]) == 1
