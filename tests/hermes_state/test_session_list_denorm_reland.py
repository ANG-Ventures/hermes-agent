import ast
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest

import hermes_state
from hermes_state import SessionDB, _session_list_denorm_enabled
from hermes_cli.config import DEFAULT_CONFIG, OPTIONAL_ENV_VARS


def _write_dashboard_flag(enabled: bool) -> None:
    config_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    config_path.write_text(
        "dashboard:\n"
        f"  session_list_denorm: {str(enabled).lower()}\n",
        encoding="utf-8",
    )


def _make_db(tmp_path: Path) -> SessionDB:
    return SessionDB(db_path=tmp_path / "state.db")


def _set_started_at(db: SessionDB, session_id: str, started_at: float) -> None:
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (started_at, session_id),
        )
    )
    db.recompute_effective_last_active(session_id)


def _seed_session(
    db: SessionDB,
    session_id: str,
    *,
    source: str = "cli",
    started_at: float,
    message_ts: float,
    archived: bool = False,
) -> None:
    db.create_session(session_id, source=source, model="test-model")
    _set_started_at(db, session_id, started_at)
    db.append_message(
        session_id,
        role="user",
        content=f"hello from {session_id}",
        timestamp=message_ts,
    )
    if archived:
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET archived = 1 WHERE id = ?",
                (session_id,),
            )
        )


def _seed_compression_chain(db: SessionDB) -> None:
    _seed_session(db, "compressed-root", started_at=80.0, message_ts=90.0)
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = 'compression' WHERE id = ?",
            (95.0, "compressed-root"),
        )
    )
    db.create_session(
        "compressed-tip",
        source="cli",
        model="test-model",
        parent_session_id="compressed-root",
    )
    _set_started_at(db, "compressed-tip", 96.0)
    db.append_message(
        "compressed-tip",
        role="user",
        content="hello from compressed tip",
        timestamp=500.0,
    )
    db.recompute_effective_last_active("compressed-root")


def _seed_listing_fixture(db: SessionDB) -> None:
    _seed_session(db, "old-but-active", started_at=100.0, message_ts=300.0)
    _seed_session(db, "newer-start", started_at=200.0, message_ts=210.0)
    _seed_session(db, "discord-row", source="discord", started_at=150.0, message_ts=250.0)
    _seed_session(db, "archived-row", started_at=50.0, message_ts=400.0, archived=True)
    _seed_compression_chain(db)


def _normalized(rows):
    """JSON round-trip like an RPC boundary; catches byte-shape drift in values."""
    return json.loads(json.dumps(rows, sort_keys=True, default=str))


def test_dashboard_session_list_denorm_default_is_false_config_only():
    assert DEFAULT_CONFIG["dashboard"]["session_list_denorm"] is False
    assert isinstance(DEFAULT_CONFIG["dashboard"]["session_list_denorm"], bool)
    assert not any("SESSION_LIST_DENORM" in name for name in OPTIONAL_ENV_VARS)


