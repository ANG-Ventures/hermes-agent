"""Cross-board "home" index: one small sqlite answering "open cards of session X".

Why: the kanban-home-cards plugin needs, on a session's FIRST turn, every open
card whose ``tasks.session_id`` is in the session's home lineage -- across
every board.  Opening ~78 board databases on the turn path costs 1.5-2.8 s on
a loaded host (t_11f2cf60), so the turn path reads this ONE index instead and
the cost moves to writers.

Layout: ``<kanban_home>/kanban/home_index.db``::

    cards(board, task_id, session_id, status, title, last_comment,
          last_activity, ev, updated_at)     -- PK (board, task_id)
    boards(board, last_event_id, db_path)    -- per-board sync watermark
    meta(key, value)                         -- 'backfilled_at'

Write path (:func:`sync_after_commit`) runs from ``kanban_db.write_txn``
after every outer COMMIT -- the one transaction boundary that guarded
mutators AND the execution lane (dispatcher, reapers) already share.  It is
event-driven, not call-site driven: every task whose ``task_events`` id is
above the board's watermark is re-read from the COMMITTED board row and
upserted.  So a writer that records its event by any path (``_append_event``
or a raw INSERT) is picked up by the next commit on that board, and a
per-row ``ev`` (the task's max event id) makes concurrent flushes converge:
an older snapshot never overwrites a newer one.

A writer that changes a card WITHOUT an event row is not seen by the write
path; :func:`resync` (CLI ``kanban home-index``; daily cron) rebuilds from
all boards and reports that drift instead of trusting it away.

Best-effort by contract: nothing here may fail or slow a board write beyond
a short bounded wait; every error is swallowed (logged at debug).

Read path (:func:`open_cards`) is one indexed query.  The index is only
trusted after a full backfill (``meta.backfilled_at``); before that -- or if
the file is missing/unreadable -- callers get :class:`IndexUnavailable` and
must render nothing rather than scan boards.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

INDEX_FILENAME = "home_index.db"
# A writer never waits longer than this for the index's own lock.
WRITE_BUSY_MS = 250

OPEN_STATUSES: tuple[str, ...] = (
    "blocked", "review", "running", "ready", "todo", "triage", "scheduled",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    board         TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    status        TEXT NOT NULL,
    title         TEXT,
    last_comment  TEXT,
    last_activity INTEGER,
    ev            INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (board, task_id)
);
CREATE INDEX IF NOT EXISTS idx_cards_session ON cards(session_id, status);
CREATE TABLE IF NOT EXISTS boards (
    board         TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    db_path       TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Same projection the plugin used to run on every board (last comment,
# last activity across events/comments), now run once per written card.
_ROW_SQL = """
SELECT t.id, t.session_id, t.status, t.title,
       (SELECT c.body FROM task_comments c WHERE c.task_id = t.id
         ORDER BY c.created_at DESC, c.id DESC LIMIT 1) AS last_comment,
       MAX(t.created_at,
           COALESCE((SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = t.id), 0),
           COALESCE((SELECT MAX(c.created_at) FROM task_comments c WHERE c.task_id = t.id), 0)
       ) AS last_activity,
       COALESCE((SELECT MAX(e.id) FROM task_events e WHERE e.task_id = t.id), 0) AS ev
  FROM tasks t
