"""Acceptance coverage for session-list recency denormalization."""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

import hermes_state
from hermes_state import SessionDB


def _write_dashboard_flag(enabled: bool) -> None:
    config_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    config_path.write_text(
        "dashboard:\n"
        f"  session_list_denorm: {str(enabled).lower()}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _enable_denorm_path() -> None:
    _write_dashboard_flag(True)


def _make_db(tmp_path: Path) -> SessionDB:
    return SessionDB(db_path=tmp_path / "state.db")


def _connection(db: SessionDB) -> sqlite3.Connection:
    assert db._conn is not None
    return db._conn


def _set_session_times(
    db: SessionDB,
    session_id: str,
    *,
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
    end_reason: Optional[str] = None,
) -> None:
    sets: List[str] = []
    params: List[object] = []
    if started_at is not None:
        sets.append("started_at = ?")
        params.append(started_at)
    if ended_at is not None or end_reason is not None:
        sets.extend(["ended_at = ?", "end_reason = ?"])
        params.extend([ended_at, end_reason])
    if not sets:
        return

    db._execute_write(
        lambda conn: conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",
            (*params, session_id),
        )
    )
    db.recompute_effective_last_active(session_id)


def _create_session(
    db: SessionDB,
    session_id: str,
    *,
    started_at: float,
    source: str = "cli",
    parent_session_id: Optional[str] = None,
    model_config: Optional[Dict[str, object]] = None,
) -> None:
    db.create_session(
        session_id,
        source=source,
        model="test-model",
        parent_session_id=parent_session_id,
        model_config=model_config,
    )
    _set_session_times(db, session_id, started_at=started_at)


def _append(
    db: SessionDB,
    session_id: str,
    timestamp: float,
    *,
    content: Optional[str] = None,
) -> int:
    return db.append_message(
        session_id,
        role="user",
        content=content or f"message at {timestamp}",
        timestamp=timestamp,
    )


def _stored(db: SessionDB, session_id: str) -> Optional[float]:
    row = _connection(db).execute(
        "SELECT effective_last_active FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return None if row is None else row["effective_last_active"]


def _ordered_ids(db: SessionDB, **kwargs) -> List[str]:
    return [
        row["id"]
        for row in db.list_sessions_rich(
            order_by_last_active=True,
            **kwargs,
        )
    ]


def _rows_bytes(rows: List[Dict[str, object]]) -> bytes:
    return json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _assert_listing_matches_oracle(db: SessionDB, **kwargs) -> None:
    actual = db.list_sessions_rich(order_by_last_active=True, **kwargs)
    expected = db.list_sessions_rich(
        order_by_last_active=True,
        _force_cte_oracle=True,
        **kwargs,
    )
    assert _rows_bytes(actual) == _rows_bytes(expected)


def test_inner_limit_queries_search_covering_indexes_without_temp_sort(tmp_path):
    db = _make_db(tmp_path)
    try:
        cases = [
            (
                "default",
                "SELECT id FROM sessions "
                "WHERE effective_last_active IS NOT NULL AND archived = ? "
                "ORDER BY effective_last_active DESC, started_at DESC, id DESC LIMIT ?",
                (0, 400),
                "idx_sessions_effective_last_active",
            ),
            (
                "source",
                "SELECT id FROM sessions "
                "WHERE source = ? AND effective_last_active IS NOT NULL AND archived = ? "
                "ORDER BY effective_last_active DESC, started_at DESC, id DESC LIMIT ?",
                ("cli", 0, 400),
                "idx_sessions_source_effective_last_active",
            ),
            (
                "id-query",
                "SELECT id FROM sessions "
                "WHERE effective_last_active IS NOT NULL AND archived = ? "
                "AND id LIKE ? ESCAPE '\\' "
                "ORDER BY effective_last_active DESC, started_at DESC, id DESC LIMIT ?",
                (0, "%root%", 400),
                "idx_sessions_effective_last_active",
            ),
        ]

        for label, sql, params, index_name in cases:
            details = [
                row["detail"]
                for row in _connection(db).execute(
                    f"EXPLAIN QUERY PLAN {sql}",
                    params,
                )
            ]
            plan = "\n".join(details)
            assert any(
                detail.startswith("SEARCH sessions USING COVERING INDEX")
                and index_name in detail
                for detail in details
            ), (label, plan)
            assert "USE TEMP B-TREE" not in plan, (label, plan)
    finally:
        db.close()


def test_migration_backfill_is_idempotent_and_repair_marker_stays_current(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        _create_session(db, "root", started_at=10.0)
        _append(db, "root", 20.0)
        db.end_session("root", "compression")
        _create_session(db, "tip", started_at=30.0, parent_session_id="root")
        _append(db, "tip", 50.0)

        def _make_stale(conn):
            conn.execute("UPDATE sessions SET effective_last_active = NULL")
            conn.execute(
                "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                (hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_META_KEY, "4"),
            )

        db._execute_write(_make_stale)
    finally:
        db.close()

    first_open = SessionDB(db_path=db_path)
    try:
        first_values = {
            row["id"]: row["effective_last_active"]
            for row in _connection(first_open).execute(
                "SELECT id, effective_last_active FROM sessions ORDER BY id"
            )
        }
        assert first_values == {"root": 50.0, "tip": None}
        first_open.backfill_effective_last_active()
        after_explicit_rerun = {
            row["id"]: row["effective_last_active"]
            for row in _connection(first_open).execute(
                "SELECT id, effective_last_active FROM sessions ORDER BY id"
            )
        }
        assert after_explicit_rerun == first_values
    finally:
        first_open.close()

    second_open = SessionDB(db_path=db_path)
    try:
        second_values = {
            row["id"]: row["effective_last_active"]
            for row in _connection(second_open).execute(
                "SELECT id, effective_last_active FROM sessions ORDER BY id"
            )
        }
        marker_row = _connection(second_open).execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_META_KEY,),
        ).fetchone()
        assert marker_row is not None
        marker = marker_row["value"]
        assert second_values == first_values
        assert marker == hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_VERSION
    finally:
        second_open.close()


