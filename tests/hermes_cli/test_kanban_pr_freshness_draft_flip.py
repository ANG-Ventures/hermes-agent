"""Handback flips the worker's OWN draft ready via ace_gate.py (t_c1ee61ae); a foreign draft still refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr

PR_URL = "https://github.com/o/r/pull/5"
HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o"})
    cfg = tmp_path / "worker-pr-draft.json"
    cfg.write_text(json.dumps({"enabled": False, "repos": ["O/R"]}))  # flip keys on the repo list only
    monkeypatch.setenv("KANBAN_WORKER_PR_DRAFT", str(cfg))


def _gh(head_ref, *, draft=True):
    calls = []

    def gh(*args):
        calls.append(args)
        path = args[-1] if not args[0].startswith("-") else args[2]
        if args[0] == "-X":
            return {"message": "ok"}
        if path.endswith("/pulls/5"):
            return {"draft": draft, "head": {"sha": HEAD, "ref": head_ref}, "base": {"ref": "main"},
                    "mergeable_state": "clean"}
        if "/compare/" in path:
            return {"behind_by": 0, "ahead_by": 1}
        if path.endswith("check-runs?per_page=100"):
            return {"check_runs": [{"status": "completed", "conclusion": "skipped"}]}
        if path.endswith("/status"):
            return {"state": "success", "total_count": 1}
        return None
    gh.calls = calls
    return gh


def _refs():
    return op.extract_pr_refs(PR_URL)


def test_own_draft_is_flipped_ready_and_not_armed():
    ready, armed = [], []
    rep = fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"),
                   evaluate=lambda *a: armed.append(a) or "log", event_land=True, ready=lambda r, n: ready.append((r, n)) or 0)
    assert ready == [("o/r", 5)]
    assert rep["draft_flipped"] == ["o/r#5"]
    entry = rep["prs"]["o/r#5"]
    assert entry["draft"] is False and "rc=0" in entry["draft_flip"]
    # Draft-skipped check-runs read green; arming here would land on CI that never ran.
    assert armed == [] and "not armed" in entry["automerge"]


@pytest.mark.parametrize("head_ref", ["daedalus/t_deadbeef-other", "apollo/x", "feature/x", ""])
def test_foreign_draft_still_refused_and_never_flipped(monkeypatch, head_ref):
    # Seam via _default_ready (raising=False) so this also runs, and refuses, on the pre-change base.
    ready = []
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda r, n: ready.append((r, n)) or 0), raising=False)
    with pytest.raises(fr.DraftPrError):
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh(head_ref))
    assert ready == []


def test_own_draft_on_unlisted_repo_refused(monkeypatch, tmp_path):
    cfg = tmp_path / "other.json"
    cfg.write_text(json.dumps({"repos": ["o/other"]}))
    monkeypatch.setenv("KANBAN_WORKER_PR_DRAFT", str(cfg))
    with pytest.raises(fr.DraftPrError):
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"), ready=lambda r, n: 0)


@pytest.mark.parametrize("rc", [21, 20, 3, None])
def test_failed_flip_refuses(rc):
    """Review hold (21), Ace gate (20), read failure (3) or ace_gate missing (None): refuse as before."""
    with pytest.raises(fr.DraftPrError) as exc:
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"), ready=lambda r, n: rc)
    assert exc.value.prs == ["o/r#5"]


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("KANBAN_HANDOFF_DRAFT_FLIP", "0")
    ready = []
    with pytest.raises(fr.DraftPrError):
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"), ready=lambda r, n: ready.append(1) or 0)
    assert ready == []


def test_mixed_own_and_foreign_draft_refuses_before_any_flip():
    def gh(*args):
        path = args[-1]
        n = path.rsplit("/", 1)[-1]
        if "/pulls/" in path:
            ref = "daedalus/t_c1ee61ae-x" if n == "5" else "daedalus/t_deadbeef-y"
            return {"draft": True, "head": {"sha": HEAD, "ref": ref}, "base": {"ref": "main"}}
        return None
    ready = []
    with pytest.raises(fr.DraftPrError) as exc:
        fr.check(op.extract_pr_refs(f"{PR_URL} https://github.com/o/r/pull/6"), task_id="t_c1ee61ae",
                 allow_arm=True, gh=gh, ready=lambda r, n: ready.append(n) or 0)
    assert ready == [] and sorted(exc.value.prs) == ["o/r#5", "o/r#6"]


def _gh_two(drafts: dict, runs: list):
    """PRs 5 and 6, both on this card's branch; ``drafts[n]`` is the draft flag, ``runs`` the head check-runs."""
    def gh(*args):
        path = args[-1]
        if "/pulls/" in path:
            n = path.rsplit("/", 1)[-1]
            return {"draft": drafts[n], "head": {"sha": HEAD, "ref": "daedalus/t_c1ee61ae-x"},
                    "base": {"ref": "main"}, "mergeable_state": "clean"}
        if "/compare/" in path:
            return {"behind_by": 0, "ahead_by": 1}
        if path.endswith("check-runs?per_page=100"):
            return {"check_runs": runs}
        if path.endswith("/status"):
            return {"state": "success", "total_count": 1}
        return None
    return gh


