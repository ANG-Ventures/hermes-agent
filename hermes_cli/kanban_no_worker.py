"""Detect operator-only flags stranding otherwise worker-owned ready cards."""
from __future__ import annotations

import sqlite3
import time


def record_stranded(conn: sqlite3.Connection, *, now: int | None = None) -> list[str]:
    """Journal once per ready/flag episode, never silently clear operator intent."""
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    recorded = []
    with kb.write_txn(conn):
        for row in conn.execute(
            "SELECT id, assignee, created_at FROM tasks "
            "WHERE status = 'ready' AND no_worker = 1"
        ).fetchall():
            if not kb.is_worker_assignee(row["assignee"]):
                continue
            entered = conn.execute(
                "SELECT MAX(changed_at) FROM task_status_audit WHERE task_id = ? "
                "AND new_status = 'ready' AND old_status IS NOT 'ready'", (row["id"],),
            ).fetchone()[0]
            flag = conn.execute(
                "SELECT id, created_at FROM task_events WHERE task_id = ? "
                "AND kind = 'no_worker_set' ORDER BY id DESC LIMIT 1", (row["id"],),
            ).fetchone()
            since = max(int(entered or row["created_at"]), int(flag["created_at"] if flag else 0))
            if now - since <= 1800:
                continue
            episode = {"ready_since": int(entered or row["created_at"]),
                       "flag_event_id": int(flag["id"]) if flag else None}
            previous = kb._latest_event(conn, row["id"], "stranded_no_worker")
            if previous and all(kb._json_dict(previous["payload"]).get(k) == v
                                for k, v in episode.items()):
                continue
            kb._append_event(conn, row["id"], "stranded_no_worker", {
                **episode, "assignee": row["assignee"], "since": since,
                "reason": "ready worker card is operator-only; clear no_worker to dispatch",
            })
            recorded.append(row["id"])
    return recorded
