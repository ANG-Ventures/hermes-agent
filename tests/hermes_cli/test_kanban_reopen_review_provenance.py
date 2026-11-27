"""Reopening an existing candidate must leave a send-back-capable review handoff."""
from argparse import Namespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "human:apollo")
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def test_existing_candidate_reopen_can_be_sent_back(board):
    tid = kb.create_task(board, title="candidate", assignee="dev")
    claimed = kb.claim_task(board, tid)
    assert claimed is not None
    meta = {"pr_url": "https://github.com/example/repo/pull/44", "head_sha": "a" * 40}
    # The original worker already finished; reopening must not spawn another one.
    with kb.write_txn(board):
        kb._end_run(board, tid, outcome="completed", status="done", metadata=meta)
        board.execute("UPDATE tasks SET status='done', claim_lock=NULL WHERE id=?", (tid,))
    assert kb.reopen_task(board, tid, actor="operator", reason="candidate still open",
                          to_status="review") == (True, None)
    events = [e for e in kb.list_events(board, tid) if e.kind == "review_requested"]
    assert events, "review entry must record implementer provenance"
    payload = events[-1].payload
    assert payload is not None
    assert payload["implementer"] == "dev"
    assert payload["implementer_run_id"] == claimed.current_run_id
    assert payload["pr_url"] == meta["pr_url"]
    assert payload["head_sha"] == meta["head_sha"]
    assert kb.request_sendback(board, tid, payload={
        "v": 1, "key": "example/repo#44@" + "a" * 40 + ":ci-red:lint",
        "repo": "example/repo", "pr": 44, "head": "a" * 40,
        "source": "ci-red", "reason": "ci-red:lint", "retry": "after_change",
        "summary": "lint failed", "evidence": {}, "round": 1,
    }) == (True, "requeued")
    task = kb.get_task(board, tid)
    assert (task.status, task.assignee) == ("ready", "dev")
    # A candidate with no worker binds the operator-selected implementer.
    other = kb.create_task(board, title="operator candidate", assignee="dev2")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (other,))
    assert kb.reopen_task(board, other, actor="operator", reason="review candidate",
                          to_status="review") == (True, None)
    event = [e for e in kb.list_events(board, other) if e.kind == "review_requested"][-1]
    assert event.payload["implementer"] == "dev2"
    assert event.payload["provenance"] == "operator-bound"


@pytest.mark.parametrize("metadata, expected_identity", [
    ({"pr_url": "https://github.com/example/repo/pull/45"},
     {"pr_url": "https://github.com/example/repo/pull/45"}),
    ({"head_sha": "b" * 40}, {"head_sha": "b" * 40}),
    ({}, {"pr_url": "https://github.com/example/repo/pull/44", "head_sha": "a" * 40}),
])
def test_reopen_binds_candidate_run_without_mixing_review_identity(board, metadata, expected_identity):
    tid = kb.create_task(board, title="new candidate", assignee="devB")
    claimed = kb.claim_task(board, tid)
    with kb.write_txn(board):
        kb._append_event(board, tid, "review_requested", {
            "implementer": "devA", "reviewer": "human:apollo",
            "pr_url": "https://github.com/example/repo/pull/44", "head_sha": "a" * 40,
        })
        kb._end_run(board, tid, outcome="completed", status="done", metadata=metadata)
        board.execute("UPDATE tasks SET status='done', claim_lock=NULL WHERE id=?", (tid,))
    assert kb.reopen_task(board, tid, actor="operator", reason="new candidate",
                          to_status="review") == (True, None)
    event = [e for e in kb.list_events(board, tid) if e.kind == "review_requested"][-1]
    assert event.payload["implementer"] == claimed.assignee
    assert event.payload["implementer_run_id"] == claimed.current_run_id
    assert {k: event.payload[k] for k in ("pr_url", "head_sha") if k in event.payload} == expected_identity


@pytest.mark.parametrize("run_profile", ["dev", "human:apollo"])
def test_reopen_retains_effective_reviewer_and_valid_implementer(board, monkeypatch, run_profile):
    tid = kb.create_task(board, title="retained reviewer", assignee="dev")
    claimed = kb.claim_task(board, tid)
    with kb.write_txn(board):
        kb._append_event(board, tid, "review_requested", {
            "implementer": "dev", "reviewer": "human:apollo",
        })
        kb._end_run(board, tid, outcome="completed", status="done")
        board.execute("UPDATE task_runs SET profile=? WHERE id=?", (run_profile, claimed.current_run_id))
        board.execute("UPDATE tasks SET status='done', assignee='human:apollo', claim_lock=NULL WHERE id=?", (tid,))
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: None)
    assert kb.reopen_task(board, tid, actor="operator", reason="retain reviewer",
                          to_status="review") == (True, None)
    event = [e for e in kb.list_events(board, tid) if e.kind == "review_requested"][-1]
    assert event.payload["reviewer"] == kb.get_task(board, tid).assignee == "human:apollo"
    assert event.payload["implementer"] == "dev"


def test_missing_review_handoff_is_an_integrity_finding(board, monkeypatch, capsys):
    from hermes_cli import kanban
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(board, title="legacy malformed review", assignee="dev")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        # Model pre-guard corruption; current entry writers repair the handoff.
        board.execute("DELETE FROM task_events WHERE task_id=? AND kind='review_requested'", (tid,))
    task = kb.get_task(board, tid)
    findings = compute_task_diagnostics(task, kb.list_events(board, tid), [])
    assert any(d.kind == "review_missing_handoff" for d in findings)
    # Integrity is reported by show/the dedicated failsafe, not the age-based
    # reviewer alert: a fresh spawnable review must remain eligible for dispatch.
    assert kb.review_awaiting_human(board, stale_minutes=999999) == []
    assert kanban._cmd_show(Namespace(task_id=tid, json=True)) == 0
    import json
    shown = json.loads(capsys.readouterr().out)
    assert any(d["kind"] == "review_missing_handoff" for d in shown["diagnostics"])
