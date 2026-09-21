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
import sys
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
    # A REAL task row: the removal runs through
    # kanban_survivor.remove_workspace_dir, which records the capture against
    # the card and needs the row to exist.
    task_id = _mktask("finished one")
    ws = root / task_id
    ws.mkdir()
    (ws / "artifact.txt").write_text("x\n", encoding="utf-8")

    assert kb.safe_remove_workspace_dir(ws, task_id=task_id, reason="test") is True
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
    gone_id = _mktask("finished")
    gone = root / gone_id
    gone.mkdir()

    task_id = _mktask("live")
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()
    kept = root / task_id
    kept.mkdir()

    assert kb.safe_remove_workspace_dir(gone, task_id=gone_id, reason="gc_archived")
    assert not kb.safe_remove_workspace_dir(
        kept, task_id=task_id, reason="gc_archived"
    )

    lines = _audit_lines()
    deleted = [ln for ln in lines if "\tDELETE\t" in ln]
    refused = [ln for ln in lines if "\tREFUSED\t" in ln]
    assert len(deleted) == 1, lines
    assert len(refused) == 1, lines
    assert "task=%s" % gone_id in deleted[0]
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


# ---------------------------------------------------------------------------
# 6b. remove_board(archive=True) -- the DEFAULT branch (review round 6).
#
# Round 3 gated `archive=False` only. That is the branch a user must opt
# into (`boards rm --delete`, dashboard `?delete=true`); the default branch
# renamed <root>/kanban/boards/<slug>/ -- which CONTAINS that board's
# workspaces/t_* -- out from under a RUNNING card with no refusal and no
# audit line anywhere under HERMES_HOME. A live worker's cwd vanishing is
# the 2026-09-20 incident's literal symptom, and deliverable 2b says log
# EVERY workspace deletion.
#
# An earlier revision of this file asserted the opposite ("archiving is a
# rename, so the guard must not block it"). That reasoning is wrong for the
# property being defended: recoverability of bytes is not liveness of the
# process holding the path.
# ---------------------------------------------------------------------------


