#!/usr/bin/env python3
# SCHEDULE: manual (operator runs once per profile store after the placement_host column deploys)
"""Backfill ``turns.placement_host`` for one profile's Blackbox store (t_c9fc6c9f).

A historical turn needs positive provenance: the session prefix of its turn_id
must match ``worker_session_id`` in the covering task_runs.metadata. Time overlap alone
does not prove worker ownership. Exactly one run of this profile may overlap
the turn, including runs whose host is unknown; the turn must start in that
closed run and have a known execution host. Dispatcher-only ``studio`` is
not evidence of local execution and is skipped. Open runs
only count for overlap detection. A final turn can end
after its own completion tool closes the run. Missing provenance, placement
reapplication failures, overlapping runs, gateway turns and already stamped rows
are left unchanged.

Dry-run is the default; ``--apply`` takes a WAL-safe VACUUM INTO backup first.

    python3 scripts/backfill-turn-placement-host.py --home ~/.hermes/profiles/daedalus \\
        --board ~/.hermes/kanban.db --profile daedalus            # inspect
    ... --apply                                                    # write
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

LOCAL_RUN_HOST = "studio"
# A worker turn is recorded by the CLI; a gateway turn on these platforms never ran in a worker.
WORKER_PLATFORMS = ("", "cli")


def _run_index(runs: list) -> tuple[list, list]:
    """Runs sorted by start, plus, for every prefix, the two runs with the
    latest end (open = +inf): O(R log R) once instead of a scan per turn."""
    runs = sorted(runs, key=lambda r: r[0])
    top: list = [(None, None)]
    first = second = None
    for run in runs:
        end = math.inf if run[1] is None else run[1]
        if first is None or end > first[0]:
            first, second = (end, run), first
        elif second is None or end > second[0]:
            second = (end, run)
        top.append((first, second))
    return [r[0] for r in runs], top


def _only_overlap(index: tuple[list, list], ts_start, ts_end):
    """The single run overlapping [ts_start, ts_end], else None (none or 2+).

    Runs starting by ``ts_end`` overlap iff they end at or after ``ts_start``;
    two such runs exist iff the prefix's second-latest end reaches it."""
    starts, top = index
    first, second = top[bisect.bisect_right(starts, ts_end)]
    if first is None or first[0] < ts_start:
        return None
    if second is not None and second[0] >= ts_start:
        return None
    return first[1]


def _plan(turns_db: Path, board_db: Path, profile: str, *,
          turns_conn: sqlite3.Connection | None = None,
          board_conn: sqlite3.Connection | None = None) -> list[tuple[str, str]]:
    with (nullcontext(board_conn) if board_conn is not None else
          sqlite3.connect(board_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as board:
        run_cols = {r[1] for r in board.execute("PRAGMA table_info(task_runs)")}
        if "metadata" not in run_cols:
            return []  # No positive worker-session provenance in this board.
        runs = board.execute(
            "SELECT started_at, ended_at, host, metadata FROM task_runs "
            "WHERE profile = ?",
            (profile,),
        ).fetchall()
    with (nullcontext(turns_conn) if turns_conn is not None else
          sqlite3.connect(turns_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        if "placement_host" not in cols:
            sys.exit(f"{turns_db}: no placement_host column yet (deploy the schema first)")
        platform = "COALESCE(platform, '')" if "platform" in cols else "''"
        rows = conn.execute(
            f"SELECT turn_id, ts_start, ts_end, {platform} FROM turns "
            "WHERE placement_host IS NULL"
        ).fetchall()
    index = _run_index(runs)
    plan = []
    for turn_id, ts_start, ts_end, plat in rows:
        # agent.turn_facade: <session_id>:<effective_task_id>:<nonce>.
        parts = turn_id.rsplit(":", 2)
        if len(parts) != 3 or not all(parts):
            continue
        session_id = parts[0]
        if plat not in WORKER_PLATFORMS or not ts_start or not ts_end:
            continue
        end = ts_end
        if end < ts_start:
            continue
        only = _only_overlap(index, ts_start, end)
        if only is None:
            continue  # no run, or two or more (open/unknown-host runs included)
        start, stop, host, metadata = only
        if stop is None or not (start <= ts_start <= stop) or not host or not host.strip():
            continue
        try:
            provenance = json.loads(metadata or "null")
        except (TypeError, ValueError):
            continue
        if not isinstance(provenance, dict) or provenance.get("worker_session_id") != session_id:
            continue
        if provenance.get("placement_reapply_failed", 0):
            continue  # The run may contain Studio calls despite its remote host.
        # `studio` is the dispatcher host, not evidence of the tool backend.
        if host == LOCAL_RUN_HOST:
            continue
        plan.append((turn_id, host))
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, required=True, help="the profile's HERMES_HOME")
    parser.add_argument("--board", type=Path, required=True, help="the board's kanban.db")
    parser.add_argument("--profile", required=True, help="task_runs.profile of this store")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args()
    turns_db = args.home / "blackbox" / "turns.db"
    for path in (turns_db, args.board):
        if not path.is_file():
            parser.error(f"does not exist: {path}")

    plan = _plan(turns_db, args.board, args.profile)
    by_host = Counter(h for _, h in plan)
    print(f"{turns_db}: mode={'apply' if args.apply else 'dry-run'} would_stamp={len(plan)} "
          f"by_host={dict(sorted(by_host.items()))}")
    if not args.apply or not plan:
        return 0
    backup = turns_db.with_name("turns-before-placement-backfill-"
                                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".db")
    with sqlite3.connect(str(turns_db), timeout=30) as conn:
        conn.execute("VACUUM INTO ?", (str(backup),))
    print(f"backup={backup}")
    # Lock the provenance database through the turns commit as well: a turns
    # lock alone cannot stop task_runs metadata changing after revalidation.
    with sqlite3.connect(args.board.resolve().as_uri() + "?mode=rw", uri=True, timeout=30) as board:
        board.execute("BEGIN IMMEDIATE")
        with sqlite3.connect(str(turns_db), timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = set(_plan(turns_db, args.board, args.profile,
                                turns_conn=conn, board_conn=board))
            cur = conn.executemany(
                "UPDATE turns SET placement_host = ? WHERE turn_id = ? AND placement_host IS NULL",
                [(h, t) for t, h in plan if (t, h) in current],
            )
            print(f"stamped={cur.rowcount}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
