"""archive_and_compact keeps its read/hash work out of the state.db write lock (t_d63a8ca8).

Live telemetry (2026-11-10) showed archive_and_compact holding the state.db write lock 18-45 s with ~1 s
of it in commit: the display re-fold recomputed every row's identity hash inside the transaction. The
identities are now hashed before BEGIN IMMEDIATE and re-checked in the transaction against the stored
``display_identity`` (which the ``messages_display_identity_update`` trigger NULLs on any identity edit).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import hermes_state
import hermes_state_messages
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path: Path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session("s", source="test")
    yield d
    d.close()


def _seed(db: SessionDB, n: int) -> None:
    db.append_messages_batch("s", [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(n)])


def _display_columns(db: SessionDB):
    with db._read_ctx() as conn:
        return conn.execute(
            "SELECT id, display_order, display_identity FROM messages WHERE session_id = ? "
            "AND (active = 1 OR compacted = 1) ORDER BY id", ("s",)).fetchall()


def _count_identity_hashes(monkeypatch):
    calls = {"in_txn": 0, "outside": 0}
    real = SessionDB._display_identity
    real_execute_write = SessionDB._execute_write
    state = {"in_txn": False}

    def counting_identity(key):
        calls["in_txn" if state["in_txn"] else "outside"] += 1
        return real(key)

    def tracking_execute_write(self, fn, *a, **k):
        def wrapped(conn):
            state["in_txn"] = True
            try:
                return fn(conn)
            finally:
                state["in_txn"] = False
        return real_execute_write(self, wrapped, *a, **k)

    monkeypatch.setattr(SessionDB, "_display_identity", staticmethod(counting_identity))
    monkeypatch.setattr(SessionDB, "_execute_write", tracking_execute_write)
    return calls


SUMMARY = [{"role": "user", "content": "[CONTEXT COMPACTION] summary"},
           {"role": "assistant", "content": "Continuing."}]


def test_history_identities_are_hashed_outside_the_write_lock(db, monkeypatch):
    _seed(db, 200)
    calls = _count_identity_hashes(monkeypatch)

    db.archive_and_compact("s", SUMMARY, watermark=db.get_active_message_watermark("s"))

    # 200 history rows hashed by the prescan; inside the transaction only the 2 rows it inserted
    # (their insert binding plus the re-fold of rows the prescan never saw).
    assert calls["outside"] >= 200
    assert calls["in_txn"] <= 2 * len(SUMMARY)


def test_write_lock_hold_excludes_the_history_hash_work(db, monkeypatch):
    """The hold (hold_started..hold_ended, the ledger the live telemetry reads) covers the write phase only:
    with each identity hash slowed to 2 ms, the ~0.6 s of history hashing lands before the hold opens.
    Measured as hashing time INSIDE the hold window, not as an absolute hold bound (host load varies)."""
    _seed(db, 300)
    real = SessionDB._display_identity
    window = {"open": False}
    spent = {"in_hold": 0.0, "outside": 0.0}

    def slow_identity(key):
        t = time.monotonic()
        time.sleep(0.002)
        result = real(key)
        spent["in_hold" if window["open"] else "outside"] += time.monotonic() - t
        return result

    monkeypatch.setattr(SessionDB, "_display_identity", staticmethod(slow_identity))
    holds = []
    real_started, real_ended = hermes_state.hold_started, hermes_state.hold_ended

    def started(db_path, op):
        window["open"] = True
        return real_started(db_path, op)

    def ended(token):
        window["open"] = False
        holds.append(token)
        return real_ended(token)

    monkeypatch.setattr(hermes_state, "hold_started", started)
    monkeypatch.setattr(hermes_state, "hold_ended", ended)
    db.archive_and_compact("s", SUMMARY, watermark=db.get_active_message_watermark("s"))

    assert len(holds) == 1
    assert spent["outside"] >= 300 * 0.002  # every history row was hashed, before the lock
    # Inside: only the rows the transaction itself inserted (2) are hashed, at bind and in the re-fold.
    assert spent["in_hold"] < spent["outside"] / 20, spent


def test_concurrent_identity_edit_after_prescan_is_recomputed(db, monkeypatch):
    """A row whose identity changes between the prescan and BEGIN IMMEDIATE must not keep the stale hash."""
    _seed(db, 6)
    real_prescan = SessionDB._prescan_display_identities
    target = {}

    def prescan_then_concurrent_edit(self, session_id):
        found = real_prescan(self, session_id)
        row_id = min(found)
        target["id"] = row_id
        # Another writer rewrites the first row after the prescan read it (an /edit, a sidecar backfill).
        self._write_rowcount("UPDATE messages SET content = ? WHERE id = ?", ("edited after prescan", row_id))
        return found

    monkeypatch.setattr(SessionDB, "_prescan_display_identities", prescan_then_concurrent_edit)
    db.archive_and_compact("s", SUMMARY, watermark=db.get_active_message_watermark("s"))

    with db._read_ctx() as conn:
        row = conn.execute("SELECT id, role, content, timestamp, tool_call_id, tool_calls, tool_name, display_kind, "
                           "display_metadata, display_identity FROM messages WHERE id = ?", (target["id"],)).fetchone()
    assert row["content"] == "edited after prescan"
    assert row["display_identity"] == db._display_identity(db._display_dedupe_key(row))


def test_drifted_prescan_is_not_reused_after_aba(db):
    """Prism d7b6f8936a56: row holds A but stores H(B) (drift). After the prescan another writer turns it
    into B and reconciles, restoring H(B). The stale prescan must not fold the row with a copy of A."""
    _seed(db, 4)
    rows = _display_columns(db)
    r0, r2 = rows[0]["id"], rows[2]["id"]
    with db._read_ctx() as conn:
        a = conn.execute("SELECT content, timestamp FROM messages WHERE id = ?", (r0,)).fetchone()
    db._write_rowcount("UPDATE messages SET content = ?, timestamp = ? WHERE id = ?", (a[0], a[1], r2))
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))
    with db._read_ctx() as conn:
        row = conn.execute(f"SELECT {hermes_state_messages._RECONCILE_KEY_COLUMNS} FROM messages WHERE id = ?",
                           (r0,)).fetchone()
    key_b = (*db._display_dedupe_key(row)[:1], "B content", *db._display_dedupe_key(row)[2:])
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET display_identity = ? WHERE id = ?", (db._display_identity(key_b), r0)))

    stale = db._prescan_display_identities("s")
    assert stale[r0][0] != stale[r0][1]  # drifted: stores H(B), holds A
    db._write_rowcount("UPDATE messages SET content = ? WHERE id = ?", ("B content", r0))
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))  # restores H(B) on r0
    assert dict((r[0], r[2]) for r in _display_columns(db))[r0] == stale[r0][0]

    db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s", stale))
    after = {r[0]: (r[1], r[2]) for r in _display_columns(db)}
    assert after[r0] == (r0, db._display_identity(key_b))
    assert after[r2][0] == r2  # the copy of A is not folded under the row that now holds B


def test_prescanned_refold_matches_a_full_recompute(db):
    """Same display_order/display_identity whether the re-fold used the prescan or hashed every row."""
    _seed(db, 12)
    carried = [{"role": "user", "content": "turn 10"}, {"role": "assistant", "content": "turn 11"}]
    db.archive_and_compact("s", [*SUMMARY, *carried], tail_count=2)
    db.append_messages_batch("s", [{"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}])
    db.archive_and_compact("s", [*SUMMARY, *carried, {"role": "user", "content": "u2"},
                                 {"role": "assistant", "content": "a2"}], tail_count=4)
    with_prescan = _display_columns(db)

    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET display_order = NULL, display_identity = NULL WHERE session_id = ?", ("s",)))
    db._execute_write(lambda conn: db._reconcile_display_orders(conn, "s"))  # no prescan: hashes every row
    assert [tuple(r) for r in _display_columns(db)] == [tuple(r) for r in with_prescan]