def test_remove_board_archive_refuses_while_a_card_is_running(kanban_home):
    """Born red at PR head cba079babd: archived a live card's board silently."""
    kb.create_board("kept", name="Kept")
    with kb.connect_closing(board="kept") as conn:
        task_id = kb.create_task(conn, title="live", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

    bdir = kb.board_dir("kept")
    ws = bdir / "workspaces" / task_id
    ws.mkdir(parents=True)
    (ws / "work.txt").write_text("hours\n", encoding="utf-8")

    with pytest.raises(ValueError, match="running or holding"):
        kb.remove_board("kept", archive=True)

    assert bdir.is_dir(), "archive moved a live card's board away"
    assert (ws / "work.txt").exists(), "the live card's cwd was relocated"
    assert any(
        "reason=remove_board" in ln and "\tREFUSED\t" in ln and "action=archive" in ln
        for ln in _audit_lines()
    ), _audit_lines()


def test_remove_board_refusal_leaves_the_active_board_pin_intact(kanban_home):
    """A REFUSED removal must not silently re-point the operator at `default`.

    FleetReview on #785: ``remove_board`` cleared ``<root>/kanban/current``
    before running the liveness gate, so an operator who ran
    ``boards switch mine`` then ``boards rm mine`` while a card was running
    got the (correct) refusal -- and every subsequent ``kanban add`` / ``list``
    / ``dispatch`` silently addressed the DEFAULT board, with no message
    saying the pin had moved.
    """
    kb.create_board("pinned", name="Pinned")
    kb.set_current_board("pinned")
    assert kb.get_current_board() == "pinned"

    with kb.connect_closing(board="pinned") as conn:
        task_id = kb.create_task(conn, title="live", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

    with pytest.raises(ValueError, match="running or holding"):
        kb.remove_board("pinned", archive=True)

    assert kb.board_dir("pinned").is_dir()
    assert kb.get_current_board() == "pinned", (
        "a refused removal reset the operator's active-board pin"
    )


@pytest.mark.parametrize("archive", [False, True], ids=["delete", "archive"])
def test_remove_board_failure_leaves_the_active_board_pin_intact(
    kanban_home, monkeypatch, archive
):
    """The pin moves only after the board directory actually goes away."""
    kb.create_board("pinned", name="Pinned")
    kb.set_current_board("pinned")
    bdir = kb.board_dir("pinned")

    if archive:
        original_rename = Path.rename

        def fail_board_rename(path, target):
            if path == bdir:
                raise OSError("forced archive failure")
            return original_rename(path, target)

        monkeypatch.setattr(Path, "rename", fail_board_rename)
        expected = OSError
    else:
        monkeypatch.setattr(
            "hermes_cli.kanban_survivor.remove_workspace_dir",
            lambda *_args, **_kwargs: False,
        )
        expected = ValueError

    with pytest.raises(expected):
        kb.remove_board("pinned", archive=archive)

    assert bdir.is_dir()
    assert kb.get_current_board() == "pinned", (
        "a failed removal reset the operator's active-board pin"
    )


@pytest.mark.parametrize("archive", [False, True], ids=["delete", "archive"])
def test_successful_remove_board_clears_the_active_board_pin(kanban_home, archive):
    """ALLOW control: a successful removal still reverts the pin to default."""
    kb.create_board("pinned", name="Pinned")
    kb.set_current_board("pinned")
    bdir = kb.board_dir("pinned")

    result = kb.remove_board("pinned", archive=archive)

    assert result["action"] == ("archived" if archive else "deleted")
    assert not bdir.exists()
    assert kb.get_current_board() == "default"


def test_board_liveness_gate_ignores_an_ambient_db_pin(kanban_home, monkeypatch):
    """``HERMES_KANBAN_DB`` must not be able to answer for another board.

    FleetReview on #785: ``_board_has_live_cards`` reached the board through
    ``connect_closing(board=slug)``, and ``kanban_db_path`` gives the ambient
    pin precedence even over an explicit board argument. In the routinely
    pinned worker environment that means removing board B inspects board A's
    tasks, concludes B is idle, and archives it out from under a live worker.
    """
    kb.create_board("busy", name="Busy")
    kb.create_board("idle", name="Idle")
    kb.init_db(board="idle")
    with kb.connect_closing(board="busy") as conn:
        task_id = kb.create_task(conn, title="live", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        conn.commit()

    # Pin the process at the IDLE board, as the dispatcher pins every worker.
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.board_dir("idle") / "kanban.db"))

    assert kb._board_has_live_cards("busy") == [task_id], (
        "the ambient DB pin answered the liveness question for another board"
    )
    with pytest.raises(ValueError, match="running or holding"):
        kb.remove_board("busy", archive=True)
    assert kb.board_dir("busy").is_dir()


def test_remove_board_archive_refuses_on_a_live_claim_lock(kanban_home):
    """Liveness is status OR an unexpired claim lock, on this branch too."""
    kb.create_board("locked", name="Locked")
    with kb.connect_closing(board="locked") as conn:
        task_id = kb.create_task(conn, title="claimed", assignee="daedalus")
        conn.execute(
            "UPDATE tasks SET status='ready', claim_expires=? WHERE id=?",
            (int(time.time()) + 900, task_id),
        )
        conn.commit()

    with pytest.raises(ValueError, match="running or holding"):
        kb.remove_board("locked", archive=True)
    assert kb.board_dir("locked").is_dir()


def test_remove_board_archive_allowed_when_idle_and_audited(kanban_home):
    """ALLOW control: the refusal is not bought by refusing every archive.

    Also pins deliverable 2b on the happy path -- ATTEMPT before the move,
    ARCHIVE after it, both naming the destination, both written OUTSIDE the
    tree that moved.
    """
    kb.create_board("stale", name="Stale")
    with kb.connect_closing(board="stale") as conn:
        task_id = kb.create_task(conn, title="finished", assignee="daedalus")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()

    bdir = kb.board_dir("stale")
    ws = bdir / "workspaces" / task_id
    ws.mkdir(parents=True)
    (ws / "work.txt").write_text("finished work\n", encoding="utf-8")

    res = kb.remove_board("stale", archive=True)

    assert res["action"] == "archived"
    moved = Path(res["new_path"])
    assert moved.is_dir()
    assert not bdir.exists()
    assert (moved / "workspaces" / task_id / "work.txt").is_file(), (
        "archive must stay recoverable"
    )

    lines = _audit_lines()
    attempts = [
        ln for ln in lines
        if "reason=remove_board" in ln and "\tATTEMPT\t" in ln and "action=archive" in ln
    ]
    archived = [
        ln for ln in lines
        if "reason=remove_board" in ln and "\tARCHIVE\t" in ln
    ]
    assert attempts, lines
    assert archived, lines
    assert lines.index(attempts[0]) < lines.index(archived[0]), (
        "ATTEMPT must precede the move it records"
    )
    assert str(moved) in archived[0], archived[0]


def test_remove_board_archive_audit_survives_the_move(kanban_home):
    """The audit log must not be inside the tree that just got relocated."""
    kb.create_board("movedaway", name="Moved")
    res = kb.remove_board("movedaway", archive=True)

    log = kb.workspace_deletion_log_path()
    assert log.is_file(), "no audit log at all after an archive"
    moved = Path(res["new_path"]).resolve()
    assert not log.resolve().is_relative_to(moved), (
        f"audit log {log} travelled with the archived board {moved}"
    )
    # And it is findable by the same whole-HOME sweep the reviewer ran.
    found = list((kanban_home / "kanban").rglob("workspace-deletions.log"))
    assert found, "rglob over HERMES_HOME found no audit file"


# ---------------------------------------------------------------------------
# 7. Audit DURABILITY and audit TRUTHFULNESS (card t_63fb42f9, review round 4)
#
# Two measured defects in the round-3 audit, both on the board lane:
#
# * The destination was inferred, and _managed_scratch_path_info cannot infer
#   a board from a board ROOT (it only matches scratch descendants). With
#   HERMES_KANBAN_BOARD pinned to the board being deleted, worker_logs_dir(None)
#   resolved INTO the doomed directory, so a successful deletion destroyed its
#   own audit: `action='deleted'`, remaining audit files `[]`.
# * DELETE was written BEFORE the executor ran. A survivor-held board left a
#   DELETE line for a board that still exists -- the log was simply false.
#
# The fix is a class fix, not a board patch: _durable_audit_log_path refuses
# any destination inside the deletion target for EVERY call site, and all
# irreversible lanes write ATTEMPT -> {DELETE|FAILED|REFUSED} around the
# executor instead of a verdict in front of it.
# ---------------------------------------------------------------------------


def _all_audit_lines(home: Path) -> list:
    """Every audit line anywhere under the temp HOME, wherever it landed."""
    lines = []
    for log in home.rglob("*workspace-deletions.log"):
        lines += [
            ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
    return lines


def test_board_delete_audit_survives_when_the_active_board_is_the_target(
    kanban_home, monkeypatch
):
    """The env-pinned named-board case: the audit must outlive the board."""
    kb.create_board("idle-audit", name="Idle audit")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "idle-audit")

    bdir = kb.board_dir("idle-audit")
    res = kb.remove_board("idle-audit", archive=False)
    assert res["action"] == "deleted"
    assert not bdir.exists()

    lines = _all_audit_lines(kanban_home)
    assert lines, "a successful board deletion destroyed its own audit"
    survivors = [Path(p) for p in kanban_home.rglob("*workspace-deletions.log")]
    assert survivors, "no audit file survived the deletion"
    for log in survivors:
        assert not log.resolve().is_relative_to(bdir.resolve()), (
            "audit was written inside the directory being deleted: %s" % log
        )
    mine = [ln for ln in lines if "board=idle-audit" in ln]
    assert any("\tATTEMPT\t" in ln for ln in mine), mine
    assert any("\tDELETE\t" in ln for ln in mine), mine


def test_board_delete_refusal_is_not_recorded_as_a_deletion(kanban_home):
    """A survivor-held board must not leave a DELETE line; it still exists."""
    from hermes_cli.kanban_survivor import SurvivorUnavailable

    kb.create_board("retained-audit", name="Retained audit")
    bdir = kb.board_dir("retained-audit")
    ws = bdir / "workspaces" / "t_test"
    ws.mkdir(parents=True)
    (ws / "unretained.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(SurvivorUnavailable):
        kb.remove_board("retained-audit", archive=False)

    assert bdir.is_dir(), "the board was supposed to be held"
    mine = [
        ln for ln in _all_audit_lines(kanban_home)
        if "board=retained-audit" in ln
    ]
    assert mine, "a refused board deletion left no audit trail"
    assert any("\tATTEMPT\t" in ln for ln in mine), mine
    assert not any("\tDELETE\t" in ln for ln in mine), (
        "audit claims a DELETE for a board that still exists: %s" % mine
    )
    assert any(
        "\tREFUSED\t" in ln or "\tFAILED\t" in ln for ln in mine
    ), mine


def test_scratch_lane_records_the_terminal_outcome_not_just_the_attempt(
    kanban_home,
):
    """ATTEMPT before the rmtree, DELETE only after it actually returned."""
    root = _scratch_root()
    task_id = _mktask("done one")
    ws = root / task_id
    ws.mkdir()

    assert kb.safe_remove_workspace_dir(ws, task_id=task_id, reason="unit") is True
    assert not ws.exists()

    mine = [ln for ln in _audit_lines() if "task=%s" % task_id in ln]
    assert any("\tATTEMPT\t" in ln for ln in mine), mine
    assert any("\tDELETE\t" in ln for ln in mine), mine
    assert mine.index([ln for ln in mine if "\tATTEMPT\t" in ln][0]) < mine.index(
        [ln for ln in mine if "\tDELETE\t" in ln][0]
    ), "DELETE must follow the ATTEMPT, not precede the removal"


def test_worktree_lane_records_attempt_then_delete(kanban_home, linked_worktree):
    """Control: the worktree lane keeps the same ATTEMPT/DELETE semantics."""
    _repo, wt = linked_worktree
    task_id = _mktask("worktree control")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', workspace_kind='worktree', "
            "workspace_path=? WHERE id=?",
            (str(wt), task_id),
        )
        conn.commit()
        kb._cleanup_worktree_workspace(
            task_id, str(wt), None, conn=conn, reason="unit"
        )

    assert not wt.is_dir(), "clean pushed worktree should have been removed"
    mine = [ln for ln in _audit_lines() if "task=%s" % task_id in ln]
    assert any("\tATTEMPT\t" in ln for ln in mine), mine
    assert any("\tDELETE\t" in ln for ln in mine), mine


def test_audit_never_writes_inside_the_directory_it_is_recording(kanban_home):
    """The class invariant, asserted directly on the helper.

    Whatever the target, the chosen destination is never inside it -- this
    is what makes the board case safe without a board-specific special case.
    """
    targets = [
        kanban_home / "kanban",
        kanban_home / "kanban" / "logs",
        kanban_home / "kanban" / "workspaces",
        kb.board_dir("some-board"),
        kanban_home,
    ]
    for target in targets:
        chosen = kb._durable_audit_log_path(target.resolve(), None)
        resolved = chosen.resolve()
        assert resolved != target.resolve(), target
        assert not resolved.is_relative_to(target.resolve()), (
            "%s would be destroyed by a deletion of %s" % (chosen, target)
        )


# ---------------------------------------------------------------------------
# 5. Liveness is a property of the PATH, not of the caller's argument
#
# Review round 5: `_task_has_live_run(conn, task_id)` only ever answered "is
# the card the CALLER named live?". Both executing lanes take the path as a
# separate argument, so any row whose `workspace_path` points at another
# card's live workspace deleted it -- logging a clean ATTEMPT/DELETE pair
# against the wrong card. That is the incident's own shape: a deleter
# destroying directories it does not own, with no usable trail.
# ---------------------------------------------------------------------------


def test_scratch_lane_refuses_a_live_card_dir_named_by_a_non_owner(kanban_home):
    """A caller that names an idle card cannot delete a live card's dir."""
    root = _scratch_root()
    live_id = _mktask("RUNNING card B")
    idle_id = _mktask("idle card A")
    victim = root / live_id
    victim.mkdir()
    (victim / "work.txt").write_text("B's unretained work\n", encoding="utf-8")

    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', claim_expires=?, "
            "workspace_path=? WHERE id=?",
            (int(time.time()) + 3600, str(victim), live_id),
        )
        # A is archived and its row points at B's directory -- the gc shape.
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_path=? WHERE id=?",
            (str(victim), idle_id),
        )
        conn.commit()

    assert kb.safe_remove_workspace_dir(
        victim, task_id=idle_id, reason="unit_crossowner",
    ) is False
    assert victim.is_dir()
    assert (victim / "work.txt").exists()

    refused = [
        ln for ln in _audit_lines()
        if "\tREFUSED\t" in ln and str(victim) in ln
    ]
    assert refused, "a cross-owner refusal left no audit trail"
    assert "owner-has-live-run" in refused[-1], refused[-1]
    # The log must name the OWNING card, not only the caller.
    assert "owner=%s" % live_id in refused[-1], refused[-1]


