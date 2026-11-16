"""A closed card's permanent pointer to what it produced (t_328f78fc).

The board records a card's output (report link, doc path, PR, ref) mostly in run summaries, run
metadata, comments and events. The fleet retention job (hermes-home ``kanban-events-retention.py``
R4-R8) stubs or moves those rows 10-14 days after the card closes; the ``tasks`` row stays. So on
close the kernel copies a compact list of pointers onto ``tasks.artifact_pointer`` (JSON list of
strings), which no retention rule touches. ``[]`` = looked, found nothing; NULL = never stamped.

Sources, in this order, deduped, capped at :data:`MAX_ENTRIES` entries / :data:`MAX_BYTES`:
the survivor PR or ref and PR urls from the newest run's metadata, then URLs and absolute paths
found in ``result``, run summaries + metadata (newest first) and comments (newest first), then
attachment names (last: attachments are never pruned). Paths inside the card's own workspace, attachment
blob paths and loopback URLs are skipped.

Backfill (cards closed before this landed, whose rows still survive live or in kanban-archive.db):
``python -m hermes_cli.kanban_artifact_pointer [--board SLUG] [--apply]``. Dry-run by default; only
NULL pointers are written, so a rerun changes nothing.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable, Optional

MAX_ENTRIES = 10
MAX_BYTES = 2048
_MAX_ENTRY_CHARS = 300

_URL_RE = re.compile(r"(?:https?|obsidian)://[^\s`\"'<>\]\[(){}|]+")
_PATH_RE = re.compile(
    r"(?<![\w.:/~-])(?:~|/(?:Users|home|Volumes|srv|mnt|System/Volumes/Data))/[^\s`\"'<>\]\[(){}|,;]+")
# Not pointers: loopback URLs, and attachment blob paths (the ``attachment:<name>`` entry names them).
_NOISE_RE = re.compile(r"^\w+://(?:127\.0\.0\.1|localhost)(?:[:/]|$)|/kanban/attachments/")
_TRAIL = ".,;:!?)]}>'\"*"


def _scan(text: Optional[str]) -> list[str]:
    if not text:
        return []
    hits = [(m.start(), m.group(0)) for m in _URL_RE.finditer(text)]
    url_spans = [(m.start(), m.end()) for m in _URL_RE.finditer(text)]
    for m in _PATH_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in url_spans):   # the path part of a URL
            hits.append((m.start(), m.group(0)))
    return [h.rstrip(_TRAIL) for _pos, h in sorted(hits)]


def _survivor_entries(meta: dict) -> list[str]:
    out: list[str] = []
    surv = meta.get("survivor")
    if isinstance(surv, dict):
        for c in surv.get("claims") or ():
            if isinstance(c, dict) and c.get("pr"):
                out.append(str(c["pr"]))
        for e in surv.get("landed") or ():
            if isinstance(e, dict) and e.get("repository"):
                out.append(f"{e['repository']}@{e.get('sha', '')}")
        for r in surv.get("refs") or ():
            if isinstance(r, dict) and r.get("branch"):
                out.append(f"{r.get('repository_path') or r.get('remote', '')}/{r['branch']}@{r.get('sha', '')}")
    for key in ("pr_url", "pr_urls", "survivor_pr", "superseded_by"):
        val = meta.get(key)
        for v in (val if isinstance(val, (list, tuple)) else [val]):
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
    return out


def _meta(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _cap(entries: Iterable[str], skip_prefix: Optional[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    size = 2   # "[]"
    for raw in entries:
        e = str(raw).strip()
        # An over-long entry is skipped, never cut: a truncated URL or path is a wrong pointer.
        if not e or len(e) > _MAX_ENTRY_CHARS or e in seen or (skip_prefix and e.startswith(skip_prefix)):
            continue
        cost = len(json.dumps(e)) + 1
        if size + cost > MAX_BYTES:
            continue
        seen.add(e)
        out.append(e)
        size += cost
        if len(out) >= MAX_ENTRIES:
            break
    return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def collect(conn: sqlite3.Connection, task_id: str, *, include_archive: bool = False) -> list[str]:
    """The pointer list for ``task_id`` from the rows that exist now (no write)."""
    row = conn.execute("SELECT result, workspace_kind, workspace_path FROM tasks WHERE id = ?",
                       (task_id,)).fetchone()
    if row is None:
        return []
    result, ws_kind, ws_path = row[0], row[1], row[2]
    skip = ws_path.rstrip("/") + "/" if ws_kind == "scratch" and ws_path else None
    runs = [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT id, summary, metadata FROM task_runs WHERE task_id = ?", (task_id,))]
    comments = [(r[0], r[1]) for r in conn.execute(
        "SELECT id, body FROM task_comments WHERE task_id = ?", (task_id,))]
    attachments = ([r[0] for r in conn.execute(
        "SELECT filename FROM task_attachments WHERE task_id = ? ORDER BY id", (task_id,))]
        if _table_exists(conn, "task_attachments") else [])
    if include_archive:
        # Full archived rows (metadata included); an unreadable archive raises ArchiveUnreadable so
        # the backfill leaves the pointer NULL instead of stamping an incomplete one.
        from hermes_cli import kanban_archive_read as kar
        runs += [(r.get("id"), r.get("summary"), r.get("metadata")) for r in kar.full_rows(
            conn, "task_runs", task_id, live_ids=[r[0] for r in runs])]
        comments += [(c.get("id"), c.get("body")) for c in kar.full_rows(
            conn, "task_comments", task_id, live_ids=[c[0] for c in comments])]
    runs = [(s, _meta(m)) for _i, s, m in sorted(runs, key=lambda r: -(r[0] or 0))]   # newest first
    comments = [b for _i, b in sorted(comments, key=lambda c: -(c[0] or 0))]
    entries: list[str] = []
    if runs:
        entries += _survivor_entries(runs[0][1])
    entries += _scan(result)
    for summary, meta in runs:
        entries += _scan(summary)
        if meta:
            entries += [str(p) for p in meta.get("artifacts") or () if isinstance(p, str)]
            entries += _scan(json.dumps(meta, ensure_ascii=False))
    for body in comments:
        entries += _scan(body)
    entries = [e for e in entries if not _NOISE_RE.search(e)]
    entries += [f"attachment:{a}" for a in attachments if a]   # last: attachments are not pruned
    return _cap(entries, skip)


def stamp(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Write the pointer for a card that just closed. Call inside the closing write transaction.
    Entries already stamped are kept after the new ones: archiving a card whose rows retention
    already moved, or re-closing a reopened card, must not drop what the first close recorded."""
    row = conn.execute("SELECT artifact_pointer FROM tasks WHERE id = ?", (task_id,)).fetchone()
    prior = read(row[0]) if row is not None else None
    entries = collect(conn, task_id)
    if prior:
        entries = _cap(entries + prior, None)
    conn.execute("UPDATE tasks SET artifact_pointer = ? WHERE id = ?", (json.dumps(entries), task_id))
    return entries


