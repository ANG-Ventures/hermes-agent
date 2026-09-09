"""Quota release must survive another gateway subsystem reaping the worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    kb._recent_worker_exits.clear()
    with kb.connect_closing() as conn:
        yield conn
    kb._recent_worker_exits.clear()


def claim(conn):
    tid = kb.create_task(conn, title="quota probe", assignee="worker")
    task = kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, 99999999)
    return task


def receipt(task, code):
    path = kb.kanban_db_path().parent / "runs" / f"{task.id}.{task.current_run_id}.exit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exit_code": code, "failure_reason": "rate_limit", "ts": time.time()}))
    return path


@pytest.mark.parametrize("code,outcome,failures", [(75, "rate_limited", 0), (1, "crashed", 1)])
def test_sidecar_drives_outcome(board, monkeypatch, code, outcome, failures):
    task = claim(board)
    receipt(task, code)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    current = kb.get_task(board, task.id)
    assert current.status == "ready"
    assert current.consecutive_failures == failures
    run = board.execute("SELECT * FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()
    assert run["outcome"] == outcome
    assert json.loads(run["metadata"])["exit_code"] == code
    if code == 75:
        assert current.next_eligible_at >= int(time.time()) + 295
        assert kb.check_respawn_guard(board, task.id) == "rate_limit_cooldown"


def test_no_sidecar_retains_reaped_status_fallback(board, monkeypatch):
    task = claim(board)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb._record_worker_exit(99999999, 75 << 8)
    kb.detect_crashed_workers(board)
    assert kb.get_task(board, task.id).consecutive_failures == 0
    assert task.id in kb.detect_crashed_workers._last_rate_limited


def lost_wait_status_probe(board, tmp_path, monkeypatch):
    task = claim(board)
    path = kb.kanban_db_path().parent / "runs" / f"{task.id}.{task.current_run_id}.exit.json"
    # Execute a real child through the production spawn function, not a fake Popen.
    # The fallback path lets this reproduce on the pre-sidecar implementation too.
    script = (
        "import os,json,pathlib,sys,time; "
        f"p=pathlib.Path(os.environ.get('HERMES_KANBAN_EXIT_FILE', {str(path)!r})); "
        "p.parent.mkdir(parents=True,exist_ok=True); "
        "p.write_text(json.dumps({'exit_code':75,'failure_reason':'rate_limit','ts':time.time()})); "
        "sys.exit(75)"
    )
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, "-c", script])
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda *_: None)
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda *_: None)
    pid = kb._default_spawn(task, str(tmp_path))
    kb._set_worker_pid(board, task.id, pid)
    reaped = []
    thread = threading.Thread(target=lambda: reaped.append(os.waitpid(-1, 0)))
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert reaped == [(pid, 75 << 8)]
    kb.reap_worker_zombies()
    kb.detect_crashed_workers(board)
    assert task.id in kb.detect_crashed_workers._last_rate_limited
    assert kb.get_task(board, task.id).consecutive_failures == 0


@pytest.mark.macos_only
def test_sidecar_wins_waitpid_race_macos(board, tmp_path, monkeypatch):
    lost_wait_status_probe(board, tmp_path, monkeypatch)


@pytest.mark.linux_only
def test_sidecar_wins_waitpid_race_linux(board, tmp_path, monkeypatch):
    lost_wait_status_probe(board, tmp_path, monkeypatch)


def test_spawn_always_uses_result_aware_path(board, tmp_path, monkeypatch):
    task = claim(board)
    popen = Mock(return_value=Mock(pid=887766))
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda *_: None)
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda *_: None)
    kb._default_spawn(task, str(tmp_path))
    assert "-Q" in popen.call_args.args[0]
    assert popen.call_args.kwargs["env"]["HERMES_KANBAN_EXIT_FILE"].endswith(
        f"/runs/{task.id}.{task.current_run_id}.exit.json"
    )
    if hasattr(kb, "_worker_processes"):
        kb._worker_processes.pop(887766, None)


@pytest.mark.parametrize("expires", [1, None])
def test_dead_quota_worker_precedes_ttl_and_orphan_recovery(board, monkeypatch, expires):
    task = claim(board)
    receipt(task, 75)
    board.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (expires, task.id))
    board.commit()
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    result = kb.dispatch_once(board, spawn_fn=Mock(side_effect=AssertionError("must cool down")))
    assert result.rate_limited == [task.id]
    assert result.reclaimed == 0
    assert result.reconciled_orphans == []
    assert kb.get_task(board, task.id).consecutive_failures == 0


@pytest.mark.parametrize("payload", ["{", "[]", '{"exit_code":true}', '{"exit_code":999}'])
def test_bad_receipt_falls_back_to_exit_status(board, monkeypatch, payload):
    task = claim(board)
    receipt(task, 1).write_text(payload)
    kb._record_worker_exit(99999999, 75 << 8)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    assert task.id in kb.detect_crashed_workers._last_rate_limited


def test_previous_runs_receipt_cannot_mask_new_crash(board, monkeypatch):
    first = claim(board)
    receipt(first, 75)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    kb.detect_crashed_workers(board)
    second = kb.claim_task(board, first.id)
    assert second.current_run_id != first.current_run_id
    kb._set_worker_pid(board, first.id, 99999999)
    kb._record_worker_exit(99999999, 1 << 8)
    kb.detect_crashed_workers(board)
    assert kb.get_task(board, first.id).consecutive_failures == 1


def test_retained_child_poll_is_targeted(board, tmp_path, monkeypatch):
    unrelated = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(9)"], stdin=subprocess.DEVNULL)
    owned = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(75)"], stdin=subprocess.DEVNULL)
    try:
        owned.wait(timeout=10)
        kb._worker_processes[owned.pid] = owned
        assert kb.reap_worker_zombies() == [owned.pid]
        assert kb._classify_worker_exit(owned.pid) == ("rate_limited", 75)
        assert unrelated.wait(timeout=10) == 9
        assert owned.pid not in kb._worker_processes
    finally:
        unrelated.wait(timeout=10)
        owned.wait(timeout=10)


@pytest.mark.parametrize("configured,expected", [(0, 0), (17, 17), (-1, 300), ("bad", 300), (True, 300)])
def test_cooldown_config_is_authoritative(board, tmp_path, monkeypatch, configured, expected):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "999")
    (tmp_path / ".hermes" / "config.yaml").write_text(json.dumps({
        "kanban": {"rate_limit_cooldown_seconds": configured}}), encoding="utf-8")
    assert kb._resolve_rate_limit_cooldown_seconds() == expected


def test_receipt_uses_connected_board_not_ambient_pin(board, tmp_path, monkeypatch):
    from hermes_cli.kanban_worker_exit import exit_file
    other_db = tmp_path / "other-board" / "kanban.db"
    with kb.connect_closing(db_path=other_db) as other:
        task = claim(other)
        path = exit_file(other_db, task.id, task.current_run_id)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"exit_code": 75}), encoding="utf-8")
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        kb.detect_crashed_workers(other)
        assert kb.get_task(other, task.id).consecutive_failures == 0
        assert task.id in kb.detect_crashed_workers._last_rate_limited
