"""Terminating (or finding dead) a worker reaps its WHOLE session (t_ca0233d7).

Workers are spawned with ``start_new_session=True``, so worker pid == sid ==
pgid. Signalling only the worker pid left everything it had started in another
process group of that session alive with ppid 1 (54 headless Chromes from a
worker's pytest, Studio load1 250, 2026-09-29).

Each test runs a REAL fake worker: a session leader that starts a setpgrp'd
``/bin/sleep`` and then waits. Only processes the test created are ever
signalled.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = [
    pytest.mark.skipif(
        not hasattr(os, "getsid") or not hasattr(os, "killpg"), reason="POSIX sessions only",
    ),
    # A crashed worker's leftovers are reparented to init, i.e. outside the
    # test's process subtree by construction; they are still only the
    # processes this test started (session == the fake worker's sid).
    pytest.mark.live_system_guard_bypass,
]

_WORKER = (
    "import os, subprocess, sys, time\n"
    "p = subprocess.Popen(['/bin/sleep', '120'], preexec_fn=os.setpgrp)\n"
    "print(p.pid, flush=True)\n"
    "if sys.argv[1] == 'exit':\n"
    "    sys.stdin.readline()\n"  # crash only when the test says so
    "    os._exit(0)\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    leftovers: list[int] = []
    monkeypatch.setattr(sys.modules[__name__], "_LEFTOVERS", leftovers)
    with kb.connect() as c:
        yield c
    for pid in leftovers:  # never leak the sleep, even when the test fails
        try:
            os.kill(pid, 9)
        except OSError:
            pass


_LEFTOVERS: list = []


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kb._pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _worker_card(conn, mode: str, max_runtime=None):
    tid = kb.create_task(conn, title="card", assignee="worker",
                         max_runtime_seconds=max_runtime)
    assert kb.claim_task(conn, tid) is not None
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        start_new_session=True,
    )
    assert worker.stdout is not None
    sleep_pid = int(worker.stdout.readline())
    _LEFTOVERS.append(sleep_pid)
    threading.Thread(target=worker.wait, daemon=True).start()  # no zombie
    assert kb._set_worker_pid(conn, tid, worker.pid)
    # The worker heartbeats (while alive) after starting its child; event
    # times are whole seconds, so step past the second the sleep was born in.
    time.sleep(1.1)
    with kb.write_txn(conn):
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
        ).fetchone()[0]
        kb._append_event(conn, tid, "heartbeat", {}, run_id=run_id)
    assert os.getsid(sleep_pid) == worker.pid
    assert os.getpgid(sleep_pid) == sleep_pid != worker.pid
    return tid, worker, sleep_pid


def _crash(worker) -> None:
    assert worker.stdin is not None
    worker.stdin.write("go\n")
    worker.stdin.flush()


def test_max_runtime_reaps_session_leftovers(conn):
    tid, worker, sleep_pid = _worker_card(conn, "stay", max_runtime=1)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (int(time.time()) - 30, tid),
        )
    assert tid in kb.enforce_max_runtime(conn)
    assert _gone(worker.pid)
    assert _gone(sleep_pid), "setpgrp'd sleep outlived its worker's session"


def test_crashed_worker_session_leftovers_reaped(conn, monkeypatch):
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    tid, worker, sleep_pid = _worker_card(conn, "exit")
    assert kb._pid_alive(worker.pid)  # heartbeat above was sent while alive
    _crash(worker)
    assert _gone(worker.pid)
    assert kb._pid_alive(sleep_pid)
    kb.detect_crashed_workers(conn)
    assert _gone(sleep_pid), "setpgrp'd sleep outlived its crashed worker"


def test_recycled_session_is_not_reaped(conn, monkeypatch):
    """Prism P1: a dead worker's pid reused by a NEW session leader that exited
    and left children. Those children postdate the recorded run's last
    evidence, so they are not the worker's and must survive."""
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    tid, worker, sleep_pid = _worker_card(conn, "exit")
    with kb.write_txn(conn):  # the recorded run ended 2 h ago
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 7200 "
            "WHERE task_id = ? AND kind IN ('claimed', 'spawned', 'heartbeat')", (tid,),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = started_at - 7200 "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)", (tid,),
        )
    _crash(worker)
    assert _gone(worker.pid)
    kb.detect_crashed_workers(conn)
    time.sleep(0.5)
    assert kb._pid_alive(sleep_pid), "reaped a session that is not the recorded worker's"


_FORK_ON_TERM = (
    "import os, signal, subprocess, sys, time\n"
    "def h(*_):\n"
    "    p = subprocess.Popen(['/bin/sleep', '120'], preexec_fn=os.setpgrp)\n"
    "    print(p.pid, flush=True)\n"
    "    time.sleep(1)\n"  # still a member when the reaper rescans
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, h)\n"
    "os.setpgrp()\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


