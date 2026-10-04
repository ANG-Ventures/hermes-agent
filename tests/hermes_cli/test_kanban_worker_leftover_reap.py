"""Servers a worker leaves behind are reaped on worker exit and by the
terminal-workspace orphan sweep (t_446b6b99).

2026-10-01: four listeners (two caddy, a preview server, a bridge) with ppid 1
and cwd in a DONE card's workspace were still running 3 h 42 m to 2 d 7 h
after their cards closed. The crash-path session reap only looks at
``running`` cards, so a worker that completes its card and exits left every
server it had started.

Every process signalled here is one the test started.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

pytestmark = [
    pytest.mark.skipif(
        not hasattr(os, "getsid") or not hasattr(os, "killpg"), reason="POSIX sessions only",
    ),
    # Leftovers are reparented to init by construction; they are still only
    # processes this test started.
    pytest.mark.live_system_guard_bypass,
]

# Fake worker: starts a TCP listener in its own process group (as a
# `caddy run &` from a shell tool would be), prints its pid and port, then
# exits once the test says the card is complete.
_WORKER = (
    "import os, subprocess, sys\n"
    "srv = ('import socket,sys,time\\n'\n"
    "       's=socket.socket(); s.bind((\"127.0.0.1\",0)); s.listen()\\n'\n"
    "       'print(s.getsockname()[1], flush=True)\\n'\n"
    "       'time.sleep(300)\\n')\n"
    "p = subprocess.Popen([sys.executable, '-c', srv], stdout=subprocess.PIPE,\n"
    "                     text=True, preexec_fn=os.setpgrp)\n"
    "port = p.stdout.readline().strip()\n"
    "print(p.pid, port, flush=True)\n"
    "sys.stdin.readline()\n"
    "os._exit(0)\n"
)

# Detached orphan: double-forks so the grandchild is reparented to init,
# with cwd inside the given workspace dir and stdio detached.
_ORPHAN = (
    "import os, sys, time\n"
    "r, w = os.pipe()\n"
    "if os.fork():  # windows-footgun: ok (POSIX-only test, module skipif)\n"
    "    os.close(w); print(os.read(r, 32).decode().strip(), flush=True); os._exit(0)\n"
    "os.setsid()  # windows-footgun: ok (POSIX-only test)\n"
    "if os.fork():  # windows-footgun: ok (POSIX-only test, module skipif)\n"
    "    os._exit(0)\n"
    "os.chdir(sys.argv[1])\n"
    "n = os.open(os.devnull, os.O_RDWR)\n"
    "os.dup2(n, 0); os.dup2(n, 1); os.dup2(n, 2)\n"
    "os.write(w, (str(os.getpid()) + '\\n').encode())\n"
    "time.sleep(300)\n"
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c
    for pid in _STARTED:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    _STARTED.clear()


_STARTED: list[int] = []


def _gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kb._pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def test_completed_worker_listener_is_reaped_on_exit(conn):
    tid = kb.create_task(conn, title="card", assignee="worker")
    task = kb.claim_task(conn, tid)
    assert task is not None
    spawned_at = time.time()
    # Spawned exactly as _default_spawn does: own session, retained handle,
    # identity registered.
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True, encoding="utf-8", start_new_session=True,
    )
    with kb._worker_processes_lock:
        kb._worker_processes[worker.pid] = worker
    kb._register_worker_identity(worker.pid, tid, task.current_run_id, spawned_at)
    assert kbd._set_worker_pid(conn, tid, worker.pid)
    srv_pid, port = (int(x) for x in worker.stdout.readline().split())
    _STARTED.append(srv_pid)
    assert _listening(port)

    assert kb.complete_task(conn, tid, summary="done", metadata={"tests_run": 1},
                            expected_run_id=task.current_run_id)
    worker.stdin.write("go\n")
    worker.stdin.flush()
    deadline = time.monotonic() + 10
    exited: list[int] = []
    while time.monotonic() < deadline and worker.pid not in exited:
        exited += kbd.reap_worker_zombies()
        time.sleep(0.05)
    assert worker.pid in exited
    assert kb._pid_alive(srv_pid), "listener died with its worker; test proves nothing"

    assert kb.reap_exited_worker_leftovers(conn, exited, grace=2.0) == [tid]
    assert _gone(srv_pid), "listener outlived its completed worker"
    assert not _listening(port)
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
    assert "worker_leftovers_reaped" in kinds
    # identity is consumed: a second pass is a no-op
    assert kb.reap_exited_worker_leftovers(conn, exited) == []


def test_leftovers_reaped_when_exit_receipt_was_consumed_elsewhere(conn):
    """The gateway loop polls ``reap_worker_zombies`` every tick and only
    LOGS the pids; ``dispatch_once`` then calls this reaper with ``[]``. The
    worker's registered identity must still drive the sweep (2026-10-03:
    t_630c9711's setsid'd ``upsmon`` + ``wall`` beeped the Studio for 6 h)."""
    tid = kb.create_task(conn, title="card", assignee="worker")
    task = kb.claim_task(conn, tid)
    assert task is not None
    spawned_at = time.time()
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True, encoding="utf-8", start_new_session=True,
    )
    with kb._worker_processes_lock:
        kb._worker_processes[worker.pid] = worker
    kb._register_worker_identity(worker.pid, tid, task.current_run_id, spawned_at)
    assert kbd._set_worker_pid(conn, tid, worker.pid)
    srv_pid, port = (int(x) for x in worker.stdout.readline().split())
    _STARTED.append(srv_pid)
    assert _listening(port)

    assert kb.complete_task(conn, tid, summary="done", metadata={"tests_run": 1},
                            expected_run_id=task.current_run_id)
    worker.stdin.write("go\n")
    worker.stdin.flush()
    # The OTHER consumer (gateway tick) takes the poll() receipt and drops it.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and worker.pid not in kbd.reap_worker_zombies():
        time.sleep(0.05)
    with kb._worker_processes_lock:
        assert worker.pid not in kb._worker_processes
        assert worker.pid in kb._worker_identities
    assert kb._pid_alive(srv_pid), "listener died with its worker; test proves nothing"

    # dispatch_once's view: nothing exited this tick.
    assert kb.reap_exited_worker_leftovers(conn, [], grace=2.0) == [tid]
    assert _gone(srv_pid), "listener outlived its worker because another poller consumed the exit"
    assert not _listening(port)
    with kb._worker_processes_lock:
        assert worker.pid not in kb._worker_identities
    assert kb.reap_exited_worker_leftovers(conn, []) == []


def test_live_worker_identity_is_not_treated_as_exited(conn):
    """A registered worker whose handle is still retained has NOT exited;
    the identity walk must leave it (and its children) alone."""
    tid = kb.create_task(conn, title="card", assignee="worker")
    task = kb.claim_task(conn, tid)
    assert task is not None
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True, encoding="utf-8", start_new_session=True,
    )
    _STARTED.append(worker.pid)
    with kb._worker_processes_lock:
        kb._worker_processes[worker.pid] = worker
    kb._register_worker_identity(worker.pid, tid, task.current_run_id, time.time())
    srv_pid, port = (int(x) for x in worker.stdout.readline().split())
    _STARTED.append(srv_pid)
    try:
        assert kb.reap_exited_worker_leftovers(conn, [], grace=0.5) == []
        assert kb._pid_alive(worker.pid) and kb._pid_alive(srv_pid)
        assert _listening(port)
        with kb._worker_processes_lock:
            assert worker.pid in kb._worker_identities
    finally:
        worker.kill()
        worker.wait(5)
        with kb._worker_processes_lock:
            kb._worker_processes.pop(worker.pid, None)
            kb._worker_identities.pop(worker.pid, None)


def _spawn_orphan(cwd: Path) -> int:
    out = subprocess.run([sys.executable, "-c", _ORPHAN, str(cwd)],
                         capture_output=True, text=True, encoding="utf-8",
                         timeout=30, check=True).stdout
    pid = int(out.strip())
    _STARTED.append(pid)
    deadline = time.monotonic() + 5
    proc = kb.psutil.Process(pid)
    while time.monotonic() < deadline and not kb._init_parented(proc, proc.ppid()):
        time.sleep(0.05)
    assert kb._init_parented(proc, proc.ppid())
    return pid


def test_orphan_sweep_reaps_terminal_card_only(conn, tmp_path):
    root = tmp_path / "ws"
    done_id = kb.create_task(conn, title="done card", assignee="worker")
    run_id = kb.create_task(conn, title="running card", assignee="worker")
    assert kb.claim_task(conn, done_id) is not None
    assert kb.complete_task(conn, done_id, summary="done")
    assert kb.claim_task(conn, run_id) is not None
    (root / done_id / "sub").mkdir(parents=True)
    (root / run_id).mkdir(parents=True)

    done_orphan = _spawn_orphan(root / done_id / "sub")
    live_orphan = _spawn_orphan(root / run_id)

    # Inside the post-terminal grace window nothing is touched.
    assert kb.sweep_terminal_workspace_orphans(conn, root=root, notify=False) == {}
    assert kb._pid_alive(done_orphan)

    reaped = kb.sweep_terminal_workspace_orphans(
        conn, root=root, grace=2.0, min_terminal_age=0, notify=False,
    )
    assert list(reaped) == [done_id]
    assert [i["pid"] for i in reaped[done_id]] == [done_orphan]
    assert _gone(done_orphan), "orphan in a DONE card's workspace survived the sweep"
    assert kb._pid_alive(live_orphan), "orphan of a RUNNING card was reaped"
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ?", (done_id,))]
    assert "orphans_reaped" in kinds
    # Nothing left to reap: the sweep is silent.
    assert kb.sweep_terminal_workspace_orphans(
        conn, root=root, min_terminal_age=0, notify=False) == {}


def test_orphan_sweep_sends_one_logs_line_only_when_it_reaped(conn, tmp_path, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(kb, "_notify_orphan_sweep", lambda board, reaped: sent.append(reaped))
    root = tmp_path / "ws"
    tid = kb.create_task(conn, title="done card", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert kb.complete_task(conn, tid, summary="done", metadata={"tests_run": 1},
                            expected_run_id=claimed.current_run_id)
    (root / tid).mkdir(parents=True)
    assert kb.sweep_terminal_workspace_orphans(conn, root=root, min_terminal_age=0) == {}
    assert sent == []
    a = _spawn_orphan(root / tid)
    b = _spawn_orphan(root / tid)
    kb.sweep_terminal_workspace_orphans(conn, root=root, grace=2.0, min_terminal_age=0)
    assert len(sent) == 1 and sorted(i["pid"] for i in sent[0][tid]) == sorted([a, b])
    assert _gone(a) and _gone(b)


def test_dispatch_tick_runs_both_reaps(conn, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [4242])
    monkeypatch.setattr(kb, "reap_exited_worker_leftovers",
                        lambda c, pids: calls.setdefault("exit", list(pids)) and ["t_x"])
    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans",
                        lambda c, board=None: calls.setdefault("sweep", board) or {})
    res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default")
    assert calls["exit"] == [4242]
    assert calls["sweep"] == "default"
    assert res.worker_leftovers_reaped == ["t_x"]
    calls.clear()
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)
    assert calls == {}, "a dry run must not signal anything"
