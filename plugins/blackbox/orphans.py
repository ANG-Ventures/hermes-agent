"""Detector: ``turn_api_calls`` turn_ids that have no parent ``turns`` row.

Every per-call ledger row belongs to a conversation turn, and every turn must
end with a ``turns`` row (``on_session_end`` -> ``store.insert_turn``). A turn
that ended through an early return / raise used to skip that hook, so its
calls were orphaned and failed turns vanished from every turn-level surface
(t_6c09f0c2). This module counts such orphans so a scheduled check can gate
on the invariant instead of relying on someone noticing a missing failure.

CLI (stdout is empty and exit 0 when clean, so a no_agent cron stays silent)::

    python -m plugins.blackbox.orphans [--since-hours 24] [--settle-s 3600] \
        [--max 0] [DB ...]

With no DB arguments it scans ``<home>/blackbox/turns.db`` plus every
``<home>/profiles/*/blackbox/turns.db`` (home = the Hermes home root). Databases are opened
read-only (``mode=ro``, never ``immutable=1`` -- they are live WAL stores).
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time
from typing import Iterable

# A turn whose most recent call is younger than this may still be running
# (its turns row lands at turn end), so it is not yet an orphan.
DEFAULT_SETTLE_S = 3600.0

_ORPHAN_SQL = """
    SELECT c.turn_id,
           MAX(c.ts) AS last_ts,
           SUM(CASE WHEN COALESCE(c.http_status, 200) != 200 THEN 1 ELSE 0 END)
    FROM turn_api_calls c
    WHERE NOT EXISTS (SELECT 1 FROM turns t WHERE t.turn_id = c.turn_id)
    GROUP BY c.turn_id
    HAVING MIN(c.ts) >= ? AND MAX(c.ts) < ?
"""


def orphan_turns(
    conn: sqlite3.Connection, *, since: float, settle_before: float
) -> list[tuple[str, float, int]]:
    """Return ``(turn_id, last_call_ts, error_calls)`` for every orphaned turn.

    Only turns whose FIRST call is at/after ``since`` and whose LAST call is
    before ``settle_before`` are considered.
    """
    return [
        (str(tid), float(last or 0.0), int(errs or 0))
        for tid, last, errs in conn.execute(_ORPHAN_SQL, (since, settle_before))
    ]


def default_db_paths(home: str | None = None) -> list[str]:
    if home is None:
        from plugins.blackbox.store import _db_path

        home = str(_db_path().parent.parent)
    root = home
    # A profile-scoped home (…/profiles/<name>) still scans the fleet root.
    parts = os.path.normpath(root).split(os.sep)
    if len(parts) >= 2 and parts[-2] == "profiles":
        root = os.sep.join(parts[:-2]) or os.sep
    paths = [os.path.join(root, "blackbox", "turns.db")]
    paths += glob.glob(os.path.join(root, "profiles", "*", "blackbox", "turns.db"))
    return sorted(p for p in set(paths) if os.path.exists(p))


def scan(
    paths: Iterable[str], *, since: float, settle_before: float
) -> dict[str, list[tuple[str, float, int]]]:
    out: dict[str, list[tuple[str, float, int]]] = {}
    for path in paths:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if {"turns", "turn_api_calls"} <= tables:
                out[path] = orphan_turns(conn, since=since, settle_before=settle_before)
        finally:
            conn.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Count turn_api_calls turn_ids with no parent turns row."
    )
    ap.add_argument("dbs", nargs="*", help="turns.db paths (default: every profile)")
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--since", type=float, default=None, help="epoch lower bound (overrides --since-hours)")
    ap.add_argument("--settle-s", type=float, default=DEFAULT_SETTLE_S)
    ap.add_argument("--max", type=int, default=0, help="orphans tolerated before failing")
    args = ap.parse_args(argv)

    now = time.time()
    since = args.since if args.since is not None else now - args.since_hours * 3600.0
    results = scan(args.dbs or default_db_paths(), since=since, settle_before=now - args.settle_s)
    total = sum(len(v) for v in results.values())
    if total <= args.max:
        return 0
    print(
        f"blackbox: {total} turn(s) have turn_api_calls rows but no turns row "
        f"(window since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}, "
        f"tolerance {args.max})"
    )
    for path, rows in sorted(results.items()):
        if not rows:
            continue
        with_err = sum(1 for _tid, _ts, errs in rows if errs)
        print(f"  {path}: {len(rows)} orphan turn(s), {with_err} with a non-200 call")
        for tid, _ts, errs in rows[:5]:
            print(f"    {tid} error_calls={errs}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
