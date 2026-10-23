"""A pool host advertises which boards it can host; a host that cannot is
never placed on and a remote mkdir fault never spends the card's retries
(t_9fb4d477: ci-box had no subs-ace mount, 4 cards spawn_failed, 2 gave up).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch: pytest.MonkeyPatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in placement test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture(autouse=True)
def _clear_faults():
    kwp._board_faults.clear()
    yield
    kwp._board_faults.clear()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return home


def _host(name, priority=0, slots=2):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=slots,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=priority)


def _sample(boards):
    return lambda h: kwp.HostSample(1.0, 16, boards=frozenset(boards[h.name]))


def _plan(boards, names=("ci-box", "ace-ai")):
    p = kwp.plan([_host(n, i) for i, n in enumerate(names)], {}, probe=_sample(boards))
    p.band, p.spill_reason = "paused", "load"
    return p


# -- the probe: real shell against a real directory tree -----------------------

# The exact remote command, run by a local sh. Only /proc/loadavg is faked
# (a Linux file the macOS test host lacks); the board scan runs for real.
_FAKE_PROC = 'cat() { if [ "$1" = /proc/loadavg ]; then echo "1.00 1.00 1.00 1/100 1"; else command cat "$@"; fi; }; '


def _local_ssh(argv, **kw):
    return subprocess.run(["sh", "-c", _FAKE_PROC + argv[-1]], capture_output=True, text=True)


def test_probe_lists_only_writable_board_roots_on_a_real_tree(tmp_path):
    root = tmp_path / "workspaces"
    for b in ("default", "house-voice", "subs-ace"):
        (root / b).mkdir(parents=True)
    (root / "subs-ace").chmod(0o555)          # the ci-box shape: present, not writable
    (root / "stray-file").write_text("x")     # not a dir: never a board

    local_ssh = _local_ssh

    try:
        s = kwp.probe_host(_host("ci-box"), runner=local_ssh, workspaces_root=str(root))
    finally:
        (root / "subs-ace").chmod(0o755)
    if os.geteuid() == 0:
        pytest.skip("root can write a 0555 dir")
    assert isinstance(s, kwp.HostSample) and s.ncpu > 0
    assert s.boards == frozenset({"default", "house-voice"})


def test_probe_with_pressure_and_boards_in_one_round_trip(tmp_path):
    root = tmp_path / "ws"
    (root / "default").mkdir(parents=True)
    pressure = tmp_path / "p.json"
    pressure.write_text('{"at": 1, "load_ratio": 0.1}')
    calls = []

    def local_ssh(argv, **kw):
        calls.append(argv)
        return _local_ssh(argv, **kw)

    s = kwp.probe_host(_host("ace-ai"), runner=local_ssh, pressure_path=str(pressure),
                       workspaces_root=str(root))
    assert len(calls) == 1
    assert s.boards == frozenset({"default"}) and '"load_ratio"' in s.pressure_text
    assert s.remote_now is not None


def test_probe_without_the_board_list_fails_closed():
    out = "3.10 2.00 1.00 1/200 99\n16\n"   # an old shell that never printed the marker
    assert kwp.probe_host(_host("ace-ai"), runner=lambda a, **k: type(
        "P", (), {"returncode": 0, "stdout": out, "stderr": ""})(), workspaces_root="/w") is None


# -- the planner: spill, never fail --------------------------------------------

def test_unhostable_board_spills_to_the_next_host():
    p = _plan({"ci-box": {"default", "house-voice"}, "ace-ai": {"default", "subs-ace"}})
    assert p.detail["ci-box"]["boards"] == ["default", "house-voice"]
    assert p.take("alpha", board="subs-ace").name == "ace-ai"
    assert p.take("alpha", board="default").name == "ci-box"


def test_no_host_can_host_the_board_waits_with_a_named_reason():
    p = _plan({"ci-box": {"default"}, "ace-ai": {"default"}})
    assert p.take("alpha", board="subs-ace") is None
    assert p.refusal.startswith("board_unhostable (")
    assert "ci-box: subs-ace no_board_root" in p.refusal
    assert p.budget == 4  # nothing reserved


def test_a_pin_to_an_unhostable_host_waits_never_spawns():
    p = _plan({"ci-box": {"default"}, "ace-ai": {"subs-ace"}})
    assert p.take("alpha", pin="ci-box", board="subs-ace") is None
    assert p.refusal.startswith("pin_host_board_unhostable (ci-box")


def test_a_host_not_asked_for_boards_is_not_filtered():
    p = kwp.plan([_host("ci-box")], {}, probe=lambda h: (1.0, 16))
    assert "boards" not in p.detail["ci-box"]
    assert p.take("alpha", board="anything").name == "ci-box"


def test_a_board_fault_quarantines_the_pair_only():
    p = _plan({"ci-box": {"default", "subs-ace"}, "ace-ai": {"default", "subs-ace"}})
    kwp.mark_board_fault("ci-box", "subs-ace", "mkdir: Permission denied")
    assert p.take("alpha", board="subs-ace").name == "ace-ai"
    assert p.take("alpha", board="default").name == "ci-box"
    assert kwp.board_fault("ci-box", "subs-ace", now=10 ** 12) is None  # expires


# -- remote mkdir: a host fault, typed -----------------------------------------

def _mkdir_proc(rc, err):
    return lambda argv, **kw: type("P", (), {"returncode": rc, "stdout": "", "stderr": err})()


def _local_mkdir(argv, **kw):
    return subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True)


def test_remote_mkdir_under_an_unwritable_board_root_is_a_board_fault(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can write a 0555 dir")
    board = tmp_path / "subs-ace"
    board.mkdir()
    board.chmod(0o555)                     # the 19:29 ci-box shape
    try:
        with pytest.raises(kwh.RemoteWorkspaceUnavailable) as ei:
            kwh.prepare_remote_workspace(_host("ci-box"), str(board / "t_x"), runner=_local_mkdir,
                                         board="subs-ace")
    finally:
        board.chmod(0o755)
    assert (ei.value.host, ei.value.board) == ("ci-box", "subs-ace")


def test_a_card_path_fault_under_a_writable_root_is_the_cards(tmp_path):
    """Prism d72c091b628d: the card's path exists as a FILE; the board is fine."""
    board = tmp_path / "subs-ace"
    board.mkdir()
    (board / "t_x").write_text("not a dir")
    with pytest.raises(RuntimeError) as ei:
        kwh.prepare_remote_workspace(_host("ci-box"), str(board / "t_x"), runner=_local_mkdir, board="subs-ace")
    assert not isinstance(ei.value, kwh.RemoteWorkspaceUnavailable)
    kwh.prepare_remote_workspace(_host("ci-box"), str(board / "t_y"), runner=_local_mkdir, board="subs-ace")
    assert (board / "t_y").is_dir()


