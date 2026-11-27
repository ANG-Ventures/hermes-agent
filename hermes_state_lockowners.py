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

import contextlib
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


def _safe_debug(*args, **kwargs) -> None:
    """A broken logging handler must not replace a write's result or original error."""
    try:
        logger.debug(*args, **kwargs)
    except Exception:
        pass

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
# Every recorded hold (>= _HOLD_RECORD_MIN_S) is also appended to <ledger>/history.jsonl so a past
# stall window can be rebuilt after the per-pid file was overwritten (t_7fe48f0b). Bounded: rotated to
# history.jsonl.1 at _HISTORY_MAX_BYTES, one backup kept (<= 2x the cap on disk).
_HISTORY_NAME = "history.jsonl"
# Cross-process guard for rotate+append (Prism 8e1b5c4107a7): _publish_lock serializes threads of ONE
# process only, so two processes could both see a full file and the second rotation would clobber the
# generation the first just archived. Serialize every append with one nonblocking attempt;
# otherwise drop diagnostics. No unlocked growth or retry wait.
# Never matches _LEDGER_NAME_RE, so never pruned.
_HISTORY_LOCK_NAME = "history.lock"
_HISTORY_MAX_BYTES = 4 * 1024 * 1024
# A writer that has waited this long for the write lock logs the holder on its own side.
WAITER_ATTRIBUTION_S = 10.0

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
# The watchdog parks on _wake while no hold is active instead of exiting, and is started by
# ensure_watchdog() BEFORE a writer takes the write lock: Thread.start() blocks until the new thread
# is scheduled, which under GIL contention took up to 1 s per start and was paid inside BEGIN
# IMMEDIATE by every write that found the watchdog gone (t_6e1c17e7).
_wake = threading.Event()
_watchdog_start_lock = threading.Lock()
_last_timeout: Dict[str, Tuple[float, str]] = {}
# Bound at import: the watchdog is internal plumbing. Tests (and libraries) that replace
# ``threading.Thread`` with an inline starter would otherwise run the watchdog loop inside
# hold_started() while it holds _holds_lock, deadlocking the writer (the 4 tui_gateway reds on #2072).
_Thread = threading.Thread


def _reset_after_fork() -> None:
    """A fork child inherits the locks in whatever state another parent thread left them, and no
    watchdog thread: start from scratch (Prism c0c7a990a4d6)."""
    global _holds_lock, _publish_lock, _active_holds, _published_seq, _watchdog_running, _watchdog_pid
    global _wake, _watchdog_start_lock
    _holds_lock, _publish_lock = threading.Lock(), threading.Lock()
    _wake, _watchdog_start_lock = threading.Event(), threading.Lock()
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
        _safe_debug("state.db lock-holder ledger %s skipped: not a private dir owned by this user", ledger)
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
        _safe_debug("state.db lock-holder ledger write failed", exc_info=True)


def _record(hold: dict, fields: dict) -> dict:
    return {"pid": hold["pid"], "seq": hold["seq"], "who": hold["who"], "op": hold["op"], "since": hold["since"],
            **fields}


def _write_json(target: str, hold: dict, fields: dict) -> None:
    tmp = f"{target}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(_record(hold, fields), handle)
    os.replace(tmp, target)


def _select_due_holds() -> Optional[List[dict]]:
    """Mark and return holds open >= _HOLD_RECORD_MIN_S; None when no hold is active (watchdog parks)."""
    now = time.monotonic()
    with _holds_lock:
        if not _active_holds:
            _wake.clear()  # under the lock: hold_started() sets it again for the next hold
            return None
        due = [h for h in _active_holds.values() if not h["marked"] and now - h["t0"] >= _HOLD_RECORD_MIN_S]
        for hold in due:
            hold["marked"] = True
    return due


def _publish_holding(hold: dict) -> None:
    with _publish_lock:
        if not hold["ended"]:  # hold_ended() already published "released": never resurrect it
            _write_record(hold, state="holding", **_phase_fields(hold, time.monotonic()))


def _watchdog_loop() -> None:
    while True:
        _wake.wait()  # parked between holds: no thread churn, no tick while idle
        time.sleep(_WATCHDOG_TICK_S)
        try:
            for hold in _select_due_holds() or ():
                _publish_holding(hold)
        except Exception:  # the one watchdog of this process must outlive a bad tick
            _safe_debug("state.db lock-holder watchdog tick failed", exc_info=True)