def test_scratch_lane_refuses_by_directory_name_when_no_path_row_exists(
    kanban_home,
):
    """Ownership also follows the ``<root>/t_<hex>`` naming convention.

    Rows created before ``workspace_path`` was stored have no path to match
    on; the directory name is the only ownership evidence and must still be
    honoured.
    """
    root = _scratch_root()
    live_id = _mktask("RUNNING card, no stored path")
    idle_id = _mktask("idle caller")
    victim = root / live_id
    victim.mkdir()

    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', workspace_path=NULL WHERE id=?",
            (live_id,),
        )
        conn.execute(
            "UPDATE tasks SET status='archived' WHERE id=?", (idle_id,)
        )
        conn.commit()

    assert kb.safe_remove_workspace_dir(
        victim, task_id=idle_id, reason="unit_crossowner_byname",
    ) is False
    assert victim.is_dir()


def test_idle_card_dir_named_by_another_card_is_still_removable(kanban_home):
    """Control: owner-derived liveness must not freeze ordinary cleanup."""
    root = _scratch_root()
    owner_id = _mktask("finished owner")
    caller_id = _mktask("archived caller")
    ws = root / owner_id
    ws.mkdir()

    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', workspace_path=? WHERE id=?",
            (str(ws), owner_id),
        )
        conn.execute(
            "UPDATE tasks SET status='archived' WHERE id=?",
            (caller_id,),
        )
        conn.commit()

    assert kb.safe_remove_workspace_dir(
        ws, task_id=caller_id, reason="unit_idle_owner",
    ) is True
    assert not ws.exists()
    # ...and the successful line still names who actually owned the dir.
    deleted = [ln for ln in _audit_lines() if "\tDELETE\t" in ln and str(ws) in ln]
    assert deleted, _audit_lines()
    assert "owner=%s" % owner_id in deleted[-1], deleted[-1]


