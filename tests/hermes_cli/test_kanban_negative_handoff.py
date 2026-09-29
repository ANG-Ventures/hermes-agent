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


# --- FleetReview #1447 @30bc5279 (t_daa1f3bf) -------------------------------


@pytest.mark.parametrize("text", [
    "unblocked once bpx#288 merged; deployed and green",
    "this was unblocked on Monday and shipped",
    "the relay could notify Apollo, so wired it",
    "you could note the SHA; landed as a682b93c",
    "CANNOT_DEPLOYED_FLAG untouched",  # substring of a longer identifier
    "merged; t_x unblocked on main",
    "the probe could notice the drift; fixed",
    "no STOP finding; admission measured 39 legs",
    "no longer blocked on bpx#288; landed",
    "could not reproduce the flake in 50 runs; closing as green",
    "no new STOP finding; admission measured 39 legs",
])
def test_phrases_need_word_boundaries(text):
    assert neg.match([text]) is None


def test_quoted_log_lines_in_result_are_not_scanned(conn, armed):
    # ``result`` often carries pasted tool/test output from a failure that was
    # later fixed; the handoff the worker writes is ``summary``.
    tid = _claimed(conn)
    assert kb.complete_task(
        conn, tid, summary=GOOD,
        result="tests green after 2nd commit\n"
               "pytest: ImportError: could not find module foo (fixed in 2nd commit)",
        metadata={"no_pr": True},
    )
    assert _status(conn, tid)["status"] == "done"


def test_result_alone_is_still_scanned(conn, armed):
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, result=NOT_DEPLOYED)
    assert _status(conn, tid)["status"] == "review"


def _scratch_task(conn):
    tid = kb.create_task(conn, title="arm + readout", assignee="daedalus")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    kb.set_workspace_path(conn, tid, ws)
    kb.claim_task(conn, tid)
    artifact = ws / "readout.md"
    artifact.write_bytes(b"readout-bytes")
    return tid, ws, artifact


def _assert_artifact_survives_bare_approval(conn, tid, ws, artifact):
    attachments = kb.list_attachments(conn, tid)
    assert [a.filename for a in attachments] == ["readout.md"]
    stored = Path(attachments[0].stored_path)
    assert stored.parent == kb.task_attachments_dir(tid).resolve()
    routed = kb.latest_run(conn, tid)
    assert routed.metadata["artifacts"] == [str(stored)]
    # Human approves from review WITHOUT repeating the implementer's metadata.
    assert kb.complete_task(conn, tid, summary="approved")
    assert _status(conn, tid)["status"] == "done"
    assert not ws.exists(), "scratch workspace is still cleaned up"
    assert stored.read_bytes() == b"readout-bytes"
    assert [a.filename for a in kb.list_attachments(conn, tid)] == ["readout.md"]


def test_negative_route_preserves_scratch_artifacts(conn, armed):
    tid, ws, artifact = _scratch_task(conn)
    assert kb.complete_task(conn, tid, summary=STOP_FINDING,
                            metadata={"no_pr": True, "artifacts": [str(artifact)]})
    assert _status(conn, tid)["status"] == "review"
    _assert_artifact_survives_bare_approval(conn, tid, ws, artifact)


def test_open_pr_route_preserves_scratch_artifacts(conn, monkeypatch):
    # Same class, sibling early-return: the open-PR review route.
    monkeypatch.setenv("KANBAN_HANDOFF_FRESHNESS", "0")
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o"})
    state = {"s": "OPEN"}
    monkeypatch.setattr(op, "_default_query",
                        lambda: (lambda repo, number: {"state": state["s"]}))
    monkeypatch.setattr("hermes_cli.kanban_survivor.preserve", lambda *a, **k: None)
    tid, ws, artifact = _scratch_task(conn)
    assert kb.complete_task(conn, tid, summary="shipped",
                            metadata={"pr_url": "https://github.com/o/r/pull/7",
                                      "artifacts": [str(artifact)]})
    assert _status(conn, tid)["status"] == "review"
    state["s"] = "MERGED"
    _assert_artifact_survives_bare_approval(conn, tid, ws, artifact)


def test_refused_route_leaves_no_orphan_copies(conn, armed):
    tid, ws, artifact = _scratch_task(conn)
    # Stale run id: request_review refuses; nothing may be attached or copied.
    assert not kb.complete_task(conn, tid, summary=STOP_FINDING, expected_run_id=999999,
                                metadata={"artifacts": [str(artifact)]})
    assert kb.list_attachments(conn, tid) == []
    att_dir = kb.task_attachments_dir(tid)
    assert not att_dir.exists() or list(att_dir.iterdir()) == []
    assert artifact.exists()