def ensure_watchdog() -> None:
    """Start this process's ledger watchdog once. Call it BEFORE taking the write lock: Thread.start()
    waits for the new thread to be scheduled, which is a GIL handoff, and under contention that cost
    up to ~1 s per start on a loaded host (t_6e1c17e7). Never raises: a write without "holding" marks
    is better than no write (thread exhaustion at load 160)."""
    global _watchdog_running, _watchdog_pid
    pid = os.getpid()
    with _watchdog_start_lock:  # claim only; the start itself runs unlocked so no writer waits on it
        if _watchdog_running and _watchdog_pid == pid:
            return
        _watchdog_running, _watchdog_pid = True, pid
    try:
        _Thread(target=_watchdog_loop, name="state-db-lock-ledger", daemon=True).start()
    except Exception:  # RuntimeError("can't start new thread"): no "holding" marks; the next write retries
        with _watchdog_start_lock:
            _watchdog_running = False
        _safe_debug("state.db lock-holder watchdog did not start", exc_info=True)


def hold_started(db_path, op: str) -> Optional[int]:
    """Register a write-lock hold that just began (BEGIN IMMEDIATE succeeded); returns its token.
    Never starts a thread: the caller ran ensure_watchdog() before it took the lock. Never raises:
    a failure here must not break the write."""
    global _hold_seq
    try:
        with _holds_lock:
            _hold_seq += 1
            token = _hold_seq
            _active_holds[token] = {"db_path": db_path, "op": op, "t0": time.monotonic(), "since": time.time(),
                                    "marked": False, "ended": False, "pid": os.getpid(), "who": None,
                                    "ledger": None, "seq": token, "commit_t0": None,
                                    "tid": threading.get_ident(), "cpu0": _thread_cpu(), "callback_cpu_s": None}
            _wake.set()
        return token
    except Exception:
        _safe_debug("state.db lock-holder ledger registration failed", exc_info=True)
        return None


def hold_committing(token: Optional[int]) -> None:
    """The caller's statements are done and ``commit()`` starts now. A long hold then says whether the
    time went to the callback or to SQLite's commit (WAL sync, autocheckpoint): t_d9caaea3, where a
    trivial SELECT+UPDATE was recorded as a 212 s hold with nothing to tell the two apart."""
    if token is None:
        return
    try:
        with _holds_lock:
            hold = _active_holds.get(token)
            if hold is None:
                return
            hold["commit_t0"], hold["commit_since"] = time.monotonic(), time.time()
            hold["callback_cpu_s"] = _callback_cpu(hold)  # this IS the writer thread: thread_time() is its CPU
            republish = hold["marked"]  # already published "holding" without the phase
        if republish:
            _publish_holding(hold)
    except Exception:
        _safe_debug("state.db lock-holder commit mark failed", exc_info=True)  # never breaks the write


def _thread_cpu() -> Optional[float]:
    try:
        return time.thread_time()
    except Exception:  # never breaks the write
        return None


def _callback_cpu(hold: dict) -> Optional[float]:
    """CPU the writer thread spent since BEGIN IMMEDIATE; None off the writer thread or when unknown."""
    try:
        if hold.get("cpu0") is None or hold.get("tid") != threading.get_ident():
            return None
        now = _thread_cpu()
        return None if now is None else round(max(now - hold["cpu0"], 0.0), 3)
    except Exception:
        return None


def _phase_fields(hold: dict, now: float) -> dict:
    """``commit_since`` (wall) and ``commit_s`` (so far): absent while the callback still runs.
    Once commit() started, ``callback_s`` (wall from BEGIN to commit) and ``callback_cpu_s`` (writer
    thread CPU over the same span): callback_s far above callback_cpu_s means the thread held the lock
    without running (GIL / CPU starvation), not that its SQL was slow (t_7fe48f0b)."""
    if hold.get("commit_t0") is None:
        return {}
    return {"commit_since": hold["commit_since"], "commit_s": round(now - hold["commit_t0"], 1),
            "callback_s": round(hold["commit_t0"] - hold["t0"], 1), "callback_cpu_s": hold.get("callback_cpu_s")}


