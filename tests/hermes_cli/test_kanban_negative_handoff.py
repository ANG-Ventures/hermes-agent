"""A completion whose handoff SAYS it did not land goes to ``review``, not ``done``.

Incidents 2026-09-28: t_d0aee724 ("NOT DEPLOYED, so nothing was armed or
measured") and t_b6eb2944 ("STOP finding: ... measures nothing") both closed
``done`` through the worker's own ``kanban_complete`` (t_4209baaa).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_negative_handoff as neg
from hermes_cli import kanban_open_pr as op

# Verbatim prefixes of the two real handoffs (task_runs 14103, 14409).
NOT_DEPLOYED = (
    "NOT DEPLOYED, so nothing was armed or measured. claude-pool#146 merged at "
    "14:24Z as 77e0c375, but ~/.hermes/deploy/claude-pool HEAD is still b75a8b46 "
    "(#145), 2 commits behind origin/main, and the bpr relay (pid 96643) has been "
    "running since 08:38Z, before the merge."
)
STOP_FINDING = (
    "STOP finding: I armed admission=observe on sub-vps-21, but it measures "
    "nothing. In 8 minutes the box served 39 legs from background principals and "
    "admission recorded legs_total=0."
)
GOOD = "merged ANG-Ventures/hermes-agent#1441 as a682b93c and deployed; readback green."


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, number: {"state": "MERGED"}))
    kb.init_db()
    with kb.connect() as c:
        yield c


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(kb, "configured_negative_handoff_review", lambda: True)


def _claimed(conn):
    tid = kb.create_task(conn, title="arm + readout", assignee="daedalus")
    kb.claim_task(conn, tid)
    return tid


def _status(conn, tid):
    return conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()


def _kinds(conn, tid):
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


@pytest.mark.parametrize("text,trigger", [
    (NOT_DEPLOYED, "NOT DEPLOYED"),
    (STOP_FINDING, "STOP finding"),
    ("could not reach the box", "could not"),
    ("blocked on bpx#288", "blocked on"),
    ("pre-flight failed; nothing was measured", "nothing was measured"),
])
def test_match_phrase_set(text, trigger):
    assert neg.match([text]) == trigger


def test_match_outcome_partial_and_clean_summary():
    assert neg.match([GOOD], {"outcome": "partial"}) == "outcome=partial"
    assert neg.match([GOOD], {"outcome": "completed"}) is None
    assert neg.match([GOOD, None]) is None


@pytest.mark.parametrize("summary,meta", [
    (NOT_DEPLOYED, {"outcome": "not_deployed", "no_pr": True}),
    (STOP_FINDING, {"verdict": "STOP", "no_pr": True}),
    (GOOD, {"outcome": "partial", "no_pr": True}),
])
def test_negative_handoff_routes_to_review(conn, armed, summary, meta):
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, summary=summary, metadata=meta)
    row = _status(conn, tid)
    assert (row["status"], row["assignee"]) == ("review", neg.REVIEWER)
    assert "completion_routed_negative_handoff" in _kinds(conn, tid)
    assert "completed" not in _kinds(conn, tid)
    comments = [r["body"] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id=?", (tid,))]
    assert any(neg.match([summary], meta) in c for c in comments)


def test_clean_handoff_is_done(conn, armed):
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, summary=GOOD, metadata={"no_pr": True})
    assert _status(conn, tid)["status"] == "done"


def test_default_off_keeps_old_behaviour(conn):
    tid = _claimed(conn)
    assert kb.configured_negative_handoff_review() is False
    assert kb.complete_task(conn, tid, summary=NOT_DEPLOYED)
    assert _status(conn, tid)["status"] == "done"


def test_approval_from_review_is_not_rerouted(conn, armed):
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, summary=NOT_DEPLOYED)
    assert _status(conn, tid)["status"] == "review"
    # The human approving the parked card may quote the same summary.
    assert kb.complete_task(conn, tid, summary=NOT_DEPLOYED)
    assert _status(conn, tid)["status"] == "done"