SKIPPED = [{"status": "completed", "conclusion": "skipped"}]


def test_retry_after_partial_flip_does_not_arm_already_flipped_pr():
    """Prism 982040536d15: A flips (rc 0), B fails (rc 3) -> refused. On retry A is non-draft with
    only draft-skipped runs and B flips: A must still not be armed."""
    refs = op.extract_pr_refs(f"{PR_URL} https://github.com/o/r/pull/6")
    rcs = {5: 0, 6: 3}
    with pytest.raises(fr.DraftPrError) as exc:
        fr.check(refs, task_id="t_c1ee61ae", allow_arm=True, gh=_gh_two({"5": True, "6": True}, SKIPPED), ready=lambda r, n: rcs[n])
    assert exc.value.prs == ["o/r#6"]
    assert set(exc.value.flipped) == {"o/r#5"} and exc.value.flipped["o/r#5"]["head"] == HEAD
    armed = []
    rep = fr.check(refs, task_id="t_c1ee61ae", allow_arm=True, gh=_gh_two({"5": False, "6": True}, SKIPPED),
                   evaluate=lambda *a: armed.append(a) or "log", event_land=True, ready=lambda r, n: 0, flipped=exc.value.flipped)
    assert armed == []
    assert "no CI started after the ready flip" in rep["prs"]["o/r#5"]["automerge"]
    assert "flipped ready" in rep["prs"]["o/r#6"]["automerge"]


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


FLIP_AT = 1_793_116_000
PRE_READY_LINT = {"name": "lint", "status": "completed", "conclusion": "success",
                  "started_at": _iso(FLIP_AT - 600)}
SKIPPED_TESTS = {"name": "tests", "status": "completed", "conclusion": "skipped",
                 "started_at": _iso(FLIP_AT - 600)}
POST_READY_TESTS = {"name": "tests", "status": "completed", "conclusion": "success",
                    "started_at": _iso(FLIP_AT + 30)}
RECORD = {"o/r#5": {"head": HEAD, "at": FLIP_AT}}


def test_prism_d34048_pre_ready_lint_success_does_not_arm_flipped_pr():
    """Prism d34048d53f3c (a): a draft-time lint SUCCESS beside skipped tests is not post-ready CI.
    The flipped PR is armed only once a non-skipped run started after the flip appears."""
    armed = []
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_c1ee61ae", allow_arm=True,
                   gh=_gh_two({"5": False}, [PRE_READY_LINT, SKIPPED_TESTS]),
                   evaluate=lambda *a: armed.append(a) or "log", event_land=True, flipped=RECORD)
    assert armed == [] and "not armed" in rep["prs"]["o/r#5"]["automerge"]
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_c1ee61ae", allow_arm=True,
                   gh=_gh_two({"5": False}, [PRE_READY_LINT, POST_READY_TESTS]),
                   evaluate=lambda *a: armed.append(a) or "log", event_land=True, flipped=RECORD)
    assert len(armed) == 1 and "spawned" in rep["prs"]["o/r#5"]["automerge"]


