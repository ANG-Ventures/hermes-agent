"""Branch-base guard (t_18e781d0): stale-base branches fail loudly, broad valid ones pass.

Fixtures are real git repos: a bare ``origin`` whose ``main`` moves on after
the worker's base was cut, exactly like the 2026-09-27 re-cuts (#1004 233
behind, #985 70 behind + DIRTY, a survivor patch with 138 files).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_branch_base as bb
from hermes_cli import kanban_db as kb

OLD = "2020-01-01T00:00:00+0000"


def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    for k in bb._SCRUB_ENV:
        env.pop(k, None)
    if date:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = date
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str, date: str | None = None) -> None:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, date=date)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """Bare origin with main; returns a 'maintainer' checkout that can push."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    dev = tmp_path / "maintainer"
    subprocess.run(["git", "clone", "-q", str(bare), str(dev)], check=True, capture_output=True)
    _git(dev, "checkout", "-q", "-b", "main")
    _commit(dev, {"hermes_cli/foo.py": "a = 1\n", "gateway/run.py": "x = 0\n",
                  "README.md": "base\n"}, "init", date=OLD)
    _git(dev, "push", "-q", "origin", "main")
    return dev


def _advance_trunk(dev: Path, n: int, touch: dict[str, str] | None = None) -> None:
    for i in range(n):
        _commit(dev, {f"docs/n{i}.md": f"{i}\n"}, f"trunk {i}", date=OLD)
    if touch:
        _commit(dev, touch, "trunk edits the same lines", date=OLD)
    _git(dev, "push", "-q", "origin", "main")


def _clone(origin_dev: Path, dest: Path) -> Path:
    url = _git(origin_dev, "remote", "get-url", "origin")
    subprocess.run(["git", "clone", "-q", url, str(dest)], check=True, capture_output=True)
    return dest


def _stale_branch(origin: Path, tmp_path: Path) -> tuple[Path, float]:
    """A worker branch cut from a stale local HEAD carrying a local-only commit."""
    work = _clone(origin, tmp_path / "ws" / "repo")
    # the anchor's local-only commit (live-tree autocommit style), pre-card
    _commit(work, {"scripts/autocommit.sh": "echo hi\n", "cron/jobs.json": "{}\n"},
            "local autocommit", date=OLD)
    created_at = time.time() - 5
    _git(work, "checkout", "-q", "-b", "wt/t_x")
    _commit(work, {"hermes_cli/foo.py": "a = 2  # card change\n"}, "card change")
    _advance_trunk(origin, 25, touch={"hermes_cli/foo.py": "a = 3  # trunk\n"})
    return work, created_at


def test_stale_base_branch_fails_with_every_signature(origin, tmp_path):
    work, created_at = _stale_branch(origin, tmp_path)
    rep = bb.check_checkout(work, scope=["hermes_cli/foo.py"], since=created_at)
    assert not rep.ok
    assert rep.trunk == "origin/main"
    assert rep.behind == 26 and rep.ahead == 2
    assert rep.conflicts == ["hermes_cli/foo.py"]
    assert any("before the card existed" in f for f in rep.foreign_commits)
    assert sorted(rep.out_of_scope) == ["cron/jobs.json", "scripts/autocommit.sh"]
    # the tree delta vs current trunk is the explosion; own files are not
    assert len(rep.tree_delta_files) > len(rep.own_files)
    text = rep.render()
    assert "FAIL" in text and "git cherry-pick" in text and "behind" in text


def test_cherry_equivalent_commit_is_foreign(origin, tmp_path):
    work = _clone(origin, tmp_path / "w")
    _git(work, "checkout", "-q", "-b", "feat")
    _commit(work, {"docs/dup.md": "same\n"}, "dup")
    _commit(origin, {"docs/dup.md": "same\n"}, "dup landed on trunk")
    _git(origin, "push", "-q", "origin", "main")
    _commit(work, {"hermes_cli/foo.py": "a = 9\n"}, "mine")
    rep = bb.check_checkout(work, max_behind=None)
    assert any("patch-equivalent" in f for f in rep.foreign_commits)
    assert not rep.ok


def test_intentionally_broad_fresh_pr_passes(origin, tmp_path):
    work = _clone(origin, tmp_path / "w")
    created_at = time.time() - 5
    _git(work, "checkout", "-q", "-b", "refactor")
    files = {f"hermes_cli/split/mod{i}.py": f"v = {i}\n" for i in range(40)}
    files.update({f"tests/hermes_cli/test_mod{i}.py": "" for i in range(40)})
    _commit(work, files, "extract 40 modules")
    rep = bb.check_checkout(work, scope=["hermes_cli/foo.py"], since=created_at)
    assert rep.ok, rep.render()
    assert len(rep.own_files) == 80 and rep.behind == 0


