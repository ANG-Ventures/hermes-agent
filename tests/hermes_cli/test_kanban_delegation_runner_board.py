"""Kanban seams for the internal ``delegations`` board (option C, P1: K1-K5).

Spec: docs/dev/delegate-background-on-kanban-runtime.md §6 and §9 P1 (card t_7f2f8ecf).
One background ``delegate_task`` unit = one card on this board. Every behaviour below keys on
the slug constant ``delegations``; a ``board.json`` on another board changes nothing (RD-9).

* K1 the worker argv is ``hermes -p <profile> delegation-unit``: final, no placement hop, and
  ``HERMES_KANBAN_HOME`` pinned to the dispatcher's kanban root;
* K2 every path that would requeue a running card parks it ``blocked`` (Ace 2026-10-21 Q1 = a);
* K3 ``spawn_now`` launches one card through the dispatcher's own launch step;
* K4 the board holds no kanban capacity (Q2 = a);
* K5 ``create_task`` skips the same-incident overlap gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_delegations_board as kdb

BOARD = kdb.DELEGATIONS_BOARD
OTHER = "projects"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kb.create_board(BOARD)
    kb.create_board(OTHER)
    kb._recent_worker_exits.clear()
    yield root
    kb._recent_worker_exits.clear()


def _conn(board):
    return kb.connect_closing(board=board)


# --------------------------------------------------------------------------
# K1 runner argv
# --------------------------------------------------------------------------

def _task(**over):
    fields = dict(
        id="t_deleg1", title="unit", body=None, assignee="unit-owner", status="running",
        priority=0, created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=7,
    )
    fields.update(over)
    return kb.Task(**fields)


@pytest.fixture
def capture_spawn(home, tmp_path, monkeypatch):
    """Run the real ``_default_spawn`` with Popen captured; returns (argv tail, env)."""
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.delenv("PYTHONPATH", raising=False)
    placed = []
    monkeypatch.setattr(kb._kwh, "prepare_remote_workspace", lambda *a, **k: placed.append(a))

    def run(board, profile_yaml="", **task_over):
        prof = home / "profiles" / "unit-owner"
        prof.mkdir(parents=True, exist_ok=True)
        prof.joinpath("config.yaml").write_text(
            "model:\n  default: claude-bpr/claude-haiku-4-5\n  provider: claude-bpr\n" + profile_yaml,
            encoding="utf-8")
        got = {}

        class FakeProc:
            pid = 4242

        def fake_popen(cmd, *a, **kw):
            got["cmd"], got["env"] = list(cmd), dict(kw.get("env") or {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        kbd._default_spawn(_task(**task_over), str(ws), board=board)
        cmd = got["cmd"]
        return cmd[cmd.index("hermes"):], got["env"]

    run.placed = placed
    return run


def test_k1_delegations_argv_is_the_unit_runner(capture_spawn, home):
    cmd, env = capture_spawn(BOARD)
    assert cmd == ["hermes", "-p", "unit-owner", "delegation-unit"]
    assert env["HERMES_KANBAN_HOME"] == str(kb.kanban_home())
    assert env["HERMES_KANBAN_BOARD"] == BOARD
    assert env["HERMES_KANBAN_TASK"] == "t_deleg1"


def test_k1_argv_is_final_over_foreign_lane_model_and_effort(capture_spawn):
    cmd, env = capture_spawn(
        BOARD, "foreign_lane:\n  harness: claude-code\n  worker_command: [/bin/true]\n",
        model_override="claude-sonnet-5", provider_override="claude-bpr",
        reasoning_effort="high", skills=["some-skill"])
    assert cmd == ["hermes", "-p", "unit-owner", "delegation-unit"]
    assert "PYTHONPATH" not in env, "the native-lane branch must not have run"
    assert kb.SHIM_MODEL_CAPPED_FROM_ENV not in env


def test_k1_no_local_uid_placement_on_the_delegations_board(capture_spawn, monkeypatch):
    from hermes_cli import kanban_local_worker_uid as klu

    monkeypatch.setattr(klu, "configured_user", lambda: "kanbanworker")
    _cmd, env = capture_spawn(BOARD)
    assert kb._kwh.PLACEMENT_ENV not in env
    assert capture_spawn.placed == []
    # Control: the same knob still places an ordinary board's worker.
    _cmd, env = capture_spawn(OTHER)
    assert env.get(kb._kwh.PLACEMENT_ENV)
    assert capture_spawn.placed


def test_k1_board_json_on_another_board_changes_nothing(capture_spawn, home):
    meta = kb.board_dir(OTHER) / "board.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data.update({"runner": "delegation-unit", "respawn": False, "slug": BOARD})
    meta.write_text(json.dumps(data), encoding="utf-8")
    cmd, env = capture_spawn(OTHER)
    assert "delegation-unit" not in cmd and "chat" in cmd
    assert "HERMES_KANBAN_HOME" not in env


# --------------------------------------------------------------------------
# K2 no respawn: each requeue path parks the card ``blocked``
# --------------------------------------------------------------------------

def _running(conn, title, *, pid, heartbeat_age=None, max_runtime=None):
    tid = kb.create_task(conn, title=title, assignee="worker", max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, pid)
    if heartbeat_age is not None:
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id=?",
                     (int(time.time()) - heartbeat_age, tid))
    conn.commit()
    return tid


def _backdate(conn, tid, seconds):
    old = int(time.time()) - seconds
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at=? WHERE id=?", (old, tid))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id="
                     "(SELECT current_run_id FROM tasks WHERE id=?)", (old, tid))


def _crash(conn):
    tids = [_running(conn, "crash", pid=99999801)]
    kbd.detect_crashed_workers(conn)
    return tids


def _rate_limited(conn):
    tid = _running(conn, "rate", pid=99999802)
    from hermes_cli.kanban_worker_exit import exit_file

    db = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
    receipt = exit_file(Path(db), tid, kb.get_task(conn, tid).current_run_id)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE,
                                   "exit_class": "quota", "ts": time.time()}))
    kbd.detect_crashed_workers(conn)
    assert kbd.detect_crashed_workers._last_rate_limited == [tid]  # the stimulus took this path
    return [tid]


def _cohort(conn):
    tids = [_running(conn, f"cohort {i}", pid=99999810 + i, heartbeat_age=5) for i in range(3)]
    kbd.detect_crashed_workers(conn)
    return tids


def _stale(conn):
    tid = _running(conn, "stale", pid=99999820, heartbeat_age=7200)
    _backdate(conn, tid, 7200)
    assert kbd.detect_stale_running(conn, stale_timeout_seconds=60,
                                    signal_fn=lambda pid, sig: None) == [tid]
    return [tid]


def _max_runtime(conn):
    tid = _running(conn, "runtime", pid=99999830, max_runtime=1)
    _backdate(conn, tid, 30)
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)
    return [tid]


REQUEUE_PATHS = {"crash": _crash, "rate_limited": _rate_limited, "cohort": _cohort,
                 "stale": _stale, "max_runtime": _max_runtime}


@pytest.mark.parametrize("path", sorted(REQUEUE_PATHS))
def test_k2_requeue_path_parks_blocked_on_delegations(home, monkeypatch, path):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_page_cohort_death", lambda *a, **k: None)
    with _conn(BOARD) as conn:
        tids = REQUEUE_PATHS[path](conn)
        for tid in tids:
            assert kb.get_task(conn, tid).status == "blocked", path
            blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
            assert blocked and blocked[-1].payload["board"] == BOARD
            assert blocked[-1].payload["outcome"]
        # A tick's promotion pass never re-runs a parked unit.
        kb.recompute_ready(conn)
        for tid in tids:
            assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("path", sorted(REQUEUE_PATHS))
def test_k2_requeue_path_unchanged_on_an_ordinary_board(home, monkeypatch, path):
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_page_cohort_death", lambda *a, **k: None)
    with _conn(OTHER) as conn:
        for tid in REQUEUE_PATHS[path](conn):
            assert kb.get_task(conn, tid).status == "ready", path
            assert not [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]


# --------------------------------------------------------------------------
# K3 spawn_now
# --------------------------------------------------------------------------

def _payload_keys(conn, tid, kind):
    rows = [e for e in kb.list_events(conn, tid) if e.kind == kind]
    assert len(rows) == 1, (kind, rows)
    return set(rows[0].payload)


def test_k3_spawn_now_writes_the_same_events_as_a_tick(home, all_assignees_spawnable):
    spawn = lambda task, workspace, **kw: os.getpid()  # noqa: E731
    with _conn(OTHER) as conn:
        ticked = kb.create_task(conn, title="tick", assignee="worker")
        res = kbd.dispatch_once(conn, spawn_fn=spawn, board=OTHER)
        assert ticked in [s[0] for s in res.spawned]
        tick_keys = (_payload_keys(conn, ticked, "spawned"), _payload_keys(conn, ticked, "placement"))
    with _conn(BOARD) as conn:
        unit = kb.create_task(conn, title="unit", assignee="worker")
        assert kbd.spawn_now(conn, unit, board=BOARD, spawn_fn=spawn) == os.getpid()
        now_keys = (_payload_keys(conn, unit, "spawned"), _payload_keys(conn, unit, "placement"))
        assert kb.get_task(conn, unit).status == "running"
    assert now_keys == tick_keys


def test_k3_spawn_now_is_delegations_only(home):
    with _conn(OTHER) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        with pytest.raises(ValueError):
            kbd.spawn_now(conn, tid, board=OTHER, spawn_fn=lambda *a, **k: 1)
        assert kb.get_task(conn, tid).status == "ready"


def test_k3_spawn_failure_parks_the_card(home):
    def boom(task, workspace, **kw):
        raise RuntimeError("no runner")

    with _conn(BOARD) as conn:
        tid = kb.create_task(conn, title="unit", assignee="worker")
        with pytest.raises(kbd.SpawnNowError):
            kbd.spawn_now(conn, tid, board=BOARD, spawn_fn=boom)
        assert kb.get_task(conn, tid).status == "blocked"


def test_k3_spawn_now_refuses_a_conn_on_another_board(home):
    """The board argument names delegations but ``conn`` is another board's file (Prism 60090edde10b)."""
    with _conn(OTHER) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        with pytest.raises(ValueError):
            kbd.spawn_now(conn, tid, board=BOARD, spawn_fn=lambda *a, **k: None)
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.current_run_id is None