def test_session_list_denorm_flag_is_live_config_only(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_LIST_DENORM", "1")

    _write_dashboard_flag(False)
    assert _session_list_denorm_enabled() is False

    _write_dashboard_flag(True)
    assert _session_list_denorm_enabled() is True


def test_flag_off_keeps_cte_oracle_output_identical_for_existing_filters(tmp_path):
    _write_dashboard_flag(False)
    db = _make_db(tmp_path)
    try:
        _seed_listing_fixture(db)
        cases = [
            {"order_by_last_active": True},
            {"order_by_last_active": True, "include_archived": True},
            {"order_by_last_active": True, "source": "discord"},
            {"order_by_last_active": True, "id_query": "old-but"},
            {"order_by_last_active": True, "id_query": "compressed-tip"},
        ]
        for kwargs in cases:
            got = db.list_sessions_rich(limit=20, **kwargs)
            oracle = db.list_sessions_rich(limit=20, _force_cte_oracle=True, **kwargs)
            assert _normalized(got) == _normalized(oracle), kwargs
    finally:
        db.close()


def test_flag_on_denorm_path_matches_cte_oracle_byte_for_byte(tmp_path):
    _write_dashboard_flag(True)
    db = _make_db(tmp_path)
    try:
        _seed_listing_fixture(db)
        cases = [
            {"order_by_last_active": True},
            {"order_by_last_active": True, "include_archived": True},
            {"order_by_last_active": True, "source": "discord"},
            {"order_by_last_active": True, "id_query": "archived-row", "include_archived": True},
            {"order_by_last_active": True, "id_query": "compressed-tip"},
        ]
        for kwargs in cases:
            got = db.list_sessions_rich(limit=20, **kwargs)
            oracle = db.list_sessions_rich(limit=20, _force_cte_oracle=True, **kwargs)
            assert _normalized(got) == _normalized(oracle), kwargs
    finally:
        db.close()


def test_backfill_version_is_strictly_newer_than_every_shipped_marker():
    """The cutover-repair fires only when marker != VERSION, so VERSION MUST be
    greater than every marker value ever stamped onto a live DB — otherwise a
    build no-ops its own repair on a DB already carrying that marker.

    "4" is a POISONED value: an uncommitted VERSION="4" build was run against
    the live state.db during development and burned marker "4" onto production
    (git history only ever committed 1->2->3). A shipped VERSION of "4" would
    therefore skip the cutover repair on Ace's real DB. VERSION must be > 4.
    """
    version = hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_VERSION
    assert version.isdigit(), f"backfill version must be an integer string, got {version!r}"
    assert int(version) >= 5, (
        f"BACKFILL_VERSION={version!r} is not strictly newer than the poisoned "
        "marker '4' burned onto the live DB — the open-path repair would be "
        "skipped on a DB that already carries this marker. Bump the version."
    )


@pytest.mark.parametrize("stale_marker", ["1", "2", "3", "4", None])
def test_stale_backfill_marker_re_repairs_on_open(tmp_path, stale_marker):
    """Any marker OLDER than the current VERSION (or absent) must re-repair the
    stored recency on open — proven against every marker ever shipped, incl. the
    poisoned "4". This is the invariant the brittle v3-only test missed: it
    hardcoded "3" and so would have gone green while a VERSION=="4" build
    silently skipped the repair on the "4"-stamped production DB.
    """
    _write_dashboard_flag(True)
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        _seed_session(db, "stale-row", started_at=100.0, message_ts=500.0)
        _seed_session(db, "fresh-row", started_at=200.0, message_ts=300.0)

        def _force_stale_marker(conn):
            conn.execute(
                "UPDATE sessions SET effective_last_active = ? WHERE id = ?",
                (1.0, "stale-row"),
            )
            if stale_marker is None:
                conn.execute(
                    "DELETE FROM state_meta WHERE key = ?",
                    (hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_META_KEY,),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)",
                    (hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_META_KEY, stale_marker),
                )

        db._execute_write(_force_stale_marker)
    finally:
        db.close()

    # Precondition: the marker we forced is genuinely older than VERSION, so the
    # repair is EXPECTED to fire. Guards against a future VERSION regressing to
    # <= a value in this list (which would make the case a no-op false-pass).
    if stale_marker is not None:
        assert int(stale_marker) < int(
            hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_VERSION
        ), "test precondition broken: forced marker is not older than VERSION"

    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened._conn is not None
        stored = reopened._conn.execute(
            "SELECT effective_last_active FROM sessions WHERE id = ?",
            ("stale-row",),
        ).fetchone()[0]
        assert stored == 500.0
        marker = reopened._conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_META_KEY,),
        ).fetchone()[0]
        assert marker == hermes_state._EFFECTIVE_LAST_ACTIVE_BACKFILL_VERSION
        got = reopened.list_sessions_rich(order_by_last_active=True, limit=10)
        oracle = reopened.list_sessions_rich(
            order_by_last_active=True,
            limit=10,
            _force_cte_oracle=True,
        )
        assert _normalized(got) == _normalized(oracle)
    finally:
        reopened.close()


