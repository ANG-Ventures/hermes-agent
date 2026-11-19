"""Lock-safe inspection of SQLite database files.

POSIX advisory locks are cancelled **process-wide** by ``close()`` on *any* fd for that file, so a
bare ``open(db_path, "rb") ... close()`` on a live database drops every lock SQLite holds from this
process (a VACUUM's EXCLUSIVE lock, an in-flight BEGIN IMMEDIATE's RESERVED lock). This module
tracks live connections so raw reads happen only when none exist.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Guards BOTH the registry and the lifecycle syscalls it describes. Reentrant
# because connect_tracked -> _canonical_db_path -> ... stays on one thread.
_live_lock = threading.RLock()
# canonical path -> number of live connections opened by this process
_live_connections: dict[str, int] = {}
# canonical main path -> thread id of a long raw copy holding that ONE database out of service
# (:func:`database_offline`). Tracked opens of that path wait; every other database never does.
_offline_paths: dict[str, int] = {}
_offline_released = threading.Condition(_live_lock)


class UntrackableConnectionError(RuntimeError):
    """A connection to a probe-able database could not be tracked. Raised rather than returning an
    untracked connection: on these paths tracking is part of the correctness contract."""


class LiveConnectionError(RuntimeError):
    """A raw file operation was attempted on a database with live connections."""


def _key(path: Path | str) -> str:
    """Canonicalise a *filesystem* path for use as a registry key."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def _canonical_db_path(conn: sqlite3.Connection) -> Optional[str]:
    """The on-disk path of ``main`` as SQLite reports it (immune to ``file:`` URIs, relative paths,
    symlinks). ``None`` for in-memory/unnamed databases, which cannot be byte-probed."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if not row or len(row) < 3 or not row[2]:
        return None
    return _key(row[2])


def track_connection(path: Path | str) -> None:
    """Record that this process holds a connection to *path* (prefer :func:`connect_tracked`; this
    is for callers managing their own connection objects, and for tests)."""
    with _live_lock:
        _track_key(_key(path))


def _track_key(key: str, delta: int = 1) -> None:
    """Adjust the live count for an already-canonical key (caller holds ``_live_lock``)."""
    remaining = _live_connections.get(key, 0) + delta
    if remaining > 0:
        _live_connections[key] = remaining
    else:
        _live_connections.pop(key, None)


def untrack_connection(path: Path | str) -> None:
    """Record that one connection to *path* has been closed."""
    with _live_lock:
        _track_key(_key(path), -1)


def _live_main_key(key: str) -> Optional[str]:
    """The tracked main-database key that makes *key* live, or ``None`` (caller holds ``_live_lock``).

    SQLite locks the main file and its WAL sidecars; a raw ``close()`` of any of those
    inodes cancels this process's POSIX locks, but the registry is keyed by the main path."""
    if key in _live_connections:
        return key
    for suffix in ("-wal", "-shm"):
        if key.endswith(suffix) and key[:-len(suffix)] in _live_connections:
            return key[:-len(suffix)]
    return None


def has_live_connection(path: Path | str) -> bool:
    """Whether this process holds a connection to *path* (or to the database it is a sidecar of).

    Point-in-time answer: a raw open/close right after it returns ``False`` can still race a
    new connection. Hold :func:`offline_file_access` across the I/O whenever possible."""
    with _live_lock:
        return _live_main_key(_key(path)) is not None


class _TrackingMixin:
    """Untrack-on-close behaviour, mixable into any Connection subclass.

    The real ``close()`` and the unregister happen together under ``_live_lock`` so a concurrent
    probe can never observe "no live connection" while this descriptor is still open. Unregister
    runs only after ``close()`` succeeds: untracking before a failing close (e.g. cross-thread
    ProgrammingError) would leave the FD open while the byte-probe guard thinks nothing is live.
    """

    _hermes_tracked_path: str | None = None

    def close(self) -> None:  # type: ignore[misc]
        with _live_lock:
            path = getattr(self, "_hermes_tracked_path", None)
            # Close first; untrack only once the descriptor is actually gone. Untracking before a failing
            # close (e.g. cross-thread ProgrammingError) leaves the FD open while the byte-probe guard
            # thinks nothing is live — see #75629.
            super().close()  # type: ignore[misc]
            if path is not None:
                self._hermes_tracked_path = None
                untrack_connection(path)