def test_worktree_lane_refuses_a_live_card_checkout_named_by_a_non_owner(
    kanban_home, linked_worktree, monkeypatch,
):
    """The guard must return BEFORE the executor -- not lean on git's refusal.

    Round 5 measured this lane surviving only because ``git worktree
    remove`` said no underneath. That backstop disappears for a detached
    directory, so the assertion here is that the survivor executor is never
    reached at all.
    """
    _repo, wt = linked_worktree
    live_id = _mktask("RUNNING worktree card")
    idle_id = _mktask("idle caller")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', claim_expires=?, "
            "workspace_path=?, workspace_kind='worktree' WHERE id=?",
            (int(time.time()) + 3600, str(wt), live_id),
        )
        conn.execute(
            "UPDATE tasks SET status='archived' WHERE id=?", (idle_id,)
        )
        conn.commit()

    called = []
    from hermes_cli import kanban_survivor

    def _tripwire(*args, **kwargs):
        called.append(args)
        raise AssertionError("executor reached for a live card's checkout")

    monkeypatch.setattr(kanban_survivor, "remove_workspace_dir", _tripwire)

    kb._cleanup_worktree_workspace(
        idle_id, str(wt), None, reason="unit_crossowner_wt",
    )

    assert not called, "the guard did not refuse before the executor"
    assert Path(wt).is_dir()
    refused = [
        ln for ln in _audit_lines()
        if "\tREFUSED\t" in ln and "owner-has-live-run" in ln
    ]
    assert refused, _audit_lines()
    assert "owner=%s" % live_id in refused[-1], refused[-1]


