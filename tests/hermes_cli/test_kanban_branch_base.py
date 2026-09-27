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
