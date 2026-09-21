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
