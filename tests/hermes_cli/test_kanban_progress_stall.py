"""A wrapper heartbeat cannot certify model or tool progress (t_7d034e3b).

The stall decision rides on the agent's own ``progress_at`` timestamp (stamped
onto heartbeat events by the worker bridge). The process probe is a veto only.

Probe determinism (t_5457397a): the reclaim/stall POLICY tests still run a REAL
worker process (so reclaim really terminates it) but feed the probe a
deterministic ``ps`` table through the ``kb._process_cpu_table`` seam. Waiting
for the real ``ps`` to read a fresh child as 0.0% is scheduler-dependent -- on
Linux procps ``pcpu`` is lifetime cputime/elapsed, so a child whose startup
burned 30 ms needs ~30 s before it rounds to 0.0, far longer on a loaded
runner -- and it ejected unrelated PRs from the merge queue. The table is still
parsed by the production ``_cpu_active_in_table``, so the children-never-veto
rule is exercised for real. The real probe keeps its own integration tests:
busy process -> active (fail-safe direction, load-immune) and in-flight process
-> idle (skips with the measured reason when the host cannot show idle).
"""
import json
import os
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


class _FakePs:
    """Deterministic ``ps`` table: every registered pid reads 0.0% CPU."""

    def __init__(self):
        self.rows = []  # (pid, ppid, pcpu)
        self.reads = 0

    def add(self, pid, ppid=None, pcpu=0.0):
        self.rows.append((int(pid), int(ppid if ppid is not None else os.getpid()), pcpu))

    def __call__(self):
        self.reads += 1
        return "".join(f"{p:>7} {pp:>7} {c:4.1f}\n" for p, pp, c in self.rows)


@pytest.fixture
def fake_ps(monkeypatch):
    fake = _FakePs()
    monkeypatch.setattr(kb, "_process_cpu_table", fake)
    return fake


def _wait_idle_real(pid, timeout=60.0):
    """Wait until the REAL probe reads idle on 6 consecutive samples.

    Only the real-probe integration test uses this. A miss caused by the host
    (``ps`` failing/timing out, or load above the core count) is a SKIP with
    the measured reason; a miss on an unloaded host with a working ``ps`` is a
    genuine FAIL (e.g. the probe regressed to always-active).
    """
    deadline, streak, probe_errors = time.monotonic() + timeout, 0, []
    while time.monotonic() < deadline:
        try:
            active = kb._cpu_active_in_table(kb._process_cpu_table(), pid)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            probe_errors.append(type(exc).__name__)
            active = True
        streak = 0 if active else streak + 1
        if streak >= 6:
            return
        time.sleep(0.25)
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    cores = os.cpu_count() or 1
    if probe_errors or load > cores:
        pytest.skip(
            f"host could not show pid {pid} idle within {timeout:.0f}s: "
            f"probe errors={sorted(set(probe_errors)) or 'none'} "
            f"({len(probe_errors)}x), loadavg1={load:.1f} on {cores} cores"
        )
    pytest.fail(
        f"pid {pid} never read idle to the real probe on an unloaded host "
        f"(loadavg1={load:.1f}, {cores} cores, no probe errors)"
    )


def _in_flight_worker(server, fake_ps=None):
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
    if fake_ps is None:
        _wait_idle_real(proc.pid)
    else:
        fake_ps.add(proc.pid)
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


def test_run_7914_shape_stalls_at_15_reclaims_at_25_escalates_after_two(board, monkeypatch, silent_server, fake_ps):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_ps)
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
        again = _in_flight_worker(silent_server, fake_ps)
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
    assert fake_ps.reads >= 5  # every stall decision consulted the probe veto


def test_real_probe_reads_in_flight_worker_as_idle(silent_server):
    """Integration proof for the REAL ``ps`` probe: a process blocked on an
    unanswered request reads idle, exactly like a dead socket. This is why
    the probe may only veto, never authorize, a reclaim."""
    proc = _in_flight_worker(silent_server)  # waits on the real probe
    try:
        assert proc.poll() is None
    finally:
        proc.kill()


def test_healthy_in_flight_wait_with_advancing_progress_is_never_reclaimed(board, monkeypatch, silent_server, fake_ps):
    """Argus repro: idle-but-in-flight process + advancing progress_at."""
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_ps)
    try:
        # The probe cannot tell this from a dead socket...
        assert kb._worker_cpu_active(proc.pid) is False
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


