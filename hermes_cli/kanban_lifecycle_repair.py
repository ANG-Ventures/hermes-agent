"""Explicit, dry-run-first repair of legacy lifecycle rows (t_ae4a1556).

Run with --db PATH; inspect the JSON manifest, then repeat with --apply.
No bulk promotion: after-merge gates are reconciled separately against GitHub.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from hermes_cli.kanban_lifecycle_guards import has_event_gate


def plan(conn: sqlite3.Connection) -> list[dict]:
    from hermes_cli.kanban_db import is_human_reviewer

    changes = []
    for row in conn.execute("SELECT id,status,assignee,body,block_kind,next_eligible_at FROM tasks ORDER BY id"):
        tid, status, assignee, body, kind, wake = row
        if status == "review" and not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='review_requested' "
            "AND json_valid(payload) AND length(json_extract(payload,'$.implementer'))>0", (tid,)
        ).fetchone():
            run = conn.execute("SELECT profile FROM task_runs WHERE task_id=? "
                               "AND outcome IN ('completed','review_requested') AND profile IS NOT NULL "
                               "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            changes.append({"id": tid, "class": "A", "implementer": run[0] if run else "human:operator"})
        if status == "blocked" and kind is None:
            changes.append({"id": tid, "class": "B", "block_kind": "needs_input",
                            "reason": "Legacy untyped block: operator must classify the recovery condition"})
        if status == "ready" and is_human_reviewer(assignee):
            changes.append({"id": tid, "class": "C", "status": "todo", "assignee": assignee})
        if status == "scheduled" and wake is None and not has_event_gate(body):
            if tid == "t_d394f63b":
                changes.append({"id": tid, "class": "D", "event": "parts-arrived"})
            else:
                changes.append({"id": tid, "class": "D", "status": "blocked", "block_kind": "needs_input",
                                "reason": "Legacy scheduled row has neither wake time nor named event"})
    return changes


def repair(conn: sqlite3.Connection, *, apply: bool = False) -> dict:
    # Re-plan under the same reserved lock that protects the writes.
    conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        changes = plan(conn)
        if apply:
            now = int(time.time())
            for change in changes:
                tid, cls = change["id"], change["class"]
                if cls == "A":
                    payload = {"implementer": change["implementer"], "source": "lifecycle_repair"}
                    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                                 (tid, "review_requested", json.dumps(payload), now))
                elif cls == "C":
                    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
                    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                                 (tid, "needs_operator", json.dumps(change), now))
                elif cls == "D" and "event" in change:
                    conn.execute("UPDATE tasks SET body=COALESCE(body,'') || ? WHERE id=?",
                                 ("\nunblock_when: event:" + change["event"], tid))
                else:
                    from hermes_cli import kanban_db as kb
                    # Classification must not change the phase an unblock resumes.
                    prior_factory = conn.row_factory
                    conn.row_factory = sqlite3.Row
                    try:
                        source_status = kb._resume_status_from_events(conn, tid)
                        block_source = kb._latest_block_source(conn, tid)
                    finally:
                        conn.row_factory = prior_factory
                    payload = {"kind": "needs_input", "reason": change["reason"],
                               "source_status": source_status}
                    if block_source == "initial_status":
                        payload["source"] = block_source
                    conn.execute("UPDATE tasks SET status='blocked',block_kind='needs_input' WHERE id=?", (tid,))
                    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                                 (tid, "blocked", json.dumps(payload), now))
                conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                             (tid, "lifecycle_repaired", json.dumps(change), now))
            remaining = plan(conn)
            conn.commit()
        else:
            remaining = changes
            conn.rollback()
        return {"applied": apply, "changes": changes, "before": len(changes), "after": len(remaining)}
    except BaseException:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    # mode=rw never creates a decoy empty board on a mistyped path.
    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
    try:
        print(json.dumps(repair(conn, apply=args.apply), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
