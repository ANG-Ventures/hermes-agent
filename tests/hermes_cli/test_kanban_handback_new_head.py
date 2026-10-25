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


def _f(path, *lines, at=1, fn="def f():"):
    """A files[] entry: one hunk at offset ``at`` (function context ``fn``) carrying ``lines``."""
    return {"filename": path, "status": "modified",
            "patch": f"@@ -{at},3 +{at},4 @@ {fn}\n ctx\n" + "\n".join(lines)}


PR_FILES = [_f("app.py", "-old = 1", "+new = 2"), _f("tests/test_app.py", "+def test_new(): ...")]
MODES = {"app.py": ["100644", "100644"], "tests/test_app.py": ["100644", "100644"]}


def _view(head, head_files, status="ahead", head_modes=MODES, seen_modes=MODES):
    return {"head": head, "status": status, "seen_files": PR_FILES, "head_files": head_files,
            "seen_modes": seen_modes, "head_modes": head_modes}

# The PR's GitHub state after the send-back, keyed by scenario. ``status`` is compare/<A>...<head>;
# ``seen_files`` is compare/<base>...<A>, ``head_files`` is pulls/N/files.
VIEWS = {
    "unchanged": {"head": A},
    # fold bot merged main; the PR's own lines are the same (fixture 1).
    "fold_merge": _view(B, PR_FILES),
    # main changed app.py outside the PR's hunk: offsets shift, lines unchanged (fixture 3).
    "fold_offset_shift": _view(B, [_f("app.py", "-old = 1", "+new = 2", at=40), PR_FILES[1]]),
    # main chmod'ed app.py on both ends: the PR's mode CHANGE is still none.
    "fold_main_chmod": _view(B, PR_FILES, head_modes=dict(MODES, **{"app.py": ["100755", "100755"]})),
    # a merge that resolves a conflict inside a PR-touched file (live: t_c9887522 44e346d675; fixture 2).
    "fold_conflict_resolution": _view(B, [_f("app.py", "-old = 1", "+new = 2", "+resolved = 3"), PR_FILES[1]]),
    # the live shape (t_c9887522 44e346d675): the resolution keeps main's adjacent insert next to the PR's
    # line, so the +/- lines are equal and only the context around the PR's hunk changed.
    "fold_adjacent_insert": _view(B, [{"filename": "app.py", "status": "modified",
                                       "patch": "@@ -1,3 +1,5 @@ def f():\n ctx\n main_insert = 1\n"
                                                "-old = 1\n+new = 2"}, PR_FILES[1]]),
    # the same edit moved to an identical-context block in another function (Prism 8b66a52674df).
    "moved_region": _view(B, [_f("app.py", "-old = 1", "+new = 2", at=90, fn="def g():"), PR_FILES[1]]),
    # app.py's text delta is unchanged but the PR now also makes it executable (Prism e8f58301f565).
    "chmod_only": _view(B, PR_FILES, head_modes=dict(MODES, **{"app.py": ["100644", "100755"]})),
    # same patch text, but the file is now a rename rather than a modify.
    "status_change": _view(B, [dict(PR_FILES[0], status="renamed", previous_filename="old_app.py"),
                               PR_FILES[1]]),
    # modes unreadable (tree truncated / REST fault): cannot be proved.
    "modes_unknown": _view(B, PR_FILES, head_modes=None),
    # file list at the REST cap: cannot be proved (fixture 4).
    "over_cap": _view(B, [_f(f"f{i}.py", "+x") for i in range(300)]),
    # a file with no patch (binary / too large): cannot be proved.
    "patchless": _view(B, [{"filename": "app.py", "status": "modified"}, PR_FILES[1]]),
    # worker reset the PR to A^: head does not descend from A (Prism cc70026dd515).
    "rewound": _view(C, PR_FILES, status="behind"),
    "new_commit": _view(C, [_f("app.py", "-old = 1", "+new = 3"), PR_FILES[1]]),
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


@pytest.mark.parametrize("scenario", ["unchanged", "fold_merge", "fold_offset_shift", "fold_main_chmod"])
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
    "new_commit", "fold_conflict_resolution", "fold_adjacent_insert", "moved_region", "chmod_only",
    "status_change", "modes_unknown", "over_cap", "patchless", "rewound"])
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
