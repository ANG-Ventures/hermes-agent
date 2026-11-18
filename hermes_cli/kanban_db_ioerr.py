"""One diagnostic row on the first SQLITE_IOERR a process sees on a kanban board (t_54b8e6cc).

The default board was zero-paged twice on 2026-11-17, each time right after a ``disk I/O error``
in every gateway's notifier. The logs kept only that text, so the writer could not be named. The
row records what names it next time: the extended error code (``SQLITE_IOERR_SHORT_READ`` = a read
past the end of a ``-wal`` the wal-index still points into), this process's descriptors on the
board files (an fd whose inode is no longer the one at the path, or has no links, means this
process is reading or writing a ``-wal``/``-shm`` someone else deleted), the files' current
inode/size, and the ``lsof`` holders. Logged once per process per board, at WARNING.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

_SEEN: set[str] = set()
_SEEN_LOCK = threading.Lock()
_SUFFIXES = ("", "-wal", "-shm")
_F_GETPATH = 50  # macOS fcntl: the path an fd was opened with


def is_ioerr(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.Error):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF == sqlite3.SQLITE_IOERR
    return "disk i/o error" in str(exc).lower()


def _fd_path(fd: int) -> Optional[str]:
    if sys.platform == "darwin":
        import fcntl

        try:
            raw = fcntl.fcntl(fd, _F_GETPATH, b"\0" * 1024)
        except OSError:
            return None
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _own_fds(db: str) -> list[dict]:
    """This process's descriptors on ``db`` / ``db-wal`` / ``db-shm``, with whether each still
    names the file at its path."""
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return []
    out = []
    for name in names:
        try:
            fd = int(name)
            st = os.fstat(fd)
        except (OSError, ValueError):
            continue
        path = _fd_path(fd)
        if not path or not path.startswith(db) or path[len(db):].removesuffix(" (deleted)") not in _SUFFIXES:
            continue
        try:
            at_path = os.stat(path.removesuffix(" (deleted)")).st_ino
        except OSError:
            at_path = None
        out.append({"fd": fd, "file": path[len(db):] or "db", "ino": st.st_ino, "nlink": st.st_nlink,
                    "size": st.st_size, "stale": st.st_nlink == 0 or at_path != st.st_ino})
    return out


def _files(db: str) -> dict:
    out: dict[str, Any] = {}
    for suffix in _SUFFIXES:
        try:
            st = os.stat(db + suffix)
            out[suffix or "db"] = {"ino": st.st_ino, "size": st.st_size}
        except OSError:
            out[suffix or "db"] = None
    return out


def _lsof_holders(db: str) -> Any:
    paths = [db + s for s in _SUFFIXES if os.path.exists(db + s)]
    try:
        proc = subprocess.run(["lsof", "-nP", "-Fpc", "--", *paths], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"
    holders, pid = [], None
    for line in proc.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c") and pid is not None:
            holders.append(f"{pid}:{line[1:]}")
    return holders


def note_board_ioerr(exc: BaseException, *, where: str, db_path: Path | str | None = None,
                     board: Optional[str] = None, conn: Any = None) -> Optional[dict]:
    """Log the diagnostic row for the first IOERR on this board in this process; ``None`` (and no
    work) for anything else or any later IOERR. Never raises."""
    try:
        if not is_ioerr(exc):
            return None
        if db_path is None:
            from hermes_cli import kanban_db as kb

            db_path = kb.kanban_db_path(board=board)
        db = os.path.realpath(os.fspath(db_path))
        with _SEEN_LOCK:
            if db in _SEEN:
                return None
            _SEEN.add(db)
        row = {
            "pid": os.getpid(), "where": where, "thread": threading.current_thread().name,
            "errorname": getattr(exc, "sqlite_errorname", None),
            "errorcode": getattr(exc, "sqlite_errorcode", None), "error": str(exc),
            "conn_id": hex(id(conn)) if conn is not None else None, "db": db,
            "files": _files(db), "own_fds": _own_fds(db), "lsof": _lsof_holders(db),
        }
        _log.warning("kanban board IOERR (first in this process): %s", json.dumps(row, sort_keys=True))
        return row
    except Exception:  # a diagnostic must never turn a tick failure into a crash
        _log.debug("kanban board IOERR diagnostic failed", exc_info=True)
        return None