class TestGatewayFlushMaintainsEffectiveLastActive:
    def _make_agent(self, db: SessionDB, session_id: str):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent

            return AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id=session_id,
                platform="desktop",
                skip_context_files=True,
                skip_memory=True,
            )

    def test_new_agent_flush_session_orders_like_cte_oracle_and_has_denorm_value(self, tmp_path):
        _write_dashboard_flag(True)
        db = _make_db(tmp_path)
        try:
            _seed_session(db, "newer-than-live", started_at=10.0, message_ts=1200.0)
            _seed_session(db, "older-than-live", started_at=20.0, message_ts=800.0)

            agent = self._make_agent(db, "live-gateway-new-session")
            turn_messages = [
                {"role": "user", "content": "live user", "timestamp": 1000.0},
                {"role": "assistant", "content": "live assistant", "timestamp": 1000.5},
            ]
            agent._flush_messages_to_session_db(turn_messages, conversation_history=[])

            stored = db._conn.execute(
                "SELECT effective_last_active FROM sessions WHERE id = ?",
                ("live-gateway-new-session",),
            ).fetchone()[0]
            assert stored is not None

            got = db.list_sessions_rich(order_by_last_active=True, limit=10)
            oracle = db.list_sessions_rich(
                order_by_last_active=True,
                limit=10,
                _force_cte_oracle=True,
            )
            assert _normalized(got) == _normalized(oracle)
            ids = [row["id"] for row in got]
            assert ids[:3] == [
                "newer-than-live",
                "live-gateway-new-session",
                "older-than-live",
            ]
        finally:
            db.close()

    def test_realistic_copy_message_churn_order_hash_matches_cte_oracle(self, tmp_path):
        """Forty real agent flushes repair stale recency on a copied snapshot.

        The source file models a production DB whose backfill marker is current
        but whose live-session denorm values were overstated by an older writer.
        Each active conversation receives realistic cumulative-history flushes;
        the final visible and include-archived ordering bytes must hash exactly
        like the independent recursive-CTE oracle.
        """
        _write_dashboard_flag(True)
        snapshot_dir = tmp_path / "snapshot"
        snapshot_dir.mkdir()
        snapshot = SessionDB(db_path=snapshot_dir / "state.db")
        active_ids = [f"churn-live-{i:02d}" for i in range(6)]
        archived_ids = [f"churn-archived-{i:02d}" for i in range(2)]
        all_ids = active_ids + archived_ids
        base_ts = 1_800_000_000.0
        try:
            for session_index, session_id in enumerate(all_ids):
                started_at = base_ts + session_index * 3_600.0
                snapshot.create_session(
                    session_id,
                    source=("desktop", "cli", "discord")[session_index % 3],
                    model="test/model",
                )
                _set_started_at(snapshot, session_id, started_at)
                for historical_turn in range(4):
                    turn_ts = started_at + 60.0 + historical_turn * 90.0
                    snapshot.append_message(
                        session_id,
                        role="user",
                        content=(
                            f"historical request {historical_turn} for {session_id}\n"
                            "with enough detail to resemble a real conversation turn"
                        ),
                        timestamp=turn_ts,
                    )
                    snapshot.append_message(
                        session_id,
                        role="assistant",
                        content=f"historical response {historical_turn} for {session_id}",
                        reasoning="brief test reasoning",
                        timestamp=turn_ts + 0.5,
                    )

            for session_id in archived_ids:
                snapshot.set_session_archived(session_id, True)

            # Preserve a current migration marker while modelling denorm drift
            # left by a missed maintenance call. Reopening the copy must not let
            # migration repair the test fixture before the flush path sees it.
            for session_index, session_id in enumerate(active_ids):
                snapshot._execute_write(
                    lambda conn, sid=session_id, i=session_index: conn.execute(
                        "UPDATE sessions SET effective_last_active = ? WHERE id = ?",
                        (base_ts + 10_000_000.0 - i, sid),
                    )
                )
        finally:
            snapshot.close()

        copied_path = tmp_path / "realistic-copy-state.db"
        shutil.copy2(snapshot_dir / "state.db", copied_path)
        db = SessionDB(db_path=copied_path)
        try:
            assert db._conn is not None
            stale_rows = [
                session_id
                for session_id in active_ids
                if db._conn.execute(
                    "SELECT effective_last_active FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()[0]
                != db.expected_effective_last_active(session_id)
            ]
            assert stale_rows == active_ids, "snapshot-open unexpectedly repaired the churn fixture"

            agents = {}
            for session_id in active_ids:
                agent = self._make_agent(db, session_id)
                # The copied rows already exist, as they do after an agent's
                # first persistence setup. Keep the flush focused on message
                # maintenance rather than create_session's separate recompute.
                agent._session_db_created = True
                agents[session_id] = agent
            transcripts = {
                session_id: db.get_messages_as_conversation(session_id)
                for session_id in active_ids
            }
            turns_by_session = {session_id: 0 for session_id in active_ids}
            churn_base = base_ts + 2_000_000.0

            for turn_index in range(40):
                session_id = active_ids[turn_index % len(active_ids)]
                turn_ts = churn_base + turn_index * 75.0
                turn_messages = [
                    {
                        "role": "user",
                        "content": (
                            f"turn {turn_index}: inspect subsystem {turn_index % 7}\n"
                            "then report the verified result"
                        ),
                        "timestamp": turn_ts,
                    }
                ]
                if turn_index % 4 == 0:
                    call_id = f"call-{turn_index:02d}"
                    turn_messages.extend(
                        [
                            {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": json.dumps(
                                                {"path": f"src/module_{turn_index % 5}.py"}
                                            ),
                                        },
                                    }
                                ],
                                "timestamp": turn_ts + 0.2,
                            },
                            {
                                "role": "tool",
                                "tool_name": "read_file",
                                "tool_call_id": call_id,
                                "content": f"verified fixture output for turn {turn_index}",
                                "timestamp": turn_ts + 0.4,
                            },
                            {
                                "role": "assistant",
                                "content": f"verified result for turn {turn_index}",
                                "timestamp": turn_ts + 0.6,
                            },
                        ]
                    )
                else:
                    turn_messages.append(
                        {
                            "role": "assistant",
                            "content": f"verified result for turn {turn_index}",
                            "reasoning": "checked the requested subsystem",
                            "timestamp": turn_ts + 0.5,
                        }
                    )

                durable_prefix = list(transcripts[session_id])
                live_messages = durable_prefix + turn_messages
                agents[session_id]._flush_messages_to_session_db(
                    live_messages,
                    conversation_history=durable_prefix,
                )
                transcripts[session_id] = live_messages
                turns_by_session[session_id] += 1

            assert sum(turns_by_session.values()) == 40
            assert min(turns_by_session.values()) >= 6
            assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 164

            for label, include_archived in (
                ("default", False),
                ("include_archived", True),
            ):
                actual_order = [
                    row["id"]
                    for row in db.list_sessions_rich(
                        order_by_last_active=True,
                        limit=100,
                        include_archived=include_archived,
                    )
                ]
                oracle_order = [
                    row["id"]
                    for row in db.list_sessions_rich(
                        order_by_last_active=True,
                        limit=100,
                        _force_cte_oracle=True,
                        include_archived=include_archived,
                    )
                ]
                actual_bytes = json.dumps(
                    actual_order, separators=(",", ":")
                ).encode("utf-8")
                oracle_bytes = json.dumps(
                    oracle_order, separators=(",", ":")
                ).encode("utf-8")
                actual_hash = hashlib.sha256(actual_bytes).hexdigest()
                oracle_hash = hashlib.sha256(oracle_bytes).hexdigest()

                assert actual_hash == oracle_hash, {
                    "mode": label,
                    "actual_hash": actual_hash,
                    "oracle_hash": oracle_hash,
                    "actual_order": actual_order,
                    "oracle_order": oracle_order,
                }
                assert actual_order[0] == "churn-live-03"
                if label == "default":
                    assert set(actual_order) == set(active_ids)
                else:
                    assert set(actual_order) == set(all_ids)
        finally:
            db.close()

    @pytest.mark.parametrize("per_row_maintenance", [False, True])
    def test_batch_flush_recomputes_compression_root_once(
        self, tmp_path, monkeypatch, per_row_maintenance
    ):
        db = _make_db(tmp_path)
        try:
            _seed_compression_chain(db)
            agent = self._make_agent(db, "compressed-tip")
            if not per_row_maintenance:
                db._execute_write(
                    lambda conn: conn.execute(
                        "UPDATE sessions SET effective_last_active = -1 WHERE id = ?",
                        ("compressed-root",),
                    )
                )
                # Isolate the batch-level contract in one case; the other keeps
                # append_message's per-row maintenance enabled to prove composition.
                monkeypatch.setattr(
                    db,
                    "_bump_effective_last_active_for_message",
                    lambda *_args, **_kwargs: None,
                )
            recompute_calls = []
            recompute = db.recompute_effective_last_active

            def _track_recompute(session_id):
                recompute_calls.append(session_id)
                recompute(session_id)

            monkeypatch.setattr(db, "recompute_effective_last_active", _track_recompute)

            agent._flush_messages_to_session_db(
                [
                    {"role": "user", "content": "batch user", "timestamp": 700.0},
                    {"role": "assistant", "content": "batch reply", "timestamp": 800.0},
                ],
                conversation_history=[],
            )

            assert recompute_calls == ["compressed-tip"]
            assert db._conn is not None
            stored_root = db._conn.execute(
                "SELECT effective_last_active FROM sessions WHERE id = ?",
                ("compressed-root",),
            ).fetchone()[0]
            assert stored_root == 800.0
        finally:
            db.close()

    def test_batch_flush_skips_recompute_when_all_rows_are_suppressed(
        self, tmp_path, monkeypatch
    ):
        db = _make_db(tmp_path)
        try:
            agent = self._make_agent(db, "suppressed-flush")
            setattr(agent, "_persist_superseded", True)
            recompute_calls = []
            monkeypatch.setattr(
                db,
                "recompute_effective_last_active",
                lambda session_id: recompute_calls.append(session_id),
            )

            agent._flush_messages_to_session_db(
                [{"role": "assistant", "content": "late zombie reply"}],
                conversation_history=[],
            )

            assert recompute_calls == []
            assert db.get_messages("suppressed-flush") == []
        finally:
            db.close()

    def test_batch_recompute_failure_does_not_mask_successful_flush(
        self, tmp_path, monkeypatch
    ):
        db = _make_db(tmp_path)
        try:
            agent = self._make_agent(db, "recompute-failure")

            def _raise_recompute(_session_id):
                raise RuntimeError("recompute failed")

            monkeypatch.setattr(
                db,
                "recompute_effective_last_active",
                _raise_recompute,
            )
            messages = [{"role": "user", "content": "durable user turn"}]

            agent._flush_messages_to_session_db(messages, conversation_history=[])

            assert len(db.get_messages("recompute-failure")) == 1
            assert agent._last_flushed_db_idx == 1
            assert agent._flushed_db_message_ids == set()
        finally:
            db.close()