def test_prism_d34048_flip_record_also_binds_a_non_card_branch():
    """The persisted record, not the branch name, is what binds: a recorded PR is gated even when
    its branch would not read as this card's own."""
    armed = []

    def gh(*args):
        out = _gh_two({"5": False}, [PRE_READY_LINT, SKIPPED_TESTS])(*args)
        if out and "head" in out:
            out["head"]["ref"] = "feature/x"
        return out
    fr.check(op.extract_pr_refs(PR_URL), task_id="t_c1ee61ae", allow_arm=True, gh=gh,
             evaluate=lambda *a: armed.append(a) or "log", event_land=True, flipped=RECORD)
    assert armed == []


def test_prism_d34048_none_then_success_race_does_not_arm():
    """Prism d34048d53f3c (b): the first check-runs read returns None, a second would succeed.
    One snapshot decides: unreadable is not evidence, so nothing arms."""
    reads = []
    green = _gh_two({"5": False}, [PRE_READY_LINT, POST_READY_TESTS])

    def gh(*args):
        if args[-1].endswith("check-runs?per_page=100"):
            reads.append(1)
            if len(reads) == 1:
                return None
        return green(*args)
    armed = []
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_c1ee61ae", allow_arm=True, gh=gh,
                   evaluate=lambda *a: armed.append(a) or "log", event_land=True, flipped=RECORD)
    assert armed == [] and "unreadable" in rep["prs"]["o/r#5"]["automerge"]
    assert len(reads) == 1


def test_own_pr_with_post_ready_ci_is_armed():
    armed = []
    runs = SKIPPED + [{"status": "completed", "conclusion": "success"}]
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_c1ee61ae", allow_arm=True,
                   gh=_gh_two({"5": False}, runs), evaluate=lambda *a: armed.append(a) or "log", event_land=True)
    assert len(armed) == 1 and "spawned" in rep["prs"]["o/r#5"]["automerge"]


def test_operator_redraft_after_flip_is_left_draft():
    """t_62389c4f (hermes-home#8702, 6x): this card flipped the PR ready at HEAD, and it is draft again at the
    SAME head. The worker's own re-draft (worker-pr-redraft.py) only runs right before a push, so no new head means
    someone else re-drafted it: leave it draft, do not refuse, do not arm."""
    ready, armed = [], []
    rep = fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"),
                   evaluate=lambda *a: armed.append(a) or "log", event_land=True, ready=lambda r, n: ready.append((r, n)) or 0,
                   flipped=RECORD)
    assert ready == [] and armed == []
    assert "o/r#5" in rep["draft_held"] and "left draft as-is" in rep["draft_held"]["o/r#5"]
    assert rep["prs"]["o/r#5"]["draft"] is True and "draft_flipped" not in rep


def test_worker_redraft_before_push_still_flips():
    """The worker's own re-draft comes with a new head (pre-push hook): today's rule, flip it ready."""
    ready = []
    rep = fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"), ready=lambda r, n: ready.append((r, n)) or 0,
                   flipped={"o/r#5": {"head": "b" * 40, "at": FLIP_AT}})
    assert ready == [("o/r", 5)] and rep["draft_flipped"] == ["o/r#5"] and "draft_held" not in rep


def test_default_ready_disabled_under_pytest():
    assert fr._default_ready() is None


def test_worker_draft_repos_absent_or_malformed(tmp_path):
    assert fr.worker_draft_repos(str(tmp_path / "nope.json")) == set()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert fr.worker_draft_repos(str(bad)) == set()


# --- E2E through complete_task on a temp board ---------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, n: {"state": "OPEN"}))
    return home


