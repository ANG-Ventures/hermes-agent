"""A negative-result handoff is never completed in place by the review policy (t_db9ca661).

Under ``kanban.review_policy=milestone_only|none`` a slice card's request_review
completes the card. t_63322ecc / t_18004327 / t_96adeb3f did that with summaries
saying their close gate was NOT met / not proven; Apollo reopened all three.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_negative_result import negative_result_match


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    kb.init_db()
    return home


def _events(conn, tid, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


# The three measured false greens (summaries as recorded) plus each pattern arm.
NEGATIVE = [
    "Close-on read 2026-10-05 23:10: NOT met. 2 pages in window.",
    "NOT YET PROVEN: '0 Cron broken pages in 12 h'",
    "acceptance is still unmet on ACE-AI",
    "do not merge until the soak finishes",
    "probe UNTRUSTED (fixture drift)",
    "verdict: FAIL on live read",
    "gate=DEFERRED to next window",
    "result FAILED",
]
# Ordinary green handoffs, including the bare-word FAIL prose the caps-scoped
# form exists to ignore (385/1392 false positives in 14 d).
POSITIVE = [
    "PR #12 merged; 14 tests pass",
    "3 new tests fail on base, pass on head",
    "fixed the failing gate test; CI green",
    "verdict: pass",
    "untrusted input is now escaped",
    "the result fail path is covered",
    "met the acceptance bar; merged",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative_summaries_match(text):
    assert negative_result_match(text) is not None


@pytest.mark.parametrize("text", POSITIVE)
def test_positive_summaries_do_not_match(text):
    assert negative_result_match(text) is None


def test_metadata_is_scanned():
    assert negative_result_match("done", {"acceptance_met": False}) == '"acceptance_met": false'
    assert negative_result_match("done", {"close_on": "NOT met"}) is not None
    assert negative_result_match("done", {"tests_run": 3}) is None


@pytest.mark.parametrize("policy", ["milestone_only", "none"])
def test_negative_handoff_routes_to_human_review(kanban_home, monkeypatch, policy):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: policy)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice: close-on soak", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="Close-on read: NOT met.", reviewer="argus",
            expected_run_id=claimed.current_run_id, with_reason=True,
        )
        assert ok is True, reason
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert kb.is_human_reviewer(task.assignee)
        assert _events(conn, tid, "review_skipped") == []
        neg = _events(conn, tid, "review_negative_result")
        assert neg and neg[0]["match"] == "NOT met" and neg[0]["policy"] == policy
        assert len(_events(conn, tid, "review_requested")) == 1


def test_positive_handoff_still_completes_in_place(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice: add flag", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn, tid, summary="PR #1 green; 3 new tests fail on base, pass on head",
            metadata={"tests_run": 1},
            expected_run_id=claimed.current_run_id, with_reason=True,
        )
        assert ok is True and "review skipped" in reason
        assert kb.get_task(conn, tid).status == "done"
        assert _events(conn, tid, "review_skipped")
        assert _events(conn, tid, "review_negative_result") == []


def test_milestone_card_negative_keeps_its_named_reviewer(kanban_home, monkeypatch):
    """Milestone cards already reach a reviewer; the guard does not reroute them."""
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="[milestone] wave 3", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="AC-4 NOT met", reviewer="argus",
                                 expected_run_id=claimed.current_run_id) is True
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == "argus"
        assert _events(conn, tid, "review_negative_result") == []