class TrackedConnection(_TrackingMixin, sqlite3.Connection):
    """A ``sqlite3.Connection`` that untracks its path exactly once on close (callers close in many
    places, some via ``contextlib.closing``, so counting closes by hand is unreliable)."""


_tracked_factory_cache: dict[type, type] = {}


def _tracking_factory(factory: type) -> type:
    """Return *factory* augmented with untrack-on-close. Callers legitimately pass their own
    ``Connection`` subclasses (tests simulate FTS5-less or pragma-failing runtimes); leaving them
    untracked would quietly unguard the database, so the tracking ``close()`` is mixed in."""
    if factory is sqlite3.Connection:
        return TrackedConnection
    if issubclass(factory, _TrackingMixin):
        return factory
    cached = _tracked_factory_cache.get(factory)
    if cached is None:
        cached = type(f"Tracked{factory.__name__}", (_TrackingMixin, factory), {})
        _tracked_factory_cache[factory] = cached
    return cached


def connect_tracked(
    path: Path | str, *, tracking_path: Path | str | None = None, connect_fn=None, **kwargs,
) -> sqlite3.Connection:
    """``sqlite3.connect`` that registers the connection for the lifetime of the fd (released on
    ``close()``). Use for any database that might be byte-probed (``state.db``, ``kanban.db``).
    Open and registration happen together under ``_live_lock`` so a concurrent
    :func:`read_header_bytes_preopen` cannot slip between them and cancel this connection's locks."""
    opener = connect_fn if connect_fn is not None else sqlite3.connect
    kwargs["factory"] = _tracking_factory(kwargs.get("factory", sqlite3.Connection))

    with _live_lock:
        _wait_while_offline(_key(tracking_path) if tracking_path is not None else _key(path))
        conn = opener(str(path), **kwargs)
        try:
            resolved = _key(tracking_path) if tracking_path is not None else _canonical_db_path(conn)
            if resolved is None:
                return conn  # in-memory / unnamed: nothing on disk to byte-probe
            if not isinstance(conn, _TrackingMixin):
                # The opener substituted its own factory and discarded ours (test doubles
                # simulating FTS5-less runtimes do this). Retag the instance's class so close()
                # still releases the registry entry rather than silently losing probe safety.
                conn = _retrofit_tracking(conn, resolved)
            conn._hermes_tracked_path = resolved
            _track_key(resolved)
            return conn
        except Exception:
            try:
                # Close via sqlite3 directly: the tracking entry was either never made or is
                # being unwound here.
                sqlite3.Connection.close(conn)
            except Exception:
                pass
            raise


def _retrofit_tracking(conn: sqlite3.Connection, resolved: str) -> sqlite3.Connection:
    """Give an already-open connection untrack-on-close semantics by swapping ``__class__`` for
    one mixing in the tracking ``close()`` (used when an opener ignored the factory we asked for)."""
    cls = type(conn)
    try:
        conn.__class__ = _tracking_factory(cls)  # type: ignore[assignment]
        return conn
    except TypeError as exc:
        raise UntrackableConnectionError(
            f"connection to {resolved} uses factory {cls.__name__}, which "
            "cannot release its tracking entry on close; byte-probe safety "
            "for this database would be silently lost"
        ) from exc


def page_count_bytes(conn: sqlite3.Connection) -> Optional[int]:
    """Logical database size in bytes (``page_count * page_size``, the header field at offset 28)
    read via PRAGMA over *conn* so no new fd is opened. ``None`` when the pragmas cannot be read."""
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count) * int(page_size)
    except (sqlite3.Error, TypeError, IndexError, ValueError) as exc:
        logger.debug("page_count/page_size unavailable: %s", exc)
        return None


def file_length_matches_header(conn: sqlite3.Connection) -> Optional[bool]:
    """Whether the file on disk is at least as long as the header claims ("torn extend" check),
    without opening the file (PRAGMA over *conn* + ``stat()``). Advisory in WAL mode: a freshly
    committed page may still live in ``-wal`` so the main file legitimately lags."""
    path_str = _canonical_db_path(conn)
    if path_str is None:
        return None
    logical = page_count_bytes(conn)
    if not logical:
        return None
    try:
        return os.path.getsize(path_str) >= logical
    except OSError:
        return None


