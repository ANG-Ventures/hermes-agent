"""Answer a read of a QUIET SQLite database from memory instead of reopening it.

Why (t_2cfa2782, measured on the Mac Studio): each read-only open+close of an idle WAL database
whose ``-shm`` no other connection holds writes about 37 KB to disk. SQLite takes the DMS lock
alone, ftruncates the ``-shm`` (APFS ``ino_shrink`` zero-fills one block) and rebuilds the
wal-index in the mapped pages; the last close then fsyncs those dirty pages
(``apfs_vnop_close -> VNOP_FSYNC``). Lab: 542 open/close cycles = 20.0 MB and 1,625 writes; the
same reads behind this memo = 1 open, 36,864 B. Each gateway's kanban notifier probes every board
like that every few seconds, which made the board ``-shm`` files 100-190 GB/day of SSD writes.

The key is a ``stat`` of the database, taken only when the ``-wal`` is absent or empty and no
``-journal`` exists. Then every committed row is in the main file, and nothing can change the
answer without changing the key: a commit appends to the ``-wal`` (size > 0, no key), and a
checkpoint or a rollback-mode commit writes the main file (mtime/size/ctime move). The ``-wal`` is
stat'ed BEFORE the main file, so a key that matches proves the content as of that moment. A value
is stored only when the key was the same before and after the read, and only once the file's mtime
is ``SETTLE_S`` old, so a coarse filesystem clock cannot hide a second write in the same tick
(the racy-git rule). Anything else, including any ``stat`` error, just reads the database.

``HERMES_SQLITE_QUIET_MEMO=0`` turns it off (every call reads the database, as before).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Hashable, Optional, TypeVar

T = TypeVar("T")

SETTLE_S = 2.0
_MAX_ENTRIES = 4096
_lock = threading.Lock()
_memo: dict[tuple[str, Hashable], tuple[tuple, Any]] = {}


def quiet_key(db_path: Path | str) -> Optional[tuple]:
    """A stat key that pins the database's committed content, or None when it cannot."""
    p = os.fspath(db_path)
    try:
        wal = os.stat(p + "-wal")
    except FileNotFoundError:
        wal = None
    except OSError:
        return None
    if wal is not None and wal.st_size:
        return None
    if os.path.lexists(p + "-journal"):
        return None
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _enabled() -> bool:
    return os.environ.get("HERMES_SQLITE_QUIET_MEMO", "1").strip() != "0"


def read(db_path: Path | str, what: Hashable, compute: Callable[[], T]) -> T:
    """Return ``compute()`` for ``db_path``, or the value it returned last time if the database
    has not changed since. ``what`` names the query (include its parameters). An exception from
    ``compute`` propagates and nothing is stored."""
    if not _enabled():
        return compute()
    slot = (os.fspath(db_path), what)
    key = quiet_key(db_path)
    if key is not None:
        with _lock:
            hit = _memo.get(slot)
        if hit is not None and hit[0] == key:
            return hit[1]
    value = compute()
    if key is not None and key[3] <= time.time_ns() - int(SETTLE_S * 1e9) and quiet_key(db_path) == key:
        with _lock:
            if len(_memo) >= _MAX_ENTRIES:
                _memo.clear()
            _memo[slot] = (key, value)
    return value


def clear() -> None:
    with _lock:
        _memo.clear()