def test_live_owner_lookup_fails_closed_on_an_unreadable_db(
    kanban_home, monkeypatch,
):
    """Consistent with ``_task_has_live_run``: unknown owner => refuse."""
    root = _scratch_root()
    ws = root / _mktask("some card")
    ws.mkdir()

    def _boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(kb, "connect_closing", _boom)
    owners = kb._live_owners_of_path(ws)
    assert owners and owners != [], "an unreadable DB must not read as idle"


@pytest.mark.parametrize("ownership", ["unknown", "ambiguous", "nested", "ancestor"])
def test_owner_resolution_refuses_unsafe_path_shapes(kanban_home, ownership):
    root = _scratch_root()
    caller = _mktask("idle caller")
    owner = _mktask("workspace owner")
    workspace = root / owner if ownership == "nested" else root / "custom"
    victim = workspace / "repo" if ownership == "nested" else workspace
    victim.mkdir(parents=True)
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id IN (?, ?)", (caller, owner))
        if ownership == "ambiguous":
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id IN (?, ?)",
                         (str(workspace), caller, owner))
        elif ownership == "nested":
            conn.execute("UPDATE tasks SET status='running', workspace_path=NULL WHERE id=?", (owner,))
        elif ownership == "ancestor":
            nested = workspace / "live-checkout"
            nested.mkdir()
            conn.execute("UPDATE tasks SET status='running', workspace_path=? WHERE id=?",
                         (str(nested), owner))
        conn.commit()
        assert not kb.safe_remove_workspace_dir(victim, task_id=caller, reason="shape", conn=conn)
    assert victim.is_dir()
    assert any("\tREFUSED\t" in line for line in _audit_lines())


# ---------------------------------------------------------------------------
# 6. CLASS LOCK -- every user-facing deletion lane, driven for real
#
# Rounds 1-5 each found the SAME defect in a lane that had not been checked
# yet: gc (root-equality), the worktree lane (no liveness, no audit), the
# board lane (audit died with its target), and finally cross-owner liveness
# in both executing lanes. The pattern is that a per-site fix leaves the
# next lane free to carry the bug, so the lock below enumerates the lanes
# and drives each one for real against the same adversarial shape: a caller
# that legitimately owns an idle card, pointed at a LIVE card's directory.
#
# This is deliberately NOT a source-reading/AST test (AGENTS.md bans those):
# each lane is executed end-to-end and judged on the directory and the audit
# log, so a lane that is rewired but still correct stays green, and a lane
# that is refactored into a new code path is still covered.
# ---------------------------------------------------------------------------


def _crossowner_fixture():
    """A LIVE card owning a real dir, plus an idle caller pointed at it."""
    root = _scratch_root()
    live_id = _mktask("RUNNING owner")
    caller_id = _mktask("idle caller")
    victim = root / live_id
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "work.txt").write_text("unretained work\n", encoding="utf-8")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='running', claim_expires=?, "
            "workspace_kind='scratch', workspace_path=? WHERE id=?",
            (int(time.time()) + 3600, str(victim), live_id),
        )
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(victim), caller_id),
        )
        conn.commit()
    return live_id, caller_id, victim