def _function_sources_by_qualname(source_path: Path) -> Dict[str, str]:
    source = source_path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    found: Dict[str, str] = {}

    def visit_body(body, prefix: str = "") -> None:
        for item in body:
            if isinstance(item, ast.ClassDef):
                visit_body(item.body, f"{prefix}{item.name}.")
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[f"{prefix}{item.name}"] = "\n".join(
                    lines[item.lineno - 1 : item.end_lineno]
                )

    visit_body(tree.body)
    return found


def _sessiondb_method_sources() -> Dict[str, str]:
    source_path = Path(hermes_state.__file__)
    prefix = "SessionDB."
    return {
        name[len(prefix):]: src
        for name, src in _function_sources_by_qualname(source_path).items()
        if name.startswith(prefix) and "." not in name[len(prefix):]
    }


def test_every_sessiondb_message_insert_path_is_effective_last_active_adjacent():
    all_functions = _function_sources_by_qualname(Path(hermes_state.__file__))
    insert_functions = {
        name: src
        for name, src in all_functions.items()
        if "INSERT INTO messages (" in src
    }
    assert insert_functions == {
        "_db_opens_cleanly": insert_functions.get("_db_opens_cleanly"),
        "SessionDB.append_message": insert_functions.get("SessionDB.append_message"),
        "SessionDB._insert_message_rows": insert_functions.get("SessionDB._insert_message_rows"),
    }

    methods = _sessiondb_method_sources()
    inserting_methods = {
        name: src
        for name, src in methods.items()
        if "INSERT INTO messages (" in src
    }
    assert inserting_methods, "source contract must see production message INSERTs"
    offenders = [
        name
        for name, src in inserting_methods.items()
        if "_bump_effective_last_active_for_message" not in src
    ]
    assert offenders == []

    recompute_required = [
        "replace_messages",
        "archive_and_compact",
        "update_session_meta",
    ]
    missing_recompute = [
        name
        for name in recompute_required
        if "_recompute_effective_last_active_for_session" not in methods[name]
    ]
    assert missing_recompute == []


