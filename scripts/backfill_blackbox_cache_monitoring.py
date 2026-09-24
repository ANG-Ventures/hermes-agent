"""Guarded one-time Blackbox cache-monitoring backfill for one profile home.

Run from the repo root with the repo venv:
python -m scripts.backfill_blackbox_cache_monitoring --home PATH  # inspect
python -m scripts.backfill_blackbox_cache_monitoring --home PATH --apply

Repeat for each profile's HERMES_HOME after deploying the schema. A WAL-safe
VACUUM INTO backup is taken before every apply; history's tier fields stay NULL.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = args.home / "blackbox" / "turns.db"
    if not db.is_file():
        parser.error(f"Blackbox store does not exist: {db}")
    uri = f"file:{db}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as conn:
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("turns", "turn_api_calls")
        }
    print(f"{db}: turns={counts['turns']} calls={counts['turn_api_calls']}; "
          f"mode={'apply' if args.apply else 'dry-run'}")
    if not args.apply:
        return
    # VACUUM INTO snapshots committed WAL frames without stopping the writer.
    backup = db.with_name("turns-before-cache-backfill-" +
                          datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".db")
    with sqlite3.connect(str(db), timeout=30) as conn:
        conn.execute("VACUUM INTO ?", (str(backup),))
    print(f"backup={backup}")
    os.environ["HERMES_HOME"] = str(args.home)
    from plugins.blackbox.store import backfill_cache_monitoring
    backfill_cache_monitoring()
    with sqlite3.connect(uri, uri=True, timeout=30) as conn:
        covered = conn.execute(
            "SELECT count(*) FROM turn_api_calls WHERE lane_family IS NOT NULL"
        ).fetchone()[0]
        gaps = conn.execute("SELECT count(*) FROM turns WHERE gap_prev_turn_s IS NOT NULL").fetchone()[0]
        first = conn.execute("SELECT count(*) FROM turns WHERE first_call_cache_miss IS NOT NULL").fetchone()[0]
    print(f"verified: lane_family={covered}, gap_prev_turn_s={gaps}, first_call_cache_miss={first}")


if __name__ == "__main__":
    main()
