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
    monkeypatch.setenv("KANBAN_MERGE_GATE_PROFILE", str(tmp_path / "absent-mgp.json"))
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
                   arm=lambda *a: armed.append(a) or "log", ready=lambda r, n: ready.append((r, n)) or 0)
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
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh(head_ref), arm=lambda *a: None)
    assert ready == []


def test_own_draft_on_unlisted_repo_refused(monkeypatch, tmp_path):
    cfg = tmp_path / "other.json"
    cfg.write_text(json.dumps({"repos": ["o/other"]}))
    monkeypatch.setenv("KANBAN_WORKER_PR_DRAFT", str(cfg))
    with pytest.raises(fr.DraftPrError):
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"),
                 arm=lambda *a: None, ready=lambda r, n: 0)


@pytest.mark.parametrize("rc", [21, 20, 3, None])
def test_failed_flip_refuses(rc):
    """Review hold (21), Ace gate (20), read failure (3) or ace_gate missing (None): refuse as before."""
    with pytest.raises(fr.DraftPrError) as exc:
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"),
                 arm=lambda *a: None, ready=lambda r, n: rc)
    assert exc.value.prs == ["o/r#5"]


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("KANBAN_HANDOFF_DRAFT_FLIP", "0")
    ready = []
    with pytest.raises(fr.DraftPrError):
        fr.check(_refs(), task_id="t_c1ee61ae", allow_arm=True, gh=_gh("daedalus/t_c1ee61ae-x"),
                 arm=lambda *a: None, ready=lambda r, n: ready.append(1) or 0)
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
                 allow_arm=True, gh=gh, arm=lambda *a: None, ready=lambda r, n: ready.append(n) or 0)
    assert ready == [] and sorted(exc.value.prs) == ["o/r#5", "o/r#6"]


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
    monkeypatch.setattr(fr, "spawn_arm", lambda *a: armed.append(a) or "/tmp/log")


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