def test_behind_but_clean_passes_at_handoff_and_fails_prepush(origin, tmp_path):
    work = _clone(origin, tmp_path / "w")
    _git(work, "checkout", "-q", "-b", "b")
    _commit(work, {"gateway/run.py": "x = 1\n"}, "mine")
    _advance_trunk(origin, 25)
    assert bb.check_checkout(work, max_behind=None).ok
    rep = bb.check_checkout(work)
    assert not rep.ok and "behind" in rep.failures[0]


def test_scope_match():
    scope = ["hermes_cli/kanban_db.py", "~/.hermes/scripts/a.py", "docs/*.md"]
    assert bb.scope_match("hermes_cli/other.py", scope)
    assert bb.scope_match("tests/hermes_cli/test_x.py", scope)
    assert bb.scope_match("scripts/b.sh", scope)
    assert bb.scope_match("docs/x.md", scope)
    assert not bb.scope_match("gateway/run.py", scope)
    assert not bb.scope_match("cron/jobs.json", scope)


def test_no_remote_is_skipped_not_failed(tmp_path):
    repo = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _commit(repo, {"a": "1"}, "x")
    rep = bb.check_checkout(repo)
    assert rep.ok and rep.skipped


def test_worktree_workspace_branches_from_fetched_trunk_not_local_head(origin, tmp_path):
    anchor = _clone(origin, tmp_path / "anchor")
    _commit(anchor, {"scripts/local.sh": "x\n"}, "local-only commit")
    _advance_trunk(origin, 5)
    trunk_tip = _git(origin, "rev-parse", "HEAD")
    target = tmp_path / "anchor" / ".worktrees" / "t_1"
    kb._ensure_git_worktree(anchor, target, "wt/t_1")
    assert _git(target, "rev-parse", "HEAD") == trunk_tip
    # no upstream: a bare `git push` cannot target the trunk
    proc = subprocess.run(["git", "-C", str(target), "rev-parse", "--abbrev-ref", "@{u}"],
                          capture_output=True, text=True)
    assert proc.returncode != 0


def test_enforce_handoff_raises_and_override_passes(origin, tmp_path):
    work, created_at = _stale_branch(origin, tmp_path)
    ws = str(work.parent)
    with pytest.raises(bb.StaleBaseError) as ei:
        bb.enforce_handoff("t_x", workspace_path=ws, workspace_kind="scratch",
                           scope=["hermes_cli/foo.py"], created_at=created_at)
    assert "remediation" in str(ei.value) and "still in-flight" in str(ei.value)
    out = bb.enforce_handoff("t_x", workspace_path=ws, workspace_kind="scratch",
                             scope=["hermes_cli/foo.py"], created_at=created_at,
                             metadata={"base_guard_override": "re-port lands both"})
    assert out["override"] == "re-port lands both"
    # a dir: workspace (shared long-lived tree) is never judged
    assert bb.enforce_handoff("t_x", workspace_path=ws, workspace_kind="dir",
                              scope=[], created_at=created_at) is None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_e2e_complete_task_refuses_stale_base_card_stays_running(board, origin, tmp_path):
    ws = tmp_path / "ws"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="worker",
                             body="edit `hermes_cli/foo.py`",
                             workspace_kind="scratch", workspace_path=str(ws))
        kb.claim_task(conn, tid)
        run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        work, _ = _stale_branch(origin, tmp_path)
        assert work.parent == ws
        with pytest.raises(bb.StaleBaseError):
            kb.complete_task(conn, tid, summary="done", expected_run_id=run)
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        assert status == "running"
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "completion_blocked_stale_base" in kinds


# --- t_22c91696: already-merged PRs are not a stale base -------------------


def _squash_merged_branch(origin: Path, tmp_path: Path, n_commits: int = 1) -> tuple[Path, float]:
    """The 2026-09-27 shape: the card's branch was squash-merged, then trunk
    moved on (140 behind) and edited the same lines, so the stale workspace
    branch reads as 'foreign patch-equivalent commit' + 'does not merge cleanly'."""
    work = _clone(origin, tmp_path / "ws" / "repo")
    created_at = time.time() - 5
    _git(work, "checkout", "-q", "-b", "daedalus/t_x")
    for i in range(n_commits):
        _commit(work, {"hermes_cli/foo.py": f"a = {10 + i}  # card change\n"}, f"card {i}")
    # the merge queue squashes the PR onto trunk
    _commit(origin, {"hermes_cli/foo.py": f"a = {10 + n_commits - 1}  # card change\n"},
            "card squash (#1)")
    _advance_trunk(origin, 12, touch={"hermes_cli/foo.py": "a = 99  # later trunk edit\n"})
    return work, created_at


