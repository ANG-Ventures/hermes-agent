"""C6 (FleetReview backfill, #966): a background parity/integrity scan that
flags ``messages_fts`` must make search fail CLOSED to the LIKE path. A valid
but lagging index answers MATCH without error, so before this the flag only
reached ``/lcm doctor`` and search silently omitted stored messages."""
from __future__ import annotations

import time
from pathlib import Path

import plugins.context_engine.lcm.db_bootstrap as B
from plugins.context_engine.lcm.store import MessageStore, build_message_fts_spec


def _lagging_store(db: Path) -> MessageStore:
    st = MessageStore(db_path=str(db))
    conn = st._conn
    assert conn is not None
    text = "zebra lagging needle"
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, search_content, timestamp) "
        "VALUES (?,?,?,?,?)",
        ("s", "user", st._cipher.encrypt_text(text, field="content"), text, 1.0),
    )
    # Drop the row from the index only: MATCH still works, just misses it.
    conn.execute(
        "INSERT INTO messages_fts(messages_fts, rowid, search_content) VALUES('delete', ?, ?)",
        (cur.lastrowid, text),
    )
    conn.commit()
    return st


def test_flagged_index_search_falls_back_to_like(tmp_path):
    st = _lagging_store(tmp_path / "lcm.db")
    try:
        assert st.search("zebra") == []  # the lag the flag describes
        B._record_integrity_failed(
            st._conn, build_message_fts_spec(),
            detail="content/index row-count mismatch (parity check)", now=time.time(),
        )
        st._conn.commit()
        hits = st.search("zebra")
        assert len(hits) == 1 and "needle" in hits[0]["content"]
    finally:
        st.close()


def test_unflagged_index_stays_on_fts(tmp_path):
    st = _lagging_store(tmp_path / "lcm.db")
    try:
        assert st._message_fts_flagged() is False
        assert st.search("zebra") == []
    finally:
        st.close()