"""

_UPSERT_SQL = """
INSERT INTO cards (board, task_id, session_id, status, title, last_comment,
                   last_activity, ev, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(board, task_id) DO UPDATE SET
    session_id = excluded.session_id,
    status = excluded.status,
    title = excluded.title,
    last_comment = excluded.last_comment,
    last_activity = excluded.last_activity,
    ev = excluded.ev,
    updated_at = excluded.updated_at
WHERE excluded.ev >= cards.ev
"""


class IndexUnavailable(Exception):
    """The index cannot answer (missing, not backfilled, unreadable)."""


# ── paths ───────────────────────────────────────────────────────────────────
def index_path() -> Path:
    from hermes_cli import kanban_db

    return kanban_db.kanban_home() / "kanban" / INDEX_FILENAME


def board_dbs() -> list[tuple[str, Path]]:
    """Every board database on this host: ``default`` + ``kanban/boards/*``."""
    from hermes_cli import kanban_db

    root = kanban_db.kanban_home()
    out: list[tuple[str, Path]] = [("default", root / "kanban.db")]
    boards = root / "kanban" / "boards"
    if boards.is_dir():
        for child in sorted(boards.iterdir(), key=lambda p: p.name.lower()):
            if child.name != "default":
                out.append((child.name, child / "kanban.db"))
    return [(s, p) for s, p in out if p.is_file() and p.stat().st_size > 0]


_SLUG_CACHE: dict[str, Optional[str]] = {}


def _board_slug_for(conn: sqlite3.Connection) -> Optional[str]:
    """Board slug of the database ``conn`` writes, or None when it is not a
    board of this kanban home (a pinned test/scratch DB): such writes are not
    indexed."""
    row = conn.execute("PRAGMA database_list").fetchone()
    file = row[2] if row else ""
    if not file:
        return None
    if file in _SLUG_CACHE:
        return _SLUG_CACHE[file]
    from hermes_cli import kanban_db

    slug: Optional[str] = None
    try:
        path = Path(file).resolve()
        root = kanban_db.kanban_home().resolve()
        if path == root / "kanban.db":
            slug = "default"
        elif path.name == "kanban.db" and path.parent.parent == root / "kanban" / "boards":
            slug = path.parent.name
    except OSError:
        slug = None
    if len(_SLUG_CACHE) > 512:
        _SLUG_CACHE.clear()
    _SLUG_CACHE[file] = slug
    return slug


def _open_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=WRITE_BUSY_MS / 1000.0,
                           isolation_level=None, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout = {WRITE_BUSY_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    return conn


def _is_backfilled(idx: sqlite3.Connection) -> bool:
    row = idx.execute("SELECT value FROM meta WHERE key = 'backfilled_at'").fetchone()
    return bool(row and row[0])


# ── write path ──────────────────────────────────────────────────────────────
# Per-process: board db file -> last event id this process already flushed.
# Lets a commit with nothing new (the common case) skip opening the index.
_FLUSHED: dict[str, int] = {}
_FLUSH_LOCK = threading.Lock()


def _apply_rows(idx: sqlite3.Connection, slug: str, rows: Iterable[Sequence[Any]],
                missing: Iterable[str] = ()) -> int:
    n = 0
    now = time.time()
    for r in rows:
        tid, sid, status, title, comment, act, ev = r
        if sid:
            idx.execute(_UPSERT_SQL, (slug, tid, sid, status, title, comment,
                                      act, int(ev or 0), now))
        else:
            # Unstamped (or un-stamped since): not in anyone's home.
            idx.execute("DELETE FROM cards WHERE board = ? AND task_id = ? AND ev <= ?",
                        (slug, tid, int(ev or 0)))
        n += 1
    for tid in missing:
        idx.execute("DELETE FROM cards WHERE board = ? AND task_id = ?", (slug, tid))
    return n


def sync_after_commit(conn: sqlite3.Connection) -> int:
    """Mirror every card written since the board's watermark into the index.

    Called by ``kanban_db.write_txn`` right after an outer COMMIT.  Never
    raises.  Returns the number of cards mirrored (tests/diagnostics).
    """
    try:
        slug = _board_slug_for(conn)
        if slug is None:
            return 0
        file = conn.execute("PRAGMA database_list").fetchone()[2]
        top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        with _FLUSH_LOCK:
            if _FLUSHED.get(file) == top:
                return 0
        path = index_path()
        if not path.is_file():
            return 0  # not installed: the backfill creates it
        idx = _open_rw(path)
        try:
            if not _is_backfilled(idx):
                return 0
            row = idx.execute("SELECT last_event_id FROM boards WHERE board = ?",
                              (slug,)).fetchone()
            wm = int(row[0]) if row else 0
            if wm > top:
                wm = 0  # board DB recreated (ids restarted): re-read it all
            ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT task_id FROM task_events WHERE id > ? AND id <= ?",
                (wm, top))]
            rows: list = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                rows.extend(conn.execute(
                    _ROW_SQL + f" WHERE t.id IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall())
            found = {r[0] for r in rows}
            idx.execute("BEGIN IMMEDIATE")
            try:
                n = _apply_rows(idx, slug, rows, [t for t in ids if t not in found])
                idx.execute(
                    "INSERT INTO boards (board, last_event_id, db_path) VALUES (?, ?, ?) "
                    "ON CONFLICT(board) DO UPDATE SET "
                    "last_event_id = MAX(boards.last_event_id, excluded.last_event_id), "
                    "db_path = excluded.db_path",
                    (slug, top, file),
                )
                idx.execute("COMMIT")
            except Exception:
                idx.execute("ROLLBACK")
                raise
        finally:
            idx.close()
        with _FLUSH_LOCK:
            _FLUSHED[file] = top
        return n
    except Exception as exc:  # the index is a mirror, never a gate
        logger.debug("kanban home index: sync skipped: %s", exc)
        return 0


# ── full resync / drift report (backfill + daily cron) ──────────────────────
def _board_rows(path: Path) -> tuple[list, int]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        rows = conn.execute(_ROW_SQL + " WHERE t.session_id IS NOT NULL "
                            "AND t.session_id != ''").fetchall()
        return rows, int(top)
    finally:
        conn.close()


def resync(*, check_only: bool = False) -> dict:
    """Compare (and unless ``check_only``, rebuild) the index from every board.

    Drift is counted on the identity that decides what a session is shown:
    ``(board, task_id) -> (session_id, status, title)``.  ``missing`` = on a
    board but not indexed, ``extra`` = indexed but gone/unstamped on the
    board, ``stale`` = both but different.  Display-only fields
    (last_comment / last_activity) are refreshed but not counted.
    """
    report: dict = {"boards": 0, "rows": 0, "missing": 0, "extra": 0,
                    "stale": 0, "errors": [], "samples": []}
    path = index_path()
    exists = path.is_file()
    if check_only and not exists:
        report["errors"].append("index missing")
        report["drift"] = None
        return report
    idx = _open_rw(path)
    try:
        dbs = board_dbs()
        seen_boards = set()
        for slug, bpath in dbs:
            try:
                rows, top = _board_rows(bpath)
            except sqlite3.Error as exc:
                report["errors"].append(f"{slug}: {exc}")
                continue
            seen_boards.add(slug)
            report["boards"] += 1
            report["rows"] += len(rows)
            want = {r[0]: (r[1], r[2], r[3]) for r in rows}
            have = {r[0]: (r[1], r[2], r[3]) for r in idx.execute(
                "SELECT task_id, session_id, status, title FROM cards WHERE board = ?",
                (slug,))}
            for tid, v in want.items():
                if tid not in have:
                    report["missing"] += 1
                    _sample(report, f"missing {slug}/{tid}")
                elif have[tid] != v:
                    report["stale"] += 1
                    _sample(report, f"stale {slug}/{tid} {have[tid][1]}->{v[1]}")
            for tid in have.keys() - want.keys():
                report["extra"] += 1
                _sample(report, f"extra {slug}/{tid}")
            if check_only:
                continue
            idx.execute("BEGIN IMMEDIATE")
            try:
                idx.execute("DELETE FROM cards WHERE board = ?", (slug,))
                _apply_rows(idx, slug, rows)
                idx.execute(
                    "INSERT INTO boards (board, last_event_id, db_path) VALUES (?, ?, ?) "
                    "ON CONFLICT(board) DO UPDATE SET last_event_id = excluded.last_event_id, "
                    "db_path = excluded.db_path",
                    (slug, top, str(bpath)),
                )
                idx.execute("COMMIT")
            except Exception:
                idx.execute("ROLLBACK")
                raise
        # Boards that vanished from disk.
        gone = [b for (b,) in idx.execute("SELECT DISTINCT board FROM cards")
                if b not in seen_boards and b not in {e.split(':')[0] for e in report["errors"]}]
        for b in gone:
            n = idx.execute("SELECT COUNT(*) FROM cards WHERE board = ?", (b,)).fetchone()[0]
            report["extra"] += n
            _sample(report, f"extra board {b} ({n} rows)")
            if not check_only:
                idx.execute("DELETE FROM cards WHERE board = ?", (b,))
                idx.execute("DELETE FROM boards WHERE board = ?", (b,))
        if not check_only and not report["errors"]:
            idx.execute(
                "INSERT INTO meta (key, value) VALUES ('backfilled_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(int(time.time())),),
            )
    finally:
        idx.close()
    with _FLUSH_LOCK:
        _FLUSHED.clear()
    report["drift"] = report["missing"] + report["extra"] + report["stale"]
    return report


def _sample(report: dict, line: str) -> None:
    if len(report["samples"]) < 20:
        report["samples"].append(line)


# ── read path (turn path) ───────────────────────────────────────────────────
def open_cards(session_ids: Sequence[str], *, timeout_s: float = 0.25) -> list[dict]:
    """Open cards homed at ``session_ids``: ONE indexed read, read-only.

    Raises :class:`IndexUnavailable` when the index is missing, not yet
    backfilled, or unreadable -- the caller renders nothing (never scans).
    """
    if not session_ids:
        return []
    path = index_path()
    if not path.is_file():
        raise IndexUnavailable("no-index")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                               timeout=timeout_s, check_same_thread=False)
    except sqlite3.Error as exc:
        raise IndexUnavailable(f"unreadable: {exc}") from exc
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")
        if not _is_backfilled(conn):
            raise IndexUnavailable("not-backfilled")
        ids = list(dict.fromkeys(session_ids))
        sql = (
            "SELECT board, task_id, status, title, last_comment, last_activity "
            f"FROM cards WHERE session_id IN ({','.join('?' * len(ids))}) "
            f"AND status IN ({','.join('?' * len(OPEN_STATUSES))})"
        )
        return [
            {"board": r[0], "id": r[1], "status": r[2], "title": r[3],
             "last_comment": r[4], "last_activity": r[5]}
            for r in conn.execute(sql, [*ids, *OPEN_STATUSES])
        ]
    except sqlite3.Error as exc:
        raise IndexUnavailable(f"unreadable: {exc}") from exc
    finally:
        conn.close()