_WORKER_WITH_CHILD = textwrap.dedent("""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(sys.argv[1] + ".tmp", "w").write(str(child.pid))
    import os; os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(120)
""")


class _OwnSpawn:
    """A spawn_fn shaped like ``_default_spawn``: own session, handle handed over via
    ``on_popen``, registered in ``_worker_processes``. ``registry_entry`` replaces what
    the registry holds under the pid (a stand-in for a reused pid's other Popen)."""

    def __init__(self, tmp_path, *, fork_child=False, registry_entry=None, after=None):
        self.procs, self.child_pid = [], None
        self.tmp_path, self.fork_child = tmp_path, fork_child
        self.registry_entry, self.after = registry_entry, after

    def __call__(self, task, workspace, *, on_popen=None, **kw):
        if self.fork_child:
            script = self.tmp_path / "worker_with_child.py"
            script.write_text(_WORKER_WITH_CHILD, encoding="utf-8")
            pidfile = self.tmp_path / "child.pid"
            argv = [sys.executable, str(script), str(pidfile)]
        else:
            argv = [sys.executable, "-c", "import time; time.sleep(120)"]
        proc = subprocess.Popen(argv, start_new_session=True)
        self.procs.append(proc)
        if on_popen is not None:
            on_popen(proc)
        with kb._worker_processes_lock:
            kb._worker_processes[proc.pid] = self.registry_entry or proc
        if self.fork_child:
            deadline = time.monotonic() + 20
            while not pidfile.exists():
                assert time.monotonic() < deadline, "worker never forked its child"
                time.sleep(0.05)
            self.child_pid = int(pidfile.read_text())
        if self.after is not None:
            self.after(task)
        return proc.pid

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=10)
            with kb._worker_processes_lock:
                kb._worker_processes.pop(p.pid, None)
        if self.child_pid and _alive(self.child_pid):
            # Reparented once its parent died, so it is outside the guarded subtree.
            subprocess.run(["kill", "-9", str(self.child_pid)], check=False)