def read(raw: Optional[str]) -> Optional[list[str]]:
    """Parsed ``tasks.artifact_pointer``; None when never stamped or unreadable."""
    if raw is None:
        return None
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return [str(v) for v in val] if isinstance(val, list) else None


def backfill(conn: sqlite3.Connection, *, apply: bool = False) -> dict:
    """Stamp every closed card whose pointer is NULL. Never overwrites a stamped pointer, so it is
    idempotent. -> {"scanned": n, "gain": cards with >=1 entry, "empty": cards with none, "applied": bool}"""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE status IN ('done', 'archived') AND artifact_pointer IS NULL ORDER BY id")]
    from hermes_cli.kanban_archive_read import ArchiveUnreadable
    plan, unreadable = [], 0
    for tid in ids:
        try:
            plan.append((tid, collect(conn, tid, include_archive=True)))
        except ArchiveUnreadable:   # left NULL: a later run stamps it once the archive reads
            unreadable += 1
    if apply:
        from hermes_cli.kanban_db_connect import write_txn
        with write_txn(conn):
            for tid, entries in plan:
                conn.execute("UPDATE tasks SET artifact_pointer = ? WHERE id = ? AND artifact_pointer IS NULL",
                             (json.dumps(entries), tid))
    gain = sum(1 for _t, e in plan if e)
    return {"scanned": len(plan), "gain": gain, "empty": len(plan) - gain, "unreadable": unreadable,
            "applied": apply}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Backfill tasks.artifact_pointer for closed cards (dry-run by default).")
    ap.add_argument("--board", default=None)
    ap.add_argument("--apply", action="store_true", help="write the pointers (default: report only)")
    args = ap.parse_args(argv)
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing(board=args.board) as conn:
        print(json.dumps(backfill(conn, apply=args.apply)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