def test_pool_workspace_is_created_0770_and_an_existing_dir_keeps_its_mode(tmp_path):
    """t_69e47673: a pool workspace is kanbanw-only (0770, umask-independent), so the ci-box prism lane reset
    walk prunes it at depth 1; `mkdir -m` sets the LAST component only (the board root keeps 2775)."""
    board = tmp_path / "subs-ace"
    board.mkdir()
    board.chmod(0o2775)
    old = os.umask(0o002)
    try:
        kwh.prepare_remote_workspace(_host("ci-box"), str(board / "t_new"), runner=_local_mkdir, board="subs-ace")
        (board / "t_old").mkdir()
        (board / "t_old").chmod(0o775)
        kwh.prepare_remote_workspace(_host("ci-box"), str(board / "t_old"), runner=_local_mkdir, board="subs-ace")
    finally:
        os.umask(old)
    assert ((board / "t_new").stat().st_mode & 0o777) == 0o770
    assert ((board / "t_old").stat().st_mode & 0o7777) == 0o775
    assert (board.stat().st_mode & 0o7777) == 0o2775


def test_local_uid_hop_keeps_the_umask_mode():
    """The Studio local-uid hop reaches its workspace through the scratch-root ACL, never a mode: no `-m`."""
    seen = []

    def runner(argv, **kw):
        seen.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    local = kwp.PoolHost(name=kwh.LOCAL_HOST_NAME, ssh_host="localhost", ssh_user="_kanbanw", slots=0,
                         capacity_pct=1.0, absence="required", profiles=(), state="active", enabled=True,
                         priority=0)
    kwh.prepare_remote_workspace(local, "/w/default/t_x", runner=runner)
    kwh.prepare_remote_workspace(_host("ci-box"), "/w/default/t_x", runner=runner)
    assert seen[0].startswith("mkdir -p -- ") and seen[1].startswith("mkdir -p -m 0770 -- ")


