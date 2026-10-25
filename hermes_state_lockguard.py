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
process-owned POSIX lock SQLite believes it holds, then:

- ``state.db``: PARKS the descriptor, open and OFD-unlocked, for the next hold() of the inode.
  SQLite never unlinks the main file, so a parked fd pins nothing, and a close would cancel every
  POSIX lock this process holds on it (howtocorrupt §2.2). :func:`retire` closes it only on a
  confirmed identity change (the path now names another inode).
- ``-shm``: never parked. :func:`retire`, which ``SessionDB.close()`` calls right after SQLite's own
  close, CLOSES a -shm descriptor once nothing in this process still uses the inode (Apollo ruling
  on Prism 9016456d284b). A parked -shm fd outlives its generation once ANOTHER process's last close
  unlinks the file, and every opener then fails with ``DeletedWalGenerationError``.

"Nothing still uses the inode" = three counts at 0, so the close can never cancel a live in-process
connection's POSIX locks (Prism 7072ffc85b72 / f73fc9c5f6cc / 5ac4f2a27277):
  * ``_HANDLES``: guarded SessionDB writers;
  * ``_CLOSING``: writers between :func:`release` and :func:`retire` (their SQLite connection is
    still open: release() runs BEFORE the close);
  * ``_READERS``: every other in-process connection, registered with :func:`registered_reader`
    (SessionDB's own read pool and read-only handles, and every raw ``sqlite3.connect`` of state.db,
    which ``tests/hermes_state/test_state_db_raw_writer_lint.py`` requires to sit in one). A
    read-only WAL connection that loses its -shm read-mark can have its frames checkpointed away
    under it (a torn read), so readers count too.

Paths are made absolute at hold() time, so a later chdir cannot make a live inode look gone.
No fd-table scan in private mode. ``scan`` keeps the pre-change fd-table scan for one release;
``off`` is the no-op path.
"""

from __future__ import annotations

import errno
import logging
import os
import struct
import sys
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Set, Tuple

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


class _Held(dict):
    """A :func:`hold` record owned by the process that took it (Prism bf7314233ba1): after fork() the
    child's counts start empty, and releasing an INHERITED record there would decrement the child's own
    fresh claims. :func:`release` ignores a record from another pid."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pid = os.getpid()

# Handles per guarded inode in this process. Several SessionDB handles on one file share the
# same descriptors' locks (hold() locks every matching descriptor), so the LAST handle unlocks.
_LOCK = threading.Lock()
_HANDLES: Dict[Identity, int] = {}
# private mode: this module's own descriptor per guarded inode, opened by the first hold() of the
# inode. A state.db fd is PARKED (OFD-unlocked, still open) when no handle counts it and the next
# hold() reuses it; a -shm fd no handle counts is closed by the next retire().
_FDS: Dict[Identity, int] = {}
_PATHS: Dict[Identity, str] = {}  # inode in _FDS -> the absolute path that named it
_SHM_IDENTS: Set[Identity] = set()  # the -shm inodes in _FDS (retire() closes them at count 0)
# registered_reader() connections (pin only), keyed by the state.db inode seen at registration AND by
# ("path", <absolute path>): a path re-pointed at another file between the registration and the connect
# (Prism e0cbbe030829) is still covered, since every private descriptor records the path that named it.
_READERS: Dict[object, int] = {}
_SHM_DB: Dict[Identity, Identity] = {}  # -shm inode in _FDS -> its state.db inode (readers key on it)
_CLOSING: Dict[Identity, int] = {}  # writers between release() and retire(): connection still open
# Extra private descriptors on an inode that already had one: a path replaced between a hold()'s stat
# and its open can make two opens land on one inode (Prism c8e1b4b3568d). Never closed on their own (a
# close cancels this process's POSIX locks on the inode); closed together with the inode's main fd.
_SURPLUS: Dict[Identity, List[int]] = {}
_OPEN_FAIL_WARNED: Set[str] = set()  # paths whose private open failed (logged once)
_POOL_WARN_FDS = 16  # expected: one per state.db file guarded in this process
_POOL_WARNED = False
_RETIRE_WARNED: Set[Identity] = set()  # inodes whose inconclusive stat was logged once
# Serializes "SQLite's last close unlinked -shm, our private -shm fd is not closed yet" against the
# deleted-WAL-generation probes (Prism 9d02254cf252): SessionDB.close() holds it from just before SQLite's
# close through retire(), and the probes (hermes_state: refuse before connect, _wal_generation_was_lost)
# take it, so they never see this module's own not-yet-retired descriptor as a live deleted generation.
# Lock order: SessionDB._lock -> RETIRE_GATE -> _LOCK. Reentrant: close() may probe while holding it.
RETIRE_GATE = threading.RLock()