def test_update_session_meta_visibility_flip_matches_cte_oracle(tmp_path):
    """Greptile P1 regression: rewriting model_config via update_session_meta can
    flip a row's session.list visibility (it carries the _delegate_from marker).
    The denorm path keys visibility on the stored effective_last_active column, so
    if update_session_meta doesn't recompute it, a row made delegate-only keeps a
    stale non-NULL value and stays visible (or a cleared row stays hidden) — the
    flag-on denorm output then diverges from the CTE oracle. Both directions must
    stay byte-identical to the oracle after the mutation.
    """
    _write_dashboard_flag(True)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        _seed_session(db, "row-A", started_at=100.0, message_ts=1000.0)
        _seed_session(db, "row-B", started_at=200.0, message_ts=2000.0)

        def _assert_matches_oracle(context):
            got = db.list_sessions_rich(order_by_last_active=True, limit=10)
            oracle = db.list_sessions_rich(
                order_by_last_active=True, limit=10, _force_cte_oracle=True
            )
            assert _normalized(got) == _normalized(oracle), context

        # Baseline: both visible, both paths agree.
        _assert_matches_oracle("baseline")

        # Make row-B delegate-only AFTER write → must vanish from BOTH paths.
        db.update_session_meta("row-B", json.dumps({"_delegate_from": "parent-x"}))
        assert "row-B" not in [
            r["id"] for r in db.list_sessions_rich(order_by_last_active=True, limit=10)
        ]
        _assert_matches_oracle("after making row-B delegate-only")

        # Clear the marker → row-B must reappear in BOTH paths.
        db.update_session_meta("row-B", json.dumps({}))
        assert "row-B" in [
            r["id"] for r in db.list_sessions_rich(order_by_last_active=True, limit=10)
        ]
        _assert_matches_oracle("after clearing row-B delegate marker")
    finally:
        db.close()


