"""review_coverage records carry the reviewed PR head (``head_sha``) — t_7fee0f83.

Card-sourced land requests (hermes-home scripts/land_request.py) take the
card's LATEST ``review_coverage`` record as the review of record and refuse
with NO_REVIEW_OF_RECORD unless it is an APPROVE carrying ``head_sha``. The
rework gate requires the field; a reviewer approval that names the head
(``metadata.head_sha``) writes the APPROVE record itself.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def review(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="head sha probe", assignee="builder")
        implementation = kb.claim_task(conn, tid)
        assert implementation
        assert kb.request_review(conn, tid, summary="ready", reviewer="argus",
                                 expected_run_id=implementation.current_run_id)
        assert kb.claim_review_task(conn, tid)
    return tid


def coverage(**overrides):
    payload = {
        "lenses": {name: "done" for name in LENSES},
        "findings": 1, "items": ["Missing behavior assertion at handler:42"],
        "review_minutes": 12, "batch_id": "batch-123", "head_sha": SHA,
    }
    payload.update(overrides)
    return "review_coverage: " + json.dumps(payload)


def latest_record(conn, task_id):
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE '%review_coverage:%' "
        "ORDER BY id DESC", (task_id,)).fetchall()
    record, _ = kb._latest_review_coverage(rows)
    return record


@pytest.mark.parametrize("head_sha", [SHA, SHA[:12], "n/a: research card, no PR"])
def test_rework_gate_accepts_head_sha_or_na(review, head_sha):
    with kb.connect() as conn:
        kb.add_comment(conn, review, "argus", coverage(head_sha=head_sha),
                       run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason="BEHAVIOUR: fix guard") == (True, "builder")


@pytest.mark.parametrize("head_sha", [None, "", "main", "HEAD", "n/a:", "xyz" * 5, SHA + "0"])
def test_rework_gate_refuses_missing_or_invalid_head_sha(review, head_sha):
    payload = json.loads(coverage().split("review_coverage: ", 1)[1])
    if head_sha is None:
        del payload["head_sha"]
    else:
        payload["head_sha"] = head_sha
    with kb.connect() as conn:
        kb.add_comment(conn, review, "argus", "review_coverage: " + json.dumps(payload),
                       run_id=kb.get_task(conn, review).current_run_id)
        ok, detail = kb.request_changes(conn, review, reason="BEHAVIOUR: fix guard")
        assert not ok and "head_sha" in detail
        assert kb.get_task(conn, review).status == "running"


def test_reviewer_approval_with_head_sha_writes_approve_record(review):
    with kb.connect() as conn:
        run_id = kb.get_task(conn, review).current_run_id
        assert kb.complete_task(conn, review, summary="LGTM", metadata={"head_sha": SHA})
        record = latest_record(conn, review)
        assert record is not None
        assert record["verdict"] == "approve"
        assert record["head_sha"] == SHA
        row = conn.execute(
            "SELECT run_id FROM task_comments WHERE task_id = ? AND body LIKE '%review_coverage:%'",
            (review,)).fetchone()
        assert row["run_id"] == run_id


def test_approve_record_supersedes_earlier_rework_record(review):
    """The land client reads only the LATEST record: approval must land after round-1 rework."""
    with kb.connect() as conn:
        kb.add_comment(conn, review, "argus", coverage(head_sha="a" * 40),
                       run_id=kb.get_task(conn, review).current_run_id)
        assert kb.request_changes(conn, review, reason="BEHAVIOUR: fix guard")[0]
        impl = kb.claim_task(conn, review)
        assert kb.request_review(conn, review, summary="fixed", reviewer="argus",
                                 expected_run_id=impl.current_run_id)
        assert kb.claim_review_task(conn, review)
        assert kb.complete_task(conn, review, summary="LGTM", metadata={"head_sha": SHA})
        assert latest_record(conn, review)["head_sha"] == SHA


def test_reviewer_approval_with_malformed_head_sha_refused_before_mutation(review):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="head_sha"):
            kb.complete_task(conn, review, summary="LGTM", metadata={"head_sha": "main"})
        assert kb.get_task(conn, review).status == "running"
        assert latest_record(conn, review) is None


def test_implementer_metadata_head_sha_never_forges_an_approval(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder")
        assert kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done", metadata={"head_sha": SHA})
        assert latest_record(conn, tid) is None


def test_reviewer_approval_of_open_pr_is_not_rerouted_to_review(review, monkeypatch):
    """The approved PR is OPEN by definition (the land queue merges it after
    approval). Open-PR completion routing (#1094) must not bounce the verdict
    back to ``review`` and drop the APPROVE record land_request.py reads."""
    from hermes_cli import kanban_open_pr as op
    monkeypatch.setattr(op, "_default_query",
                        lambda: (lambda repo, number: {"state": "OPEN"}))
    with kb.connect() as conn:
        assert kb.complete_task(
            conn, review,
            summary="LGTM https://github.com/ANG-Ventures/example/pull/670",
            metadata={"head_sha": SHA},
        )
        assert kb.get_task(conn, review).status == "done"
        record = latest_record(conn, review)
        assert record is not None and record["verdict"] == "approve"
        assert record["head_sha"] == SHA
