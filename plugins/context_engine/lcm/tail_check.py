"""Bounded open-time tail check for lcm.db after an unclean gateway exit.

2026-11-11: a Mac Studio kernel panic left ~58 torn pages at the END of a 34 GB
lcm.db (the last pages a checkpoint wrote). Every later compaction aborted with
"database disk image is malformed" for hours and nothing said why. A full
``PRAGMA integrity_check`` on that file takes minutes, so it cannot run at open.

This module runs a check bounded by a VM-step budget and a wall-clock deadline,
and only when the gateway lifecycle sentinel says the previous gateway life died
uncleanly (``prior_unclean_exit``), or when the last check of this store failed
(so a repaired store clears on its next open). It reads only through SQLite, so
it never opens a raw fd on a live database (a raw ``close()`` would cancel this
process's POSIX locks).

Two probes:

* right-edge walk: for every b-tree (tables incl. FTS shadow tables, plain
  indexes) read the LAST ``rows_per_btree`` entries in key order, all columns.
  Appended rows land on the right edge of a rowid table, so this reads the
  pages the last checkpoints wrote for ``messages`` / ``messages_fts_data`` /
  ``summary_nodes``. It cannot see torn index pages in the middle of the key
  range (an index insert lands wherever its key sorts).
* page-header scan (only when the SQLite build has ``sqlite_dbpage``): the
  last ``tail_pages`` pages of the file, read through the pager (WAL-aware).
  Flags a page whose type byte says b-tree but whose header is impossible.
  (An all-zero page is NOT flagged: a valid overflow page of a zero-filled
  BLOB is all zeros and is not on the freelist.)

Neither probe proves the whole file is sound. A failure is a strong signal;
a pass means "the tail the last checkpoints wrote reads back".

Result is published three ways: ``MessageStore.tail_health`` (engine status),
``<HERMES_HOME>/state/lcm-store-health.json`` keyed by db path (monitors),
and an ERROR log line. Nothing is repaired here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import struct
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HEALTH_MARKER_RELATIVE = ("state", "lcm-store-health.json")
LIFECYCLE_SENTINEL_RELATIVE = ("state", "gateway.lifecycle.json")

DEFAULT_ROWS_PER_BTREE = 64
DEFAULT_TAIL_PAGES = 1024
DEFAULT_DEADLINE_S = 10.0
DEFAULT_VM_STEP_BUDGET = 50_000_000
_PROGRESS_OPS = 10_000
_MAX_BAD_PAGES_REPORTED = 50

_BTREE_PAGE_TYPES = (0x02, 0x05, 0x0A, 0x0D)

STATUS_OK = "ok"
STATUS_UNHEALTHY = "unhealthy"
STATUS_INCOMPLETE = "incomplete"


class _Budget(Exception):
    pass


def health_marker_path(hermes_home: str | Path) -> Path:
    return Path(hermes_home).joinpath(*HEALTH_MARKER_RELATIVE)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _db_key(db_path: str | Path) -> str:
    try:
        return str(Path(db_path).resolve())
    except OSError:
        return str(db_path)


def read_store_health(hermes_home: str | Path, db_path: str | Path) -> Optional[Dict[str, Any]]:
    marker = _read_json(health_marker_path(hermes_home)) or {}
    stores = marker.get("stores")
    if not isinstance(stores, dict):
        return None
    entry = stores.get(_db_key(db_path))
    return entry if isinstance(entry, dict) else None


class _MarkerLock:
    """Cross-process exclusive lock around the marker read-modify-replace (flock on a
    sidecar file). Best-effort on platforms without fcntl."""

    def __init__(self, path: Path) -> None:
        self._path = path.with_name(path.name + ".lock")
        self._fd: Optional[int] = None

    def __enter__(self) -> "_MarkerLock":
        self._fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)  # releases the flock
            self._fd = None


def _write_store_health(hermes_home: str | Path, db_path: str | Path, entry: Dict[str, Any]) -> None:
    path = health_marker_path(hermes_home)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _MarkerLock(path):
            _replace_store_entry(path, db_path, entry)
    except OSError:
        logger.warning("LCM could not write store health marker %s", path, exc_info=True)


def _replace_store_entry(path: Path, db_path: str | Path, entry: Dict[str, Any]) -> None:
    marker = _read_json(path) or {}
    stores = marker.get("stores") if isinstance(marker.get("stores"), dict) else {}
    stores[_db_key(db_path)] = entry
    marker["stores"] = stores
    marker["updated_at"] = entry.get("checked_at")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lcm-store-health.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _process_sentinel_path() -> Optional[Path]:
    """The gateway PROCESS lifecycle sentinel (owned by the process home, not by the
    profile a store belongs to: a multiplexed gateway claims it once)."""
    try:
        from gateway.lifecycle_ledger import get_lifecycle_sentinel_path

        return get_lifecycle_sentinel_path()
    except Exception:
        return None


def unclean_boot_key(hermes_home: str | Path) -> Optional[str]:
    """Key of the current gateway life when it booted after an unclean exit, else None.

    Reads the process sentinel first, then the store's own profile home (a gateway
    launched from that profile, or a standalone install)."""
    candidates = [_process_sentinel_path(), Path(hermes_home).joinpath(*LIFECYCLE_SENTINEL_RELATIVE)]
    seen = set()
    for path in candidates:
        if path is None or str(path) in seen:
            continue
        seen.add(str(path))
        sentinel = _read_json(path)
        if sentinel and sentinel.get("prior_unclean_exit"):
            return f"{sentinel.get('pid')}:{sentinel.get('start_time') or sentinel.get('started_at')}"
    return None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _right_edge_walk(conn: sqlite3.Connection, rows: int, bad: List[str], tick=lambda: None) -> int:
    """Read the last ``rows`` entries of every b-tree; return how many b-trees were read."""
    objects = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND rootpage > 0 ORDER BY rootpage"
    ).fetchall()
    walked = 0
    for kind, name, tbl_name, sql in objects:
        name = str(name)
        tick()
        try:
            if kind == "table":
                if name.startswith("sqlite_") and name != "sqlite_sequence":
                    continue
                if sql and re.search(r"\bWITHOUT\s+ROWID\b", str(sql), re.IGNORECASE):
                    pk = [str(r[1]) for r in sorted(
                        (r for r in conn.execute(f"PRAGMA table_info({_quote(name)})") if r[5]),
                        key=lambda r: r[5])]
                    order = ", ".join(f"{_quote(c)} DESC" for c in pk)
                else:
                    order = "rowid DESC"
                for _ in conn.execute(f"SELECT * FROM {_quote(name)} ORDER BY {order} LIMIT ?", (rows,)):
                    pass
            else:
                if sql is None or re.search(r"\bWHERE\b", str(sql), re.IGNORECASE):
                    continue  # autoindex or partial index: no safe INDEXED BY scan
                info = conn.execute(f"PRAGMA index_xinfo({_quote(name)})").fetchall()
                key_cols = [r for r in info if r[5] == 1]
                if not key_cols or any(r[2] is None for r in key_cols):
                    continue  # expression index
                order = ", ".join(
                    f"{_quote(str(r[2]))} COLLATE {r[4]} {'ASC' if r[3] else 'DESC'}" for r in key_cols
                )
                cols = ", ".join(_quote(str(r[2])) for r in key_cols)
                stmt = (f"SELECT {cols} FROM {_quote(str(tbl_name))} INDEXED BY {_quote(name)} "
                        f"ORDER BY {order} LIMIT ?")
                plan = " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + stmt, (rows,)))
                if "TEMP B-TREE" in plan.upper():
                    continue  # would sort the whole index: not bounded
                for _ in conn.execute(stmt, (rows,)):
                    pass
            walked += 1
        except _Budget:
            raise
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError) and "interrupted" in str(exc).lower():
                raise _Budget() from exc
            if isinstance(exc, sqlite3.OperationalError) and "no such module" in str(exc).lower():
                continue  # virtual table whose module is not loaded here
            bad.append(f"{kind}:{name}: {exc}")
    return walked


def _has_dbpage(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM sqlite_dbpage WHERE pgno = 1").fetchone()
        return True
    except sqlite3.DatabaseError:
        return False


def _page(conn: sqlite3.Connection, pgno: int) -> Optional[bytes]:
    row = conn.execute("SELECT data FROM sqlite_dbpage WHERE pgno = ?", (pgno,)).fetchone()
    return bytes(row[0]) if row and row[0] is not None else None


def _header_ok(data: bytes, offset: int, page_size: int, usable: int) -> bool:
    kind = data[offset]
    head = 12 if kind in (0x02, 0x05) else 8
    if len(data) < offset + head:
        return False
    first_free, ncells, content_start, frag = struct.unpack(">HHHB", data[offset + 1:offset + 8])
    content_start = content_start or 65536
    ptr_end = offset + head + 2 * ncells
    if ptr_end > usable or content_start < ptr_end or content_start > usable or frag > 60:
        return False
    if first_free and (first_free < ptr_end or first_free > usable - 4):
        return False
    for i in range(min(ncells, (usable - offset - head) // 2)):
        cell = struct.unpack(">H", data[offset + head + 2 * i:offset + head + 2 * i + 2])[0]
        if cell < content_start or cell >= usable:
            return False
    if kind in (0x02, 0x05):
        right = struct.unpack(">I", data[offset + 8:offset + 12])[0]
        if right == 0:
            return False
    return True


def _page_scan(conn: sqlite3.Connection, tail_pages: int, bad: List[str], tick=lambda: None) -> int:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    header = _page(conn, 1) or b""
    reserved = header[20] if len(header) > 20 else 0
    usable = page_size - reserved
    first = max(2, page_count - tail_pages + 1)
    scanned = 0
    for pgno in range(first, page_count + 1):
        tick()
        data = _page(conn, pgno)
        scanned += 1
        if data is None:
            bad.append(f"page:{pgno}: unreadable")
            continue
        kind = data[0]
        if kind in _BTREE_PAGE_TYPES:
            if not _header_ok(data, 0, page_size, usable):
                bad.append(f"page:{pgno}: b-tree header invalid (type 0x{kind:02x})")
    return scanned


def check_tail(
    conn: sqlite3.Connection,
    *,
    rows_per_btree: int = DEFAULT_ROWS_PER_BTREE,
    tail_pages: int = DEFAULT_TAIL_PAGES,
    deadline_s: float = DEFAULT_DEADLINE_S,
    vm_step_budget: int = DEFAULT_VM_STEP_BUDGET,
    use_dbpage: bool = True,
) -> Dict[str, Any]:
    """Run both probes on ``conn``; never raises. Returns the health entry."""
    started = time.monotonic()
    steps = 0
    # The progress handler counts per statement; short statements may never reach the
    # interval, so the budget/deadline is ALSO checked explicitly between statements.
    interval = max(1, min(_PROGRESS_OPS, int(vm_step_budget)))

    def _over() -> bool:
        return steps > vm_step_budget or time.monotonic() - started > deadline_s

    def _progress() -> int:
        nonlocal steps
        steps += interval
        return 1 if _over() else 0

    def _tick() -> None:
        nonlocal steps
        steps += 1
        if _over():
            raise _Budget()

    bad: List[str] = []
    result: Dict[str, Any] = {"btrees_walked": 0, "pages_checked": 0, "dbpage": False}
    conn.set_progress_handler(_progress, interval)
    try:
        try:
            result["btrees_walked"] = _right_edge_walk(conn, rows_per_btree, bad, _tick)
            if use_dbpage and _has_dbpage(conn):
                result["dbpage"] = True
                result["pages_checked"] = _page_scan(conn, tail_pages, bad, _tick)
            status = STATUS_UNHEALTHY if bad else STATUS_OK
        except _Budget:
            status = STATUS_UNHEALTHY if bad else STATUS_INCOMPLETE
            result["reason"] = "budget_exhausted"
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                status = STATUS_UNHEALTHY if bad else STATUS_INCOMPLETE
                result["reason"] = "budget_exhausted"
            else:
                bad.append(f"check: {exc}")
                status = STATUS_UNHEALTHY
    finally:
        conn.set_progress_handler(None, 0)
    result["status"] = status
    result["bad_pages"] = bad[:_MAX_BAD_PAGES_REPORTED]
    result["bad_count"] = len(bad)
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    if bad and "reason" not in result:
        result["reason"] = bad[0]
    return result


def maybe_check_store_tail(
    conn: sqlite3.Connection, db_path: str | Path, hermes_home: str | Path, **kwargs: Any
) -> Optional[Dict[str, Any]]:
    """Run :func:`check_tail` when this gateway life follows an unclean exit (once per
    life per store) or when the store's last recorded check was not ``ok``.

    Returns the health entry, or ``None`` when no check was due. Never raises.
    """
    try:
        boot_key = unclean_boot_key(hermes_home)
        previous = read_store_health(hermes_home, db_path)
        # Only a FAILED check re-runs on every open (so a repaired store clears).
        # ok / incomplete run once per gateway life.
        previously_bad = bool(previous) and previous.get("status") == STATUS_UNHEALTHY
        if boot_key is None and not previously_bad:
            return None
        if boot_key is not None and not previously_bad and previous and previous.get("boot_key") == boot_key:
            return previous
        entry = check_tail(conn, **kwargs)
        entry.update({
            "db_path": _db_key(db_path),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "boot_key": boot_key,
            "trigger": "unclean_exit" if boot_key else "previous_check_failed",
        })
        _write_store_health(hermes_home, db_path, entry)
        if entry["status"] == STATUS_UNHEALTHY:
            logger.error(
                "LCM store FAILED its tail check after an unclean exit: db=%s bad=%d first=%s "
                "(marker %s). The store is damaged; compaction and ingest will fail until it is "
                "repaired. Run `/lcm doctor`.",
                entry["db_path"], entry["bad_count"], entry.get("reason"), health_marker_path(hermes_home),
            )
        elif entry["status"] == STATUS_INCOMPLETE:
            logger.warning("LCM store tail check hit its budget before finishing: db=%s elapsed=%ss",
                           entry["db_path"], entry["elapsed_s"])
        else:
            logger.info("LCM store tail check ok: db=%s btrees=%d pages=%d elapsed=%ss",
                        entry["db_path"], entry["btrees_walked"], entry["pages_checked"], entry["elapsed_s"])
        return entry
    except Exception:
        logger.warning("LCM store tail check could not run for %s", db_path, exc_info=True)
        return None