def test_schema_rollback_drops_indexes_before_column(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("DROP COLUMN requires SQLite >= 3.35")

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_source_effective_last_active")
        conn.execute("DROP INDEX IF EXISTS idx_sessions_effective_last_active")
        conn.execute("ALTER TABLE sessions DROP COLUMN effective_last_active")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(sessions)")}
        assert "effective_last_active" not in columns
        assert "idx_sessions_effective_last_active" not in indexes
        assert "idx_sessions_source_effective_last_active" not in indexes
    finally:
        conn.close()


def test_insert_is_monotonic_and_delete_only_message_falls_back_to_started_at(tmp_path):
    db = _make_db(tmp_path)
    try:
        _create_session(db, "root", started_at=10.0)
        _append(db, "root", 100.0, content="newer")
        _append(db, "root", 50.0, content="stale writer")
        assert _stored(db, "root") == 100.0

        db.clear_messages("root")

        assert _stored(db, "root") == 10.0
        assert _ordered_ids(db, limit=10) == ["root"]
        _assert_listing_matches_oracle(db, limit=10)
    finally:
        db.close()


def test_compression_split_archive_and_reopen_match_oracle(tmp_path):
    db = _make_db(tmp_path)
    try:
        _create_session(db, "root", started_at=10.0)
        _append(db, "root", 20.0)
        db.end_session("root", "compression")
        _create_session(db, "tip", started_at=30.0, parent_session_id="root")
        _append(db, "tip", 200.0)

        assert _stored(db, "root") == 200.0
        assert _stored(db, "tip") is None
        _assert_listing_matches_oracle(db, limit=10)

        before_archive = _stored(db, "root")
        db.set_session_archived("root", True)
        assert _stored(db, "root") == before_archive
        _assert_listing_matches_oracle(db, limit=10, include_archived=True)
        _assert_listing_matches_oracle(db, limit=10, archived_only=True)

        db.set_session_archived("root", False)
        db.reopen_session("root")
        assert _stored(db, "root") == 20.0
        assert _stored(db, "tip") is None
        _assert_listing_matches_oracle(db, limit=10)
    finally:
        db.close()


def test_downward_message_replacement_drops_chain_max(tmp_path):
    db = _make_db(tmp_path)
    try:
        _create_session(db, "root", started_at=10.0)
        _append(db, "root", 40.0)
        db.end_session("root", "compression")
        _create_session(db, "tip", started_at=20.0, parent_session_id="root")
        _append(db, "tip", 300.0)
        assert _stored(db, "root") == 300.0

        db.replace_messages(
            "tip",
            [{"role": "user", "content": "replacement", "timestamp": 30.0}],
        )

        assert _stored(db, "root") == 40.0
        assert _stored(db, "root") == db.expected_effective_last_active("root")
        _assert_listing_matches_oracle(db, limit=10)
    finally:
        db.close()


def test_tool_member_contributes_positive_chain_max_while_tool_roots_can_be_denied(tmp_path):
    db = _make_db(tmp_path)
    try:
        _create_session(db, "root", started_at=10.0)
        _append(db, "root", 20.0)
        db.end_session("root", "compression")
        _create_session(
            db,
            "tool-member",
            source="tool",
            started_at=30.0,
            parent_session_id="root",
        )
        _append(db, "tool-member", 500.0)
        _create_session(db, "tool-root", source="tool", started_at=600.0)
        _append(db, "tool-root", 700.0)

        assert _stored(db, "root") == 500.0
        assert db.expected_effective_last_active("root") == 500.0
        assert "tool-root" not in _ordered_ids(
            db,
            limit=10,
            exclude_sources=["tool"],
        )
        _assert_listing_matches_oracle(
            db,
            limit=10,
            exclude_sources=["tool"],
        )
    finally:
        db.close()


def test_tied_visible_roots_use_started_at_then_id_tiebreak(tmp_path):
    db = _make_db(tmp_path)
    try:
        _create_session(db, "tie-a", started_at=10.0)
        _append(db, "tie-a", 100.0)
        _create_session(db, "tie-b", started_at=20.0)
        _append(db, "tie-b", 100.0)
        _create_session(db, "tie-c", started_at=20.0)
        _append(db, "tie-c", 100.0)

        assert _ordered_ids(db, limit=10) == ["tie-c", "tie-b", "tie-a"]
        _assert_listing_matches_oracle(db, limit=10)
    finally:
        db.close()


@pytest.mark.parametrize(
    "delete_mode",
    [
        "delegate-cascade",
        "delete-session",
        "delete-sessions",
        "delete-empty-sessions",
        "prune-sessions",
    ],
)
def test_all_five_delete_orphan_sites_promote_surviving_continuation(
    tmp_path,
    delete_mode,
):
    db = _make_db(tmp_path)
    try:
        parent_id = "parent"
        if delete_mode == "delegate-cascade":
            _create_session(db, "grand", started_at=1.0)
            _create_session(
                db,
                parent_id,
                started_at=10.0,
                parent_session_id="grand",
                model_config={"_delegate_from": "grand"},
            )
        else:
            _create_session(db, parent_id, started_at=10.0)

        db.end_session(parent_id, "compression")
        _create_session(
            db,
            "child",
            started_at=20.0,
            parent_session_id=parent_id,
        )
        _append(db, "child", 50.0)
        assert _stored(db, "child") is None

        if delete_mode == "delegate-cascade":
            db.delete_session("grand")
        elif delete_mode == "delete-session":
            db.delete_session(parent_id)
        elif delete_mode == "delete-sessions":
            db.delete_sessions([parent_id])
        elif delete_mode == "delete-empty-sessions":
            db.delete_empty_sessions()
        elif delete_mode == "prune-sessions":
            _set_session_times(
                db,
                parent_id,
                started_at=1.0,
                ended_at=2.0,
                end_reason="compression",
            )
            db.prune_sessions(older_than_days=0)

        assert db.get_session(parent_id) is None
        child = db.get_session("child")
        assert child is not None
        assert child["parent_session_id"] is None
        assert _stored(db, "child") == 50.0
        assert "child" in _ordered_ids(db, limit=10)
        _assert_listing_matches_oracle(db, limit=10)
    finally:
        db.close()


def test_mid_chain_delete_recomputes_surviving_root_and_orphaned_tip(tmp_path):
    db = _make_db(tmp_path)
    try:
        _create_session(db, "root", started_at=10.0)
        _append(db, "root", 20.0)
        db.end_session("root", "compression")
        _create_session(db, "mid", started_at=30.0, parent_session_id="root")
        _append(db, "mid", 200.0)
        db.end_session("mid", "compression")
        _create_session(db, "tip", started_at=40.0, parent_session_id="mid")
        _append(db, "tip", 300.0)
        assert _stored(db, "root") == 300.0

        db.delete_session("mid")

        assert _stored(db, "root") == 20.0
        tip = db.get_session("tip")
        assert tip is not None
        assert tip["parent_session_id"] is None
        assert _stored(db, "tip") == 300.0
        _assert_listing_matches_oracle(db, limit=10)
    finally:
        db.close()


def test_concurrent_upsert_parent_merge_and_insert_keep_both_roots_correct(tmp_path):
    db_path = tmp_path / "state.db"
    setup = SessionDB(db_path=db_path)
    try:
        _create_session(setup, "parent", started_at=10.0)
        _append(setup, "parent", 20.0)
        setup.end_session("parent", "compression")
        _create_session(setup, "child", started_at=30.0)
        _append(setup, "child", 400.0)
    finally:
        setup.close()

    upserter = SessionDB(db_path=db_path)
    inserter = SessionDB(db_path=db_path)
    barrier = threading.Barrier(3)
    errors: List[BaseException] = []

    def _upsert() -> None:
        try:
            barrier.wait(timeout=5)
            upserter.create_session(
                "child",
                source="cli",
                parent_session_id="parent",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def _insert() -> None:
        try:
            barrier.wait(timeout=5)
            inserter.append_message(
                "child",
                role="user",
                content="concurrent",
                timestamp=500.0,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_upsert), threading.Thread(target=_insert)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        child = upserter.get_session("child")
        assert child is not None
        assert child["parent_session_id"] == "parent"
        assert _stored(upserter, "child") is None
        assert _stored(upserter, "parent") == 500.0
        assert _stored(upserter, "parent") == upserter.expected_effective_last_active(
            "parent"
        )
        _assert_listing_matches_oracle(upserter, limit=10)
    finally:
        upserter.close()
        inserter.close()


def test_begin_immediate_serializes_detach_with_concurrent_insert(tmp_path):
    db_path = tmp_path / "state.db"
    setup = SessionDB(db_path=db_path)
    try:
        _create_session(setup, "root", started_at=10.0)
        _append(setup, "root", 20.0)
        setup.end_session("root", "compression")
        _create_session(setup, "child", started_at=30.0, parent_session_id="root")
        _append(setup, "child", 400.0)
    finally:
        setup.close()

    detacher = SessionDB(db_path=db_path)
    inserter = SessionDB(db_path=db_path)
    detach_holds_write_lock = threading.Event()
    release_detach = threading.Event()
    insert_started = threading.Event()
    insert_done = threading.Event()
    errors: List[BaseException] = []
    original_resolve = detacher._resolve_effective_last_active_root
    paused = False

    def _paused_resolve(conn, session_id):
        nonlocal paused
        root_id = original_resolve(conn, session_id)
        if session_id == "child" and not paused:
            paused = True
            detach_holds_write_lock.set()
            assert release_detach.wait(timeout=5)
        return root_id

    detacher._resolve_effective_last_active_root = _paused_resolve

    def _detach() -> None:
        try:
            detacher.update_session_meta(
                "child",
                json.dumps({"_branched_from": "root"}),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def _insert() -> None:
        try:
            insert_started.set()
            inserter.append_message(
                "child",
                role="user",
                content="during detach",
                timestamp=500.0,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            insert_done.set()

    detach_thread = threading.Thread(target=_detach)
    insert_thread = threading.Thread(target=_insert)
    try:
        detach_thread.start()
        assert detach_holds_write_lock.wait(timeout=5)
        insert_thread.start()
        assert insert_started.wait(timeout=5)
        assert not insert_done.wait(timeout=0.05), (
            "concurrent writer unexpectedly committed while detach held BEGIN IMMEDIATE"
        )
        release_detach.set()
        detach_thread.join(timeout=10)
        insert_thread.join(timeout=10)

        assert not detach_thread.is_alive()
        assert not insert_thread.is_alive()
        assert errors == []
        assert _stored(detacher, "root") == 20.0
        assert _stored(detacher, "child") == 500.0
        assert _stored(detacher, "root") == detacher.expected_effective_last_active(
            "root"
        )
        assert _stored(detacher, "child") == detacher.expected_effective_last_active(
            "child"
        )
        _assert_listing_matches_oracle(detacher, limit=10)
    finally:
        release_detach.set()
        detacher.close()
        inserter.close()


def test_deferred_detach_upgrade_fails_after_concurrent_insert(tmp_path):
    db_path = tmp_path / "state.db"
    setup = SessionDB(db_path=db_path)
    try:
        _create_session(setup, "root", started_at=10.0)
        _append(setup, "root", 20.0)
        setup.end_session("root", "compression")
        _create_session(setup, "child", started_at=30.0, parent_session_id="root")
        _append(setup, "child", 400.0)
    finally:
        setup.close()

    deferred = sqlite3.connect(
        db_path,
        timeout=0.2,
        isolation_level=None,
    )
    deferred.row_factory = sqlite3.Row
    writer = SessionDB(db_path=db_path)
    try:
        assert deferred.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        deferred.execute("BEGIN")
        assert deferred.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?",
            ("child",),
        ).fetchone()[0] == "root"

        writer.append_message(
            "child",
            role="user",
            content="wins after deferred read",
            timestamp=500.0,
        )

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            deferred.execute(
                "UPDATE sessions SET parent_session_id = NULL WHERE id = ?",
                ("child",),
            )
        deferred.rollback()

        child = writer.get_session("child")
        assert child is not None
        assert child["parent_session_id"] == "root"
        assert _stored(writer, "root") == 500.0
        assert _stored(writer, "root") == writer.expected_effective_last_active("root")
    finally:
        try:
            deferred.rollback()
        except sqlite3.Error:
            pass
        deferred.close()
        writer.close()


def _literal_sql(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("?")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_sql(node.left)
        right = _literal_sql(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _parent_mutation_contract(source: str) -> Tuple[Set[str], Set[str]]:
    tree = ast.parse(source)
    # fork-parity: upstream's parity merge SPLIT the hermes_state monolith, so
    # the SessionDB body is now assembled from mixin classes across sibling
    # modules (hermes_state_portability.SessionPortabilityMixin et al). Scan the
    # SessionDB class when present, otherwise every mixin class in the module —
    # a hard `next(... name == "SessionDB")` raises StopIteration on the split-out
    # files and would silently under-count the mutation sites.
    carriers = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and (node.name == "SessionDB" or node.name.endswith("Mixin"))
    ]
    sites: Set[str] = set()
    maintained: Set[str] = set()
    maintenance_calls = {
        "_collect_orphan_effective_last_active_targets",
        "_recompute_effective_last_active",
        "_recompute_effective_last_active_for_session",
        "_recompute_effective_last_active_many",
    }

    for session_db in carriers:
        for function in session_db.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr in maintenance_calls:
                    maintained.add(function.name)
                if node.func.attr not in {"execute", "executemany", "executescript"}:
                    continue
                sql = _literal_sql(node.args[0])
                if sql is None:
                    continue
                for statement in sql.split(";"):
                    normalized = " ".join(statement.split())
                    set_clause = ""
                    if normalized.upper().startswith("UPDATE SESSIONS SET "):
                        set_clause = normalized.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
                    is_update = bool(
                        re.search(
                            r"(?:^|,)\s*parent_session_id\s*=",
                            set_clause,
                            flags=re.IGNORECASE,
                        )
                    )
                    is_upsert = bool(
                        re.search(
                            r"ON CONFLICT\s*\([^)]*\).*parent_session_id\s*=\s*COALESCE",
                            normalized,
                            flags=re.IGNORECASE,
                        )
                    )
                    if is_update or is_upsert:
                        sites.add(function.name)

    return sites, maintained


def test_all_six_session_parent_mutation_sites_are_maintenance_adjacent():
    # fork-parity: upstream's parity merge SPLIT the hermes_state monolith into
    # hermes_state{,_portability,_schema,_search,_common}.py. The 6th mutation
    # site (import_sessions' parent re-link) now lives in hermes_state_portability,
    # so scanning hermes_state.py alone finds only 5 and the contract reads as a
    # violation. Scan every module the SessionDB mixins are assembled from.
    import hermes_state_portability

    source = "\n".join(
        Path(mod.__file__).read_text(encoding="utf-8")
        for mod in (hermes_state, hermes_state_portability)
    )
    sites, maintained = _parent_mutation_contract(source)

    assert len(sites) == 6, sites
    assert sites - maintained == set()


def test_parent_mutation_contract_detects_a_new_unmaintained_site():
    source = """
class SessionDB:
    def existing(self, conn):
        conn.execute('UPDATE sessions SET parent_session_id = NULL WHERE id = ?', ('x',))
        self._recompute_effective_last_active_for_session(conn, 'x')

    def violating_seventh_site(self, conn):
        conn.execute('UPDATE sessions SET parent_session_id = ? WHERE id = ?', ('p', 'x'))
"""
    sites, maintained = _parent_mutation_contract(source)

    assert sites == {"existing", "violating_seventh_site"}
    assert sites - maintained == {"violating_seventh_site"}


def test_reconcile_audit_reports_injected_drift(tmp_path, caplog):
    db = _make_db(tmp_path)
    try:
        _create_session(db, "root", started_at=10.0)
        _append(db, "root", 20.0)
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET effective_last_active = ? WHERE id = ?",
                (999.0, "root"),
            )
        )

        caplog.set_level("WARNING", logger="hermes_state")
        drift = db.audit_effective_last_active(limit=10)

        assert drift == [{"id": "root", "stored": 999.0, "expected": 20.0}]
        assert "effective_last_active drift" in caplog.text
    finally:
        db.close()
