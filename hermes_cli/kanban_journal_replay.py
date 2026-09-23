"""Rebuild a kanban board DB from a snapshot plus the mutation journal.

The recovery half of :mod:`hermes_cli.kanban_journal`. The snapshot bounds loss
to the snapshot interval; replaying the journal tail on top of it closes that
window to a single fsync.

Usage::

    hermes kanban journal-replay --board subs-ace \\
        --snapshot "/Volumes/Models SSD 4TB/fleet-backups/kanban/<ts>/subs-ace.kanban.db" \\
        --into /tmp/recovered.db

What replay restores, and what it deliberately does not:

* **Comment bodies -- the reason this exists.** ``comment_body`` records carry
  the full text, so threads written after the snapshot come back. On
  2026-09-21 the subs-ace comment threads were the ONLY unrecoverable part of
  the wipe (card t_357330bf); everything else could be reconstructed.
* **Status / lifecycle events** are replayed into ``task_events`` so the
  audit trail is continuous across the restore seam.
* It does **not** attempt to synthesise task ROWS that never existed in the
  snapshot from event records alone. An event says a task was created; it does
  not carry the full row. Such tasks are reported as ``orphan_events`` for an
  operator rather than half-invented -- inventing a plausible-looking row is
  exactly the failure mode this card is about.

Replay is idempotent: re-running against the same snapshot and journal produces
the same DB. Comments are matched on (task_id, author, body, created_at) so a
second pass inserts nothing.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_journal

__all__ = ["replay_into", "ReplayResult"]


class ReplayResult:
    """What a replay did, in numbers an operator can check."""

    def __init__(self) -> None:
        self.entries_read = 0
        self.comments_restored = 0
        self.comments_already_present = 0
        self.events_restored = 0
        self.orphan_events = 0
        self.orphan_task_ids: set = set()
        self.snapshot_ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "entries_read": self.entries_read,
            "comments_restored": self.comments_restored,
            "comments_already_present": self.comments_already_present,
            "events_restored": self.events_restored,
            "orphan_events": self.orphan_events,
            "orphan_task_ids": sorted(self.orphan_task_ids),
            "snapshot_ts": self.snapshot_ts,
        }

    def __str__(self) -> str:
        return (
            "replayed " + str(self.entries_read) + " journal entries: "
            + str(self.comments_restored) + " comments restored ("
            + str(self.comments_already_present) + " already present), "
            + str(self.events_restored) + " events restored, "
            + str(self.orphan_events) + " orphan events across "
            + str(len(self.orphan_task_ids)) + " unknown task(s)"
        )


def _snapshot_watermark(conn: sqlite3.Connection) -> float:
    """The newest timestamp the snapshot already contains.

    Journal records at or before this are already in the DB; replaying them
    would duplicate. Taken as the max over comments and events rather than the
    snapshot file's mtime, which can drift from its contents.
    """
    newest = 0.0
    for sql in (
        "SELECT MAX(created_at) FROM task_comments",
        "SELECT MAX(created_at) FROM task_events",
        "SELECT MAX(created_at) FROM tasks",
    ):
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.Error:
            continue
        if row and row[0]:
            try:
                newest = max(newest, float(row[0]))
            except (TypeError, ValueError):
                continue
    return newest


def replay_into(
    board: Optional[str],
    snapshot: Path,
    into: Path,
    *,
    since: Optional[float] = None,
) -> ReplayResult:
    """Copy *snapshot* to *into*, then replay *board*'s journal on top.

    The snapshot is never modified: recovery must be re-runnable, and a replay
    that mutated its own input would be a one-shot.
    """
    snapshot = Path(snapshot)
    into = Path(into)
    if not snapshot.exists():
        raise FileNotFoundError("snapshot does not exist: " + str(snapshot))
    into.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(snapshot), str(into))

    result = ReplayResult()
    conn = sqlite3.connect(str(into))
    conn.row_factory = sqlite3.Row
    try:
        watermark = _snapshot_watermark(conn) if since is None else float(since)
        result.snapshot_ts = watermark

        known = {
            str(r[0]) for r in conn.execute("SELECT id FROM tasks").fetchall()
        }

        for record in kanban_journal.read_entries(board):
            result.entries_read += 1
            try:
                ts = float(record.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts <= watermark:
                continue
            kind = str(record.get("kind") or "")
            task_id = record.get("task_id")
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}

            if task_id and task_id not in known:
                # An event for a task the snapshot never had. Reported, not
                # invented: an event does not carry enough to reconstruct the
                # row, and a plausible-looking fabricated task is worse than a
                # named gap.
                result.orphan_events += 1
                result.orphan_task_ids.add(str(task_id))
                continue

            if kind == "comment_body":
                created = payload.get("created_at") or int(ts)
                author = payload.get("author") or "unknown"
                body = payload.get("body") or ""
                if not body:
                    continue
                dupe = conn.execute(
                    "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? "
                    "AND body = ? AND created_at = ?",
                    (task_id, author, body, created),
                ).fetchone()
                if dupe:
                    result.comments_already_present += 1
                    continue
                conn.execute(
                    "INSERT INTO task_comments "
                    "(task_id, author, body, run_id, session_ref, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        author,
                        body,
                        record.get("run_id"),
                        payload.get("session_ref"),
                        created,
                    ),
                )
                result.comments_restored += 1
                continue

            # Every other kind is a lifecycle event: replay it so the audit
            # trail is continuous across the restore seam.
            if not kind or not task_id:
                continue
            import json as _json

            pl = _json.dumps(payload, ensure_ascii=False) if payload else None
            dupe = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? "
                "AND created_at = ?",
                (task_id, kind, int(ts)),
            ).fetchone()
            if dupe:
                continue
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, record.get("run_id"), kind, pl, int(ts)),
            )
            result.events_restored += 1

        conn.commit()
    finally:
        conn.close()
    return result
