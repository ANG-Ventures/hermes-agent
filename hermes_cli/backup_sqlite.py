"""WAL-safe SQLite snapshots. Direct execution needs only the standard library.

Desktop invokes this file before stopping its backend, even when application
imports cannot load. Full and quick backups use the same SQLite copy operation.
"""
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class _SQLiteBackupTimeout(RuntimeError):
    """Raised when a SQLite snapshot remains busy past its deadline."""


# Bounds for a single SQLite safe-copy (fork). Two guards, both needed:
#
#   * STALL: the longest the copy may run without copying any page. Fast-fails a DB held under an
#     exclusive lock by another process (live Chrome profile), where not one page ever moves.
#   * BUDGET: an absolute wall-clock cap, SIZED TO THE DATABASE. Terminates the case the stall
#     guard structurally cannot: sqlite3's backup API RESTARTS the copy whenever the source is
#     written mid-backup, so a large DB under continuous writes can thrash — pages keep copying
#     (never a stall) while `remaining` keeps resetting and the copy never converges. Measured on
#     a live 3 GB state.db: repeated resets to the full page count, destination pinned at 65 MB
#     for 6+ minutes with no stall ever detected.
#
# BUDGET replaces a FIXED 60s wall-clock deadline, which did not scale with the database: a 3 GB
# live state.db takes ~39s of healthy copying (measured) — under 60s idle, over it under load —
# so the copy failed closed and `hermes backup --quick` shipped a snapshot with NO state.db
# (2026-09-20). The per-GB allowance is ~9x the measured healthy rate, so it bounds a pathological
# copy without ever aborting a merely large one.
_SAFE_COPY_STALL_DEADLINE_S = 60.0
_SAFE_COPY_BASE_BUDGET_S = 60.0
_SAFE_COPY_BUDGET_PER_GB_S = 120.0


def _safe_copy_budget_s(src: Path) -> float:
    """Absolute wall-clock budget for copying *src*, scaled by its size."""
    try:
        gb = src.stat().st_size / (1024 ** 3)
    except OSError:
        gb = 0.0
    return _SAFE_COPY_BASE_BUDGET_S + gb * _SAFE_COPY_BUDGET_PER_GB_S


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        with suppress(Exception):
            conn.close()


def _safe_copy_db(src: Path, dst: Path, *, timeout_seconds: float = 10.0) -> bool:
    """Copy a SQLite database with the backup() API (WAL-safe consistent snapshot).

    Fails closed when no consistent snapshot can be made: copying only the main file loses WAL data.
    Bounded so it can never hang the whole backup: a ``busy_timeout`` caps each lock wait, a STALL
    deadline aborts a copy that moves no pages at all, and a size-scaled BUDGET aborts a copy that
    keeps moving pages but never converges (sqlite restart-thrash under continuous writes); see
    ``_SAFE_COPY_STALL_DEADLINE_S``.
    """
    conn = backup_conn = None
    try:
        # sqlite3.connect() creates a missing destination with the process
        # umask, which is commonly 0022 (0644).  Snapshot databases contain
        # session and tool state, so create the inode owner-only before SQLite
        # writes its first byte.  O_NOFOLLOW also refuses a planted symlink on
        # platforms that support it.  Tighten an existing internal staging
        # file as well (NamedTemporaryFile callers already create it 0600).
        if os.name != "nt":
            open_flags = os.O_WRONLY | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            secure_fd = os.open(dst, open_flags, 0o600)
            try:
                os.fchmod(secure_fd, 0o600)
            finally:
                os.close(secure_fd)
        # timeout=0.0 disables sqlite3's implicit busy wait so the progress callback owns the
        # full locked-source deadline instead of adding the default timeout before each callback.
        conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0)
        backup_conn = sqlite3.connect(str(dst))
        now0 = time.monotonic()
        busy_deadline = now0 + max(0.0, timeout_seconds)
        stall_deadline = now0 + _SAFE_COPY_STALL_DEADLINE_S
        budget_s = _safe_copy_budget_s(src)
        budget_deadline = now0 + budget_s
        last_copied = -1

        def _check_backup_progress(status: int, remaining: int, total: int) -> None:
            nonlocal busy_deadline, stall_deadline, last_copied
            now = time.monotonic()
            # Size-scaled absolute cap: bounds a restart-thrashing copy that never stalls but
            # never converges either.
            if now > budget_deadline:
                try:
                    size_note = f"{src.stat().st_size / (1024 ** 3):.2f} GB"
                except OSError:
                    size_note = "unknown size"
                raise TimeoutError(
                    f"safe-copy exceeded its {budget_s:.0f}s budget for {size_note} "
                    f"({remaining}/{total} pages remaining; source written faster than it can be copied?)")
            # Stall cap: abort fast when NO PAGES are being copied at all. A sqlite backup RESTART
            # (source written mid-copy) resets `copied`, which differs from the previous value and
            # so counts as progress — work being redone, not a stall. The budget bounds that case.
            copied = total - remaining
            if copied != last_copied:
                last_copied = copied
                stall_deadline = now + _SAFE_COPY_STALL_DEADLINE_S
            elif now > stall_deadline:
                raise TimeoutError(
                    f"safe-copy copied no pages for {_SAFE_COPY_STALL_DEADLINE_S}s "
                    f"({remaining}/{total} pages remaining; locked by another process?)")
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if now >= busy_deadline:
                    raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds")
            else:
                busy_deadline = now + max(0.0, timeout_seconds)

        conn.backup(backup_conn, pages=256, progress=_check_backup_progress, sleep=0.1)
        return True
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        # Windows won't remove the partial destination while SQLite still has it open.
        _close_quietly(backup_conn)
        backup_conn = None
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    finally:
        _close_quietly(backup_conn)
        _close_quietly(conn)


def preflight_state_db(home: Path) -> dict:
    """Publish an emergency snapshot; do not prune recovery files on failure."""
    source = home / "state.db"
    if not source.exists():
        return {"path": None, "message": "state.db not found (fresh install?)"}
    prefix = "state.db.pre-update-emergency-"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    destination = home / f"{prefix}{stamp}-{os.getpid()}.bak"
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".partial", dir=home)
    os.close(fd)
    staged = Path(name)
    try:
        if not _safe_copy_db(source, staged):
            raise RuntimeError("SQLite safe copy failed; previous emergency snapshots were retained")
        connection = sqlite3.connect(str(staged))
        try:
            result = connection.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise RuntimeError(f"SQLite snapshot integrity check failed: {result}")
        finally:
            connection.close()
        size = staged.stat().st_size
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    for old in sorted(home.glob(f"{prefix}*.bak"), reverse=True)[2:]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Could not prune emergency snapshot %s: %s", old, exc)
    return {"path": str(destination), "bytes": size}


if __name__ == "__main__":
    print(json.dumps(preflight_state_db(Path(sys.argv[1]))))
