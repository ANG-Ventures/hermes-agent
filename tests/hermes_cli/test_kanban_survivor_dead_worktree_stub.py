"""t_c41effce: a dead linked-worktree stub must be NAMED, with a remedy that works.

A linked worktree whose admin dir (`<main>/.git/worktrees/<name>`) was removed
leaves a `.git` gitfile pointing at nothing. `_repos` still counts it (its
`.git` exists), and the first git call in `_capture` exits 128, which
`_git(check=True)` collapsed into `survivor_unavailable: git remote failed
(rc=128)` -- no path, no cause, no remedy (measured on t_2df0cc1d: `baseline/`,
17,391 files, `.git` -> `repo/.git/worktrees/baseline`).

Neither the worktree `repair` verb (rc=1, "does not reference a repository")
nor the `prune` verb (fleet-guarded; it cannot recreate the admin dir either)
revives the stub, so the only remedy is to MOVE it aside after checking it.

Fail-closed stays: these tests pin the MESSAGE, never a skip.
"""
import json
import shutil
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


def stub_workspace(conn, tmp_path, *, stub_at_dispatch=False):
    """`repo/` (published HEAD) plus `baseline/`, a linked worktree of it.

    Returns (task id, workspace, main repo, stub). The stub is still LIVE;
    `kill_stub` removes its admin dir, which is the incident shape.
    `stub_at_dispatch` puts the stub in `bases` (present when the baseline
    was recorded), as it was on t_2df0cc1d.
    """
    tid = kb.create_task(conn, title="workspace with a linked worktree")
    ws = kb.resolve_workspace(kb.get_task(conn, tid))
    ws.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / f"{tid}.git"
    git(tmp_path, "init", "--bare", str(remote))
    repo = ws / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "a")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "HEAD:refs/heads/published")
    stub = ws / "baseline"
    if not stub_at_dispatch:
        kb.set_workspace_path(conn, tid, ws)
    git(repo, "worktree", "add", "--detach", str(stub), "HEAD")
    if stub_at_dispatch:
        kb.set_workspace_path(conn, tid, ws)
    return tid, ws, repo, stub


def kill_stub(repo, stub):
    admin = repo / ".git" / "worktrees" / stub.name
    assert admin.is_dir()
    shutil.rmtree(admin)
    # Precondition: git itself refuses this directory with rc=128.
    probe = subprocess.run(["git", "-C", str(stub), "remote"], capture_output=True)
    assert probe.returncode == 128


def test_dead_worktree_stub_is_named_with_a_working_remedy(board, tmp_path):
    tid, ws, repo, stub = stub_workspace(board, tmp_path)
    kill_stub(repo, stub)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["a.txt"]})
    message = str(excinfo.value)

    # The bare constant must never reach the operator for this shape.
    assert "git remote failed" not in message
    assert "rc=128" not in message
    # WHICH directory, WHAT it is, WHICH dead target, WHICH main repo.
    assert "./baseline" in message
    assert "dead linked worktree stub" in message
    assert str(repo / ".git" / "worktrees" / "baseline") in message
    assert f"linked worktree of {repo}" in message
    # The remedy names the step that works and not one that does not.
    assert "MOVE" in message and "never delete" in message
    assert "prune" not in message
    # Not recorded at dispatch, so no survivor-flag suffix.
    assert "recorded at dispatch" not in message

    # Fail-closed is preserved: still no completion, nothing deleted.
    assert kb.get_task(board, tid).status != "done"
    assert stub.exists() and (stub / "a.txt").exists()
    assert any(e.kind == "workspace_held" for e in kb.list_events(board, tid))


def test_following_the_remedy_completes(board, tmp_path):
    """The instruction IS the product: doing exactly what it says must work."""
    tid, ws, repo, stub = stub_workspace(board, tmp_path)
    kill_stub(repo, stub)
    with pytest.raises(ValueError):
        kb.complete_task(board, tid, metadata={"changed_files": ["a.txt"]})

    shutil.move(str(stub), str(tmp_path / "quarantine-baseline"))
    assert kb.complete_task(board, tid, metadata={"changed_files": ["a.txt"]})
    assert kb.get_task(board, tid).status == "done"


def test_stub_recorded_at_dispatch_is_told_the_MOVE_needs_a_survivor_flag(board, tmp_path):
    """A stub present at dispatch is in `bases`; a bare MOVE then hits the
    'recorded repository missing' gate, so the remedy has to name both steps."""
    tid, ws, repo, stub = stub_workspace(board, tmp_path, stub_at_dispatch=True)
    assert "baseline" in json.loads(
        board.execute("SELECT bases FROM task_workspace_survivors WHERE task_id = ?",
                      (tid,)).fetchone()[0]
    )
    kill_stub(repo, stub)

    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["a.txt"]})
    message = str(excinfo.value)
    assert "dead linked worktree stub" in message
    assert "recorded at dispatch" in message
    assert "--survivor-pr" in message and "--survivor-ref" in message

    # ...and the warning is TRUE: the bare MOVE really does hit that gate.
    shutil.move(str(stub), str(tmp_path / "quarantine-baseline"))
    with pytest.raises(ValueError) as excinfo:
        kb.complete_task(board, tid, metadata={"changed_files": ["a.txt"]})
    assert "recorded repository missing" in str(excinfo.value)


def test_a_live_linked_worktree_is_not_misclassified(board, tmp_path):
    """The classifier keys on a MISSING gitdir target only; a healthy linked
    worktree completes exactly as before."""
    tid, ws, repo, stub = stub_workspace(board, tmp_path)
    assert kb.complete_task(board, tid, metadata={"changed_files": ["a.txt"]})
    assert kb.get_task(board, tid).status == "done"
