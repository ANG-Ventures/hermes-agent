"""Caller-thread stall per spawn for the gateway's real forking callers (t_aa2e2d4e). Manual, not collected.

    <runtime-python> tests/tools/spawn_callers_bench.py --rss-gb 4 --threads 200 --spawns 40

Same harness shape as ``spawn_gil_bench.py`` (#172): a host process with ``--rss-gb`` of
page-touched, fragmented heap and ``--threads`` idle threads; a ticker thread records the
longest gap between two of its loop iterations while each call runs (a gap that long means
some thread held the GIL, or the whole process was stopped, for that time). Each call is a REAL
gateway caller function, unmodified, with stub ``gh`` / ``ssh`` executables first on PATH so
it never touches the network:

* ``pr_gate.query_pr``          hermes_cli/kanban_pr_gate.py  (``gh api repos/../pulls/N``)
* ``pr_gate._git``              hermes_cli/kanban_pr_gate.py  (``git -C <repo> remote -v``)
* ``owner_wake._gh_json``       gateway/kanban_owner_wake.py   (``gh api <path>``)
* ``worker_pool._probe_once``   hermes_cli/kanban_worker_pool.py (the gate-tick ``ssh`` probe)

``--tree <main worktree> --modes main`` measures main's own code; this checkout with
``--modes main,branch`` measures the same callers without, then with, the hook.

Modes: ``main`` = plain subprocess (what the gateway does without the hook: fork_exec, which on
macOS is a real fork of the whole process); ``branch`` = after
``tools.spawn_nofork.install_process_hook()`` (what ``start_gateway`` now does). On a tree
without the hook (main) only ``main`` runs. Off macOS the hook does not install and both modes
are plain Popen (vfork).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time


def _tree() -> str:
    # --tree <checkout> imports the callers from another checkout (e.g. a main worktree), so the
    # "main" number is main's own code, not this branch's with the hook left off.
    if "--tree" in sys.argv:
        return os.path.abspath(sys.argv[sys.argv.index("--tree") + 1])
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


sys.path.insert(0, _tree())

from hermes_cli import kanban_pr_gate as pr_gate  # noqa: E402
from hermes_cli import kanban_worker_pool as kwp  # noqa: E402
from gateway import kanban_owner_wake as owner_wake  # noqa: E402


class Ticker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = False
        self.window_max = 0.0

    def run(self):
        last = time.perf_counter()
        while not self.stop:
            now = time.perf_counter()
            if now - last > self.window_max:
                self.window_max = now - last
            last = now


def _stubs(root: str) -> str:
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir)
    scripts = {
        "gh": '#!/bin/sh\necho \'{"state":"open","merged_at":null,"merge_commit_sha":null,"head":{"sha":"x"}}\'\n',
        "ssh": "#!/bin/sh\necho '1.50 32'\n",
    }
    for name, body in scripts.items():
        path = os.path.join(bindir, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    return bindir


def _callers(repo: str):
    host = kwp.PoolHost(name="bench", ssh_host="bench.invalid", ssh_user="u", slots=None, capacity_pct=100.0,
                        absence="", profiles=(), state="active", enabled=True, priority=0)
    return {
        "pr_gate.query_pr": lambda: pr_gate.query_pr("o/r", 1),
        "pr_gate._git": lambda: pr_gate._git(repo, "remote", "-v"),
        "owner_wake._gh_json": lambda: owner_wake._gh_json("repos/o/r/pulls/1"),
        "worker_pool._probe_once": lambda: kwp._probe_once(host, subprocess.run),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rss-gb", type=float, default=4.0)
    ap.add_argument("--threads", type=int, default=200)
    ap.add_argument("--spawns", type=int, default=40)
    ap.add_argument("--fds", type=int, default=1500)
    ap.add_argument("--modes", default="main,branch")
    ap.add_argument("--tree", default=None, help="checkout to import the callers from (default: this one)")
    opts = ap.parse_args()
    try:
        import tools.spawn_nofork as nofork
        has_hook = hasattr(nofork, "install_process_hook")
    except ImportError:
        nofork, has_hook = None, False

    import _posixsubprocess
    counters = {"fork_exec": 0}
    owner = subprocess if hasattr(subprocess, "_fork_exec") else _posixsubprocess
    name = "_fork_exec" if owner is subprocess else "fork_exec"
    real_fork = getattr(owner, name)

    def counted_fork(*args, **kwargs):
        counters["fork_exec"] += 1
        return real_fork(*args, **kwargs)

    setattr(owner, name, counted_fork)

    tmp = tempfile.mkdtemp(prefix="t_aa2e2d4e-bench-")
    os.environ["PATH"] = _stubs(tmp) + os.pathsep + os.environ.get("PATH", "")
    repo = os.path.join(tmp, "repo")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    callers = _callers(repo)
    for label, fn in callers.items():  # every caller must succeed before it is timed
        assert fn() is not None, f"{label} returned None against the stubs"

    descriptors = [os.open(os.devnull, os.O_RDONLY) for _ in range(opts.fds)]
    chunks = [bytearray(64 * 1024) for _ in range(int(opts.rss_gb * (1 << 30)) // (64 * 1024))]
    for chunk in chunks:
        chunk[::4096] = b"\x01" * 16
    idle = threading.Event()
    for _ in range(opts.threads):
        threading.Thread(target=idle.wait, daemon=True).start()
    sys.setswitchinterval(0.0005)
    ticker = Ticker()
    ticker.start()
    import resource
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    results = {"tree": _tree(), "python": sys.version.split()[0], "executable": sys.executable, "platform": sys.platform,
               "pid": os.getpid(), "maxrss_mb": maxrss // (1 << 20) if sys.platform == "darwin" else maxrss // 1024,
               "threads": threading.active_count(), "fds_open": len(descriptors), "has_hook": has_hook,
               "modes": {}}
    ms = lambda xs, q: round(1000 * statistics.quantiles(xs, n=20)[q], 3)  # noqa: E731
    for mode in opts.modes.split(","):
        if mode == "branch":
            if not has_hook:
                results["modes"][mode] = "skipped: tools.spawn_nofork has no install_process_hook (main tree)"
                continue
            results["hook_installed"] = nofork.install_process_hook()
        per = {}
        for label, fn in callers.items():
            counters["fork_exec"] = 0
            stalls, walls = [], []
            for _ in range(opts.spawns):
                time.sleep(0.01)
                ticker.window_max = 0.0
                t0 = time.perf_counter()
                out = fn()
                walls.append(time.perf_counter() - t0)
                stalls.append(ticker.window_max)
                assert out is not None, label
            per[label] = {"fork_exec_calls": counters["fork_exec"], "calls": opts.spawns,
                          "stall_ms_p50": round(1000 * statistics.median(stalls), 3),
                          "stall_ms_p95": ms(stalls, 18), "stall_ms_max": round(1000 * max(stalls), 3),
                          "wall_ms_p50": round(1000 * statistics.median(walls), 3), "wall_ms_p95": ms(walls, 18)}
        results["modes"][mode] = per
        if mode == "branch":
            nofork.uninstall_process_hook()
    ticker.stop = True
    idle.set()
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
