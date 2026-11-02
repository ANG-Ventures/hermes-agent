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


def test_terminal_timeout_spares_worker_after_setproctitle_clobbers_its_env(tmp_path):
    """Prism c3557f4f9c71: a booted worker runs the real ``_set_process_title()``; with
    ``setproctitle`` that blanks the exec-time env psutil reads, so only the title is left."""
    spt = pytest.importorskip("setproctitle")
    import pathlib
    import psutil

    repo = str(pathlib.Path(__file__).resolve().parents[2])
    spt_dir = str(pathlib.Path(spt.__file__).resolve().parent.parent)
    worker = (
        "import sys,time\n"
        f"sys.path[:0]=[{repo!r},{spt_dir!r}]\n"
        "from hermes_cli.main import _set_process_title\n"
        "_set_process_title()\n"
        f"open({str(tmp_path / 'titled')!r},'w').write('1')\n"
        "time.sleep(60)\n"
    )
    src = (
        "import os,subprocess,sys,time\n"
        f"env=dict(os.environ,**{BOOTED!r})\n"
        f"p=subprocess.Popen([sys.executable,'-c',{worker!r}],start_new_session=True,"
        "env=env,stdin=subprocess.DEVNULL)\n"
        f"open({str(tmp_path / 'worker.pid')!r},'w').write(str(p.pid))\n"
        f"t=time.monotonic()\n"
        f"while not os.path.exists({str(tmp_path / 'titled')!r}) and time.monotonic()-t<60: time.sleep(0.05)\n"
        "time.sleep(60)\n"
    )
    env = LocalEnvironment(cwd=str(tmp_path), timeout=60)
    import threading
    box = {}
    th = threading.Thread(target=lambda: box.update(
        r=env.execute(f"{shlex.quote(sys.executable)} -c {shlex.quote(src)}", timeout=20)))
    th.start()
    pid = _wait_pid(tmp_path / "worker.pid", timeout=20)
    try:
        _wait_pid(tmp_path / "titled", timeout=40)
        proc = psutil.Process(pid)
        # The premise: the grant is gone from what psutil reads; the title is what is left.
        assert proc.environ().get("HERMES_KANBAN_OWNER_PID") != "pending"
        assert " ".join(a for a in proc.cmdline() if a) == f"hermes kanban-worker {BOOTED['HERMES_KANBAN_TASK']}"
        th.join(timeout=60)
        assert box["r"].get("returncode") == 124, box
        time.sleep(0.5)
        assert _alive(pid), "booted (retitled) worker died with its spawner's tool call"
    finally:
        _cleanup(pid)
        th.join(timeout=5)


BOOTED = {"HERMES_KANBAN_TASK": "t_4d4b30ff", "HERMES_KANBAN_RUN_ID": "1",
          "HERMES_KANBAN_OWNER_PID": "pending"}


class _FakeProc:
    def __init__(self, pid, title, parents=()):
        self.pid, self._title, self._parents = pid, title, list(parents)

    def environ(self):
        return {}

    def cmdline(self):
        return [self._title]

    def parents(self):
        return self._parents


def test_title_fallback_names_only_the_topmost_titled_process(monkeypatch):
    """A nested hermes CLI under a worker inherits the task id and takes the same title; it
    is not a second worker root (its session is not the worker's, so it would escape the sweep)."""
    from agent import delegation_context as dc

    monkeypatch.setattr(dc.os, "getsid", lambda pid: pid)
    title = "hermes kanban-worker t_4d4b30ff"
    worker = _FakeProc(101, title)
    nested = _FakeProc(202, title, parents=[_FakeProc(150, "bash"), worker])
    assert dc._is_dispatched_worker_root(worker)
    assert not dc._is_dispatched_worker_root(nested)
    assert not dc._is_dispatched_worker_root(_FakeProc(303, "hermes kanban-worker t_xyz"))


def test_spawned_worker_env_drops_the_cron_script_run_token(monkeypatch, tmp_path):
    """Prism 6a32bf9b7440: a dispatch run by a cron script must not hand the script's run
    token to the worker, or a detached daemon of the worker is swept as a script member."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from cron.scheduler_script import SCRIPT_RUN_TOKEN_ENV

    monkeypatch.setenv(SCRIPT_RUN_TOKEN_ENV, "run-token-x")
    captured = {}

    class _Proc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _Proc()

    task = kb.Task(id="t_4d4b30ff", title="slice", body=None, assignee="default",
                   status="in_progress", priority=0, created_by=None, created_at=0,
                   started_at=None, completed_at=None, workspace_kind="scratch",
                   workspace_path=None, claim_lock=None, claim_expires=None, tenant=None)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with monkeypatch.context() as m:
        m.setattr("subprocess.Popen", _fake_popen)
        m.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
        m.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")
        kbd._default_spawn(task, str(workspace))
    assert SCRIPT_RUN_TOKEN_ENV not in captured["env"]
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_4d4b30ff"