def test_group_born_during_reap_is_reaped():
    """Prism: a member that answers SIGTERM by forking into a NEW process
    group must not escape the reap."""
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "c = subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdout=sys.stdout)\n"
         "time.sleep(120)\n",
         _FORK_ON_TERM],
        stdout=subprocess.PIPE, text=True, encoding="utf-8", start_new_session=True,
    )
    assert leader.stdout is not None
    assert leader.stdout.readline().strip() == "ready"
    born_after = time.time() - 5
    leader.kill()
    leader.wait()
    reaped = kb._reap_worker_session(leader.pid, born_after=born_after,
                                     born_before=time.time())
    escaped = int(leader.stdout.readline())
    try:
        assert reaped >= 2  # the forker's group and the one it created
        assert _gone(escaped), "group created on SIGTERM escaped the reap"
    finally:
        try:
            os.kill(escaped, 9)
        except OSError:
            pass


def test_reap_stops_when_session_continuity_breaks(monkeypatch):
    """Prism: ownership must hold for every rescan, not just the first. A
    rescan sharing no (pid, create_time) with the previous one is a reused
    sid; nothing in it may be signalled."""
    signalled: list = []
    scans = iter([[(111, 111)], [(222, 222)]])
    monkeypatch.setattr(kb, "_worker_session_members", lambda sid: next(scans, []))
    births = {111: 100.0, 222: 10_000.0}
    monkeypatch.setattr(kb, "_member_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(kb.os, "killpg", lambda pg, sig: signalled.append(pg))
    assert kb._reap_worker_session(99_999, born_after=50.0, born_before=200.0,
                                   grace=0.5) == 1
    assert signalled == [111], "signalled a group from a non-continuous rescan"


def test_reap_refuses_init_and_own_session():
    window = {"born_after": 0.0, "born_before": time.time()}
    assert kb._reap_worker_session(0, **window) == 0
    assert kb._reap_worker_session(1, **window) == 0
    assert kb._reap_worker_session(os.getsid(0), **window) == 0
    assert kb._reap_worker_session(os.getpid(), **window) == 0


# --- run-identity sweep: children that setsid() OUT of the worker session -------------

def _spawn_escapee(task_id: str, run_id: str) -> subprocess.Popen:
    """A child in its OWN session (as browser_harness.daemon does) carrying the run identity."""
    env = {**os.environ, "HERMES_KANBAN_TASK": task_id, "HERMES_KANBAN_RUN_ID": run_id}
    p = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(120)"],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env, start_new_session=True)
    assert p.stdout is not None and p.stdout.readline().strip() == "ready"
    return p


def test_env_identity_reap_kills_a_daemon_that_left_the_session():
    """The browser-use harness daemon setsid()s, so the session reap never sees it;
    its environment still names the run. Born inside the window => reaped."""
    born_after = time.time() - 5
    victim = _spawn_escapee("t_envreap1", "4242")
    bystander = _spawn_escapee("t_envreap1", "4243")   # same task, a NEWER run: not ours
    try:
        # the session reap of the WORKER (a fake sid here: the victim is in its own session) sees nothing
        assert kb._reap_worker_session(99_999_9, born_after=born_after, born_before=time.time()) == 0
        assert not _gone(victim.pid)
        n = kb._reap_run_env_escapees("t_envreap1", 4242, born_after=born_after,
                                      born_before=time.time(), grace=2.0)
        assert n == 1
        assert _gone(victim.pid), "run-identified escapee survived the reap"
        assert not _gone(bystander.pid), "a different run's process was signalled"
    finally:
        for p in (victim, bystander):
            try:
                p.kill(); p.wait(timeout=5)
            except Exception:
                pass


def test_env_identity_reap_respects_birth_window_and_unknown_bounds():
    victim = _spawn_escapee("t_envreap2", "7")
    try:
        # born AFTER the window closed -> a recycled/unrelated process; untouched
        assert kb._reap_run_env_escapees("t_envreap2", 7, born_after=1.0, born_before=2.0) == 0
        assert not _gone(victim.pid)
        # unknown bounds -> no reap, ever
        assert kb._reap_run_env_escapees("t_envreap2", 7, born_after=None, born_before=time.time()) == 0
        assert kb._reap_run_env_escapees("t_envreap2", 7, born_after=0.0, born_before=None) == 0
        assert kb._reap_run_env_escapees(None, 7, born_after=0.0, born_before=time.time()) == 0
        assert not _gone(victim.pid)
    finally:
        victim.kill(); victim.wait(timeout=5)


def test_env_identity_scan_never_lists_self_or_own_session(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_envreap3")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "99")
    # this very process carries the identity and must be invisible to the sweep
    assert all(pid != os.getpid() for pid, _ in kb._run_env_escapees("t_envreap3", 99))
    assert kb._reap_run_env_escapees("t_envreap3", 99, born_after=0.0, born_before=time.time() + 10) == 0
