"""t_9db37763: the scratch-workspace tripwire gitfile must not block completion.

Incident t_82a5c853 placed a gitfile at ``kanban/workspaces/.git`` pointing at
``/nonexistent/...`` so a worker's git in a scratch workspace can never walk up
into the live repository. Git then exits 128 "not a git repository" from every
scratch workspace, and ``_repos`` turned that into ``survivor_unavailable`` --
every scratch card's ``kanban_complete`` refused.

"Git says no repository encloses this dir" is NO survivor, not an unreadable
one. A repo the worker created inside the workspace must still be captured,
and a repo that exists but cannot be read must still fail closed.
"""
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, check=True,
    ).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    import hermes_cli.kanban_survivor as survivor
    monkeypatch.setattr(survivor, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def scratch_under_tripwire(conn):
    """A scratch workspace whose parent carries the live tripwire gitfile."""
    tid = kb.create_task(conn, title="scratch card under the tripwire")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    (ws.parent / ".git").write_text(
        "gitdir: /nonexistent/kanban-scratch-workspace-is-not-a-repo (repo tripwire)\n"
    )
    # Precondition: this is the incident shape -- git refuses with rc=128.
    probe = subprocess.run(["git", "-C", str(ws), "rev-parse", "--show-toplevel"],
                           capture_output=True)
    assert probe.returncode == 128
    kb.set_workspace_path(conn, tid, ws)
    return tid, ws


def nested_repo_with_commit(ws):
    repo = ws / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "work.txt").write_text("the worker's unpublished commit\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "local work")
    return repo


def survivor_row(conn, tid):
    row = conn.execute(
        "SELECT survivor, held_reason FROM task_workspace_survivors WHERE task_id = ?", (tid,)
    ).fetchone()
    return (json.loads(row[0]) if row and row[0] else None), (row[1] if row else None)


def test_a_scratch_workspace_under_the_tripwire_completes_with_no_survivor(board):
    tid, ws = scratch_under_tripwire(board)

    assert kb.complete_task(board, tid, summary="PASS verdict")

    assert kb.get_task(board, tid).status == "done"
    survivor, held = survivor_row(board, tid)
    assert survivor is None and not held


def test_b_a_nested_repo_under_the_tripwire_is_still_captured(board):
    tid, ws = scratch_under_tripwire(board)
    repo = nested_repo_with_commit(ws)
    head = git(repo, "rev-parse", "HEAD")

    assert kb.complete_task(board, tid, metadata={"changed_files": ["repo/work.txt"]})

    assert kb.get_task(board, tid).status == "done"
    survivor, held = survivor_row(board, tid)
    assert survivor is not None and not held
    # The unpublished commit is carried in a bundle that really holds it.
    assert survivor["kind"] == "bundle"
    bundle = Path(survivor["bundles"][0]["path"])
    # Completion may reap the workspace, so restore the bundle from outside
    # it; the snapshot commit sits on top of the worker's HEAD.
    restored = bundle.parent / "restored"
    git(bundle.parent, "clone", "-q", str(bundle), str(restored))
    git(restored, "merge-base", "--is-ancestor", head, "HEAD")


def test_c_a_nested_repo_with_a_corrupt_object_store_still_fails_closed(board):
    tid, ws = scratch_under_tripwire(board)
    repo = nested_repo_with_commit(ws)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["repo/work.txt"]})

    assert "survivor_unavailable" in str(excinfo.value)
    assert kb.get_task(board, tid).status != "done"
    assert repo.exists()


def test_only_a_dangling_gitfile_is_excused_not_a_broken_enclosing_repo(board):
    """A `.git` DIRECTORY that git cannot use is not the tripwire: stay closed."""
    tid = kb.create_task(board, title="scratch under a broken repo")
    ws = kb.resolve_workspace(kb.get_task(board, tid))
    ws.mkdir(parents=True, exist_ok=True)
    (ws.parent / ".git").mkdir()
    kb.set_workspace_path(board, tid, ws)
    probe = subprocess.run(["git", "-C", str(ws), "rev-parse", "--show-toplevel"],
                           capture_output=True)
    if probe.returncode == 0:
        pytest.skip("tmp_path sits inside a real repository; the broken-dir shape is masked")

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, summary="PASS verdict")

    assert "survivor_unavailable" in str(excinfo.value)
    assert kb.get_task(board, tid).status != "done"