def _lane_gc(live_id, caller_id, victim):
    kanban_cli._cmd_gc(argparse.Namespace())


def _lane_choke_point(live_id, caller_id, victim):
    kb.safe_remove_workspace_dir(
        victim, task_id=caller_id, reason="class_lock",
    )


def _lane_complete_task(live_id, caller_id, victim):
    with kb.connect_closing() as conn:
        kb._cleanup_workspace(conn, caller_id)


def _lane_deferred_parent(live_id, caller_id, victim):
    """The parent-cleanup lane: a done child releases the parent's dir."""
    child_id = _mktask("done child")
    with kb.connect_closing() as conn:
        kb.link_tasks(conn, caller_id, child_id)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child_id,))
        conn.commit()
        kb._try_cleanup_parent_workspaces(conn, child_id)


DELETION_LANES = {
    "gc": _lane_gc,
    "choke_point": _lane_choke_point,
    "complete_task": _lane_complete_task,
    "deferred_parent_cleanup": _lane_deferred_parent,
}


@pytest.mark.parametrize("lane", sorted(DELETION_LANES))
def test_no_deletion_lane_removes_a_live_cards_workspace(kanban_home, lane):
    """No lane may delete a directory whose OWNER is live.

    Each round of review found this in one more lane. Adding a lane to the
    product without adding it here is the regression this test exists to
    make loud: the parametrisation is the contract.
    """
    live_id, caller_id, victim = _crossowner_fixture()

    DELETION_LANES[lane](live_id, caller_id, victim)

    assert victim.is_dir(), f"lane {lane!r} deleted a live card's workspace"
    assert (victim / "work.txt").exists(), (
        f"lane {lane!r} destroyed a live card's unretained work"
    )
    deleted = [
        ln for ln in _audit_lines()
        if "\tDELETE\t" in ln and str(victim) in ln
    ]
    assert not deleted, f"lane {lane!r} logged a DELETE it must not perform"


@pytest.mark.parametrize("lane", ["gc", "choke_point", "complete_task"])
def test_every_deletion_lane_still_removes_an_idle_cards_workspace(
    kanban_home, lane,
):
    """Control: the class lock above must not be satisfied by a no-op.

    A guard that refuses everything passes the cross-owner test and breaks
    the product. Each lane must still delete a directory whose owner is
    genuinely idle.
    """
    root = _scratch_root()
    owner_id = _mktask("idle owner")
    ws = root / owner_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "junk.txt").write_text("junk\n", encoding="utf-8")
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch', "
            "workspace_path=? WHERE id=?",
            (str(ws), owner_id),
        )
        conn.commit()

    DELETION_LANES[lane](owner_id, owner_id, ws)

    assert not ws.exists(), f"lane {lane!r} became a no-op"


# ---------------------------------------------------------------------------
# 9. The deletion audit is not reapable by routine log maintenance
#    (card t_63fb42f9, review round 7)
#
# `workspace_deletion_log_path` puts the audit in `worker_logs_dir(board)` --
# the same directory as the disposable per-task `t_*.log` worker logs.
# `gc_worker_logs` swept that directory with a bare `iterdir()` and no name
# filter, so the audit was reaped at --log-retention-days (default 30). And it
# is the SAME command: `_cmd_gc` deletes archived workspaces and then calls
# gc_worker_logs, so the lane that performs deletions destroyed the record of
# them. Measured through the real CLI on a sealed temp HERMES_HOME:
# "GC complete: 0 workspace(s), 0 event row(s), 1 log file(s) removed" with
# the audit gone afterwards.
#
# The failure is quiet in the obvious test: a gc run that DOES reap a
# workspace writes to the audit first, refreshing its mtime, so the log
# survives. It only dies on a run with nothing to reap -- the steady state.
# Every test below therefore ages the audit past retention with no concurrent
# deletion, and each pairs with an ALLOW control proving the reaper still
# reaps ordinary worker logs.
# ---------------------------------------------------------------------------


_YEAR_AGO = 400 * 24 * 3600


def _age(path: Path, seconds: int = _YEAR_AGO) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _seed_aged_logs(board=None):
    """Audit log + an ordinary worker log, both aged past any retention."""
    log_dir = kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    audit = kb.workspace_deletion_log_path(board=board)
    audit.write_text(
        "2026-09-20T13:52:00\tDELETE\ttask=t_4e750964\tpath=/w\n"
        "2026-09-20T13:54:00\tDELETE\ttask=t_4e750964\tpath=/w\n",
        encoding="utf-8",
    )
    ordinary = log_dir / "t_deadbeef.log"
    ordinary.write_text("worker noise\n", encoding="utf-8")
    _age(audit)
    _age(ordinary)
    return audit, ordinary


