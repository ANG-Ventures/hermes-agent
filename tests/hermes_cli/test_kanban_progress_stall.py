"""A wrapper heartbeat cannot certify model or tool progress (t_7d034e3b).

The stall decision rides on the agent's own ``progress_at`` timestamp (stamped
onto heartbeat events by the worker bridge). The process probe is a veto only.

Probe determinism (t_5457397a, t_46a2f8a1): the reclaim/stall POLICY tests
still run a REAL worker process (so reclaim really terminates it) but inject a
deterministic CPU-seconds sampler through the ``kb._process_cpu_seconds`` seam;
the production delta logic in ``_worker_cpu_active`` still decides. The real
probe is a delta of psutil ``cpu_times()`` over a short window, NOT procps
``pcpu`` (lifetime cputime/elapsed on Linux, which read a once-busy stalled
worker as busy for minutes and a fresh idle child as busy for ~1 s per 1 ms of
startup CPU). Real-probe integration tests rotate the fault: fresh idle child,
once-busy-now-blocked child, genuinely busy child.
"""
import json
import socket
import subprocess
import sys
import threading
import time

import psutil
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


class _FakeCpu:
    """Deterministic CPU-seconds sampler: registered pids are idle (constant)
    or busy (advance on every read); anything else is NoSuchProcess."""

    def __init__(self):
        self.pids = {}  # pid -> [cpu_seconds, busy]
        self.reads = 0

    def add(self, pid, busy=False):
        self.pids[int(pid)] = [1.0, busy]

    def __call__(self, pid):
        self.reads += 1
        entry = self.pids.get(int(pid))
        if entry is None:
            raise psutil.NoSuchProcess(int(pid))
        if entry[1]:
            entry[0] += 0.01
        return entry[0]


@pytest.fixture
def fake_cpu(monkeypatch):
    fake = _FakeCpu()
    monkeypatch.setattr(kb, "_process_cpu_seconds", fake)
    monkeypatch.setattr(kb, "_CPU_SAMPLE_SECONDS", 0.0)
    return fake


def _in_flight_worker(server, fake_cpu=None):
    """Real process blocked on an in-flight request: no CPU now, no children."""
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
    if fake_cpu is not None:
        fake_cpu.add(proc.pid)
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


def test_run_7914_shape_stalls_at_15_reclaims_at_25_escalates_after_two(board, monkeypatch, silent_server, fake_cpu):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_cpu)
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
        again = _in_flight_worker(silent_server, fake_cpu)
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
    assert fake_cpu.reads >= 10  # every stall decision consulted the probe veto (2 samples each)


def _real_reads(pid, n=4):
    return [kb._worker_cpu_active(pid) for _ in range(n)]


def test_real_probe_reads_fresh_in_flight_worker_as_idle_at_once(silent_server):
    """Fault 1, fresh idle child: a just-started process blocked on an
    unanswered request reads idle immediately -- no waiting out its startup
    CPU, which lifetime ``pcpu`` needed ~30 s (far more on a loaded runner)
    to forget. Idle like a dead socket, which is why the probe may only veto."""
    proc = _in_flight_worker(silent_server)
    try:
        reads = _real_reads(proc.pid)
        assert reads[1:] == [False, False, False], reads  # 1st window may catch the send
        assert proc.poll() is None
    finally:
        proc.kill()