def _claimed(conn):
    tid = kb.create_task(conn, title="slice", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


def _wire(monkeypatch, head_ref_for, ready, armed):
    monkeypatch.setattr(fr, "_default_gh", lambda: _gh(head_ref_for()))
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda r, n: ready.append((r, n)) or 0),
                        raising=False)
    monkeypatch.setattr(fr, "spawn_evaluate", lambda *a: armed.append(a) or "/tmp/log")


def test_e2e_own_draft_flipped_and_routed_to_review(board, monkeypatch):
    ready, armed, tid_box = [], [], []
    _wire(monkeypatch, lambda: f"daedalus/{tid_box[0]}-fix", ready, armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        tid_box.append(tid)
        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}",
                                metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "review"
        meta = kb.latest_run(conn, tid).metadata
        assert meta["handoff_freshness"]["draft_flipped"] == ["o/r#5"]
    assert ready == [("o/r", 5)] and armed == []


def test_e2e_operator_redraft_survives_next_handback(board, monkeypatch):
    """t_62389c4f through complete_task: an earlier handback of this card flipped #5 ready at HEAD (its
    handoff_draft_flipped event); the operator re-drafted it with no push since. This handback leaves it draft,
    routes the card to review and names the hold in the review summary."""
    ready, armed, tid_box = [], [], []
    _wire(monkeypatch, lambda: f"daedalus/{tid_box[0]}-fix", ready, armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        tid_box.append(tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped", {"flipped": RECORD})
        assert kb.complete_task(conn, tid, summary=f"done again {PR_URL}", metadata={"pr_url": PR_URL},
                                expected_run_id=run)
        assert _status(conn, tid) == "review"
        latest = kb.latest_run(conn, tid)
        assert "o/r#5" in latest.metadata["handoff_freshness"]["draft_held"]
        assert "o/r#5: left draft as-is" in (latest.summary or "")
    assert ready == [] and armed == []


def test_e2e_legacy_round_draft_mark_does_not_lift_a_same_head_hold(board, monkeypatch):
    """t_d3da1a4e: the round draft is removed. An old ``round_draft_marked`` event on the card is ignored, so a
    same-head draft stays an operator hold even with a mark by this card's own run."""
    ready, armed, tid_box = [], [], []
    _wire(monkeypatch, lambda: f"daedalus/{tid_box[0]}-fix", ready, armed)
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "1")
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        tid_box.append(tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped", {"flipped": RECORD})
            kb._append_event(conn, tid, "round_draft_marked",
                             {"marked": {"o/r#5": {"run": str(run), "head": HEAD, "at": FLIP_AT + 5}}}, run_id=run)
        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}", metadata={"pr_url": PR_URL},
                                expected_run_id=run)
        assert _status(conn, tid) == "review"
        assert "o/r#5" in kb.latest_run(conn, tid).metadata["handoff_freshness"]["draft_held"]
    assert ready == [] and armed == []


