"""A refused ``request-changes --coverage`` leaves no review_coverage record (t_c5bfb48b).

21:15 on 2026-10-03 (t_768c9e91): Apollo held the review run (``claim --review``)
and sent back with ``--coverage`` whose ``execution`` lens was a bare ``n/a``.
The CLI committed the ``review_coverage: {"verdict":"CHANGES_REQUESTED",...}``
comment first, then the coverage gate refused the call. The lander read that
orphan comment as the deciding record at the head and refused the merge.

Contract: the whole coverage JSON is validated before any DB write, and a
failed call leaves zero new ``task_comments`` rows, so the card's prior APPROVE
stays the newest record at that head.
"""
import json
import shlex
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES

SESSION = "20261003_211500_operator"
HEAD = "1ee13c3d39"
TAKEOVER = '--takeover "probe: validate-first"'


def _coverage(**over) -> str:
    record = {
        "verdict": "CHANGES_REQUESTED", "head_sha": HEAD,
        "lenses": {k: "done" for k in LENSES},
        "findings": 1, "items": ["land the dependency PR first"],
        "review_minutes": 4, "batch_id": "apollo-2125",
    }
    record.update(over)
    return json.dumps(record)


# The incident's shape: a lens state of bare "n/a" (no applicability reason).
BAD_LENS = _coverage(lenses={**{k: "done" for k in LENSES}, "execution": "n/a"})
BAD_RECORDS = {
    "bad_lens": BAD_LENS,
    "missing_lens": _coverage(lenses={k: "done" for k in LENSES if k != "mutation"}),
    "zero_findings": _coverage(findings=0, items=[]),
    "not_json": "{verdict: CHANGES_REQUESTED",
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    import os
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    for key in ("HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "_HERMES_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="builder", session_id=SESSION)
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                                 expected_run_id=impl.current_run_id)
        # The prior APPROVE at the head (an earlier review round).
        kb.add_comment(conn, tid, "apollo", "review_coverage: " + json.dumps(
            {"verdict": "APPROVE", "head_sha": HEAD}))
    return tid


def _comments(tid):
    with kb.connect() as conn:
        return [(c.id, c.body) for c in kb.list_comments(conn, tid)]


def _newest_record(tid):
    """The newest review_coverage record, as review_of_record reads it."""
    for _cid, body in reversed(_comments(tid)):
        for line in body.splitlines():
            payload = kb._review_coverage_payload(line)
            if payload is not None:
                return json.loads(payload)
    return None


def _claim(monkeypatch, tid):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    assert "Claimed" in cli.run_slash(f"claim {tid} --review")
    with kb.connect() as conn:
        return kb.get_task(conn, tid).current_run_id


@pytest.mark.parametrize("name", sorted(BAD_RECORDS))
def test_held_review_run_bad_coverage_writes_nothing(board, monkeypatch, name):
    """The incident path: the caller holds the run via ``claim --review``."""
    run_id = _claim(monkeypatch, board)
    before = _comments(board)
    out = cli.run_slash(
        f'request-changes {board} "land #1695" --coverage {shlex.quote(BAD_RECORDS[name])}'
    )
    assert "cannot request changes" in out, out
    assert _comments(board) == before, "a refused send-back must leave zero new comments"
    assert _newest_record(board) == {"verdict": "APPROVE", "head_sha": HEAD}
    with kb.connect() as conn:
        task = kb.get_task(conn, board)
    assert task.status == "running" and task.current_run_id == run_id


@pytest.mark.parametrize("name", sorted(BAD_RECORDS))
def test_parked_review_bad_coverage_writes_nothing(board, monkeypatch, name):
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    before = _comments(board)
    out = cli.run_slash(
        f'request-changes {board} "land #1695" --coverage {shlex.quote(BAD_RECORDS[name])} {TAKEOVER}'
    )
    assert "cannot request changes" in out, out
    assert _comments(board) == before
    assert _newest_record(board) == {"verdict": "APPROVE", "head_sha": HEAD}
    with kb.connect() as conn:
        task = kb.get_task(conn, board)
        runs = kb.list_runs(conn, board)
    assert task.status == "review" and task.claim_lock is None
    assert all(r.outcome is not None for r in runs), "no orphan review run"


def test_db_request_changes_bad_coverage_refused_before_any_write(board, monkeypatch):
    """The kanban_db primitive (tool surface) refuses before opening a run."""
    monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
    with kb.connect() as conn:
        events_before = len(kb.list_events(conn, board))
        ok, detail = kb.request_changes(
            conn, board, reason="land #1695", claimer="apollo",
            coverage=BAD_LENS, session_ref=kb.derive_session_ref(SESSION),
        )
        assert not ok and "lens execution" in detail, detail
        assert len(kb.list_events(conn, board)) == events_before
    assert _newest_record(board) == {"verdict": "APPROVE", "head_sha": HEAD}


def test_held_review_run_valid_coverage_still_records_and_routes(board, monkeypatch):
    run_id = _claim(monkeypatch, board)
    out = cli.run_slash(
        f'request-changes {board} "land #1695" --coverage {shlex.quote(_coverage())}'
    )
    assert "Requested changes" in out, out
    rec = _newest_record(board)
    assert rec is not None and rec["verdict"] == "CHANGES_REQUESTED"
    with kb.connect() as conn:
        task = kb.get_task(conn, board)
        stamped = [c for c in kb.list_comments(conn, board)
                   if c.body.startswith("review_coverage:") and "apollo-2125" in c.body]
    assert task.status == "ready"
    assert len(stamped) == 1 and stamped[0].run_id == run_id