def test_real_probe_reads_once_busy_now_blocked_worker_as_idle(silent_server):
    """Fault 2, once-busy-now-blocked (the production bug): a worker that
    burned ~1 s of CPU and then stalled reads idle at once. Lifetime ``pcpu``
    read this busy for ~1000 s, so it vetoed its own reclaim."""
    code = (
        "import socket,sys,time;t=time.process_time()\n"
        "while time.process_time()-t<1.0: pass\n"
        "s=socket.create_connection(('127.0.0.1',int(sys.argv[1])))\n"
        "s.sendall(b'POST /v1/messages HTTP/1.1\\r\\n\\r\\n');s.recv(1)"
    )
    port = silent_server.getsockname()[1]
    proc = _reap_in_background(subprocess.Popen([sys.executable, "-c", code, str(port)]))
    try:
        silent_server.settimeout(90)
        conn, _ = silent_server.accept()  # the burn is over; the child is blocking
        conn.recv(64)
        _in_flight_worker.conns.append(conn)
        assert kb._process_cpu_seconds(proc.pid) >= 0.9  # real CPU history
        reads = _real_reads(proc.pid)
        assert reads[1:] == [False, False, False], reads
    finally:
        proc.kill()


def test_real_probe_exited_pid_reads_idle():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    assert kb._worker_cpu_active(proc.pid) is False  # NoSuchProcess: nothing to veto


def test_healthy_in_flight_wait_with_advancing_progress_is_never_reclaimed(board, monkeypatch, silent_server, fake_cpu):
    """Argus repro: idle-but-in-flight process + advancing progress_at."""
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_cpu)
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


def _stalled_worker_with_persistent_child(server, fake_cpu):
    """7914 shape + one persistent child (execute_code kernel / LSP server)."""
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
    fake_cpu.add(proc.pid)
    for kid in kids:  # even a BUSY child never vetoes: only the worker pid is sampled
        fake_cpu.add(kid, busy=True)
    return proc, kids


def test_persistent_idle_child_does_not_hide_a_stall(board, monkeypatch, silent_server, fake_cpu):
    """Argus round 2: an any-child veto blinded the detector to every worker
    holding a kernel/LSP child. Stale progress + idle child -> reclaimed at T+25."""
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc, kids = _stalled_worker_with_persistent_child(silent_server, fake_cpu)
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
    """Fault 3, genuinely busy: a worker burning CPU (real process, real probe)
    is never flagged on one stale window."""
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
    psutil.AccessDenied(1),
    OSError("proc unreadable"),
    RuntimeError("probe broke"),
    "psutil-missing",
])
def test_process_probe_failure_never_authorizes_reclaim(board, monkeypatch, silent_server, failure):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(kb, "_CPU_SAMPLE_SECONDS", 0.0)
    proc = _in_flight_worker(silent_server)  # the probe itself stays real
    try:
        if failure == "psutil-missing":
            monkeypatch.setitem(sys.modules, "psutil", None)
        else:
            def broken(*_a, **_kw):
                raise failure
            monkeypatch.setattr(psutil.Process, "cpu_times", broken)
        assert kb._worker_cpu_active(proc.pid) is True
        tid = _running(board, now, proc.pid, progress_at=now - 1600)
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert kb.get_task(board, tid).status == "running"
        assert proc.poll() is None
    finally:
        proc.kill()


def test_injected_sampler_drives_the_delta(fake_cpu):
    fake_cpu.add(101)
    fake_cpu.add(102, busy=True)
    assert kb._worker_cpu_active(101) is False
    assert kb._worker_cpu_active(102) is True
    assert kb._worker_cpu_active(103) is False  # NoSuchProcess -> idle


def test_run_without_progress_signal_is_unknown_and_never_reclaimed(board, monkeypatch, silent_server, fake_cpu):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_cpu)
    try:
        tid = _running(board, now, proc.pid, progress_at=None)
        rid = kb.get_task(board, tid).current_run_id
        assert kb.heartbeat_worker(board, tid, expected_run_id=rid)  # legacy: no progress_at
        assert kb.detect_progress_stalls(board, stall_seconds=900, reclaim_seconds=1500) == []
        assert _count(board, tid, "stalled") == 0
    finally:
        proc.kill()


def test_dispatch_tick_observes_progress_despite_fresh_heartbeats(board, monkeypatch, silent_server, fake_cpu):
    now = int(time.time())
    monkeypatch.setattr(kb.time, "time", lambda: now)
    proc = _in_flight_worker(silent_server, fake_cpu)
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
