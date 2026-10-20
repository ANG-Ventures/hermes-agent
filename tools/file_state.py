"""Cross-agent file state coordination.

Prevents mangled edits when concurrent subagents (same filesystem; same
process, or detached delegation workers in other processes) touch the same file: B writes a file A already read, so A's next
write would clobber B's changes. Complements the single-agent path-overlap
check in ``agent.tool_dispatch_helpers._should_parallelize_tool_batch``. A process-wide
``FileStateRegistry`` tracks per-agent read stamps, the global last writer and
a per-path lock; every method is a no-op under ``HERMES_DISABLE_FILE_STATE_GUARD=1``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# (mtime, read_ts, partial). partial=True when read_file returned a windowed
# view (offset > 1 or limit < total_lines) — a later write should still warn
# so the model re-reads in full.
ReadStamp = Tuple[float, float, bool]

# Bounded so long sessions don't accumulate unbounded state.
_MAX_PATHS_PER_AGENT = 4096
_MAX_GLOBAL_WRITERS = 4096


def _disabled() -> bool:
    # Re-read each call so tests can toggle via monkeypatch.setenv.
    return os.environ.get("HERMES_DISABLE_FILE_STATE_GUARD", "").strip() == "1"


def guard_disabled() -> bool:
    """True when the user switched the read-before-write guard off; the file
    tools then warn instead of refusing stale/unread write_file overwrites."""
    return _disabled()


def _mtime_or_none(resolved: str) -> Optional[float]:
    try:
        return os.path.getmtime(resolved)
    except OSError:
        return None


def _fmt_ts(ts: float) -> str:
    # Short wall-clock for warnings; avoids datetime formatting on the hot path.
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _evict_oldest(container, cap: int) -> None:
    """Pop entries until *container* is within *cap* (sets: arbitrary; dicts: oldest
    by insertion order). An eviction only costs one redundant re-send or staleness check."""
    for _ in range(len(container) - cap):
        try:
            if isinstance(container, set):
                container.pop()
            else:
                container.pop(next(iter(container)))
        except (StopIteration, KeyError):
            break


def _lock_file_for(resolved: str) -> Optional[Path]:
    """Lock file shared by every process of this install (all profiles, like the process-wide
    registry): ``<hermes root>/state/file-locks/<sha256 of the path>``."""
    import hashlib

    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root() / "state" / "file-locks"
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return root / hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()


@contextmanager
def _cross_process_lock(resolved: str):
    """Exclusive ``flock`` on the path's lock file; a no-op where it cannot be taken (no fcntl on
    Windows, where detached units run in-process anyway; unwritable state dir)."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    lock_file = _lock_file_for(resolved)
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600) if lock_file else None
    except OSError:
        fd = None
    if fd is None:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass  # flock unsupported on this filesystem (ENOLCK/EOPNOTSUPP): the thread lock still holds
        yield
    finally:
        os.close(fd)  # closing the only descriptor releases the flock


# ── cross-process last writer ──────────────────────────────────────────────
# Detached delegation workers (tools/delegate_detached.py) run sibling subagents in other processes,
# so the process-local ``_last_writer`` alone would stop refusing a stale sibling overwrite. Every write
# is also published as ``<lock file>.writer`` = {task_id, ts, pid, create_time}; a record from ANOTHER
# process counts only while that process still runs (a finished worker is not a concurrent sibling).
_PROC_START: Optional[float] = None


def _own_start_time() -> Optional[float]:
    global _PROC_START
    if _PROC_START is None:
        try:
            import psutil

            _PROC_START = float(psutil.Process(os.getpid()).create_time())
        except Exception:
            return None
    return _PROC_START


def _publish_writer(resolved: str, task_id: str, ts: float) -> None:
    lock_file = _lock_file_for(resolved)
    if lock_file is None:
        return
    path = lock_file.with_name(lock_file.name + ".writer")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps({"task_id": task_id, "ts": ts, "pid": os.getpid(),
                                   "create_time": _own_start_time()}), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass


