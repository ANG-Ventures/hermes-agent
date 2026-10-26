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


def _f(path, sha, status="modified", **kw):
    """A compare files[] entry: the file's blob sha at the tip of that side."""
    return dict({"filename": path, "status": status, "sha": sha}, **kw)


APP_A, APP_C, APP_M = "1" * 40, "2" * 40, "3" * 40   # app.py blob at A; after an implementer edit; after a main edit
TEST_A = "4" * 40
PR_FILES = [_f("app.py", APP_A), _f("tests/test_app.py", TEST_A, status="added")]
MODES = {"app.py": ["100644", "100644"], "tests/test_app.py": [None, "100644"]}
MAIN_OTHER = [_f("docs/unrelated.md", "5" * 40)]     # main moved, touching no PR path


def _view(head, head_files, status="ahead", head_modes=MODES, seen_modes=MODES,
          main_status="ahead", main_files=MAIN_OTHER):
    return {"head": head, "status": status, "seen_files": PR_FILES, "head_files": head_files,
            "seen_modes": seen_modes, "head_modes": head_modes,
            "main_status": main_status, "main_files": main_files}

# The PR's GitHub state after the send-back, keyed by scenario. ``status`` is compare/<A>...<head>;
# ``seen_files``/``head_files`` are compare/<base>...<A|head>; ``main_*`` is compare/<mb_seen>...<mb_head>.
VIEWS = {
    "unchanged": {"head": A},
    # fold bot merged main; main touched no PR path and every PR blob is byte-identical (fixture 2).
    "fold_merge": _view(B, PR_FILES),
    # fold onto an unchanged merge base (a re-merge): nothing on main moved.
    "fold_same_base": _view(B, PR_FILES, main_status="identical", main_files=[]),
    # Prism cdd568482ca1: H2 reverts the edit in block 1 and applies it in identical block 2 of the same
    # function. The normalised patch text was equal; the blob is not (fixture 1).
    "relocated_edit": _view(B, [_f("app.py", APP_C), PR_FILES[1]]),
    # a merge resolving a conflict inside a PR file (live: t_c9887522 44e346d675).
    "fold_conflict_resolution": _view(B, [_f("app.py", APP_M), PR_FILES[1]],
                                      main_files=[_f("app.py", APP_M)]),
    # fixture 3, the accepted conservative cost: main touched a PR path and the fold auto-merged cleanly.
    # Even with byte-equal PR blobs a hand resolution cannot be excluded, so it routes.
    "fold_main_touched_pr_path": _view(B, PR_FILES, main_files=[_f("app.py", APP_M)]),
    # main renamed something away from a PR path: previous_filename counts as touching it.
    "fold_main_renamed_pr_path": _view(B, PR_FILES, main_files=[
        _f("lib/app.py", APP_M, status="renamed", previous_filename="app.py")]),
    "main_unreadable": _view(B, PR_FILES, main_status=None, main_files=None),
    "main_over_cap": _view(B, PR_FILES, main_files=[_f(f"m{i}.py", "6" * 40) for i in range(300)]),
    # app.py's blob is unchanged but the PR now also makes it executable (Prism e8f58301f565).
    "chmod_only": _view(B, PR_FILES, head_modes=dict(MODES, **{"app.py": ["100644", "100755"]})),
    # same blob, but the file is now a rename rather than a modify.
    "status_change": _view(B, [dict(PR_FILES[0], status="renamed", previous_filename="old_app.py"),
                               PR_FILES[1]]),
    # modes unreadable (tree truncated / REST fault): cannot be proved.
    "modes_unknown": _view(B, PR_FILES, head_modes=None),
    # file list at the REST cap: cannot be proved.
    "over_cap": _view(B, [_f(f"f{i}.py", "7" * 40) for i in range(300)]),
    # a file with no blob sha: cannot be proved.
    "shaless": _view(B, [{"filename": "app.py", "status": "modified"}, PR_FILES[1]]),
    # worker reset the PR to A^: head does not descend from A (Prism cc70026dd515).
    "rewound": _view(C, PR_FILES, status="behind"),
    "new_commit": _view(C, [_f("app.py", APP_C), PR_FILES[1]]),
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


@pytest.mark.parametrize("scenario", ["unchanged", "fold_merge", "fold_same_base"])
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


@pytest.mark.parametrize("scenario", [
    "new_commit", "relocated_edit", "fold_conflict_resolution", "fold_main_touched_pr_path",
    "fold_main_renamed_pr_path", "main_unreadable", "main_over_cap", "chmod_only", "status_change",
    "modes_unknown", "over_cap", "shaless", "rewound"])
def test_content_the_reviewer_has_not_seen_routes_to_review(sent_back, monkeypatch, scenario):
    status, kinds, _ = _complete_as_worker(sent_back, monkeypatch, scenario)
    assert status == "review"
    assert "review_requested" in kinds and nh.EVENT not in kinds


def test_bounce_is_bounded_per_send_back(sent_back, monkeypatch):
    """A worker that cannot move the PR still reaches the reviewer after MAX_BOUNCES."""
    for _ in range(nh.MAX_BOUNCES):
        assert _complete_as_worker(sent_back, monkeypatch, "unchanged")[0] == "ready"
    status, kinds, _ = _complete_as_worker(sent_back, monkeypatch, "unchanged")
    assert status == "review" and "review_requested" in kinds


def test_request_review_skip_path_does_not_report_a_bounce_as_completed(sent_back, monkeypatch):
    """Prism da56fe12b0d6/6dc02dcaa12e: review_policy=none must not read a bounce as success."""
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "none")
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back)
        ok, note = kb.request_review(conn, sent_back, summary=f"r2 {PR}",
                                     expected_run_id=run.current_run_id, with_reason=True)
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (sent_back,))]
        status = kb.get_task(conn, sent_back).status
    assert ok and note.startswith("NOT handed back: no new head since")
    assert status == "ready"
    assert "review_skipped" not in kinds and nh.EVENT in kinds