def _reset_after_fork() -> None:
    """A fork child shares the parent's open descriptions, so an F_UNLCK would lift the PARENT's
    guard: never unlock. Close the inherited copies instead (Prism 377384e39385): an OFD lock lives
    until the LAST descriptor on its description closes, and the parent still holds one, so the
    parent stays guarded; a forgotten copy would pin a ``-shm`` the parent's last close unlinks, and
    the deleted-generation probe would then refuse every opener until the child exits. Right after
    fork the child holds no POSIX locks (they are not inherited), so the close cancels nothing."""
    global _LOCK, _HANDLES, _FDS, _PATHS, _SHM_IDENTS, _READERS, _CLOSING, _SHM_DB, RETIRE_GATE, _SURPLUS
    inherited = list(_FDS.values()) + [fd for fds in _SURPLUS.values() for fd in fds]
    _SURPLUS = {}
    # The inherited _LOCK is held (_before_fork took it); a fresh one releases it for the child. The
    # gate may be held by a parent thread that does not exist in the child: fresh too.
    _LOCK, _HANDLES, _FDS, _PATHS, _SHM_IDENTS = threading.Lock(), {}, {}, {}, set()
    RETIRE_GATE = threading.RLock()
    _READERS, _CLOSING, _SHM_DB = {}, {}, {}
    for fd in inherited:
        try:
            os.close(fd)
        except OSError:
            pass


def _before_fork() -> None:
    """Fork only between, never inside, an open+park (Prism 11061d53f43a): every os.open() of a
    private descriptor and its _park() run under _LOCK, so holding it across fork() means the child's
    _FDS lists every private descriptor it inherited, and _reset_after_fork closes all of them."""
    _LOCK.acquire()


def _after_fork_in_parent() -> None:
    _LOCK.release()


if hasattr(os, "register_at_fork"):  # POSIX only
    os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_in_parent,
                        after_in_child=_reset_after_fork)


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


def _private_fd(path: str, ident: Identity, db_ident: Optional[Identity] = None) -> Optional[int]:
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
            # Another hold() already parked the replacement (stat outside _LOCK, Prism c8e1b4b3568d):
            # keep this one tracked as a surplus of that inode, closed together with its main fd.
            _SURPLUS.setdefault(real, []).append(fd)
            return None
        _park(path, real, fd, db_ident)
        return None
    _park(path, ident, fd, db_ident)
    return fd


def _park(path: str, ident: Identity, fd: int, db_ident: Optional[Identity] = None) -> None:
    """Record *fd* as this module's descriptor on *ident*. Callers hold _LOCK."""
    _FDS[ident] = fd
    _PATHS[ident] = path
    if path.endswith("-shm"):
        _SHM_IDENTS.add(ident)
        if db_ident is not None:
            _SHM_DB[ident] = db_ident
    _note_pool_size()


def _close_private(ident: Identity) -> None:
    """Forget and close this module's descriptor on *ident*. Callers hold _LOCK."""
    fd = _FDS.pop(ident, None)
    _PATHS.pop(ident, None)
    _SHM_IDENTS.discard(ident)
    _SHM_DB.pop(ident, None)
    for extra in [fd] + _SURPLUS.pop(ident, []):
        if extra is None:
            continue
        try:
            os.close(extra)
        except OSError:
            pass


def _note_pool_size() -> None:
    """Log once if the parked-descriptor pool outgrows its expected bound. Callers hold _LOCK."""
    global _POOL_WARNED
    if not _POOL_WARNED and len(_FDS) > _POOL_WARN_FDS:
        _POOL_WARNED = True
        logger.warning("WAL lock guard: %d private descriptors parked (> %d); one per guarded state.db "
                       "is expected", len(_FDS), _POOL_WARN_FDS)


