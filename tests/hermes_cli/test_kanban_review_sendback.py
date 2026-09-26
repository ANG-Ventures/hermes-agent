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
from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES


def _coverage(**override) -> str:
    record = {
        "lenses": {name: "done" for name in REQUIRED_REVIEW_LENSES},
        "findings": 1,
        "items": ["F1 boundary test missing"],
        "review_minutes": 5,
        "batch_id": "apollo-r1-sendback",
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


def test_tool_request_changes_sends_back_parked_review(board: Path) -> None:
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
    board: Path, monkeypatch: pytest.MonkeyPatch,
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


def test_tool_send_back_without_coverage_is_refused(board: Path) -> None:
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
