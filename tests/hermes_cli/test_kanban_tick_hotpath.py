"""Per-tick cost guards for the in-gateway kanban dispatcher (card t_45a4bee2).

A 40 s py-spy of the Apollo gateway (2026-10-26) put 34 % of all GIL samples
in the dispatcher tick. Each test below pins one repeated cost that tick paid
on every pass, and each fails against the code before the fix:

1. two ``git`` forks per gated card's workspace per tick (toplevel + remotes);
2. a full YAML re-parse of the board-home ``config.yaml`` per knob read;
3. a ``ps -o stat=`` fork per live worker pid per tick (macOS);
4. a host-wide process walk + realpath per tick for the orphan sweep.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_gate as prg


# --------------------------------------------------------------------------- 1


def _git_init(path: Path, remote: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)


@pytest.fixture
def git_spawns(monkeypatch: pytest.MonkeyPatch) -> list:
    if hasattr(prg, "_REPO_PROBE_CACHE"):
        prg._REPO_PROBE_CACHE.clear()
    calls: list = []
    real = prg.subprocess.run

    def counting(argv, *a, **k):
        calls.append(list(argv))
        return real(argv, *a, **k)

    monkeypatch.setattr(prg.subprocess, "run", counting)
    return calls


def test_unchanged_checkout_spawns_git_once_per_probe(tmp_path, git_spawns):
    repo = tmp_path / "wt"
    _git_init(repo, "git@github.com:o/r.git")
    for _ in range(20):
        assert prg._remotes_for(str(repo)) == {"o/r"}
        assert prg._is_repo_toplevel(str(repo)) is True
    toplevel = [c for c in git_spawns if "--show-toplevel" in c]
    remote = [c for c in git_spawns if c[-2:] == ["remote", "-v"]]
    assert len(toplevel) == 1, f"rev-parse ran {len(toplevel)}x for an unchanged checkout"
    assert len(remote) == 1, f"remote -v ran {len(remote)}x for an unchanged checkout"


def test_rewritten_remotes_are_seen_on_the_next_call(tmp_path, git_spawns):
    repo = tmp_path / "wt"
    _git_init(repo, "git@github.com:o/r.git")
    assert prg._remotes_for(str(repo)) == {"o/r"}
    assert prg._remotes_for(str(repo)) == {"o/r"}
    # Rewrite .git/config directly (no git), as an editor or a re-clone would.
    cfg = repo / ".git" / "config"
    cfg.write_text(cfg.read_text().replace("o/r.git", "o/other.git"))
    assert prg._remotes_for(str(repo)) == {"o/other"}


def test_linked_worktree_sees_a_remote_change_in_the_common_dir(tmp_path, git_spawns):
    origin = tmp_path / "origin"
    _git_init(origin, "git@github.com:o/r.git")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=origin, check=True)
    wt = tmp_path / "worktrees" / "t_x"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "wt/t_x"],
                   cwd=origin, check=True)
    assert (wt / ".git").is_file()
    assert prg._remotes_for(str(wt)) == {"o/r"}
    assert prg._remotes_for(str(wt)) == {"o/r"}
    subprocess.run(["git", "remote", "set-url", "origin", "git@github.com:o/moved.git"],
                   cwd=origin, check=True)
    assert prg._remotes_for(str(wt)) == {"o/moved"}


def test_walk_up_still_answers_not_toplevel_and_stays_cached(tmp_path, git_spawns):
    """t_cb701eee: a dir under a parent checkout is NOT a toplevel, every call."""
    outer = tmp_path / "outer"
    _git_init(outer, "git@github.com:ANG-Ventures/hermes-home.git")
    scratch = outer / "kanban" / "workspaces" / "t_x"
    scratch.mkdir(parents=True)
    for _ in range(5):
        assert prg._is_repo_toplevel(str(scratch)) is False
        assert prg._remotes_for(str(scratch)) == set()
    assert len([c for c in git_spawns if "--show-toplevel" in c]) == 1
    # ...and becoming a checkout of its own is seen at once.
    subprocess.run(["git", "init", "-q"], cwd=scratch, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:o/own.git"],
                   cwd=scratch, check=True)
    assert prg._is_repo_toplevel(str(scratch)) is True
    assert prg._remotes_for(str(scratch)) == {"o/own"}


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def probe_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(prg, "_repo_probe_clock", clock, raising=False)
    return clock


def _git_init_with_include(tmp_path: Path) -> tuple[Path, Path]:
    """Prism repro: .git/config defines no remote; an [include]d file defines origin."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    ext = tmp_path / "ext.gitconfig"
    ext.write_text('[remote "origin"]\n\turl = https://github.com/acme/old.git\n')
    with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write(f"[include]\n\tpath = {ext}\n")
    return repo, ext


