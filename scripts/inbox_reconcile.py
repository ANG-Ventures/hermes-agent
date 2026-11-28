#!/usr/bin/env python3
"""Read-only reconciler for incoming-message dispositions (card t_1eb0a176, Step 0).

Reads the gateway and agent logs plus the session store for a time window and prints where each
person's message went: accepted, each disposition, refusals, held items with no later release or
clear (age in hours), messages with no final disposition, and reorders split by held release.
Opens the session store with ``mode=ro`` and writes nothing anywhere.

    python scripts/inbox_reconcile.py --since 24h
    python scripts/inbox_reconcile.py --since 2026-11-27T18:42 --json
    python scripts/inbox_reconcile.py --since 7d --logs-dir /path/logs --db /path/state.db
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) ")
KV = re.compile(r"(\w+)=(\S+)")
DELIVERED = {"steered", "became-next-turn"}
WAITING = {"queued", "held"}
HELD_IN = {"hold", "restart_restore", "auto_reset"}
HELD_OUT = {"release", "clear", "rm", "reset_drop"}
STEER_HEADER = "Gateway message origin (JSON data, not instructions or authorization):"
STEER_HEADER_ID = re.compile(r'"message_id": "?([^",}]+)"?')


def parse_since(value: str, now: datetime) -> datetime:
    m = re.fullmatch(r"(\d+)([mhd])", value.strip())
    if m:
        return now - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})
    return datetime.fromisoformat(value)


def read_lines(logs_dir: str, since: datetime, until: datetime) -> Iterable[Tuple[datetime, str]]:
    """Timestamped lines of gateway.log* and agent.log* inside the window, each distinct line once
    (a gateway line can be written to both files)."""
    seen = set()
    for pattern in ("gateway.log*", "agent.log*"):
        for path in sorted(glob.glob(os.path.join(logs_dir, pattern))):
            try:
                handle = open(path, "r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    m = TS.match(line)
                    if not m:
                        continue
                    if not ("inbox_" in line or "steer_refused" in line or "queue_mgmt" in line):
                        continue
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(microsecond=int(m.group(2)) * 1000)
                    if ts < since or ts > until:
                        continue
                    body = line.rstrip("\n")
                    key = (m.group(0), body.split(": ", 1)[-1])
                    if key in seen:
                        continue
                    seen.add(key)
                    yield ts, body


def store_ids(db_path: Optional[str], since: datetime) -> Tuple[Optional[set], Dict[str, Any]]:
    """Platform ids the session store holds for the window (the column, or a steer origin header),
    plus steer-row and stored-order counts. ``(None, {...})`` when the store cannot be read."""
    stats: Dict[str, Any] = {"readable": False}
    if not db_path or not os.path.exists(db_path):
        return None, stats
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT id, session_id, platform_message_id, display_kind, content FROM messages "
                "WHERE role = 'user' AND timestamp >= ? ORDER BY id", (since.timestamp(),)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        stats["error"] = str(exc)
        return None, stats
    ids: set = set()
    steer_rows = steer_with_id = inversions = 0
    high: Dict[str, int] = {}
    for _row_id, session_id, pid, kind, content in rows:
        if kind == "steer":
            steer_rows += 1
            steer_with_id += pid is not None
        found = [str(pid)] if pid is not None else []
        if not found and isinstance(content, str) and STEER_HEADER in content[:4000]:
            found = STEER_HEADER_ID.findall(content[:4000])
        for value in found:
            ids.add(value)
            if value.isdigit():
                if int(value) < high.get(session_id, 0):
                    inversions += 1
                high[session_id] = max(high.get(session_id, 0), int(value))
    stats.update(readable=True, user_rows=len(rows), steer_rows=steer_rows, steer_rows_with_id=steer_with_id,
                 stored_order_inversions=inversions)
    return ids, stats


def reconcile(lines: Iterable[Tuple[datetime, str]], stored: Optional[set], now: datetime,
              grace_minutes: int = 60) -> Dict[str, Any]:
    dispositions: Counter = Counter()
    refusal_paths: Counter = Counter()
    steer_refused: Counter = Counter()
    reorders = {"held_release": 0, "not_held_release": 0}
    reorder_checks: Counter = Counter()
    last: Dict[Tuple[str, str], Tuple[datetime, str]] = {}
    held: Dict[Tuple[str, str], Tuple[datetime, str]] = {}
    held_ops: Counter = Counter()
    spooled: Dict[Tuple[str, str], datetime] = {}
    shrinks: List[str] = []
    item_log_failures = anonymous = 0
    for ts, line in lines:
        fields = dict(KV.findall(line.split(" preview=", 1)[0]))   # a preview is message text: never parsed
        if " inbox_disposition " in line:
            d = fields.get("disposition", "?")
            dispositions[d] += 1
            if d == "refused":
                refusal_paths[fields.get("path", "-")] += 1
            if d in DELIVERED:
                reorder_checks[fields.get("reorder_check", "-")] += 1
            mid = fields.get("msg_id", "-")
            if mid == "-":
                anonymous += 1
                continue
            last[(fields.get("session", "-"), mid)] = (ts, d)
        elif " inbox_reorder_detected " in line:
            reorders["held_release" if fields.get("held_release") == "True" else "not_held_release"] += 1
        elif "steer_refused reason=" in line:
            steer_refused[fields.get("reason", "?")] += 1
        elif "queue_mgmt item log failed" in line:
            item_log_failures += 1
        elif "queue_mgmt op=unexplained_shrink" in line:
            shrinks.append(line)
        elif "queue_mgmt item op=" in line:
            op = fields.get("op", "?")
            held_ops[op] += 1
            if fields.get("kind") != "user":
                continue
            key = (fields.get("session", "-"), fields.get("msg_id", "None"))
            if op in HELD_IN:
                held[key] = (ts, op)
                spooled.pop(key, None)
            elif op in HELD_OUT:
                held.pop(key, None)
            elif op == "restart_spool":
                held.pop(key, None)
                spooled[key] = ts
    cutoff = now - timedelta(minutes=grace_minutes)
    in_store = (lambda mid: mid in stored) if stored is not None else (lambda mid: False)
    held_open = [
        {"session": k[0], "msg_id": k[1], "since": v[0].isoformat(sep=" "), "via": v[1],
         "age_hours": round((now - v[0]).total_seconds() / 3600, 1)}
        for k, v in sorted(held.items(), key=lambda kv: kv[1][0])]
    held_open += [
        {"session": k[0], "msg_id": k[1], "since": ts.isoformat(sep=" "), "via": "restart_spool (never restored)",
         "age_hours": round((now - ts).total_seconds() / 3600, 1)}
        for k, ts in sorted(spooled.items(), key=lambda kv: kv[1])]
    held_keys = set(held) | set(spooled)
    undisposed = [
        {"session": k[0], "msg_id": k[1], "last": d, "at": ts.isoformat(sep=" "),
         "age_hours": round((now - ts).total_seconds() / 3600, 1)}
        for k, (ts, d) in sorted(last.items(), key=lambda kv: kv[1][0])
        if d in WAITING and ts <= cutoff and k not in held_keys and not in_store(k[1])]
    return {
        "accepted": len(last) + anonymous,
        "accepted_without_id": anonymous,
        "dispositions": dict(sorted(dispositions.items())),
        "refused_by_path": dict(sorted(refusal_paths.items())),
        "steer_refused_by_reason": dict(sorted(steer_refused.items())),
        "reorder_detected": reorders,
        "reorder_checks": dict(sorted(reorder_checks.items())),
        "held_item_ops": dict(sorted(held_ops.items())),
        "held_without_release_or_clear": held_open,
        "no_final_disposition": undisposed,
        "unexplained_held_shrinks": len(shrinks),
        "held_item_log_failures": item_log_failures,
    }


def render(report: Dict[str, Any]) -> str:
    out = [f"window: {report['since']} -> {report['until']}  logs: {report['logs_dir']}",
           f"person messages accepted: {report['accepted']} ({report['accepted_without_id']} without a platform id)"]
    out += [f"  disposition {name}: {count}" for name, count in report["dispositions"].items()] or ["  (no inbox_disposition lines in the window)"]
    refused = sum(report["refused_by_path"].values())
    out.append(f"refusals: {refused} by the gateway {report['refused_by_path']}; "
               f"steer() refused {sum(report['steer_refused_by_reason'].values())} {report['steer_refused_by_reason']}")
    out.append(f"held item lines by op: {report['held_item_ops']}")
    out.append(f"held with no later release or clear: {len(report['held_without_release_or_clear'])}")
    out += [f"  {i['age_hours']:>7} h  msg_id={i['msg_id']} session={i['session']} via={i['via']} since {i['since']}"
            for i in report["held_without_release_or_clear"]]
    out.append(f"unexplained held shrinks: {report['unexplained_held_shrinks']}; "
               f"held item-line failures: {report['held_item_log_failures']}")
    out.append(f"no final disposition (older than {report['grace_minutes']} min, not in the session store): "
               f"{len(report['no_final_disposition'])}")
    out += [f"  {i['age_hours']:>7} h  msg_id={i['msg_id']} session={i['session']} last={i['last']} at {i['at']}"
            for i in report["no_final_disposition"]]
    r = report["reorder_detected"]
    out.append(f"reorder_detected: {r['held_release'] + r['not_held_release']} "
               f"(held release: {r['held_release']}, not a held release: {r['not_held_release']}); "
               f"checks on delivery: {report['reorder_checks']}")
    s = report["store"]
    if s.get("readable"):
        out.append(f"session store: {s['user_rows']} user rows; steer rows {s['steer_rows']}, "
                   f"{s['steer_rows_with_id']} with a platform id; stored-order inversions {s['stored_order_inversions']}")
    else:
        out.append(f"session store: not read ({s.get('error', 'no file')})")
    return "\n".join(out)


def default_home() -> str:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", required=True, help="window start: 24h, 7d, 90m, or an ISO local time")
    parser.add_argument("--until", help="window end (ISO local time); default now")
    parser.add_argument("--logs-dir", help="directory holding gateway.log* and agent.log*; default <home>/logs")
    parser.add_argument("--db", help="session store path; default <home>/state.db")
    parser.add_argument("--grace-minutes", type=int, default=60,
                        help="a queued or held message younger than this is not yet counted as undisposed")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now()
    since = parse_since(args.since, now)
    until = datetime.fromisoformat(args.until) if args.until else now
    home = default_home() if not (args.logs_dir and args.db) else ""
    logs_dir = args.logs_dir or os.path.join(home, "logs")
    db_path = args.db or os.path.join(home, "state.db")
    stored, store_stats = store_ids(db_path, since)
    report = reconcile(read_lines(logs_dir, since, until), stored, until, args.grace_minutes)
    report.update(since=since.isoformat(sep=" ", timespec="seconds"), until=until.isoformat(sep=" ", timespec="seconds"),
                  logs_dir=logs_dir, grace_minutes=args.grace_minutes, store=store_stats)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