def _phase_suffix(rec: dict, now: float, *, released: bool) -> str:
    """``, Ns of it in commit`` when the record says commit() had started; empty for older records."""
    if released:
        commit_s = rec.get("commit_s")
    else:
        since = rec.get("commit_since")
        commit_s = None if since is None else now - float(since)
    if commit_s is None:
        return ""
    return f", {float(commit_s):.0f}s of it in commit"


def hold_ended(token: Optional[int]) -> None:
    """Best-effort telemetry: no diagnostic failure may escape transaction cleanup."""
    try:
        _hold_ended(token)
    except Exception:
        pass


def _hold_ended(token: Optional[int]) -> None:
    """Close a hold; one of >= _HOLD_RECORD_MIN_S is written to the ledger with its duration."""
    if token is None:
        return
    with _holds_lock:
        hold = _active_holds.pop(token, None)
        if hold is not None:
            hold["ended"] = True
    if hold is None:
        return
    now = time.monotonic()
    held = now - hold["t0"]
    if held >= _HOLD_RECORD_MIN_S or hold["marked"]:
        fields = _phase_fields(hold, now)
        if hold.get("commit_t0") is None:  # rolled back / raised before commit(): all of it was callback
            fields.update(callback_s=round(held, 1), callback_cpu_s=_callback_cpu(hold))
        with _publish_lock:
            _write_record(hold, state="released", held_s=round(held, 1), released_at=time.time(), **fields)
            _append_history(hold, dict(fields, state="released", held_s=round(held, 1),
                                       released_at=time.time()))


def _try_lock_fd(lock_fd: int) -> bool:
    """Non-blocking exclusive lock on *lock_fd* (flock on POSIX, a 1-byte msvcrt lock on Windows)."""
    try:
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except ImportError:
        import msvcrt  # windows-footgun: ok — Windows-only branch, fcntl is absent there

        os.lseek(lock_fd, 0, os.SEEK_SET)
        msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _open_history_lock(name: str, *, dir_fd: Optional[int] = None) -> Optional[int]:
    """fd of history.lock, or None when unusable. Drop diagnostics on failure: every
    append needs the rotation lock to preserve the hard byte bound."""
    try:
        if dir_fd is None:
            return os.open(name, os.O_RDWR | os.O_CREAT, 0o600)
        return os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    except OSError:
        with contextlib.suppress(Exception):
            _safe_debug("state.db lock-holder history.lock unusable; dropping record", exc_info=True)
        return None


def _acquire_history_lock(lock_fd: int) -> bool:
    """One immediate attempt. Contended diagnostics are dropped, never waited for."""
    try:
        return _try_lock_fd(lock_fd)
    except Exception:
        return False


def _append_history(hold: dict, fields: dict) -> None:
    """Append one released hold to <ledger>/history.jsonl, rotating at _HISTORY_MAX_BYTES.
    Callers hold _publish_lock (threads of this process); rotate+append also holds the cross-process
    history.lock. Busy/unusable locks and oversized records are dropped, never retried or
    appended unlocked. Never raises. Runs after
    _write_record, which resolved the ledger."""
    try:
        ledger = hold.get("ledger")
        if ledger is None:
            return
        line = (json.dumps(_record(hold, fields), sort_keys=True) + "\n").encode("utf-8")
        if not _DIR_FD_OK:
            if os.path.islink(ledger) or not os.path.isdir(ledger):
                return
            path = os.path.join(ledger, _HISTORY_NAME)
            lock_fd = _open_history_lock(os.path.join(ledger, _HISTORY_LOCK_NAME))
            try:
                if lock_fd is None or not _acquire_history_lock(lock_fd):
                    return  # drop: unlocked appends would defeat rotation and the byte bound
                if len(line) > _HISTORY_MAX_BYTES:
                    return
                with contextlib.suppress(FileNotFoundError):
                    if os.path.getsize(path) + len(line) > _HISTORY_MAX_BYTES:
                        os.replace(path, path + ".1")
                with open(path, "ab") as handle:
                    handle.write(line)
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
            return
        fd = _open_ledger(ledger, create=False)
        if fd is None:
            return
        try:
            lock_fd = _open_history_lock(_HISTORY_LOCK_NAME, dir_fd=fd)
            try:
                if lock_fd is None or not _acquire_history_lock(lock_fd):
                    return  # drop, never append without serialized size check/rotation
                if len(line) > _HISTORY_MAX_BYTES:
                    return
                with contextlib.suppress(FileNotFoundError):
                    st = os.stat(_HISTORY_NAME, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode) and st.st_size + len(line) > _HISTORY_MAX_BYTES:
                        os.replace(_HISTORY_NAME, _HISTORY_NAME + ".1", src_dir_fd=fd, dst_dir_fd=fd)
                out = os.open(_HISTORY_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600,
                              dir_fd=fd)
                try:
                    os.write(out, line)  # one O_APPEND write per record: concurrent writers never interleave lines
                finally:
                    os.close(out)
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
        finally:
            os.close(fd)
    except Exception:
        with contextlib.suppress(Exception):
            _safe_debug("state.db lock-holder history append failed", exc_info=True)