def test_bounced_note_reads_the_bounced_run_not_a_successor(sent_back, monkeypatch):
    """Prism bec5d5358114: a successor claimed right after the bounce must not hide it."""
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    with kb.connect() as conn:
        bounced = kb.claim_task(conn, sent_back).current_run_id
        assert kb.complete_task(conn, sent_back, summary=f"r2 {PR}", expected_run_id=bounced)
        successor = kb.claim_task(conn, sent_back).current_run_id
        assert successor != bounced
        assert nh.bounced_note(conn, sent_back, bounced).startswith("NOT handed back: no new head since")
        assert nh.bounced_note(conn, sent_back, successor) is None


def test_a_bounce_never_arms_the_sent_back_head(sent_back, monkeypatch):
    """Prism 6dc02dcaa12e: the freshness gate must not arm fleet-merge on a completion that bounces."""
    from hermes_cli import kanban_pr_freshness as fresh
    seen_allow = []

    def check(refs, *, allow_arm, **kw):
        seen_allow.append(allow_arm)
        return {}

    monkeypatch.setattr(fresh, "check", check)
    assert _complete_as_worker(sent_back, monkeypatch, "unchanged")[0] == "ready"
    assert seen_allow == [False]
    assert _complete_as_worker(sent_back, monkeypatch, "new_commit")[0] == "review"
    assert seen_allow == [False, True]


def test_a_freshness_update_voids_the_no_op_verdict(sent_back, monkeypatch):
    """Prism 4f5d7679ba58 / 6dc02dcaa12e: the PR is at the sent-back head but behind main; the freshness gate
    update-branches it (merging a main change to a PR path). The verdict judged the old head, so it routes."""
    from hermes_cli import kanban_pr_freshness as fresh
    key = "ANG-Ventures/example-home#670"
    monkeypatch.setattr(fresh, "check", lambda refs, **kw: {
        "prs": {key: {"head": A, "behind_by": 21, "update_branch": "requested"}}})
    status, kinds, _ = _complete_as_worker(sent_back, monkeypatch, "unchanged")
    assert status == "review" and "review_requested" in kinds and nh.EVENT not in kinds


def test_a_head_the_gate_saw_differently_voids_the_verdict(sent_back, monkeypatch):
    from hermes_cli import kanban_pr_freshness as fresh
    monkeypatch.setattr(fresh, "check", lambda refs, **kw: {
        "prs": {"ANG-Ventures/example-home#670": {"head": C}}})
    assert _complete_as_worker(sent_back, monkeypatch, "unchanged")[0] == "review"


def test_a_verdict_names_the_heads_it_judged():
    no_new = {"seen": A, "reason": "unchanged", "prs": {}, "heads": {"ANG-Ventures/example-home#670": A}}
    assert nh.still_holds(no_new, {"prs": {"ANG-Ventures/example-home#670": {"head": A, "behind_by": 0}}})
    assert nh.still_holds(no_new, {}) is no_new