def test_routed_handoff_keeps_summary_and_result(conn, armed):
    tid = _claimed(conn)
    details = "journal_legs=39 legs_total=0; root cause: x-hermes-lane not forwarded"
    assert kb.complete_task(conn, tid, summary=STOP_FINDING,
                            result=details, metadata={"no_pr": True})
    assert _status(conn, tid)["status"] == "review"
    routed = kb.latest_run(conn, tid)
    assert STOP_FINDING in routed.summary and details in routed.summary


def test_claimed_review_approval_is_not_rerouted(conn, armed):
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, summary=NOT_DEPLOYED, metadata={"outcome": "partial"})
    assert _status(conn, tid)["status"] == "review"
    review = kb.claim_review_task(conn, tid)
    assert review is not None and _status(conn, tid)["status"] == "running"
    # The claimed approval quotes the negative handoff and keeps outcome=partial.
    assert kb.complete_task(conn, tid, summary=NOT_DEPLOYED, metadata={"outcome": "partial"},
                            expected_run_id=review.current_run_id)
    assert _status(conn, tid)["status"] == "done"


def test_negative_route_promotes_prose_named_artifact(conn, armed):
    tid, ws, artifact = _scratch_task(conn)
    assert kb.complete_task(conn, tid, summary=f"{STOP_FINDING} Readout: {artifact}",
                            metadata={"no_pr": True})
    assert _status(conn, tid)["status"] == "review"
    _assert_artifact_survives_bare_approval(conn, tid, ws, artifact)


# --- FleetReview #1447 @3021cfdb (t_daa1f3bf) -------------------------------


def test_result_headline_is_scanned_even_with_a_summary(conn, armed):
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, summary="Attempted rollout; details in result",
                            result="NOT DEPLOYED; nothing was armed")
    assert _status(conn, tid)["status"] == "review"


@pytest.mark.parametrize("status", ["blocked", "ready"])
def test_non_running_card_completes_with_negative_wording(conn, armed, status):
    tid = kb.create_task(conn, title="vendor", assignee="daedalus")
    if status == "blocked":
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="vendor")
    assert _status(conn, tid)["status"] == status
    # Operator close of a card no worker is running: not an implementer handoff.
    assert kb.complete_task(conn, tid, summary="blocked on vendor, closing as won't-fix")
    assert _status(conn, tid)["status"] == "done"


def test_open_pr_route_escalates_negative_handoff_to_apollo(conn, armed, monkeypatch):
    monkeypatch.setenv("KANBAN_HANDOFF_FRESHNESS", "0")
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o"})
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, number: {"state": "OPEN"}))
    monkeypatch.setattr(kb, "configured_review_assignee", lambda: "argus")
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, summary=NOT_DEPLOYED,
                            metadata={"pr_url": "https://github.com/o/r/pull/7"})
    row = _status(conn, tid)
    assert (row["status"], row["assignee"]) == ("review", neg.REVIEWER)
    kinds = _kinds(conn, tid)
    assert "completion_routed_to_review" in kinds
    assert "completion_routed_negative_handoff" in kinds
    assert kb.latest_run(conn, tid).metadata["negative_handoff"] == "NOT DEPLOYED"


def test_bare_approval_event_carries_routed_artifacts(conn, armed):
    tid, ws, artifact = _scratch_task(conn)
    assert kb.complete_task(conn, tid, summary=STOP_FINDING,
                            metadata={"artifacts": [str(artifact)]})
    stored = kb.list_attachments(conn, tid)[0].stored_path
    assert kb.complete_task(conn, tid, summary="approved")
    completed = [e for e in kb.list_events(conn, tid) if e.kind == "completed"][-1]
    assert completed.payload["artifacts"] == [stored]
    # No duplicate attachment row for the carried copy.
    assert len(kb.list_attachments(conn, tid)) == 1


def test_vanished_copy_after_route_does_not_fail_the_route(conn, armed, monkeypatch):
    tid, ws, artifact = _scratch_task(conn)
    real = kb.request_review

    def racing_request_review(*a, **k):
        out = real(*a, **k)
        for p in k["metadata"]["artifacts"]:
            Path(p).unlink()
        return out

    monkeypatch.setattr(kb, "request_review", racing_request_review)
    assert kb.complete_task(conn, tid, summary=STOP_FINDING,
                            metadata={"artifacts": [str(artifact)]})
    assert _status(conn, tid)["status"] == "review"
    assert kb.list_attachments(conn, tid) == []
    assert "routed_artifact_missing" in _kinds(conn, tid)