def _stat_identity(path: str) -> Tuple[Optional[Identity], Optional[OSError]]:
    """(identity, None) when *path* stats; (None, ENOENT error) when it is gone; (None, err) on
    any other OSError (inconclusive: EACCES, EIO, ...)."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return None, exc
    return (st.st_dev, st.st_ino), None


def _in_use(ident: Identity) -> bool:
    """A guarded writer, a writer between release() and retire(), or a registered reader still uses
    *ident* in this process. A reader registers the state.db inode: its -shm read-mark is on whatever
    -shm generation that file has, so a -shm counts the readers of its state.db. Callers hold _LOCK."""
    if _HANDLES.get(ident, 0) or _CLOSING.get(ident, 0) or _READERS.get(ident, 0):
        return True
    db = _SHM_DB.get(ident)
    # A writer whose hold() preceded the -shm counts only its state.db inode until refresh_shm(), yet
    # its connection already holds POSIX locks on this -shm (Prism 1de1d88b5bec).
    if db is not None and (_READERS.get(db, 0) or _HANDLES.get(db, 0) or _CLOSING.get(db, 0)):
        return True
    path = _PATHS.get(ident)
    if path is None:
        return False
    base = path[:-len("-shm")] if ident in _SHM_IDENTS else path
    return bool(_READERS.get(("path", base), 0))


def _settle(counts: Dict[Identity, int], idents) -> None:
    """Decrement *counts* once per ident in *idents*. Callers hold _LOCK."""
    for ident in idents:
        remaining = counts.get(ident, 0) - 1
        if remaining > 0:
            counts[ident] = remaining
        else:
            counts.pop(ident, None)


def retire(closing: Optional[List[Identity]] = None) -> None:
    """Settle *closing* (what :func:`release` returned for this handle: its SQLite connection is
    closed now), then close every ``-shm`` descriptor nothing in this process still uses (never
    parked, see the module docstring), and a parked ``state.db`` descriptor nothing uses whose inode
    is CONFIRMED gone: its absolute path no longer exists (ENOENT) or names a different inode. Any
    other stat error keeps that descriptor and logs once (Prism a97a215db7d8): closing it would
    cancel every POSIX lock this process holds on a possibly live inode. One ``os.stat`` per parked
    ``state.db`` fd, no fd-table scan. Call it AFTER the handle's own close and outside
    ``SessionDB._lock``. Never raises."""
    try:
        with _LOCK:
            if closing:
                _settle(_CLOSING, closing)
        if _MODE != "private":
            return
        with _LOCK:
            for ident in [i for i in _SHM_IDENTS if not _in_use(i)]:
                _close_private(ident)
            parked = {i: (fd, _PATHS[i]) for i, fd in _FDS.items() if not _in_use(i) and i in _PATHS}
        if not parked:
            return
        gone = set()
        for ident, (_fd, path) in parked.items():
            real, err = _stat_identity(path)
            if real is not None:
                if real != ident:
                    gone.add(ident)
            elif getattr(err, "errno", None) == errno.ENOENT:
                gone.add(ident)
            elif ident not in _RETIRE_WARNED:
                _RETIRE_WARNED.add(ident)
                logger.warning("WAL lock guard: cannot stat %s (%s); keeping its private descriptor", path, err)
        if not gone:
            return
        with _LOCK:
            for ident in gone:
                if _in_use(ident) or _FDS.get(ident) != parked[ident][0]:
                    continue  # re-armed by a hold() or a registered reader meanwhile
                _close_private(ident)
    except Exception:
        logger.debug("WAL lock guard: descriptor retire failed", exc_info=True)


def retire_completed_shm() -> None:
    """For the deleted-generation PROBES only (under ``RETIRE_GATE``): close a private ``-shm`` fd whose
    generation ended with a completed in-process close, even while a registered reader's block has not
    exited yet (its connection closed last and unlinked -shm, Prism e5b80b447fa9). "Completed" is
    proven, never assumed (Prism 24d6965d4428): the path is gone (ENOENT) or names another inode AND no
    other descriptor in this process references the inode, so no SQLite connection here still uses it.
    An externally unlinked -shm that a live connection still holds keeps the fd: that is the real
    deleted-generation case and the probe must see it. The fd-table scan runs only when a candidate
    exists (a pinned -shm whose path is gone): never on the hold/checkpoint/open fast path. Never raises."""
    if _MODE != "private":
        return
    try:
        with _LOCK:
            pinned = {i: (_FDS[i], _PATHS[i]) for i in _SHM_IDENTS if i in _FDS and i in _PATHS}
        candidates = {}
        for ident, (fd, path) in pinned.items():
            real, err = _stat_identity(path)
            if (real is not None and real != ident) or (real is None and getattr(err, "errno", None) == errno.ENOENT):
                candidates[ident] = fd
        if not candidates:
            return
        with _LOCK:
            mine = {fd for i in candidates for fd in [_FDS.get(i)] + _SURPLUS.get(i, []) if fd is not None}
        others = {ident for fd, ident in _other_fds_on(set(candidates), mine)}
        with _LOCK:
            for ident, fd in candidates.items():
                if ident not in others and _FDS.get(ident) == fd:
                    _close_private(ident)
    except Exception:
        logger.debug("WAL lock guard: completed -shm retire failed", exc_info=True)


def _other_fds_on(identities: Set[Identity], exclude: Set[int]):
    """``(fd, ident)`` for this process's descriptors on *identities*, except *exclude*. Probe path only."""
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            if not name.isdigit() or int(name) in exclude:
                continue
            try:
                st = os.stat(f"{fd_dir}/{name}")
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in identities:
                yield int(name), (st.st_dev, st.st_ino)
        return


