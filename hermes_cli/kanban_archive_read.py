"""Read-only view of a card's rows moved off the board into ``kanban-archive.db``.

The fleet retention job (hermes-home ``scripts/kanban-events-retention.py`` rule R8, t_d1b5fc83)
moves the comments and runs of done/archived cards completed more than 14 days ago from
``kanban.db`` into ``kanban-archive.db`` beside it. It keeps the ``tasks`` row. The archive
tables use the live table names and columns, plus ``moved_at``. ``kanban show`` (CLI and the
``kanban_show`` tool) is the one reader that lists them, labelled archived. Every other reader
works on live cards and is unchanged.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

ARCHIVE_NAME = "kanban-archive.db"
_COMMENT_COLS = ("id", "author", "body", "created_at", "run_id")
_RUN_COLS = ("id", "profile", "status", "outcome", "summary", "error", "started_at", "ended_at")


def _archive_path(conn: sqlite3.Connection) -> Path | None:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return Path(path).parent / ARCHIVE_NAME
    return None


_TIMEOUT_S = 5.0


def _rows(arch: sqlite3.Connection, table: str, cols: tuple, task_id: str, order: str,
          where: str = "", params: tuple = ()) -> list[dict[str, Any]]:
    have = {r[1] for r in arch.execute("PRAGMA table_info(%s)" % table)}
    if not have:
        return []
    sel = ", ".join(c if c in have else "NULL AS %s" % c for c in cols)
    cur = arch.execute("SELECT %s FROM %s WHERE task_id = ?%s ORDER BY %s" % (sel, table, where, order),
                       (task_id, *params))
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def archived_rows(conn: sqlite3.Connection, task_id: str, *, state_type: str | None = None,
                  state_name: str | None = None) -> dict[str, Any]:
    """``{"comments": [...], "runs": [...]}`` from the board's archive file, empty when there is none.

    ``state_type``/``state_name`` filter the runs exactly like ``kanban_db.list_runs``. An archive that
    exists but cannot be read (locked, corrupt) adds ``"error": "<sqlite3 error class>: <message>"``
    so a failed read never looks like a card with no archived history."""
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None and state_type not in ("status", "outcome"):
        raise ValueError("state_type must be 'status' or 'outcome'")
    path = _archive_path(conn)
    if path is None or not path.is_file():
        return {"comments": [], "runs": []}
    run_where, run_params = ("", ()) if state_type is None else (" AND %s = ?" % state_type, (state_name,))
    arch = None
    try:
        arch = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_TIMEOUT_S)
        return {"comments": _rows(arch, "task_comments", _COMMENT_COLS, task_id, "created_at, id"),
                "runs": _rows(arch, "task_runs", _RUN_COLS, task_id, "started_at, id", run_where, run_params)}
    except sqlite3.Error as exc:
        return {"comments": [], "runs": [], "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if arch is not None:
            arch.close()


def present(archived: dict) -> bool:
    return bool(archived.get("comments") or archived.get("runs") or archived.get("error"))


def text_lines(archived: dict, fmt_ts) -> list[str]:
    """The ``kanban show`` text block; every line says archived."""
    out = []
    if archived.get("error"):
        out += ["", "archived history unreadable: %s (%s)" % (archived["error"], ARCHIVE_NAME)]
    if archived.get("comments"):
        out += ["", "Archived comments (%d, %s):" % (len(archived["comments"]), ARCHIVE_NAME)]
        out += ["  [%s] [archived] %s: %s" % (fmt_ts(c["created_at"]), c["author"], c["body"])
                for c in archived["comments"]]
    if archived.get("runs"):
        out += ["", "Archived runs (%d, %s):" % (len(archived["runs"]), ARCHIVE_NAME)]
        for r in archived["runs"]:
            out.append("  #%-3s %-12s @%s  %s  [archived]" % (r["id"], r["outcome"] or r["status"] or "-",
                                                             r["profile"] or "-", fmt_ts(r["started_at"])))
            if r["summary"]:
                out.append("        → %s" % r["summary"].splitlines()[0][:160])
    return out
