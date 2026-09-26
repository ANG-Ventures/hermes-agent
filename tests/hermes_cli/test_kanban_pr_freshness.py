"""Handoff freshness gate (t_14b81673): draft refused, stale head updated, green armed."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr

REPO = "o/r"
PR_URL = "https://github.com/o/r/pull/5"
HEAD = "a" * 40


class FakeGh:
    def __init__(self, *, draft=False, behind=0, checks=("success",), status="success"):
        self.draft, self.behind, self.checks, self.status = draft, behind, checks, status
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        path = args[-1] if not args[0].startswith("-") else args[2]
        if args[0] == "-X":
            return {"message": "Updating pull request branch."}
        if path.endswith("/pulls/5"):
            return {"draft": self.draft, "head": {"sha": HEAD}, "base": {"ref": "main"}}
        if "/compare/" in path:
            return {"behind_by": self.behind, "ahead_by": 1}
        if path.endswith("check-runs?per_page=100"):
            return {"check_runs": [{"status": "completed", "conclusion": c} for c in self.checks]}
        if path.endswith("/status"):
            return {"state": self.status, "total_count": 1}
        return None

    def updates(self):
        return [c for c in self.calls if c[:2] == ("-X", "PUT")]


def _refs():
    return op.extract_pr_refs(PR_URL)


# --- unit ----------------------------------------------------------------


def test_draft_is_refused_before_any_mutation():
    gh = FakeGh(draft=True, behind=50)
    armed = []
    with pytest.raises(fr.DraftPrError) as exc:
        fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh, arm=lambda *a: armed.append(a))
    assert exc.value.prs == ["o/r#5"]
    assert gh.updates() == [] and armed == []


def test_stale_head_is_updated_bound_to_measured_sha_and_not_armed():
    gh = FakeGh(behind=21)
    armed = []
    rep = fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh, arm=lambda *a: armed.append(a))
    assert gh.updates() == [("-X", "PUT", "repos/o/r/pulls/5/update-branch",
                             "-f", f"expected_head_sha={HEAD}")]
    assert rep["prs"]["o/r#5"]["update_branch"] == "requested"
    assert armed == []  # the old head must never be armed


def test_at_threshold_is_not_updated():
    gh = FakeGh(behind=20, checks=("failure",))
    fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh, arm=lambda *a: None)
    assert gh.updates() == []


def test_green_fresh_pr_is_armed_with_head_sha():
    gh = FakeGh(behind=3)
    armed = []
    rep = fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh,
                   arm=lambda *a: armed.append(a) or "/tmp/log")
    assert armed == [(REPO, 5, HEAD, "t_x")]
    assert "spawned" in rep["prs"]["o/r#5"]["automerge"]


@pytest.mark.parametrize("checks,status", [(("success", "in_progress"), "success"),
                                           (("failure",), "success"),
                                           ((), "success"),
                                           (("success",), "failure")])
def test_not_green_is_not_armed(checks, status):
    gh = FakeGh(checks=checks, status=status)
    armed = []
    fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh, arm=lambda *a: armed.append(a))
    assert armed == []


def test_milestone_and_kill_switches_do_not_arm(monkeypatch):
    armed = []
    fr.check(_refs(), task_id="t_x", allow_arm=False, gh=FakeGh(), arm=lambda *a: armed.append(a))
    monkeypatch.setenv("KANBAN_HANDOFF_AUTOMERGE", "0")
    fr.check(_refs(), task_id="t_x", allow_arm=True, gh=FakeGh(), arm=lambda *a: armed.append(a))
    assert armed == []
    monkeypatch.setenv("KANBAN_HANDOFF_FRESHNESS", "0")
    fr.check(_refs(), task_id="t_x", allow_arm=True, gh=FakeGh(draft=True), arm=None)  # no raise


def test_lookup_failure_fails_open():
    rep = fr.check(_refs(), task_id="t_x", allow_arm=True, gh=lambda *a: None, arm=None)
    assert "fail-open" in rep["prs"]["o/r#5"]["lookup"]


def test_default_gh_disabled_under_pytest():
    assert fr._default_gh() is None


# --- E2E through complete_task / request_review on a temp board ----------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, n: {"state": "OPEN"}))
    return home


def _use_gh(monkeypatch, gh, armed):
    monkeypatch.setattr(fr, "_default_gh", lambda: gh)
    monkeypatch.setattr(fr, "spawn_arm", lambda *a: armed.append(a) or "/tmp/log")


def _claimed(conn, title="slice"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def test_e2e_draft_pr_handoff_refused_card_stays_running(board, monkeypatch):
    armed = []
    _use_gh(monkeypatch, FakeGh(draft=True), armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(fr.DraftPrError):
            kb.complete_task(conn, tid, summary=f"done {PR_URL}", expected_run_id=run)
        assert _status(conn, tid) == "running"
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "completion_blocked_draft_pr" in kinds


def test_e2e_request_review_draft_refused(board, monkeypatch):
    _use_gh(monkeypatch, FakeGh(draft=True), [])
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(fr.DraftPrError):
            kb.request_review(conn, tid, summary=f"PR {PR_URL}", expected_run_id=run,
                              with_reason=True)
        assert _status(conn, tid) == "running"


def test_e2e_stale_head_updated_then_routed_to_review(board, monkeypatch):
    gh, armed = FakeGh(behind=40), []
    _use_gh(monkeypatch, gh, armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}", expected_run_id=run)
        assert _status(conn, tid) == "review"
        assert len(gh.updates()) == 1 and armed == []
        meta = kb.latest_run(conn, tid).metadata
        assert meta["handoff_freshness"]["prs"]["o/r#5"]["behind_by"] == 40


def test_e2e_green_slice_armed_milestone_not(board, monkeypatch):
    armed = []
    _use_gh(monkeypatch, FakeGh(), armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}", expected_run_id=run)
        mid, mrun = _claimed(conn, title="[milestone] big thing")
        assert kb.complete_task(conn, mid, summary=f"done {PR_URL}", expected_run_id=mrun)
    assert armed == [(REPO, 5, HEAD, tid)]
