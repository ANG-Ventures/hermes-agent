"""A dir/worktree workspace at or under the live runtime tree is refused.

Incident 2026-10-16: t_3b6e4e6b and t_91b6a4c4 were minted with
``workspace_kind=worktree, workspace_path=~/.hermes/runtime/hermes-agent``;
dispatch cut ``<runtime>/.worktrees/<id>`` inside the live import tree. Create
refuses the shape; dispatch refuses it for cards minted before the fix, as a
``spawn_failed`` run, without creating a worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
RUNTIME_TREE_WORKSPACE_ERROR = "workspace under the live runtime tree; use ~/.hermes/hermes-agent (fork/main)"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home, all_assignees_spawnable):
    with kb.connect() as c:
        yield c


@pytest.fixture
def runtime_tree(kanban_home):
    repo = kanban_home / "runtime" / "hermes-agent"
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return repo


def _paths(runtime_tree: Path, tmp_path: Path) -> dict[str, Path]:
    link = tmp_path / "rt-link"
    link.symlink_to(runtime_tree)
    return {
        "root": runtime_tree,
        "nested": runtime_tree / ".worktrees" / "t_x",
        "symlink": link,
        "symlink_nested": link / "sub",
    }


@pytest.mark.parametrize("kind", ["worktree", "dir"])
@pytest.mark.parametrize("variant", ["root", "nested", "symlink", "symlink_nested"])
def test_runtime_tree_workspace_refused_at_create(conn, runtime_tree, tmp_path, kind, variant):
    path = _paths(runtime_tree, tmp_path)[variant]
    with pytest.raises(ValueError, match="workspace under the live runtime tree"):
        kb.create_task(
            conn, title="squat", assignee="daedalus-opus",
            workspace_kind=kind, workspace_path=str(path),
        )
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_runtime_tree_workspace_sibling_checkout_allowed_at_create(conn, kanban_home):
    sibling = kanban_home / "hermes-agent"
    sibling.mkdir()
    # A look-alike prefix is not "under" the runtime tree.
    lookalike = kanban_home / "runtime" / "hermes-agent-old"
    for p in (sibling, lookalike):
        assert kb.create_task(
            conn, title="ok", assignee="daedalus-opus",
            workspace_kind="dir", workspace_path=str(p),
        )


def _spawn_should_not_run(task, workspace_path, board=None):  # pragma: no cover
    raise AssertionError("spawn_fn must not be reached for a runtime-tree workspace")


@pytest.mark.parametrize("kind", ["worktree", "dir"])
def test_runtime_tree_workspace_refused_at_dispatch(conn, runtime_tree, tmp_path, kind):
    # A card minted before the create guard: write the path straight to the row.
    task_id = kb.create_task(
        conn, title="pre-fix card", assignee="daedalus-opus",
        workspace_kind=kind, workspace_path=str(tmp_path / "placeholder"),
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(tmp_path / "rt-link" if kind == "dir" else runtime_tree), task_id),
        )
    (tmp_path / "rt-link").symlink_to(runtime_tree)

    result = kbd.dispatch_once(conn, spawn_fn=_spawn_should_not_run)

    assert task_id in result.spawn_failed
    run = conn.execute(
        "SELECT outcome, error FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert run["outcome"] == "spawn_failed"
    assert RUNTIME_TREE_WORKSPACE_ERROR in run["error"]
    assert not (runtime_tree / ".worktrees").exists()
    listed = subprocess.run(
        ["git", "-C", str(runtime_tree), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert listed.count("worktree ") == 1
