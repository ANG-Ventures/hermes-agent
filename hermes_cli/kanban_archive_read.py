"""Read fallback for a card's comments/runs/events moved into the board's archive files (t_3be5b45f).

hermes-home ``scripts/kanban-events-retention.py`` moves the comments and runs of done/archived cards
completed more than 14 days ago into ``<board dir>/kanban-archive.db`` (``archived_comments``,
``archived_runs``; text columns stored as zlib(utf-8) in ``<col>_z``) and their events into
``<board dir>/kanban-events-archive.db`` (``archived_events``, ``payload_z``; only
``rule='R4_terminal_full'`` rows are card history). The ``tasks`` row stays on the board.

:func:`fill` gives ``kanban show`` / the ``kanban_show`` tool those rows back for ONE task. It never
touches the caller's (writer) connection beyond a read of ``PRAGMA database_list`` and a COUNT: each
archive is opened on its own ``mode=ro`` connection and closed again. It only reads the archive for a
terminal task (done/archived) whose board has no rows in that table, and it is a no-op when the archive
file is absent. Any archive error degrades to the board rows (logged at debug), never a failed show.
Same shape as hermes-home ``scripts/lib/kanban_archive.py`` (shadow views), without the TEMP views.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import zlib
from typing import Any, Optional

logger = logging.getLogger(__name__)

COMMENTS_ARCHIVE = "kanban-archive.db"
EVENTS_ARCHIVE = "kanban-events-archive.db"
TERMINAL = ("done", "archived")

# board table -> (archive file, archive table, columns, zlib columns, extra WHERE, ORDER BY)
_SPECS = {
    "task_comments": (COMMENTS_ARCHIVE, "archived_comments",
                      ("id", "task_id", "author", "body", "created_at", "run_id", "session_ref"),
                      ("body",), "", "created_at ASC, id ASC"),
    "task_runs": (COMMENTS_ARCHIVE, "archived_runs",
                  ("id", "task_id", "profile", "step_key", "status", "claim_lock", "claim_expires",
                   "worker_pid", "max_runtime_seconds", "last_heartbeat_at", "started_at", "ended_at",
                   "outcome", "summary", "metadata", "error", "host"),
                  ("summary", "metadata", "error"), "", "started_at ASC, id ASC"),
    "task_events": (EVENTS_ARCHIVE, "archived_events",
                    ("id", "task_id", "run_id", "kind", "payload", "created_at", "actor_profile",
                     "actor_session_id"),
                    ("payload",), " AND rule = 'R4_terminal_full'", "created_at ASC, id ASC"),
}


def _unz(v: Any) -> Optional[str]:
    return None if v is None else zlib.decompress(v).decode("utf-8")


def _board_dir(conn: sqlite3.Connection) -> Optional[str]:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return os.path.dirname(os.path.abspath(row[2]))
    return None  # in-memory board: no archive can sit next to it


def archived_rows(board_dir: str, table: str, task_id: str) -> list[sqlite3.Row]:
    """Archived rows of ``table`` for one task, shaped like the board table's rows (zlib decoded).
    Empty when the archive file or table is absent."""
    fname, atab, cols, zcols, extra, order = _SPECS[table]
    path = os.path.join(board_dir, fname)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return []
    uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
    ac = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        ac.row_factory = sqlite3.Row
        ac.create_function("kanban_unz", 1, _unz, deterministic=True)
        have = {r[1] for r in ac.execute("PRAGMA table_info(%s)" % atab)}
        if not have:
            return []
        sel = []
        for c in cols:
            if c in zcols and c + "_z" in have:
                sel.append("kanban_unz(%s_z) AS %s" % (c, c))
            elif c in have:
                sel.append(c)
            else:
                sel.append("NULL AS %s" % c)
        return ac.execute("SELECT %s FROM %s WHERE task_id = ?%s ORDER BY %s"
                          % (", ".join(sel), atab, extra, order), (task_id,)).fetchall()
    finally:
        ac.close()


def fill(kb, conn: sqlite3.Connection, task, comments: list, runs: list, events: list, *,
         include_active: bool = True, state_type: Optional[str] = None,
         state_name: Optional[str] = None) -> tuple[list, list, list]:
    """-> (comments, runs, events), each topped up from the archive when the task is terminal and the
    board holds no rows of that kind for it. Run filters mirror :func:`kanban_db.list_runs`."""
    if task is None or task.status not in TERMINAL:
        return comments, runs, events
    out = {"task_comments": comments, "task_runs": runs, "task_events": events}
    try:
        board_dir = _board_dir(conn)
        if board_dir is None:
            return comments, runs, events
        for table, model in (("task_comments", kb.Comment), ("task_runs", kb.Run), ("task_events", kb.Event)):
            if conn.execute("SELECT 1 FROM %s WHERE task_id = ? LIMIT 1" % table, (task.id,)).fetchone():
                continue  # board still owns this card's rows: never mix, never double-count
            rows = [model.from_row(r) for r in archived_rows(board_dir, table, task.id)]
            if table == "task_runs":
                if not include_active:
                    rows = [r for r in rows if r.ended_at is not None]
                if state_type is not None:
                    rows = [r for r in rows if getattr(r, state_type) == state_name]
            if rows:
                out[table] = rows
    except (sqlite3.Error, zlib.error, UnicodeDecodeError, OSError):
        logger.debug("kanban archive read fallback failed for %s", getattr(task, "id", "?"), exc_info=True)
        return comments, runs, events
    return out["task_comments"], out["task_runs"], out["task_events"]
