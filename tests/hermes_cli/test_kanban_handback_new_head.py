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