def test_gc_worker_logs_does_not_reap_the_deletion_audit(kanban_home):
    """The helper: an aged audit survives, an aged worker log does not."""
    audit, ordinary = _seed_aged_logs()

    removed = kb.gc_worker_logs(older_than_seconds=30 * 24 * 3600)

    assert audit.is_file(), "routine log GC deleted the deletion audit"
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 2, (
        "audit history was truncated"
    )
    # ALLOW control -- the reaper must not be neutered into a no-op.
    assert not ordinary.exists(), "gc_worker_logs stopped reaping worker logs"
    assert removed == 1, f"expected exactly the worker log to be removed, got {removed}"


def test_real_kanban_gc_cli_does_not_reap_the_deletion_audit(kanban_home):
    """The REPORTED surface: `hermes kanban gc` as a real subprocess.

    The helper alone is not what an operator runs. This drives the actual
    CLI entry point in a child process against the same temp HERMES_HOME,
    which is the shape that printed "1 log file(s) removed" for the audit.
    """
    audit, ordinary = _seed_aged_logs()

    env = dict(os.environ)
    env["HERMES_HOME"] = str(kanban_home)
    env.pop("HERMES_KANBAN_BOARD", None)
    repo_root = Path(kb.__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    res = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "gc"],
        cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=300,
    )

    assert res.returncode == 0, res.stderr[-2000:]
    assert audit.is_file(), (
        "`hermes kanban gc` deleted the deletion audit; stdout=%r" % res.stdout
    )
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 2
    # ALLOW control -- ordinary worker logs past retention are still reaped.
    assert not ordinary.exists(), "gc stopped reaping ordinary worker logs"
    assert "1 log file(s) removed" in res.stdout, res.stdout


def test_gc_spares_the_audit_on_a_named_board_too(kanban_home):
    """Both audit destinations: the named board's logs/ dir, not just default."""
    kb.create_board("auditboard", name="Audit board")
    audit, ordinary = _seed_aged_logs(board="auditboard")
    assert audit.parent == kb.worker_logs_dir(board="auditboard")

    removed = kb.gc_worker_logs(
        older_than_seconds=30 * 24 * 3600, board="auditboard",
    )

    assert audit.is_file(), "named-board audit was reaped by log GC"
    assert not ordinary.exists()
    assert removed == 1


def test_gc_spares_rotated_generations_of_the_audit(kanban_home):
    """A rotated audit is still audit history, not a disposable worker log."""
    audit, ordinary = _seed_aged_logs()
    rotated = audit.with_name(audit.name + ".1")
    rotated.write_text("older history\n", encoding="utf-8")
    _age(rotated)

    kb.gc_worker_logs(older_than_seconds=30 * 24 * 3600)

    assert audit.is_file()
    assert rotated.is_file(), "a rotated audit generation was reaped"
    assert not ordinary.exists()


def test_rotate_worker_log_refuses_the_audit_even_with_backup_count_zero(
    kanban_home,
):
    """The second site in the class: rotation must not unlink the audit.

    `_rotate_worker_log` is currently only ever called on `<task>.log`, so
    this is unreachable today -- but with backup_count=0 it does a bare
    `unlink()`, which would destroy the audit silently if a future caller
    ever pointed it there. The guard lives in the function, not in the call
    sites, so the site cannot become reachable later.
    """
    audit, _ordinary = _seed_aged_logs()
    audit.write_text("x" * 5000, encoding="utf-8")

    kb._rotate_worker_log(audit, max_bytes=10, backup_count=0)
    assert audit.is_file(), "rotation with backup_count=0 unlinked the audit"

    kb._rotate_worker_log(audit, max_bytes=10, backup_count=3)
    assert audit.is_file(), "rotation renamed the audit out from under readers"
    assert not audit.with_name(audit.name + ".1").exists()

    # ALLOW control -- an ordinary oversized worker log still rotates.
    plain = kb.worker_logs_dir() / "t_rotateme.log"
    plain.write_text("y" * 5000, encoding="utf-8")
    kb._rotate_worker_log(plain, max_bytes=10, backup_count=1)
    assert not plain.exists(), "rotation stopped rotating ordinary worker logs"
    assert plain.with_name(plain.name + ".1").is_file()


