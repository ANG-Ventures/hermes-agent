"""A wrapper heartbeat cannot certify model or tool progress (t_7d034e3b).

The stall decision rides on the agent's own ``progress_at`` timestamp (stamped
onto heartbeat events by the worker bridge). The process probe is a veto only.
Every process-shaped test here drives the REAL probe against a REAL process.
"""
import json
import socket
import subprocess
import sys
import threading
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


@pytest.fixture
def silent_server():
    """A local 'provider' that accepts connections and never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv
    srv.close()


def _reap_in_background(proc):
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _wait_idle(pid, timeout=30.0):
    """Wait until the REAL probe reads idle on 6 consecutive samples."""
    deadline, streak = time.time() + timeout, 0
    while time.time() < deadline:
        streak = 0 if kb._worker_has_active_child(pid) else streak + 1
        if streak >= 6:
            return
        time.sleep(0.25)
    pytest.fail(f"pid {pid} never read idle to the real probe")


def _in_flight_worker(server):
    """Real process blocked on an in-flight request: 0% CPU, no children."""
    code = (
        "import socket,sys;s=socket.create_connection(('127.0.0.1',int(sys.argv[1])));"
        "s.sendall(b'POST /v1/messages HTTP/1.1\\r\\n\\r\\n');s.recv(1)"
    )
    port = server.getsockname()[1]
    proc = _reap_in_background(subprocess.Popen([sys.executable, "-c", code, str(port)]))
    server.settimeout(30)
    conn, _ = server.accept()  # the request is now in flight and never answered
    conn.recv(64)
    _in_flight_worker.conns.append(conn)
    _wait_idle(proc.pid)
    return proc


_in_flight_worker.conns = []


def _running(board, now, pid, *, progress_at, started_ago=1800):
    tid = kb.create_task(board, title="waiting on capped pool", assignee="argus")
    task = kb.claim_task(board, tid)
    board.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    board.execute("UPDATE task_runs SET started_at=? WHERE id=?", (now - started_ago, task.current_run_id))
    board.commit()
    if progress_at is not None:
        assert kb.heartbeat_worker(board, tid, expected_run_id=task.current_run_id, progress_at=progress_at)
    return tid


def _heartbeat(board, tid, progress_at):
    rid = kb.get_task(board, tid).current_run_id
    assert kb.heartbeat_worker(board, tid, expected_run_id=rid, progress_at=progress_at)


def _count(board, tid, kind):
    return board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?", (tid, kind)
    ).fetchone()[0]


def test_run_7914_shape_stalls_at_15_reclaims_at_25_escalates_after_two(board, monkeypatch, silent_server):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server)
    # Last real progress = the API call start 15 min ago; the wrapper keeps
    # heartbeating (fresh last_heartbeat_at) with that same progress_at.
    tid = _running(board, now, proc.pid, progress_at=now - 900)
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
    stalled = board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='stalled'", (tid,)
    ).fetchall()
    assert len(stalled) == 1
    evidence = json.loads(stalled[0]["payload"])
    assert evidence["agent_progress_age_seconds"] >= 900
    assert "no_agent_progress" in evidence["signals"]
    now += 300
    _heartbeat(board, tid, now - 1200)  # heartbeat fresh, progress unchanged
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
    assert _count(board, tid, "stalled") == 1  # paged once
    now += 300
    _heartbeat(board, tid, now - 1500)
    assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == [tid]
    assert kb.get_task(board, tid).status == "ready"
    proc.wait(timeout=10)  # the real worker was terminated
    for _ in range(2):
        again = _in_flight_worker(silent_server)
        claimed = kb.claim_task(board, tid)
        board.execute("UPDATE task_runs SET started_at=? WHERE id=?", (now - 1500, claimed.current_run_id))
        board.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (again.pid, tid))
        board.commit()
        _heartbeat(board, tid, now - 1500)
        kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500)
        again.wait(timeout=10)
    assert kb.get_task(board, tid).status == "blocked"
    assert board.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND outcome='stalled'", (tid,)
    ).fetchone()[0] == 3


def test_healthy_in_flight_wait_with_advancing_progress_is_never_reclaimed(board, monkeypatch, silent_server):
    """Argus repro: real idle-but-in-flight process + advancing progress_at."""
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server)
    try:
        # The real probe cannot tell this from a dead socket...
        assert kb._worker_has_active_child(proc.pid) is False
        # ...but the agent reports progress (turns completing) each window.
        tid = _running(board, now, proc.pid, progress_at=now - 120, started_ago=3000)
        for _ in range(30):  # 30 minute-ticks across a 50+ minute run
            assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
            now += 60
            _heartbeat(board, tid, now - 120)
        assert kb.get_task(board, tid).status == "running"
        assert _count(board, tid, "stalled") == 0
        assert proc.poll() is None
    finally:
        proc.kill()


def test_live_child_vetoes_stale_progress(board, monkeypatch):
    """Healthy long tool call: idle parent, live child process."""
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);time.sleep(120)"
    parent = _reap_in_background(subprocess.Popen([sys.executable, "-c", code]))
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            ps = subprocess.run(["ps", "-A", "-o", "ppid="], capture_output=True, text=True).stdout
            if str(parent.pid) in ps.split():
                break
            time.sleep(0.1)
        time.sleep(2)  # let parent CPU decay: only the child must hold the veto
        assert kb._worker_has_active_child(parent.pid) is True
        tid = _running(board, now, parent.pid, progress_at=now - 1600)
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert _count(board, tid, "stalled") == 0
        assert kb.get_task(board, tid).status == "running"
    finally:
        subprocess.run(["pkill", "-P", str(parent.pid)])
        parent.kill()


@pytest.mark.parametrize("failure", [
    OSError("ps missing"),
    subprocess.CalledProcessError(1, ["ps"]),
    subprocess.TimeoutExpired(["ps"], 2),
])
def test_process_probe_failure_never_authorizes_reclaim(board, monkeypatch, silent_server, failure):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server)
    try:
        def broken(*_a, **_kw):
            raise failure
        monkeypatch.setattr(subprocess, "run", broken)
        assert kb._worker_has_active_child(proc.pid) is True
        tid = _running(board, now, proc.pid, progress_at=now - 1600)
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert kb.get_task(board, tid).status == "running"
        assert proc.poll() is None
    finally:
        proc.kill()


def test_run_without_progress_signal_is_unknown_and_never_reclaimed(board, monkeypatch, silent_server):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server)
    try:
        tid = _running(board, now, proc.pid, progress_at=None)
        rid = kb.get_task(board, tid).current_run_id
        assert kb.heartbeat_worker(board, tid, expected_run_id=rid)  # legacy: no progress_at
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert _count(board, tid, "stalled") == 0
    finally:
        proc.kill()


def test_dispatch_tick_observes_progress_despite_fresh_heartbeats(board, monkeypatch, silent_server):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server)
    try:
        tid = _running(board, now, proc.pid, progress_at=now - 901)
        kb.dispatch_once(board, max_spawn=1, spawn_fn=lambda *args: 123)
        assert _count(board, tid, "stalled") == 1
    finally:
        proc.kill()


def test_worker_bridge_stamps_progress_and_wait_tickers_do_not(board, monkeypatch):
    """E2E through the real AIAgent._touch_activity -> heartbeat bridge."""
    import run_agent
    from tools import kanban_tools

    tid = kb.create_task(board, title="bridge", assignee="daedalus")
    task = kb.claim_task(board, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setattr(kanban_tools, "inject_new_comments_from_env", lambda agent: None)
    agent = object.__new__(run_agent.AIAgent)
    agent._last_progress_ts = 1000.0

    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)
    agent._emit_wait_notice("⏳ waiting on model — 60s with no response yet")
    assert agent._last_progress_ts == 1000.0
    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)
    agent._touch_activity("waiting for non-streaming API response", progress=False)
    assert agent._last_progress_ts == 1000.0
    assert kb._run_progress_at(board, tid, task.current_run_id) == 1000

    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)
    agent._touch_activity("tool completed: terminal (1.0s)")
    assert agent._last_progress_ts > 1000.0
    assert kb._run_progress_at(board, tid, task.current_run_id) == int(agent._last_progress_ts)
