"""REGRESSION: MessageStore init cost must not scale with the number of rows.

Both 2026-09-22 Apollo freezes were one-time backfills that re-scanned the whole
`messages` table on every boot (search_content: no index on the NULL predicate;
ingested_at: no done-marker). Each one was invisible to the unit suite because
fixtures have ten rows. This test builds a store with N rows and asserts that
opening it a SECOND time performs no statement whose cost is proportional to N —
measured two ways so the verdict does not depend on wall clock:

  1. Trace every statement the reopen issues and EXPLAIN it: refuse any plan that
     is a bare `SCAN messages` without `USING INDEX`.
  2. A ratio: init time at N=20 000 vs N=200 must be < 5x (a scan would be ~100x).

Plus the source-contract lint (test_lcm_backfill_cost.py) that refuses any
unbounded, unmarked UPDATE/DELETE on the init path at all. Three layers because
two independent instances of this class shipped in one day.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from plugins.context_engine.lcm.store import MessageStore


def _fill(db: Path, n: int) -> None:
    st = MessageStore(db_path=str(db))
    conn = st._conn
    assert conn is not None
    rows = [
        ("s%d" % (i % 50), "user", st._cipher.encrypt_text("m%d" % i, field="content"), float(i))
        for i in range(n)
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


def _traced_reopen(db: Path) -> tuple[float, list[str], MessageStore]:
    """Reopen the store with a trace hook; return (seconds, [sql...], store).

    The STORE is returned (not its connection) so the caller keeps it alive:
    an earlier revision returned `st._conn`, the store was garbage-collected,
    the connection closed, and every EXPLAIN raised sqlite3.Error into a
    `continue` — the check passed on the exact code that froze Apollo.
    """
    seen: list[str] = []
    real_connect = sqlite3.connect

    def connect(*a, **k):
        c = real_connect(*a, **k)
        c.set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
        return c

    sqlite3.connect = connect
    try:
        t0 = time.perf_counter()
        st = MessageStore(db_path=str(db))
        dt = time.perf_counter() - t0
        assert st._conn is not None
        return dt, seen, st
    finally:
        sqlite3.connect = real_connect


def _plans_that_scan_messages(db: Path, sqls: list[str]) -> list[str]:
    """Every statement in `sqls` that reads/writes `messages` via a full SCAN.

    EXPLAINs on a FRESH connection to the same file. A statement that cannot be
    explained is reported as a finding, never skipped — a probe that skips on
    error is a probe that passes on the bug.
    """
    bad = []
    conn = sqlite3.connect(str(db))
    for sql in sqls:
        head = sql.split(" ", 1)[0].upper()
        if head not in ("SELECT", "UPDATE", "DELETE") or " messages" not in sql.lower():
            continue
        if "sqlite_master" in sql or "lcm_migration_state" in sql or "messages_fts" in sql:
            continue
        try:
            plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
        except sqlite3.Error as exc:
            bad.append(f"{sql[:90]}  ->  COULD NOT EXPLAIN ({exc})")
            continue
        if "SCAN messages" in plan and "USING INDEX" not in plan and "USING COVERING INDEX" not in plan:
            bad.append(f"{sql[:90]}  ->  {plan}")
    conn.close()
    return bad


def test_second_open_issues_no_full_scan_of_messages(tmp_path):
    db = tmp_path / "lcm.db"
    _fill(db, 200)
    _, sqls, st = _traced_reopen(db)
    bad = _plans_that_scan_messages(db, sqls)
    del st
    assert not bad, "init path full-scans `messages` (this is the Apollo-freeze class):\n" + "\n".join(bad)


def test_init_time_does_not_scale_with_row_count(tmp_path):
    small = tmp_path / "small.db"
    big = tmp_path / "big.db"
    _fill(small, 200)
    _fill(big, 20_000)
    # warm both once (first open may build indexes) then measure the steady-state second open
    _traced_reopen(small)
    _traced_reopen(big)
    ts = min(_traced_reopen(small)[0] for _ in range(3))
    tb = min(_traced_reopen(big)[0] for _ in range(3))
    ratio = tb / max(ts, 1e-4)
    # 100x rows; a full scan would be ~100x time. Healthy init is O(1) in rows.
    assert ratio < 5.0, f"init scales with row count: {ts:.4f}s @200 rows vs {tb:.4f}s @20000 rows (ratio {ratio:.1f}x)"
