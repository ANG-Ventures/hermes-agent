"""A refused coverage send-back journals nothing (t_fc9f7319).

``request_changes`` posting its own ``review_coverage`` used to INSERT the
comment and journal ``commented`` (and, on a parked-review send-back,
``claimed`` from ``_open_review_run``) BEFORE the run-scoped coverage gate
could refuse. SQLite rolled the rows back; the journal lines stayed, and
``kanban_journal_replay`` rebuilt a running card holding a claim_lock.

Contract: a call that posts its own record is judged on that record before
the txn, and nothing after the first journaled write can refuse it. A refused
call leaves the card in ``review`` (or its held run untouched) and zero new
journal lines.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_journal
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES


def _coverage(**over) -> str:
    record = {
        "verdict": "CHANGES_REQUESTED", "head_sha": "1ee13c3d39",
        "lenses": {k: "done" for k in LENSES},
        "findings": 1, "items": ["land the dependency PR first"],
        "review_minutes": 4, "batch_id": "apollo-1530",
    }
    record.update(over)
    return json.dumps(record)


GOOD = _coverage()
BAD = {
    "bad_lens": _coverage(lenses={**{k: "done" for k in LENSES}, "execution": "n/a"}),
    "zero_findings": _coverage(findings=0, items=[]),
    "not_json": "{verdict: CHANGES_REQUESTED",
    "not_object": "[1]",
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    import os

    home = tmp_path / "hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="builder")
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="ready", reviewer="human",
                                 expected_run_id=impl.current_run_id)
        slug = kb._journal_board_slug(conn)
    return tid, slug


def _journal(slug, tid) -> list:
    return [e["kind"] for e in kanban_journal.read_entries(slug) if e.get("task_id") == tid]


def _card(tid):
    with kb.connect() as conn:
        row = conn.execute("SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?",
                           (tid,)).fetchone()
        comments = [c.body for c in kb.list_comments(conn, tid)]
    return tuple(row), comments


@pytest.mark.parametrize("name", sorted(BAD))
def test_parked_send_back_bad_coverage_journals_nothing(board, name):
    tid, slug = board
    before_journal = _journal(slug, tid)
    before_card = _card(tid)
    with kb.connect() as conn:
        ok, detail = kb.request_changes(conn, tid, reason="rework", claimer="apollo",
                                        coverage=BAD[name])
    assert ok is False and detail
    assert _card(tid) == before_card
    assert before_card[0] == ("review", None, None)
    assert _journal(slug, tid)[len(before_journal):] == []


def _refusing_gate(monkeypatch):
    """A run-scoped gate that disagrees with the pre-txn text check (a later
    rule reading prior events, say). Before the fix it refused AFTER the
    comment and ``claimed`` were journaled."""
    calls = []

    def gate(conn, task_id, run_id):
        calls.append(run_id)
        return "refused by the run-scoped gate"

    monkeypatch.setattr(kb, "_validate_review_coverage", gate)
    return calls


def test_parked_send_back_no_refusal_after_claimed_is_journaled(board, monkeypatch):
    tid, slug = board
    calls = _refusing_gate(monkeypatch)
    before = _journal(slug, tid)
    with kb.connect() as conn:
        ok, detail = kb.request_changes(conn, tid, reason="rework", claimer="apollo",
                                        coverage=GOOD)
    new = _journal(slug, tid)[len(before):]
    # The record was judged before the txn; nothing after ``claimed`` refuses,
    # so every journal line belongs to a committed send-back.
    assert (ok, detail, calls) == (True, "builder", []), (ok, detail, new)
    assert new[0] == "claimed" and "changes_requested" in new, new
    assert _card(tid)[0][0] == "ready"


def test_held_run_send_back_no_refusal_after_commented_is_journaled(board, monkeypatch):
    tid, slug = board
    with kb.connect() as conn:
        task = kb.claim_review_task(conn, tid, claimer="apollo")
        run_id = task.current_run_id
    _refusing_gate(monkeypatch)
    before = _journal(slug, tid)
    with kb.connect() as conn:
        ok, detail = kb.request_changes(conn, tid, reason="rework", expected_run_id=run_id,
                                        coverage=GOOD)
    new = _journal(slug, tid)[len(before):]
    assert (ok, detail) == (True, "builder"), (ok, detail, new)
    assert new[0] == "commented" and "changes_requested" in new, new


def test_send_back_without_own_record_still_runs_the_run_scoped_gate(board, monkeypatch):
    """A held run with no ``coverage`` arg relies on a comment posted earlier on
    the run: the run-scoped gate still decides, and its refusal writes nothing."""
    tid, slug = board
    with kb.connect() as conn:
        run_id = kb.claim_review_task(conn, tid, claimer="apollo").current_run_id
    calls = _refusing_gate(monkeypatch)
    before = _journal(slug, tid)
    with kb.connect() as conn:
        ok, detail = kb.request_changes(conn, tid, reason="rework", expected_run_id=run_id)
    assert (ok, detail) == (False, "refused by the run-scoped gate")
    assert calls == [run_id]
    assert _journal(slug, tid)[len(before):] == []
    status, _lock, current = _card(tid)[0]
    assert (status, current) == ("running", run_id)


def test_valid_coverage_records_once_on_the_opened_run(board):
    tid, slug = board
    with kb.connect() as conn:
        ok, detail = kb.request_changes(conn, tid, reason="rework", claimer="apollo",
                                        coverage=GOOD)
        assert (ok, detail) == (True, "builder")
        runs = kb.list_runs(conn, tid)
        stamped = [c for c in kb.list_comments(conn, tid) if "apollo-1530" in c.body]
    assert len(stamped) == 1 and stamped[0].run_id == runs[-1].id
    assert _card(tid)[0][0] == "ready"
