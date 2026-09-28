"""C5 (PR #966): prune_empty_sessions must not delete a row rebound between
its read-only candidate pass and the write lock."""

from __future__ import annotations

import sqlite3

from plugins.context_engine.lcm.lifecycle_state import LifecycleStateStore


class _RacingConn:
    def __init__(self, conn, on_begin):
        self._conn, self._on_begin = conn, on_begin

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("BEGIN IMMEDIATE") and self._on_begin:
            hook, self._on_begin = self._on_begin, None
            hook()
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_rebound_row_survives_prune(tmp_path):
    db = tmp_path / "lcm.db"
    store = LifecycleStateStore(db)
    try:
        store.bind_session("s-old", conversation_id="conv-1")
        store.bind_session("s-gone", conversation_id="conv-2")

        def rebind():
            other = sqlite3.connect(db)
            other.execute(
                "UPDATE lcm_lifecycle_state SET current_session_id = 's-new' "
                "WHERE conversation_id = 'conv-1'"
            )
            other.commit()
            other.close()

        store._conn = _RacingConn(store._conn, rebind)
        assert store.prune_empty_sessions() == 1
        assert store.get_by_conversation("conv-1") is not None
        assert store.get_by_conversation("conv-2") is None
    finally:
        store._conn = getattr(store._conn, "_conn", store._conn)
        store.close()