# No atexit close of the pool (Prism c00385eecb2c / ea4900b85b87): at exit a daemon writer may still
# be inside a transaction on a live connection, and closing our descriptor would cancel its POSIX
# locks. The kernel releases the descriptors and their OFD locks at process exit.


def register_reader(db_path) -> List[object]:
    """Count an in-process connection on *db_path* so :func:`retire` never closes a private
    descriptor (its ``state.db``, or any ``-shm`` generation of it) under the connection. Register
    BEFORE the connect: its ``-shm`` may appear only at the first read, and the file may not exist yet
    (a writer's first open). Counted by the inode seen now AND by the absolute path, so a file replaced
    at the path before the connect is covered too (Prism e0cbbe030829). SessionDB's writer registers
    for its whole open, connect through hold() (Prism c4ca84c945ca / 8f1afbe91566).
    Takes no file lock and opens nothing: one ``os.stat``.
    Returns the token :func:`unregister_reader` needs. Never raises."""
    try:
        path = os.path.abspath(os.fspath(db_path))
        ident = _identity(path)
        idents: List[object] = [("path", path)] + ([] if ident is None else [ident])
        with _LOCK:
            for ident in idents:
                _READERS[ident] = _READERS.get(ident, 0) + 1
        # Bound to this process (Prism 1e8ea9086d86): a fork child starts with empty counts, and an
        # inherited token settled there would decrement the CHILD's own readers' registrations.
        return [("pid", os.getpid())] + idents
    except Exception:
        logger.debug("WAL lock guard: reader registration failed for %s", db_path, exc_info=True)
        return []


def unregister_reader(token: List[object], *, retire_now: bool = True) -> None:
    """Drop a :func:`register_reader` count (after the connection's own close), then :func:`retire`
    whatever that leaves unused (``retire_now=False``: settle only, for a caller under
    ``SessionDB._lock`` whose connection stays open and is now counted by :func:`hold`). Never raises."""
    if not token:
        return
    owner, idents = token[0], token[1:]
    if not (isinstance(owner, tuple) and owner[:1] == ("pid",) and owner[1] == os.getpid()):
        return  # minted before a fork (or malformed): its counts belong to another process
    try:
        with _LOCK:
            _settle(_READERS, idents)
    except Exception:
        logger.debug("WAL lock guard: reader unregistration failed", exc_info=True)
    if retire_now:
        retire()


@contextmanager
def registered_reader(db_path) -> Iterator[None]:
    """``with registered_reader(path): conn = sqlite3.connect(...) ... conn.close()``. Every
    in-process state.db connection outside SessionDB's writer opens and closes inside one, so the
    lock guard never closes a ``-shm`` descriptor under its read-mark (Prism f73fc9c5f6cc). Close the
    connection INSIDE the block. Registration never raises."""
    token = register_reader(db_path)
    try:
        yield
    finally:
        unregister_reader(token)