def test_landed_branch_is_skipped_not_failed(origin, tmp_path):
    work, created_at = _squash_merged_branch(origin, tmp_path)
    rep = bb.check_checkout(work, scope=["hermes_cli/foo.py"], since=created_at,
                            max_behind=None)
    assert rep.ok and rep.skipped and "already landed" in rep.skipped, rep.render()
    assert bb.enforce_handoff("t_x", workspace_path=str(work.parent), workspace_kind="scratch",
                              scope=[], created_at=created_at) is None


def _states(head_sha: str | None = None, merge_sha: str = "abc123def4567", **by_number):
    def q(repo, number):
        st = by_number.get(f"n{number}")
        if st is None:
            return None
        out = {"state": st, "merge_commit_sha": merge_sha}
        if head_sha:
            out["head_sha"] = head_sha
        return out
    return lambda: q


def _squash_sha(origin: Path) -> str:
    return _git(origin, "log", "-1", "--format=%H", "--grep=card squash")


def _card(conn, ws: Path) -> tuple[str, int]:
    tid = kb.create_task(conn, title="slice", assignee="worker", body="edit `hermes_cli/foo.py`",
                         workspace_kind="scratch", workspace_path=str(ws))
    kb.claim_task(conn, tid)
    return tid, conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_e2e_merged_survivor_pr_completes_without_override(board, origin, tmp_path, monkeypatch):
    from hermes_cli import kanban_open_pr as op, kanban_survivor as ks
    # survivor capture/remote verification is a later, separate gate (network)
    monkeypatch.setattr(ks, "preserve", lambda *a, **k: None)
    # multi-commit squash: git cherry cannot match it, only the PR state can
    work, _ = _squash_merged_branch(origin, tmp_path, n_commits=2)
    # the merged PR's head IS this checkout's HEAD: the PR carried these commits
    monkeypatch.setattr(op, "_default_query", _states(_git(work, "rev-parse", "HEAD"),
                                                      _squash_sha(origin), n7="MERGED"))
    with kb.connect() as conn:
        tid, run = _card(conn, work.parent)
        with pytest.raises(bb.StaleBaseError):  # same checkout, no merged PR named
            kb.complete_task(conn, tid, summary="done", expected_run_id=run)
        assert kb.complete_task(conn, tid, summary="done", expected_run_id=run,
                                survivor_pr="ANG-Ventures/hermes-agent#7")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, "base_guard_survivor_merged")).fetchone()
        assert ev and f"ANG-Ventures/hermes-agent#7 @ {_squash_sha(origin)[:12]}" in ev[0]


def test_e2e_open_survivor_pr_with_foreign_commit_still_refuses(board, origin, tmp_path, monkeypatch):
    from hermes_cli import kanban_open_pr as op
    monkeypatch.setattr(op, "_default_query", _states(n7="OPEN"))
    with kb.connect() as conn:
        work, _ = _stale_branch(origin, tmp_path)
        tid, run = _card(conn, work.parent)
        with pytest.raises(bb.StaleBaseError):
            kb.complete_task(conn, tid, summary="done", expected_run_id=run,
                             survivor_pr="ANG-Ventures/hermes-agent#7")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"


# --- FleetReview #1394 P1: a merged PR must be tied to the checkout ----------


def test_e2e_unrelated_merged_pr_does_not_bypass_guard(board, origin, tmp_path, monkeypatch):
    """Naming ANY merged PR must not excuse a stale checkout whose commits that
    PR never carried (key c2b6863a508d)."""
    from hermes_cli import kanban_open_pr as op, kanban_survivor as ks
    monkeypatch.setattr(ks, "preserve", lambda *a, **k: None)
    with kb.connect() as conn:
        work, _ = _stale_branch(origin, tmp_path)
        unrelated = _git(origin, "rev-parse", "HEAD")  # a trunk sha, not this branch
        for head in (unrelated, None):  # wrong head / no head evidence at all
            monkeypatch.setattr(op, "_default_query", _states(head, n7="MERGED"))
            tid, run = _card(conn, work.parent)
            with pytest.raises(bb.StaleBaseError):
                kb.complete_task(conn, tid, summary="done", expected_run_id=run,
                                 survivor_pr="ANG-Ventures/hermes-agent#7")
            assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"
            ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                              (tid, "completion_blocked_stale_base")).fetchone()
            assert ev and "survivor_merged_untied" in ev[0]


