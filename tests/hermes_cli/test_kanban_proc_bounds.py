"""Process bounds for kanban worker trees (t_368e9873).

2026-10-01: a worker's scratch bench re-spawned itself 18,818 times and filled
the uid's process table (10,340 / 10,666); every fork on the host failed.
2026-09-29: a worker's pytest leaked 153 headless Chromes (load 250).

The drills here reproduce both failures in miniature and prove the guard
stops them. Every process signalled here is one the test started.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_proc_bounds as kpb

POSIX = hasattr(os, "getsid") and hasattr(os, "killpg")

pytestmark = [
    # Leftovers are reparented to init by construction; they are still only
    # processes this test started.
    pytest.mark.live_system_guard_bypass,
]

_STARTED: list[int] = []


def _gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kb._pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


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
            os.killpg(pid, 9)
        except OSError:
            pass
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    _STARTED.clear()


# ---------------------------------------------------------------------------
# config resolution
# ---------------------------------------------------------------------------

resource = pytest.importorskip("resource")


@pytest.mark.parametrize(
    "raw, current, want",
    [
        ("auto", (10666, 16000), 7999),        # 75% of the macOS default
        (None, (10666, 16000), 7999),          # unset == auto (via default)
        ("garbage", (10666, 16000), 7999),     # typo fails SAFE: guard stays on
        (500, (10666, 16000), 500),
        ("500", (10666, 16000), 500),
        (20000, (10666, 16000), None),         # never RAISES the inherited limit
        (0, (10666, 16000), None),
        ("off", (10666, 16000), None),
        (False, (10666, 16000), None),
        ("auto", (resource.RLIM_INFINITY, resource.RLIM_INFINITY), None),
        (300, (resource.RLIM_INFINITY, resource.RLIM_INFINITY), 300),
    ],
)
def test_resolve_worker_nproc_limit(raw, current, want):
    cfg = {} if raw is None else {"worker_nproc_limit": raw}
    assert kpb.resolve_worker_nproc_limit(cfg, current=current) == want


@pytest.mark.parametrize(
    "raw, want",
    [(None, 256), (512, 512), (5, 32), (0, None), ("off", None), ("x", 256)],
)
def test_worker_max_procs_per_run(raw, want):
    cfg = {} if raw is None else {"worker_max_procs_per_run": raw}
    assert kpb.worker_max_procs_per_run(cfg) == want


def test_defaults_are_in_config_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    k = DEFAULT_CONFIG["kanban"]
    assert k["worker_nproc_limit"] == "auto"
    assert k["worker_max_procs_per_run"] == kpb.DEFAULT_WORKER_MAX_PROCS_PER_RUN


def test_nproc_preexec_lowers_the_child_limit():
    """The preexec lowers RLIMIT_NPROC in a real child (read back by it)."""
    soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    if soft != resource.RLIM_INFINITY and soft <= 4321:
        pytest.skip("inherited limit already at/below the probe value")
    preexec = kpb.chain_preexec(None, kpb.worker_nproc_preexec_limit(4321))
    out = subprocess.run(
        [sys.executable, "-c",
         "import resource;print(resource.getrlimit(resource.RLIMIT_NPROC))"],
        preexec_fn=preexec, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "(4321, 4321)"


# ---------------------------------------------------------------------------
# DRILL 1: fork bomb stops at the cap (RLIMIT_NPROC is per-uid)
# ---------------------------------------------------------------------------

# Bounded bomb: forks up to ATTEMPTS sleepers, reports how many it got and why
# it stopped, then kills its own process group. Runs in its own session.
_BOMB = (
    "import os, signal, sys, time\n"
    "attempts = int(sys.argv[1]); kids = 0; err = '-'\n"
    "for _ in range(attempts):\n"
    "    try:\n"
    "        pid = os.fork()  # windows-footgun: ok (POSIX-only test)\n"
    "    except OSError as e:\n"
    "        err = type(e).__name__; break\n"
    "    if pid == 0:\n"
    "        time.sleep(60); os._exit(0)\n"
    "    kids += 1\n"
    "print(kids, err, flush=True)\n"
    "os.killpg(0, signal.SIGKILL)  # windows-footgun: ok (POSIX-only test)\n"
)

_ATTEMPTS = 200
_HEADROOM = 30


def _uid_procs() -> int:
    uid = os.getuid()
    n = 0
    for p in kb.psutil.process_iter(["uids"]):
        try:
            if p.info["uids"].real == uid:
                n += 1
        except Exception:
            pass
    return n


def _run_bomb(preexec) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", _BOMB, str(_ATTEMPTS)],
        preexec_fn=preexec, start_new_session=True,
        capture_output=True, text=True, timeout=120,
    )
    kids, err = proc.stdout.split()
    return int(kids), err


@pytest.mark.skipif(not POSIX or not hasattr(resource, "RLIMIT_NPROC"),
                    reason="POSIX RLIMIT_NPROC only")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root bypasses RLIMIT_NPROC")
def test_drill_fork_bomb_stops_at_the_cap():
    # Control arm (the mutant: no preexec): the same bomb gets every fork.
    kids, err = _run_bomb(None)
    assert (kids, err) == (_ATTEMPTS, "-"), "control bomb was limited; drill proves nothing"

    cap = _uid_procs() + _HEADROOM
    soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    if soft != resource.RLIM_INFINITY and cap >= soft:
        pytest.skip("uid already near its limit")
    kids, err = _run_bomb(kpb.worker_nproc_preexec_limit(cap))
    assert err in ("BlockingIOError", "OSError"), (kids, err)
    # The uid count drifts with other processes; the bomb must stop near the
    # headroom, far below its attempts.
    assert kids < _ATTEMPTS
    assert kids <= _HEADROOM * 3, kids


# ---------------------------------------------------------------------------
# DRILL 2: live worker over the per-run cap is killed, reaped, blocked
# ---------------------------------------------------------------------------

# Fake worker: forks N sleepers in its session plus one that setsid()s OUT of
# the session (as a browser daemon does), prints every pid, then idles.
_WORKER = (
    "import os, sys, time\n"
    "n = int(sys.argv[1]); pids = []\n"
    "for i in range(n):\n"
    "    pid = os.fork()  # windows-footgun: ok (POSIX-only test)\n"
    "    if pid == 0:\n"
    "        if i == 0:\n"
    "            os.setsid()  # windows-footgun: ok (POSIX-only test)\n"
    "        elif i % 2:\n"
    "            os.setpgid(0, 0)\n"
    "        time.sleep(300); os._exit(0)\n"
    "    pids.append(pid)\n"
    "print(' '.join(map(str, pids)), flush=True)\n"
    "time.sleep(300)\n"
)


def _spawn_worker(conn, tid: str, n: int) -> tuple[subprocess.Popen, list[int]]:
    task = kb.get_task(conn, tid)
    env = dict(os.environ, HERMES_KANBAN_TASK=tid,
               HERMES_KANBAN_RUN_ID=str(task.current_run_id))
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(n)], stdout=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    _STARTED.append(worker.pid)
    kids = [int(x) for x in worker.stdout.readline().split()]
    _STARTED.extend(kids)
    assert kb._set_worker_pid(conn, tid, worker.pid)
    return worker, kids


@pytest.mark.skipif(not POSIX, reason="POSIX sessions only")
def test_drill_runaway_worker_tree_is_killed_reaped_and_blocked(conn):
    big = kb.create_task(conn, title="runaway", assignee="worker")
    small = kb.create_task(conn, title="healthy", assignee="worker")
    assert kb.claim_task(conn, big) is not None
    assert kb.claim_task(conn, small) is not None
    time.sleep(1.1)  # spawn strictly after the claim second (owner window)
    w_big, kids_big = _spawn_worker(conn, big, 40)
    w_small, kids_small = _spawn_worker(conn, small, 4)

    counts = kpb.census_worker_trees({
        (big, str(kb.get_task(conn, big).current_run_id)): w_big.pid,
        (small, str(kb.get_task(conn, small).current_run_id)): w_small.pid,
    })
    by_task = {k[0]: v["procs"] for k, v in counts.items()}
    assert by_task[big] == 41, counts  # worker + 40 (incl. the setsid escapee)
    assert by_task[small] == 5, counts

    capped = kpb.enforce_worker_process_cap(conn, cap=32, notify=False)
    assert capped == [big]
    w_big.wait(timeout=10)
    survivors = [p for p in kids_big if not _gone(p, timeout=10)]
    assert survivors == [], f"{len(survivors)} of the runaway tree survived"

    t = kb.get_task(conn, big)
    assert t.status == "blocked"
    assert t.block_kind == "capability"
    ev = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='process_cap_exceeded'",
        (big,)).fetchone()
    assert ev is not None and '"procs": 41' in ev["payload"]

    # The healthy worker under the cap is untouched.
    assert w_small.poll() is None
    assert all(kb._pid_alive(p) for p in kids_small)
    assert kb.get_task(conn, small).status == "running"


@pytest.mark.skipif(not POSIX, reason="POSIX sessions only")
def test_cap_off_and_dry_run_never_signal(conn, monkeypatch):
    tid = kb.create_task(conn, title="runaway", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    time.sleep(1.1)
    w, kids = _spawn_worker(conn, tid, 40)
    monkeypatch.setattr(kpb, "worker_max_procs_per_run", lambda *a, **k: None)
    assert kpb.enforce_worker_process_cap(conn, notify=False) == []
    kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)
    assert w.poll() is None and all(kb._pid_alive(p) for p in kids)


def test_failed_block_is_not_reported_as_capped(conn, monkeypatch):
    # Prism P1: a kill whose block did not persist must not be paged or
    # returned as a blocked card.
    tid = kb.create_task(conn, title="runaway", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _STARTED.append(sleeper.pid)
    assert kb._set_worker_pid(conn, tid, sleeper.pid)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker",
                        lambda *a, **k: {"terminated": True})
    pages: list = []
    monkeypatch.setattr(kpb, "_notify_cap", lambda *a: pages.append(a))
    fake = lambda runs: {k: {"procs": 999, "top": [("x", 999)]} for k in runs}

    def _locked(*a, **k):
        raise kb.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb, "block_task", _locked)
    assert kpb.enforce_worker_process_cap(conn, cap=32, census=fake) == []
    monkeypatch.setattr(kb, "block_task", lambda *a, **k: False)
    assert kpb.enforce_worker_process_cap(conn, cap=32, census=fake) == []
    assert pages == []
    # The audit event still lands, and its payload is JSON (Prism P1 #2).
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='process_cap_exceeded'",
        (tid,)).fetchall()
    assert len(rows) == 2 and '"top": [["x", 999]]' in rows[0]["payload"]


@pytest.mark.skipif(not POSIX, reason="POSIX sessions only")
def test_census_ignores_members_older_than_the_worker(conn, monkeypatch):
    # Prism P1: a recycled worker pid must not inherit an older session's
    # members (or what they fork). Simulate: the recorded worker is "born"
    # after its session's members.
    tid = kb.create_task(conn, title="card", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    w, kids = _spawn_worker(conn, tid, 6)
    key = (tid, str(kb.get_task(conn, tid).current_run_id))
    assert kpb.census_worker_trees({key: w.pid})[key]["procs"] == 7  # control
    real = kb._member_birth
    monkeypatch.setattr(kb, "_member_birth",
                        lambda pid: time.time() + 5.0 if pid == w.pid else real(pid))
    # The 5 older session members drop out; the worker (the new leader) and
    # the env-tagged setsid escapee outside the session still count.
    assert kpb.census_worker_trees({key: w.pid})[key]["procs"] == 2


def test_cmdline_profile_cards():
    f = kb._cmdline_profile_cards
    assert f(["chrome", "--user-data-dir=/x/workspaces/t_ab12/chrome"]) == {"t_ab12"}
    assert f(["chrome", "--user-data-dir", "/r/.worktrees/t_cd34/p"]) == {"t_cd34"}
    assert f(["chrome", "--user-data-dir=/tmp/prof", "/x/t_ab12"]) == set()
    assert f(["chrome", "--user-data-dir=/x/t_ab12x/p"]) == set()
    assert f(None) == set()


def test_dispatch_tick_runs_the_cap(conn, monkeypatch):
    calls: list = []
    monkeypatch.setattr(kpb, "enforce_worker_process_cap",
                        lambda c: calls.append(1) or ["t_x"])
    res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
    assert calls == [1]
    assert res.process_capped == ["t_x"]


# ---------------------------------------------------------------------------
# DRILL 3: a worker-launched headless Chrome is reaped with the card
# ---------------------------------------------------------------------------

def _headless_chrome() -> str | None:
    import pwd

    # The real home: the conn fixture monkeypatches Path.home to tmp_path.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    for pattern in (
        "Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell",
        ".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell",
    ):
        for c in sorted(home.glob(pattern)):
            return str(c)
    return (shutil.which("chrome-headless-shell") or shutil.which("chromium")
            or shutil.which("google-chrome"))


# Fake worker: launches headless Chrome detached into its OWN session (the way
# a browser harness daemon or a pytest fixture does), with its profile under
# the card workspace, prints the browser pid, waits for "go", exits.
_CHROME_WORKER = (
    "import os, subprocess, sys\n"
    "p = subprocess.Popen([sys.argv[1], '--headless', '--no-sandbox',\n"
    "     '--user-data-dir=' + sys.argv[2], '--remote-debugging-port=0', 'about:blank'],\n"
    "     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
    "print(p.pid, flush=True)\n"
    "sys.stdin.readline()\n"
    "os._exit(0)\n"
)


def _chrome_procs(profile: str) -> list[int]:
    out = []
    for p in kb.psutil.process_iter(["cmdline"]):
        try:
            if any(profile in a for a in (p.info["cmdline"] or [])):
                out.append(p.pid)
        except Exception:
            pass
    return out


@pytest.mark.skipif(not POSIX, reason="POSIX sessions only")
def test_drill_worker_chrome_is_reaped_with_the_card(conn, tmp_path):
    chrome = _headless_chrome()
    if chrome is None:
        pytest.skip("no headless Chrome on this host")
    tid = kb.create_task(conn, title="browser card", assignee="worker")
    task = kb.claim_task(conn, tid)
    assert task is not None
    time.sleep(1.1)
    profile = str(tmp_path / "ws" / tid / "chrome-profile")
    env = dict(os.environ, HERMES_KANBAN_TASK=tid,
               HERMES_KANBAN_RUN_ID=str(task.current_run_id))
    spawned_at = time.time()
    worker = subprocess.Popen(
        [sys.executable, "-c", _CHROME_WORKER, chrome, profile],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
        start_new_session=True,
    )
    with kb._worker_processes_lock:
        kb._worker_processes[worker.pid] = worker
    kb._register_worker_identity(worker.pid, tid, task.current_run_id, spawned_at)
    assert kb._set_worker_pid(conn, tid, worker.pid)
    browser = int(worker.stdout.readline())
    _STARTED.append(browser)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(_chrome_procs(profile)) < 2:
        time.sleep(0.2)
    leaked = _chrome_procs(profile)
    assert len(leaked) >= 2, "Chrome did not start its helpers; drill proves nothing"
    _STARTED.extend(leaked)
    # The live per-run census sees the browser too (Linux Chrome erases its
    # environ window; the profile path under the card workspace names it).
    key = (tid, str(task.current_run_id))
    assert kpb.census_worker_trees({key: worker.pid})[key]["procs"] >= 1 + len(leaked)

    assert kb.complete_task(conn, tid, summary="done")
    worker.stdin.write("go\n")
    worker.stdin.flush()
    deadline = time.monotonic() + 10
    exited: list[int] = []
    while time.monotonic() < deadline and worker.pid not in exited:
        exited += kb.reap_worker_zombies()
        time.sleep(0.05)
    assert worker.pid in exited
    assert kb._pid_alive(browser), "Chrome died with its worker; drill proves nothing"

    assert kb.reap_exited_worker_leftovers(conn, exited, grace=5.0) == [tid]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _chrome_procs(profile):
        time.sleep(0.2)
    assert _chrome_procs(profile) == [], "Chrome outlived its card"
