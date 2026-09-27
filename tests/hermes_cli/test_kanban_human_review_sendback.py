"""The HUMAN review lane can send a review card back to its implementer (t_088fe9e3).

Under ``review_policy=milestone_only`` with ``review_assignee`` resolving to the
human lane, the operator session (Apollo, D-1) reviews via
``hermes kanban claim <id> --review``. Its later ``comment`` and
``request-changes`` calls are separate CLI processes, so the dispatcher env
never attests to the review run. The claim itself is the provenance: the
session that made it is recorded on the ``claimed`` event, and only that
session (never a delegate child, never a cron job) inherits the run id.
"""
import json
import shlex
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES

SESSION = "20260925_150000_operator"
OTHER_SESSION = "20260925_150000_bystander"


def _coverage_json() -> str:
    return json.dumps({
        "lenses": {k: "done" for k in LENSES},
        "findings": 1, "items": ["Missing guard at handler:42"],
        "review_minutes": 5, "batch_id": "batch-human-1",
        "head_sha": "n/a: fixture card has no PR",
    })


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    import os
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT", "_HERMES_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        # Homed to the operator session, like a card Apollo files from chat.
        tid = kb.create_task(conn, title="human review", assignee="builder",
                             session_id=SESSION)
        impl = kb.claim_task(conn, tid)
        assert impl
        assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                                 expected_run_id=impl.current_run_id)
    return tid


def _as_session(monkeypatch, session):
    if session is None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_ID", session)


def _claim(monkeypatch, tid, session=SESSION):
    _as_session(monkeypatch, session)
    assert "Claimed" in cli.run_slash(f"claim {tid} --review")
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "running"
    return task.current_run_id


# Refusal probes pass --takeover so the HOME-SESSION guard (a separate gate)
# is satisfied and the review-claim gate is the one under test.
TAKEOVER = '--takeover "probe: review-claim gate"'


def _status(tid):
    with kb.connect() as conn:
        return kb.get_task(conn, tid).status


def test_cli_claim_coverage_comment_then_request_changes_routes_to_ready(board, monkeypatch):
    run_id = _claim(monkeypatch, board)
    cli.run_slash(f"comment {board} {shlex.quote('review_coverage: ' + _coverage_json())}")
    with kb.connect() as conn:
        stamped = [c for c in kb.list_comments(conn, board) if "review_coverage:" in c.body]
    assert stamped and stamped[-1].run_id == run_id

    out = cli.run_slash(f'request-changes {board} "BEHAVIOUR: add the missing guard"')
    assert "Requested changes" in out, out
    with kb.connect() as conn:
        task = kb.get_task(conn, board)
        assert task.status == "ready"
        assert task.assignee == "builder"
        events = [e for e in kb.list_events(conn, board) if e.kind == "changes_requested"]
        assert events and events[-1].payload["reason"] == "BEHAVIOUR: add the missing guard"
        rework = [c for c in kb.list_comments(conn, board)
                  if "BEHAVIOUR: add the missing guard" in c.body]
        assert rework, "reason must land as the implementer's rework comment"


def test_request_changes_coverage_flag_works_for_claim_holder(board, monkeypatch):
    run_id = _claim(monkeypatch, board)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())}'
    )
    assert "Requested changes" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, board).status == "ready"
        assert any(c.run_id == run_id and "batch-human-1" in c.body
                   for c in kb.list_comments(conn, board))


def test_session_without_the_claim_is_refused(board, monkeypatch):
    _claim(monkeypatch, board, SESSION)
    _as_session(monkeypatch, OTHER_SESSION)
    cli.run_slash(f"comment {board} {shlex.quote('review_coverage: ' + _coverage_json())}")
    with kb.connect() as conn:
        assert all(c.run_id is None for c in kb.list_comments(conn, board))
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "cannot request changes" in out and "claim" in out, out
    assert _status(board) == "running"
    with kb.connect() as conn:
        assert all(c.run_id is None for c in kb.list_comments(conn, board))


def test_sessionless_claim_is_not_bound_and_refused(board, monkeypatch):
    _claim(monkeypatch, board, None)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "cannot request changes" in out, out
    assert _status(board) == "running"


def test_delegate_child_caller_is_refused_even_with_the_claiming_session(board, monkeypatch):
    _claim(monkeypatch, board, SESSION)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")  # DELEGATED_CHILD_ENV_MARKER
    cli.run_slash(f"comment {board} {shlex.quote('review_coverage: ' + _coverage_json())}")
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "Requested changes" not in out, out
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    assert _status(board) == "running"
    with kb.connect() as conn:
        assert all(c.run_id is None for c in kb.list_comments(conn, board))


def test_delegate_child_cannot_inherit_the_run_via_the_resolver(board, monkeypatch):
    """Direct gate check: the DB-level connect refusal must not be the only wall."""
    run_id = _claim(monkeypatch, board, SESSION)
    with kb.connect() as conn:
        assert cli._operator_review_run_id(conn, board) == run_id
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
        assert cli._operator_review_run_id(conn, board) is None

def test_cron_session_cannot_bind_a_review_claim(board, monkeypatch):
    cron = "cron_abc123_20260925_150000"
    _claim(monkeypatch, board, cron)
    out = cli.run_slash(
        f'request-changes {board} "BEHAVIOUR: fix guard" --coverage {shlex.quote(_coverage_json())} {TAKEOVER}'
    )
    assert "cannot request changes" in out, out
    assert _status(board) == "running"


def test_claim_event_records_session_ref_not_raw_session_id(board, monkeypatch):
    run_id = _claim(monkeypatch, board, SESSION)
    with kb.connect() as conn:
        claimed = [e for e in kb.list_events(conn, board)
                   if e.kind == "claimed" and e.run_id == run_id]
    assert claimed[-1].payload.get("session_ref") == kb.derive_session_ref(SESSION)
    assert SESSION not in json.dumps(claimed[-1].payload)
