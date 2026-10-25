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
import re
import stat
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
# Serializes ledger publication with hold completion: a watchdog "holding" write that lost the race
# to hold_ended() must not overwrite the "released" record (Prism 147f02dd5e23).
_publish_lock = threading.Lock()
_active_holds: Dict[int, dict] = {}
_hold_seq = 0
# Highest hold seq published per ledger dir by THIS process (its own <pid>.json). A descheduled older
# writer's hold_ended() must not replace a newer writer's record (Prism "reordered publication").
_published_seq: Dict[str, int] = {}
# Only names this module writes are ever pruned: <pid>.json and its <pid>.json.<tid>.tmp.
_LEDGER_NAME_RE = re.compile(r"^\d+\.json(\.\d+\.tmp)?$")
_DIR_FD_OK = (all(f in os.supports_dir_fd for f in (os.open, os.unlink, os.stat, os.rename))  # os.replace shares rename's renameat
              and os.listdir in os.supports_fd and os.stat in os.supports_follow_symlinks
              and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"))
_watchdog_running = False
_watchdog_pid = 0
_last_timeout: Dict[str, Tuple[float, str]] = {}
# Bound at import: the watchdog is internal plumbing. Tests (and libraries) that replace
# ``threading.Thread`` with an inline starter would otherwise run the watchdog loop inside
# hold_started() while it holds _holds_lock, deadlocking the writer (the 4 tui_gateway reds on #2072).
_Thread = threading.Thread


def _reset_after_fork() -> None:
    """A fork child inherits the locks in whatever state another parent thread left them, and no
    watchdog thread: start from scratch (Prism c0c7a990a4d6)."""
    global _holds_lock, _publish_lock, _active_holds, _published_seq, _watchdog_running, _watchdog_pid
    _holds_lock, _publish_lock = threading.Lock(), threading.Lock()
    _active_holds, _published_seq = {}, {}
    _watchdog_running, _watchdog_pid = False, 0


if hasattr(os, "register_at_fork"):  # POSIX only
    os.register_at_fork(after_in_child=_reset_after_fork)


def _ledger_dir(db_path) -> str:
    return os.path.realpath(os.fspath(db_path)) + _LEDGER_SUFFIX


def _open_ledger(ledger: str, *, create: bool) -> Optional[int]:
    """fd of the ledger dir, or None when it is missing or not ours. Never follows a symlink: a
    planted ``<db>.lockholders`` link must not steer writes or the 24 h prune into its target. The
    ledger is diagnostic only, so a wrong dir is skipped, never repaired or deleted."""
    if create:
        try:
            os.mkdir(ledger, 0o700)
        except FileExistsError:
            pass
    fd = os.open(ledger, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)  # ELOOP on a symlink
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:  # windows-footgun: ok — POSIX only (_DIR_FD_OK)
        os.close(fd)
        logger.debug("state.db lock-holder ledger %s skipped: not a private dir owned by this user", ledger)
        return None
    return fd


def _write_record(hold: dict, **fields) -> None:
    """Atomic per-pid write of *hold* plus *fields*; never raises (attribution must not break a write).
    Callers hold _publish_lock."""
    try:
        if hold["ledger"] is None:  # resolved lazily: only holds >= _HOLD_RECORD_MIN_S pay for it
            from hermes_state_holders import describe_holder_pid

            hold["ledger"] = _ledger_dir(hold["db_path"])
            hold["who"] = describe_holder_pid(hold["pid"])
        ledger = hold["ledger"]
        if hold["seq"] < _published_seq.get(ledger, 0):
            return  # a newer hold of this process already owns <pid>.json
        if not _DIR_FD_OK:  # Windows: no dir_fd / O_NOFOLLOW; write only, never prune
            if os.path.lexists(ledger) and (os.path.islink(ledger) or not os.path.isdir(ledger)):
                return
            os.makedirs(ledger, exist_ok=True)
            _write_json(os.path.join(ledger, f"{hold['pid']}.json"), hold, fields)
            _published_seq[ledger] = hold["seq"]
            return
        fd = _open_ledger(ledger, create=True)
        if fd is None:
            return
        try:
            name = f"{hold['pid']}.json"
            tmp = f"{name}.{threading.get_ident()}.tmp"
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, "w", encoding="utf-8") as handle:
                json.dump(_record(hold, fields), handle)
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            _published_seq[ledger] = hold["seq"]
            cutoff = time.time() - _LEDGER_MAX_AGE_S
            for entry in os.listdir(fd):
                if not _LEDGER_NAME_RE.match(entry):
                    continue
                try:
                    st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                        os.unlink(entry, dir_fd=fd)
                except OSError:
                    pass
        finally:
            os.close(fd)
    except Exception:
        logger.debug("state.db lock-holder ledger write failed", exc_info=True)


