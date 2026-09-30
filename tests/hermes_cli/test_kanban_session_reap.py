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
        stdout=subprocess.PIPE, text=True, encoding="utf-8", start_new_session=True,
    )
    assert worker.stdout is not None
    sleep_pid = int(worker.stdout.readline())
    _LEFTOVERS.append(sleep_pid)
    threading.Thread(target=worker.wait, daemon=True).start()  # no zombie
    assert kb._set_worker_pid(conn, tid, worker.pid)
    assert os.getsid(sleep_pid) == worker.pid
    assert os.getpgid(sleep_pid) == sleep_pid != worker.pid
    return tid, worker, sleep_pid


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
    assert _gone(worker.pid)
    assert kb._pid_alive(sleep_pid)
    kb.detect_crashed_workers(conn)
    assert _gone(sleep_pid), "setpgrp'd sleep outlived its crashed worker"


def test_reap_refuses_init_and_own_session():
    assert kb._reap_worker_session(0) == 0
    assert kb._reap_worker_session(1) == 0
    assert kb._reap_worker_session(os.getsid(0)) == 0
    assert kb._reap_worker_session(os.getpid()) == 0
