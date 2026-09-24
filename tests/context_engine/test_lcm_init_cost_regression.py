"""REGRESSION: LCM engine load / session-start cost must not scale with row count.

Three Apollo freezes in three days, all the same class — something on the
engine-load path (``plugins/context_engine/__init__.py::_LOAD_LOCK``, every
turn's ``init_agent`` waits on it) or the per-agent session-start path cost
O(rows) of a 2.5 M-row / 11 GB ``messages`` table, and fixtures with ten rows
could not see it:

  1. 2026-09-22 search_content backfill probe: no index on the NULL predicate (#887).
  2. 2026-09-22 ingested_at backfill: no done-marker (#902).
  3. 2026-09-24 FTS row-count parity: ``SELECT COUNT(*) FROM messages`` +
     ``COUNT(*) FROM messages_fts_docsize`` on EVERY load, inside
     ``_fts_needs_rebuild_structural`` (card t_d3963974). The previous revision
     of THIS test whitelisted ``USING COVERING INDEX`` and skipped every
     ``messages_fts*`` statement — it was green on the exact code that froze
     Apollo. Plus two write-lock amplifiers on the same paths: three
     unconditional metadata upserts per load, and the lifecycle GC holding
     ``BEGIN IMMEDIATE`` across two ``SELECT DISTINCT session_id`` full scans on
     every session start.

This test builds a store with N rows, then traces EVERYTHING a steady-state
engine construction + ``on_session_start`` issues and asserts, independently of
wall clock:

  1. Query plan: no statement may traverse ``messages``, ``messages_fts*`` or
     ``summary_nodes`` — ANY ``SCAN`` of those tables is a finding, covering
     index or not (a covering-index COUNT still touches every row), unless the
     statement is row-bounded by a ``LIMIT`` clause.
  2. Write lock: engine construction (the part under ``_LOAD_LOCK``) must
     succeed while ANOTHER connection holds the SQLite write lock, with a zero
     busy timeout — i.e. a no-op open issues no write and can never queue
     behind an in-flight writer.
  3. A background-scan corruption flag survives an ordinary open (the old
     unconditional ``_clear_integrity_failed`` erased it on every load).
  4. Scale ratio: 200 vs 20 000 rows, second-open time must be < 5x.

Plus the source-contract lint (test_lcm_backfill_cost.py) that refuses any
unbounded, unmarked UPDATE/DELETE on the init path at all.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from plugins.context_engine.lcm import db_bootstrap
from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine
from plugins.context_engine.lcm.lifecycle_state import LifecycleStateStore
from plugins.context_engine.lcm.store import MessageStore, build_message_fts_spec

# Any full traversal of one of these tables on a per-load / per-session-start
# path is the freeze class. Both EXPLAIN output dialects are matched
# ("SCAN messages ..." on SQLite >= 3.36, "SCAN TABLE messages ..." before).
_GUARDED_TABLES = ("messages", "messages_fts", "summary_nodes", "nodes_fts")
_SCAN_RE = re.compile(
    r"\bSCAN (?:TABLE )?(?:%s)(?:_\w+)?\b" % "|".join(_GUARDED_TABLES), re.IGNORECASE
)
_LIFECYCLE_ROWS = 250  # > LCMConfig.empty_lifecycle_gc_threshold (200): GC fires


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
    lifecycle = LifecycleStateStore(db)
    for i in range(_LIFECYCLE_ROWS):
        lifecycle.bind_session("empty-%d" % i, conversation_id="conv-%d" % i)
    lifecycle.close()


def _close(engine: LCMEngine) -> None:
    engine._close_storage()


def _engine(db: Path) -> LCMEngine:
    return LCMEngine(
        config=LCMConfig(database_path=str(db)),
        hermes_home=str(db.parent),
    )


def _traced_reopen(db: Path) -> tuple[float, list[str], LCMEngine]:
    """Construct a fresh engine + bind a session with a trace hook.

    Returns (seconds, [sql...], engine). The ENGINE is returned (not a
    connection) so the caller keeps it alive: an earlier revision returned
    `st._conn`, the store was garbage-collected, the connection closed, and
    every EXPLAIN raised sqlite3.Error into a `continue` — the check passed on
    the exact code that froze Apollo.
    """
    seen: list[str] = []
    real_connect = sqlite3.connect
    caller = threading.get_ident()

    def connect(*a, **k):
        c = real_connect(*a, **k)

        def trace(sql: str) -> None:
            # Only statements issued on the LOADING thread count: that is the
            # thread holding _LOAD_LOCK / running init_agent. Work the engine
            # hands to its own background threads (the throttled FTS parity +
            # integrity lane, which opens a private connection) is the fix, not
            # the bug — it is joined below so it cannot leak into a later test.
            if threading.get_ident() == caller:
                seen.append(" ".join(sql.split()))

        c.set_trace_callback(trace)
        return c

    sqlite3.connect = connect
    try:
        t0 = time.perf_counter()
        engine = _engine(db)
        engine.on_session_start("probe-session", platform="cli")
        dt = time.perf_counter() - t0
        return dt, seen, engine
    finally:
        sqlite3.connect = real_connect
        db_bootstrap.join_background_integrity_scans(timeout=30.0)


def _plans_that_scan_guarded_tables(db: Path, sqls: list[str]) -> list[str]:
    """Every statement in `sqls` whose plan traverses a guarded table.

    EXPLAINs on a FRESH connection to the same file. A statement that cannot be
    explained is reported as a finding, never skipped — a probe that skips on
    error is a probe that passes on the bug. ``USING COVERING INDEX`` is NOT an
    exemption: ``SELECT COUNT(*) FROM messages`` plans as a covering-index scan
    and still reads all 2.5 M rows (11 s cold on the fleet DB). The only
    exemption is a row-bounded statement (a ``LIMIT`` clause), e.g. the O(1)
    partial-index presence probe ``SELECT 1 FROM messages WHERE search_content
    IS NULL LIMIT 1``.
    """
    bad = []
    conn = sqlite3.connect(str(db))
    for sql in sqls:
        head = sql.split(" ", 1)[0].upper()
        if head not in ("SELECT", "UPDATE", "DELETE", "INSERT", "REPLACE"):
            continue
        lowered = sql.lower()
        if not any(t in lowered for t in _GUARDED_TABLES):
            continue
        if "sqlite_master" in lowered or "lcm_migration_state" in lowered:
            continue
        if sql.startswith("--"):
            continue  # FTS5-internal shadow statements echoed by the trace hook
        try:
            plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
        except sqlite3.Error as exc:
            bad.append(f"{sql[:100]}  ->  COULD NOT EXPLAIN ({exc})")
            continue
        if _SCAN_RE.search(plan) and " limit " not in lowered:
            bad.append(f"{sql[:100]}  ->  {plan}")
    conn.close()
    return bad


def test_second_open_issues_no_full_scan_of_guarded_tables(tmp_path):
    db = tmp_path / "lcm.db"
    _fill(db, 200)
    _, sqls, engine = _traced_reopen(db)
    bad = _plans_that_scan_guarded_tables(db, sqls)
    _close(engine)
    assert not bad, (
        "engine load / session start full-scans a guarded table (the Apollo-freeze class):\n"
        + "\n".join(bad)
    )


def test_engine_construction_needs_no_write_lock(tmp_path, monkeypatch):
    """Steady-state engine construction must not need the SQLite write lock.

    Everything constructed here runs under ``_LOAD_LOCK`` on every turn. An
    unconditional write (metadata DELETE / migration-marker upsert) queues
    behind whoever holds the write lock — measured on the fleet DB: the
    background FTS integrity-check holds it for 139 s, the old lifecycle GC for
    the length of two full-table scans — and every turn waits with it.
    """
    db = tmp_path / "lcm.db"
    _fill(db, 200)
    _close(_engine(db))  # first open may legitimately write markers; steady state is the second
    real_connect = sqlite3.connect

    def connect(*a, **k):
        k["timeout"] = 0.0
        return real_connect(*a, **k)

    monkeypatch.setattr(db_bootstrap, "SQLITE_BUSY_TIMEOUT_MS", 0)
    monkeypatch.setattr(sqlite3, "connect", connect)
    holder = real_connect(str(db), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        try:
            engine = _engine(db)
        except sqlite3.OperationalError as exc:
            pytest.fail(
                f"engine construction needed the write lock (issued a write on the load path): {exc}"
            )
        _close(engine)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_ordinary_open_keeps_background_corruption_flag(tmp_path):
    db = tmp_path / "lcm.db"
    _fill(db, 200)
    spec = build_message_fts_spec()
    conn = sqlite3.connect(str(db))
    db_bootstrap._record_integrity_failed(conn, spec, detail="probe: index corrupt")
    conn.commit()
    conn.close()
    st = MessageStore(db_path=str(db))
    assert st._conn is not None
    flag = db_bootstrap.load_integrity_failed(st._conn, spec)
    st.close()
    assert flag is not None and "probe" in str(flag["detail"]), (
        "an ordinary (no-op) open erased the background scan's corruption flag"
    )


def test_concurrent_ingest_between_parity_counts_cannot_trigger_rebuild(tmp_path):
    """The two parity reads must see one WAL snapshot, not two commits.

    On fork/main the first COUNT and the docsize COUNT are separate autocommit
    statements. Force a real ingest/FTS-trigger commit between them; a false
    mismatch returned True and the caller dropped+rebuilt the entire index
    under _LOAD_LOCK. No mocked FTS, no timing race in the test.
    """
    db = tmp_path / "lcm.db"
    _fill(db, 200)
    writer = sqlite3.connect(str(db))
    reader = sqlite3.connect(str(db))
    spec = build_message_fts_spec()
    committed = False

    def after_first_count(sql: str) -> None:
        nonlocal committed
        if not committed and sql.strip().upper() == 'SELECT COUNT(*) FROM "MESSAGES"':
            writer.execute(
                "INSERT INTO messages(session_id, role, content, timestamp) "
                "VALUES ('race', 'user', 'new turn', 1)"
            )
            writer.commit()
            committed = True

    reader.set_trace_callback(after_first_count)
    try:
        needs_rebuild = db_bootstrap._fts_needs_rebuild_structural(reader, spec)
        assert committed, "probe never interleaved an ingest after the first parity count"
        assert not needs_rebuild, "a concurrent valid FTS insert caused a false rebuild"
    finally:
        reader.close()
        writer.close()


def test_init_time_does_not_scale_with_row_count(tmp_path):
    small = tmp_path / "small.db"
    big = tmp_path / "big.db"
    _fill(small, 200)
    _fill(big, 20_000)
    # warm both once (first open may build indexes) then measure the steady-state second open
    _close(_traced_reopen(small)[2])
    _close(_traced_reopen(big)[2])

    def best(db):
        times = []
        for _ in range(3):
            dt, _, engine = _traced_reopen(db)
            _close(engine)
            times.append(dt)
        return min(times)

    ts = best(small)
    tb = best(big)
    ratio = tb / max(ts, 1e-4)
    # 100x rows; a full scan would be ~100x time. Healthy init is O(1) in rows.
    assert ratio < 5.0, f"init scales with row count: {ts:.4f}s @200 rows vs {tb:.4f}s @20000 rows (ratio {ratio:.1f}x)"