def test_worker_boot_never_changes_a_pr_draft_state(board, monkeypatch):
    """t_d3da1a4e: a kanban worker boot issues no draft write, even for a PR this card flipped ready on its own
    branch (the exact shape the removed round draft acted on)."""
    import subprocess
    import tools.kanban_tools as kt
    from hermes_cli import cli_single_query as csq
    from hermes_cli import kanban_placement_failfast as pff
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "1")
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped", {"flipped": RECORD})
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(kt, "register_current_worker_from_env", lambda: True)
    monkeypatch.setattr("agent.delegation_context.owned_kanban_task", lambda: tid)
    monkeypatch.setattr(pff, "boot_check", lambda task, preloads=(): None)
    gh_argv = []
    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        if not (isinstance(argv, list) and argv and argv[0] == "gh"):
            return real_run(argv, *a, **kw)
        gh_argv.append(list(argv))
        out = {"state": "open", "draft": False, "head": {"sha": HEAD, "ref": f"daedalus/{tid}-fix"}}
        if "graphql" in argv:
            out = {"data": {"viewer": {"databaseId": 1}}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    class _Cli:
        def start_preloaded_skills(self):
            pass

        def _claim_active_session(self, *_a, **_k):
            return False  # stop the one-shot right after boot

    with pytest.raises(SystemExit):
        csq._run_single_query_mode(_Cli(), "do it", None, True, True)
    assert gh_argv == [], gh_argv


def test_e2e_held_draft_with_draft_ok_takes_the_override_path(board, monkeypatch):
    """Prism 400b19850a8f: an explicit --draft-ok on a held draft is the audited override, unchanged:
    the card completes (not routed to review) and completion_draft_override is written."""
    ready, armed, tid_box = [], [], []
    _wire(monkeypatch, lambda: f"daedalus/{tid_box[0]}-fix", ready, armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        tid_box.append(tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped", {"flipped": RECORD})
        assert kb.complete_task(conn, tid, summary=f"done {PR_URL}", metadata={"pr_url": PR_URL},
                                expected_run_id=run, draft_ok="intentionally left draft")
        assert _status(conn, tid) == "done"
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completion_draft_override'",
                          (tid,)).fetchall()
        assert len(ev) == 1
        override = json.loads(ev[0][0])
        assert override["prs"] == ["o/r#5"] and override["reason"] == "intentionally left draft"
        assert "left draft as-is" in override["held"]["o/r#5"]
        assert kb.latest_run(conn, tid).metadata["draft_override"] == override
    assert ready == [] and armed == []


def test_e2e_foreign_draft_still_refused(board, monkeypatch):
    ready, armed = [], []
    _wire(monkeypatch, lambda: "daedalus/t_deadbeef-other", ready, armed)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        with pytest.raises(fr.DraftPrError):
            kb.complete_task(conn, tid, summary=f"done {PR_URL}",
                             metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "running"
    assert ready == [] and armed == []


def test_e2e_partial_flip_record_persists_across_handoffs(board, monkeypatch):
    """Prism d34048d53f3c through complete_task: A flips, B rc 3 -> refused, A's flip recorded on the
    card. Retry with A non-draft + pre-ready lint success: A NOT armed. Post-flip test run: armed."""
    state = {"drafts": {"5": True, "6": True}, "runs": [PRE_READY_LINT, SKIPPED_TESTS], "tid": None}
    rcs = {5: 0, 6: 3}

    def gh(*args):
        path = args[-1]
        if "/pulls/" in path:
            n = path.rsplit("/", 1)[-1]
            return {"draft": state["drafts"][n], "head": {"sha": HEAD, "ref": f"daedalus/{state['tid']}-x"},
                    "base": {"ref": "main"}, "mergeable_state": "clean"}
        if "/compare/" in path:
            return {"behind_by": 0, "ahead_by": 1}
        if path.endswith("check-runs?per_page=100"):
            return {"check_runs": state["runs"]}
        if path.endswith("/status"):
            return {"state": "success", "total_count": 1}
        return None
    armed = []
    monkeypatch.setattr(fr, "_default_gh", lambda: gh)
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda r, n: rcs[n]))
    monkeypatch.setattr(fr, "spawn_evaluate", lambda *a: armed.append(a[1]) or "/tmp/log")
    monkeypatch.setattr(fr.time, "time", lambda: FLIP_AT)
    urls = f"{PR_URL} https://github.com/o/r/pull/6"
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        state["tid"] = tid
        with pytest.raises(fr.DraftPrError):
            kb.complete_task(conn, tid, summary=f"done {urls}", metadata={"pr_urls": urls.split()},
                             expected_run_id=run)
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='handoff_draft_flipped'",
                          (tid,)).fetchall()
        assert [json.loads(r[0])["flipped"] for r in ev] == [{"o/r#5": {"head": HEAD, "at": FLIP_AT}}]
        # Retry: A already ready on GitHub, B flips now; A carries only draft-time checks.
        state["drafts"] = {"5": False, "6": True}
        rcs[6] = 0
        assert kb.complete_task(conn, tid, summary=f"done {urls}", metadata={"pr_urls": urls.split()},
                                expected_run_id=run)
        assert armed == []
