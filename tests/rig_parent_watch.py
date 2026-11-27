"""Parent-death watch for test rigs that start a gateway in its own process group (t_c791d60f).

A rig that starts ``hermes gateway run`` with ``start_new_session=True`` and ends it only in fixture
teardown leaks the gateway at ppid 1 when pytest dies before teardown runs: SIGKILL (a killed worker,
an OOM), a ``timeout`` SIGTERM, a kanban runtime cap. The rig's own session is what keeps a group
kill aimed at pytest from reaching the gateway, so nothing ends it. Measured on the Studio
2026-11-11 for the hermes-home rig (t_410c3080): gateways days old at ppid 1.

Rigs register each group they start::

    from tests import rig_parent_watch
    proc = subprocess.Popen([...], start_new_session=True)
    rig_parent_watch.track(proc.pid)        # right after the spawn
    ...
    <stop the group>
    rig_parent_watch.untrack(proc.pid)      # once the rig has ended it itself

``track`` starts (once per pytest process) a watcher in its own session, with a pipe on stdin whose
only write end this pytest process holds. Protocol, one line each:

    add <pgid>    reap this process group if the pytest process dies
    del <pgid>    the rig stopped it itself; forget it

EOF on stdin means the write end closed, which happens only when this pytest process exits, by any
means, SIGKILL included (the kernel closes its fds). The watcher then SIGTERMs every group still
registered, waits ``GRACE_S``, SIGKILLs survivors, appends one line per group to its log and exits.
A clean pytest exit with every group ``del``-ed leaves nothing to do; a group a rig forgot to stop
(a boot failure before the fixture's yield) is reaped at pytest exit as well.

Run as a script, this file is the watcher. Standard library only and no import from the test tree:
it runs after pytest may already be gone. Shape copied from hermes-home
``plugins/model-providers/tests_e2e/gateway_rig_parent_watch.py``.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

GRACE_S = 10.0
LOG_ENV = "HERMES_TEST_RIG_WATCH_LOG"

# ── watcher side (runs as ``python rig_parent_watch.py <log>``) ────────────────


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _log(path: str, line: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} pid={os.getpid()} {line}\n")
    except OSError:
        pass


def _reap(pgids: set[int], log_path: str, grace: float = GRACE_S) -> None:
    for pg in sorted(pgids):
        try:
            os.killpg(pg, signal.SIGTERM)
            _log(log_path, f"parent died: SIGTERM process group {pg}")
        except ProcessLookupError:
            _log(log_path, f"parent died: process group {pg} already gone")
        except PermissionError as exc:
            _log(log_path, f"parent died: process group {pg} not signalable ({exc})")
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_group_alive(pg) for pg in pgids):
        time.sleep(0.2)
    for pg in sorted(pgids):
        if _group_alive(pg):
            try:
                os.killpg(pg, signal.SIGKILL)
                _log(log_path, f"parent died: SIGKILL process group {pg} (survived {grace:g}s SIGTERM)")
            except (ProcessLookupError, PermissionError):
                pass


def main(argv: list[str]) -> int:
    log_path = argv[1] if len(argv) > 1 else os.devnull
    # A dying pytest's terminal signals must not preempt the reap; EOF is the only trigger.
    for sig in (signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    groups: set[int] = set()
    for line in sys.stdin:
        verb, _, arg = line.strip().partition(" ")
        if verb in ("add", "del") and arg.isdigit() and int(arg) > 1:
            (groups.add if verb == "add" else groups.discard)(int(arg))
    if groups:
        _reap(groups, log_path)
    return 0


# ── rig side (imported by the test process) ────────────────────────────────

_lock = threading.Lock()
_watch: subprocess.Popen | None = None
_watch_owner: int | None = None


def _ensure_watch() -> subprocess.Popen | None:
    """The watcher for THIS process (a forked child gets its own, never its parent's)."""
    global _watch, _watch_owner
    if not hasattr(os, "killpg"):
        return None
    if _watch is not None and _watch_owner == os.getpid() and _watch.poll() is None:
        return _watch
    log_path = os.environ.get(LOG_ENV) or os.path.join(tempfile.gettempdir(), f"hermes-rig-watch-{os.getuid()}.log")
    _watch = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), log_path],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, text=True, close_fds=True)
    _watch_owner = os.getpid()
    return _watch


def _send(verb: str, pgid: int) -> None:
    with _lock:
        watch = _ensure_watch() if verb == "add" else (_watch if _watch_owner == os.getpid() else None)
        if watch is None or watch.stdin is None:
            return
        try:
            watch.stdin.write(f"{verb} {int(pgid)}\n")
            watch.stdin.flush()
        except (OSError, ValueError):
            pass


def track(pgid: int) -> None:
    """Reap process group ``pgid`` (a ``start_new_session=True`` child's pid) if this process dies."""
    _send("add", pgid)


def untrack(pgid: int) -> None:
    """The rig ended ``pgid`` itself; the watcher forgets it."""
    _send("del", pgid)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