# --- FleetReview #1447 @aa9e59a3 (t_c48014d3) -------------------------------


def test_reviewer_artifact_is_merged_with_routed_artifacts(conn, armed):
    # Approval that brings its own artifact must not drop the implementer's.
    tid, ws, artifact = _scratch_task(conn)
    assert kb.complete_task(conn, tid, summary=STOP_FINDING,
                            metadata={"artifacts": [str(artifact)]})
    routed = kb.list_attachments(conn, tid)[0].stored_path
    review_note = ws / "review.md"
    review_note.write_bytes(b"review-bytes")
    assert kb.complete_task(conn, tid, summary="approved",
                            metadata={"artifacts": [str(review_note)]})
    assert _status(conn, tid)["status"] == "done"
    completed = [e for e in kb.list_events(conn, tid) if e.kind == "completed"][-1]
    arts = completed.payload["artifacts"]
    assert arts[0] == routed and len(arts) == 2
    assert Path(arts[1]).read_bytes() == b"review-bytes"
    # One attachment row per file: the carried copy is not re-registered.
    names = sorted(a.filename for a in kb.list_attachments(conn, tid))
    assert names == ["readout.md", "review.md"]


def test_reviewer_repeating_routed_artifact_is_not_duplicated(conn, armed):
    tid, ws, artifact = _scratch_task(conn)
    assert kb.complete_task(conn, tid, summary=STOP_FINDING,
                            metadata={"artifacts": [str(artifact)]})
    routed = kb.list_attachments(conn, tid)[0].stored_path
    assert kb.complete_task(conn, tid, summary="approved",
                            metadata={"artifacts": [routed]})
    completed = [e for e in kb.list_events(conn, tid) if e.kind == "completed"][-1]
    assert completed.payload["artifacts"] == [routed]
    assert len(kb.list_attachments(conn, tid)) == 1


@pytest.mark.parametrize("text,trigger", [
    ("no workaround for STOP finding: admission measures nothing", "STOP finding"),
    ("no idea why; blocked on vendor creds", "blocked on"),
    ("not sure, but could not reach the box", "could not"),
    ("never mind the flake: NOT DEPLOYED", "NOT DEPLOYED"),
])
def test_negator_governing_another_word_does_not_suppress(text, trigger):
    assert neg.match([text]) == trigger


def test_negative_verdict_below_result_headline_is_scanned(conn, armed):
    tid = _claimed(conn)
    assert kb.complete_task(conn, tid, summary="Merged PR",
                            result="Rollout status:\nNOT DEPLOYED; blocked on credentials")
    assert _status(conn, tid)["status"] == "review"


@pytest.mark.parametrize("result", [
    "tests green\n```\nImportError: could not find module foo\n```",
    "tests green\n    ImportError: could not find module foo",
    "tests green\n> ERROR could not connect (retried, fixed)",
    "tests green\nImportError: could not find module foo",
    "tests green\nE   RuntimeError: could not bind port",
    "tests green\n2026-09-28 13:00:01 WARNING could not reach cache",
    "tests green\n[13:00:01] could not reach cache; retrying",
    "tests green\n$ ssh box  # could not resolve host first try",
    "tests green\nTraceback (most recent call last):",
    # "<program>: message" tool output (FleetReview #1464 @03d1cc77).
    "Deployment complete\nssh: Could not resolve hostname box\nRetried successfully",
    "tests green\ncurl: (6) Could not resolve host: example.com",
    "tests green\nfatal: could not read Username for 'https://github.com'",
    "tests green\nerror: could not lock config file .git/config",
    "tests green\n/usr/bin/ssh: Could not resolve hostname box",
    "tests green\nbash: line 1: could not open /tmp/x",
    "tests green\npython3.13: could not import site",
    "tests green\nssh[4242]: Could not resolve hostname box",
])
def test_pasted_log_lines_in_result_body_are_not_scanned(result):
    assert neg.match(neg.handoff_texts(GOOD, result)) is None


@pytest.mark.parametrize("result, trigger", [
    ("Rollout:\nstatus: blocked on credentials", "blocked on"),
    ("Rollout:\nVerdict: could not deploy", "could not"),
    ("Rollout:\nSsh access could not be granted", "could not"),
])
def test_prose_labels_are_still_scanned(result, trigger):
    assert neg.match(neg.handoff_texts(GOOD, result)) == trigger
