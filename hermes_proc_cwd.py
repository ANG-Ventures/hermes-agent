"""Read other processes' cwds without letting a dead mount freeze this process.

``psutil.Process.cwd()`` on macOS (``proc_pidinfo``) blocks in the kernel while the
target's cwd sits on a dead hard NFS mount, and it does so holding the GIL: every thread
of the caller stops (Studio, 2026-11-03: the gateway dispatcher and two agent workers
froze until the mount was force-unmounted). So the probe runs in a short-lived child
process; a pid that does not answer within the timeout is skipped and the child is
abandoned, never waited on in the caller.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import time
from typing import Iterable

logger = logging.getLogger(__name__)

CWD_PROBE_TIMEOUT_SECONDS = 2.0
# Interpreter start + ``import psutil``; a loaded host is slow, and a miss only skips a pass.
_PROBE_START_TIMEOUT_SECONDS = 15.0

# One JSON line per pid, ``[pid, cwd]``, cwd "" when unreadable. "ready" once psutil is in.
_PROBE_CHILD = (
    "import json, sys\n"
    "import psutil\n"
    "w = sys.stdout.write\n"
    "w('ready\\n'); sys.stdout.flush()\n"
    "for a in sys.argv[1:]:\n"
    "    pid = int(a)\n"
    "    try:\n"
    "        cwd = psutil.Process(pid).cwd() or ''\n"
    "    except Exception:\n"
    "        cwd = ''\n"
    "    w(json.dumps([pid, cwd]) + '\\n'); sys.stdout.flush()\n"
)

# pid -> the probe child stuck on it. While that child lives the mount is still dead, so
# the pid is skipped without spawning another child that would only block the same way.
_STUCK: dict[int, subprocess.Popen] = {}
_STUCK_LOCK = threading.Lock()


def _abandon(child: subprocess.Popen) -> None:
    """Kill *child* without blocking: a process in uninterruptible sleep dies only when its
    syscall returns, so the reap is left to a daemon thread."""
    try:
        child.kill()
    except OSError:
        pass
    threading.Thread(target=child.wait, name="cwd-probe-reap", daemon=True).start()


def _reap(child: subprocess.Popen, timeout: float) -> None:
    """Wait up to *timeout* for a child that should be exiting, else abandon it."""
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _abandon(child)


def _pump(stream, out: "queue.Queue[str | None]") -> None:
    try:
        for line in stream:
            out.put(line)
    except (OSError, ValueError):
        pass
    out.put(None)


def _run_batch(pids: list[int], timeout: float, found: dict[int, str]) -> list[int]:
    """Probe *pids* in one child. Returns the pids not reached when a probe hung (the hung
    pid itself excluded), or [] when the batch finished or could not run at all."""
    try:
        child = subprocess.Popen(
            # -P and cwd="/": a module in the caller's cwd (a worker workspace) must not
            # shadow json/psutil and run inside the probe; "/" is also never a dead mount.
            [sys.executable, "-P", "-c", _PROBE_CHILD, *map(str, pids)],
            cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        logger.warning("cwd probe: could not start the probe child (%s); %d pid(s) skipped", exc, len(pids))
        return []
    lines: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_pump, args=(child.stdout, lines), name="cwd-probe-read", daemon=True).start()
    try:
        first = lines.get(timeout=_PROBE_START_TIMEOUT_SECONDS)
    except queue.Empty:
        first = None
        logger.warning("cwd probe: probe child did not start in %.0fs; %d pid(s) skipped",
                       _PROBE_START_TIMEOUT_SECONDS, len(pids))
        _abandon(child)
        return []
    if first is None or first.strip() != "ready":
        _reap(child, timeout)
        logger.warning("cwd probe: probe child failed to start; %d pid(s) skipped", len(pids))
        return []
    for index, pid in enumerate(pids):
        try:
            line = lines.get(timeout=timeout)
        except queue.Empty:
            logger.warning("cwd probe: pid %d did not answer in %.1fs (cwd on a dead mount?); skipped",
                           pid, timeout)
            with _STUCK_LOCK:
                _STUCK[pid] = child
            _abandon(child)
            return pids[index + 1:]
        if line is None:  # child died mid-batch: skip the rest of this pass
            break
        try:
            got, cwd = json.loads(line)
        except (ValueError, TypeError):
            continue
        if cwd:
            found[int(got)] = cwd
    _reap(child, timeout)
    return []


def probe_cwds(pids: Iterable[int], *, timeout: float = CWD_PROBE_TIMEOUT_SECONDS) -> dict[int, str]:
    """``{pid: cwd}`` for each of *pids* whose cwd could be read. A pid whose probe does not
    answer within *timeout* seconds is skipped (one log line names it); a pid that cannot
    be read is absent. The caller never blocks on a single pid for longer than *timeout*."""
    with _STUCK_LOCK:
        for pid, child in list(_STUCK.items()):
            if child.poll() is not None:
                del _STUCK[pid]
        stuck = set(_STUCK)
    todo = [pid for pid in dict.fromkeys(pids) if pid not in stuck]
    found: dict[int, str] = {}
    started = time.monotonic()
    while todo:
        todo = _run_batch(todo, timeout, found)
    if stuck:
        logger.debug("cwd probe: %d pid(s) skipped, earlier probe still stuck: %s", len(stuck), sorted(stuck))
    logger.debug("cwd probe: %d pid(s) answered in %.2fs", len(found), time.monotonic() - started)
    return found