def test_merge_commit_ahead_is_not_already_landed(origin, tmp_path):
    """git cherry omits merge commits; a patch-equivalent commit plus a merge
    commit carrying new changes is NOT already landed (key 16b979e96639)."""
    work, created_at = _squash_merged_branch(origin, tmp_path)
    # side branch: a commit patch-equivalent to one trunk already has
    _git(work, "checkout", "-q", "-b", "side", "HEAD~1")
    _commit(work, {"docs/n0.md": "0\n"}, "side, same patch as trunk 0")
    _git(work, "checkout", "-q", "daedalus/t_x")
    # evil merge: the merge commit itself introduces an unpublished change
    _git(work, "merge", "-q", "--no-ff", "--no-commit", "side")
    (work / "gateway" / "run.py").write_text("x = 1  # only in the merge commit\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "merge side")
    _git(work, "fetch", "-q", "origin")
    cherry = _git(work, "cherry", "origin/main", "HEAD").splitlines()
    assert cherry and all(ln.startswith("- ") for ln in cherry)  # the blind spot
    rep = bb.check_checkout(work, scope=["hermes_cli/foo.py"], since=created_at,
                            max_behind=None)
    assert not rep.skipped, rep.render()
    assert not rep.ok and "gateway/run.py" in rep.out_of_scope, rep.render()


# --- FleetReview #1434 P1: merged PR must carry the content, on this trunk ---


def _complete_refused(conn, work: Path) -> str:
    tid, run = _card(conn, work.parent)
    with pytest.raises(bb.StaleBaseError):
        kb.complete_task(conn, tid, summary="done", expected_run_id=run,
                         survivor_pr="ANG-Ventures/hermes-agent#7")
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                      (tid, "completion_blocked_stale_base")).fetchone()
    assert ev and "survivor_merged_untied" in ev[0]
    return tid


def test_e2e_merged_pr_that_reverted_the_checkout_does_not_bypass_guard(
        board, origin, tmp_path, monkeypatch):
    """The PR head descends from the checkout HEAD but a later PR commit
    reverted its change; the squash that landed holds none of it (key
    8db3d1e0e4ce). Ancestry alone would excuse the failures."""
    from hermes_cli import kanban_open_pr as op, kanban_survivor as ks
    monkeypatch.setattr(ks, "preserve", lambda *a, **k: None)
    work = _clone(origin, tmp_path / "ws" / "repo")
    _git(work, "checkout", "-q", "-b", "daedalus/t_x")
    _commit(work, {"hermes_cli/foo.py": "a = 10  # card change\n"}, "card 0")
    _commit(work, {"hermes_cli/foo.py": "a = 11  # card change\n"}, "card 1")
    head = _git(work, "rev-parse", "HEAD")
    # the PR (pushed from elsewhere) reverts the card's change, then squashes:
    pr = _clone(origin, tmp_path / "pr")
    _git(pr, "fetch", "-q", str(work), "daedalus/t_x")
    _git(pr, "checkout", "-q", "-b", "pr", "FETCH_HEAD")
    _commit(pr, {"hermes_cli/foo.py": "a = 1\n", "docs/pr.md": "pr\n"}, "revert card")
    pr_head = _git(pr, "rev-parse", "HEAD")
    _git(work, "fetch", "-q", str(pr), "pr")  # PR head known locally: ancestry is testable
    _commit(origin, {"docs/pr.md": "pr\n"}, "card squash (#7)")
    _advance_trunk(origin, 3, touch={"hermes_cli/foo.py": "a = 99  # later trunk edit\n"})
    assert _git(work, "merge-base", "--is-ancestor", head, pr_head) == ""  # ancestry holds
    monkeypatch.setattr(op, "_default_query", _states(pr_head, _squash_sha(origin), n7="MERGED"))
    with kb.connect() as conn:
        _complete_refused(conn, work)


def test_e2e_pr_merged_into_another_repo_does_not_bypass_guard(
        board, origin, tmp_path, monkeypatch):
    """The PR head IS this checkout's HEAD, but it merged into a fork: its merge
    commit never reached this checkout's trunk (key 9212a9f6ac95)."""
    from hermes_cli import kanban_open_pr as op, kanban_survivor as ks
    monkeypatch.setattr(ks, "preserve", lambda *a, **k: None)
    work, _ = _squash_merged_branch(origin, tmp_path, n_commits=2)
    head = _git(work, "rev-parse", "HEAD")
    fork = _clone(origin, tmp_path / "fork")
    _git(fork, "fetch", "-q", str(work), "daedalus/t_x")
    _git(fork, "merge", "-q", "--no-ff", "-X", "theirs", "-m", "fork merge (#7)", "FETCH_HEAD")
    fork_merge = _git(fork, "rev-parse", "HEAD")
    _git(work, "fetch", "-q", str(fork), "main")  # object present locally, not on trunk
    monkeypatch.setattr(op, "_default_query", _states(head, fork_merge, n7="MERGED"))
    with kb.connect() as conn:
        _complete_refused(conn, work)