def _fd_dir() -> Optional[str]:
    for candidate in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(candidate):
            return candidate
    return None


def process_holds_open_fd(path: Path | str) -> bool:
    """Whether any descriptor of this process refers to *path*'s inode.

    The tracked registry only knows connections opened through :func:`connect_tracked`; a plain
    ``sqlite3.connect`` (the board anchor, notifier helpers, another SQLite library) is invisible to
    it, yet a raw ``close()`` cancels that connection's POSIX locks all the same (t_54b8e6cc). The
    kernel's descriptor table is the complete answer. ``False`` where there is no descriptor
    directory to read (no POSIX advisory locks to lose there either)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    target = (st.st_dev, st.st_ino)
    fd_dir = _fd_dir()
    if fd_dir is None:
        return False
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return False
    for name in names:
        try:
            fst = os.fstat(int(name))
        except (OSError, ValueError):
            continue  # the listing's own descriptor, or one closed since
        if (fst.st_dev, fst.st_ino) == target:
            return True
    return False


def read_header_bytes_preopen(path: Path | str, *, length: int = 100, force: bool = False) -> Optional[bytes]:
    """Read the first *length* bytes of *path* -- only when no connection is live. The ONLY
    sanctioned byte-level read of a database file, for first-open validation (real SQLite? zeroed?
    overwritten?). Check and open/read/close run together under ``_live_lock`` so a connection
    cannot be opened between deciding "nothing is live" and closing this descriptor."""
    with _live_lock:
        if not force and _live_main_key(_key(path)) is not None:
            logger.debug(
                "refusing byte-level read of %s: a live connection exists in "
                "this process and close() would cancel its POSIX locks",
                path)
            return None
        try:
            with open(path, "rb") as handle:
                return handle.read(length)
        except OSError:
            return None


@contextlib.contextmanager
def offline_file_access(path: Path | str, *, what: str = "read"):
    """Hold the connection-lifecycle lock across a raw read of a database file: checking
    :func:`has_live_connection` and *then* doing raw I/O is a check/use race (a connection opened
    in between loses its POSIX locks to the raw ``close()``). Held only for the raw I/O."""
    with _live_lock:
        _refuse_if_live(path, what)
        yield


def _refuse_if_live(path: Path | str, what: str) -> None:
    """Raise :class:`LiveConnectionError` if *path* is live (caller holds ``_live_lock``)."""
    main = _live_main_key(_key(path))
    if main is not None:
        subject = "it" if main == _key(path) else f"its main database {main}"
        raise LiveConnectionError(
            f"Refusing to {what} {path}: a connection to {subject} is still open "
            "in this process, and raw file access would cancel that "
            "connection's POSIX advisory locks. Close all database "
            "handles (stop the gateway/dashboard) and retry.")


def _wait_while_offline(key: str) -> None:
    """Block while another thread holds *key* in :func:`database_offline` (caller holds
    ``_live_lock``; the wait releases it, so other databases keep opening and closing)."""
    while _offline_paths.get(key, threading.get_ident()) != threading.get_ident():
        _offline_released.wait()


@contextlib.contextmanager
def database_offline(path: Path | str, *, what: str = "read"):
    """:func:`offline_file_access` for long raw I/O (a multi-hundred-MB copy). Refuses if *path* is
    live, then holds only THIS database out of service: a :func:`connect_tracked` of *path* from
    another thread waits until the block exits, while opens and closes of any other database (and
    the global lifecycle lock) stay free. Keyed by the resolved main path, the key tracked
    connections use (``tracking_path`` when given); an opener that bypasses
    :func:`connect_tracked` is not excluded."""
    key = _key(path)
    with _live_lock:
        _wait_while_offline(key)
        _refuse_if_live(path, what)
        _offline_paths[key] = threading.get_ident()
    try:
        yield
    finally:
        with _live_lock:
            _offline_paths.pop(key, None)
            _offline_released.notify_all()
