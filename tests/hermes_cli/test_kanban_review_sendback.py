"""Human/orchestrator reviewer send-back on a parked ``review`` card.

With ``review_dispatch`` off nothing ever claims a ``review`` card, so the
human (or Apollo) is the reviewer. ``request_changes(claimer=...)`` opens the
review run as the caller and requests changes in ONE transaction, leaving the
same audit trail a dispatched reviewer leaves (t_f31e2ff3). The #999 coverage
gate needs a review_coverage comment bound to the CURRENT run, so the
caller-supplied ``coverage`` is recorded on the newly opened run inside that
same transaction, before the gate reads it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_parser as kp
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES


def _coverage(**override) -> str:
    record = {
        "lenses": {name: "done" for name in REQUIRED_REVIEW_LENSES},
        "findings": 1,
        "items": ["F1 boundary test missing"],
        "review_minutes": 5,
        "batch_id": "apollo-r1-sendback",
        # #1129: coverage names the reviewed PR head.
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
    }
    record.update(override)
    return json.dumps(record)


COVERAGE = _coverage()


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    # The caller stands in for Apollo: prove it the way production does (the
    # operator gateway's own process tree), not by the env name (t_3b9dbdb1).
    from hermes_cli import kanban_identity as _ki
    monkeypatch.setattr(_ki, "_runs_under_operator_gateway", lambda name: True)
    # The caller is NOT a dispatched worker: no task/run identity.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _parked_review(conn) -> str:
    """builder implements, requests review; nobody claims the review."""
    tid = kb.create_task(conn, title="parked review", assignee="builder")
    impl = kb.claim_task(conn, tid, claimer="builder:1")
    assert impl is not None
    assert kb.request_review(
        conn, tid, summary="ready", reviewer="human",
        expected_run_id=impl.current_run_id,
    ) is True
    task = kb.get_task(conn, tid)
    assert task.status == "review" and task.current_run_id is None
    return tid


def _kinds(conn, tid):
    return [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None, r["run_id"])
        for r in conn.execute(
            "SELECT kind, payload, run_id FROM task_events "
            "WHERE task_id = ? ORDER BY id", (tid,),
        ).fetchall()
    ]


def _assert_sent_back(conn, tid, claimer: str) -> None:
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.assignee == "builder"  # implementer from review_requested
    assert task.claim_lock is None
    events = _kinds(conn, tid)
    after = events[[k for k, _, _ in events].index("review_requested") + 1:]
    assert [k for k, _, _ in after] == ["claimed", "commented", "changes_requested"]
    (_, claimed, claim_run), (_, _, comment_run), (_, changed, change_run) = after
    assert claimed["source_status"] == "review"
    assert claimed["lock"] == claimer
    assert claim_run == claimed["run_id"] == comment_run == change_run
    coverage_rows = [
        c for c in kb.list_comments(conn, tid)
        if c.body.startswith("review_coverage:")
    ]
    assert [(c.run_id, c.author) for c in coverage_rows] == [(claim_run, claimer)]
    assert changed["implementer"] == "builder"
    run = conn.execute(
        "SELECT status, outcome, claim_lock FROM task_runs WHERE id = ?",
        (claim_run,),
    ).fetchone()
    assert run["outcome"] == "changes_requested"
    # _end_run clears the run's claim_lock; the claimer lives on the event.


def test_send_back_opens_review_run_and_requests_changes(board: Path) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        ok, detail = kb.request_changes(
            conn, tid, reason="add the boundary test", claimer="apollo",
            coverage=COVERAGE,
        )
        assert (ok, detail) == (True, "builder")
        _assert_sent_back(conn, tid, "apollo")


def _assert_untouched(conn, tid, before, runs_before) -> None:
    task = kb.get_task(conn, tid)
    assert task.status == "review"
    assert task.claim_lock is None and task.current_run_id is None
    assert _kinds(conn, tid) == before
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,),
    ).fetchone()[0] == runs_before
    assert not [
        c for c in kb.list_comments(conn, tid)
        if "review_coverage:" in c.body
    ]


def _snapshot(conn, tid):
    return _kinds(conn, tid), conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,),
    ).fetchone()[0]


def test_send_back_without_coverage_is_refused_before_claim(board: Path) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="apollo",
        )
        assert ok is False
        assert detail == kb._REVIEW_COVERAGE_MISSING
        _assert_untouched(conn, tid, before, runs_before)
        kinds = [k for k, _, _ in _kinds(conn, tid)]
        assert kinds[-1] == "review_requested"  # no review claim after it


def test_send_back_with_incomplete_coverage_rolls_claim_back(board: Path) -> None:
    dropped = REQUIRED_REVIEW_LENSES[-1]
    partial = _coverage(lenses={
        name: "done" for name in REQUIRED_REVIEW_LENSES if name != dropped
    })
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="apollo", coverage=partial,
        )
        assert ok is False
        assert f"missing/invalid lens {dropped}" in detail
        _assert_untouched(conn, tid, before, runs_before)


def test_send_back_without_head_sha_rolls_claim_back(board: Path) -> None:
    # #1129's head_sha requirement is enforced on the parked-review path too.
    no_head = json.loads(COVERAGE)
    no_head.pop("head_sha")
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="apollo",
            coverage=json.dumps(no_head),
        )
        assert ok is False
        assert "head_sha" in detail
        _assert_untouched(conn, tid, before, runs_before)


def test_without_claimer_parked_review_is_still_refused(board: Path) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before = _kinds(conn, tid)
        ok, detail = kb.request_changes(conn, tid, reason="fix")
        assert ok is False and "not in an active review run" in detail
        assert kb.get_task(conn, tid).status == "review"
        assert _kinds(conn, tid) == before


def test_running_under_worker_claim_is_refused(board: Path) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="in flight", assignee="builder")
        impl = kb.claim_task(conn, tid, claimer="builder:1")
        before = _kinds(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="apollo", coverage=COVERAGE,
        )
        assert ok is False
        assert detail == "active run was not claimed from review"
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.claim_lock == "builder:1"
        assert task.current_run_id == impl.current_run_id
        assert _kinds(conn, tid) == before


def test_refusal_after_claim_rolls_the_claim_back(board: Path) -> None:
    """Any refusal behind the claim (the #999 coverage gate sits at the same
    point) leaves the card parked in review: no run, no claimed event."""
    with kb.connect() as conn:
        tid = _parked_review(conn)
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? "
                "AND kind = 'review_requested'", (tid,),
            )
        runs_before = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,),
        ).fetchone()[0]
        before = _kinds(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="apollo", coverage=COVERAGE,
        )
        assert (ok, detail) == (False, "no prior review_requested event")
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.claim_lock is None and task.current_run_id is None
        assert _kinds(conn, tid) == before
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,),
        ).fetchone()[0] == runs_before


def test_dispatched_reviewer_path_unchanged(board: Path) -> None:
    """A worker run passing its own run id never auto-claims."""
    with kb.connect() as conn:
        tid = _parked_review(conn)
        review = kb.claim_review_task(conn, tid, claimer="argus:1")
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="argus:1",
            expected_run_id=review.current_run_id, coverage=COVERAGE,
        )
        assert (ok, detail) == (True, "builder")
        _assert_sent_back(conn, tid, "argus:1")


def _cli_send_back(tid: str) -> int:
    return kc._cmd_request_changes(
        argparse.Namespace(
            task_id=tid, reason=["add", "the", "test"], coverage=COVERAGE,
        ),
    )


def test_cli_request_changes_sends_back_parked_review(
    board: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = "20260926_070000_operator"
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    with kb.connect() as conn:
        tid = _parked_review(conn)
    assert _cli_send_back(tid) == 0
    with kb.connect() as conn:
        events = _kinds(conn, tid)
        after = events[[k for k, _, _ in events].index("review_requested") + 1:]
        # The one-shot send-back leaves claim --review's audit: the session
        # is bound on the claimed event, then the human-lane rework comment.
        assert [k for k, _, _ in after] == [
            "claimed", "commented", "changes_requested", "commented",
        ]
        claimed, run_id = after[0][1], after[0][2]
        assert claimed["session_ref"] == kb.derive_session_ref(session)
        rework = [
            c for c in kb.list_comments(conn, tid)
            if c.body.startswith("changes requested (human review lane):")
        ]
        assert [c.run_id for c in rework] == [run_id]
        # Drop the trailing rework comment; the rest is the shared shape.
        conn.execute(
            "DELETE FROM task_events WHERE id = (SELECT MAX(id) FROM task_events "
            "WHERE task_id = ?)", (tid,),
        )
        _assert_sent_back(conn, tid, "apollo")


@pytest.mark.parametrize("session", [None, "cron_abc123_20260926_070000"])
def test_cli_parked_send_back_refused_without_bindable_session(
    board: Path, monkeypatch: pytest.MonkeyPatch, session,
) -> None:
    """Same provenance rule as claim --review (t_088fe9e3): a sessionless
    caller or a cron job cannot open the human-lane review run."""
    if session is None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_ID", session)
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before = len(_kinds(conn, tid))
    assert _cli_send_back(tid) == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.claim_lock is None
        assert len(_kinds(conn, tid)) == before


@pytest.fixture
def chat_session(monkeypatch: pytest.MonkeyPatch) -> str:
    """A bindable chat session: the only caller that may open a human-lane
    review run from the tool (FleetReview #1081, same rule as the CLI)."""
    sid = "20260926_070000_chat"
    monkeypatch.setenv("HERMES_SESSION_ID", sid)
    return sid


def test_tool_request_changes_sends_back_parked_review(board: Path, chat_session) -> None:
    from tools import kanban_tools as tools

    with kb.connect() as conn:
        tid = _parked_review(conn)
    out = json.loads(tools._handle_request_changes({
        "task_id": tid, "reason": "add the boundary test",
        "coverage": json.loads(COVERAGE),
    }))
    assert out["ok"] is True
    assert out["implementer"] == "builder"
    assert out["status"] == "ready"
    with kb.connect() as conn:
        _assert_sent_back(conn, tid, "apollo")


def test_tool_send_back_from_gateway_session_attributes_active_profile(
    board: Path, monkeypatch: pytest.MonkeyPatch, chat_session,
) -> None:
    """An orchestrator in a gateway session has no HERMES_PROFILE (only dispatched
    workers do): the review claim and coverage comment name the active profile."""
    from hermes_cli import profiles
    from tools import kanban_tools as tools

    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    with kb.connect() as conn:
        tid = _parked_review(conn)
    out = json.loads(tools._handle_request_changes({
        "task_id": tid, "reason": "add the boundary test",
        "coverage": json.loads(COVERAGE),
    }))
    assert out["ok"] is True
    with kb.connect() as conn:
        _assert_sent_back(conn, tid, "default")


def test_tool_send_back_without_coverage_is_refused(board: Path, chat_session) -> None:
    from tools import kanban_tools as tools

    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
    out = json.loads(tools._handle_request_changes({
        "task_id": tid, "reason": "add the boundary test",
    }))
    assert "missing review_coverage" in out["error"]
    with kb.connect() as conn:
        _assert_untouched(conn, tid, before, runs_before)


@pytest.mark.parametrize("session", [None, "cron_abc123_20260926_070000"])
def test_tool_parked_send_back_refused_without_bindable_session(
    board: Path, monkeypatch: pytest.MonkeyPatch, session,
) -> None:
    """FleetReview #1081: the tool path had no session check, so a sessionless
    or cron caller could open and close a human-lane review run the CLI
    refuses. Now refused identically, before any write."""
    from tools import kanban_tools as tools

    if session is None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_SESSION_ID", session)
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
    out = json.loads(tools._handle_request_changes({
        "task_id": tid, "reason": "add the boundary test",
        "coverage": json.loads(COVERAGE),
    }))
    assert "human-lane review claim" in out["error"]
    with kb.connect() as conn:
        _assert_untouched(conn, tid, before, runs_before)


def test_dashboard_request_changes_route_sends_back_parked_review(board: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.kanban.dashboard import plugin_api

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    client = TestClient(app)
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
    url = f"/api/plugins/kanban/tasks/{tid}/request-changes"
    refused = client.post(url, json={"reason": "fix", "author": "apollo"})
    assert refused.status_code == 409
    assert "missing review_coverage" in refused.json()["detail"]
    with kb.connect() as conn:
        _assert_untouched(conn, tid, before, runs_before)
    ok = client.post(url, json={
        "reason": "add the boundary test", "author": "apollo",
        "coverage": COVERAGE,
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["implementer"] == "builder"
    with kb.connect() as conn:
        _assert_sent_back(conn, tid, "apollo")


# --- operator send-back (t_7481005e) ---------------------------------------
# #999 made a review card leave review only with a full coverage record. An
# operator profile bouncing a card for a non-review reason ("wrong repo",
# "rebase first") uses ``operator="<who: why>"`` instead: coverage waived for
# that call, an ``operator_override`` event on the closed run. Reviewer runs
# keep the gate; non-operator profiles are refused.

OPERATOR = "Ace via Apollo: wrong repo, re-port onto hermes-home"


def _operator_overrides(conn, tid):
    return [
        (payload, run_id) for kind, payload, run_id in _kinds(conn, tid)
        if kind == "operator_override"
    ]


def test_operator_send_back_without_coverage_succeeds_and_is_recorded(
    board: Path,
) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        ok, detail = kb.request_changes(
            conn, tid, reason="rebase first", claimer="apollo",
            operator=OPERATOR,
        )
        assert (ok, detail) == (True, "builder")
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.assignee == "builder"
        events = _kinds(conn, tid)
        after = events[[k for k, _, _ in events].index("review_requested") + 1:]
        assert [k for k, _, _ in after] == [
            "claimed", "changes_requested", "operator_override",
        ]
        run_id = after[0][2]
        assert after[1][1]["operator"] == OPERATOR
        overrides = _operator_overrides(conn, tid)
        assert len(overrides) == 1
        payload, ev_run = overrides[0]
        assert ev_run == run_id
        assert payload["action"] == "request-changes"
        assert payload["reason"] == OPERATOR
        assert payload["coverage_waived"] is True
        assert payload["by_profile"] == "apollo"
        assert not [
            c for c in kb.list_comments(conn, tid)
            if c.body.startswith("review_coverage:")
        ]


@pytest.mark.parametrize("profile", ["argus", "daedalus"])
def test_operator_send_back_refused_for_non_operator_profile(
    board: Path, monkeypatch: pytest.MonkeyPatch, profile: str,
) -> None:
    monkeypatch.setenv("HERMES_PROFILE", profile)
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="rebase first", claimer=profile,
            operator=OPERATOR,
        )
        assert ok is False
        assert "--operator is for operator profiles" in detail
        assert profile in detail
        _assert_untouched(conn, tid, before, runs_before)


@pytest.mark.parametrize("reason", ["just bounce it", ": no who", "Ace:  "])
def test_operator_send_back_needs_who_colon_why(board: Path, reason: str) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="rebase first", claimer="apollo", operator=reason,
        )
        assert ok is False and "<who: why>" in detail
        _assert_untouched(conn, tid, before, runs_before)


def test_reviewer_run_without_coverage_is_still_refused(
    board: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is unchanged for a dispatched reviewer: no coverage, no
    send-back -- and it cannot borrow --operator either."""
    with kb.connect() as conn:
        tid = _parked_review(conn)
        review = kb.claim_review_task(conn, tid, claimer="argus:1")
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", expected_run_id=review.current_run_id,
        )
        assert ok is False and detail == kb._REVIEW_COVERAGE_MISSING
        monkeypatch.setenv("HERMES_PROFILE", "argus")
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", expected_run_id=review.current_run_id,
            operator=OPERATOR,
        )
        assert ok is False and "--operator is for operator profiles" in detail
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        assert task.current_run_id == review.current_run_id
        assert not _operator_overrides(conn, tid)


def test_cli_operator_send_back_parked_review(
    board: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SESSION_ID", "20260927_090000_operator")
    with kb.connect() as conn:
        tid = _parked_review(conn)
    rc = kc._cmd_request_changes(argparse.Namespace(
        task_id=tid, reason=["rebase", "first"], coverage=None,
        operator=OPERATOR,
    ))
    assert rc == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert len(_operator_overrides(conn, tid)) == 1
        rework = [
            c.body for c in kb.list_comments(conn, tid)
            if c.body.startswith("changes requested (operator send-back,")
        ]
        assert rework == [
            f"changes requested (operator send-back, {OPERATOR}): rebase first"
        ]


# --- operator_kind (t_86ca5b3d) ---------------------------------------------
# A landing gate must tell a human CHANGES REQUESTED (holds) from automation's
# send-back (never holds) by a FIELD on the changes_requested event, not by
# parsing the operator string.

def _changes_requested_payload(conn, tid):
    return [p for k, p, _ in _kinds(conn, tid) if k == "changes_requested"][-1]


def test_operator_send_back_defaults_to_human_kind(board: Path) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        ok, _ = kb.request_changes(
            conn, tid, reason="rebase first", claimer="apollo", operator=OPERATOR,
        )
        assert ok
        assert _changes_requested_payload(conn, tid)["operator_kind"] == "human"


def test_operator_send_back_records_machine_kind(board: Path) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        ok, _ = kb.request_changes(
            conn, tid, reason="HOLD-FR", claimer="apollo",
            operator="merge-pass: prism soft gate HOLD f-1", operator_kind="machine",
        )
        assert ok
        payload = _changes_requested_payload(conn, tid)
        assert payload["operator_kind"] == "machine"
        assert payload["operator"] == "merge-pass: prism soft gate HOLD f-1"


@pytest.mark.parametrize("kind,operator,why", [
    ("robot", OPERATOR, "operator_kind must be one of"),
    ("machine", None, "operator_kind needs --operator"),
])
def test_operator_kind_refused_before_any_write(
    board: Path, kind: str, operator, why: str,
) -> None:
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
        ok, detail = kb.request_changes(
            conn, tid, reason="x", claimer="apollo", operator=operator, operator_kind=kind,
        )
        assert ok is False and why in detail
        _assert_untouched(conn, tid, before, runs_before)


def test_reviewer_send_back_event_has_no_operator_kind(board: Path) -> None:
    """Only operator send-backs carry the field: a reviewer verdict is unchanged."""
    with kb.connect() as conn:
        tid = _parked_review(conn)
        review = kb.claim_review_task(conn, tid, claimer="argus:1")
        kb.add_comment(conn, tid, "argus", "review_coverage: " + COVERAGE,
                       run_id=review.current_run_id)
        ok, _ = kb.request_changes(conn, tid, reason="fix", expected_run_id=review.current_run_id)
        assert ok
        assert "operator_kind" not in _changes_requested_payload(conn, tid)


def test_cli_operator_kind_machine_reaches_the_event(
    board: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SESSION_ID", "20260927_090000_operator")
    with kb.connect() as conn:
        tid = _parked_review(conn)
    top = argparse.ArgumentParser()
    kp.build_parser(top.add_subparsers(dest="cmd"))
    ns = top.parse_args([
        "kanban", "request-changes", tid, "HOLD-FR",
        "--operator", "merge-pass: prism soft gate HOLD f-1", "--operator-kind", "machine",
    ])
    assert getattr(ns, "operator_kind", None) == "machine"
    rc = kc._cmd_request_changes(ns)
    assert rc == 0
    with kb.connect() as conn:
        assert _changes_requested_payload(conn, tid)["operator_kind"] == "machine"


def test_cli_without_operator_still_needs_coverage(
    board: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SESSION_ID", "20260927_090000_operator")
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
    rc = kc._cmd_request_changes(argparse.Namespace(
        task_id=tid, reason=["rebase", "first"], coverage=None,
    ))
    assert rc == 1
    with kb.connect() as conn:
        _assert_untouched(conn, tid, before, runs_before)


def test_foreign_card_operator_send_back_records_one_event(board: Path) -> None:
    """On a foreign card the home guard also honours --operator; the send-back
    and the guard must not both write an operator_override for one call."""
    with kb.connect() as conn:
        tid = _parked_review(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ?",
                ("20260927_080000_homesess", tid),
            )
        with kb.mutation_actor(
            session_ids=("20260927_090000_callersess",), profile="apollo",
            operator=OPERATOR,
        ):
            ok, detail = kb.request_changes(
                conn, tid, reason="rebase first", claimer="apollo",
                operator=OPERATOR,
            )
        assert (ok, detail) == (True, "builder")
        overrides = _operator_overrides(conn, tid)
        assert len(overrides) == 1
        assert overrides[0][0]["coverage_waived"] is True


def test_dashboard_operator_send_back(
    board: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plugins.kanban.dashboard import plugin_api

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    client = TestClient(app)
    with kb.connect() as conn:
        tid = _parked_review(conn)
        before, runs_before = _snapshot(conn, tid)
    url = f"/api/plugins/kanban/tasks/{tid}/request-changes"
    monkeypatch.setenv("HERMES_PROFILE", "argus")
    refused = client.post(url, json={
        "reason": "rebase first", "author": "argus", "operator": OPERATOR,
    })
    assert refused.status_code == 409
    assert "--operator is for operator profiles" in refused.json()["detail"]
    with kb.connect() as conn:
        _assert_untouched(conn, tid, before, runs_before)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    ok = client.post(url, json={
        "reason": "rebase first", "author": "apollo", "operator": OPERATOR,
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["implementer"] == "builder"
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert len(_operator_overrides(conn, tid)) == 1


def test_claimer_without_run_id_cannot_close_another_reviewers_live_run(board: Path) -> None:
    """C5 #70 (PR #1081): argus holds a live review claim; an apollo send-back
    with no expected_run_id must be refused, leaving argus's run open."""
    with kb.connect() as conn:
        tid = _parked_review(conn)
        review = kb.claim_review_task(conn, tid, claimer="argus:1")
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="apollo", coverage=COVERAGE,
        )
        assert ok is False and "live review run" in detail
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.current_run_id == review.current_run_id
        # A matching profile name is not proof of run ownership: refused too.
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="argus:1", coverage=COVERAGE,
        )
        assert ok is False and "live review run" in detail
        # The run's owner closes it with its run id.
        ok, detail = kb.request_changes(
            conn, tid, reason="fix", claimer="argus:1", coverage=COVERAGE,
            expected_run_id=review.current_run_id,
        )
        assert (ok, detail) == (True, "builder")