def test_update_session_meta_recomputes_previous_root(tmp_path):
    """Greptile P4 regression: a model_config marker flip can MOVE a row between
    compression roots (a continuation child branching away from its root). The P1
    fix recomputed the row's NEW root but not its PREVIOUS root, so the old root
    kept an effective_last_active that still folded in the departed child's fresh
    message and sorted ahead of the CTE path. update_session_meta must capture the
    previous root BEFORE the write and recompute it after (like the other
    linkage-changing paths). Mutation-proven: drop the previous-root recompute → RED.
    """
    _write_dashboard_flag(True)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # root (compression) -> child continuation carrying the fresh message.
        _seed_session(db, "root", started_at=1000.0, message_ts=1000.0)
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = 'compression' WHERE id = ?",
                (1001.0, "root"),
            )
        )
        db.create_session("child", source="cli", model="test-model", parent_session_id="root")
        db.append_message("child", role="user", content="c", timestamp=5000.0)
        db.recompute_effective_last_active("root")
        # rival timestamp sits between root's own recency (1000) and the child's (5000).
        _seed_session(db, "rival", started_at=1500.0, message_ts=3000.0)

        # Precondition: root's stored recency currently folds in the child (5000).
        assert (
            db._conn.execute(
                "SELECT effective_last_active FROM sessions WHERE id = ?", ("root",)
            ).fetchone()[0]
            == 5000.0
        )

        # Branch the child away from root → root's recency must drop back to 1000.
        db.update_session_meta("child", json.dumps({"_branched_from": "root"}))
        assert (
            db._conn.execute(
                "SELECT effective_last_active FROM sessions WHERE id = ?", ("root",)
            ).fetchone()[0]
            == 1000.0
        ), "previous root kept stale recency including the departed child's message"

        got = db.list_sessions_rich(order_by_last_active=True, limit=10)
        oracle = db.list_sessions_rich(
            order_by_last_active=True, limit=10, _force_cte_oracle=True
        )
        assert _normalized(got) == _normalized(oracle)
    finally:
        db.close()