def test_included_config_remote_change_is_seen_immediately(tmp_path, git_spawns, probe_clock):
    repo, ext = _git_init_with_include(tmp_path)
    assert prg._remotes_for(str(repo)) == {"acme/old"}
    assert prg._remotes_for(str(repo)) == {"acme/old"}
    ext.write_text('[remote "origin"]\n\turl = https://github.com/acme/brand-new.git\n')
    # Same clock reading: only the include-aware signature can notice this.
    assert prg._remotes_for(str(repo)) == {"acme/brand-new"}


def test_invisible_config_dependency_is_reprobed_after_ttl(
    tmp_path, git_spawns, probe_clock, monkeypatch: pytest.MonkeyPatch,
):
    """A dependency no signature can see (GIT_CONFIG_GLOBAL) is re-probed within the TTL."""
    glob = tmp_path / "global.gitconfig"
    glob.write_text('[remote "upstream"]\n\turl = https://github.com/acme/old.git\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    repo = tmp_path / "wt"
    _git_init(repo, "git@github.com:o/r.git")
    assert prg._remotes_for(str(repo)) == {"o/r", "acme/old"}
    glob.write_text('[remote "upstream"]\n\turl = https://github.com/acme/new.git\n')
    ttl = getattr(prg, "_REPO_PROBE_TTL_SECONDS", 120)
    probe_clock.t += ttl + 1
    assert prg._remotes_for(str(repo)) == {"o/r", "acme/new"}


def test_repeat_within_ttl_still_hits_the_cache(tmp_path, git_spawns, probe_clock):
    repo, _ext = _git_init_with_include(tmp_path)
    for _ in range(10):
        assert prg._remotes_for(str(repo)) == {"acme/old"}
        probe_clock.t += 5
    assert len([c for c in git_spawns if c[-2:] == ["remote", "-v"]]) == 1
    assert len([c for c in git_spawns if "--show-toplevel" in c]) == 1


# --------------------------------------------------------------------------- 2


@pytest.fixture
def board_home(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    if hasattr(kb, "_BOARD_HOME_CFG_CACHE"):
        kb._BOARD_HOME_CFG_CACHE.clear()
    return home


@pytest.fixture
def yaml_parses(monkeypatch: pytest.MonkeyPatch) -> list:
    from hermes_cli import config as cfgmod

    calls: list = []
    real = cfgmod.fast_safe_load

    def counting(stream, *a, **k):
        calls.append(getattr(stream, "name", stream))
        return real(stream, *a, **k)

    monkeypatch.setattr(cfgmod, "fast_safe_load", counting)
    return calls


def test_board_home_cfg_parses_an_unchanged_file_once(board_home, yaml_parses):
    (board_home / "config.yaml").write_text("kanban:\n  dispatch_interval_seconds: 60\n")
    for _ in range(50):
        section = kb._board_home_kanban_cfg()
        assert section == {"dispatch_interval_seconds": 60}
        section["dispatch_interval_seconds"] = 1  # callers may mutate their copy
    assert len(yaml_parses) == 1, f"board-home config.yaml parsed {len(yaml_parses)}x"


def test_board_home_cfg_sees_an_edit_on_the_next_call(board_home, yaml_parses):
    path = board_home / "config.yaml"
    path.write_text("kanban:\n  review_policy: all\n")
    assert kb._board_home_kanban_cfg() == {"review_policy": "all"}
    st = path.stat()
    path.write_text("kanban:\n  review_policy: none\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # pin mtime: ctime/size still move
    assert kb._board_home_kanban_cfg() == {"review_policy": "none"}


def test_board_home_cfg_missing_and_unparseable(board_home):
    assert kb._board_home_kanban_cfg() == {}
    (board_home / "config.yaml").write_text("kanban: [unclosed\n")
    with pytest.raises(Exception):
        kb._board_home_kanban_cfg()
    (board_home / "config.yaml").write_text("kanban:\n  x: ${TICK_HOTPATH_VAR}\n")
    os.environ["TICK_HOTPATH_VAR"] = "a"
    try:
        assert kb._board_home_kanban_cfg() == {"x": "a"}
        os.environ["TICK_HOTPATH_VAR"] = "b"  # env expansion is per call, not cached
        assert kb._board_home_kanban_cfg() == {"x": "b"}
    finally:
        os.environ.pop("TICK_HOTPATH_VAR", None)


# --------------------------------------------------------------------------- 3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX zombies")
def test_pid_alive_spawns_no_subprocess_per_pid(monkeypatch: pytest.MonkeyPatch):
    kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            for _ in range(4)]
    try:
        spawned: list = []
        real_run = subprocess.run

        def counting_run(argv, *a, **k):
            spawned.append(list(argv))
            return real_run(argv, *a, **k)

        monkeypatch.setattr(kbd.subprocess, "run", counting_run)
        for _ in range(3):
            assert all(kbd._pid_alive(k.pid) for k in kids)
        assert spawned == [], f"_pid_alive forked per pid: {spawned[:3]}"
    finally:
        for k in kids:
            k.kill()
            k.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX zombies")
def test_pid_alive_reports_dead_zombie_and_unknown_pids():
    pid = os.fork()  # windows-footgun: ok (POSIX-only test)
    if pid == 0:
        os._exit(0)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and kb.psutil.Process(pid).status() != kb.psutil.STATUS_ZOMBIE:
            time.sleep(0.02)
        assert kb.psutil.Process(pid).status() == kb.psutil.STATUS_ZOMBIE
        assert kbd._pid_alive(pid) is False, "an unreaped zombie child counted as alive"
    finally:
        os.waitpid(pid, 0)
    assert kbd._pid_alive(pid) is False
    assert kbd._pid_alive(os.getpid()) is True
    assert kbd._pid_alive(0) is False and kbd._pid_alive(None) is False


# --------------------------------------------------------------------------- 4


@pytest.fixture
def conn(tmp_path, monkeypatch: pytest.MonkeyPatch):
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
    if hasattr(kbd, "_ORPHAN_SWEEP_LAST"):
        kbd._ORPHAN_SWEEP_LAST.clear()
    with kb.connect() as c:
        yield c


def test_orphan_sweep_runs_once_per_window_per_board(conn, monkeypatch: pytest.MonkeyPatch):
    sweeps: list = []
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "reap_exited_worker_leftovers", lambda c, pids: [])
    monkeypatch.setattr(kb, "sweep_terminal_workspace_orphans",
                        lambda c, board=None: sweeps.append(board) or {})
    clock = [1000.0]
    monkeypatch.setattr(kbd, "_orphan_sweep_clock", lambda: clock[0], raising=False)

    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default")
    clock[0] += 60
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default")
    assert sweeps == ["default"], f"orphan sweep ran {len(sweeps)}x inside one window"
    clock[0] += 300
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, board="default")
    assert sweeps == ["default", "default"], "a tick past the window did not sweep again"