def _arm_private(ranges: Dict[str, Tuple[Identity, Tuple[int, int]]], held: Held) -> None:
    """OFD-read-lock *ranges* (path -> (inode, range)) on private descriptors. Callers hold _LOCK."""
    main = (_SHARED_FIRST, _SHARED_SIZE)
    db_ident = next((i for i, rng in ranges.values() if rng == main), None)
    if db_ident is None:  # refresh_shm: the handle's own state.db inode is in *held*
        db_ident = next((i for i, rng in held.items() if tuple(rng) == main), None)
    for path, (ident, (start, length)) in ranges.items():
        if ident in held:
            continue  # this handle already counts the inode; its descriptor stays locked
        # Counted whether or not the private open or the OFD lock succeeds (Prism e63d481511da,
        # 2814ba2fca00): the count is what keeps retire() from closing any private descriptor on the
        # inode (one a sibling opens later included), and so from cancelling this live connection's
        # POSIX locks; it must not depend on either. A failure only loses the stray-close protection
        # for this handle (logged once per path), never the connection-lifetime protection.
        try:
            fd = _private_fd(path, ident, db_ident)
        except OSError as exc:
            fd = None
            if path not in _OPEN_FAIL_WARNED:
                _OPEN_FAIL_WARNED.add(path)
                logger.warning("WAL lock guard: cannot open %s (%s); counting the handle unlocked", path, exc)
        locked = False
        if fd is not None:
            try:
                locked = _ofd_lock(fd, _F_RDLCK, start, length)
            except OSError:
                locked = False
        if not locked:
            logger.debug("WAL lock guard: OFD lock on %s not taken; counting the handle unlocked", path)
        held[ident] = (start, length)
        _HANDLES[ident] = _HANDLES.get(ident, 0) + 1


def _private_ranges(db_path, *, shm_only: bool = False) -> Dict[str, Tuple[Identity, Tuple[int, int]]]:
    base = os.path.abspath(os.fspath(db_path))  # cached paths must survive a chdir (Prism 5214a704d594)
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
    held = _Held() if held is None else held
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


def release(held: Held) -> List[Identity]:
    """Drop this handle's claim. The last handle on an inode unlocks the range on every descriptor
    still referencing it. Call BEFORE the handle's own close so SQLite's close-time reset sees only
    real holders: a sibling process's intact locks still refuse the unlink, and a true last close
    ends the generation, so a later ``state.db`` replace never pairs with a stale WAL.

    The OFD copy is swapped back for the POSIX lock SQLite believes it still holds (a stray close
    cancelled the real one): unlocked outright, the still-open connection would let a sibling's
    close take EXCLUSIVE and unlink ``-wal``/``-shm`` under it before its own close runs. SQLite's
    close upgrades or drops that process-owned lock itself, so a true last close is unaffected.
    Private mode keeps the descriptor open, OFD-unlocked: ``state.db`` stays parked for reuse, a
    ``-shm`` is closed by :func:`retire` after SQLite's close; see the module docstring.

    Returns the inodes this handle still uses until its own close: pass them to :func:`retire`
    after that close, so a sibling's retire() cannot close a descriptor under the still-open
    connection in between (Prism 5ac4f2a27277)."""
    if not supported() or not held:
        return []
    if getattr(held, "pid", os.getpid()) != os.getpid():
        held.clear()  # inherited across fork(): its claims were the parent's; the child counts none
        return []
    with _LOCK:
        closing = list(held)
        for ident in closing:
            _CLOSING[ident] = _CLOSING.get(ident, 0) + 1
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
            return closing
        if _MODE == "private":
            for ident, (start, length) in to_unlock.items():
                fd = _FDS.get(ident)
                if fd is None:
                    continue
                try:
                    _ofd_lock(fd, _F_RDLCK, start, length, cmd=_F_SETLK)  # never refused: our OFD lock excludes writers
                    _ofd_lock(fd, _F_UNLCK, start, length)
                except OSError:
                    pass
            return closing
        try:
            for fd, ident in _own_fds_for(set(to_unlock)):
                start, length = to_unlock[ident]
                _ofd_lock(fd, _F_RDLCK, start, length, cmd=_F_SETLK)  # never refused: our OFD lock excludes writers
                _ofd_lock(fd, _F_UNLCK, start, length)
        except OSError:
            pass
    return closing
