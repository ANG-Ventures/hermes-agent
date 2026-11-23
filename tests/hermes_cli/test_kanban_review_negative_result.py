"""A negative-result handoff is never completed in place by the review policy (t_db9ca661).

Under ``kanban.review_policy=milestone_only|none`` a slice card's request_review
completes the card. t_63322ecc / t_18004327 / t_96adeb3f did that with summaries
saying their close gate was NOT met / not proven; Apollo reopened all three.
"""
from __future__ import annotations

import json
import sqlite3
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
    # t_1938555e: real verdicts from the 14 d sample, each outside the first line.
    "PR o/r#1 is open.\nThe done criterion is NOT yet proven: no live-portal check after a bake.",
    "PR o/r#1 is open.\nNot proven:\n- no live train has run this yet",
    "Opened two PRs.\nWhat is not proven:\n- the added-file case on the Studio",
    "Opened o/r#1. Live acceptance is still NOT met: there is no completed .backup yet.",
    "Draft PR o/r#1. It is still a DRAFT, do not land.",
    "Card test met on mbp. Acceptance is not proven:\n- ACE-AI",
    # Prism round 1 on #536: a labelled verdict and a polite instruction are verdicts.
    "Result: not proven; the live check is pending.",
    "PR o/r#1 is open.\nStatus: NOT proven live.",
    "Please do not merge until the live check passes.",
    "PR o/r#1 is open. Please don't land it yet.",
    # Prism round 2 on #536: a bold label is still a label.
    "PR o/r#1 is open.\n**Status:** NOT proven live.",
    "__Result__: not proven yet.",
    # Operator send-back on #536: ``` fences get no special handling, so a ``` never hides a later verdict and
    # pasted negative output in a fence keeps the draft (fail toward the draft).
    "The formatter recognizes ``` markers.\nAcceptance NOT met: live soak pending.",
    "- ```text\n  sample output\n  ```\n\nAcceptance NOT met: live soak pending.",
    "Done. The probe printed:\n```\nResult: FAIL\nNot proven: idle\n```\nThat is the expected refusal.",
    "Done. Output:\n```text\nverdict: FAIL\n",
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
    # t_1938555e: prose that DESCRIBES code, a test or quoted output, measured in the 14 d sample.
    "Done.\n- f67496fc94d3 (P2): Any other failure is reported as not proven and is not cached.",
    "Done. A timeout or a refused admit counts as not proven, so the card stays port_stranded.",
    "Done. The journal says `NOT proven (idle: no delivery in N min ...)`, rc 6.",
    'Done. It pages "NOT proven back" only when that limit expires.',
    "Done. A forced abort gave rc 1 and result.json with verdict FAIL and error KeyError.",
    "Done. The guard script now checks chmod and mv, so a failed replace prints RESULT FAIL.",
    "Proof: I ran the H14 block and graded it. Result: FAIL on the live root, as intended.",
    "Done. If two halves carry the same step key, the halves do not merge.",
    "PR o/r#1 is green; I do not merge.",
    "Done. It looks like a process race, but I have not proven that.",
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
    assert negative_result_match("done", {"gates": {"acceptance_met": False}}) == '"acceptance_met": false'
    assert negative_result_match("done", {"pr_state": "draft, label hold, do not land"}) == "do not land"
    assert negative_result_match("done", {"decisions": ["held reads stay rc 1 since the fault is not proven gone"]}) is None


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


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("human:apollo", "human:apollo"), (None, "human"), ("argus", "human")],
)
def test_negative_handoff_resolves_configured_human_lane(
    kanban_home, monkeypatch, configured, expected,
):
    """The negative-result route lands on the board's named human lane, not the
    bare sentinel (t_626f68ea: 57/89 bare-human review cards came from here)."""
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: configured)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice: close-on soak", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="Close-on read: NOT met.",
                                 expected_run_id=claimed.current_run_id) is True
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.assignee == expected
        neg = _events(conn, tid, "review_negative_result")
        assert neg and neg[0]["reviewer"] == expected and neg[0]["match"] == "NOT met"


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


def test_failed_negative_result_event_rolls_back_with_the_transition(kanban_home, monkeypatch):
    """The review_negative_result event rides the review transition's txn.
    A failure writing it (lock timeout, disk full) must roll the whole
    handoff back with its staged copies, never leave a committed review whose
    referenced artifact copies were discarded (Prism P1 83af2fcef278 on #1859)."""
    from hermes_cli import kanban_db_workspace as kbw

    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    real_append = kb._append_event
    fail = {"on": True}

    def _append(conn, task_id, kind, *a, **k):
        if kind == "review_negative_result" and fail["on"]:
            raise sqlite3.OperationalError("database is locked")
        return real_append(conn, task_id, kind, *a, **k)

    monkeypatch.setattr(kb, "_append_event", _append)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice: close-on soak", assignee="builder")
        ws = kbw.resolve_workspace(kb.get_task(conn, tid))
        kbw.set_workspace_path(conn, tid, ws)
        artifact = ws / "evidence.json"
        artifact.write_bytes(b'{"ok": false}')
        claimed = kb.claim_task(conn, tid)
        kwargs = dict(
            summary="Close-on read: NOT met.", metadata={"artifacts": [str(artifact)]},
            expected_run_id=claimed.current_run_id,
        )
        with pytest.raises(sqlite3.OperationalError):
            kb.request_review(conn, tid, **kwargs)
        attachment_dir = kb.task_attachments_dir(tid)
        assert kb.get_task(conn, tid).status == "running"
        assert kb.list_attachments(conn, tid) == []
        assert _events(conn, tid, "review_requested") == []
        assert not attachment_dir.exists() or not any(attachment_dir.iterdir())

        fail["on"] = False
        assert kb.request_review(conn, tid, **kwargs) is True
        task = kb.get_task(conn, tid)
        assert task.status == "review" and kb.is_human_reviewer(task.assignee)
        attachments = kb.list_attachments(conn, tid)
        assert [e["match"] for e in _events(conn, tid, "review_negative_result")] == ["NOT met"]
    assert [a.filename for a in attachments] == ["evidence.json"]
    stored = Path(attachments[0].stored_path)
    assert stored.read_bytes() == b'{"ok": false}'
    assert sorted(p.name for p in attachment_dir.iterdir()) == ["evidence.json"]
