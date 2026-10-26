"""Hold a state.db writer's WAL-mode file locks in a form a stray ``close()`` cannot cancel.

SQLite protects a live WAL generation with two POSIX advisory locks: a SHARED lock on the main
file's lock range and a shared lock on the DMS byte of ``state.db-shm``. A sibling process may
checkpoint and unlink ``-wal``/``-shm`` at its close only after taking both EXCLUSIVE. POSIX locks
are per process, so any ``open()``/``close()`` of those two files inside the holder — a raw probe,
a plugin, a tool reading ``~/.hermes`` — cancels both (sqlite.org/howtocorrupt.html §2.2) and the
next foreign close strands the holder on a deleted generation (``DeletedWalGenerationError``).

This module takes the same two ranges as *open file description* locks (``F_OFD_SETLK``) on
descriptors it opens itself, one private ``O_RDONLY`` fd per guarded file per ``SessionDB``
instance. OFD locks belong to the description, not the process, and conflict by inode range, not
by descriptor identity: a read lock on ANY description of the inode refuses a foreign EXCLUSIVE
exactly like SQLite's own, and a stray ``close()`` elsewhere cannot cancel it. Read locks from N
instances on one inode are compatible and independent, so there is nothing to count, share or
retire: ``release()`` unlocks and closes only the instance's own fds. No-op on Windows and on
runtimes without OFD locks.

Close ordering (unchanged): the guard is lifted before the handle's own close so a true last close
still ends the generation normally. Lifting it re-takes SQLite's own POSIX locks on the same ranges
(on the private fd) so the connection is never unlocked while it is still open; the private fds
are closed only AFTER SQLite's connection closes, because closing any descriptor of the file would
cancel that process-owned lock (§2.2 again) inside the gap.

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
Held = Dict[Identity, Tuple[int, Tuple[int, int]]]  # guarded inode -> (private fd, (start, length))


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


def _guard_targets(db_path) -> Tuple[Tuple[str, Tuple[int, int]], ...]:
    base = os.fspath(db_path)
    return ((base, (_SHARED_FIRST, _SHARED_SIZE)), (base + "-shm", (_SHM_DMS_BYTE, 1)))


def _arm(path: str, rng: Tuple[int, int], held: Held) -> None:
    """Open a private fd on *path* and take the OFD read lock; a no-op when *held* already covers
    the file's current inode (one stat, zero opens). An entry whose inode is no longer at the path
    (a foreign checkpoint minted a new ``-shm``) is retired once the new one is armed."""
    ident = _identity(path)
    if ident is None or ident in held:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    start, length = rng
    if not _ofd_lock(fd, _F_RDLCK, start, length):
        os.close(fd)  # a foreign EXCLUSIVE is in flight; the next refresh retries
        return
    for stale in [i for i, (_, r) in held.items() if r == rng]:
        _close_quietly(_unlock(held.pop(stale)))  # the stale inode is unlinked: its POSIX locks are moot
    held[ident] = (fd, rng)


def _unlock(entry: Tuple[int, Tuple[int, int]]) -> int:
    """Swap the OFD lock back for the POSIX lock SQLite believes it still holds (a stray close may
    have cancelled the real one), then drop the OFD range. Returns the private fd, which the caller
    closes only after SQLite's connection has closed: unlocked outright, or closed now (any close
    cancels the process's POSIX locks on the file), the still-open connection would let a sibling's
    close take EXCLUSIVE and unlink ``-wal``/``-shm`` under it before its own close runs. SQLite's
    close upgrades or drops that process-owned lock itself, so a true last close is unaffected."""
    fd, (start, length) = entry
    try:
        _ofd_lock(fd, _F_RDLCK, start, length, cmd=_F_SETLK)  # never refused: our OFD lock excludes writers
        _ofd_lock(fd, _F_UNLCK, start, length)
    except OSError:
        pass
    return fd


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def hold(db_path, held: Optional[Held] = None) -> Held:
    """Guard ``state.db`` and its ``-shm`` for one SessionDB handle on private descriptors. Returns
    the record :func:`release` needs; pass it back to refresh an existing one (a ``-shm`` minted or
    replaced after open, a reopened connection). O(1) in the process's fd count: one ``stat`` per
    file, an ``open`` + ``fcntl`` only for an inode not yet held. Never raises."""
    held = {} if held is None else held
    if not supported():
        return held
    try:
        for path, rng in _guard_targets(db_path):
            _arm(path, rng, held)
    except OSError:
        logger.debug("WAL lock guard unavailable for %s", os.fspath(db_path), exc_info=True)
    return held


def release(held: Held) -> List[int]:
    """Drop this handle's guard. Call BEFORE the handle's own close so SQLite's close-time reset
    sees only real holders: a sibling's intact locks (OFD or POSIX, this process or another) still
    refuse the unlink, and a true last close ends the generation, so a later ``state.db`` replace
    never pairs with a stale WAL. Returns the private fds; pass them to :func:`close_fds` AFTER the
    connection closes (see :func:`_unlock` for why not before)."""
    if not supported() or not held:
        return []
    entries = list(held.values())
    held.clear()
    return [_unlock(entry) for entry in entries]


def close_fds(fds: List[int]) -> None:
    """Second half of :func:`release`: close the private descriptors once SQLite's connection has."""
    for fd in fds:
        _close_quietly(fd)