def _stalled_worker_with_persistent_child(server, fake_ps):
    """7914 shape + one persistent idle child (execute_code kernel / LSP server)."""
    code = (
        "import socket,subprocess,sys;"
        "d=subprocess.DEVNULL;subprocess.Popen([sys.executable,'-c','import time;time.sleep(600)'],"
        "stdin=d,stdout=d,stderr=d);"
        "s=socket.create_connection(('127.0.0.1',int(sys.argv[1])));"
        "s.sendall(b'POST /v1/messages HTTP/1.1\\r\\n\\r\\n');s.recv(1)"
    )
    port = server.getsockname()[1]
    proc = _reap_in_background(subprocess.Popen([sys.executable, "-c", code, str(port)]))
    server.settimeout(30)
    conn, _ = server.accept()
    conn.recv(64)
    _in_flight_worker.conns.append(conn)
    deadline, kids = time.monotonic() + 20, []
    while time.monotonic() < deadline and not kids:
        ps = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
        kids = [int(p) for p, pp in (l.split() for l in ps.splitlines()) if int(pp) == proc.pid]
        time.sleep(0.1)
    assert kids  # the persistent idle child really exists
    fake_ps.add(proc.pid)
    for kid in kids:  # the child is in the table, parented to the worker
        fake_ps.add(kid, ppid=proc.pid)
    return proc, kids


def test_persistent_idle_child_does_not_hide_a_stall(board, monkeypatch, silent_server, fake_ps):
    """Argus round 2: an any-child veto blinded the detector to every worker
    holding a kernel/LSP child. Stale progress + idle child -> reclaimed at T+25."""
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc, kids = _stalled_worker_with_persistent_child(silent_server, fake_ps)
    try:
        tid = _running(board, now, proc.pid, progress_at=now - 900)
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert _count(board, tid, "stalled") == 1  # stalled at T+15
        now += 600
        _heartbeat(board, tid, now - 1500)  # wrapper heartbeat fresh, progress frozen
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == [tid]
        assert kb.get_task(board, tid).status == "ready"  # reclaimed at T+25
        proc.wait(timeout=10)
    finally:
        proc.kill()
        for kid in kids:  # reparented once the worker dies; kill by pid
            subprocess.run(["kill", "-9", str(kid)], stderr=subprocess.DEVNULL)


def test_cpu_active_worker_vetoes_stale_progress(board, monkeypatch):
    """A worker burning CPU (real process) is never flagged on one stale window."""
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    busy = _reap_in_background(subprocess.Popen([sys.executable, "-c", "while True: pass"]))
    try:
        deadline = time.monotonic() + 20  # time.time is frozen by the patch above
        while time.monotonic() < deadline and not kb._worker_cpu_active(busy.pid):
            time.sleep(0.2)
        assert kb._worker_cpu_active(busy.pid) is True
        tid = _running(board, now, busy.pid, progress_at=now - 1600)
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert _count(board, tid, "stalled") == 0
        assert kb.get_task(board, tid).status == "running"
    finally:
        busy.kill()


@pytest.mark.parametrize("failure", [
    OSError("ps missing"),
    subprocess.CalledProcessError(1, ["ps"]),
    subprocess.TimeoutExpired(["ps"], 2),
])
def test_process_probe_failure_never_authorizes_reclaim(board, monkeypatch, silent_server, failure):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    # An UNINSTALLED table: skips the real-idle wait, but the probe itself stays
    # real so it runs into the broken ``ps`` below.
    proc = _in_flight_worker(silent_server, _FakePs())
    try:
        def broken(*_a, **_kw):
            raise failure
        monkeypatch.setattr(subprocess, "run", broken)
        assert kb._worker_cpu_active(proc.pid) is True
        tid = _running(board, now, proc.pid, progress_at=now - 1600)
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert kb.get_task(board, tid).status == "running"
        assert proc.poll() is None
    finally:
        proc.kill()


def test_run_without_progress_signal_is_unknown_and_never_reclaimed(board, monkeypatch, silent_server, fake_ps):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_ps)
    try:
        tid = _running(board, now, proc.pid, progress_at=None)
        rid = kb.get_task(board, tid).current_run_id
        assert kb.heartbeat_worker(board, tid, expected_run_id=rid)  # legacy: no progress_at
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert _count(board, tid, "stalled") == 0
    finally:
        proc.kill()


def test_dispatch_tick_observes_progress_despite_fresh_heartbeats(board, monkeypatch, silent_server, fake_ps):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_ps)
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
