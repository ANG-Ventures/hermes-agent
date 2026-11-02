"""A Kanban worker outlives the process that spawned it (t_4d4b30ff).

2026-11-02 07:08: a `hermes kanban dispatch` run from a terminal-tool call hit
the call's timeout, and the tool's teardown SIGKILLed the 47 setsid'd workers
the dispatch had spawned (35-card cohort death). These tests replay that: a
spawner started through the real LocalEnvironment spawns a worker the way
``_default_spawn`` does, then the tool call times out.
"""
import os
import shlex
import sys
import time

import pytest

from agent.deadline import kill_process_tree
from tools.environments.local import LocalEnvironment

# Real signal delivery is the point: the orphaned workers leave the test subtree by design
# (setsid + reparent), and every pid signalled here was spawned by the test itself.
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.live_system_guard_bypass]


def _spawner(tmp_path, *, worker_env: dict, grandchild: bool = False) -> str:
    """Python source of a spawner that starts a setsid'd 'worker' and then hangs."""
    worker = "import time; time.sleep(60)"
    if grandchild:
        worker = (
            "import subprocess,sys,time\n"
            "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            f"open({str(tmp_path / 'grandchild.pid')!r},'w').write(str(g.pid))\n"
            "time.sleep(60)\n"
        )
    return (
        "import os,subprocess,sys,time\n"
        f"env=dict(os.environ,**{worker_env!r})\n"
        f"p=subprocess.Popen([sys.executable,'-c',{worker!r}],start_new_session=True,"
        "env=env,stdin=subprocess.DEVNULL)\n"
        f"open({str(tmp_path / 'worker.pid')!r},'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )


def _wait_pid(path, timeout=10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} never written")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _cleanup(*pids):
    for pid in pids:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, TypeError):
            pass


DISPATCHED = {"HERMES_KANBAN_TASK": "t_replay", "HERMES_KANBAN_RUN_ID": "1",
              "HERMES_KANBAN_OWNER_PID": "pending"}


def _run_timed_out_call(tmp_path, worker_env, *, grandchild=False):
    env = LocalEnvironment(cwd=str(tmp_path), timeout=2)
    src = _spawner(tmp_path, worker_env=worker_env, grandchild=grandchild)
    result = env.execute(f"{shlex.quote(sys.executable)} -c {shlex.quote(src)}", timeout=2)
    assert result.get("returncode") == 124, result
    time.sleep(0.5)


def test_terminal_timeout_leaves_dispatched_worker_and_its_children_alive(tmp_path):
    _run_timed_out_call(tmp_path, DISPATCHED, grandchild=True)
    worker = _wait_pid(tmp_path / "worker.pid")
    grandchild = _wait_pid(tmp_path / "grandchild.pid")
    try:
        assert _alive(worker), "dispatched worker died with its spawner's tool call"
        assert _alive(grandchild), "the worker's own child died with the spawner"
    finally:
        _cleanup(worker, grandchild)


def test_terminal_timeout_still_sweeps_a_plain_setsid_escapee(tmp_path):
    # Not a dispatched worker (no unclaimed grant): the anti-orphan sweep still applies.
    _run_timed_out_call(tmp_path, {"HERMES_KANBAN_TASK": "t_replay",
                                   "HERMES_KANBAN_OWNER_PID": "12345"})
    worker = _wait_pid(tmp_path / "worker.pid")
    try:
        deadline = time.monotonic() + 5
        while _alive(worker) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(worker)
    finally:
        _cleanup(worker)


def test_kill_process_tree_spares_dispatched_worker(tmp_path):
    import subprocess
    src = _spawner(tmp_path, worker_env=DISPATCHED)
    spawner = subprocess.Popen([sys.executable, "-c", src], start_new_session=True)
    worker = _wait_pid(tmp_path / "worker.pid")
    try:
        kill_process_tree(spawner.pid)
        spawner.wait(timeout=5)
        time.sleep(0.3)
        assert _alive(worker)
    finally:
        _cleanup(spawner.pid, worker)