def test_id_query_deep_chain_matches_cte_oracle(tmp_path):
    """Greptile P2 regression: the denorm id_query recursion previously capped at
    depth < 100 while the legacy CTE search is unbounded, so a compression chain
    whose matching tip sits beyond depth 100 was found by the oracle but missed by
    the flag-on denorm path — the same session.list search returning different rows
    once the flag is enabled. Compression edges are tree-structured (one parent per
    child), so the recursion terminates without the magic cap. A >100-deep chain
    searched by its deep tip id must resolve identically in both paths.
    """
    _write_dashboard_flag(True)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        depth = 130
        prev = None
        for i in range(depth):
            sid = f"c{i:03d}"
            db.create_session(sid, source="cli", model="test-model", parent_session_id=prev)
            db.append_message(sid, role="user", content=f"turn {i}", timestamp=1000.0 + i)
            if prev is not None:
                db._execute_write(
                    lambda conn, p=prev: conn.execute(
                        "UPDATE sessions SET ended_at = ?, end_reason = 'compression' WHERE id = ?",
                        (999.0, p),
                    )
                )
            prev = sid

        deep_tip = f"c{depth - 1:03d}"  # depth 129, well beyond the old cap
        got = db.list_sessions_rich(order_by_last_active=True, limit=10, id_query=deep_tip)
        oracle = db.list_sessions_rich(
            order_by_last_active=True, limit=10, id_query=deep_tip, _force_cte_oracle=True
        )
        assert _normalized(got) == _normalized(oracle)
        # And the deep-tip search actually resolves to a row (not an empty result
        # from a truncated recursion). Before the fix the denorm path returned []
        # for a tip beyond depth 100 while the oracle found it.
        assert len(got) == 1, got
    finally:
        db.close()


