"""A RUNNING card's own worker must reach a terminal-for-the-worker state.

Incident t_7fee0f83 run 11571 (2026-09-26): kanban.review_policy=milestone_only,
the card's PR (#1129) still OPEN, and the card's history held a same-actor
review drain (implementer daedalus, reviewer daedalus via takeover). The worker
got BOTH transitions refused on its own live run:

- request_review -> "review_policy=milestone_only: card needs no review
  session and could not be completed in place"
- complete_task  -> False ("unknown id or already terminal")

The history here is built through the real kernel transitions (claim ->
request_review -> reviewer takeover + claim -> request_changes -> re-claim),
not hand-seeded events, so it pins the whole path the incident took. Each tool
must land the card in exactly one of {review (open PR), done}.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES
from hermes_cli import kanban_open_pr as op

PR_URL = "https://github.com/ANG-Ventures/hermes-agent/pull/1129"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(kb, "spawnable_reviewer_profiles", lambda: ["daedalus", "argus"])
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "argus")
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    return home


def _oracle(monkeypatch, state):
    monkeypatch.setattr(
        op, "_default_query",
        lambda: (lambda repo, number: {"state": state}),
    )


def _run_id(conn, tid):
    return conn.execute(
        "SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def _incident_history(conn):
    """Replay t_7fee0f83 up to run 11571 through real transitions."""
    tid = kb.create_task(conn, title="gh-apps Phase 2b slice", assignee="daedalus")
    kb.claim_task(conn, tid)
    ok, why = kb.request_review(
        conn, tid, summary=f"shipped {PR_URL}", reviewer="argus",
        expected_run_id=_run_id(conn, tid), force=True, with_reason=True,
    )
    assert ok, why
    assert _status(conn, tid) == "review"
    # Review drain: the implementer's own profile takes the card over and
    # returns it (reviewer == implementer on the changes_requested event).
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET assignee='daedalus' WHERE id=?", (tid,))
    assert kb.claim_review_task(conn, tid) is not None
    coverage = json.dumps({
        "lenses": {k: "n/a: merge-state drain only" for k in REQUIRED_REVIEW_LENSES},
        "findings": 1, "items": ["#1129 CONFLICTING/DIRTY"], "review_minutes": 2,
        "batch_id": "review-drain", "head_sha": "193087a55d",
    })
    kb.add_comment(conn, tid, "daedalus", "review_coverage: " + coverage,
                   run_id=_run_id(conn, tid))
    ok, why = kb.request_changes(
        conn, tid, reason="rebase #1129", expected_run_id=_run_id(conn, tid))
    assert ok, why
    kb.claim_task(conn, tid)
    assert _status(conn, tid) == "running"
    return tid, _run_id(conn, tid)


@pytest.mark.parametrize("policy", ["milestone_only", "all"])
def test_request_review_with_open_pr_reaches_review(board, monkeypatch, policy):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: policy)
    with kb.connect() as conn:
        _oracle(monkeypatch, "MERGED")  # history setup: nothing routed
        tid, run_id = _incident_history(conn)
        _oracle(monkeypatch, "OPEN")
        ok, reason = kb.request_review(
            conn, tid, summary=f"rebased {PR_URL}",
            metadata={"pr_url": PR_URL},
            expected_run_id=run_id, with_reason=True,
        )
        assert ok is True, reason
        assert _status(conn, tid) == "review"


def test_complete_with_open_pr_reaches_review(board, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    with kb.connect() as conn:
        _oracle(monkeypatch, "MERGED")
        tid, run_id = _incident_history(conn)
        _oracle(monkeypatch, "OPEN")
        assert kb.complete_task(
            conn, tid, summary=f"rebased {PR_URL}", expected_run_id=run_id,
        ) is True
        assert _status(conn, tid) == "review"


def test_request_review_with_merged_pr_completes_in_place(board, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    with kb.connect() as conn:
        _oracle(monkeypatch, "MERGED")
        tid, run_id = _incident_history(conn)
        ok, reason = kb.request_review(
            conn, tid, summary=f"landed {PR_URL}",
            expected_run_id=run_id, with_reason=True,
        )
        assert ok is True, reason
        assert _status(conn, tid) == "done"