def test_ssh_transport_failure_is_not_a_board_fact():
    with pytest.raises(RuntimeError) as ei:
        kwh.prepare_remote_workspace(_host("ci-box"), "/w/subs-ace/t_x",
                                     runner=_mkdir_proc(255, "Connection refused"), board="subs-ace")
    assert not isinstance(ei.value, kwh.RemoteWorkspaceUnavailable)


def test_note_board_fault_pages_once_per_episode(monkeypatch):
    sent = []
    from hermes_cli import kanban_budget
    monkeypatch.setattr(kanban_budget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv) or True)
    assert kwh.note_board_fault("ci-box", "subs-ace", "mkdir: Permission denied") is True
    assert kwh.note_board_fault("ci-box", "subs-ace", "mkdir: Permission denied") is False
    assert kwh.note_board_fault("ci-box", "house-voice", "mkdir: Permission denied") is True
    assert len(sent) == 2 and "ci-box" in sent[0][-1] and "'subs-ace'" in sent[0][-1]


# -- the dispatcher, end to end: 0 retries consumed, spill next tick -----------

def test_dispatch_mkdir_fault_consumes_no_retry_and_spills_next_tick(kanban_home, monkeypatch):
    pages = []
    from hermes_cli import kanban_budget
    monkeypatch.setattr(kanban_budget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: pages.append(argv) or True)
    # Both hosts' probes claim the board (the mount vanished after the probe):
    # the spawn itself is what fails.
    boards = {"ci-box": {"default"}, "ace-ai": {"default"}}
    placed = []

    def spawn(task, workspace, *, board=None, placement=None):
        if placement.name == "ci-box":
            raise kwh.RemoteWorkspaceUnavailable(
                "ci-box", "default", f"worker host ci-box: mkdir {workspace} failed: Permission denied")
        placed.append((task.id, placement.name))
        return 4242

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="alpha")
        for _ in range(4):  # more ticks than the default failure_limit (3)
            p = _plan(boards)
            res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=8, spawn_limit=0, spawn_paused="test",
                                    spillover=p, reconcile_orphans=False, failure_limit=3)
            print("TICK", res.spawned, res.spawn_failed, res.placement_waits, res.respawn_guarded)
            if placed:
                break
        task = kb.get_task(conn, tid)
        runs = conn.execute("SELECT outcome, metadata FROM task_runs WHERE task_id=? ORDER BY id",
                            (tid,)).fetchall()
    assert placed == [(tid, "ace-ai")]
    assert task.status == "running" and task.consecutive_failures == 0
    assert runs[0]["outcome"] == "spawn_failed" and '"infrastructure": true' in runs[0]["metadata"]
    assert '"board_fault"' in runs[0]["metadata"]
    assert len(pages) == 1


def test_a_root_that_cannot_create_is_a_board_fault_even_if_access_tests_pass(tmp_path):
    """Prism d762ee26a639: ENOSPC/quota pass -d/-w/-x. Shimmed mkdir/mktemp both fail like a full disk."""
    board = tmp_path / "subs-ace"
    board.mkdir()
    shim = tmp_path / "bin"
    shim.mkdir()
    for name in ("mkdir", "mktemp"):
        (shim / name).write_text("#!/bin/sh\necho 'No space left on device' >&2\nexit 1\n")
        (shim / name).chmod(0o755)

    def full_disk(argv, **kw):
        return subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"})

    with pytest.raises(kwh.RemoteWorkspaceUnavailable):
        kwh.prepare_remote_workspace(_host("ci-box"), str(board / "t_x"), runner=full_disk, board="subs-ace")
    assert list(board.iterdir()) == []


def test_root_probe_leaves_no_residue_on_a_card_path_fault(tmp_path):
    board = tmp_path / "subs-ace"
    board.mkdir()
    (board / "t_x").write_text("file")
    with pytest.raises(RuntimeError):
        kwh.prepare_remote_workspace(_host("ci-box"), str(board / "t_x"), runner=_local_mkdir, board="subs-ace")
    assert sorted(p.name for p in board.iterdir()) == ["t_x"]
