"""A Kanban worker outlives the process that spawned it (t_4d4b30ff).

2026-11-02 07:08: a `hermes kanban dispatch` run from a terminal-tool call hit
the call's timeout, and the tool's teardown SIGKILLed the 47 setsid'd workers
the dispatch had spawned (35-card cohort death). These tests replay that: a
spawner started through the real LocalEnvironment spawns a worker the way
``_default_spawn`` does, then the tool call times out.
"""
import os
import pathlib
import sqlite3
import shlex
import sys
import time

import pytest

from agent.deadline import kill_process_tree
from tools.environments.local import LocalEnvironment

# Real signal delivery is the point: the orphaned workers leave the test subtree by design
# (setsid + reparent), and every pid signalled here was spawned by the test itself.
pytestmark = [pytest.mark.platforms("posix"), pytest.mark.live_system_guard_bypass]


@pytest.fixture(autouse=True)
def isolated_worker_board(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    db = tmp_path / "workers.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE tasks(current_run_id INTEGER, worker_pid INTEGER);"
            "CREATE TABLE task_runs(id INTEGER, worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER);"
        )
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: db)


def _record_worker(tmp_path, pid, *, fingerprint=None, ended_at=None):
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    with sqlite3.connect(tmp_path / "workers.db") as conn:
        conn.execute("INSERT INTO tasks VALUES (?, ?)", (1, pid))
        conn.execute("INSERT INTO task_runs VALUES (?, ?, ?, ?)",
                     (1, pid, fingerprint or _process_fingerprint(pid), ended_at))


def _spawner(tmp_path, *, worker_env: dict, grandchild: bool = False, recorded=True, titled=False) -> str:
    """Python source of a spawner that starts a setsid'd 'worker' and then hangs."""
    worker = "import time; time.sleep(60)"
    if titled:
        spt = pytest.importorskip("setproctitle")
        worker = (
            "import sys,time\n"
            f"sys.path[:0]={[str(pathlib.Path(__file__).resolve().parents[2]), str(pathlib.Path(spt.__file__).resolve().parent.parent)]!r}\n"
            "from hermes_cli.main import _set_process_title\n"
            "_set_process_title()\n"
            f"open({str(tmp_path / 'titled')!r},'w').write('1')\n"
            "time.sleep(60)\n"
        )
    if grandchild:
        worker = (
            "import subprocess,sys,time\n"
            "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            f"open({str(tmp_path / 'grandchild.pid')!r},'w').write(str(g.pid))\n"
            "time.sleep(60)\n"
        )
    register = ""
    if recorded:
        register = (
            f"sys.path.insert(0,{str(pathlib.Path(__file__).resolve().parents[2])!r})\n"
            "from hermes_cli.kanban_db_dispatch import _process_fingerprint\n"
            f"c=sqlite3.connect({str(tmp_path / 'workers.db')!r})\n"
            "c.execute('INSERT INTO tasks VALUES (1, ?)',(p.pid,))\n"
            "c.execute('INSERT INTO task_runs VALUES (1, ?, ?, NULL)',(p.pid,_process_fingerprint(p.pid)))\n"
            "c.commit();c.close()\n"
        )
    return (
        "import os,subprocess,sys,time,sqlite3\n"
        f"env=dict(os.environ,**{worker_env!r})\n"
        f"p=subprocess.Popen([sys.executable,'-c',{worker!r}],start_new_session=True,"
        "env=env,stdin=subprocess.DEVNULL)\n" +
        register +
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
    src = _spawner(tmp_path, worker_env=worker_env, grandchild=grandchild,
                   recorded=grandchild)
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
    spawner = subprocess.Popen([sys.executable, "-c", src], start_new_session=True,
                               stdin=subprocess.DEVNULL)
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
        _record_worker(tmp_path, pid)
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


@pytest.mark.parametrize("owner,titled", [("pending", False), ("12345", True)])
def test_forged_worker_is_killed_without_a_live_board_row(tmp_path, owner, titled):
    import subprocess
    src = _spawner(tmp_path, worker_env=dict(BOOTED, HERMES_KANBAN_OWNER_PID=owner), recorded=False, titled=titled)
    spawner = subprocess.Popen([sys.executable, "-c", src], start_new_session=True,
                               stdin=subprocess.DEVNULL)
    worker = _wait_pid(tmp_path / "worker.pid")
    try:
        if titled:
            _wait_pid(tmp_path / "titled", timeout=20)
        kill_process_tree(spawner.pid)
        spawner.wait(timeout=5)
        assert not _alive(worker), "child-controlled worker markers bypassed tree cleanup"
    finally:
        _cleanup(spawner.pid, worker)


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


@pytest.mark.parametrize("state", ["recycled", "ended", "missing", "locked"])
def test_unverified_board_worker_is_not_spared(tmp_path, state):
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              start_new_session=True, stdin=subprocess.DEVNULL)
    lock = None
    try:
        if state == "recycled":
            _record_worker(tmp_path, child.pid, fingerprint="other-boot|1")
        elif state == "ended":
            _record_worker(tmp_path, child.pid, ended_at=1)
        elif state == "missing":
            (tmp_path / "workers.db").unlink()
        else:
            _record_worker(tmp_path, child.pid)
            lock = sqlite3.connect(tmp_path / "workers.db")
            lock.execute("BEGIN EXCLUSIVE")
        from agent.delegation_context import spare_dispatched_workers
        import psutil
        procs = [psutil.Process(child.pid)]
        assert spare_dispatched_workers(procs) == procs
        assert (tmp_path / "workers.db").exists() == (state != "missing")
    finally:
        if lock is not None:
            lock.rollback()
            lock.close()
        _cleanup(child.pid)
        child.wait(timeout=5)


