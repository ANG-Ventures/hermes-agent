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
import sys
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


#: Set by a process that IS a cross-process delegation unit (the kanban-runtime runner,
#: tools/delegation_unit_worker.py): its siblings are other processes whatever the knobs say now.
FORCE_CROSS_PROCESS = False


def _cross_process_enabled() -> bool:
    """The cross-process layer (flock + published last writer) exists only for delegation units that run
    in other processes: detached workers (``delegation.detached_background``) and kanban-runtime units
    (``delegation.background_runtime: kanban``). Off, every process keeps its process-local behaviour:
    no flock, and another process's write never refuses this one's edit."""
    if FORCE_CROSS_PROCESS:
        return True
    try:
        from tools.delegate_tool_config import _get_background_runtime, _get_detached_background

        if bool(_get_detached_background()) or _get_background_runtime() == "kanban":
            return True
    except Exception:
        pass
    # Kanban units that outlived a knob flip (a gateway restarted with ``in_process``) still edit
    # files in their own processes: this process keeps coordinating while any of them is live.
    ad = sys.modules.get("tools.async_delegation")
    try:
        return bool(ad is not None and ad.kanban_active_count())
    except Exception:
        return False


def _lock_roots() -> List[Path]:
    """Lock directories, primary first: ``<kanban root>/state/file-locks`` (the kanban root is the install
    root unless ``HERMES_KANBAN_HOME`` pins it, which the dispatcher does for every kanban-runtime unit so a
    runner under another profile home derives the gateway's directory, spec §8 row 10), then the install
    root's own directory when it differs. A detached worker (or a process from before the kanban root
    took over) coordinates through the install root only; taking and publishing in BOTH keeps every
    generation of sibling in one lock domain (Prism a2f98fb174ac)."""
    roots: List[Path] = []
    try:
        from hermes_cli.kanban_db import kanban_home

        roots.append(kanban_home() / "state" / "file-locks")
    except Exception:
        pass
    try:
        from hermes_constants import get_default_hermes_root

        roots.append(get_default_hermes_root() / "state" / "file-locks")
    except Exception:
        pass
    # Two spellings of ONE directory (a symlink, a bind mount) would open the same lock inode twice, and
    # the second LOCK_EX would wait on the first forever: keep one root per filesystem identity
    # (Prism 66442f47f3d4 / 17c51e65b828).
    out: List[Path] = []
    seen = set()
    for root in roots:
        ident = _dir_identity(root)
        if ident not in seen:
            seen.add(ident)
            out.append(root)
    return out


def _dir_identity(root: Path):
    """``(st_dev, st_ino)`` of *root* (created if missing), else its realpath."""
    real = os.path.realpath(str(root))
    try:
        os.makedirs(real, exist_ok=True)
        st = os.stat(real)
        return (st.st_dev, st.st_ino)
    except OSError:
        return real


def _lock_files_for(resolved: str) -> List[Path]:
    """Every lock file of *resolved* (see ``_lock_roots``), primary first; [] while the layer is off or
    no lock directory can be created."""
    import hashlib

    if not _cross_process_enabled():
        return []
    name = hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()
    out = []
    for root in _lock_roots():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        out.append(root / name)
    return out


def _lock_file_for(resolved: str) -> Optional[Path]:
    """The primary lock file shared by every process of this install (all profiles, like the
    process-wide registry): ``<kanban root>/state/file-locks/<sha256 of the path>``. None while the
    cross-process layer is off."""
    files = _lock_files_for(resolved)
    return files[0] if files else None


def cross_process_locks_available() -> bool:
    """A working exclusive ``flock`` can be taken in every lock directory (``_lock_roots``). A kanban-runtime
    unit edits files from another process, so without one its edits and a sibling's would interleave
    unserialized: such a unit must stay in-process (Prism 7bf4070bc0f0). Never raises."""
    try:
        import fcntl
    except ImportError:
        return False
    roots = _lock_roots()
    if not roots:
        return False
    for root in roots:
        try:
            root.mkdir(parents=True, exist_ok=True)
            fd = os.open(root / ".probe", os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            pass  # another process holds the probe lock: flock works here
        except OSError:
            return False  # ENOLCK/EOPNOTSUPP: this filesystem has no working flock
        finally:
            os.close(fd)
    return True


@contextmanager
def _cross_process_lock(resolved: str):
    """Exclusive ``flock`` on each of the path's lock files, taken in one global (sorted) order so two
    processes never deadlock; a no-op where it cannot be taken (no fcntl on Windows, where detached
    units run in-process anyway; unwritable state dir)."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    fds = []
    try:
        for lock_file in sorted(_lock_files_for(resolved), key=lambda p: os.path.realpath(str(p))):
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError:
                continue
            fds.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                pass  # flock unsupported on this filesystem (ENOLCK/EOPNOTSUPP): the thread lock still holds
        yield
    finally:
        for fd in reversed(fds):
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
    for lock_file in _lock_files_for(resolved):
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
    """The latest writer of *resolved* recorded by another, still-running process in any lock
    directory; else None."""
    latest = None
    for lock_file in _lock_files_for(resolved):
        latest = _latest(latest, _foreign_writer_in(lock_file))
    return latest


def _foreign_writer_in(lock_file: Path) -> Optional[Tuple[str, float]]:
    try:
        data = json.loads(lock_file.with_name(lock_file.name + ".writer").read_text(encoding="utf-8-sig"))
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
    for lock_file in _lock_files_for(resolved):
        path = lock_file.with_name(lock_file.name + ".writer")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
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
        for p in owned if _cross_process_enabled() else ():
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