def test_a_shared_7_char_prefix_is_not_the_reviewed_head(sent_back, monkeypatch):
    """Prism c804beeb6820: the coverage names an abbreviated head; a new commit sharing it routes."""
    short = A[:7]
    collide = short + "f" * 33
    with kb.connect() as conn:
        kb.add_comment(conn, sent_back, "argus", _coverage(short))
    VIEWS["collide"] = {"head": collide}
    VIEWS["resolved_same"] = {"head": A, "seen": A}
    try:
        assert nh.classify({"head": collide}, short) is None
        assert nh.classify({"head": collide, "seen": A}, short) is None
        assert nh.classify({"head": A, "seen": A}, short) == "unchanged"
        assert nh.classify({"head": A}, short) is None   # unresolved abbreviation: fail open
        assert _complete_as_worker(sent_back, monkeypatch, "collide")[0] == "review"
    finally:
        VIEWS.pop("collide")
        VIEWS.pop("resolved_same")


def test_a_runless_completion_never_reads_a_historical_bounce(sent_back, monkeypatch):
    """Prism a8fea1c5db96: an operator completing a card whose newest run is an old bounce gets no bounce note."""
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back).current_run_id
        assert kb.complete_task(conn, sent_back, summary=f"r2 {PR}", expected_run_id=run)
        assert kb.latest_run(conn, sent_back).outcome == nh.OUTCOME
        assert nh.bounced_note(conn, sent_back, None) is None
        assert nh.bounce(conn, sent_back, {"seen": A}, expected_run_id=None) is False


def test_request_review_reports_its_own_bounce_when_a_successor_reached_review(sent_back, monkeypatch):
    """Prism 35bc66834b1d: the card's status after the call is not this invocation's outcome."""
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "none")
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    monkeypatch.setattr(kb, "_task_status", lambda conn, tid: "review")  # a successor got there first
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back)
        ok, note = kb.request_review(conn, sent_back, summary=f"r2 {PR}",
                                     expected_run_id=run.current_run_id, with_reason=True)
    assert ok and note.startswith("NOT handed back")


def test_cli_reports_its_own_bounce_when_a_successor_claimed_the_card(sent_back, monkeypatch, capsys):
    """Prism a0341975cd2e: a successor claimed between the bounce and the print must not read as Completed."""
    import argparse
    from hermes_cli import kanban as kcli

    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    real_bounce = nh.bounce

    def bounce_then_successor(conn, tid, *a, **kw):
        ok = real_bounce(conn, tid, *a, **kw)
        kb.claim_task(conn, tid)
        return ok

    monkeypatch.setattr(nh, "bounce", bounce_then_successor)
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back).current_run_id
    monkeypatch.setattr(kcli, "_worker_run_id_for", lambda tid: run)
    ns = argparse.Namespace(task_ids=[sent_back], result=None, summary=f"r2 {PR}", metadata=None,
                            force=False, superseded_by=None, draft_ok=None, external=None, watcher=None,
                            survivor_ref=None, survivor_pr=None, survivor_unbound=None,
                            survivor_none=False, reason=None)
    assert kcli._cmd_complete(ns) == 0
    out = capsys.readouterr().out
    assert "NOT handed back" in out and f"Completed {sent_back}" not in out


def test_the_bounce_redacts_the_handoff_like_request_review(sent_back, monkeypatch):
    """Prism cde5d0610ef1: a bounced run row feeds successor prompts; credentials must not survive into it."""
    token = "ghp_" + "Z" * 36
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back).current_run_id
        assert kb.complete_task(conn, sent_back, summary=f"used {token}; {PR}",
                                metadata={"password": "hunter2", "note": f"tok {token}"},
                                expected_run_id=run)
        ended = kb.get_run(conn, run)
        context = kb.build_worker_context(conn, sent_back)
    assert ended.outcome == nh.OUTCOME
    assert token not in ended.summary and token not in json.dumps(ended.metadata)
    assert "hunter2" not in json.dumps(ended.metadata)
    assert token not in context and "hunter2" not in context


def test_a_worker_posted_coverage_does_not_redefine_the_seen_head(sent_back, monkeypatch):
    """Prism e76f9616eb30: the worker pushes C and posts a coverage-shaped comment naming C from its own run."""
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: (
        {"head": C} if seen == C else VIEWS["new_commit"]))
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back).current_run_id
        kb.add_comment(conn, sent_back, "builder", _coverage(C), run_id=run)
        assert nh.seen_head(conn, sent_back, nh._pending_send_back(conn, sent_back)) == A
        assert kb.complete_task(conn, sent_back, summary=f"r2 {PR}", expected_run_id=run)
        assert kb.get_task(conn, sent_back).status == "review"