def test_worker_on_another_board_is_spared(tmp_path):
    """Prism 9827115d879b: a worker dispatched with ``--board work`` is recorded on that
    board's DB, not on the cleanup caller's current (``default``) board."""
    import json
    import subprocess
    import psutil
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    from agent.delegation_context import spare_dispatched_workers

    board = kb.boards_root() / "work"
    board.mkdir(parents=True)
    (board / "board.json").write_text(json.dumps({"name": "work"}))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        with sqlite3.connect(board / "kanban.db") as conn:
            conn.executescript(
                "CREATE TABLE tasks(current_run_id INTEGER, worker_pid INTEGER);"
                "CREATE TABLE task_runs(id INTEGER, worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER);"
            )
            conn.execute("INSERT INTO tasks VALUES (1, ?)", (child.pid,))
            conn.execute("INSERT INTO task_runs VALUES (1, ?, ?, NULL)",
                         (child.pid, _process_fingerprint(child.pid)))
        assert kb.get_current_board() == kb.DEFAULT_BOARD
        assert spare_dispatched_workers([psutil.Process(child.pid)]) == []
    finally:
        _cleanup(child.pid)
        child.wait(timeout=5)


def test_malformed_sibling_board_does_not_hide_a_live_worker(tmp_path):
    """Prism f67814337e1f: board discovery is per directory, so one board.json with invalid
    UTF-8 costs only that board; a live worker on ``work`` is still spared."""
    import json
    import subprocess
    import psutil
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    from agent.delegation_context import spare_dispatched_workers

    broken = kb.boards_root() / "broken"
    broken.mkdir(parents=True)
    (broken / "board.json").write_bytes(b"\xff")
    board = kb.boards_root() / "work"
    board.mkdir(parents=True)
    (board / "board.json").write_text(json.dumps({"name": "work"}))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        with sqlite3.connect(board / "kanban.db") as conn:
            conn.executescript(
                "CREATE TABLE tasks(current_run_id INTEGER, worker_pid INTEGER);"
                "CREATE TABLE task_runs(id INTEGER, worker_pid INTEGER, worker_started_at TEXT, ended_at INTEGER);"
            )
            conn.execute("INSERT INTO tasks VALUES (1, ?)", (child.pid,))
            conn.execute("INSERT INTO task_runs VALUES (1, ?, ?, NULL)",
                         (child.pid, _process_fingerprint(child.pid)))
        assert spare_dispatched_workers([psutil.Process(child.pid)]) == []
    finally:
        _cleanup(child.pid)
        child.wait(timeout=5)


def test_worker_recorded_after_first_snapshot_survives_hard_kill(tmp_path, monkeypatch):
    """Prism daf8e5e65006: W is in the first snapshot, its board row commits, the next
    scan recognises it. The accumulated target set must drop it, and W must be running."""
    import subprocess
    import psutil
    from agent import delegation_context as dc

    src = _spawner(tmp_path, worker_env=DISPATCHED, recorded=False)
    spawner = subprocess.Popen([sys.executable, "-c", src], start_new_session=True,
                               stdin=subprocess.DEVNULL)
    worker = _wait_pid(tmp_path / "worker.pid")
    real = dc.spare_dispatched_workers
    calls = []

    def spare_then_commit(procs):
        out = real(procs)
        if not calls:
            assert worker in {p.pid for p in out}, "premise: W unrecorded at first snapshot"
            _record_worker(tmp_path, worker)
        calls.append(len(out))
        return out

    monkeypatch.setattr(dc, "spare_dispatched_workers", spare_then_commit)
    try:
        kill_process_tree(spawner.pid)
        spawner.wait(timeout=5)
        time.sleep(0.3)
        assert len(calls) >= 2
        assert _alive(worker), "worker recognised on a later scan was still signalled"
        assert psutil.Process(worker).status() != psutil.STATUS_STOPPED
    finally:
        _cleanup(spawner.pid, worker)
