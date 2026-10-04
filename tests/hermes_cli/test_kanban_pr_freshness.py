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


@pytest.fixture(autouse=True)
def _fixture_owner_is_fleet(monkeypatch):
    """The ``o/r`` fixture repo stands in for a fleet repo (t_06dccfe3)."""
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o"})


class FakeGh:
    def __init__(self, *, draft=False, behind=0, checks=("success",), status="success", ms="clean"):
        self.draft, self.behind, self.checks, self.status, self.ms = draft, behind, checks, status, ms
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        path = args[-1] if not args[0].startswith("-") else args[2]
        if args[0] == "-X":
            return {"message": "Updating pull request branch."}
        if path.endswith("/pulls/5"):
            return {"draft": self.draft, "head": {"sha": HEAD}, "base": {"ref": "main"},
                    "mergeable_state": self.ms}
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
    gh = FakeGh(behind=21, ms="behind")
    armed = []
    rep = fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh, arm=lambda *a: armed.append(a))
    assert gh.updates() == [("-X", "PUT", "repos/o/r/pulls/5/update-branch",
                             "-f", f"expected_head_sha={HEAD}")]
    assert rep["prs"]["o/r#5"]["update_branch"] == "requested"
    assert armed == []  # the old head must never be armed


def test_behind_without_strict_rule_is_not_updated_and_armed():
    # t_39a33e70: far behind but GitHub does not block on it (no strict up-to-date rule):
    # the head merges as is, so no update-branch push; a green head is armed like a fresh one.
    gh, armed = FakeGh(behind=40, ms="blocked"), []
    rep = fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh, arm=lambda *a: armed.append(a) or "log")
    assert gh.updates() == []
    assert "update_branch" not in rep["prs"]["o/r#5"]
    assert armed == [("o/r", 5, HEAD, "t_x")]


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


def test_foreign_pr_is_never_updated_or_armed():
    """t_06dccfe3: the gate never touches a PR the fleet does not own."""
    gh, armed = FakeGh(draft=True, behind=50), []
    refs = op.extract_pr_refs("https://github.com/stephenschoettler/hermes-lcm/pull/5")
    rep = fr.check(refs, task_id="t_x", allow_arm=True, gh=gh, arm=lambda *a: armed.append(a))
    assert rep["prs"] == {} and gh.calls == [] and armed == []


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
    gh, armed = FakeGh(behind=40, ms="behind"), []
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
        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}",
                                metadata={"pr_url": PR_URL}, expected_run_id=run)
        mid, mrun = _claimed(conn, title="[milestone] big thing")
        assert kb.complete_task(conn, mid, summary=f"done {PR_URL}",
                                metadata={"pr_url": PR_URL}, expected_run_id=mrun)
    assert armed == [(REPO, 5, HEAD, tid)]


def test_e2e_pr_only_mentioned_in_prose_is_routed_but_never_armed(board, monkeypatch):
    """FleetReview #1234: a green fleet PR named only in summary prose (context,
    not this card's handoff) routes the card to review but is NOT handed to
    fleet-merge.sh. Only metadata.pr_url / --survivor-pr authorize arming."""
    armed = []
    _use_gh(monkeypatch, FakeGh(), armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(conn, tid, summary=f"background: see {PR_URL}",
                                expected_run_id=run)
        assert _status(conn, tid) == "review"
        meta = kb.latest_run(conn, tid).metadata
        assert "not armed" in meta["handoff_freshness"]["prs"]["o/r#5"]["automerge"]
    assert armed == []


# --- audited per-card draft override (t_f38605be) ------------------------


def _events(conn, tid, kind):
    import json
    return [json.loads(r["payload"]) if r["payload"] else None for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (tid, kind))]


def test_draft_ok_reports_override_and_skips_mutation():
    gh = FakeGh(draft=True, behind=50)
    armed = []
    rep = fr.check(_refs(), task_id="t_x", allow_arm=True, gh=gh,
                   arm=lambda *a: armed.append(a), draft_ok=True)
    assert rep["draft_override"] == ["o/r#5"]
    assert gh.updates() == [] and armed == []