def _alive(pid: int) -> bool:
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _dead_within(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _pid_write_fails(*a, **k):
    import sqlite3
    raise sqlite3.OperationalError("database is locked")


def test_k3_post_launch_failure_stops_the_worker(home, tmp_path, monkeypatch):
    """The pid write raises after the process exists: it is stopped, the card parks (Prism 6a23557fb4be)."""
    spawn = _OwnSpawn(tmp_path)
    monkeypatch.setattr(kbd, "_set_worker_pid", _pid_write_fails)
    with _conn(BOARD) as conn:
        tid = kb.create_task(conn, title="unit", assignee="worker")
        try:
            with pytest.raises(kbd.SpawnNowError):
                kbd.spawn_now(conn, tid, board=BOARD, spawn_fn=spawn)
            assert spawn.procs[0].poll() is not None
            assert kb.get_task(conn, tid).status == "blocked"
        finally:
            spawn.cleanup()


def test_k3_claim_lost_after_spawn_stops_the_worker(home, tmp_path, monkeypatch):
    """The claim is revoked between spawn and the pid write: SpawnNowError, and nothing of
    this launch keeps running (Prism eebca4e909a0). The worker's create time is unreadable,
    so the shared identity-checked abort fails closed; spawn_now's own handle must stop it."""
    monkeypatch.setattr(kb, "_pid_started_in_claim", lambda *a, **k: None)

    def revoke(task):
        with _conn(BOARD) as other:
            other.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, current_run_id = NULL "
                          "WHERE id = ?", (task.id,))
            other.commit()

    spawn = _OwnSpawn(tmp_path, fork_child=True, after=revoke)
    with _conn(BOARD) as conn:
        tid = kb.create_task(conn, title="unit", assignee="worker")
        try:
            with pytest.raises(kbd.SpawnNowError, match="claim_lost"):
                kbd.spawn_now(conn, tid, board=BOARD, spawn_fn=spawn)
            assert _dead_within(spawn.procs[0].pid)
            assert _dead_within(spawn.child_pid)
        finally:
            spawn.cleanup()


