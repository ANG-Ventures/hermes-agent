"""archive_task must stop the card's live worker (t_89dfa2c9).

Incident 2026-09-29 (t_cfbbf9a9): archiving a running card nulled worker_pid,
ended the run as ``reclaimed`` and rmtree'd the workspace, but never signalled
the worker. It ran 12 more minutes from a deleted cwd: every guard-hook call
failed closed (12 CRITICAL pages) and it armed launchd jobs for the dropped
card. Nothing could reap it afterwards: the pid pointer was gone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    # Real identity probe: the worker below is a real child created inside
    # the claim window, so it must verify on its own merits.
    monkeypatch.setattr(kb, "_pid_started_in_claim", kb._real_pid_started_in_claim)
    return home


def _running_card_with_worker(conn, pid_factory):
    tid = kb.create_task(conn, title="archive me", assignee="builder")
    host = kb._claimer_id().split(":", 1)[0]
    task = kb.claim_task(conn, tid, claimer=f"{host}:{os.getpid()}")
    assert task is not None
    time.sleep(1.1)  # process create time strictly after the claim second
    pid = pid_factory()
    assert kb._set_worker_pid(conn, tid, pid, run_id=task.current_run_id)
    return tid


def test_archive_terminates_live_worker(board):
    child = None

    def spawn():
        nonlocal child
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        return child.pid

    try:
        with kb.connect_closing() as conn:
            tid = _running_card_with_worker(conn, spawn)
            assert kb.archive_task(conn, tid) is True
            assert kb.get_task(conn, tid).status == "archived"
            kinds = [e.kind for e in kb.list_events(conn, tid)]
            assert "archive_worker_terminated" in kinds, kinds
        assert child.wait(timeout=10) is not None  # the worker is gone
    finally:
        if child is not None and child.poll() is None:
            child.kill()


def test_archive_never_signals_its_own_process(board, monkeypatch):
    sent = []
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker",
                        lambda *a, **k: sent.append(a) or {"terminated": True})
    with kb.connect_closing() as conn:
        tid = _running_card_with_worker(conn, os.getpid)
        assert kb.archive_task(conn, tid) is True
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert sent == []
    assert "archive_worker_terminated" not in kinds


def test_archive_without_worker_signals_nothing(board, monkeypatch):
    sent = []
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker",
                        lambda *a, **k: sent.append(a) or {"terminated": True})
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="idle", assignee="builder")
        assert kb.archive_task(conn, tid) is True
    assert sent == []