def _record(hold: dict, fields: dict) -> dict:
    return {"pid": hold["pid"], "seq": hold["seq"], "who": hold["who"], "op": hold["op"], "since": hold["since"],
            **fields}


def _write_json(target: str, hold: dict, fields: dict) -> None:
    tmp = f"{target}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(_record(hold, fields), handle)
    os.replace(tmp, target)


def _select_due_holds() -> Optional[List[dict]]:
    """Mark and return holds open >= _HOLD_RECORD_MIN_S; None when no hold is active (watchdog exits)."""
    global _watchdog_running
    now = time.monotonic()
    with _holds_lock:
        if not _active_holds:
            _watchdog_running = False  # under the lock: hold_started() restarts it
            return None
        due = [h for h in _active_holds.values() if not h["marked"] and now - h["t0"] >= _HOLD_RECORD_MIN_S]
        for hold in due:
            hold["marked"] = True
    return due


def _publish_holding(hold: dict) -> None:
    with _publish_lock:
        if not hold["ended"]:  # hold_ended() already published "released": never resurrect it
            _write_record(hold, state="holding")


def _watchdog_loop() -> None:
    while True:
        time.sleep(_WATCHDOG_TICK_S)
        due = _select_due_holds()
        if due is None:
            return
        for hold in due:
            _publish_holding(hold)


def hold_started(db_path, op: str) -> Optional[int]:
    """Register a write-lock hold that just began (BEGIN IMMEDIATE succeeded); returns its token.
    Never raises: a failure here (thread exhaustion at load 160) must not break the write."""
    global _hold_seq, _watchdog_running, _watchdog_pid
    try:
        with _holds_lock:
            _hold_seq += 1
            token = _hold_seq
            _active_holds[token] = {"db_path": db_path, "op": op, "t0": time.monotonic(), "since": time.time(),
                                    "marked": False, "ended": False, "pid": os.getpid(), "who": None,
                                    "ledger": None, "seq": token}
            if not _watchdog_running or _watchdog_pid != os.getpid():
                try:
                    _Thread(target=_watchdog_loop, name="state-db-lock-ledger", daemon=True).start()
                    _watchdog_running, _watchdog_pid = True, os.getpid()
                except Exception:  # RuntimeError("can't start new thread"): no "holding" marks this time
                    logger.debug("state.db lock-holder watchdog did not start", exc_info=True)
        return token
    except Exception:
        logger.debug("state.db lock-holder ledger registration failed", exc_info=True)
        return None


def hold_ended(token: Optional[int]) -> None:
    """Close a hold; one of >= _HOLD_RECORD_MIN_S is written to the ledger with its duration."""
    if token is None:
        return
    with _holds_lock:
        hold = _active_holds.pop(token, None)
        if hold is not None:
            hold["ended"] = True
    if hold is None:
        return
    held = time.monotonic() - hold["t0"]
    if held >= _HOLD_RECORD_MIN_S or hold["marked"]:
        with _publish_lock:
            _write_record(hold, state="released", held_s=round(held, 1), released_at=time.time())


def note_lock_hold(db_path, op: str, held_s: float) -> None:
    """Record a finished in-process lock hold (``SessionDB._lock``, op ``_lock:<fn>``) as a
    "released" row, as hold_ended() does for a write transaction. Never raises (t_aeae3fe7)."""
    global _hold_seq
    try:
        with _holds_lock:
            _hold_seq += 1
            seq = _hold_seq
        now = time.time()
        hold = {"db_path": db_path, "op": op, "t0": time.monotonic() - held_s, "since": now - held_s,
                "marked": True, "ended": True, "pid": os.getpid(), "who": None, "ledger": None, "seq": seq}
        with _publish_lock:
            _write_record(hold, state="released", held_s=round(held_s, 1), released_at=now)
    except Exception:
        logger.debug("state.db lock-holder ledger: lock-hold note failed", exc_info=True)


def _pid_alive(pid: int) -> bool:
    # Never os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT (Prism 765905ac86f6).
    from hermes_cli._subprocess_compat import pid_exists_stdlib

    return pid_exists_stdlib(pid)


def recent_ledger_holders(db_path, window_s: float, *, exclude_pid: Optional[int] = None) -> List[str]:
    """Ledger lines for holds that are still open (live pid) or ended within the last *window_s*."""
    ledger = _ledger_dir(db_path)
    if os.path.islink(ledger):
        return []
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
            with open(os.path.join(ledger, name), encoding="utf-8-sig") as handle:
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


def last_timeout_holder(db_path) -> Optional[str]:
    """Holder named by this process's most recent write-lock deadline (last 10 min) on *db_path* only:
    a multiplexed process serves several profiles' stores, and another store's holder must never
    reach this one's chat (Prism eb32d69446e1)."""
    entry = _last_timeout.get(os.path.realpath(os.fspath(db_path)))
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