def ledger_history(db_path, start: float, end: float) -> List[dict]:
    """Recorded holds (any pid) that overlapped the wall-clock window [start, end], oldest first.
    Reads history.jsonl.1 then history.jsonl; malformed lines are skipped. Never raises."""
    out: List[dict] = []
    try:
        ledger = _ledger_dir(db_path)
    except Exception:
        return out
    if os.path.islink(ledger):
        return out
    for name in (_HISTORY_NAME + ".1", _HISTORY_NAME):
        try:
            with open(os.path.join(ledger, name), "rb") as handle:
                for raw in handle:
                    try:
                        rec = json.loads(raw.decode("utf-8"))
                        if not isinstance(rec, dict):
                            continue
                        since = float(rec["since"])
                        released = float(rec.get("released_at") or since + float(rec.get("held_s") or 0.0))
                    except (ValueError, KeyError, TypeError):
                        continue
                    if since <= end and released >= start:
                        out.append(rec)
        except OSError:
            continue
    out.sort(key=lambda rec: float(rec["since"]))
    return out


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
            phase = _phase_suffix(rec, now, released=False)
            found.append((now, f"{who} has held the write lock for {now - since:.0f}s ({op}{phase}, still holding)"))
        elif rec.get("state") == "released":
            released = float(rec.get("released_at") or 0.0)
            if now - released <= window_s:
                phase = _phase_suffix(rec, now, released=True)
                found.append((released, f"{who} held the write lock for {float(rec.get('held_s') or 0):.0f}s "
                                         f"({op}{phase}), released {now - released:.0f}s ago"))
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
        _safe_debug("state.db lock-holder ledger read failed", exc_info=True)
    if lines:
        return " | ".join(lines)
    if not sys.platform.startswith("linux"):
        return ("holder unknown: no Hermes process recorded a write-lock hold of "
                f"{_HOLD_RECORD_MIN_S:.0f}s+ in the last {window_s:.0f}s (/proc/locks is Linux-only)")
    return ("holder unknown: no write-class lock held at the deadline and no Hermes process recorded a "
            f"write-lock hold of {_HOLD_RECORD_MIN_S:.0f}s+ in the last {window_s:.0f}s")


def log_write_lock_waiter(db_path, waited_s: float, *, lock: str) -> str:
    """Waiter-side attribution: one WARNING naming the current holder once a write has waited
    WAITER_ATTRIBUTION_S for *lock* (``writer`` = this SessionDB's in-process writer lock, ``sqlite``
    = the state.db write lock). Returns the detail; never raises (t_7fe48f0b)."""
    try:
        detail = describe_write_lock_holders(db_path, waited_s + 5.0)
        logger.warning("state.db write waiting %.1fs for the %s lock (%s, pid %d): holder: %s",
                       waited_s, lock, Path(db_path).name, os.getpid(), detail)
        return detail
    except Exception:
        _safe_debug("state.db waiter-side attribution failed", exc_info=True)
        return ""


def log_write_lock_holders(db_path, patience_s: float) -> str:
    """One WARNING naming the write-lock holders at the moment a writer gave up waiting; returns the detail."""
    detail = describe_write_lock_holders(db_path, patience_s + 5.0)
    _last_timeout[os.path.realpath(os.fspath(db_path))] = (time.time(), detail)
    with contextlib.suppress(Exception):
        logger.warning(
            "state.db write lock unavailable for %.0fs (%s): %s", patience_s, Path(db_path).name, detail
        )
    return detail