def test_deep_chain_stored_recency_matches_unbounded_oracle(tmp_path):
    """Greptile P3 regression: the id_query search recursion was unbounded (P2 fix)
    but the CTEs that compute the STORED effective_last_active (root-resolve,
    _expected_effective_last_active, and the backfill recompute-all) still capped at
    depth < 100. So a >100-deep compression chain whose freshest message is past hop
    100 had its stored recency truncated to the 100-hop max — the denorm path then
    ordered/paginated on a stale value that diverges from the unbounded CTE oracle.
    A recompute (backfill-on-open, visibility flip, archive_and_compact) would even
    REGRESS an already-correct value back to the truncated one. All recency CTEs must
    be unbounded to match the oracle. Mutation-proven: restore any cap → this REDs.
    """
    _write_dashboard_flag(True)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        depth = 130
        prev = None
        for i in range(depth):
            sid = f"c{i:03d}"
            if prev is not None:
                db._execute_write(
                    lambda conn, p=prev: conn.execute(
                        "UPDATE sessions SET ended_at = ?, end_reason = 'compression' WHERE id = ?",
                        (999.0, p),
                    )
                )
            db.create_session(sid, source="cli", model="test-model", parent_session_id=prev)
            # Deepest hop (past 100) carries the freshest timestamp.
            db.append_message(sid, role="user", content=f"t{i}", timestamp=1000.0 + i * 10.0)
            prev = sid

        deep_tip_ts = 1000.0 + (depth - 1) * 10.0  # 2290.0, at hop 129
        root = "c000"

        # The fresh per-row oracle must see the deep tip (unbounded).
        assert db.expected_effective_last_active(root) == deep_tip_ts

        # And a recompute (what backfill-on-open / visibility-flip / compaction do)
        # must NOT truncate the stored value back to the 100-hop max.
        db.recompute_effective_last_active(root)
        stored = db._conn.execute(
            "SELECT effective_last_active FROM sessions WHERE id = ?", (root,)
        ).fetchone()[0]
        assert stored == deep_tip_ts, f"stored recency truncated to {stored}, expected {deep_tip_ts}"

        # A rival session whose timestamp sits between hop-100 and the deep tip must
        # rank BELOW the deep chain's root in both paths.
        db.create_session("rival", source="cli", model="test-model")
        db.append_message("rival", role="user", content="rival", timestamp=1000.0 + 115 * 10.0)
        got = db.list_sessions_rich(order_by_last_active=True, limit=10)
        oracle = db.list_sessions_rich(
            order_by_last_active=True, limit=10, _force_cte_oracle=True
        )
        assert _normalized(got) == _normalized(oracle)
    finally:
        db.close()


@pytest.mark.skipif(
    not os.environ.get("SESSION_LIST_REAL_COPY_DB"),
    reason="set SESSION_LIST_REAL_COPY_DB=/path/to/state.db for real-copy churn check",
)
def test_real_copy_denorm_listing_matches_cte_oracle(tmp_path):
    _write_dashboard_flag(True)
    src = Path(os.environ["SESSION_LIST_REAL_COPY_DB"])
    dst = tmp_path / "real-copy-state.db"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        sidecar = src.with_name(src.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, dst.with_name(dst.name + suffix))

    db = SessionDB(db_path=dst)
    try:
        sample = db.list_sessions_rich(
            order_by_last_active=True,
            limit=1,
            _force_cte_oracle=True,
        )
        if not sample:
            pytest.skip("real-copy DB has no sessions to diff")
        sample_id = sample[0]["id"]
        sample_source = sample[0]["source"]
        cases = [
            {"order_by_last_active": True, "limit": 200},
            {"order_by_last_active": True, "limit": 200, "include_archived": True},
            {"order_by_last_active": True, "limit": 200, "id_query": sample_id},
            {"order_by_last_active": True, "limit": 200, "source": sample_source},
        ]
        for kwargs in cases:
            assert _normalized(db.list_sessions_rich(**kwargs)) == _normalized(
                db.list_sessions_rich(_force_cte_oracle=True, **kwargs)
            )
    finally:
        db.close()