def test_k3_post_launch_failure_kills_the_workers_children(home, tmp_path, monkeypatch):
    """The worker's own child tools die with it: the group is signalled (Prism 92e06f55de54)."""
    spawn = _OwnSpawn(tmp_path, fork_child=True)
    monkeypatch.setattr(kbd, "_set_worker_pid", _pid_write_fails)
    with _conn(BOARD) as conn:
        tid = kb.create_task(conn, title="unit", assignee="worker")
        try:
            with pytest.raises(kbd.SpawnNowError):
                kbd.spawn_now(conn, tid, board=BOARD, spawn_fn=spawn)
            assert spawn.procs[0].poll() is not None
            assert _dead_within(spawn.child_pid)
        finally:
            spawn.cleanup()


def test_k3_post_launch_stop_signals_only_this_launchs_process(home, tmp_path, monkeypatch):
    """The registry holds ANOTHER Popen under the same pid (pid reuse): only the handle this
    launch created is signalled, never the registry's (Prism 4c387d9a1291)."""
    class OtherBoardsWorker:
        pid, returncode, signalled = None, None, []

        def poll(self):
            return None

        def terminate(self):
            self.signalled.append("terminate")

        def kill(self):
            self.signalled.append("kill")

        def wait(self, timeout=None):
            return None

        def send_signal(self, sig):
            self.signalled.append(sig)

    other = OtherBoardsWorker()
    spawn = _OwnSpawn(tmp_path, registry_entry=other)
    monkeypatch.setattr(kbd, "_set_worker_pid", _pid_write_fails)
    with _conn(BOARD) as conn:
        tid = kb.create_task(conn, title="unit", assignee="worker")
        try:
            with pytest.raises(kbd.SpawnNowError):
                kbd.spawn_now(conn, tid, board=BOARD, spawn_fn=spawn)
            assert spawn.procs[0].poll() is not None
            assert other.signalled == []
            with kb._worker_processes_lock:
                assert kb._worker_processes.get(spawn.procs[0].pid) is other
        finally:
            spawn.cleanup()


def test_k3_stale_spawn_failure_leaves_the_successor_run(home, monkeypatch):
    """Run A's workspace failure must not end run B, which claimed the card meanwhile (Prism f2afc6f28e76)."""
    successor = {}

    def resolve_then_lose_the_card(task, **kw):
        with _conn(BOARD) as other:
            other.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?", (task.id,))
            other.commit()
            successor["task"] = kb.claim_task(other, task.id)
        raise OSError("workspace gone")

    monkeypatch.setattr(kbd._kbw, "resolve_workspace", resolve_then_lose_the_card)
    with _conn(BOARD) as conn:
        tid = kb.create_task(conn, title="unit", assignee="worker")
        with pytest.raises(kbd.SpawnNowError):
            kbd.spawn_now(conn, tid, board=BOARD, spawn_fn=lambda *a, **k: None)
        run_b = successor["task"].current_run_id
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.current_run_id == run_b
        assert conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (run_b,)).fetchone()[0] is None


