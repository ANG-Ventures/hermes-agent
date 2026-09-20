"""Guards for kanban workspace deletion (incident 2026-09-20).

The whole default-board scratch root ``~/.hermes/kanban/workspaces/`` was
deleted wholesale twice while a worker was actively running inside it. Two
distinct defects made that possible and invisible:

* ``_cmd_gc`` gated on a bare ``path.relative_to(scratch_root)``.
  ``Path.relative_to`` SUCCEEDS on an equal path (returns ``.``), so an
  archived row whose ``workspace_path`` was the root itself passed the
  containment check and would ``shutil.rmtree`` the root.
* Nothing consulted the card's run state, and nothing logged a deletion, so
  the incident left no trail naming a deleter.

These tests pin the three properties of ``kb.safe_remove_workspace_dir``:
strict containment, live-run refusal, and the audit log.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _scratch_root() -> Path:
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _mktask(title: str) -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee="daedalus")


def _audit_lines() -> list:
    log = kb.workspace_deletion_log_path()
    if not log.exists():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# 1. Strict containment -- the root-equality hazard
# ---------------------------------------------------------------------------


def test_safe_remove_refuses_the_workspaces_root_itself(kanban_home):
    """The root is not a strict descendant of itself; it must survive.

    This is the exact shape of the incident: ``relative_to(root)`` returns
    ``.`` for ``root``, so an equality-blind guard permits the wipe.
    """
    root = _scratch_root()
    victim = root / "t_livecard"
    victim.mkdir()
    (victim / "work.txt").write_text("unretained work\n", encoding="utf-8")

    assert kb.safe_remove_workspace_dir(root, task_id=None, reason="test") is False

    assert root.is_dir()
    assert victim.is_dir()
    assert (victim / "work.txt").read_text(encoding="utf-8") == "unretained work\n"


def test_safe_remove_allows_a_task_dir_under_the_root(kanban_home):
    root = _scratch_root()
    ws = root / "t_done"
    ws.mkdir()
    (ws / "artifact.txt").write_text("x\n", encoding="utf-8")

    assert kb.safe_remove_workspace_dir(ws, task_id="t_done", reason="test") is True
    assert not ws.exists()
    assert root.is_dir()


def test_safe_remove_refuses_paths_outside_any_workspaces_root(kanban_home, tmp_path):
    outside = tmp_path / "my-source-tree"
    outside.mkdir()
    (outside / "main.py").write_text("print(1)\n", encoding="utf-8")

    assert kb.safe_remove_workspace_dir(outside, task_id="t_x", reason="test") is False
    assert (outside / "main.py").exists()


# ---------------------------------------------------------------------------
# 2. Live-run refusal
# ---------------------------------------------------------------------------


def test_safe_remove_refuses_a_running_cards_workspace(kanban_home):
    """A card mid-run owns its directory even if a caller asks to delete it."""
    root = _scratch_root()
    task_id = _mktask("live one")
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

    ws = root / task_id
    ws.mkdir()
    (ws / "in-flight.txt").write_text("mid-run\n", encoding="utf-8")

    assert kb.safe_remove_workspace_dir(ws, task_id=task_id, reason="test") is False
    assert (ws / "in-flight.txt").exists()


def test_safe_remove_refuses_a_card_holding_an_unexpired_claim_lock(kanban_home):
    """An unexpired dispatch claim is ownership too, whatever the status says."""
    root = _scratch_root()
    task_id = _mktask("claimed one")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='todo', claim_lock='host:1', claim_expires=? "
            "WHERE id=?",
            (int(time.time()) + 900, task_id),
        )
        conn.commit()

    ws = root / task_id
    ws.mkdir()
    assert kb.safe_remove_workspace_dir(ws, task_id=task_id, reason="test") is False
    assert ws.is_dir()


def test_safe_remove_allows_a_card_whose_claim_lock_expired(kanban_home):
    root = _scratch_root()
    task_id = _mktask("stale claim")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='todo', claim_lock='host:1', claim_expires=? "
            "WHERE id=?",
            (int(time.time()) - 60, task_id),
        )
        conn.commit()

    ws = root / task_id
    ws.mkdir()
    assert kb.safe_remove_workspace_dir(ws, task_id=task_id, reason="test") is True
    assert not ws.exists()


# ---------------------------------------------------------------------------
# 3. Audit log
# ---------------------------------------------------------------------------


def test_deletion_audit_log_records_both_outcomes(kanban_home):
    root = _scratch_root()
    gone = root / "t_gone"
    gone.mkdir()

    task_id = _mktask("live")
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
    kept = root / task_id
    kept.mkdir()

    assert kb.safe_remove_workspace_dir(gone, task_id="t_gone", reason="gc_archived")
    assert not kb.safe_remove_workspace_dir(
        kept, task_id=task_id, reason="gc_archived"
    )

    lines = _audit_lines()
    deleted = [ln for ln in lines if "\tDELETE\t" in ln]
    refused = [ln for ln in lines if "\tREFUSED\t" in ln]
    assert len(deleted) == 1, lines
    assert len(refused) == 1, lines
    assert "task=t_gone" in deleted[0]
    assert "reason=gc_archived" in deleted[0]
    assert "pid=%d" % os.getpid() in deleted[0]
    assert "detail=task-has-live-run" in refused[0]


# ---------------------------------------------------------------------------
# 4. The incident, end to end, through `hermes kanban gc`
# ---------------------------------------------------------------------------


def test_gc_does_not_rmtree_the_root_for_an_archived_row_pointing_at_it(kanban_home):
    """THE regression test for the 2026-09-20 wipe.

    An archived row whose ``workspace_path`` equals the scratch root used to
    satisfy ``relative_to(scratch_root)`` and take the whole root -- every
    live card's scratch dir -- with it.
    """
    root = _scratch_root()

    archived_id = _mktask("archived, bad path")
    running_id = _mktask("running sibling")

    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(root), archived_id),
        )
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running_id,))
        conn.commit()

    sibling_ws = root / running_id
    sibling_ws.mkdir()
    (sibling_ws / "work.txt").write_text("hours of work\n", encoding="utf-8")

    rc = kanban_cli._cmd_gc(argparse.Namespace())

    assert rc == 0
    assert root.is_dir(), "gc deleted the scratch ROOT"
    assert (sibling_ws / "work.txt").read_text(encoding="utf-8") == "hours of work\n"

    refused = [ln for ln in _audit_lines() if "\tREFUSED\t" in ln]
    assert refused, "root-deletion attempt left no audit trail"
    assert "not-a-managed-scratch-descendant" in refused[0]


def test_gc_still_removes_a_normal_archived_scratch_workspace(kanban_home):
    """The guard must not turn gc into a no-op."""
    root = _scratch_root()
    task_id = _mktask("archived, normal")
    ws = root / task_id
    ws.mkdir()
    (ws / "junk.txt").write_text("junk\n", encoding="utf-8")

    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(ws), task_id),
        )
        conn.commit()

    assert kanban_cli._cmd_gc(argparse.Namespace()) == 0
    assert not ws.exists()
    assert root.is_dir()


def test_gc_skips_an_archived_row_that_still_holds_a_live_claim(kanban_home):
    """Archived + still-claimed is a contradiction; fail closed on the data."""
    root = _scratch_root()
    task_id = _mktask("archived but claimed")
    ws = root / task_id
    ws.mkdir()
    (ws / "work.txt").write_text("keep me\n", encoding="utf-8")

    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch', "
            "workspace_path=?, claim_lock='host:9', claim_expires=? WHERE id=?",
            (str(ws), int(time.time()) + 600, task_id),
        )
        conn.commit()

    assert kanban_cli._cmd_gc(argparse.Namespace()) == 0
    assert (ws / "work.txt").exists()


# ---------------------------------------------------------------------------
# 5. The WORKTREE lane -- same liveness + audit contract as the scratch lane
# ---------------------------------------------------------------------------
#
# ``_cleanup_workspace`` routes ``kind == 'worktree'`` to
# ``_cleanup_worktree_workspace`` and returns before ever reaching
# ``safe_remove_workspace_dir``. Before this fix that lane gated ONLY on
# dirty/unpushed: a ``running`` card with a clean, pushed worktree -- run
# 1880's exact situation, which held a ``git worktree add --force --lock`` --
# was removed under the live process, with no audit line. Measured on a real
# temp HERMES_HOME: ``WT_STILL_EXISTS False / AUDIT_EXISTS False``.


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=False, timeout=60,
    )


@pytest.fixture
def linked_worktree(tmp_path):
    """A real repo with a real linked worktree that is clean and fully pushed.

    Returns ``(repo_root, worktree_path)``. Skips if git is unavailable.
    """
    if shutil.which("git") is None:  # pragma: no cover - env dependent
        pytest.skip("git not available")
    remote = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)

    wt = tmp_path / "wt-card"
    # `main` is checked out in the primary worktree, so a linked worktree
    # must be detached at the same commit (still clean, still fully pushed).
    res = _git("worktree", "add", "--detach", str(wt), "main", cwd=repo)
    if res.returncode != 0 or not wt.is_dir():  # pragma: no cover
        pytest.skip("git worktree add unavailable: %s" % res.stderr)
    return repo, wt


def test_worktree_removal_writes_an_audit_line(kanban_home, linked_worktree):
    """DELIVERABLE 2b: *every* workspace deletion is logged, worktrees too."""
    _repo, wt = linked_worktree
    task_id = _mktask("worktree card")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', workspace_kind='worktree', "
            "workspace_path=? WHERE id=?",
            (str(wt), task_id),
        )
        conn.commit()

    with kb.connect_closing() as conn:
        kb._cleanup_worktree_workspace(
            task_id, str(wt), None, conn=conn, reason="complete_task"
        )

    lines = _audit_lines()
    assert lines, "worktree removal left no audit trail at all"
    mine = [ln for ln in lines if "task=%s" % task_id in ln]
    assert mine, lines
    assert "\tDELETE\t" in mine[-1], mine
    assert "reason=complete_task" in mine[-1]
    assert str(wt) in mine[-1]
    assert not wt.is_dir(), "a clean, pushed, non-live worktree should go"


def test_worktree_removal_refuses_a_running_card_and_audits_it(
    kanban_home, linked_worktree
):
    """A running card's worktree survives even when git says it is clean.

    This is run 1880: clean tree, everything pushed, card mid-run. The old
    lane removed it because dirty/unpushed were its only gates.
    """
    _repo, wt = linked_worktree
    task_id = _mktask("live worktree card")
    (wt / "scratch-note.txt").write_text("in flight\n", encoding="utf-8")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', workspace_kind='worktree', "
            "workspace_path=? WHERE id=?",
            (str(wt), task_id),
        )
        conn.commit()
        kb._cleanup_worktree_workspace(
            task_id, str(wt), None, conn=conn, reason="complete_task"
        )

    assert wt.is_dir(), "removed a RUNNING card's worktree"
    refused = [
        ln for ln in _audit_lines()
        if "\tREFUSED\t" in ln and "task=%s" % task_id in ln
    ]
    assert refused, _audit_lines()
    assert "detail=task-has-live-run" in refused[-1]


def test_worktree_removal_refuses_an_unexpired_claim_lock(
    kanban_home, linked_worktree
):
    _repo, wt = linked_worktree
    task_id = _mktask("claimed worktree card")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='todo', workspace_kind='worktree', "
            "workspace_path=?, claim_lock='host:3', claim_expires=? WHERE id=?",
            (str(wt), int(time.time()) + 900, task_id),
        )
        conn.commit()
        kb._cleanup_worktree_workspace(
            task_id, str(wt), None, conn=conn, reason="complete_task"
        )

    assert wt.is_dir()
    assert any(
        "\tREFUSED\t" in ln and "task=%s" % task_id in ln
        for ln in _audit_lines()
    )


def test_complete_task_worktree_lane_reaches_the_audit(
    kanban_home, linked_worktree
):
    """The wiring, not just the helper: _cleanup_workspace -> audit line."""
    _repo, wt = linked_worktree
    task_id = _mktask("worktree via complete")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', workspace_kind='worktree', "
            "workspace_path=? WHERE id=?",
            (str(wt), task_id),
        )
        conn.commit()
        kb._cleanup_workspace(conn, task_id)

    assert any(
        "task=%s" % task_id in ln for ln in _audit_lines()
    ), "the complete_task worktree lane still bypasses the audit"


# ---------------------------------------------------------------------------
# 6. remove_board(archive=False) -- a board dir CONTAINS that board's
#    workspaces/, so the same liveness + audit contract applies.
# ---------------------------------------------------------------------------


def test_remove_board_delete_refuses_while_a_card_is_running(kanban_home):
    kb.create_board("doomed", name="Doomed")
    with kb.connect_closing(board="doomed") as conn:
        task_id = kb.create_task(conn, title="live", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

    bdir = kb.board_dir("doomed")
    ws = bdir / "workspaces" / task_id
    ws.mkdir(parents=True)
    (ws / "work.txt").write_text("hours\n", encoding="utf-8")

    with pytest.raises(ValueError, match="running or holding"):
        kb.remove_board("doomed", archive=False)

    assert (ws / "work.txt").exists(), "board delete took a live card's work"
    assert any("reason=remove_board" in ln for ln in _audit_lines())


def test_remove_board_delete_allowed_when_idle_and_audited(kanban_home):
    kb.create_board("spent", name="Spent")
    with kb.connect_closing(board="spent") as conn:
        task_id = kb.create_task(conn, title="done one", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()

    bdir = kb.board_dir("spent")
    assert bdir.is_dir()
    res = kb.remove_board("spent", archive=False)

    assert res["action"] == "deleted"
    assert not bdir.exists()
    assert any(
        "reason=remove_board" in ln and "\tDELETE\t" in ln
        for ln in _audit_lines()
    ), _audit_lines()


def test_remove_board_archive_is_unaffected(kanban_home):
    """Archiving is a rename, not a deletion -- the guard must not block it."""
    kb.create_board("kept", name="Kept")
    with kb.connect_closing(board="kept") as conn:
        task_id = kb.create_task(conn, title="live", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

    res = kb.remove_board("kept", archive=True)
    assert res["action"] == "archived"
    assert Path(res["new_path"]).is_dir()
