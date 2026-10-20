"""``reopen`` of a done scratch card must not strand it (t_b259e24e).

``complete_task`` removes a scratch workspace. ``reopen_task`` used to leave
the dead ``workspace_path`` on the card, so the next dispatcher tick refused it
as ``stranded_by_mount_loss`` and paged #alerts, although the volume was
mounted the whole time. Live case 2026-10-20: t_dcdd5962 (a relay-reopened
reading card) paged at 07:40 and again at 07:59.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_workspace_policy as policy


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text("kanban: {}\n")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return tmp_path


@pytest.fixture
def mounted_root(home, monkeypatch):
    root = home / "volume" / "workspaces"
    (root / "default").mkdir(parents=True)
    with kb.connect_closing() as conn:
        conn.execute(
            "INSERT INTO workspace_mount_roots(root, mount_path) VALUES (?, ?)",
            (str(root), str(root.parent)),
        )
    monkeypatch.setattr("os.path.ismount", lambda p: Path(p) == root.parent)
    return root


def _done_scratch_card(conn, root, *, keep_tree: bool):
    tid = kb.create_task(conn, title="reading", assignee="w", workspace_kind="scratch")
    path = root / "default" / tid
    path.mkdir()
    conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(path), tid))
    kb.claim_task(conn, tid, claimer="host:1")
    kb.complete_task(conn, tid, summary="attempt 1 written")
    if not keep_tree and path.exists():
        # complete_task's cleanup is best-effort; make the removed state explicit.
        import shutil

        shutil.rmtree(path)
    return tid, path


def test_reopen_clears_the_removed_scratch_path_so_admission_does_not_strand(mounted_root):
    with kb.connect_closing() as conn:
        tid, path = _done_scratch_card(conn, mounted_root, keep_tree=False)
        assert not path.exists()

        ok, err = kb.reopen_task(conn, tid, actor="relay", reason="attempt 1 rejected")
        assert ok, err

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert not task.workspace_path
        # The dispatcher's admission check now admits it (recreates a fresh tree).
        kb._validate_workspace_admission(task, conn=conn, dry_run=True)
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        assert "workspace_reset" in kinds


def test_without_the_reset_the_same_card_is_refused_stranded(mounted_root):
    """Control arm: the dead path itself is what the dispatcher refuses."""
    with kb.connect_closing() as conn:
        tid, path = _done_scratch_card(conn, mounted_root, keep_tree=False)
        task = kb.get_task(conn, tid)
        assert task.workspace_path == str(path)
        with pytest.raises(policy.WorkspaceUnavailable, match="stranded_by_mount_loss"):
            kb._validate_workspace_admission(task, conn=conn, dry_run=True)


def test_reopen_keeps_a_scratch_path_that_still_exists(mounted_root):
    with kb.connect_closing() as conn:
        tid, path = _done_scratch_card(conn, mounted_root, keep_tree=True)
        path.mkdir(exist_ok=True)

        ok, err = kb.reopen_task(conn, tid, actor="relay", reason="attempt 1 rejected")
        assert ok, err
        assert kb.get_task(conn, tid).workspace_path == str(path)


def test_reopen_keeps_the_path_while_its_volume_is_gone(mounted_root, home):
    """Parent dir absent = the volume may be unmounted; a remount can bring the
    path back, so reopen leaves recovery to ``workspace reset``."""
    with kb.connect_closing() as conn:
        tid, path = _done_scratch_card(conn, mounted_root, keep_tree=False)
        mounted_root.parent.rename(home / "detached")

        ok, err = kb.reopen_task(conn, tid, actor="relay", reason="attempt 1 rejected")
        assert ok, err
        assert kb.get_task(conn, tid).workspace_path == str(path)


def test_reopen_keeps_a_missing_dir_kind_path(mounted_root, home):
    """A ``dir`` path is operator-owned; reopen never rewrites it."""
    with kb.connect_closing() as conn:
        missing = home / "operator-tree"
        tid = kb.create_task(conn, title="dir card", assignee="w",
                             workspace_kind="dir", workspace_path=str(missing))
        kb.claim_task(conn, tid, claimer="host:1")
        kb.complete_task(conn, tid, summary="done")

        ok, err = kb.reopen_task(conn, tid, actor="op", reason="redo")
        assert ok, err
        assert kb.get_task(conn, tid).workspace_path == str(missing)