# --------------------------------------------------------------------------
# K4 budget isolation
# --------------------------------------------------------------------------

def test_k4_delegations_running_cards_hold_no_kanban_capacity(home):
    with _conn(BOARD) as conn:
        for i in range(3):
            _running(conn, f"unit {i}", pid=99999840 + i)
    with _conn(OTHER) as conn:
        _running(conn, "ordinary", pid=99999850)
    assert kbd.count_running_tasks_other_boards(kb.DEFAULT_BOARD) == 1
    assert kb.count_running_tasks_host() == 1
    by_board = kb.count_running_by_placement(kb.list_boards(include_archived=False))
    assert BOARD not in by_board and by_board[OTHER][0] == 1


# --------------------------------------------------------------------------
# K5 no overlap gate
# --------------------------------------------------------------------------

_INCIDENT = ("grok lane returns HTTP 426 upgrade required on every call",
             'origin: session {sid}\nxAI 426 on grok: "Upgrade Required: client version too old for this endpoint"')


def _mint_pair(conn):
    ids = []
    for sid in ("sess-a", "sess-b"):
        ids.append(kb.create_task(conn, title=_INCIDENT[0], body=_INCIDENT[1].format(sid=sid),
                                  assignee="worker", session_id=sid, session_explicit=True,
                                  duplicate_guard=True, force_reason="pair"))
    return ids


def _overlap_marks(conn, tid):
    return ([e for e in kb.list_events(conn, tid) if e.kind == "overlap_detected"],
            [c for c in kb.list_comments(conn, tid) if c.body.startswith("OVERLAPS")])


def test_k5_delegations_create_skips_the_overlap_gate(home):
    with _conn(BOARD) as conn:
        for tid in _mint_pair(conn):
            assert _overlap_marks(conn, tid) == ([], [])
    with _conn(OTHER) as conn:  # control: the same pair is flagged elsewhere
        assert any(_overlap_marks(conn, tid)[0] for tid in _mint_pair(conn))


# --------------------------------------------------------------------------
# E2E: real spawn, SIGKILL, one tick -> blocked, one spawned event
# --------------------------------------------------------------------------

_STUB = textwrap.dedent("""
    import os, sys, time
    time.sleep(float(os.environ.get("STUB_SLEEP", "60")))
    from hermes_cli import kanban_db as kb
    with kb.connect_closing(board=os.environ["HERMES_KANBAN_BOARD"]) as conn:
        kb.complete_task(conn, os.environ["HERMES_KANBAN_TASK"], summary="stub done")
""")


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_e2e_sigkilled_unit_is_parked_not_respawned(home, tmp_path, monkeypatch,
                                                    all_assignees_spawnable):
    stub = tmp_path / "stub_runner.py"
    stub.write_text(_STUB, encoding="utf-8")
    repo = str(Path(__file__).resolve().parents[2])
    # The runner subcommand is stubbed: the spawned argv is ``<stub> -p <profile> delegation-unit``.
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: [sys.executable, str(stub)])
    monkeypatch.setenv("PYTHONPATH", repo)
    monkeypatch.setattr(kbd, "_propagate_module_import_root",
                        lambda cmd, env: env.__setitem__("PYTHONPATH", repo))
    ws = tmp_path / "session-cwd"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "keep.txt").write_text("user work\n", encoding="utf-8")
    before = _tree_hash(ws)

    with _conn(BOARD) as conn:
        tid = kb.create_task(conn, title="unit", assignee="worker",
                             workspace_kind="dir", workspace_path=str(ws))
        pid = kbd.spawn_now(conn, tid, board=BOARD)
        # The dispatcher's own Popen handle (I8: never signal a bare pid).
        with kb._worker_processes_lock:
            proc = kb._worker_processes[pid]
        try:
            assert list(proc.args)[-3:] == ["-p", "worker", "delegation-unit"]
            proc.kill()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        for _ in range(2):
            kbd.dispatch_once(conn, board=BOARD, spawn_fn=lambda *a, **k: pytest.fail("respawned"))
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert [e.kind for e in kb.list_events(conn, tid)].count("spawned") == 1
        assert kb.archive_task(conn, tid) is True
    assert _tree_hash(ws) == before