def test_is_deletion_audit_path_recognises_every_durable_destination(kanban_home):
    """The predicate must cover all four _durable_audit_log_path fallbacks.

    An audit that lands in a fallback location is still an audit; a
    name-based guard that only knew the default-board basename would leave
    the outward fallbacks reapable by any future sweep of their directory.
    """
    for name in (
        "workspace-deletions.log",
        "workspace-deletions.log.1",
        "workspace-deletions.log.7",
        "kanban-workspace-deletions.log",
        "hermes-workspace-deletions.log",
    ):
        assert kb.is_deletion_audit_path(Path("/any/dir") / name), name

    for name in (
        "t_deadbeef.log",
        "t_deadbeef.log.1",
        "dispatcher.log",
        "workspace-deletions.log.bak",
        "workspace-deletions.txt",
    ):
        assert not kb.is_deletion_audit_path(Path("/any/dir") / name), name


# ---------------------------------------------------------------------------
# Cost. The choke point is on `kanban gc`'s hot path, once per archived
# scratch row, and in steady state almost every one of those workspaces was
# already removed at completion (FleetReview on PR #785).
# ---------------------------------------------------------------------------

def test_a_vanished_workspace_costs_no_owner_scan_and_no_audit_line(kanban_home):
    """An absent directory must short-circuit before the expensive gates.

    `_live_owners_of_path` opens a connection and full-scans `tasks`,
    resolving every row's path. Running that for a path with nothing at it --
    and appending a permanent REFUSED line to a log that is deliberately
    never reaped -- turned `kanban gc` into minutes of syscalls and unbounded
    audit noise for zero removals.
    """
    root = _scratch_root()
    task_id = _mktask("already cleaned")
    gone = root / task_id
    assert not gone.exists()

    before = len(_audit_lines())
    scans = {"n": 0}
    real = kb._live_owners_of_path

    def counting(*a, **kw):
        scans["n"] += 1
        return real(*a, **kw)

    kb._live_owners_of_path = counting
    try:
        with kb.connect_closing() as conn:
            removed = kb.safe_remove_workspace_dir(
                gone, task_id=task_id, reason="gc_archived", conn=conn,
            )
    finally:
        kb._live_owners_of_path = real

    assert removed is False
    assert scans["n"] == 0, "a vanished workspace still paid for an owner scan"
    assert len(_audit_lines()) == before, (
        "a vanished workspace appended a permanent, never-reapable audit line"
    )


def test_owner_lookup_reuses_the_callers_connection_for_the_same_board(kanban_home):
    """No second connection to a database the caller already has open.

    Completion calls this while holding the completing task's connection; a
    redundant connection to the same file is pure cost on that path.
    """
    root = _scratch_root()
    task_id = _mktask("owner")
    ws = root / task_id
    ws.mkdir()
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id=?", (str(ws), task_id)
        )
        conn.commit()

        opened: list = []
        real = kb.connect

        def counting(*a, **kw):
            opened.append(kw.get("board"))
            return real(*a, **kw)

        kb.connect = counting
        try:
            owners = kb._live_owners_of_path(ws, conn=conn, live_only=False)
        finally:
            kb.connect = real

    assert owners == [task_id], owners
    assert opened == [], f"opened a redundant connection: {opened}"


def test_completion_commits_before_cleanup_so_the_workspace_is_removed(kanban_home):
    """DISPUTE evidence for the "Completion cleanup gated" finding (#785).

    The finding argued that `complete_task` passes the completing card's own
    id into the new liveness gates, so if the terminal status and the claim
    were not already committed, EVERY successful completion would silently
    skip workspace removal and completed workspaces would accumulate.

    Measured: they are. `complete_task` writes `status='done'`,
    `claim_lock=NULL`, `claim_expires=NULL` inside `write_txn` BEFORE
    `_cleanup_workspace` runs, so self-liveness is already False by then and
    the directory is removed. This test pins that ORDERING, which is the load
    bearing fact -- if a future change moves cleanup inside the transaction,
    the gate really would refuse every completion and this goes red.
    """
    root = _scratch_root()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="completes", assignee="daedalus", workspace_kind="scratch",
        )
        ws = root / task_id
        ws.mkdir(parents=True)
        (ws / "work.txt").write_text("ephemeral\n", encoding="utf-8")
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id=?", (str(ws), task_id)
        )
        conn.commit()

        # Claim it exactly as the dispatcher does: running + a live lock, the
        # two conditions `_task_has_live_run` refuses on.
        assert kb.claim_task(conn, task_id, claimer="probe:1", ttl_seconds=900)
        assert kb.get_task(conn, task_id).status == "running"

        assert kb.complete_task(conn, task_id, result="done", summary="s") is True

        row = conn.execute(
            "SELECT status, claim_expires FROM tasks WHERE id=?", (task_id,)
        ).fetchone()

    assert row["status"] == "done"
    assert row["claim_expires"] is None
    assert not ws.exists(), (
        "completion left its scratch workspace behind — the self-liveness "
        "gate refused a card that had already been committed terminal"
    )
    lines = _audit_lines()
    assert any("\tDELETE\t" in ln and "reason=complete_task" in ln for ln in lines), lines
