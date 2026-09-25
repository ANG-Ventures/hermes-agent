"""Session-scoped LCM reads must not see soft-hidden replay copies.

The replay-block dedup migration marks the NON-canonical copy of a replayed
block with ``superseded_by`` (the higher store_ids, i.e. rows that sit in the
session tail). Search/grep already filtered them; the per-session read paths
(tail, count, token total, messages/after, range, load page/window) did not,
so re-bind reconcile kept matching incoming replays against hidden rows and
session counts kept counting them.
"""
from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine
from plugins.context_engine.lcm.store import MessageStore

STORE_PY = Path(__file__).resolve().parents[2] / "plugins" / "context_engine" / "lcm" / "store.py"


def _msgs(lo: int, hi: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(lo, hi)
    ]


def _hide(store: MessageStore, store_ids: list[int]) -> None:
    store._conn.executemany(
        "UPDATE messages SET superseded_by='replay-dedup-test' WHERE store_id=?",
        [(sid,) for sid in store_ids],
    )
    store._conn.commit()


def _seed_with_hidden_block(store: MessageStore, session: str = "S") -> tuple[list[int], list[int]]:
    """m0..m59, then a replayed copy of m40..m59 (hidden), then m60..m69.

    Returns (canonical store_ids in order, hidden store_ids).
    """
    canonical = [store.append(session, m) for m in _msgs(0, 60)]
    hidden = [store.append(session, m) for m in _msgs(40, 60)]
    canonical += [store.append(session, m) for m in _msgs(60, 70)]
    _hide(store, hidden)
    store._conn.execute("UPDATE messages SET token_estimate = 7 WHERE session_id = ?", (session,))
    store._conn.commit()
    return canonical, hidden


class TestStoreSessionReadsHideSuperseded:
    def test_every_session_read_excludes_hidden_rows(self, tmp_path):
        store = MessageStore(str(tmp_path / "lcm.db"))
        canonical, hidden = _seed_with_hidden_block(store)
        hidden_set = set(hidden)

        def ids(rows):
            return [r["store_id"] for r in rows]

        assert store.get_session_count("S") == 70
        raw_total, visible_total = store._conn.execute(
            "SELECT SUM(token_estimate), "
            "SUM(CASE WHEN superseded_by IS NULL THEN token_estimate ELSE 0 END) "
            "FROM messages WHERE session_id='S'"
        ).fetchone()
        assert (raw_total, visible_total) == (90 * 7, 70 * 7)
        assert store.get_session_token_total("S") == visible_total
        assert ids(store.get_session_messages("S")) == canonical
        assert ids(store.get_session_messages_after("S", after_store_id=0)) == canonical
        assert ids(store.get_session_tail("S", limit=30)) == canonical[-30:]
        assert ids(store.get_range("S")) == canonical
        assert store.count_session_load_messages("S") == 70
        assert ids(store.load_session_page("S", limit=1000)) == canonical
        # A window anchored right after the hidden block steps over it.
        anchor = canonical[60]
        window = ids(store.load_session_window("S", anchor_store_id=anchor, before=2, after=1))
        assert window == [canonical[58], canonical[59], canonical[60], canonical[61]]
        for rows in (
            store.get_session_messages("S"),
            store.get_session_tail("S", limit=1000),
            store.load_session_page("S", limit=1000),
        ):
            assert not hidden_set.intersection(ids(rows))

    def test_raw_reads_by_store_id_still_see_hidden_rows(self, tmp_path):
        """Explicit store_id lookups stay raw: summaries may cite a hidden copy."""
        store = MessageStore(str(tmp_path / "lcm.db"))
        _, hidden = _seed_with_hidden_block(store)
        assert store.get(hidden[0]) is not None

    def test_visible_partial_index_drives_session_reads(self, tmp_path):
        store = MessageStore(str(tmp_path / "lcm.db"))
        _seed_with_hidden_block(store)
        sql = store._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_msg_session_visible'"
        ).fetchone()
        assert sql and "superseded_by IS NULL" in sql[0]
        plan = " ".join(
            str(r[-1])
            for r in store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages "
                "WHERE session_id = ? AND superseded_by IS NULL",
                ("S",),
            )
        )
        assert "idx_msg_session_visible" in plan, plan

    def test_reopen_is_idempotent(self, tmp_path):
        db = str(tmp_path / "lcm.db")
        MessageStore(db)._conn.close()
        store = MessageStore(db)
        n = store._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_msg_session_visible'"
        ).fetchone()[0]
        assert n == 1


class TestRebindReconcileIgnoresHiddenTail:
    def test_full_replay_over_hidden_block_ingests_only_the_new_turn(self, tmp_path):
        db = str(tmp_path / "lcm.db")
        eng = LCMEngine(config=LCMConfig())
        eng._bind_storage(db)
        eng._session_id = "S"
        _seed_with_hidden_block(eng._store)
        before_raw = sqlite3.connect(db).execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        incoming = [dict(m) for m in _msgs(0, 70)] + [
            {"role": "user", "content": "GENUINELY_NEW_TURN"}
        ]
        eng._ingest_cursor_needs_reconcile = True
        eng._ingest_messages(incoming)

        conn = sqlite3.connect(db)
        after_raw = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert after_raw == before_raw + 1, "re-bind replay re-appended stored rows"
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content='GENUINELY_NEW_TURN'"
        ).fetchone()[0] == 1
        assert eng._store.get_session_count("S") == 71


# Detector: every per-session read in store.py carries the visible clause.
_RAW_SESSION_READ_ALLOWLIST = {
    # Deletion must remove hidden copies too.
    "delete_session_messages",
    # Diagnostics report raw bucket counts on purpose.
    "get_source_stats",
    # Retention doctor measures on-disk footprint; hidden copies occupy disk
    # and are purged with their session, so they count there.
    "scan_session_retention_stats",
}


def _functions_reading_session_messages(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        src_parts = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                src_parts.append(sub.value)
        text = " ".join(src_parts)
        if re.search(r"FROM\s+messages\b", text) and re.search(r"\bsession_id\s*=\s*\?", text):
            yield node, text


def test_store_session_reads_all_filter_superseded():
    source = STORE_PY.read_text()
    tree = ast.parse(source)
    offenders = []
    for fn, _text in _functions_reading_session_messages(tree):
        if fn.name in _RAW_SESSION_READ_ALLOWLIST:
            continue
        if re.search(r"^\s*(DELETE|UPDATE)\b", _text.strip(), re.IGNORECASE):
            continue
        body = ast.get_source_segment(source, fn) or ""
        if "_VISIBLE_MESSAGE_CLAUSE" not in body and "superseded_by IS NULL" not in body:
            offenders.append(fn.name)
    assert not offenders, (
        "per-session message reads must filter soft-hidden replay copies "
        f"(add _VISIBLE_MESSAGE_CLAUSE or allowlist with a reason): {offenders}"
    )
