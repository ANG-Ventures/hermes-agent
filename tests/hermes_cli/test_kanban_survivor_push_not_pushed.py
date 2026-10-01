"""A NOT PUSHED survivor on a card that is NOT done gets pushed, not only attached (t_dadfedb2).

t_947cea0e (2026-09-29): the worker's 22 KB implementation.patch was recorded as
a ``NOT PUSHED`` workspace survivor attachment and the design was lost until the
card was re-minted (t_af7d0f70). A card that leaves the board without being done
(archive) now pushes its unpublished workspace to ``kanban-survivor/<id>`` on the
repository's push remote and comments the ref.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    # Model separate durable and temporary roots within the disposable test home.
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def fixture_repo(conn, nested=False):
    tid = kb.create_task(conn, title="implement fixture")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    repo = ws / "repo" if nested else ws
    repo.mkdir(exist_ok=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    remote = Path.home() / f"{tid}.git"
    git(repo, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "HEAD:main")
    kbw.set_workspace_path(conn, tid, ws)
    return tid, ws, repo


def _remote_branch(tid):
    remote = Path.home() / f"{tid}.git"
    return subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "-q", f"refs/heads/kanban-survivor/{tid}"],
        capture_output=True,
    ).stdout.decode().strip()


def test_archive_pushes_not_pushed_survivor_and_comments_ref(board, tmp_path):
    tid, ws, repo = fixture_repo(board)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "code.py").write_text("value = 2\n")
    (repo / "design.md").write_text("the exhaustion design\n")
    (repo / "ignored.txt").write_text("must not be pushed")
    assert kb.archive_task(board, tid)
    assert not ws.exists()
    survivor = json.loads(board.execute(
        "SELECT survivor FROM task_workspace_survivors WHERE task_id = ?", (tid,)).fetchone()[0])
    assert survivor["kind"] == "patch" and survivor["notice"] == "NOT PUSHED"
    sha = _remote_branch(tid)
    assert sha, "NOT PUSHED survivor was only attached, never pushed"
    remote = Path.home() / f"{tid}.git"
    assert git(remote, "rev-parse", f"{sha}^") == base
    assert git(remote, "show", f"{sha}:code.py") == "value = 2"
    assert git(remote, "show", f"{sha}:design.md") == "the exhaustion design"
    assert "ignored.txt" not in git(remote, "ls-tree", "--name-only", sha)
    comments = [c.body for c in kb.list_comments(board, tid) if c.author == "kanban"]
    assert any(f"kanban-survivor/{tid} @ {sha}" in body for body in comments), comments
    events = [json.loads(r[0]) for r in board.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'workspace_survivor_pushed'",
        (tid,))]
    assert [e["pushed"][0]["sha"] for e in events] == [sha]
    # The patch attachment is still recorded alongside the pushed ref.
    assert any(a.filename == "implementation.patch" for a in kb.list_attachments(board, tid))


def test_done_card_is_not_pushed(board):
    tid, ws, repo = fixture_repo(board)
    (repo / "code.py").write_text("value = 2\n")
    assert kb.complete_task(board, tid, metadata={"changed_files": ["code.py"]})
    assert not ws.exists()
    assert _remote_branch(tid) == ""


def test_published_clean_workspace_is_not_pushed(board):
    tid, ws, repo = fixture_repo(board)
    assert kb.archive_task(board, tid)
    assert _remote_branch(tid) == ""


def test_push_failure_is_commented_and_never_blocks(board, monkeypatch):
    tid, ws, repo = fixture_repo(board)
    git(repo, "remote", "set-url", "--push", "origin", str(Path.home() / "missing.git"))
    (repo / "code.py").write_text("value = 2\n")
    assert kb.archive_task(board, tid)
    assert not ws.exists()
    assert any("survivor push FAILED" in c.body for c in kb.list_comments(board, tid))
