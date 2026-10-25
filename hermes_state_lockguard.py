"""Hold a state.db writer's WAL-mode file locks in a form a stray ``close()`` cannot cancel.

SQLite protects a live WAL generation with two POSIX advisory locks: a SHARED lock on the main
file's lock range and a shared lock on the DMS byte of ``state.db-shm``. A sibling process may
checkpoint and unlink ``-wal``/``-shm`` at its close only after taking both EXCLUSIVE. POSIX locks
are per process, so any ``open()``/``close()`` of those two files inside the holder — a raw probe,
a plugin, a tool reading ``~/.hermes`` — cancels both (sqlite.org/howtocorrupt.html §2.2) and the
next foreign close strands the holder on a deleted generation (``DeletedWalGenerationError``).

This module adds the same two ranges as *open file description* locks (``F_OFD_SETLK``) on the
descriptors SQLite itself holds. OFD locks belong to the description, not the process: a stray
``close()`` elsewhere cannot cancel them, they die with the connection's own descriptor (nothing
extra to track or retire), and they conflict with a foreign EXCLUSIVE exactly like SQLite's own,
so the sibling's close-time unlink is refused while a guarded handle is open. The guard is
lifted before the handle's own close so a true last close still ends the generation normally;
lifting it first re-takes SQLite's own POSIX locks on the same ranges, so the handle is never
unlocked while its connection is still open. No-op on Windows and on runtimes without OFD locks.

Ownership model: the guard is a property of the *descriptor*, and a descriptor number is
reusable. Each ``hold()`` therefore locks every matching descriptor unconditionally (an OFD
re-lock on an already-locked description is idempotent) and ``release()`` unlocks only while
another handle in this process still needs the range — tracked by handle count per INODE, not
per fd, so a recycled fd number can never be mistaken for a surviving lock.

Private descriptors (``HERMES_STATE_LOCKGUARD_MODE=private``, the default, t_aeae3fe7). Finding
SQLite's descriptors meant ``listdir('/dev/fd')`` + one ``fstat`` per fd, and ``hold()`` runs under
``SessionDB._lock``: with ~500 fds and a dozen GIL-busy threads each fstat queued for the GIL and
one hold() took 16-28 s (Discord replies 1-16 min late, 2026-10-24). OFD locks conflict by inode
range, so the same read lock on a descriptor THIS module opens refuses a foreign EXCLUSIVE just as
well. One private ``O_RDONLY|O_CLOEXEC`` descriptor per guarded inode, shared by every handle on it
(the same per-inode handle count). ``release()`` of the last handle swaps the OFD lock for the
process-owned POSIX lock SQLite believes it holds and leaves the descriptor OPEN: closing a
descriptor on a live inode cancels every POSIX lock this process holds there (howtocorrupt §2.2),
the very bug this module exists for. :func:`retire` closes it after SQLite's own close of the
last guarded handle, and only when no other descriptor in this process references the inode (an
unguarded reader's locks would go with it; that check is the one fd-table scan left, on the close
path only). A kept descriptor would otherwise pin a ``-shm`` a later true last close unlinks (the
deleted-generation probe then refuses every opener) and leak one fd per database. ``scan`` keeps the pre-change fd-table scan for one release; ``off`` is
the no-op path.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import threading
from typing import Dict, Optional, Set, Tuple

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

# Read once at import (D5). Unknown values fall back to the default rather than disarming the guard.
_MODES = ("private", "scan", "off")
_MODE = (os.environ.get("HERMES_STATE_LOCKGUARD_MODE") or "private").strip().lower()
if _MODE not in _MODES:
    logger.warning("HERMES_STATE_LOCKGUARD_MODE=%r is not one of %s; using 'private'", _MODE, "|".join(_MODES))
    _MODE = "private"
if _MODE == "off":
    _F_OFD_SETLK = None

# struct flock differs per libc: glibc/musl put type+whence first, Darwin/BSD last.
_FLOCK_FORMAT = "@qqihh" if sys.platform == "darwin" or "bsd" in sys.platform else "@hhqqi"

Identity = Tuple[int, int]
Held = Dict[Identity, Tuple[int, int]]  # inode this handle guards -> its (start, length) range

# Handles per guarded inode in this process. Several SessionDB handles on one file share the
# same descriptors' locks (hold() locks every matching descriptor), so the LAST handle unlocks.
_LOCK = threading.Lock()
_HANDLES: Dict[Identity, int] = {}
# private mode: this module's own descriptor per guarded inode, opened by hold() and closed by
# retire() once no handle counts the inode (after SQLite's own close, never before it).
_FDS: Dict[Identity, int] = {}


def _reset_after_fork() -> None:
    """A fork child shares the parent's open descriptions, so an F_UNLCK would lift the PARENT's
    guard: never unlock. Close the inherited copies instead (Prism 377384e39385): an OFD lock lives
    until the LAST descriptor on its description closes, and the parent still holds one, so the
    parent stays guarded; a forgotten copy would pin a ``-shm`` the parent's last close unlinks, and
    the deleted-generation probe would then refuse every opener until the child exits. Right after
    fork the child holds no POSIX locks (they are not inherited), so the close cancels nothing."""
    global _LOCK, _HANDLES, _FDS
    inherited = list(_FDS.values())
    _LOCK, _HANDLES, _FDS = threading.Lock(), {}, {}
    for fd in inherited:
        try:
            os.close(fd)
        except OSError:
            pass


if hasattr(os, "register_at_fork"):  # POSIX only
    os.register_at_fork(after_in_child=_reset_after_fork)


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


def _own_fds_for(identities: Set[Identity]):
    """Yield ``(fd, identity)`` for every descriptor of this process on one of *identities*
    (SQLite's own connection descriptors; the cached header-probe fd too, harmless)."""
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            if not name.isdigit():
                continue
            fd = int(name)
            try:
                st = os.fstat(fd)
            except OSError:
                continue
            ident = (st.st_dev, st.st_ino)
            if ident in identities:
                yield fd, ident
        return


def _guard_ranges(db_path) -> Held:
    base = os.fspath(db_path)
    ranges: Held = {}
    for path, rng in ((base, (_SHARED_FIRST, _SHARED_SIZE)), (base + "-shm", (_SHM_DMS_BYTE, 1))):
        ident = _identity(path)
        if ident is not None:
            ranges[ident] = rng
    return ranges


def _private_fd(path: str, ident: Identity) -> Optional[int]:
    """This module's descriptor on *ident* (opened on first use), or None when the path was
    replaced between the stat and the open (the next hold()/refresh guards the new file). Callers
    hold _LOCK."""
    fd = _FDS.get(ident)
    if fd is not None:
        return fd
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    real = (st.st_dev, st.st_ino)
    if real != ident:
        # Replaced between the stat and the open. Never close it here: SQLite may already hold
        # POSIX locks on the new file, and a close would cancel them. Keep it for the next hold().
        if real in _FDS:
            return None  # unreachable in practice (one open per inode under _LOCK); never close
        _FDS[real] = fd
        return None
    _FDS[ident] = fd
    return fd


def _inodes_with_other_fds(candidates: Dict[Identity, int]) -> Set[Identity]:
    """Inodes in *candidates* (ident -> our private fd) that some OTHER descriptor of this process
    still references: an unguarded in-process SQLite connection (a read-only handle, the delivery
    ledger) or a guarded one between its release() and its close. The fd-table scan runs only
    here, on the close path, never under ``SessionDB._lock`` and never from hold()/checkpoint."""
    mine = set(candidates.values())
    return {ident for fd, ident in _own_fds_for(set(candidates)) if fd not in mine}


def retire() -> None:
    """Close private descriptors no guarded handle counts any more, unless another descriptor in
    this process still references the inode. Any close cancels every POSIX lock this process holds
    on the inode (howtocorrupt §2.2), including a live in-process reader's SHARED/read-mark locks
    (Prism 44919d3eb909), so those are kept and retried by the next retire(). A descriptor whose
    lock was refused at hold() is swept the same way (Prism 78f32301dabd). Call it AFTER the
    handle's own close and outside ``SessionDB._lock``. No scan when nothing is retirable. Never
    raises."""
    if _MODE != "private":
        return
    try:
        with _LOCK:
            candidates = {i: fd for i, fd in _FDS.items() if _HANDLES.get(i, 0) == 0}
        if not candidates:
            return
        busy = _inodes_with_other_fds(candidates)
        with _LOCK:
            for ident, fd in candidates.items():
                if ident in busy or _HANDLES.get(ident, 0) or _FDS.get(ident) != fd:
                    continue  # still referenced, or re-armed by a hold() meanwhile
                del _FDS[ident]
                try:
                    os.close(fd)
                except OSError:
                    pass
    except Exception:
        logger.debug("WAL lock guard: descriptor retire failed", exc_info=True)


def _arm_private(ranges: Dict[str, Tuple[Identity, Tuple[int, int]]], held: Held) -> None:
    """OFD-read-lock *ranges* (path -> (inode, range)) on private descriptors. Callers hold _LOCK."""
    for path, (ident, (start, length)) in ranges.items():
        if ident in held:
            continue  # this handle already counts the inode; its descriptor stays locked
        fd = _private_fd(path, ident)
        if fd is not None and _ofd_lock(fd, _F_RDLCK, start, length):
            held[ident] = (start, length)
            _HANDLES[ident] = _HANDLES.get(ident, 0) + 1


def _private_ranges(db_path, *, shm_only: bool = False) -> Dict[str, Tuple[Identity, Tuple[int, int]]]:
    base = os.fspath(db_path)
    pairs = [] if shm_only else [(base, (_SHARED_FIRST, _SHARED_SIZE))]
    pairs.append((base + "-shm", (_SHM_DMS_BYTE, 1)))
    out = {}
    for path, rng in pairs:
        ident = _identity(path)
        if ident is not None:
            out[path] = (ident, rng)
    return out


def hold(db_path, held: Optional[Held] = None) -> Held:
    """Lock the guard ranges on ``state.db`` and its ``-shm`` (private mode: on this module's own
    descriptor per inode, O(1) in the fd-table size). Returns the record :func:`release` needs;
    pass it back to extend an existing one (a ``-shm`` minted after open, a reopened connection).
    Idempotent per handle: an inode already in *held* is not counted twice. Never raises."""
    held = {} if held is None else held
    if not supported():
        return held
    if _MODE == "private":
        try:
            ranges = _private_ranges(db_path)
            with _LOCK:
                _arm_private(ranges, held)
        except OSError:
            logger.debug("WAL lock guard unavailable for %s", os.fspath(db_path), exc_info=True)
        return held
    ranges = _guard_ranges(db_path)
    try:
        with _LOCK:
            for fd, ident in _own_fds_for(set(ranges)):
                start, length = ranges[ident]
                if _ofd_lock(fd, _F_RDLCK, start, length) and ident not in held:
                    held[ident] = ranges[ident]
                    _HANDLES[ident] = _HANDLES.get(ident, 0) + 1
    except OSError:
        logger.debug("WAL lock guard unavailable for %s", os.fspath(db_path), exc_info=True)
    return held


def refresh_shm(db_path, held: Held) -> Held:
    """Guard a ``-shm`` minted after open (the periodic-checkpoint path, under ``SessionDB._lock``).
    Private mode costs one ``os.stat`` when the current ``-shm`` is already guarded; ``scan`` mode
    is the old full :func:`hold`. Never raises; a missing ``-shm`` is a no-op."""
    if not supported():
        return held
    if _MODE != "private":
        return hold(db_path, held)
    try:
        ranges = _private_ranges(db_path, shm_only=True)
        if all(ident in held for ident, _ in ranges.values()):
            return held
        with _LOCK:
            _arm_private(ranges, held)
    except OSError:
        logger.debug("WAL lock guard refresh unavailable for %s", os.fspath(db_path), exc_info=True)
    return held


def release(held: Held) -> None:
    """Drop this handle's claim. The last handle on an inode unlocks the range on every descriptor
    still referencing it. Call BEFORE the handle's own close so SQLite's close-time reset sees only
    real holders: a sibling process's intact locks still refuse the unlink, and a true last close
    ends the generation, so a later ``state.db`` replace never pairs with a stale WAL.

    The OFD copy is swapped back for the POSIX lock SQLite believes it still holds (a stray close
    cancelled the real one): unlocked outright, the still-open connection would let a sibling's
    close take EXCLUSIVE and unlink ``-wal``/``-shm`` under it before its own close runs. SQLite's
    close upgrades or drops that process-owned lock itself, so a true last close is unaffected.
    Private mode keeps the descriptor open; :func:`retire` closes it after SQLite's close."""
    if not supported() or not held:
        return
    with _LOCK:
        to_unlock: Held = {}
        for ident, rng in held.items():
            remaining = _HANDLES.get(ident, 1) - 1
            if remaining > 0:
                _HANDLES[ident] = remaining
            else:
                _HANDLES.pop(ident, None)
                to_unlock[ident] = rng
        held.clear()
        if not to_unlock:
            return
        if _MODE == "private":
            for ident, (start, length) in to_unlock.items():
                fd = _FDS.get(ident)
                if fd is None:
                    continue
                try:  # kept open until retire(): a close here would cancel every POSIX lock on the inode
                    _ofd_lock(fd, _F_RDLCK, start, length, cmd=_F_SETLK)  # never refused: our OFD lock excludes writers
                    _ofd_lock(fd, _F_UNLCK, start, length)
                except OSError:
                    pass
            return
        try:
            for fd, ident in _own_fds_for(set(to_unlock)):
                start, length = to_unlock[ident]
                _ofd_lock(fd, _F_RDLCK, start, length, cmd=_F_SETLK)  # never refused: our OFD lock excludes writers
                _ofd_lock(fd, _F_UNLCK, start, length)
        except OSError:
            pass
