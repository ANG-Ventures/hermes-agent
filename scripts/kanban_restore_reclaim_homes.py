#!/usr/bin/env python3
"""One-shot: undo ``reclaim --takeover`` re-homes (t_0b3b0667).

Before t_0b3b0667 a ``hermes kanban reclaim <id> --takeover`` moved a FOREIGN
card's home session to the operator and subscribed the operator's chat. On
2026-09-29 19:11-19:12 PDT that re-homed 14 cards into Apollo's session.

For every ``takeover`` event with ``action == "reclaim"`` that re-homed the card
(``prev_session_id`` present), this prints one row and, with ``--apply``:

* sets ``tasks.session_id`` back to ``prev_session_id``;
* deletes the notify subscriptions the takeover added: rows created within
  ``--sub-window`` seconds after the event that were not in the event's
  ``previous_home`` (the taker's chat);
* appends a ``session_restamped`` event naming the source event.

A card is SKIPPED when its home moved again after the event (current
session_id != the event's session_id, or a later takeover/session_restamped
event). Default is a dry run.

    python scripts/kanban_restore_reclaim_homes.py --since 2026-09-29T19:00 --until 2026-09-29T19:20
    python scripts/kanban_restore_reclaim_homes.py ... --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli import kanban_db as kb  # noqa: E402


def _ts(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


def _payload(row) -> dict:
    try:
        data = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plan(conn, *, since: int, until: int, by_session: str | None, sub_window: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, task_id, created_at, payload FROM task_events "
        "WHERE kind = 'takeover' AND created_at BETWEEN ? AND ? ORDER BY id",
        (since, until),
    ).fetchall()
    out = []
    for ev in rows:
        p = _payload(ev)
        if p.get("action") != "reclaim" or not p.get("prev_session_id"):
            continue
        taker = p.get("session_id")
        if by_session and taker != by_session:
            continue
        tid = ev["task_id"]
        task = conn.execute(
            "SELECT status, session_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        row = {
            "task_id": tid, "event_id": ev["id"], "status": task["status"] if task else None,
            "current": task["session_id"] if task else None,
            "restore_to": p["prev_session_id"], "taker": taker,
            "remove_subs": [], "skip": None,
        }
        later = conn.execute(
            "SELECT id, kind FROM task_events WHERE task_id = ? AND id > ? "
            "AND kind IN ('takeover', 'session_restamped') ORDER BY id",
            (tid, ev["id"]),
        ).fetchall()
        moved_again = [
            r["id"] for r in later
            if r["kind"] == "session_restamped" or _payload(r).get("prev_session_id")
        ]
        if task is None:
            row["skip"] = "card gone"
        elif task["session_id"] != taker:
            row["skip"] = f"home is now {task['session_id']!r}, not the taker"
        elif moved_again:
            row["skip"] = f"re-homed again by event {moved_again[0]}"
        keep = {
            (h.get("platform"), h.get("chat_id"), h.get("thread_id") or "")
            for h in (p.get("previous_home") or []) if isinstance(h, dict)
        }
        for s in conn.execute(
            "SELECT platform, chat_id, thread_id FROM kanban_notify_subs "
            "WHERE task_id = ? AND created_at BETWEEN ? AND ?",
            (tid, ev["created_at"], ev["created_at"] + sub_window),
        ).fetchall():
            key = (s["platform"], s["chat_id"], s["thread_id"] or "")
            if key not in keep:
                row["remove_subs"].append(
                    {"platform": key[0], "chat_id": key[1], "thread_id": key[2]})
        out.append(row)
    return out


def apply(conn, rows: list[dict]) -> int:
    n = 0
    for row in rows:
        if row["skip"]:
            continue
        with kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET session_id = ? WHERE id = ? AND session_id = ?",
                (row["restore_to"], row["task_id"], row["taker"]),
            )
            if cur.rowcount != 1:
                row["skip"] = "home changed during apply"
                continue
            for s in row["remove_subs"]:
                conn.execute(
                    "DELETE FROM kanban_notify_subs WHERE task_id = ? AND platform = ? "
                    "AND chat_id = ? AND thread_id = ?",
                    (row["task_id"], s["platform"], s["chat_id"], s["thread_id"]),
                )
            kb._append_event(conn, row["task_id"], "session_restamped", {
                "session_id": row["restore_to"],
                "restored_from_event": row["event_id"],
                "undid_takeover_by": row["taker"],
                "removed_subs": row["remove_subs"],
                "reason": "t_0b3b0667: reclaim --takeover is not an ownership transfer",
            })
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", required=True, help="local ISO time, e.g. 2026-09-29T19:00")
    ap.add_argument("--until", required=True)
    ap.add_argument("--by-session", default=None, help="only takeovers BY this session")
    ap.add_argument("--sub-window", type=int, default=30,
                    help="seconds after the event in which the taker's sub was written")
    ap.add_argument("--board", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    with kb.connect_closing(board=args.board) as conn:
        rows = plan(conn, since=_ts(args.since), until=_ts(args.until),
                    by_session=args.by_session, sub_window=args.sub_window)
        applied = apply(conn, rows) if args.apply else 0
    if args.json:
        print(json.dumps({"rows": rows, "applied": applied}, indent=2))
    else:
        print(f"{'task':<12} {'status':<10} {'restore_to':<26} {'taker':<26} {'-subs':<24} skip")
        for r in rows:
            subs = ",".join(f"{s['platform']}:{s['chat_id']}" for s in r["remove_subs"]) or "-"
            print(f"{r['task_id']:<12} {str(r['status']):<10} {r['restore_to']:<26} "
                  f"{str(r['taker']):<26} {subs:<24} {r['skip'] or ''}")
        todo = sum(1 for r in rows if not r["skip"])
        print(f"{len(rows)} re-homing reclaim takeovers; {todo} restorable; "
              + (f"applied {applied}" if args.apply else "dry run (pass --apply)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
