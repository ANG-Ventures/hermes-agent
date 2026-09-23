"""A wrapper heartbeat cannot certify model or tool progress."""
import json
import os
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


def _running(board, now):
    tid = kb.create_task(board, title="waiting on capped pool", assignee="argus")
    task = kb.claim_task(board, tid)
    board.execute("UPDATE tasks SET worker_pid=54774, last_heartbeat_at=? WHERE id=?", (now, tid))
    board.execute("UPDATE task_runs SET started_at=? WHERE id=?", (now - 901, task.current_run_id))
    board.commit()
    return tid


def test_heartbeats_without_progress_stall_reclaim_and_escalate(board, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_worker_has_active_child", lambda pid: False)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **kw: {"terminated": True})
    tid = _running(board, now)
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
    events = board.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind='stalled'", (tid,)).fetchall()
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["progress_age_seconds"] >= 900
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
    assert len(board.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='stalled'", (tid,)).fetchall()) == 1
    now += 600
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == [tid]
    assert kb.get_task(board, tid).status == "ready"
    for _ in range(2):
        claimed = kb.claim_task(board, tid)
        board.execute("UPDATE task_runs SET started_at=? WHERE id=?", (now - 1500, claimed.current_run_id))
        board.execute("UPDATE tasks SET worker_pid=54774, last_heartbeat_at=? WHERE id=?", (now, tid))
        board.commit()
        kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500)
    assert kb.get_task(board, tid).status == "blocked"
    assert board.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? AND outcome='stalled'", (tid,)).fetchone()[0] == 3


def test_active_child_or_log_growth_prevents_stall(board, monkeypatch, tmp_path):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    tid = _running(board, now)
    monkeypatch.setattr(kb, "_worker_has_active_child", lambda pid: True)
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
    monkeypatch.setattr(kb, "_worker_has_active_child", lambda pid: False)
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"session_id: progress\n")
    os.utime(log, (now, now))
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='stalled'", (tid,)).fetchone()[0] == 0


def test_dispatch_tick_observes_progress_despite_fresh_heartbeats(board, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_worker_has_active_child", lambda pid: False)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    tid = _running(board, now)
    kb.dispatch_once(board, max_spawn=1, spawn_fn=lambda *args: 123)
    assert board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='stalled'", (tid,)
    ).fetchone()[0] == 1


def test_fresh_provider_dump_counts_only_for_current_session(board, monkeypatch, tmp_path):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_worker_has_active_child", lambda pid: False)
    tid = _running(board, now)
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    kb._stamp_worker_log_run_boundary(log)
    with log.open("ab") as stream:
        stream.write(b"session_id: current_session\n")
    os.utime(log, (now - 901, now - 901))
    sessions = tmp_path / ".hermes" / "profiles" / "argus" / "sessions"
    sessions.mkdir(parents=True)
    stale_other = sessions / "request_dump_other_session_0.json"
    stale_other.write_text("{}")
    os.utime(stale_other, (now, now))
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='stalled'", (tid,)).fetchone()[0] == 1
    recent = sessions / "request_dump_current_session_0.json"
    recent.write_text("{}")
    os.utime(recent, (now, now))
    now += 600
    # A fresh request on this session prevents reclaim, but cannot erase a
    # historical stall event; future ticks use the fresh dump timestamp.
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
