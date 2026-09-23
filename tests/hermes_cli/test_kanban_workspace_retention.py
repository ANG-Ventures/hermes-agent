"""Done-card scratch workspaces must actually be reclaimed (card t_b99d8978).

Measured on the Mac Studio 2026-09-23: 307 ``done`` cards still held their
scratch workspaces (65 GB) because completion cleanup was REFUSED for nearly
all of them. The workspace-deletion audit named the reason on every line:
``owner-has-live-run owner=<ambiguous-owner>,t_…`` -- nine ``dir:`` cards
whose workspace was the kanban home itself (``dir:~/.hermes``) counted as
"owners" of every scratch dir beneath it. With one of them live the refusal
was owner-has-live-run; with none live it was <ambiguous-owner>. Either way
nothing was removed, and ``kanban gc`` only ever looked at ``archived`` rows,
so nothing retried.

These tests drive the real lanes against a sealed temp home.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _mktask(title: str) -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee="daedalus")


def _set(task_id: str, **cols) -> None:
    keys = ", ".join(f"{k}=?" for k in cols)
    with kb.connect_closing() as conn:
        conn.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*cols.values(), task_id))
        conn.commit()


def _scratch(task_id: str, status: str, *, finished_days_ago: float = 0.0) -> Path:
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    ws = root / task_id
    ws.mkdir()
    _set(task_id, status=status, workspace_kind="scratch", workspace_path=str(ws),
         completed_at=int(time.time() - finished_days_ago * 86400))
    return ws


def _home_rooted_dir_card(home: Path, status: str) -> str:
    """A ``dir:`` card whose workspace is the kanban home -- the live shape."""
    tid = _mktask(f"dir card on the home ({status})")
    extra = {"claim_expires": int(time.time()) + 3600} if status == "running" else {}
    _set(tid, status=status, workspace_kind="dir", workspace_path=str(home), **extra)
    return tid


@pytest.mark.parametrize("dir_card_states", [("running",), ("done", "done"),
                                             ("running", "done", "blocked")])
def test_home_rooted_dir_cards_do_not_own_scratch_workspaces(kanban_home, dir_card_states):
    for st in dir_card_states:
        _home_rooted_dir_card(kanban_home, st)
    tid = _mktask("finished scratch card")
    ws = _scratch(tid, "done")

    assert kb._live_owners_of_path(ws) == []
    with kb.connect_closing() as conn:
        kb._cleanup_workspace(conn, tid)
    assert not ws.exists(), "completion cleanup still refused by a home-rooted dir card"


def test_enclosing_scratch_card_is_still_an_owner(kanban_home):
    """Control: the exemption is only for paths ABOVE the workspaces root.

    A live card whose own scratch dir encloses the target still protects it.
    """
    live = _mktask("live enclosing card")
    enclosing = _scratch(live, "running")
    _set(live, claim_expires=int(time.time()) + 3600)
    victim = enclosing / "repo"
    victim.mkdir()
    (victim / "work.txt").write_text("unretained\n", encoding="utf-8")
    caller = _mktask("idle caller")
    _set(caller, status="archived")

    assert kb._live_owners_of_path(victim) == [live]
    assert not kb.safe_remove_workspace_dir(victim, task_id=caller, reason="t")
    assert (victim / "work.txt").exists()


def test_custom_dir_under_the_root_still_owns_nested_paths(kanban_home):
    """Control: a stored path strictly under the root (not task-named) is managed
    storage and keeps owning what is below it."""
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    custom = root / "custom"
    (custom / "repo").mkdir(parents=True)
    live = _mktask("live custom-dir card")
    _set(live, status="running", workspace_kind="dir", workspace_path=str(custom),
         claim_expires=int(time.time()) + 3600)
    assert kb._live_owners_of_path(custom / "repo") == [live]


def _gc(**kw):
    return kanban_cli._cmd_gc(argparse.Namespace(**kw))


def test_gc_reaps_old_done_workspaces_and_spares_every_protected_state(kanban_home):
    _home_rooted_dir_card(kanban_home, "running")
    old_done = _scratch(_mktask("old done"), "done", finished_days_ago=4)
    fresh_done = _scratch(_mktask("fresh done"), "done", finished_days_ago=1)
    protected = {}
    for st in ("running", "review", "blocked", "ready", "todo", "triage"):
        tid = _mktask(f"old {st}")
        protected[st] = _scratch(tid, st, finished_days_ago=30)
        if st == "running":
            _set(tid, claim_expires=int(time.time()) + 3600)

    assert _gc(done_retention_days=3) == 0

    assert not old_done.exists()
    assert fresh_done.is_dir()
    for st, ws in protected.items():
        assert ws.is_dir(), f"gc removed a {st} card's workspace"
    audit = kb.workspace_deletion_log_path().read_text(encoding="utf-8")
    assert f"\tDELETE\ttask={old_done.name}\t" in audit, "no ledger line for the reaped workspace"


def test_gc_done_retention_negative_disables_the_done_sweep(kanban_home):
    old_done = _scratch(_mktask("old done"), "done", finished_days_ago=10)
    assert _gc(done_retention_days=-1) == 0
    assert old_done.is_dir()


def test_gc_dry_run_deletes_nothing_and_lists_candidates(kanban_home, capsys):
    old_done = _scratch(_mktask("old done"), "done", finished_days_ago=10)
    archived = _scratch(_mktask("archived"), "archived", finished_days_ago=10)
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    old_log = log_dir / "t_deadbeef.log"
    old_log.write_text("x\n", encoding="utf-8")
    import os
    past = time.time() - 400 * 86400
    os.utime(old_log, (past, past))

    assert _gc(done_retention_days=3, dry_run=True) == 0

    out = capsys.readouterr().out
    assert old_done.is_dir() and archived.is_dir() and old_log.exists()
    assert str(old_done) in out and str(archived) in out
    assert "2 workspace candidate(s)" in out