def test_e2e_draft_refused_without_override_completes_with_it(board, monkeypatch):
    _use_gh(monkeypatch, FakeGh(draft=True), [])
    reason = "CI vehicle for upstream PR up/r#638; intentionally left draft"
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(fr.DraftPrError) as exc:
            kb.complete_task(conn, tid, summary=f"done {PR_URL}", expected_run_id=run)
        assert "draft_ok" in str(exc.value)
        assert _status(conn, tid) == "running"
        assert _events(conn, tid, "completion_draft_override") == []

        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}",
                                expected_run_id=run, draft_ok=reason) is True
        assert _status(conn, tid) == "done"
        assert _events(conn, tid, "completion_draft_override") == [
            {"prs": ["o/r#5"], "reason": reason}]
        assert _events(conn, tid, "completion_routed_to_review") == []
        meta = kb.latest_run(conn, tid).metadata
        assert meta["draft_override"] == {"prs": ["o/r#5"], "reason": reason}


def test_e2e_empty_draft_ok_refused_before_any_gate(board, monkeypatch):
    _use_gh(monkeypatch, FakeGh(draft=True), [])
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        for blank in ("", "   "):
            with pytest.raises(kb.EmptyDraftOverrideError):
                kb.complete_task(conn, tid, summary=f"done {PR_URL}",
                                 expected_run_id=run, draft_ok=blank)
        assert _status(conn, tid) == "running"
        assert len(_events(conn, tid, "completion_blocked_empty_draft_override")) == 2
        assert _events(conn, tid, "completion_blocked_draft_pr") == []


def test_e2e_draft_ok_still_routes_other_open_prs(board, monkeypatch):
    """The override drops ONLY the drafts; a non-draft open PR still routes to review."""
    def gh(*args):
        path = args[-1] if not args[0].startswith("-") else args[2]
        if path.endswith("/pulls/5"):
            return {"draft": True, "head": {"sha": HEAD}, "base": {"ref": "main"}}
        if path.endswith("/pulls/6"):
            return {"draft": False, "head": {"sha": HEAD}, "base": {"ref": "main"}}
        if "/compare/" in path:
            return {"behind_by": 0, "ahead_by": 1}
        return None
    _use_gh(monkeypatch, gh, [])
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.complete_task(
            conn, tid, summary=f"done {PR_URL} and https://github.com/o/r/pull/6",
            expected_run_id=run, draft_ok="vehicle") is True
        assert _status(conn, tid) == "review"
        assert _events(conn, tid, "completion_draft_override") == [
            {"prs": ["o/r#5"], "reason": "vehicle"}]
        routed = _events(conn, tid, "completion_routed_to_review")
        assert routed and routed[0]["open_prs"] == ["o/r#6"]


def _cli(rest):
    import argparse, contextlib, io, shlex
    from hermes_cli import kanban as kc
    wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
    parser = kc.build_parser(wrap.add_subparsers(dest="_top"))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = kc.kanban_command(parser.parse_args(shlex.split(rest)))
    return rc, out.getvalue(), err.getvalue()


def test_cli_draft_ok_flag_refused_then_audited(board, monkeypatch):
    _use_gh(monkeypatch, FakeGh(draft=True), [])
    with kb.connect() as conn:
        tid, _ = _claimed(conn)
    rc, _, err = _cli(f"complete {tid} --summary 'done {PR_URL}'")
    assert rc == 1 and "DRAFT PR" in err and "--draft-ok" in err
    rc, _, err = _cli(f"complete {tid} --summary 'done {PR_URL}' --draft-ok ' '")
    assert rc == 1 and "empty draft_ok" in err
    rc, out, _ = _cli(f"complete {tid} --summary 'done {PR_URL}' --draft-ok 'CI vehicle'")
    assert rc == 0, out
    assert f"Completed {tid}" in out and "draft override recorded for o/r#5: CI vehicle" in out
    with kb.connect() as conn:
        assert _status(conn, tid) == "done"
        assert _events(conn, tid, "completion_draft_override") == [
            {"prs": ["o/r#5"], "reason": "CI vehicle"}]
