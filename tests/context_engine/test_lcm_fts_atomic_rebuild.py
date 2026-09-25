"""REGRESSION: an LCM FTS repair must be atomic to every other connection.

2026-09-24 Apollo freeze #3 had three defects. #966 fixed two of them: the O(rows)
two-snapshot COUNT parity probe is now off the engine-load path. The third is here.
`repair_external_content_fts` ran DROP TABLE / CREATE VIRTUAL TABLE / 'rebuild' /
DROP+CREATE TRIGGER as separately autocommitted statements. Python's legacy sqlite3
isolation opens no implicit transaction for DDL. So for the whole rebuild
(13-28 min on the 11.9 GB fleet store) other connections saw the index MISSING
(live: "LCM ingest failed: no such table: main.messages_fts") or EMPTY (docsize=0
vs 2.79 M messages, captured on disk by a snapshot). A dropped trigger also let
concurrent inserts bypass the index, which is real drift.

Both tests below were RED on fork/main dc228d5810.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import plugins.context_engine.lcm.db_bootstrap as B
from plugins.context_engine.lcm.db_bootstrap import repair_external_content_fts
from plugins.context_engine.lcm.store import MessageStore, build_message_fts_spec


def _store_with_lagging_index(db: Path, n: int = 300, *, structural: bool = False):
    st = MessageStore(db_path=str(db))
    conn = st._conn
    assert conn is not None
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
        [
            ("s", "user", st._cipher.encrypt_text("m%d" % i, field="content"), float(i))
            for i in range(n)
        ],
    )
    conn.commit()
    if structural:
        # Structural damage (a missing FTS5 shadow table): the ENGINE-LOAD path
        # (throttle=True) still rebuilds inline for this, with no prior write to
        # open a transaction, so every DDL statement autocommits on its own.
        conn.execute("DROP TABLE messages_fts_config")
    else:
        # Lagging index: drop the newest indexed doc so explicit repair must rebuild.
        conn.execute(
            "DELETE FROM messages_fts_docsize WHERE id = (SELECT MAX(id) FROM messages_fts_docsize)"
        )
    conn.commit()
    return st, conn


@pytest.mark.parametrize(
    "structural,throttle",
    [(True, True), (False, False)],
    ids=["engine-load-structural", "explicit-repair-parity"],
)
def test_rebuild_is_atomic_to_other_connections(tmp_path, structural, throttle):
    db = tmp_path / "lcm.db"
    st, conn = _store_with_lagging_index(db, structural=structural)
    spec = build_message_fts_spec()
    before = conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0]

    observer = sqlite3.connect(str(db), timeout=0.1)
    seen: list[tuple[int, int, int]] = []

    def observe(_sql: str) -> None:
        exists = observer.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts'"
        ).fetchone()[0]
        docs = (
            observer.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0]
            if exists
            else -1
        )
        trig = observer.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='msg_fts_insert'"
        ).fetchone()[0]
        seen.append((exists, docs, trig))

    conn.set_trace_callback(observe)
    try:
        result = repair_external_content_fts(conn, spec, throttle=throttle)
    finally:
        conn.set_trace_callback(None)
    assert result["rebuilt"] is True
    torn = [s for s in seen if s[0] != 1 or s[1] < before or s[2] != 1]
    assert not torn, (
        f"another connection saw a torn index/trigger during repair "
        f"(exists, docs, insert_trigger)={torn[:5]} — the repair is not one transaction"
    )
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM messages_fts_docsize").fetchone()[0] == total
    assert not conn.in_transaction
    observer.close()
    del st


def test_failed_rebuild_rolls_back_to_the_old_index(tmp_path, monkeypatch):
    db = tmp_path / "lcm.db"
    st, conn = _store_with_lagging_index(db)
    spec = build_message_fts_spec()
    real_drop = B._drop_fts_table

    def drop_then_die(c, name):
        real_drop(c, name)
        raise sqlite3.OperationalError("simulated crash mid-rebuild")

    monkeypatch.setattr(B, "_drop_fts_table", drop_then_die)
    with pytest.raises(sqlite3.OperationalError):
        repair_external_content_fts(conn, spec, throttle=False)
    assert not conn.in_transaction
    other = sqlite3.connect(str(db))
    assert (
        other.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'messages_fts'").fetchone()[0]
        == 1
    ), "a failed rebuild left the FTS index dropped"
    other.close()
    del st
