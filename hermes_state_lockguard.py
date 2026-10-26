"""Hold a state.db writer's WAL-mode file locks in a form a stray ``close()`` cannot cancel.

SQLite protects a live WAL generation with two POSIX advisory locks: a SHARED lock on the main
file's lock range and a shared lock on the DMS byte of ``state.db-shm``. A sibling process may
checkpoint and unlink ``-wal``/``-shm`` at its close only after taking both EXCLUSIVE. POSIX locks
are per process, so any ``open()``/``close()`` of those two files inside the holder — a raw probe,
a plugin, a tool reading ``~/.hermes`` — cancels both (sqlite.org/howtocorrupt.html §2.2) and the
next foreign close strands the holder on a deleted generation (``DeletedWalGenerationError``).

This module takes the same two ranges as *open file description* locks (``F_OFD_SETLK``) on
descriptors it opens itself: ONE private ``O_RDONLY`` fd per guarded inode in this process, found
by a single ``stat`` of the path (never by enumerating the fd table: that scan ran under
``SessionDB._lock`` and cost 16-28 s under GIL load, t_2aad1d42). OFD locks belong to the
description, not the process, and conflict by inode range, not by descriptor identity: a read lock
on ANY description of the inode refuses a foreign EXCLUSIVE exactly like SQLite's own, and a stray
``close()`` elsewhere cannot cancel it. No-op on Windows and on runtimes without OFD locks.

Ownership model (ruled, t_2aad1d42 round 3): one private fd per guarded inode in this process,
handles counted per inode, and the fd is CLOSED when its count reaches zero; never parked past
zero. A parked fd on a ``-shm`` that a foreign last close later unlinks is exactly a
deleted-generation holder to ``refuse_deleted_wal_generation``, and a parked fd on ``state.db``
makes a process that has closed every SessionDB a foreign holder to the repair preflight
(``hermes_state_holders``); in the gateway no handle ever closes, so either would strand every
later opener in every process. Closing at zero is safe against the §2.2 trap (closing any
descriptor of a file cancels every POSIX lock this process holds on it): a counted handle is a
live SessionDB connection, so at zero no SessionDB in this process has a connection on that inode
(``hold()`` runs in ``_open_writer`` before the connection's first write, so a connection cannot
write before it is counted; raw readers are the narrowing below). The close itself runs just AFTER
the releasing handle's own connection has closed (``release()`` hands the inodes to
``close_fds()``), because closing before it would cancel the POSIX lock swapped in for that very
connection; ``close_fds()`` re-checks the count under the lock, so a handle that counted the inode
in between keeps the fd and closes it on its own release. An fd whose lock was refused is never
counted and never closed in ``_arm``; it stays until a later ``hold()`` counts it or a release on
that db finds it at zero.

Known narrowing (t_2aad1d42): a raw ``sqlite3.connect(state.db)`` reader elsewhere in the process
(backup, open_db, kanban readers) was previously locked INCIDENTALLY by the old fd-table scan; it is
now covered only while a SessionDB writer is alive in the process (in the gateway: always). Explicit
coverage for SessionDB-less processes is follow-up card t_8f3bf459, not this module.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("hermes_state")

# SQLite's unix VFS lock geometry (os_unix.c): the SHARED range on the main file and the
# deadman-switch byte of the -shm file.
_PENDING_BYTE = 0x40000000
_SHARED_FIRST = _PENDING_BYTE + 2
_SHARED_SIZE = 510
_SHM_DMS_BYTE = 128

# Windows has no POSIX advisory locks, and the module promises to be a no-op there. Gate on the
# platform FIRST: some Windows installs have a third-party module importable as `fcntl` (stock
# CPython for Windows ships none), and letting the import decide would arm the guard on a
# lookalike. Off Windows a partial module is equally fatal at import time — every importer of
# hermes_state dies before supported() can say "no" (#118026) — so tolerate a missing attribute
# the same way as a missing module and fall back to the no-op.
if os.name == "nt":
    fcntl = None  # type: ignore[assignment]
    _F_OFD_SETLK: Optional[int] = None
    _F_RDLCK = _F_UNLCK = _SEEK_SET = _F_SETLK = 0
else:
    try:
        import fcntl
        # CPython exports F_OFD_SETLK only from 3.12. The kernel ABI values are stable: 37 on every
        # Linux arch (asm-generic/fcntl.h), 90 on XNU (bsd/sys/fcntl.h, documented in fcntl(2)).
        _F_OFD_SETLK = getattr(
            fcntl, "F_OFD_SETLK", {"linux": 37, "darwin": 90}.get(sys.platform.rstrip("0123456789")))
        _F_RDLCK, _F_UNLCK, _SEEK_SET = fcntl.F_RDLCK, fcntl.F_UNLCK, os.SEEK_SET
        _F_SETLK = fcntl.F_SETLK
        # The constants alone do not make the guard usable: _ofd_lock() calls fcntl.fcntl(), so a
        # module that has the constants but no callable would pass supported() and then raise
        # from the first hold(). Probe the whole capability here, not just the symbols.
        if not callable(getattr(fcntl, "fcntl", None)):
            raise ImportError("fcntl module has no fcntl() callable")
    except (ImportError, AttributeError):
        fcntl = None  # type: ignore[assignment]
        _F_OFD_SETLK = None
        _F_RDLCK = _F_UNLCK = _SEEK_SET = _F_SETLK = 0

# struct flock differs per libc: glibc/musl put type+whence first, Darwin/BSD last.
_FLOCK_FORMAT = "@qqihh" if sys.platform == "darwin" or "bsd" in sys.platform else "@hhqqi"

Identity = Tuple[int, int]
Held = Dict[Identity, Tuple[int, int]]  # inode this handle guards -> its (start, length) range

# Process-wide, tiny: the private fd per guarded inode (+ the path it was opened at) and the number
# of live handles on it.
_LOCK = threading.Lock()


def _no_live_connection(path: str) -> bool:
    """No tracked connection (hermes_cli.sqlite_safe_read: every SessionDB writer, pool reader and
    checkpoint connection) to *path*'s database. This, not the handle count, gates a close: the
    count never saw pool readers, the checkpoint connection or an initialising writer (Prism
    12297a6faa04, f4694ff962e4, 26c5daf67097, 81153d4ceb9f). Fails CLOSED without the registry."""
    try:
        from hermes_cli.sqlite_safe_read import has_live_connection
    except ImportError:
        return False
    try:
        return not has_live_connection(path)
    except Exception:
        return False
_FDS: Dict[Identity, Tuple[int, str]] = {}
_HANDLES: Dict[Identity, int] = {}
_DUPS: Dict[Identity, List[int]] = {}  # path-move duplicates, closed with their inode


def supported() -> bool:
    return _F_OFD_SETLK is not None


def _flock(lock_type: int, start: int, length: int) -> bytes:
    if _FLOCK_FORMAT == "@qqihh":
        return struct.pack(_FLOCK_FORMAT, start, length, 0, lock_type, _SEEK_SET)
    return struct.pack(_FLOCK_FORMAT, lock_type, _SEEK_SET, start, length, 0)


def _ofd_lock(fd: int, lock_type: int, start: int, length: int, *, cmd: Optional[int] = None) -> bool:
    """Apply a non-blocking OFD lock (a process-owned POSIX one with ``cmd=_F_SETLK``); False when
    the range is held EXCLUSIVE elsewhere."""
    assert fcntl is not None and _F_OFD_SETLK is not None
    try:
        fcntl.fcntl(fd, _F_OFD_SETLK if cmd is None else cmd, _flock(lock_type, start, length))
    except BlockingIOError:
        return False
    return True


def _identity(path: str) -> Optional[Identity]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _shm_path(db_path) -> str:
    return os.fspath(db_path) + "-shm"


def _arm(path: str, rng: Tuple[int, int], held: Held) -> None:
    """Count *held* onto the inode at *path*, opening the process's private fd for it on first use.
    One ``stat``; an inode this handle already guards costs nothing more. Never raises."""
    ident = _identity(path)
    if ident is None or ident in held:
        return
    start, length = rng
    with _LOCK:
        entry = _FDS.get(ident)
        if entry is None:
            try:
                fd = os.open(path, os.O_RDONLY)
                st = os.fstat(fd)  # key by what was actually opened: the path may have moved on
            except OSError:
                return
            ident = (st.st_dev, st.st_ino)
            if ident in _FDS:  # path moved onto a registered inode: keep the duplicate, close it WITH the inode
                _DUPS.setdefault(ident, []).append(fd)
            entry = _FDS.setdefault(ident, (fd, path))
            if ident in held:
                return
        if _ofd_lock(entry[0], _F_RDLCK, start, length):
            held[ident] = rng
            _HANDLES[ident] = _HANDLES.get(ident, 0) + 1
        # Refused (foreign EXCLUSIVE, or this process's own POSIX lock): never closed here, a close
        # would cancel our own SQLite locks. Stays uncounted for the next hold() or the retired sweep.


def hold(db_path, held: Optional[Held] = None) -> Held:
    """Guard ``state.db`` and its ``-shm`` for one SessionDB handle. Returns the record
    :func:`release` needs; pass it back for a reopened connection. O(1) in the process's fd count:
    one ``stat`` per file, an ``open`` only for an inode no handle in this process guards."""
    held = {} if held is None else held
    if not supported():
        return held
    base = os.fspath(db_path)
    _arm(base, (_SHARED_FIRST, _SHARED_SIZE), held)
    _arm(_shm_path(base), (_SHM_DMS_BYTE, 1), held)
    return held


def refresh_shm(db_path, held: Held) -> None:
    """Checkpoint-time refresh: arm a ``-shm`` minted or replaced since open (a foreign checkpoint
    ended the generation). One ``stat`` of ``-shm`` only; an unchanged inode costs nothing else, and
    ``state.db`` itself is never re-armed here (a replaced main file is _raise_if_db_replaced's
    business, not a new file to lock). The old ``-shm`` entry stays counted until release()."""
    if supported() and held:
        _arm(_shm_path(db_path), (_SHM_DMS_BYTE, 1), held)
        with _LOCK:
            _sweep_retired_locked()


def sweep_retired() -> None:
    """Open-path sweep before ``refuse_deleted_wal_generation``: our uncounted fd on an unlinked inode is not a live holder."""
    if supported():
        with _LOCK:
            _sweep_retired_locked()


def _sweep_retired_locked() -> None:
    """Close every fd that NO handle counts and whose path no longer names its inode. Safe without a
    connection-level check: SQLite holds the -shm DMS byte SHARED for a connection's whole life, so a
    foreign close-time reset that unlinked the inode was granted EXCLUSIVE only while no connection
    in this process had it open, and no new one can reach an unlinked inode by path. Runs from the
    checkpoint refresh as well as release(), because a long-lived gateway never releases: a parked
    fd on an unlinked -shm would read as a deleted-generation holder and refuse every later opener
    in every process (Prism 975630a37100 / 5fa2be0eea10). O(len(_FDS)) stats, a handful."""
    for ident, (fd, path) in list(_FDS.items()):
        if _HANDLES.get(ident) or _identity(path) == ident:
            continue
        del _FDS[ident]
        _close_all(fd, ident)


def release(db_path, held: Held) -> List[Identity]:
    """Drop this handle's claim. The last handle on an inode swaps the OFD lock back for the POSIX
    lock SQLite believes it still holds (a stray close may have cancelled the real one) and unlocks
    the OFD range. Call BEFORE the handle's own close so SQLite's close-time reset sees only real
    holders: a sibling's intact locks still refuse the unlink, and a true last close ends the
    generation, so a later ``state.db`` replace never pairs with a stale WAL.

    Returns the inodes to close AFTER the connection closes (:func:`close_fds`): this handle's own
    that reached count zero, matched by inode not by path string (a handle opened under another
    spelling of the file counts onto the same inode, Prism 49110a024c0e), plus any uncounted inode
    the path no longer names (retired). An uncounted fd still AT its path (a refused arm) is left:
    a live sibling may hold SQLite locks on it that a close would drop."""
    if not supported():
        return []
    with _LOCK:
        mine = list(held)
        for ident, (start, length) in held.items():
            remaining = _HANDLES.get(ident, 1) - 1
            if remaining > 0:
                _HANDLES[ident] = remaining
                continue
            _HANDLES.pop(ident, None)
            entry = _FDS.get(ident)
            if entry is None:
                continue
            try:
                _ofd_lock(entry[0], _F_RDLCK, start, length, cmd=_F_SETLK)  # never refused: our OFD lock excludes writers
                _ofd_lock(entry[0], _F_UNLCK, start, length)
            except OSError:
                pass
        held.clear()
        return [ident for ident in mine if ident in _FDS and not _HANDLES.get(ident)] + [
            ident for ident, (_, path) in _FDS.items()
            if ident not in mine and not _HANDLES.get(ident) and _identity(path) != ident]


def close_fds(idents: List[Identity]) -> None:
    """Second half of :func:`release`, after the releasing connection closed. An fd closes only when
    no handle counts its inode AND (the path no longer names it, or no tracked connection to the
    database is live). Otherwise it stays parked for the next hold(), release or retired sweep."""
    with _LOCK:
        for ident in idents:
            if _HANDLES.get(ident):
                continue
            entry = _FDS.get(ident)
            if entry is None:
                continue
            fd, path = entry
            retired = _identity(path) != ident
            if not retired and not _no_live_connection(_db_path_of(path)):
                continue
            del _FDS[ident]
            _close_all(fd, ident)


def uncounted(db_path) -> List[Identity]:
    """Uncounted inodes opened at *db_path* or its ``-shm``: :func:`close_fds` candidates for a late reader's close."""
    if not supported():
        return []
    paths = (os.fspath(db_path), _shm_path(db_path))
    with _LOCK:
        return [ident for ident, (_, path) in _FDS.items() if path in paths and not _HANDLES.get(ident)]


def _db_path_of(path: str) -> str:
    return path[:-4] if path.endswith("-shm") else path


def _close_all(fd: int, ident: Identity) -> None:
    """Close an inode's private fd and its duplicates. Caller holds _LOCK; no handle counts the inode."""
    for each in [fd] + _DUPS.pop(ident, []):
        try:
            os.close(each)
        except OSError:
            pass