def test_a_same_second_runless_coverage_does_not_replace_the_reviewers(sent_back, monkeypatch):
    """Prism 6ecbccbd1579: a runless comment naming C, stamped in the send-back's own second, is not "seen"."""
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: (
        {"head": C} if seen == C else VIEWS["new_commit"]))
    with kb.connect() as conn:
        send_back = nh._pending_send_back(conn, sent_back)
        cid = kb.add_comment(conn, sent_back, "builder", _coverage(C))
        at = conn.execute("SELECT created_at FROM task_events WHERE id = ?", (send_back,)).fetchone()[0]
        conn.execute("UPDATE task_comments SET created_at = ? WHERE id = ?", (at, cid))
        conn.commit()
        assert nh.seen_head(conn, sent_back, send_back) == A
        run = kb.claim_task(conn, sent_back).current_run_id
        assert kb.complete_task(conn, sent_back, summary=f"r2 {PR}", expected_run_id=run)
        assert kb.get_task(conn, sent_back).status == "review"


def test_only_operator_proven_runless_coverage_counts(tmp_path, monkeypatch):
    """A runless coverage posted BEFORE the send-back counts only under an operator label, never unverified:*."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(op, "_default_query", lambda: lambda repo, n: {"state": "OPEN"})
    monkeypatch.setattr(kb, "_operator_caller_profiles", lambda: frozenset({"apollo"}))
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder")
        run = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary=f"r1 {PR}", expected_run_id=run.current_run_id)
        assert kb.claim_review_task(conn, tid)
        kb.add_comment(conn, tid, "apollo", _coverage(A))            # operator, runless: counts
        kb.add_comment(conn, tid, "unverified:apollo", _coverage(C))  # unproven claim: refused
        kb.add_comment(conn, tid, "builder", _coverage(C))            # runless implementer label: refused
        assert kb.request_changes(conn, tid, reason="BEHAVIOUR: build r7", operator="apollo: test")[0]
        assert nh.seen_head(conn, tid, nh._pending_send_back(conn, tid)) == A


def test_a_voided_verdict_is_armed_like_any_handback(sent_back, monkeypatch):
    """Prism 188c162a38e9: the gate ran unarmed for the verdict; when still_holds() voids it the completion
    routes to review, and that PR is armed as any review handback is."""
    from hermes_cli import kanban_pr_freshness as fresh
    key = "ANG-Ventures/example-home#670"
    calls = []

    def check(refs, *, allow_arm, **kw):
        calls.append(allow_arm)
        entry = {"head": C}
        if allow_arm:
            entry["automerge"] = "fleet-merge spawned (log)"
        return {"prs": {key: entry}}

    monkeypatch.setattr(fresh, "check", check)
    status, kinds, _ = _complete_as_worker(sent_back, monkeypatch, "unchanged")
    assert status == "review" and nh.EVENT not in kinds
    assert calls == [False, True]
    with kb.connect() as conn:
        assert kb.latest_run(conn, sent_back).metadata["handoff_freshness"]["prs"][key]["automerge"] \
            .startswith("fleet-merge spawned")


def test_a_voided_verdict_on_an_updated_branch_is_not_rearmed(sent_back, monkeypatch):
    """The gate's update-branch voids the verdict; the new head's CI is fleet-merge's, as in check()."""
    from hermes_cli import kanban_pr_freshness as fresh
    calls = []

    def check(refs, *, allow_arm, **kw):
        calls.append(allow_arm)
        return {"prs": {"ANG-Ventures/example-home#670": {"head": A, "update_branch": "requested"}}}

    monkeypatch.setattr(fresh, "check", check)
    assert _complete_as_worker(sent_back, monkeypatch, "unchanged")[0] == "review"
    assert calls == [False]


def test_request_review_tool_reports_its_own_bounce_when_a_successor_claimed(sent_back, monkeypatch):
    """Prism 42d6d2f869f5: the tool's bounced receipt is this invocation's run, never a re-read of the card."""
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "configured_review_policy", lambda: "none")
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    monkeypatch.setattr(nh, "_default_reader", lambda: lambda repo, n, seen: VIEWS["unchanged"])
    with kb.connect() as conn:
        run = kb.claim_task(conn, sent_back).current_run_id
    real = kb.request_review

    def bounce_then_successor(conn, tid, *a, **kw):
        out = real(conn, tid, *a, **kw)
        kb.claim_task(conn, tid)
        return out

    monkeypatch.setattr(kb, "request_review", bounce_then_successor)
    monkeypatch.setattr(kt, "_worker_guard", lambda name, args: sent_back)
    monkeypatch.setattr(kt, "_worker_run_id", lambda tid: run)
    out = json.loads(kt._handle_request_review({"summary": f"r2 {PR}"}))
    with kb.connect() as conn:
        successor = kb.get_task(conn, sent_back)
    assert successor.status == "running" and successor.current_run_id != run
    assert out["ok"] and out["note"].startswith("NOT handed back")
    assert out["run_id"] == run and out["status"] == "ready"
