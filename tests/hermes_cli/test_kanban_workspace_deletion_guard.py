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
