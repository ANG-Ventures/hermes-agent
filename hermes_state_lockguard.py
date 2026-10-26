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

Ownership model: one private fd per guarded inode in this process, handles counted per inode.
Closing any descriptor of a file cancels every POSIX lock this process holds on it (§2.2 again): a
sibling connection's WAL write lock at ``-shm`` byte 120 mid-transaction, or the locks of a handle
that connected a moment ago and has not counted itself yet. So an fd on an inode that is still AT
its path is never closed: it is parked once its count reaches zero and re-used by the next handle.
Only an inode the path no longer names (a ``-shm`` unlinked by a true last close, a generation a
foreign checkpoint replaced) is closed, by :func:`sweep_retired` after the handle's own connection
has closed and with nobody counting it: no connection in this process can still be opening that
inode (a fresh ``sqlite3.connect`` resolves the path, hence the new inode), and a parked fd on an
unlinked ``-shm`` would otherwise read as a deleted-generation holder to
``refuse_deleted_wal_generation``'s fd scan and refuse every later open. The LAST handle on an
inode swaps the OFD lock back to SQLite's own POSIX lock on the same range and unlocks the OFD
copy, so the connection is never unlocked while still open and its own close still ends the
generation normally.

Known narrowing (t_2aad1d42): a raw ``sqlite3.connect(state.db)`` reader elsewhere in the process
(backup, open_db, kanban readers) was previously locked INCIDENTALLY by the old fd-table scan when
some SessionDB called hold(). With per-instance fds it is covered only while a SessionDB writer is
alive in the process, which in the gateway is always. Explicit coverage for SessionDB-less
processes is a follow-up card (t_8f3bf459), not this module.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import threading
from typing import Dict, Optional, Tuple

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
_FDS: Dict[Identity, Tuple[int, str]] = {}
_HANDLES: Dict[Identity, int] = {}


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
                entry = _FDS[ident] = (os.open(path, os.O_RDONLY), path)
            except OSError:
                return
        fd = entry[0]
        # Refused only while a foreign close-time EXCLUSIVE is in flight; the fd stays parked for
        # the next hold() (an OFD re-lock on an already-locked description is idempotent). It is
        # never closed here: live connections may already hold POSIX locks on this inode.
        if _ofd_lock(fd, _F_RDLCK, start, length):
            held[ident] = rng
            _HANDLES[ident] = _HANDLES.get(ident, 0) + 1


def hold(db_path, held: Optional[Held] = None) -> Held:
    """Guard ``state.db`` and its ``-shm`` for one SessionDB handle. Returns the record
    :func:`release` needs; pass it back for a reopened connection. O(1) in the process's fd count:
    one ``stat`` per file, an ``open`` only for an inode this process has never guarded."""
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


def release(held: Held) -> None:
    """Drop this handle's claim. The last handle on an inode swaps the OFD lock back for the POSIX
    lock SQLite believes it still holds (a stray close may have cancelled the real one) and unlocks
    the OFD range; the fd stays parked (see :func:`sweep_retired`). Call BEFORE the handle's own close so SQLite's close-time
    reset sees only real holders: a sibling's intact locks still refuse the unlink, and a true last
    close ends the generation (SQLite's own unlock also drops the swapped-in POSIX lock, locks being
    per process), so a later ``state.db`` replace never pairs with a stale WAL."""
    if not supported() or not held:
        return
    with _LOCK:
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


def sweep_retired(db_path) -> None:
    """Close the parked fds of ``db_path``'s retired inodes: opened at this db's path or its
    ``-shm``, counted by nobody, and no longer what the path names. Call AFTER the handle's own
    connection has closed (its true last close is what unlinks ``-shm``). See the module docstring
    for why an inode still at its path is never closed."""
    if not supported():
        return
    base = os.fspath(db_path)
    with _LOCK:
        for ident, (fd, path) in list(_FDS.items()):
            if path not in (base, _shm_path(base)) or _HANDLES.get(ident) or _identity(path) == ident:
                continue
            del _FDS[ident]
            try:
                os.close(fd)
            except OSError:
                pass
