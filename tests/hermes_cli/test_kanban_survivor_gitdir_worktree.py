"""A linked worktree stored under the workspace's own ``.git`` is not nested content.

Card t_5a20f492. ``home-autocommit`` keeps a linked worktree at
``<repo>/.git/autocommit/main-gate-worktree``. ``_registered_nested`` took every
``git worktree list`` entry under the workspace as a nested repository, so every
``kanban complete --survivor-pr`` on a hermes-home card refused with
``nested repository requires separate recovery (.git/autocommit/main-gate-worktree)``.

Mutation check: drop the git-storage exclusion in ``_registered_nested`` ->
``test_worktree_under_git_dir_is_not_nested`` and
``test_worktree_under_git_dir_does_not_hold_the_card`` go red.
"""
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_survivor as survivor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


def _init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo


@pytest.fixture
def board(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def _gate_worktree(ws):
    gate = ws / ".git" / "autocommit" / "main-gate-worktree"
    git(ws, "worktree", "add", str(gate), "-b", "gate")
    assert (gate / ".git").is_file(), "fixture: the gate worktree is on disk"
    assert str(gate) in git(ws, "worktree", "list", "--porcelain"), "fixture: git lists it"
    return gate


def test_worktree_under_git_dir_is_not_nested(tmp_path):
    ws = _init(tmp_path / "ws").resolve()
    _gate_worktree(ws)
    assert survivor._registered_nested(ws) == []


def test_real_nested_worktree_is_still_reported_beside_it(tmp_path):
    ws = _init(tmp_path / "ws").resolve()
    _gate_worktree(ws)
    git(ws, "worktree", "add", str(ws / "nested"), "-b", "side")
    assert survivor._registered_nested(ws) == [ws / "nested"]


def test_checked_out_gitlink_is_still_reported(tmp_path):
    sub = _init(tmp_path / "sub")
    ws = _init(tmp_path / "ws").resolve()
    git(ws, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "vendor/pool")
    git(ws, "commit", "-m", "add submodule")
    _gate_worktree(ws)
    assert survivor._registered_nested(ws) == [ws / "vendor" / "pool"]


def test_hand_clone_is_still_refused_by_the_walk(board, tmp_path):
    """The exclusion is registry-only; a nested clone in the tree still refuses."""
    ws = _init(tmp_path / "ws").resolve()
    _gate_worktree(ws)
    _init(ws / "cloned-by-hand")
    tid = kb.create_task(board, title="gate worktree plus a hand clone")
    kbw.set_workspace_path(board, tid, ws)
    with pytest.raises(survivor.SurvivorUnavailable, match="nested repository"):
        survivor.preserve(board, tid, {"changed_files": ["code.py"]}, workspace=ws)


def test_worktree_under_git_dir_does_not_hold_the_card(board, tmp_path):
    """End to end: the hermes-home shape completes instead of refusing."""
    ws = _init(tmp_path / "ws").resolve()
    _gate_worktree(ws)
    (ws / "code.py").write_text("value = 2\n")
    tid = kb.create_task(board, title="hermes-home shaped card")
    kbw.set_workspace_path(board, tid, ws)
    survivor.record_baseline(board, tid, ws)
    survivor.preserve(board, tid, {"changed_files": ["code.py"]}, workspace=ws)
