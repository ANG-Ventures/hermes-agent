"""Read-only view of a card's rows moved off the board into ``kanban-archive.db``.

The fleet retention job (hermes-home ``scripts/kanban-events-retention.py`` rule R8, t_d1b5fc83)
moves the comments and runs of done/archived cards completed more than 14 days ago from
``kanban.db`` into ``kanban-archive.db`` beside it. It keeps the ``tasks`` row. The archive
tables use the live table names and columns, plus ``moved_at``. ``kanban show`` (CLI and the
``kanban_show`` tool) lists them labelled archived; the worker context (prior attempts, parent
handoffs, comment thread) and the PR -> card lookup (``kanban_pr_owner.pr_cards``) merge them in,
live first (t_589fb988).
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
                  state_name: str | None = None, live_comment_ids=(), live_run_ids=()) -> dict[str, Any]:
    """``{"comments": [...], "runs": [...]}`` from the board's archive file, empty when there is none.

    ``state_type``/``state_name`` filter the runs exactly like ``kanban_db.list_runs``. An archive that
    exists but cannot be read (locked, corrupt) adds ``"error": "<sqlite3 error class>: <message>"``
    so a failed read never looks like a card with no archived history.

    Call this AFTER reading the live comments and runs, and pass their ids (t_f06682ef). R8 commits the
    archive before it deletes the live rows, so a move that lands between the two reads leaves a row in
    both, never in neither; an archived row whose id is live is dropped here so it is not listed twice."""
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None and state_type not in ("status", "outcome"):
        raise ValueError("state_type must be 'status' or 'outcome'")
    path = _archive_path(conn)
    if path is None or not path.exists():   # only MISSING is absent; a dir/non-file must fail (t_78dbef10)
        return {"comments": [], "runs": []}
    run_where, run_params = ("", ()) if state_type is None else (" AND %s = ?" % state_type, (state_name,))
    arch = None
    try:
        arch = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_TIMEOUT_S)
        live_c, live_r = set(live_comment_ids), set(live_run_ids)
        return {"comments": [c for c in _rows(arch, "task_comments", _COMMENT_COLS, task_id, "created_at, id")
                             if c["id"] not in live_c],
                "runs": [r for r in _rows(arch, "task_runs", _RUN_COLS, task_id, "started_at, id", run_where,
                                          run_params) if r["id"] not in live_r]}
    except sqlite3.Error as exc:
        return {"comments": [], "runs": [], "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if arch is not None:
            arch.close()


class ArchiveUnreadable(RuntimeError):
    """``kanban-archive.db`` exists but could not be read; the caller must not render less history."""


def _connect_ro(conn: sqlite3.Connection) -> sqlite3.Connection | None:
    path = _archive_path(conn)
    if path is None or not path.exists():   # only MISSING is absent; a dir/non-file must fail (t_78dbef10)
        return None
    try:
        return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_TIMEOUT_S)
    except sqlite3.Error as exc:
        raise ArchiveUnreadable("%s: %s: %s" % (ARCHIVE_NAME, type(exc).__name__, exc)) from exc


def full_rows(conn: sqlite3.Connection, table: str, task_id: str, *, live_ids=()) -> list[dict[str, Any]]:
    """Archived ``table`` rows of ``task_id`` with the LIVE table's columns (NULL where the archive lacks one).

    For readers that build ``Run``/``Comment`` objects from archived rows (worker context, t_589fb988). Read
    live first and pass the live ids: an archived row whose id is still live is dropped, so the live copy
    always wins. No archive file / no such table = ``[]``; an unreadable archive raises ``ArchiveUnreadable``."""
    arch = _connect_ro(conn)
    if arch is None:
        return []
    try:
        have = {r[1] for r in arch.execute("PRAGMA table_info(%s)" % table)}
        if not have:
            return []
        cols = [r[1] for r in conn.execute("PRAGMA main.table_info(%s)" % table)]
        sel = ", ".join(c if c in have else "NULL AS %s" % c for c in cols)
        live = set(live_ids)
        return [row for row in (dict(zip(cols, r)) for r in arch.execute(
            "SELECT %s FROM %s WHERE task_id = ? ORDER BY id" % (sel, table), (task_id,)))
            if row.get("id") not in live]
    except sqlite3.Error as exc:
        raise ArchiveUnreadable("%s: %s: %s" % (ARCHIVE_NAME, type(exc).__name__, exc)) from exc
    finally:
        arch.close()


def runs_with_archive(conn: sqlite3.Connection, task_id: str) -> list:
    """``kanban_db.list_runs(conn, task_id)`` plus the card's archived runs, in start order, deduped by id."""
    from hermes_cli import kanban_db as kb

    live = kb.list_runs(conn, task_id)
    runs = live + [kb.Run.from_row(r) for r in full_rows(conn, "task_runs", task_id,
                                                          live_ids=[r.id for r in live])]
    return sorted(runs, key=lambda r: (r.started_at, r.id))


def comments_with_archive(conn: sqlite3.Connection, task_id: str) -> list:
    """``kanban_db.list_comments(conn, task_id)`` plus the card's archived comments, oldest first, deduped by id."""
    from hermes_cli import kanban_db as kb

    live = kb.list_comments(conn, task_id)
    comments = live + [kb.Comment.from_row(r) for r in full_rows(conn, "task_comments", task_id,
                                                                  live_ids=[c.id for c in live])]
    return sorted(comments, key=lambda c: (c.created_at, c.id))


def run_metadatas(conn: sqlite3.Connection, task_id: str) -> list:
    """Every non-NULL ``task_runs.metadata`` of the card, live rows first, then archived rows not live."""
    live = conn.execute("SELECT id, metadata FROM task_runs WHERE task_id = ?", (task_id,)).fetchall()
    out = [r[1] for r in live if r[1] is not None]
    out += [r["metadata"] for r in full_rows(conn, "task_runs", task_id, live_ids=[r[0] for r in live])
            if r.get("metadata") is not None]
    return out


def task_ids_with_run_metadata_like(conn: sqlite3.Connection, patterns) -> set:
    """Task ids whose ARCHIVED runs' metadata matches any ``LIKE`` pattern (PR -> card lookup, t_589fb988)."""
    patterns = list(patterns)
    arch = _connect_ro(conn)
    if arch is None or not patterns:
        if arch is not None:
            arch.close()
        return set()
    try:
        have = {r[1] for r in arch.execute("PRAGMA table_info(task_runs)")}
        if not {"task_id", "metadata"} <= have:
            return set()
        return {r[0] for r in arch.execute(
            "SELECT DISTINCT task_id FROM task_runs WHERE " + " OR ".join(["metadata LIKE ?"] * len(patterns)),
            patterns)}
    except sqlite3.Error as exc:
        raise ArchiveUnreadable("%s: %s: %s" % (ARCHIVE_NAME, type(exc).__name__, exc)) from exc
    finally:
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