def _foreign_writer(resolved: str) -> Optional[Tuple[str, float]]:
    """The last writer of *resolved* recorded by another, still-running process; else None."""
    lock_file = _lock_file_for(resolved)
    if lock_file is None:
        return None
    try:
        data = json.loads(lock_file.with_name(lock_file.name + ".writer").read_text(encoding="utf-8"))
        pid, task_id, ts = int(data["pid"]), str(data["task_id"]), float(data["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid == os.getpid():
        return None  # this process's own writes are tracked (and forgotten) in memory
    create_time = data.get("create_time")
    try:
        import psutil

        alive = create_time is None or abs(psutil.Process(pid).create_time() - float(create_time)) < 0.01
    except Exception as exc:  # noqa: BLE001
        alive = type(exc).__name__ not in {"NoSuchProcess", "ZombieProcess"}  # unknown counts as alive
    return (task_id, ts) if alive else None


def _forget_published(resolved: str, task_id: str) -> None:
    lock_file = _lock_file_for(resolved)
    if lock_file is None:
        return
    path = lock_file.with_name(lock_file.name + ".writer")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid() and data.get("task_id") == task_id:
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _latest(local: Optional[Tuple[str, float]], foreign: Optional[Tuple[str, float]]):
    if local is None or (foreign is not None and foreign[1] > local[1]):
        return foreign
    return local


class FileStateRegistry:
    """Process-wide coordinator for cross-agent file edits."""

    def __init__(self) -> None:
        self._reads: Dict[str, Dict[str, ReadStamp]] = defaultdict(dict)
        self._last_writer: Dict[str, Tuple[str, float]] = {}
        self._path_locks: Dict[str, threading.Lock] = {}
        self._path_lock_users: Dict[str, int] = {}
        self._meta_lock = threading.Lock()  # guards _path_locks
        self._state_lock = threading.Lock()  # guards _reads + _last_writer

    @contextmanager
    def lock_path(self, resolved: str):
        """Per-path lock: threads on the same path serialize, different paths proceed.
        The lock entry is dropped once the last holder/waiter exits. Inside it, an advisory
        ``flock`` serializes the same path across processes (detached delegation workers,
        tools/delegate_detached.py): the thread lock is taken first, so one process never
        holds two descriptors on one lock file."""
        with self._meta_lock:
            lock = self._path_locks.setdefault(resolved, threading.Lock())
            self._path_lock_users[resolved] = self._path_lock_users.get(resolved, 0) + 1
        lock.acquire()
        try:
            with _cross_process_lock(resolved):
                yield
        finally:
            lock.release()
            with self._meta_lock:
                users = self._path_lock_users[resolved] - 1
                if users:
                    self._path_lock_users[resolved] = users
                else:
                    self._path_lock_users.pop(resolved, None)
                    self._path_locks.pop(resolved, None)

    def _stamp(self, task_id: str, resolved: str, mtime: float, now: float, partial: bool) -> None:
        """Caller holds ``_state_lock``."""
        agent_reads = self._reads[task_id]
        agent_reads[resolved] = (float(mtime), now, bool(partial))
        _evict_oldest(agent_reads, _MAX_PATHS_PER_AGENT)

    def record_read(self, task_id: str, resolved: str, *, partial: bool = False,
                    mtime: Optional[float] = None) -> None:
        if _disabled():
            return
        mtime = _mtime_or_none(resolved) if mtime is None else mtime
        if mtime is None:
            return
        with self._state_lock:
            self._stamp(task_id, resolved, mtime, time.time(), partial)

    def note_write(self, task_id: str, resolved: str, *, mtime: Optional[float] = None) -> None:
        """Record a successful write: global last-writer AND this agent's own
        read stamp (a write is an implicit read of the current content)."""
        if _disabled():
            return
        mtime = _mtime_or_none(resolved) if mtime is None else mtime
        if mtime is None:
            return
        now = time.time()
        with self._state_lock:
            self._last_writer[resolved] = (task_id, now)
            _evict_oldest(self._last_writer, _MAX_GLOBAL_WRITERS)
            self._stamp(task_id, resolved, mtime, now, False)
        _publish_writer(resolved, task_id, now)

    @staticmethod
    def _sibling_conflict(task_id: str, resolved: str, stamp, last_writer) -> Optional[str]:
        """Case 1 of :meth:`check_stale`: a sibling subagent wrote *resolved* after this
        agent's last read (or this agent never read it). None otherwise."""
        if last_writer is None:
            return None
        writer_tid, writer_ts = last_writer
        if writer_tid == task_id:
            return None
        if stamp is None:
            return (
                f"{resolved} was modified by sibling subagent "
                f"{writer_tid!r} but this agent never read it. "
                "Read the file before writing to avoid overwriting "
                "the sibling's changes."
            )
        read_ts = stamp[1]
        if writer_ts > read_ts:
            return (
                f"{resolved} was modified by sibling subagent "
                f"{writer_tid!r} at {_fmt_ts(writer_ts)} — after "
                f"this agent's last read at {_fmt_ts(read_ts)}. "
                "Re-read the file before writing."
            )
        return None

    def sibling_write_conflict(self, task_id: str, resolved: str) -> Optional[str]:
        """The sibling-subagent staleness message, or None. Card t_f4377203: writes REFUSE on
        this (it used to be a warning attached after the clobber). An absent file is never a
        conflict (the write creates it)."""
        if _disabled():
            return None
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
            last_writer = self._last_writer.get(resolved)
        last_writer = _latest(last_writer, _foreign_writer(resolved))
        if last_writer is None or not os.path.exists(resolved):
            return None
        return self._sibling_conflict(task_id, resolved, stamp, last_writer)

    def check_stale(self, task_id: str, resolved: str) -> Optional[str]:
        """Model-facing warning if this write would be stale, else ``None``. Severity
        order: sibling wrote after our read > mtime drift / partial read > never read."""
        if _disabled():
            return None
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
            last_writer = self._last_writer.get(resolved)
        last_writer = _latest(last_writer, _foreign_writer(resolved))

        if stamp is None and last_writer is None:  # net-new file / first touch
            return None
        current_mtime = _mtime_or_none(resolved)
        if current_mtime is None:
            return None  # file doesn't exist — write creates it; not stale

        # Case 1: sibling subagent modified after our last read.
        sibling = self._sibling_conflict(task_id, resolved, stamp, last_writer)
        if sibling:
            return sibling

        if stamp is not None:
            read_mtime, _read_ts, partial = stamp
            if current_mtime != read_mtime:
                return (
                    f"{resolved} was modified since you last read it "
                    "on disk (external edit or unrecorded writer). "
                    "Re-read the file before writing.")
            if partial:
                return (
                    f"{resolved} was last read with offset/limit pagination "
                    "(partial view). Read the remaining pages, or use patch, "
                    "before overwriting it.")
            return None

        return (
            f"{resolved} was not read by this agent. "
            "Read the file first so you can write an informed edit.")

    def writes_since(self, exclude_task_id: str, since_ts: float,
                     paths: Iterable[str]) -> Dict[str, List[str]]:
        """``{writer_task_id: [paths]}`` for writes after ``since_ts`` by agents
        other than ``exclude_task_id`` (delegate_task's "subagent modified files
        you previously read" reminder)."""
        if _disabled():
            return {}
        paths_set = set(paths)
        out: Dict[str, List[str]] = defaultdict(list)
        with self._state_lock:
            for p, (writer_tid, ts) in self._last_writer.items():
                if writer_tid != exclude_task_id and ts >= since_ts and p in paths_set:
                    out[writer_tid].append(p)
        return dict(out)

    def known_reads(self, task_id: str) -> List[str]:
        """Resolved paths this agent has read."""
        if _disabled():
            return []
        with self._state_lock:
            return list(self._reads.get(task_id, {}).keys())

    def forget_task(self, task_id: str) -> None:
        """Release read stamps and writer claims owned by a task after its lifecycle ends.

        A finished task is not a concurrent sibling: leaving its writer claims behind makes
        the next run of the same job (a fresh ``cron:<job>:<uuid>`` id) refuse to write the
        same scratch path as "modified by sibling subagent" hours after the writer exited."""
        with self._state_lock:
            self._reads.pop(task_id, None)
            owned = [p for p, (writer_tid, _ts) in self._last_writer.items() if writer_tid == task_id]
            for p in owned:
                del self._last_writer[p]
        for p in owned:
            # Under the path lock writers publish under, so a sibling's record published between the
            # ownership check and the unlink is never the one removed.
            with self.lock_path(p):
                _forget_published(p, task_id)

    def clear(self) -> None:
        """Reset all state. Intended for tests only."""
        with self._state_lock:
            self._reads.clear()
            self._last_writer.clear()
        with self._meta_lock:
            self._path_locks.clear()
            self._path_lock_users.clear()


_registry = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _registry


# Convenience wrappers (short names used at call sites).
def record_read(task_id: str, resolved_or_path: str | Path, *, partial: bool = False) -> None:
    _registry.record_read(task_id, str(resolved_or_path), partial=partial)


def note_write(task_id: str, resolved_or_path: str | Path) -> None:
    _registry.note_write(task_id, str(resolved_or_path))


def check_stale(task_id: str, resolved_or_path: str | Path) -> Optional[str]:
    return _registry.check_stale(task_id, str(resolved_or_path))


def sibling_write_conflict(task_id: str, resolved_or_path: str | Path) -> Optional[str]:
    return _registry.sibling_write_conflict(task_id, str(resolved_or_path))


def lock_path(resolved_or_path: str | Path):
    return _registry.lock_path(str(resolved_or_path))


def writes_since(exclude_task_id: str, since_ts: float, paths: Iterable[str | Path]) -> Dict[str, List[str]]:
    return _registry.writes_since(exclude_task_id, since_ts, [str(p) for p in paths])


def known_reads(task_id: str) -> List[str]:
    return _registry.known_reads(task_id)


__all__ = [
    "FileStateRegistry",
    "get_registry",
    "record_read",
    "note_write",
    "check_stale",
    "lock_path",
    "writes_since",
    "known_reads"]
