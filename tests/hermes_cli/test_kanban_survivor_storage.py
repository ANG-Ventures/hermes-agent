"""A durable checkout must not borrow Git storage from a disposable source."""
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *map(str, args)], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def source(board):
    tid = kb.create_task(board, title="storage independence")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    git(ws, "init", "-b", "main")
    git(ws, "config", "user.name", "Test")
    git(ws, "config", "user.email", "test@example.invalid")
    (ws / "implementation.py").write_text("value = 42\n")
    git(ws, "add", "implementation.py")
    git(ws, "commit", "-m", "implementation")
    kb.set_workspace_path(board, tid, ws)
    return tid, ws, git(ws, "rev-parse", "HEAD")


@pytest.mark.parametrize("mode", ["live", "origin", "landed"])
@pytest.mark.parametrize("storage", ["linked", "gitfile", "symlink", "alternates", "object-symlink"])
def test_disposable_git_storage_never_authorizes_cleanup(board, tmp_path, mode, storage):
    tid, ws, sha = source(board)
    live = tmp_path / "live"
    if storage == "linked":
        git(ws, "worktree", "add", "--detach", live, sha)
    elif storage in {"gitfile", "symlink"}:
        live.mkdir()
        if storage == "gitfile":
            (live / ".git").write_text(f"gitdir: {ws / '.git'}\n")
        else:
            (live / ".git").symlink_to(ws / ".git", target_is_directory=True)
    else:
        git(tmp_path, "clone", "--shared" if storage == "alternates" else "--no-local", ws, live)
        if storage == "object-symlink":
            shutil.rmtree(live / ".git" / "objects")
            (live / ".git" / "objects").symlink_to(ws / ".git" / "objects", target_is_directory=True)
    assert git(live, "rev-parse", "HEAD") == sha
    git(live, "cat-file", "-e", sha)
    metadata = {"changed_files": ["implementation.py"]}
    if mode == "landed":
        metadata["landed"] = [{"repo_path": str(live), "sha": sha}]
        with pytest.raises(ValueError, match="survivor_unavailable"):
            kb.complete_task(board, tid, metadata=metadata)
        assert ws.exists()
        assert kb.get_task(board, tid).status != "done"
    else:
        git(ws, "remote", "add", mode, live)
        assert kb.complete_task(board, tid, metadata=metadata)
        receipt = kb.latest_run(board, tid).metadata["survivor"]
        assert receipt["kind"] == "bundle"
        restored = tmp_path / "restored"
        git(tmp_path, "clone", receipt["bundles"][0]["path"], restored)
        assert (restored / "implementation.py").read_text() == "value = 42\n"
        assert not ws.exists()


@pytest.mark.parametrize("mode", ["live", "origin", "landed"])
def test_independent_linked_worktree_survives_cleanup(board, tmp_path, mode):
    tid, ws, sha = source(board)
    durable = tmp_path / "durable"
    git(tmp_path, "clone", "--no-local", ws, durable)
    live = tmp_path / "linked"
    git(durable, "worktree", "add", "--detach", live, sha)
    metadata = {"changed_files": ["implementation.py"]}
    if mode == "landed":
        metadata["landed"] = [{"repo_path": str(live), "sha": sha}]
    else:
        git(ws, "remote", "add", mode, live)
    assert kb.complete_task(board, tid, metadata=metadata)
    receipt = kb.latest_run(board, tid).metadata["survivor"]
    assert receipt["kind"] == ("landed" if mode == "landed" else "ref")
    assert not ws.exists()
    git(live, "cat-file", "-e", sha)
    assert git(live, "show", f"{sha}:implementation.py") == "value = 42"
