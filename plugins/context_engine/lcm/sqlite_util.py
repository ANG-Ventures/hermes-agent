"""SQLite lock-contention helpers shared by the LCM engine.

Isolated from ``engine.py`` (WS5 seam): lock-contention detection, bounded
``busy_timeout`` changes, and transaction-preserving savepoints are pure SQLite
concerns with no engine state. Callers keep their own policy constants (for
example the session-end timeout budget).
"""

from __future__ import annotations

import functools
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Iterator, List


def _is_sqlite_locked_error(exc: BaseException) -> bool:
    """Return True when an exception chain represents SQLite lock contention."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if isinstance(current, sqlite3.Error) and "locked" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _sqlite_busy_timeout_ms(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


@contextmanager
def _sqlite_savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """Isolate helper writes without taking ownership of a caller transaction."""
    # UUID hex contains only identifier-safe characters and keeps every nested
    # helper's SAVEPOINT name unique with a fixed upper bound on name length.
    name = f"lcm_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        finally:
            conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


@contextmanager
def _temporary_sqlite_busy_timeout(
    connections: List[sqlite3.Connection | None],
    timeout_ms: int,
) -> Iterator[None]:
    """Temporarily bound SQLite lock waits for gateway-critical paths."""
    bounded_timeout = max(0, int(timeout_ms))
    originals: list[tuple[sqlite3.Connection, int]] = []
    for conn in connections:
        if conn is None:
            continue
        original = _sqlite_busy_timeout_ms(conn)
        conn.execute(f"PRAGMA busy_timeout={bounded_timeout}")
        originals.append((conn, original))
    try:
        yield
    finally:
        for conn, original in reversed(originals):
            conn.execute(f"PRAGMA busy_timeout={original}")


def connect_read(database: str, **kwargs) -> sqlite3.Connection:
    """Open an LCM READ connection: ``sqlite3.connect`` plus ``PRAGMA temp_store = MEMORY``.

    Every read connection in this plugin goes through here (contract test:
    tests/context_engine/test_lcm_read_connections_temp_store.py). With the default temp_store a
    search's ORDER BY / GROUP BY B-trees spill to etilqs_* files once they outgrow the page
    cache: on a copy of the Studio's 14 GB lcm.db one lcm_grep FTS read wrote up to 7.4 GB of
    temp files (660 MB inside the 3 s hybrid deadline; t_188b19e8). With MEMORY the same 24
    queries wrote nothing and peak RSS rose from 310 MB to 458 MB.
    """
    conn = sqlite3.connect(database, **kwargs)
    try:
        conn.execute("PRAGMA temp_store = MEMORY")
    except BaseException:
        conn.close()
        raise
    return conn


class StoreClosed(sqlite3.Error):
    """The connection was closed; the call never touched SQLite.

    Subclasses ``sqlite3.Error`` so existing fail-open ``except sqlite3.Error`` callers keep working.
    """


class GuardedConnection:
    """A ``check_same_thread=False`` connection whose every use and whose ``close()`` share one lock.

    Closing a sqlite3 connection while another thread is inside ``execute``/``fetch`` on it is a
    use-after-free in ``_sqlite3`` (gateway SIGSEGV 2026-11-02, t_055f9d8d; 2026-11-10 in the
    lifecycle store, t_35b4c55d). Every long-lived LCM connection is wrapped at ``sqlite3.connect``
    so the rule holds for store methods and for raw ``store.connection`` users alike: a call waits
    for an in-flight one, and once ``close()`` ran every call raises :class:`StoreClosed`.
    The lock is a leaf: nothing acquires another lock while holding it.
    """

    __slots__ = ("_raw", "_guard_lock", "_closed")

    def __init__(self, raw: sqlite3.Connection) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_guard_lock", threading.RLock())
        object.__setattr__(self, "_closed", False)

    def _live(self) -> sqlite3.Connection:
        if self._closed:
            raise StoreClosed("LCM sqlite connection is closed")
        return self._raw

    def _call(self, name: str, *args, **kwargs):
        with self._guard_lock:
            result = getattr(self._live(), name)(*args, **kwargs)
            return GuardedCursor(result, self) if isinstance(result, sqlite3.Cursor) else result

    def __getattr__(self, name: str):
        if name in GuardedConnection.__slots__:  # half-built instance: never recurse
            raise AttributeError(name)
        with self._guard_lock:
            value = getattr(self._live(), name)
        if callable(value):
            return functools.partial(self._call, name)
        return value

    def __setattr__(self, name: str, value) -> None:
        with self._guard_lock:
            setattr(self._live(), name, value)

    def __enter__(self):
        with self._guard_lock:
            self._live().__enter__()
        return self

    def __exit__(self, *exc):
        with self._guard_lock:
            return self._live().__exit__(*exc)

    def backup(self, target, *args, **kwargs):
        if isinstance(target, GuardedConnection):
            with target._guard_lock:
                return self._call("backup", target._live(), *args, **kwargs)
        return self._call("backup", target, *args, **kwargs)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._guard_lock:
            if self._closed:
                return
            object.__setattr__(self, "_closed", True)
            self._raw.close()


class GuardedCursor:
    """A cursor from a :class:`GuardedConnection`; fetching runs under the connection's lock."""

    __slots__ = ("_raw", "_conn")

    def __init__(self, raw: sqlite3.Cursor, conn: GuardedConnection) -> None:
        self._raw = raw
        self._conn = conn

    def _call(self, name: str, *args, **kwargs):
        with self._conn._guard_lock:
            self._conn._live()
            result = getattr(self._raw, name)(*args, **kwargs)
            return self if result is self._raw else result

    def __getattr__(self, name: str):
        with self._conn._guard_lock:
            self._conn._live()
            value = getattr(self._raw, name)
        if callable(value):
            return functools.partial(self._call, name)
        return value

    def __iter__(self):
        return self

    def __next__(self):
        with self._conn._guard_lock:
            self._conn._live()
            return next(self._raw)


def guard_connection(raw: sqlite3.Connection) -> GuardedConnection:
    """Wrap a long-lived shared connection; see :class:`GuardedConnection`."""
    return GuardedConnection(raw)
