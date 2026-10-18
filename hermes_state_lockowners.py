"""Who holds (or just held) the SQLite write lock on state.db.

Two sources: ``/proc/locks`` for the holder at this instant (Linux only), and a holder-side ledger
every SessionDB writer keeps for its own long holds, which still names a holder that released just
before a waiter's deadline and works on macOS.

The "database is locked for over Ns" failure names the victim but not the holder, and the
open-descriptor scan (``hermes_state_holders``) cannot tell a reader from the one writer: every
Hermes process has the DB open. SQLite's unix VFS takes ``fcntl`` byte-range locks whose offsets
encode the lock kind, and the kernel exports them with the owning pid, so the holder can be named
at the moment the deadline passes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# sqlite/src/os_unix.c: UNIX_SHM_BASE = (22 + SQLITE_SHM_NLOCK) * 4 = 120; WAL lock i sits at BASE + i.
_SHM_LOCK_NAMES = {
    120: "WAL write",
    121: "WAL checkpoint",
    122: "WAL recover",
    128: "shm dead-man switch",
}
# Rollback-journal / exclusive locks on the main file live in the pending-byte page (1 GiB).
_PENDING_BYTE = 0x40000000
_MAIN_LOCK_NAMES = {
    _PENDING_BYTE: "PENDING",
    _PENDING_BYTE + 1: "RESERVED",
}


def _describe_range(sidecar: str, start: int, end: int) -> str:
    if sidecar == "-shm":
        if start in _SHM_LOCK_NAMES and end == start:
            return _SHM_LOCK_NAMES[start]
        if 123 <= start <= 127 and end == start:
            return f"WAL read slot {start - 123}"
        return f"shm bytes {start}-{end}"
    if start == end and start in _MAIN_LOCK_NAMES:
        return _MAIN_LOCK_NAMES[start]
    if start == _PENDING_BYTE + 2:
        return "SHARED range"
    return f"db bytes {start}-{end}"


def parse_proc_locks(text: str, inodes: Dict[Tuple[int, int], str]) -> List[Tuple[int, str, str]]:
    """``(pid, lock kind, sidecar)`` for every WRITE lock on one of ``inodes``.

    ``inodes`` maps ``(st_dev, st_ino)`` to ``""`` (main file), ``"-wal"`` or ``"-shm"``. Read locks
    are dropped: they never block a writer in WAL mode. An OFD lock is reported with pid ``-1``
    (the kernel does not export its owner).
    """
    found: List[Tuple[int, str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        # "N: POSIX ADVISORY WRITE <pid> MAJ:MIN:INO <start> <end|EOF>"; a blocked waiter is "N: -> POSIX ...".
        if len(fields) < 8 or fields[1] == "->":
            continue
        _, _kind, _cls, access, pid_s, ident, start_s, end_s = fields[:8]
        if access != "WRITE":
            continue
        try:
            major_s, minor_s, ino_s = ident.split(":")
            dev = os.makedev(int(major_s, 16), int(minor_s, 16))
            key = (dev, int(ino_s))
            pid = int(pid_s)
            start = int(start_s)
            end = start if end_s == "EOF" else int(end_s)
        except ValueError:
            continue
        sidecar = inodes.get(key)
        if sidecar is None:
            continue
        found.append((pid, _describe_range(sidecar, start, end), sidecar))
    return found


def state_db_write_lock_holders(db_path) -> List[str]:
    """Operator-facing lines naming the processes that hold a write-class lock on ``db_path``.

    Empty when nothing is held or the platform has no ``/proc/locks``.
    """
    if not sys.platform.startswith("linux"):
        return []
    base = os.path.realpath(os.fspath(db_path))
    inodes: Dict[Tuple[int, int], str] = {}
    for sidecar in ("", "-wal", "-shm"):
        try:
            st = os.stat(base + sidecar)
        except OSError:
            continue
        inodes[(st.st_dev, st.st_ino)] = sidecar
    # /proc/locks is host-wide and served over several read()s: lock churn in other processes
    # shifts it mid-read and can skip the holder. A skip rarely repeats, so union three passes.
    held: List[Tuple[int, str, str]] = []
    for _ in range(3):
        try:
            with open("/proc/locks", encoding="ascii", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return []
        held.extend(entry for entry in parse_proc_locks(text, inodes) if entry not in held)
    from hermes_state_holders import describe_holder_pid

    lines = []
    for pid, kind, sidecar in held:
        who = describe_holder_pid(pid) if pid > 0 else "OFD lock, owner pid not exported by the kernel"
        lines.append(f"{who} holds {kind} lock on {Path(base + sidecar).name}")
    return lines


# ── Holder-side ledger ──
# /proc/locks shows a holder only while it still holds, and macOS has no /proc/locks at all, so the
# deadline scan reported "released just before it" for every stall on the Studio. Each SessionDB
# writer therefore records its OWN long holds: a watchdog thread marks a hold still open after
# _HOLD_RECORD_MIN_S ("holding"), and the end of any hold that long is written with its duration
# ("released"). One small JSON file per pid under ``<state.db>.lockholders/``, latest hold only.
_HOLD_RECORD_MIN_S = 2.0
_LEDGER_SUFFIX = ".lockholders"
_LEDGER_MAX_AGE_S = 86400.0
_TIMEOUT_MEMORY_S = 600.0
_WATCHDOG_TICK_S = 0.5

_holds_lock = threading.Lock()
_active_holds: Dict[int, dict] = {}
_hold_seq = 0
_watchdog_running = False
_watchdog_pid = 0
_last_timeout: Dict[str, Tuple[float, str]] = {}


def _ledger_dir(db_path) -> str:
    return os.path.realpath(os.fspath(db_path)) + _LEDGER_SUFFIX


def _write_record(hold: dict, **fields) -> None:
    """Atomic per-pid write of *hold* plus *fields*; never raises (attribution must not break a write)."""
    try:
        if hold["ledger"] is None:  # resolved lazily: only holds >= _HOLD_RECORD_MIN_S pay for it
            from hermes_state_holders import describe_holder_pid

            hold["ledger"] = _ledger_dir(hold["db_path"])
            hold["who"] = describe_holder_pid(hold["pid"])
        ledger = hold["ledger"]
        os.makedirs(ledger, mode=0o700, exist_ok=True)
        target = os.path.join(ledger, f"{hold['pid']}.json")
        tmp = f"{target}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"pid": hold["pid"], "who": hold["who"], "op": hold["op"], "since": hold["since"],
                       **fields}, handle)
        os.replace(tmp, target)
        cutoff = time.time() - _LEDGER_MAX_AGE_S
        for name in os.listdir(ledger):
            path = os.path.join(ledger, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    os.unlink(path)
            except OSError:
                pass
    except Exception:
        logger.debug("state.db lock-holder ledger write failed", exc_info=True)


def _watchdog_loop() -> None:
    global _watchdog_running
    while True:
        time.sleep(_WATCHDOG_TICK_S)
        now = time.monotonic()
        with _holds_lock:
            if not _active_holds:
                _watchdog_running = False  # under the lock: hold_started() restarts it
                return
            due = [h for h in _active_holds.values()
                   if not h["marked"] and now - h["t0"] >= _HOLD_RECORD_MIN_S]
            for hold in due:
                hold["marked"] = True
        for hold in due:
            _write_record(hold, state="holding")


def hold_started(db_path, op: str) -> int:
    """Register a write-lock hold that just began (BEGIN IMMEDIATE succeeded); returns its token."""
    global _hold_seq, _watchdog_running, _watchdog_pid
    with _holds_lock:
        _hold_seq += 1
        token = _hold_seq
        _active_holds[token] = {"db_path": db_path, "op": op, "t0": time.monotonic(), "since": time.time(),
                                "marked": False, "pid": os.getpid(), "who": None, "ledger": None}
        if not _watchdog_running or _watchdog_pid != os.getpid():  # a fork child has no watchdog thread
            _watchdog_running, _watchdog_pid = True, os.getpid()
            threading.Thread(target=_watchdog_loop, name="state-db-lock-ledger", daemon=True).start()
    return token


def hold_ended(token: int) -> None:
    """Close a hold; one of >= _HOLD_RECORD_MIN_S is written to the ledger with its duration."""
    with _holds_lock:
        hold = _active_holds.pop(token, None)
    if hold is None:
        return
    held = time.monotonic() - hold["t0"]
    if held >= _HOLD_RECORD_MIN_S:
        _write_record(hold, state="released", held_s=round(held, 1), released_at=time.time())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def recent_ledger_holders(db_path, window_s: float, *, exclude_pid: Optional[int] = None) -> List[str]:
    """Ledger lines for holds that are still open (live pid) or ended within the last *window_s*."""
    ledger = _ledger_dir(db_path)
    try:
        names = os.listdir(ledger)
    except OSError:
        return []
    now = time.time()
    found: List[Tuple[float, str]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(ledger, name), encoding="utf-8") as handle:
                rec = json.load(handle)
            pid = int(rec["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid == exclude_pid:
            continue
        op = rec.get("op") or "write"
        who = rec.get("who") or f"PID {pid}"
        since = float(rec.get("since") or 0.0)
        if rec.get("state") == "holding" and _pid_alive(pid):
            found.append((now, f"{who} has held the write lock for {now - since:.0f}s ({op}, still holding)"))
        elif rec.get("state") == "released":
            released = float(rec.get("released_at") or 0.0)
            if now - released <= window_s:
                found.append((released, f"{who} held the write lock for {float(rec.get('held_s') or 0):.0f}s "
                                         f"({op}), released {now - released:.0f}s ago"))
    return [line for _, line in sorted(found, reverse=True)]


def last_timeout_holder(db_path=None) -> Optional[str]:
    """Holder named by this process's most recent write-lock deadline (last 10 min): on *db_path*, or
    on any state.db when None (the cron explainer renders without a path and must match the turn's)."""
    if db_path is not None:
        entry = _last_timeout.get(os.path.realpath(os.fspath(db_path)))
    else:
        entry = max(_last_timeout.values(), default=None)
    if entry is None or time.time() - entry[0] > _TIMEOUT_MEMORY_S:
        return None
    return entry[1]


def describe_write_lock_holders(db_path, window_s: float) -> str:
    """Current /proc/locks holders (Linux) plus the holder-side ledger, one line; never raises."""
    try:
        lines = state_db_write_lock_holders(db_path)
    except Exception:
        lines = []
    try:
        # Own pid included: another SessionDB instance in this process (a multiplexed profile, a
        # housekeeping thread's handle) holds the lock as surely as another process does.
        lines += [line for line in recent_ledger_holders(db_path, window_s) if line not in lines]
    except Exception:
        logger.debug("state.db lock-holder ledger read failed", exc_info=True)
    if lines:
        return " | ".join(lines)
    if not sys.platform.startswith("linux"):
        return ("holder unknown: no Hermes process recorded a write-lock hold of "
                f"{_HOLD_RECORD_MIN_S:.0f}s+ in the last {window_s:.0f}s (/proc/locks is Linux-only)")
    return ("holder unknown: no write-class lock held at the deadline and no Hermes process recorded a "
            f"write-lock hold of {_HOLD_RECORD_MIN_S:.0f}s+ in the last {window_s:.0f}s")


def log_write_lock_holders(db_path, patience_s: float) -> str:
    """One WARNING naming the write-lock holders at the moment a writer gave up waiting; returns the detail."""
    detail = describe_write_lock_holders(db_path, patience_s + 5.0)
    _last_timeout[os.path.realpath(os.fspath(db_path))] = (time.time(), detail)
    logger.warning(
        "state.db write lock unavailable for %.0fs (%s): %s", patience_s, Path(db_path).name, detail
    )
    return detail
