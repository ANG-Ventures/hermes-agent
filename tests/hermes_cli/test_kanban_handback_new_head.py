"""A worker completion routes to review only when its PR has a commit the reviewer has not seen.

Regression for t_f716021d: t_d72c9cfc (hermes-home#6525, head unchanged since the send-back) and
t_fafca854 (hermes-home#5877, only a fold-bot merge of main) were both routed to review as handbacks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_handback_new_head as nh
from hermes_cli import kanban_open_pr as op
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES

PR = "https://github.com/ANG-Ventures/example-home/pull/670"
A = "a" * 39 + "1"        # head the reviewer sent back
B = "b" * 39 + "2"        # fold-bot merge of main on top of A
C = "c" * 39 + "3"        # implementer commit on top of A
M = "d" * 39 + "4"        # a main commit brought in by B


def _coverage(head: str) -> str:
    return "review_coverage: " + json.dumps({
        "lenses": {k: "done" for k in LENSES}, "findings": 1,
        "items": ["Missing behavior assertion at handler:42"],
        "review_minutes": 5, "batch_id": "b-1", "head_sha": head,
    })


# The PR's GitHub state after the send-back, keyed by scenario. ``pr_commits`` are the PR's own
# commits (pulls/N/commits); ``since_seen`` is compare/<A>...<head>.
VIEWS = {
    "unchanged": {"head": A, "pr_commits": [{"sha": A, "parents": 1}], "since_seen": [], "complete": True},
    "fold_merge": {"head": B, "pr_commits": [{"sha": A, "parents": 1}, {"sha": B, "parents": 2}],
                   "since_seen": [M, B], "complete": True},
    "new_commit": {"head": C, "pr_commits": [{"sha": A, "parents": 1}, {"sha": C, "parents": 1}],
                   "since_seen": [C], "complete": True},
}


@pytest.fixture
def sent_back(tmp_path, monkeypatch):
    """A card reviewed at head A, sent back, and re-claimed by its worker."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(op, "_default_query", lambda: lambda repo, n: {"state": "OPEN"})
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder")
        run = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary=f"r1 {PR}", expected_run_id=run.current_run_id)
        assert kb.get_task(conn, tid).status == "review"
        assert kb.claim_review_task(conn, tid)
        kb.add_comment(conn, tid, "argus", _coverage(A), run_id=kb.get_task(conn, tid).current_run_id)
        assert kb.request_changes(conn, tid, reason="BEHAVIOUR: build r7")[0]
        assert kb.get_task(conn, tid).status == "ready"
    return tid


def _complete_as_worker(tid, monkeypatch, scenario):
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS[scenario])
    with kb.connect() as conn:
        run = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary=f"posted a note; {PR}",
                                expected_run_id=run.current_run_id)
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND id > (SELECT MAX(id) FROM task_events "
            "WHERE task_id=? AND kind='claimed') ORDER BY id", (tid, tid))]
        comments = [r[0] for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))]
        return kb.get_task(conn, tid).status, kinds, comments


@pytest.mark.parametrize("scenario", ["unchanged", "fold_merge"])
def test_no_new_head_is_not_a_handback(sent_back, monkeypatch, scenario):
    status, kinds, comments = _complete_as_worker(sent_back, monkeypatch, scenario)
    assert status == "ready"
    assert "review_requested" not in kinds and "completion_routed_to_review" not in kinds
    assert nh.EVENT in kinds
    assert comments[-1] == f"no new head since {A[:10]}; the send-back stands"


def test_bounced_run_keeps_the_workers_handoff(sent_back, monkeypatch):
    """Prism 9c7fd6708b92: the bounce must not drop the run's own summary and metadata."""
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back)
        assert kb.complete_task(conn, sent_back, summary=f"suites.json collides with t_x; {PR}",
                                metadata={"tests_run": 3}, expected_run_id=run.current_run_id)
        ended = kb.latest_run(conn, sent_back)
    assert ended.outcome == nh.OUTCOME
    assert ended.summary.startswith("no new head since") and "suites.json collides" in ended.summary
    assert ended.metadata["tests_run"] == 3 and ended.metadata["no_new_head"]["reason"] == "unchanged"


def test_implementer_commit_since_send_back_routes_to_review(sent_back, monkeypatch):
    status, kinds, _ = _complete_as_worker(sent_back, monkeypatch, "new_commit")
    assert status == "review"
    assert "review_requested" in kinds and nh.EVENT not in kinds


def test_bounce_is_bounded_per_send_back(sent_back, monkeypatch):
    """A worker that cannot move the PR still reaches the reviewer after MAX_BOUNCES."""
    for _ in range(nh.MAX_BOUNCES):
        assert _complete_as_worker(sent_back, monkeypatch, "unchanged")[0] == "ready"
    status, kinds, _ = _complete_as_worker(sent_back, monkeypatch, "unchanged")
    assert status == "review" and "review_requested" in kinds
