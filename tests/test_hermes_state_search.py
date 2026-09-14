"""Tests for hermes_state.py — SessionDB SQLite CRUD, FTS5 search, export.

Split from the original single test_hermes_state.py. That file held 537 tests and the
parallel runner treats a FILE as the unit of parallelism, so it could not be shared
across workers: it took 600s on CI and was SIGKILLed by the 300s per-file cap, failing
the job while reporting "0 failed". ~10 autouse fixtures run per test, so cost scales
with test count (measured locally: setup 27.6s vs call 5.6s — 86% of runtime is
per-test fixture overhead, not test logic). No test was changed, removed or weakened;
the four parts collect exactly the same ids as the original.
"""

import re
import sqlite3
import time
import json
import threading
from pathlib import Path
from unittest import mock

import pytest

import hermes_state
from agent.session_activity import ActivityProvenance
from hermes_state import SCHEMA_SQL, SCHEMA_VERSION, SessionDB


class _NoFtsCursor(sqlite3.Cursor):
    """Simulate a SQLite build without the fts5 module."""

    def execute(self, sql, parameters=()):
        probe = sql.strip()
        if "USING fts5" in probe:
            raise sqlite3.OperationalError("no such module: fts5")
        if probe in (
            "SELECT * FROM messages_fts LIMIT 0",
            "SELECT * FROM messages_fts_trigram LIMIT 0",
        ):
            raise sqlite3.OperationalError("no such table: " + probe.split()[-3])
        return super().execute(sql, parameters)

    def executescript(self, sql_script):
        if "USING fts5" in sql_script:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().executescript(sql_script)


class _NoFtsConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoFtsCursor)


class _NoFtsExistingTableCursor(_NoFtsCursor):
    """Simulate existing FTS virtual tables under a runtime without FTS5."""

    def execute(self, sql, parameters=()):
        probe = sql.strip()
        if probe in (
            "SELECT * FROM messages_fts LIMIT 0",
            "SELECT * FROM messages_fts_trigram LIMIT 0",
        ):
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)


class _NoFtsExistingTableConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoFtsExistingTableCursor)


class _NoTrigramCursor(sqlite3.Cursor):
    """Simulate a SQLite build with FTS5 but without the trigram tokenizer."""

    def executescript(self, sql_script):
        if "tokenize='trigram'" in sql_script:
            raise sqlite3.OperationalError("no such tokenizer: trigram")
        return super().executescript(sql_script)


class _NoTrigramConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoTrigramCursor)


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    session_db.close()


@pytest.fixture(autouse=True)
def _no_fts_rebuild_throttle(monkeypatch):
    """Zero the FTS-rebuild inter-chunk throttle for every test in this file.

    ``optimize_fts_storage`` sleeps ``max(_FTS_REBUILD_MIN_PAUSE,
    chunk_cost * _FTS_REBUILD_DUTY_FACTOR)`` between chunks so a LIVE
    gateway/CLI sharing the DB isn't starved of the write lock. Tests run
    against a private tmp-path DB with no concurrent process — the sleep
    protects nobody and was pure dead time (measured: 4.1s of a 4.6s
    migration test was time.sleep; ~20s across the file, whose total was
    ~52s). The duty-cycle POLICY (sleep >= 4x chunk cost) stays covered by
    the production constants themselves; no test asserts on wall-clock
    pacing.
    """
    monkeypatch.setattr(SessionDB, "_FTS_REBUILD_MIN_PAUSE", 0.0)
    monkeypatch.setattr(SessionDB, "_FTS_REBUILD_DUTY_FACTOR", 0.0)


# =========================================================================
# Connection lifecycle
# =========================================================================


class TestListSessionsRich:
    """Tests for enhanced session listing with preview and last_active."""

    def test_preview_from_first_user_message(self, db):
        db.create_session("s1", "cli")
        db.append_message("s1", "system", "You are a helpful assistant.")
        db.append_message("s1", "user", "Help me refactor the auth module please")
        db.append_message("s1", "assistant", "Sure, let me look at it.")
        sessions = db.list_sessions_rich()
        assert len(sessions) == 1
        assert "Help me refactor the auth module" in sessions[0]["preview"]





    def test_last_active_prefers_session_activity_heartbeat(self, db):
        """Mid-turn agent heartbeats must advance last_active without new messages (#72016)."""
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "hello")
        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=?",
                (1_700_000_000.0, "s1", "user"),
            )
            db._conn.commit()

        before = db.list_sessions_rich()[0]["last_active"]
        heartbeat = 1_700_000_500.0
        db.touch_session_activity(
            "s1",
            heartbeat,
            description="starting API call #1",
            provenance=ActivityProvenance.UNKNOWN,
        )
        after = db.list_sessions_rich()[0]["last_active"]
        assert after == heartbeat
        assert after > before

        row = db.get_session("s1")
        assert row["last_activity_at"] == heartbeat
        assert row["last_activity_description"] == "starting API call #1"
        assert row["last_activity_provenance"] == "unknown"

        activity = db.get_session_activity("s1")
        assert activity["last_activity_at"] == heartbeat
        assert activity["last_activity_description"] == "starting API call #1"
        assert "phase" not in activity

        # Never move last_activity_at backwards.
        db.touch_session_activity("s1", heartbeat - 100, description="ignored")
        assert db.get_session("s1")["last_activity_at"] == heartbeat
        assert db.get_session("s1")["last_activity_description"] == "starting API call #1"

    def test_clear_session_activity_labels_keeps_timestamp(self, db):
        """Turn-end label clear must wipe desc/provenance without moving ts."""
        db.create_session("s1", "cli")
        heartbeat = 1_700_000_500.0
        db.touch_session_activity(
            "s1",
            heartbeat,
            description="compressing context",
            provenance=ActivityProvenance.AGENT_COMPRESSION,
        )
        row = db.get_session("s1")
        assert row["last_activity_at"] == heartbeat
        assert row["last_activity_description"] == "compressing context"
        assert row["last_activity_provenance"] == "agent.compression"

        db.clear_session_activity_labels("s1")
        row = db.get_session("s1")
        assert row["last_activity_at"] == heartbeat
        assert row["last_activity_description"] == ""
        assert row["last_activity_provenance"] == "unknown"
        activity = db.get_session_activity("s1")
        assert activity["last_activity_at"] == heartbeat
        assert activity["last_activity_description"] == ""
        assert activity["last_activity_provenance"] == "unknown"

    def test_last_active_uses_newer_message_over_stale_heartbeat(self, db):
        """Rate-limited heartbeats can lag message writes; last_active must take max."""
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "hello")
        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=?",
                (1_700_000_800.0, "s1"),
            )
            db._conn.commit()
        db.touch_session_activity("s1", 1_700_000_500.0, description="api")  # older than message
        assert db.list_sessions_rich()[0]["last_active"] == 1_700_000_800.0

    def test_list_gateway_sessions_last_active_uses_activity_heartbeat(self, db):
        db.create_session(
            "gw-1",
            "telegram",
            session_key="agent:main:telegram:dm:c1",
            chat_id="c1",
            chat_type="dm",
        )
        db.append_message("gw-1", "user", "ping")
        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=?",
                (1_700_000_000.0, "gw-1"),
            )
            db._conn.commit()

        heartbeat = 1_700_000_900.0
        db.touch_session_activity(
            "gw-1",
            heartbeat,
            description="compressing context",
        )
        rows = db.list_gateway_sessions(active_only=True)
        assert len(rows) == 1
        assert rows[0]["last_active"] == heartbeat
        activity = db.get_session_activity("gw-1")
        assert activity["last_activity_description"] == "compressing context"

    def test_order_by_last_active_surfaces_recently_touched_older_session_first(self, db):
        t0 = 1709500000.0
        db.create_session("old", "cli")
        db.create_session("new", "cli")

        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "old"))
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 10, "new"))

        db.append_message("old", "user", "old first")
        db.append_message("new", "user", "new first")
        db.append_message("old", "assistant", "old touched later")

        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 1, "old", "user", "old first"),
            )
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 11, "new", "user", "new first"),
            )
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND role=? AND content=?",
                (t0 + 20, "old", "assistant", "old touched later"),
            )
            db._conn.commit()

        assert [s["id"] for s in db.list_sessions_rich(limit=5)] == ["new", "old"]
        assert [
            s["id"] for s in db.list_sessions_rich(limit=5, order_by_last_active=True)
        ] == ["old", "new"]







    def test_rich_list_session_key_filter_precedes_limit(self, db):
        lane_key = "agent:main:telegram:dm:lane"
        db.create_session(
            "lane_oldest", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.create_session(
            "lane_newest", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        for i in range(60):
            db.create_session(
                f"foreign_{i}", "telegram",
                session_key=f"agent:main:telegram:dm:foreign-{i}",
                user_id=f"foreign-user-{i}", chat_id=f"foreign-{i}",
            )
        db.create_session(
            "legacy_null_key", "telegram", user_id="lane-user", chat_id="lane"
        )

        sessions = db.list_sessions_rich(
            source="telegram", session_key=lane_key, limit=2
        )

        assert [session["id"] for session in sessions] == [
            "lane_newest", "lane_oldest",
        ]

    def test_rich_list_session_key_scopes_search_and_projects_compression(self, db):
        lane_key = "agent:main:telegram:dm:lane"
        db.create_session(
            "lane_root", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.set_session_title("lane_root", "Needle root")
        db.end_session("lane_root", "compression")
        db.create_session(
            "lane_tip", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane", parent_session_id="lane_root",
        )
        db.set_session_title("lane_tip", "Needle continuation")
        db.append_message("lane_tip", "user", "latest lane activity")
        db.create_session(
            "foreign_match", "telegram",
            session_key="agent:main:telegram:dm:foreign",
            user_id="foreign-user", chat_id="foreign",
        )
        db.set_session_title("foreign_match", "Needle foreign")

        sessions = db.list_sessions_rich(
            source="telegram",
            session_key=lane_key,
            search_query="needle",
            order_by_last_active=True,
            limit=1,
        )

        assert [session["id"] for session in sessions] == ["lane_tip"]
        assert sessions[0]["_lineage_root_id"] == "lane_root"

    @pytest.mark.parametrize(
        "end_reason",
        [
            "session_reset",
            "session_switch",
            "idle",
            "daily",
            "suspended",
            "resume_pending_expired",
        ],
    )
    def test_rich_list_keeps_legacy_reset_children_visible(self, db, end_reason):
        from hermes_state_common import _ephemeral_child_sql

        lane_key = "agent:main:telegram:dm:lane"
        parent_id = f"parent_{end_reason}"
        child_id = f"child_{end_reason}"
        db.create_session(parent_id, "telegram", session_key=lane_key)
        db.end_session(parent_id, end_reason)
        # No _reset_from marker: this is the on-disk shape written before the
        # marker existed. The unchanged routing key proves a reset boundary.
        db.create_session(
            child_id,
            "telegram",
            session_key=lane_key,
            parent_session_id=parent_id,
        )

        listed = [row["id"] for row in db.list_sessions_rich(source="telegram")]
        assert {parent_id, child_id}.issubset(listed)
        assert db.session_count(source="telegram", exclude_children=True) == 2
        assert db.session_count_by_source(exclude_children=True)["telegram"] == 2
        ephemeral = db._conn.execute(
            f"SELECT s.id FROM sessions s WHERE {_ephemeral_child_sql('s')}"
        ).fetchall()
        assert child_id not in {row["id"] for row in ephemeral}

    def test_reset_parent_does_not_surface_unrelated_child(self, db):
        db.create_session(
            "reset_parent",
            "telegram",
            session_key="agent:main:telegram:dm:lane",
        )
        db.end_session("reset_parent", "session_reset")
        db.create_session(
            "unrelated_child",
            "tool",
            session_key="delegate:other",
            parent_session_id="reset_parent",
        )

        listed = [row["id"] for row in db.list_sessions_rich()]
        assert "unrelated_child" not in listed
        assert db.session_count(exclude_children=True) == 1

    def test_resume_walker_does_not_cross_reset_boundary(self, db):
        """resolve_resume_session_id must not redirect a reset parent's resume
        into the post-reset conversation — that would restore the exact
        context the user reset away. Covers both the durable marker and the
        legacy markerless shape."""
        lane_key = "agent:main:telegram:dm:lane"
        # Marker shape (rows written by current gateway code).
        db.create_session("walk_parent", "telegram", session_key=lane_key)
        db.append_message("walk_parent", "user", "before reset")
        db.end_session("walk_parent", "session_reset")
        db.create_session(
            "walk_child",
            "telegram",
            session_key=lane_key,
            parent_session_id="walk_parent",
            model_config={"_reset_from": "walk_parent"},
        )
        db.append_message("walk_child", "user", "after reset")
        assert db.resolve_resume_session_id("walk_parent") == "walk_parent"

        # Legacy markerless shape (pre-marker on-disk rows).
        lane2 = "agent:main:telegram:dm:lane2"
        db.create_session("legacy_parent", "telegram", session_key=lane2)
        db.append_message("legacy_parent", "user", "before reset")
        db.end_session("legacy_parent", "session_reset")
        db.create_session(
            "legacy_child",
            "telegram",
            session_key=lane2,
            parent_session_id="legacy_parent",
        )
        db.append_message("legacy_child", "user", "after reset")
        assert db.resolve_resume_session_id("legacy_parent") == "legacy_parent"

    # Compression-tip following (the walker's original purpose) is pinned by
    # tests/hermes_state/test_resolve_resume_session_id.py
    # ::test_follows_compression_tip_when_parent_retains_messages.

    def test_session_key_predicate_can_use_session_key_index(self, db):
        plan = db._conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT s.id FROM sessions s WHERE s.session_key = ? "
            "ORDER BY s.started_at DESC LIMIT 10",
            ("agent:main:telegram:dm:lane",),
        ).fetchall()

        detail = " ".join(row[-1] for row in plan)
        assert "idx_sessions_session_key" in detail, detail

    def test_delegate_subagent_marker_hides_orphaned_row(self, db):
        """``_delegate_from`` keeps delegate rows out of pickers after orphaning."""
        db.create_session("parent", "cli")
        db.create_session(
            "delegate",
            "cli",
            parent_session_id="parent",
            model_config={"_delegate_from": "parent"},
        )
        db.append_message("delegate", "user", "scan the repo")

        assert "delegate" not in [s["id"] for s in db.list_sessions_rich()]

        db._conn.execute(
            "UPDATE sessions SET parent_session_id = NULL WHERE id = ?", ("delegate",)
        )
        db._conn.commit()

        assert "delegate" not in [s["id"] for s in db.list_sessions_rich()]


    def test_delete_session_expected_targets_fail_closed_on_new_delegate(self, db):
        db.create_session("parent", "cli")
        db.create_session(
            "delegate",
            "cli",
            parent_session_id="parent",
            model_config={"_delegate_from": "parent"},
        )
        db.create_session(
            "branch",
            "cli",
            parent_session_id="parent",
            model_config={"_branched_from": "parent"},
        )

        expected_ids = db.get_session_delete_targets("parent")
        assert expected_ids == ["parent", "delegate"]

        db.create_session(
            "late-delegate",
            "cli",
            parent_session_id="parent",
            model_config={"_delegate_from": "parent"},
        )

        assert (
            db.delete_session("parent", expected_delete_ids=expected_ids) is False
        )
        assert db.get_session("parent") is not None
        assert db.get_session("delegate") is not None
        assert db.get_session("late-delegate") is not None
        assert db.get_session("branch") is not None




    def test_subagent_session_still_hidden(self, db):
        """Sub-agent children (parent NOT ended with 'branched') remain hidden."""
        db.create_session("root", "cli")
        db.create_session("delegate", "cli", parent_session_id="root")

        sessions = db.list_sessions_rich()
        ids = [s["id"] for s in sessions]
        assert "delegate" not in ids, "Delegate sub-agent should not appear in default list"
        assert "root" in ids

    def test_branch_session_visible_after_parent_reopen_and_reend(self, db):
        """Branch sessions stay visible after the parent is reopened and re-ended.

        Regression for issue #20856: /branch (aka /fork) sessions vanished from
        /resume and /sessions once the parent was reopened (e.g. resumed) and
        re-ended with a different end_reason — tui_shutdown overwriting
        'branched' — which broke the legacy end_reason heuristic. The stable
        _branched_from marker in model_config keeps them visible.
        """
        import json as _json

        db.create_session("parent", "cli")
        db.end_session("parent", "branched")
        db.create_session(
            "branch",
            "cli",
            model_config={"_branched_from": "parent"},
            parent_session_id="parent",
        )
        db.append_message("branch", "user", "Exploring the alternative approach")

        # Marker is persisted at creation time.
        branch_row = db.get_session("branch")
        cfg = _json.loads(branch_row["model_config"]) if branch_row["model_config"] else {}
        assert cfg.get("_branched_from") == "parent"

        # Visible immediately after branching.
        assert "branch" in [s["id"] for s in db.list_sessions_rich()]

        # Parent reopened + re-ended with a different reason (the bug trigger).
        db.reopen_session("parent")
        db.end_session("parent", "tui_shutdown")

        # Branch must STILL be visible — the marker survives the parent's
        # end_reason churn, unlike the legacy 'branched' heuristic.
        ids = [s["id"] for s in db.list_sessions_rich()]
        assert "branch" in ids, "Branch should stay visible after parent re-end"

    def test_branch_session_visible_in_list(self, db):
        """Branch sessions (parent ended with 'branched') must appear in list_sessions_rich."""
        db.create_session("parent", "cli")
        db.end_session("parent", "branched")
        db.create_session("branch", "cli", parent_session_id="parent")
        db.append_message("branch", "user", "Exploring the alternative approach")

        sessions = db.list_sessions_rich()
        ids = [s["id"] for s in sessions]
        assert "branch" in ids, "Branch session should be visible in default list"

    def test_compression_child_still_hidden(self, db):
        """Compression continuation sessions remain hidden (parent ended with 'compression')."""
        import time as _time
        t0 = _time.time()
        db.create_session("root", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
            (t0 + 1800, "root"),
        )
        db._conn.commit()
        db.create_session("continuation", "cli", parent_session_id="root")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?", (t0 + 1801, "continuation")
        )
        db._conn.commit()

        sessions = db.list_sessions_rich(project_compression_tips=False)
        ids = [s["id"] for s in sessions]
        assert "continuation" not in ids, "Compression continuation should stay hidden"

    def test_delete_parent_cascades_delegate_children(self, db):
        db.create_session("parent", "cli")
        db.create_session(
            "delegate",
            "cli",
            parent_session_id="parent",
            model_config={"_delegate_from": "parent"},
        )
        db.create_session(
            "branch",
            "cli",
            parent_session_id="parent",
            model_config={"_branched_from": "parent"},
        )

        assert db.delete_session("parent") is True
        assert db.get_session("delegate") is None
        assert db.get_session("branch") is not None

    def test_last_active_fallback_to_started_at(self, db):
        db.create_session("s1", "cli")
        sessions = db.list_sessions_rich()
        # No messages, so last_active falls back to started_at
        assert sessions[0]["last_active"] == sessions[0]["started_at"]

    def test_last_active_from_latest_message(self, db):
        import time
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "Hello")
        time.sleep(0.01)
        db.append_message("s1", "assistant", "Hi there!")
        sessions = db.list_sessions_rich()
        # last_active should be close to now (the assistant message)
        assert sessions[0]["last_active"] > sessions[0]["started_at"]

    def test_order_by_last_active_uses_compression_tip_activity(self, db):
        """A compression root whose tip was touched recently must rank above
        a newer uncompressed session, even when that tip activity lives in a
        different row and the outer LIMIT could otherwise cut it.

        This is the case that forced SQL-level chain walking: a naive "cap
        the SQL fetch at limit*K" optimization would drop the old root off
        the SQL page before post-projection could promote it.
        """
        t0 = 1709500000.0
        db.create_session("root1", "cli")
        # Parity note (2026-08-08): upstream added CompressionSessionClosedError —
        # appending to a session already closed by compression now raises ("adopt
        # its live continuation before appending"). That guard is correct and this
        # test is not about it, so write the root's message BEFORE stamping it
        # closed. The ordering assertion under test is unaffected: it depends on
        # the row's timestamps, not on when the INSERT happened.
        db.append_message("root1", "user", "old ask")
        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root1"))
            db._conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                (t0 + 100, "compression", "root1"),
            )

        # Continuation tip created after root ended; last activity much later.
        db.create_session("tip1", "cli", parent_session_id="root1")
        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 101, "tip1"))
        db.append_message("tip1", "user", "latest message")

        # Bunch of newer, uncompressed sessions — fresher start_at but older
        # last activity than the tip. Explicitly pin message timestamps so
        # they don't pick up wall-clock from append_message.
        for i in range(5):
            sid = f"newer{i}"
            db.create_session(sid, "cli")
            with db._lock:
                db._conn.execute(
                    "UPDATE sessions SET started_at=? WHERE id=?",
                    (t0 + 500 + i, sid),
                )
            db.append_message(sid, "user", f"msg {i}")
            with db._lock:
                db._conn.execute(
                    "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                    (t0 + 500 + i, sid, f"msg {i}"),
                )

        # Tip activity timestamp is the latest thing in the DB.
        with db._lock:
            db._conn.execute(
                "UPDATE messages SET timestamp=? WHERE session_id=? AND content=?",
                (t0 + 10_000, "tip1", "latest message"),
            )
            db._conn.commit()

        # limit=1 is the stress test: the old root must win the single slot.
        top = db.list_sessions_rich(limit=1, order_by_last_active=True)
        assert len(top) == 1
        # Projection surfaces the tip's id in the root's slot.
        assert top[0]["id"] == "tip1"
        assert top[0]["_lineage_root_id"] == "root1"

    def test_preview_empty_when_no_user_messages(self, db):
        db.create_session("s1", "cli")
        db.append_message("s1", "system", "System prompt")
        sessions = db.list_sessions_rich()
        assert sessions[0]["preview"] == ""

    def test_preview_newlines_collapsed(self, db):
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "Line one\nLine two\nLine three")
        sessions = db.list_sessions_rich()
        assert "\n" not in sessions[0]["preview"]
        assert "Line one Line two" in sessions[0]["preview"]

    def test_preview_truncated_at_60(self, db):
        db.create_session("s1", "cli")
        long_msg = "A" * 100
        db.append_message("s1", "user", long_msg)
        sessions = db.list_sessions_rich()
        assert len(sessions[0]["preview"]) == 63  # 60 chars + "..."
        assert sessions[0]["preview"].endswith("...")

    def test_rich_list_cwd_prefix_filter(self, db):
        db.create_session("s1", "cli", cwd="/repo")
        db.create_session("s2", "cli", cwd="/repo/subdir")
        db.create_session("s3", "cli", cwd="/repo-wt-feature")

        sessions = db.list_sessions_rich(cwd_prefix="/repo")
        assert [session["id"] for session in sessions] == ["s2", "s1"]

    def test_rich_list_includes_title(self, db):
        db.create_session("s1", "cli")
        db.set_session_title("s1", "refactoring auth")
        sessions = db.list_sessions_rich()
        assert sessions[0]["title"] == "refactoring auth"

    def test_rich_list_source_filter(self, db):
        db.create_session("s1", "cli")
        db.create_session("s2", "telegram")
        sessions = db.list_sessions_rich(source="cli")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"

    def test_v16_migration_tags_linked_delegate_rows(self, tmp_path):
        """Pre-marker linked subagent rows get tagged, then cascade with parent."""
        import json

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        db.create_session("parent", "cli")
        db.create_session("delegate", "cli", parent_session_id="parent")
        db._conn.execute("UPDATE schema_version SET version = 15")
        db._conn.commit()
        db.close()

        db = SessionDB(db_path=db_path)
        row = db.get_session("delegate")
        assert json.loads(row["model_config"])["_delegate_from"] == "parent"
        assert db.delete_session("parent") is True
        assert db.get_session("delegate") is None
        db.close()

    def test_v16_migration_tags_orphaned_delegate_rows(self, tmp_path):
        import json

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        db.create_session("orphan", "cli")
        db.append_message("orphan", "user", "Echo progress")
        db.append_message("orphan", "tool", "step 1", tool_name="terminal")
        db._conn.execute("UPDATE schema_version SET version = 15")
        db._conn.commit()
        db.close()

        db = SessionDB(db_path=db_path)
        assert "orphan" not in [s["id"] for s in db.list_sessions_rich()]
        row = db.get_session("orphan")
        assert json.loads(row["model_config"])["_delegate_from"] == "__orphaned__"
        db.close()



class TestDeleteAndExport:
    def test_delete_session(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")

        assert db.delete_session("s1") is True
        assert db.get_session("s1") is None
        assert db.message_count(session_id="s1") == 0





    def test_resolve_session_id_ambiguous_prefix_returns_none(self, db):
        db.create_session(session_id="20260315_092437_c9a6aa", source="cli")
        db.create_session(session_id="20260315_092437_c9a6bb", source="cli")
        assert db.resolve_session_id("20260315_092437_c9a6") is None




    def test_import_sessions_rejects_oversized_payloads_atomically(self, db):
        oversized = "x" * (SessionDB._IMPORT_MAX_SESSION_BYTES + 1)
        result = db.import_sessions(
            [{"id": "oversized", "messages": [{"role": "user", "content": oversized}]}]
        )

        assert result["ok"] is False
        assert result["errors"][0]["error"] == "session exceeds the import size limit"
        assert db.get_session("oversized") is None

        result = db.import_sessions(
            [
                {
                    "id": "too-many-messages",
                    "messages": [
                        {"role": "user", "content": "x"}
                    ]
                    * (SessionDB._IMPORT_MAX_MESSAGES_PER_SESSION + 1),
                }
            ]
        )

        assert result["ok"] is False
        assert result["errors"][0]["error"] == "messages exceeds the per-session import limit"
        assert db.get_session("too-many-messages") is None

    def test_compression_lineage_terminates_for_preexisting_cycle(self, db):
        db.create_session("a", "cli")
        db.end_session("a", "compression")
        db.create_session("b", "cli", parent_session_id="a")
        db.end_session("b", "compression")
        db._conn.execute("UPDATE sessions SET parent_session_id = ? WHERE id = ?", ("b", "a"))
        db._conn.commit()

        lineage = db.get_compression_lineage("a")
        assert set(lineage) == {"a", "b"}
        assert len(lineage) == 2
        assert set(db.export_session_lineage("a")["lineage_session_ids"]) == {"a", "b"}

    def test_delete_nonexistent(self, db):
        assert db.delete_session("nope") is False

    def test_delete_session_cascades_per_model_usage(self, db):
        db.create_session(session_id="usage", source="cli", model="gpt-5")
        db.update_token_counts(
            "usage", input_tokens=10, model="gpt-5",
            billing_provider="openai", api_call_count=1,
        )
        assert db.delete_session("usage") is True
        count = db._conn.execute(
            "SELECT COUNT(*) FROM session_model_usage WHERE session_id = 'usage'"
        ).fetchone()[0]
        assert count == 0

    def test_export_all(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")
        db.append_message("s1", role="user", content="A")

        exports = db.export_all()
        assert len(exports) == 2

    def test_export_all_with_source(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")

        exports = db.export_all(source="cli")
        assert len(exports) == 1
        assert exports[0]["source"] == "cli"

    def test_export_nonexistent(self, db):
        assert db.export_session("nope") is None

    def test_export_session(self, db):
        db.create_session(session_id="s1", source="cli", model="test")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi")

        export = db.export_session("s1")
        assert isinstance(export, dict)
        assert export["source"] == "cli"
        assert len(export["messages"]) == 2

    def test_import_exported_session_round_trips(self, db, tmp_path):
        db.create_session(
            session_id="s1",
            source="cli",
            model="test-model",
            model_config={"temperature": 0.2},
            user_id="user-1",
            cwd="/workspace",
        )
        db.set_session_title("s1", "Imported session")
        db.update_session_cwd(
            "s1",
            "/workspace/project",
            git_branch="feature/import",
            git_repo_root="/workspace/project",
        )
        db.append_message("s1", role="user", content="Hello", timestamp=10)
        db.append_message(
            "s1",
            role="assistant",
            content="Hi",
            timestamp=11,
            tool_calls=[{"id": "call-1", "function": {"name": "noop"}}],
            reasoning_details=[{"type": "summary", "text": "short"}],
        )
        db.end_session("s1", "complete")

        exported = db.export_session("s1")
        exported["handoff_state"] = "active"
        exported["handoff_platform"] = "telegram"
        exported["handoff_error"] = "stale runtime state"
        exported["rewind_count"] = 3
        target = SessionDB(db_path=tmp_path / "target_state.db")
        try:
            result = target.import_sessions([exported])
            assert result["ok"] is True
            assert result["imported"] == 1
            assert result["skipped"] == 0

            imported = target.get_session("s1")
            assert imported["title"] == "Imported session"
            assert imported["source"] == "cli"
            assert imported["model"] == "test-model"
            assert imported["cwd"] == "/workspace/project"
            assert imported["git_branch"] == "feature/import"
            assert imported["git_repo_root"] == "/workspace/project"
            assert imported["message_count"] == 2
            assert imported["tool_call_count"] == 1
            assert imported["handoff_state"] is None
            assert imported["handoff_platform"] is None
            assert imported["handoff_error"] is None
            assert imported["rewind_count"] == 0

            messages = target.get_messages("s1")
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[0]["content"] == "Hello"
            assert messages[1]["tool_calls"][0]["id"] == "call-1"

            duplicate = target.import_sessions([exported])
            assert duplicate["imported"] == 0
            assert duplicate["skipped"] == 1
            assert duplicate["skipped_ids"] == ["s1"]
        finally:
            target.close()

    def test_import_sessions_detaches_cycle_and_lineage_still_terminates(self, db):
        result = db.import_sessions(
            [
                {
                    "id": "a",
                    "source": "cli",
                    "parent_session_id": "b",
                    "end_reason": "compression",
                    "messages": [],
                },
                {
                    "id": "b",
                    "source": "cli",
                    "parent_session_id": "a",
                    "end_reason": "compression",
                    "messages": [],
                },
            ]
        )

        assert result["ok"] is True
        assert result["detached"] == 1
        assert db.get_session("a")["parent_session_id"] is None
        assert db.get_session("b")["parent_session_id"] == "a"
        assert db.get_compression_lineage("a") == ["a", "b"]

    def test_import_sessions_detaches_self_parent(self, db):
        result = db.import_sessions(
            [
                {
                    "id": "self",
                    "source": "cli",
                    "parent_session_id": "self",
                    "end_reason": "compression",
                    "messages": [],
                }
            ]
        )

        assert result["ok"] is True
        assert result["detached"] == 1
        assert db.get_session("self")["parent_session_id"] is None

    def test_import_sessions_recomputes_imported_compression_root_recency(self, db):
        result = db.import_sessions(
            [
                {
                    "id": "root",
                    "source": "cli",
                    "end_reason": "compression",
                    "messages": [
                        {"role": "user", "content": "root", "timestamp": 100.0}
                    ],
                },
                {
                    "id": "child",
                    "source": "cli",
                    "parent_session_id": "root",
                    "messages": [
                        {"role": "user", "content": "child", "timestamp": 500.0}
                    ],
                },
            ]
        )

        assert result["ok"] is True
        assert db.get_session("root")["effective_last_active"] == 500.0
        assert db.expected_effective_last_active("root") == 500.0

    def test_import_sessions_recomputes_preexisting_compression_root_recency(self, db):
        db.create_session("root", source="cli")
        db.append_message("root", role="user", content="root", timestamp=100.0)
        db.end_session("root", "compression")

        result = db.import_sessions(
            [
                {
                    "id": "child",
                    "source": "cli",
                    "parent_session_id": "root",
                    "messages": [
                        {"role": "user", "content": "child", "timestamp": 500.0}
                    ],
                }
            ]
        )

        assert result["ok"] is True
        assert db.get_session("root")["effective_last_active"] == 500.0
        assert db.expected_effective_last_active("root") == 500.0

    def test_import_sessions_rejects_invalid_batch_atomically(self, db):
        result = db.import_sessions(
            [
                {"id": "valid", "source": "cli", "messages": []},
                {"source": "cli", "messages": []},
            ]
        )

        assert result["ok"] is False
        assert result["imported"] == 0
        assert result["errors"] == [
            {"index": 1, "error": "session id is required"}
        ]
        assert db.get_session("valid") is None

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            (
                {"id": "bad-json", "model_config": "{not-json", "messages": []},
                "model_config must be valid JSON",
            ),
            (
                {"id": "bad-text", "user_id": {"not": "text"}, "messages": []},
                "user_id must be a string",
            ),
            (
                {"id": "missing-role", "messages": [{"content": "x"}]},
                "messages[0].role must be a non-empty string",
            ),
            (
                {"id": "null-role", "messages": [{"role": None, "content": "x"}]},
                "messages[0].role must be a non-empty string",
            ),
        ],
    )
    def test_import_sessions_rejects_invalid_metadata(self, db, payload, error):
        result = db.import_sessions([payload])

        assert result["ok"] is False
        assert result["errors"] == [{"index": 0, "session_id": payload["id"], "error": error}]
        assert db.get_session(payload["id"]) is None

    def test_import_sessions_restores_valid_parents_and_detaches_missing(self, db):
        result = db.import_sessions(
            [
                {
                    "id": "child",
                    "source": "cli",
                    "parent_session_id": "parent",
                    "messages": [],
                },
                {"id": "parent", "source": "cli", "messages": []},
                {
                    "id": "orphan",
                    "source": "cli",
                    "parent_session_id": "missing",
                    "messages": [],
                },
            ]
        )

        assert result["ok"] is True
        assert result["imported"] == 3
        assert result["detached"] == 1
        assert db.get_session("child")["parent_session_id"] == "parent"
        assert db.get_session("orphan")["parent_session_id"] is None

    def test_resolve_session_id_escapes_like_wildcards(self, db):
        db.create_session(session_id="20260315_092437_c9a6ff", source="cli")
        db.create_session(session_id="20260315X092437_c9a6ff", source="cli")
        assert db.resolve_session_id("20260315_092437") == "20260315_092437_c9a6ff"

    def test_resolve_session_id_exact(self, db):
        db.create_session(session_id="20260315_092437_c9a6ff", source="cli")
        assert db.resolve_session_id("20260315_092437_c9a6ff") == "20260315_092437_c9a6ff"

    def test_resolve_session_id_unique_prefix(self, db):
        db.create_session(session_id="20260315_092437_c9a6ff", source="cli")
        assert db.resolve_session_id("20260315_092437_c9a6") == "20260315_092437_c9a6ff"


# =========================================================================
# Prune
# =========================================================================

class TestFTSExternalContentMigration:
    """v23 migration: inline-mode FTS tables (v11-v22) are rebuilt as
    external-content tables, and role='tool' rows are excluded from the
    trigram index while remaining searchable via the standard index."""

    @staticmethod
    def _build_v22_db(db_path):
        """Build a v22-shaped DB by hand: inline FTS tables + concat triggers."""
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        # Replace the current (v23) FTS objects with the v22 inline shape.
        conn.executescript("""
            DROP TABLE IF EXISTS messages_fts;
            DROP TABLE IF EXISTS messages_fts_trigram;
            DROP VIEW IF EXISTS messages_fts_trigram_src;

            CREATE VIRTUAL TABLE messages_fts USING fts5(content);
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (
                    new.id,
                    COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
                );
            END;

            CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram');
            CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts_trigram(rowid, content) VALUES (
                    new.id,
                    COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
                );
            END;
        """)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (22)")
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', ?)",
            (time.time(),),
        )
        rows = [
            ("user", "find the 大别山项目 deployment notes", None, None),
            ("assistant", "关于大别山项目的总结在这里", None,
             '{"function":{"name":"send_message","arguments":"{}"}}'),
            ("tool", "TOOLBLOB " + "x" * 5000 + " 项目文件内容测试", "read_file", None),
        ]
        for role, content, tool_name, tool_calls in rows:
            conn.execute(
                "INSERT INTO messages (session_id, timestamp, role, content, tool_name, tool_calls) "
                "VALUES ('s1', ?, ?, ?, ?, ?)",
                (time.time(), role, content, tool_name, tool_calls),
            )
        conn.commit()
        # Sanity: v22 inline tables have their own content shadow tables.
        shadow = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'messages_fts_content'"
        ).fetchall()
        assert shadow, "sanity: v22 inline FTS must have a content shadow table"
        conn.close()

    def test_v22_open_leaves_legacy_untouched_and_advertises(self, tmp_path):
        """Opening a legacy v22 DB must NOT auto-migrate the FTS layout, but
        the main schema_version DOES advance (decoupled) so future non-FTS
        migrations aren't blocked. The inline index keeps working and the
        opt-in flag is set."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)

        db = SessionDB(db_path=db_path)
        try:
            # DECOUPLED: the main schema_version advances to current even though
            # the FTS layout stays legacy — future migrations must not be gated
            # behind the FTS opt-in.
            version = db._conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            assert version == SCHEMA_VERSION, "main schema version must advance"
            # But the FTS storage layout is NOT stamped current — it's legacy.
            assert db.get_meta("fts_storage_version") is None
            assert db.fts_optimize_available() is True
            assert db.get_meta("fts_optimize_available") == "1"

            # Legacy inline shape is intact (content shadow table still there).
            assert db._conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'messages_fts_content'"
            ).fetchone() is not None

            # Search still works on the legacy index (no deferred rebuild).
            assert db.fts_rebuild_status() is None
            assert len(db.search_messages("deployment")) == 1
            assert len(db.search_messages("send_message")) == 1  # #16751 held

            # A new write is indexed live by the legacy triggers.
            db.append_message("s1", role="user", content="AFTEROPEN token")
            assert len(db.search_messages("AFTEROPEN")) == 1
        finally:
            db.close()






    def _simulate_pre_fix_demote_crash_window(self, db):
        """Replay the pre-fix demote crash window: trash + empty v23 schema,
        no rebuild markers (executescript committed mid-demote before markers).

        Mirrors what happened when ``_ensure_fts_schema`` ran inside
        ``_execute_write`` and the process died before the marker writes.
        """
        from hermes_state import FTS_SQL, FTS_TRIGRAM_SQL

        conn = db._conn
        db._drop_fts_triggers(conn)
        conn.execute("DROP VIEW IF EXISTS messages_fts_trigram_src")
        had = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('messages_fts', 'messages_fts_trigram') "
            "AND sql LIKE 'CREATE VIRTUAL TABLE%' LIMIT 1"
        ).fetchone())
        assert had, "sanity: expected legacy/virtual FTS tables to demote"
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "DELETE FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('messages_fts', 'messages_fts_trigram') "
            "AND sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
        conn.execute("PRAGMA writable_schema=RESET")
        shadows = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND (name LIKE 'messages_fts_%' ESCAPE '\\' "
                "OR name LIKE 'messages_fts_trigram_%' ESCAPE '\\')"
            ).fetchall()
        ]
        for sh in shadows:
            conn.execute(f"ALTER TABLE {sh} RENAME TO fts_v22_trash_{sh}")
        # executescript commits — empty v23 tables without markers.
        conn.executescript(FTS_SQL)
        try:
            conn.executescript(FTS_TRIGRAM_SQL)
        except sqlite3.OperationalError:
            pass
        # Intentionally leave fts_rebuild_* unset (the crash window).

    def test_optimize_resume_after_demote_crash_window_restores_search(
        self, tmp_path
    ):
        """Pre-fix: demote crash left trash + empty v23 index, no markers.
        Re-run tore down trash and stamped optimized with docsize=0 — permanent
        search loss for historical rows. Re-run must backfill and restore."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)

        db = SessionDB(db_path=db_path)
        try:
            assert len(db.search_messages("deployment")) == 1
            self._simulate_pre_fix_demote_crash_window(db)
            # Crash window shape: no markers, trash present, empty index.
            assert db.get_meta("fts_rebuild_high_water") is None
            assert db.get_meta("fts_rebuild_progress") is None
            assert db._has_fts_trash(db._conn) is True
            assert db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize"
            ).fetchone()[0] == 0
            assert len(db.search_messages("deployment")) == 0

            # Still offered (trash and/or empty-index heal).
            assert db.fts_optimize_available() is True

            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert db.fts_rebuild_status() is None
            assert db.fts_optimize_available() is False
            assert db.get_meta("fts_storage_version") == str(
                hermes_state.FTS_STORAGE_VERSION
            )
            assert db._conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_v22_trash%'"
            ).fetchall() == []
            # Historical rows searchable again; index fully populated.
            assert len(db.search_messages("deployment")) == 1
            assert len(db.search_messages("TOOLBLOB")) == 1
            n_msg = db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            n_fts = db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize"
            ).fetchone()[0]
            assert n_fts == n_msg
            db._conn.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
        finally:
            db.close()

    def test_optimize_heals_premature_stamp_with_empty_index(self, tmp_path):
        """Pre-fix settle could stamp fts_storage_version after tearing down
        trash with an empty index and no markers. Re-run must clear the stamp,
        backfill, and re-earn the layout version."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)

        db = SessionDB(db_path=db_path)
        try:
            self._simulate_pre_fix_demote_crash_window(db)
            # Simulate the bad resume: trash already gone, empty index stamped.
            trash = [
                r[0] for r in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE 'fts\\_v22\\_trash\\_%' ESCAPE '\\'"
                ).fetchall()
            ]
            for tbl in trash:
                db._conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            db._conn.execute(
                "INSERT INTO state_meta (key, value) VALUES "
                "('fts_storage_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(hermes_state.FTS_STORAGE_VERSION),),
            )
            db._conn.commit()

            assert db.get_meta("fts_rebuild_high_water") is None
            assert db._has_fts_trash(db._conn) is False
            assert db._fts_external_index_empty_with_messages(db._conn) is True
            # Must still be offered despite the premature stamp.
            assert db.fts_optimize_available() is True
            assert len(db.search_messages("deployment")) == 0

            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert len(db.search_messages("deployment")) == 1
            assert db.get_meta("fts_storage_version") == str(
                hermes_state.FTS_STORAGE_VERSION
            )
            assert db.fts_optimize_available() is False
        finally:
            db.close()

    def test_optimize_heals_high_water_without_progress(self, tmp_path):
        """high_water without progress used to make fts_rebuild_step return
        False immediately (treated as finished by another process), then
        settle stamped success while the marker remained. Re-seed progress
        and complete the empty-index backfill."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            self._simulate_pre_fix_demote_crash_window(db)
            hw = db._conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages"
            ).fetchone()[0]
            # Orphan shape: high_water alone on an empty external index.
            db.set_meta("fts_rebuild_high_water", str(hw))
            db._conn.execute(
                "DELETE FROM state_meta WHERE key = ?", ("fts_rebuild_progress",)
            )
            db._conn.commit()
            assert db.get_meta("fts_rebuild_progress") is None
            assert db.fts_optimize_available() is True
            # Empty index: base FTS MATCH finds nothing (gap LIKE may still
            # supplement when high_water is set — that is intentional).
            assert db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize"
            ).fetchone()[0] == 0

            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert db.get_meta("fts_rebuild_high_water") is None
            assert db.get_meta("fts_rebuild_progress") is None
            n_msg = db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            n_fts = db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize"
            ).fetchone()[0]
            assert n_fts == n_msg
            assert len(db.search_messages("deployment")) == 1
            assert db.fts_optimize_available() is False
        finally:
            db.close()

    def test_repair_rebuilds_partial_index_without_duplicates(self, tmp_path):
        """high_water without progress on a PARTIALLY indexed DB must not
        replay the backfill from zero on top of surviving rows: the chunk
        worker inserts its whole id range with no anti-join, so replay
        duplicates every already-indexed row. Recovery must reset the index
        to a known-empty surface first, then rebuild."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            self._simulate_pre_fix_demote_crash_window(db)
            hw = db._conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages"
            ).fetchone()[0]
            db.set_meta("fts_rebuild_high_water", str(hw))
            db._conn.execute(
                "DELETE FROM state_meta WHERE key = ?", ("fts_rebuild_progress",)
            )
            # Partial index: one row survived from an interrupted backfill.
            db._conn.execute(
                "INSERT INTO messages_fts(rowid, content, tool_name, tool_calls) "
                "SELECT id, content, tool_name, tool_calls FROM messages "
                "WHERE id = 1"
            )
            db._conn.commit()
            assert db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize"
            ).fetchone()[0] == 1

            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            n_msg = db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            n_fts = db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize"
            ).fetchone()[0]
            # Exactly one index entry per message: no replay duplicates.
            assert n_fts == n_msg
            assert len(db.search_messages("deployment")) == 1
            db._conn.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
        finally:
            db.close()

    def test_repair_bookkeeping_reseeds_missing_progress(self, tmp_path):
        """Unit: high_water without progress gets progress='0' without
        forcing a full marker reset when a real backfill is already claimed."""
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="bookkeeping needle")
            db.set_meta("fts_rebuild_high_water", "42")
            db._conn.execute(
                "DELETE FROM state_meta WHERE key = ?", ("fts_rebuild_progress",)
            )
            db._conn.commit()
            db._repair_optimize_bookkeeping()
            assert db.get_meta("fts_rebuild_high_water") == "42"
            assert db.get_meta("fts_rebuild_progress") == "0"
        finally:
            db.close()

    def test_demote_writes_markers_before_empty_schema(self, tmp_path):
        """Demote must commit rebuild markers before createscript builds the
        empty v23 tables — so a crash between stage and ensure still leaves
        a resumable claim rather than an unmarked empty index."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)
        db = SessionDB(db_path=db_path)
        try:
            # Patch ensure to fail *after* the staged write commits, simulating
            # death mid schema-create. Markers must already be durable.
            orig_ensure = db._ensure_fts_schema
            calls = {"n": 0}

            def boom(cursor, table_name, ddl):
                calls["n"] += 1
                if table_name == "messages_fts":
                    # Markers must already be on disk from the staged write.
                    row = db._conn.execute(
                        "SELECT value FROM state_meta "
                        "WHERE key = 'fts_rebuild_high_water'"
                    ).fetchone()
                    assert row is not None, (
                        "markers must be committed before empty v23 schema create"
                    )
                    progress = db._conn.execute(
                        "SELECT value FROM state_meta "
                        "WHERE key = 'fts_rebuild_progress'"
                    ).fetchone()
                    assert progress is not None and progress[0] == "0"
                    raise sqlite3.OperationalError("simulated crash mid-ensure")
                return orig_ensure(cursor, table_name, ddl)

            db._ensure_fts_schema = boom  # type: ignore[method-assign]
            try:
                db._demote_legacy_fts_to_trash()
                raise AssertionError("demote should have raised")
            except sqlite3.OperationalError as exc:
                assert "simulated crash" in str(exc)

            # Staged demote survived: markers + trash, no successful stamp.
            assert db.get_meta("fts_rebuild_high_water") is not None
            assert db.get_meta("fts_rebuild_progress") == "0"
            assert db._has_fts_trash(db._conn) is True
            assert db.get_meta("fts_storage_version") is None

            # Restore ensure and resume — full optimize completes.
            db._ensure_fts_schema = orig_ensure  # type: ignore[method-assign]
            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert len(db.search_messages("deployment")) == 1
            assert db.fts_optimize_available() is False
        finally:
            db.close()

    def test_optimize_settle_refuses_pending_backfill(self, tmp_path):
        """Settle must not stamp while high_water markers remain."""
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="settle guard needle")
            # Plant markers without going through demote.
            db.set_meta("fts_rebuild_high_water", "1")
            db.set_meta("fts_rebuild_progress", "0")
            # The public contract: optimize returns ok=False when still
            # pending. Simulate an unfinishable backfill by stubbing the
            # chunk step to a no-op while markers stay.
            db.fts_rebuild_step = lambda: False  # type: ignore[method-assign]
            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is False
            assert result.get("reason") == "backfill_incomplete"
            assert db.get_meta("fts_storage_version") is None
            assert db.get_meta("fts_rebuild_high_water") is not None
        finally:
            db.close()

    def test_v23_fresh_db_born_optimized(self, tmp_path):
        """A brand-new DB is born on v23 — no legacy layout, no opt-in flag,
        no pending rebuild."""
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            assert db.fts_optimize_available() is False
            assert db.fts_rebuild_status() is None
            assert db.get_meta("fts_optimize_available") is None
            # Already external-content: no shadow copy tables.
            assert db._conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'messages_fts_content'"
            ).fetchone() is None
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="hello fresh world")
            assert len(db.search_messages("fresh")) == 1
        finally:
            db.close()


    def test_v23_cjk_tool_role_filter_uses_like_fallback(self, tmp_path):
        """A CJK query with role_filter=['tool'] must bypass the trigram index
        (tool rows aren't in it) and still find matches via LIKE."""
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="tool", content="错误日志：数据库连接超时",
                              tool_name="terminal")
            hits = db.search_messages("数据库连接", role_filter=["tool"])
            assert len(hits) == 1
            assert hits[0]["role"] == "tool"
        finally:
            db.close()

    def test_cjk_like_fallback_hides_rewound_messages(self, tmp_path):
        """The CJK LIKE fallback must honor the same visibility rule as the
        FTS5 paths: rewound rows (active=0, compacted=0) are hidden unless
        include_inactive=True; compaction-archived rows (active=0,
        compacted=1) stay discoverable (#38763)."""
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="被撤销的搜索目标内容")
            db.append_message("s1", role="user", content="被压缩归档的搜索目标内容")

            def _flags(conn):
                # First row: rewound (active=0, compacted=0) — hidden.
                conn.execute(
                    "UPDATE messages SET active = 0 WHERE content LIKE '%撤销%'"
                )
                # Second row: compaction-archived (active=0, compacted=1) — visible.
                conn.execute(
                    "UPDATE messages SET active = 0, compacted = 1 "
                    "WHERE content LIKE '%归档%'"
                )
            db._execute_write(_flags)

            # Short-CJK query (2 chars — below the 3-char trigram minimum)
            # forces the LIKE fallback; both rows contain the token 内容.
            # search_messages strips full content from results — assert on
            # the snippet column instead.
            hits = db.search_messages("内容")
            snippets = [h["snippet"] or "" for h in hits]
            assert any("归档" in s for s in snippets), "archived row must stay visible"
            assert not any("撤销" in s for s in snippets), "rewound row must be hidden"

            # include_inactive=True surfaces everything.
            all_hits = db.search_messages("内容", include_inactive=True)
            assert len(all_hits) == 2
        finally:
            db.close()

    def test_interrupted_optimize_reopen_still_reports_available(self, tmp_path):
        """An interrupted optimize followed by a process restart must keep
        offering the resume: the legacy vtables are gone (demoted), so the
        legacy-shape check alone would say "already compact" — the gate has
        to accept pending rebuild markers / trash tables too. And the reopen
        must NOT stamp fts_storage_version (the transition isn't done)."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)

        db = SessionDB(db_path=db_path)
        try:
            db._demote_legacy_fts_to_trash()
            db.fts_rebuild_step()  # one chunk, then "the process dies"
        finally:
            db.close()

        # Fresh open, as the CLI would after the interrupt.
        db = SessionDB(db_path=db_path)
        try:
            # The CLI gate must still offer optimize-storage (resume).
            assert db.fts_optimize_available() is True
            # The layout must NOT be stamped current mid-transition.
            assert db.get_meta("fts_storage_version") is None
            # Search stays complete through the gap supplement meanwhile.
            assert len(db.search_messages("TOOLBLOB")) == 1

            # Re-running the command resumes and completes the transition.
            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert db.fts_optimize_available() is False
            assert db.get_meta("fts_storage_version") == str(
                hermes_state.FTS_STORAGE_VERSION
            )
            assert db._conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_v22_trash%'"
            ).fetchall() == []
            for term in ("TOOLBLOB", "deployment"):
                assert db._conn.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                    (term,),
                ).fetchone()[0] == 1
            db._conn.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
        finally:
            db.close()

    def test_optimize_fts_storage_resumable_after_interrupt(self, tmp_path):
        """A partially-completed optimize resumes on re-run: after demote +
        one chunk, re-invoking finishes without duplicating rows."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)

        db = SessionDB(db_path=db_path)
        try:
            # Simulate an interrupted run: demote + a single backfill chunk,
            # then stop (as if the process died mid-optimize).
            db._demote_legacy_fts_to_trash()
            assert db.fts_rebuild_status() is not None
            db.fts_rebuild_step()  # one chunk only

            # Old rows not yet backfilled are still findable via gap supplement.
            assert len(db.search_messages("TOOLBLOB")) == 1

            # Re-run the full command — must resume, not restart or duplicate.
            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert db.fts_rebuild_status() is None
            assert db._conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0] == SCHEMA_VERSION
            for term in ("TOOLBLOB", "deployment"):
                assert db._conn.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                    (term,),
                ).fetchone()[0] == 1
            db._conn.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
        finally:
            db.close()

    def test_optimize_fts_storage_transitions_to_v23(self, tmp_path):
        """`optimize_fts_storage()` migrates a legacy DB to v23 external-content
        to completion: no shadow copies, tool rows excluded from trigram,
        version bumped, everything searchable exactly once."""
        db_path = tmp_path / "v22.db"
        self._build_v22_db(db_path)

        db = SessionDB(db_path=db_path)
        try:
            assert db.fts_optimize_available() is True
            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True

            # Layout stamped current; flag cleared; no longer "available".
            assert db.get_meta("fts_storage_version") == str(
                hermes_state.FTS_STORAGE_VERSION
            )
            assert db._conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0] == SCHEMA_VERSION
            assert db.fts_optimize_available() is False
            assert db.fts_rebuild_status() is None

            # External-content: no *_content shadow tables, no trash left.
            for shadow in ("messages_fts_content", "messages_fts_trigram_content"):
                assert db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = ?", (shadow,)
                ).fetchone() is None
            assert db._conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_v22_trash%'"
            ).fetchall() == []

            # Standard FTS: all rows incl tool metadata (#16751).
            assert len(db.search_messages("TOOLBLOB")) == 1
            assert len(db.search_messages("send_message")) == 1
            # Trigram excludes tool rows; CJK conversation search works.
            assert len(db.search_messages("大别山项目")) == 2
            assert db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH '\"项目文件内容\"'"
            ).fetchone()[0] == 0
            assert db.search_messages("项目文件内容", role_filter=["tool"]) != []
            # No duplicate index entries; integrity clean.
            for term in ("TOOLBLOB", "deployment"):
                assert db._conn.execute(
                    "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
                    (term,),
                ).fetchone()[0] == 1
            db._conn.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
        finally:
            db.close()

    def test_v23_trigram_stays_in_sync_on_write_paths(self, tmp_path):
        """INSERT/UPDATE/DELETE through SessionDB keep both indexes coherent
        under the new trigger shape (integrity-check verifies external
        content agreement)."""
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="搜索大别山项目相关资料")
            db.append_message("s1", role="tool", content="工具输出的大段内容在这里",
                              tool_name="web_search")
            db.append_message("s1", role="assistant", content="assistant reply")

            # Trigram: user+assistant only; standard: everything.
            assert db._conn.execute("SELECT COUNT(*) FROM messages_fts_trigram").fetchone()[0] == 2
            assert db._conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 3

            # Rewind-style UPDATE (active=0) must not desync the index — the
            # triggers only fire on content/tool column changes.
            def _deactivate(conn):
                conn.execute("UPDATE messages SET active = 0 WHERE role = 'assistant'")
            db._execute_write(_deactivate)

            # FTS5 integrity-check raises SQLITE_CORRUPT_VTAB on any
            # index/content disagreement; passing = indexes are coherent.
            db._conn.execute(
                "INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)"
            )
            db._conn.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram, rank) "
                "VALUES('integrity-check', 1)"
            )
        finally:
            db.close()

    def test_fts_teardown_single_key_high_water_drains_and_drops(self, tmp_path):
        """#79324: single-column-key trash tables drain via a high-water
        marker so each chunk only scans rows after the previous chunk.

        Builds a large trash table with a rowid-like integer PK, then drives
        ``_fts_teardown_trash_step`` to completion. Verifies every row is
        removed, the marker advances monotonically, the marker is cleared,
        and the table is dropped at the end.
        """
        db = SessionDB(db_path=tmp_path / "trash.db")
        try:
            conn = db._conn
            # A plain trash table shaped like a demoted FTS shadow table
            # (single integer PK — the common, large-table shape).
            conn.execute(
                "CREATE TABLE fts_v22_trash_messages_fts_data "
                "(docid INTEGER PRIMARY KEY, block BLOB)"
            )
            conn.executemany(
                "INSERT INTO fts_v22_trash_messages_fts_data "
                "(docid, block) VALUES (?, ?)",
                [(i, b"x" * 64) for i in range(1, 2501)],
            )
            conn.commit()

            assert db._has_fts_trash(conn) is True

            steps = 0
            while db._fts_teardown_trash_step():
                steps += 1
                assert steps < 100, "teardown never finished"

            # All rows gone, table dropped, marker cleaned up.
            assert db._has_fts_trash(conn) is False
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name = "
                "'fts_v22_trash_messages_fts_data'"
            ).fetchone() is None
            assert db.get_meta("fts_teardown_fts_v22_trash_messages_fts_data_progress") is None
            # Multiple chunks were needed (2500 rows / 500 chunk).
            assert steps >= 5
        finally:
            db.close()

    def test_fts_teardown_high_water_resumes_after_interruption(self, tmp_path):
        """#79324: the high-water marker survives an interrupted teardown,
        so the next call resumes from the marker instead of the table start."""
        db = SessionDB(db_path=tmp_path / "trash.db")
        try:
            conn = db._conn
            conn.execute(
                "CREATE TABLE fts_v22_trash_messages_fts_data "
                "(docid INTEGER PRIMARY KEY, block BLOB)"
            )
            conn.executemany(
                "INSERT INTO fts_v22_trash_messages_fts_data "
                "(docid, block) VALUES (?, ?)",
                [(i, b"x" * 64) for i in range(1, 1201)],
            )
            conn.commit()

            # Drain two chunks, then simulate a crash: the marker stays at
            # the last drained key and the remaining rows are intact.
            assert db._fts_teardown_trash_step() is True
            assert db._fts_teardown_trash_step() is True
            marker = db.get_meta("fts_teardown_fts_v22_trash_messages_fts_data_progress")
            assert marker is not None
            assert int(marker) == 1000  # 2 chunks x 500 rows

            remaining = conn.execute(
                "SELECT COUNT(*) FROM fts_v22_trash_messages_fts_data"
            ).fetchone()[0]
            assert remaining == 200

            # Resume: drains the rest, drops the table.
            while db._fts_teardown_trash_step():
                pass
            assert db._has_fts_trash(conn) is False
            assert db.get_meta("fts_teardown_fts_v22_trash_messages_fts_data_progress") is None
        finally:
            db.close()

    def test_fts_teardown_compound_key_keeps_legacy_path(self, tmp_path):
        """#79324: multi-column-PK trash tables (small by construction) keep
        the legacy chunked delete — the high-water path only applies to
        single-column keys."""
        db = SessionDB(db_path=tmp_path / "trash.db")
        try:
            conn = db._conn
            conn.execute(
                "CREATE TABLE fts_v22_trash_messages_fts_idx "
                "(segid INTEGER, term TEXT, pgno INTEGER, "
                "PRIMARY KEY (segid, term, pgno)) WITHOUT ROWID"
            )
            conn.executemany(
                "INSERT INTO fts_v22_trash_messages_fts_idx "
                "(segid, term, pgno) VALUES (?, ?, ?)",
                [(i % 3, f"term-{i}", i) for i in range(20)],
            )
            conn.commit()

            steps = 0
            while db._fts_teardown_trash_step():
                steps += 1
                assert steps < 10

            assert db._has_fts_trash(conn) is False
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name = "
                "'fts_v22_trash_messages_fts_idx'"
            ).fetchone() is None
        finally:
            db.close()



# ---------------------------------------------------------------------------
# apply_wal_with_fallback — read-only probe tests
# ---------------------------------------------------------------------------


class TestSessionTitle:
    def test_set_and_get_title(self, db):
        db.create_session(session_id="s1", source="cli")
        assert db.set_session_title("s1", "My Session") is True

        session = db.get_session("s1")
        assert session["title"] == "My Session"








    def test_title_empty_string_normalized_to_none(self, db):
        """Empty strings are normalized to None (clearing the title)."""
        db.create_session(session_id="s1", source="cli")
        db.set_session_title("s1", "My Title")
        # Setting to empty string should clear the title (normalize to None)
        db.set_session_title("s1", "")

        session = db.get_session("s1")
        assert session["title"] is None

    def test_auto_title_only_sets_an_empty_title(self, db):
        db.create_session(session_id="s1", source="cli")
        assert db.set_auto_title_if_empty("s1", "Generated Title") is True
        assert db.set_auto_title_if_empty("s1", "Replacement Title") is False
        assert db.get_session_title("s1") == "Generated Title"

    def test_multiple_empty_titles_no_conflict(self, db):
        """Multiple sessions can have empty-string (normalized to NULL) titles."""
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="cli")
        db.set_session_title("s1", "")
        db.set_session_title("s2", "")
        # Both should be None, no uniqueness conflict
        assert db.get_session("s1")["title"] is None
        assert db.get_session("s2")["title"] is None

    def test_set_title_nonexistent_session(self, db):
        assert db.set_session_title("nonexistent", "Title") is False

    def test_title_in_export(self, db):
        db.create_session(session_id="s1", source="cli")
        db.set_session_title("s1", "Export Test")
        db.append_message("s1", role="user", content="Hello")

        export = db.export_session("s1")
        assert export["title"] == "Export Test"

    def test_title_in_search_sessions(self, db):
        db.create_session(session_id="s1", source="cli")
        db.set_session_title("s1", "Debugging Auth")
        db.create_session(session_id="s2", source="cli")

        sessions = db.search_sessions()
        titled = [s for s in sessions if s.get("title") == "Debugging Auth"]
        assert len(titled) == 1
        assert titled[0]["id"] == "s1"

    def test_title_initially_none(self, db):
        db.create_session(session_id="s1", source="cli")
        session = db.get_session("s1")
        assert session["title"] is None

    def test_title_survives_end_session(self, db):
        db.create_session(session_id="s1", source="cli")
        db.set_session_title("s1", "Before End")
        db.end_session("s1", end_reason="user_exit")

        session = db.get_session("s1")
        assert session["title"] == "Before End"
        assert session["ended_at"] is not None

    def test_title_with_special_characters(self, db):
        db.create_session(session_id="s1", source="cli")
        title = "PR #438 — fixing the 'auth' middleware"
        db.set_session_title("s1", title)

        session = db.get_session("s1")
        assert session["title"] == title

    def test_update_title(self, db):
        db.create_session(session_id="s1", source="cli")
        db.set_session_title("s1", "First Title")
        db.set_session_title("s1", "Updated Title")

        session = db.get_session("s1")
        assert session["title"] == "Updated Title"




class TestAutoMaintenance:
    def _make_old_ended(self, db, sid: str, days_old: int = 100):
        """Create a session that is ended and was started `days_old` days ago."""
        db.create_session(session_id=sid, source="cli")
        db.end_session(sid, end_reason="done")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - days_old * 86400, sid),
        )
        db._conn.commit()

    def test_first_run_prunes_and_vacuums(self, db):
        self._make_old_ended(db, "old1", days_old=100)
        self._make_old_ended(db, "old2", days_old=100)
        db.create_session(session_id="new", source="cli")  # active, must survive

        result = db.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 2
        assert result["vacuumed"] is True
        assert result.get("error") is None
        assert db.get_session("old1") is None
        assert db.get_session("old2") is None
        assert db.get_session("new") is not None

    def test_second_call_within_interval_skips(self, db):
        self._make_old_ended(db, "old", days_old=100)
        first = db.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert first["skipped"] is False
        assert first["pruned"] == 1

        # Create another prunable session; a second call within
        # min_interval_hours should still skip without touching it.
        self._make_old_ended(db, "old2", days_old=100)
        second = db.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert second["skipped"] is True
        assert second["pruned"] == 0
        assert db.get_session("old2") is not None  # untouched






    def test_auto_prune_deletes_transcript_files(self, db, tmp_path):
        """Issue #3015: auto-prune must also delete on-disk transcript files."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        self._make_old_ended(db, "old1", days_old=100)
        self._make_old_ended(db, "old2", days_old=100)
        db.create_session(session_id="new", source="cli")  # active

        # Transcript files mimicking real gateway/CLI layout
        (sessions_dir / "old1.json").write_text("{}")
        (sessions_dir / "old1.jsonl").write_text("{}\n")
        (sessions_dir / "old2.jsonl").write_text("{}\n")
        (sessions_dir / "request_dump_old1_001.json").write_text("{}")
        (sessions_dir / "new.jsonl").write_text("{}\n")  # active, must survive

        result = db.maybe_auto_prune_and_vacuum(
            retention_days=90, sessions_dir=sessions_dir
        )
        assert result["pruned"] == 2

        # Pruned transcript files are gone
        assert not (sessions_dir / "old1.json").exists()
        assert not (sessions_dir / "old1.jsonl").exists()
        assert not (sessions_dir / "old2.jsonl").exists()
        assert not (sessions_dir / "request_dump_old1_001.json").exists()
        # Active session's transcript is untouched
        assert (sessions_dir / "new.jsonl").exists()

    def test_auto_prune_without_sessions_dir_preserves_files(self, db, tmp_path):
        """Backward-compat: no sessions_dir = DB-only cleanup (legacy behavior)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        self._make_old_ended(db, "old", days_old=100)
        (sessions_dir / "old.jsonl").write_text("{}\n")

        result = db.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["pruned"] == 1
        # File stays — caller didn't opt in
        assert (sessions_dir / "old.jsonl").exists()

    def test_corrupt_last_run_marker_treated_as_no_prior_run(self, db):
        """A non-numeric marker must not break maintenance."""
        db.set_meta("last_auto_prune", "not-a-timestamp")
        self._make_old_ended(db, "old", days_old=100)
        result = db.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 1

    def test_no_prunable_sessions_no_vacuum(self, db):
        """When prune deletes 0 rows, VACUUM is skipped (wasted I/O)."""
        db.create_session(session_id="fresh", source="cli")  # too recent
        result = db.maybe_auto_prune_and_vacuum(retention_days=90)
        assert result["skipped"] is False
        assert result["pruned"] == 0
        assert result["vacuumed"] is False
        # But last-run is still recorded so we don't retry immediately.
        assert db.get_meta("last_auto_prune") is not None

    def test_prune_sessions_deletes_files_for_pruned_only(self, db, tmp_path):
        """Active-session transcripts must never be deleted by prune."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        self._make_old_ended(db, "old", days_old=100)
        db.create_session(session_id="active", source="cli")  # not ended
        (sessions_dir / "old.jsonl").write_text("{}\n")
        (sessions_dir / "active.jsonl").write_text("{}\n")

        count = db.prune_sessions(older_than_days=90, sessions_dir=sessions_dir)
        assert count == 1
        assert not (sessions_dir / "old.jsonl").exists()
        assert (sessions_dir / "active.jsonl").exists()

    def test_second_call_after_interval_runs_again(self, db):
        self._make_old_ended(db, "old", days_old=100)
        db.maybe_auto_prune_and_vacuum(retention_days=90, min_interval_hours=24)

        # Backdate the last-run marker to force another run.
        db.set_meta("last_auto_prune", str(time.time() - 48 * 3600))

        self._make_old_ended(db, "old2", days_old=100)
        result = db.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=24
        )
        assert result["skipped"] is False
        assert result["pruned"] == 1
        assert db.get_session("old2") is None

    def test_state_meta_survives_vacuum(self, db):
        """Marker written just before VACUUM must still be readable after."""
        self._make_old_ended(db, "old", days_old=100)
        db.maybe_auto_prune_and_vacuum(retention_days=90)
        marker = db.get_meta("last_auto_prune")
        assert marker is not None
        # Should parse as a float timestamp close to now.
        assert abs(float(marker) - time.time()) < 60

    def test_vacuum_disabled_via_flag(self, db):
        self._make_old_ended(db, "old", days_old=100)
        result = db.maybe_auto_prune_and_vacuum(retention_days=90, vacuum=False)
        assert result["pruned"] == 1
        assert result["vacuumed"] is False





# =========================================================================
# FTS5 indexing of tool_calls / tool_name (#16751)
# =========================================================================

class TestBulkDeleteSessions:
    """``delete_sessions(ids)`` — the bulk-delete primitive backing the
    sessions-page "Delete N selected" button. Per-row contract matches
    :meth:`SessionDB.delete_session` (children orphaned, not cascade-
    deleted), but applied across the whole list in one transaction.

    Invariants this class locks in:

    1. Returns the real deleted count (existing intersection), not
       just ``len(session_ids)`` — selection state in the UI can race
       against another tab's delete.
    2. Unknown IDs are silently skipped, never raise.
    3. ``message_count > 0`` sessions are deleted too — unlike
       ``delete_empty_sessions``, the user explicitly picked them, so
       we trust the selection.
    4. Live (un-ended) and archived sessions ARE deleted on explicit
       selection (no bulk-sweep safety guards apply when the user
       hand-picks the row).
    5. Children of any deleted parent are orphaned, even when the
       parent is mid-list.
    6. ``[]`` / ``None``-laden lists are safe no-ops.
    """

    def test_deletes_listed_sessions(self, db):
        db.create_session(session_id="a", source="cli")
        db.append_message("a", role="user", content="hi")
        db.create_session(session_id="b", source="cli")
        db.create_session(session_id="c", source="cli")

        deleted = db.delete_sessions(["a", "b"])
        assert deleted == 2
        assert db.get_session("a") is None
        assert db.get_session("b") is None
        # Unlisted survives.
        assert db.get_session("c") is not None





    def test_orphans_children_of_deleted_parents(self, db):
        """Bulk-deleting a parent leaves its children alive but
        re-parented to NULL. Same contract as the single-session
        :meth:`delete_session` path."""
        db.create_session(session_id="parent", source="cli")
        db.create_session(
            session_id="child", source="cli", parent_session_id="parent"
        )

        deleted = db.delete_sessions(["parent"])
        assert deleted == 1
        child = db.get_session("child")
        assert child is not None
        assert child["parent_session_id"] is None


    def test_cleans_up_transcript_files(self, db, tmp_path):
        """When ``sessions_dir`` is provided, on-disk transcripts are
        swept as part of the bulk operation — mirrors the per-row
        :meth:`delete_session(sessions_dir=...)` behaviour so the
        bulk-delete CLI / web flows don't leak files."""
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="cli")
        (tmp_path / "s1.jsonl").write_text("")
        (tmp_path / "s2.json").write_text("{}")

        deleted = db.delete_sessions(["s1", "s2"], sessions_dir=tmp_path)
        assert deleted == 2
        assert not (tmp_path / "s1.jsonl").exists()
        assert not (tmp_path / "s2.json").exists()

    def test_dedupes_duplicate_ids(self, db):
        """The same ID listed twice counts as one deletion. Defends
        against a hand-crafted POST body or a UI bug that double-adds
        the same selection."""
        db.create_session(session_id="real", source="cli")
        deleted = db.delete_sessions(["real", "real"])
        assert deleted == 1

    def test_deletes_archived_and_active_when_selected(self, db):
        """Unlike the safety-gated ``delete_empty_sessions`` sweep,
        explicit bulk-select trusts the user — archived sessions and
        un-ended live sessions are both deleted when in the list.
        Otherwise the selection UI would silently 'leak' rows the user
        thought they'd removed."""
        db.create_session(session_id="archived", source="cli")
        db.end_session("archived", end_reason="done")
        db.set_session_archived("archived", True)
        db.create_session(session_id="live", source="cli")

        deleted = db.delete_sessions(["archived", "live"])
        assert deleted == 2
        assert db.get_session("archived") is None
        assert db.get_session("live") is None

    def test_drops_non_string_entries(self, db):
        """Stray ``None`` / empty strings in the input list are
        filtered out before hitting SQL. Callers may pull selection IDs
        from a Set-like that occasionally contains noise; we don't want
        a SQL parameter-type error to fail the whole batch."""
        db.create_session(session_id="real", source="cli")
        # noinspection PyTypeChecker
        deleted = db.delete_sessions(["real", None, "", "ghost"])  # type: ignore[list-item]
        assert deleted == 1
        assert db.get_session("real") is None

    def test_empty_list_is_noop(self, db):
        """``[]`` returns 0 without touching the DB. Guards against a
        bulk endpoint with an empty payload triggering an
        unconditional 'wipe everything' if the caller forgets the
        WHERE clause."""
        db.create_session(session_id="keep", source="cli")
        assert db.delete_sessions([]) == 0
        assert db.get_session("keep") is not None

    def test_returns_real_count_skipping_unknown_ids(self, db):
        """Unknown IDs are silently skipped — the return value reflects
        what was *actually* deleted, so the UI can show an accurate
        toast even if the selection raced against another tab."""
        db.create_session(session_id="real", source="cli")

        deleted = db.delete_sessions(["real", "ghost1", "ghost2"])
        assert deleted == 1
        assert db.get_session("real") is None


class TestExcludeSources:
    """Tests for exclude_sources on list_sessions_rich and search_messages."""

    def test_list_sessions_rich_excludes_tool_source(self, db):
        db.create_session("s1", "cli")
        db.create_session("s2", "tool")
        db.create_session("s3", "telegram")
        sessions = db.list_sessions_rich(exclude_sources=["tool"])
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s3" in ids
        assert "s2" not in ids





    def test_search_messages_excludes_tool_source(self, db):
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "Python deployment question")
        db.create_session("s2", "tool")
        db.append_message("s2", "user", "Python automated question")
        results = db.search_messages("Python", exclude_sources=["tool"])
        sources = [r["source"] for r in results]
        assert "cli" in sources
        assert "tool" not in sources

    def test_list_sessions_rich_exclude_multiple_sources(self, db):
        db.create_session("s1", "cli")
        db.create_session("s2", "tool")
        db.create_session("s3", "cron")
        db.create_session("s4", "telegram")
        sessions = db.list_sessions_rich(exclude_sources=["tool", "cron"])
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s4" in ids
        assert "s2" not in ids
        assert "s3" not in ids

    def test_list_sessions_rich_no_exclusion_returns_all(self, db):
        db.create_session("s1", "cli")
        db.create_session("s2", "tool")
        sessions = db.list_sessions_rich()
        ids = [s["id"] for s in sessions]
        assert "s1" in ids
        assert "s2" in ids

    def test_list_sessions_rich_source_and_exclude_combined(self, db):
        """When source= is explicit, exclude_sources should not conflict."""
        db.create_session("s1", "cli")
        db.create_session("s2", "tool")
        db.create_session("s3", "telegram")
        # Explicit source filter: only tool sessions, no exclusion
        sessions = db.list_sessions_rich(source="tool")
        ids = [s["id"] for s in sessions]
        assert ids == ["s2"]

    def test_search_messages_no_exclusion_returns_all_sources(self, db):
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "Rust deployment question")
        db.create_session("s2", "tool")
        db.append_message("s2", "user", "Rust automated question")
        results = db.search_messages("Rust")
        sources = [r["source"] for r in results]
        assert "cli" in sources
        assert "tool" in sources

    def test_search_messages_source_include_and_exclude(self, db):
        """source_filter (include) and exclude_sources can coexist."""
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "Golang test")
        db.create_session("s2", "telegram")
        db.append_message("s2", "user", "Golang test")
        db.create_session("s3", "tool")
        db.append_message("s3", "user", "Golang test")
        # Include cli+tool, but exclude tool → should only return cli
        results = db.search_messages(
            "Golang", source_filter=["cli", "tool"], exclude_sources=["tool"]
        )
        sources = [r["source"] for r in results]
        assert sources == ["cli"]




class TestPruneSessions:
    def test_prune_old_ended_sessions(self, db):
        # Create and end an "old" session
        db.create_session(session_id="old", source="cli")
        db.end_session("old", end_reason="done")
        # Manually backdate started_at
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 100 * 86400, "old"),
        )
        db._conn.commit()

        # Create a recent session
        db.create_session(session_id="new", source="cli")

        pruned = db.prune_sessions(older_than_days=90)
        assert pruned == 1
        assert db.get_session("old") is None
        session = db.get_session("new")
        assert session is not None
        assert session["id"] == "new"


    def test_prune_skips_active_sessions(self, db):
        db.create_session(session_id="active", source="cli")
        # Backdate but don't end
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 200 * 86400, "active"),
        )
        db._conn.commit()

        pruned = db.prune_sessions(older_than_days=90)
        assert pruned == 0
        assert db.get_session("active") is not None
        assert db.count_open_prune_matches(older_than_days=90) == 1

    def test_open_prune_match_count_applies_other_filters(self, db):
        db.create_session(session_id="matching-open", source="cron")
        db.create_session(session_id="other-source", source="cli")
        db.create_session(session_id="ended", source="cron")
        db.end_session("ended", "completed")
        old = time.time() - 200 * 86400
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id IN (?, ?, ?)",
            (old, "matching-open", "other-source", "ended"),
        )
        db._conn.commit()

        assert db.count_open_prune_matches(
            older_than_days=90, source="cron", archived=False
        ) == 1
        assert {row["id"] for row in db.list_prune_candidates(
            older_than_days=90, source="cron", archived=False
        )} == {"ended"}

    def test_prune_entire_old_chain(self, db):
        """All sessions in a chain are old — entire chain is pruned."""
        old_ts = time.time() - 200 * 86400

        db.create_session(session_id="X", source="cli")
        db.end_session("X", end_reason="compressed")
        db.create_session(session_id="Y", source="cli", parent_session_id="X")
        db.end_session("Y", end_reason="compressed")
        db.create_session(session_id="Z", source="cli", parent_session_id="Y")
        db.end_session("Z", end_reason="done")

        for sid in ("X", "Y", "Z"):
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (old_ts, sid)
            )
        db._conn.commit()

        pruned = db.prune_sessions(older_than_days=90)
        assert pruned == 3
        for sid in ("X", "Y", "Z"):
            assert db.get_session(sid) is None

    def test_prune_with_multilevel_chain(self, db):
        """Pruning old sessions orphans newer children instead of crashing on FK."""
        old_ts = time.time() - 200 * 86400
        recent_ts = time.time() - 10 * 86400

        # Chain: A (old) -> B (old) -> C (recent) -> D (recent)
        db.create_session(session_id="A", source="cli")
        db.end_session("A", end_reason="compressed")
        db.create_session(session_id="B", source="cli", parent_session_id="A")
        db.end_session("B", end_reason="compressed")
        db.create_session(session_id="C", source="cli", parent_session_id="B")
        db.end_session("C", end_reason="compressed")
        db.create_session(session_id="D", source="cli", parent_session_id="C")
        db.end_session("D", end_reason="done")

        # Backdate A and B to be old; C and D stay recent
        for sid, ts in [("A", old_ts), ("B", old_ts), ("C", recent_ts), ("D", recent_ts)]:
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?", (ts, sid)
            )
        db._conn.commit()

        # Should not raise IntegrityError
        pruned = db.prune_sessions(older_than_days=90)
        assert pruned == 2  # only A and B
        assert db.get_session("A") is None
        assert db.get_session("B") is None
        # C and D survive, C is orphaned (parent_session_id NULL)
        c = db.get_session("C")
        assert c is not None
        assert c["parent_session_id"] is None
        d = db.get_session("D")
        assert d is not None
        assert d["parent_session_id"] == "C"

    def test_prune_with_source_filter(self, db):
        for sid, src in [("old_cli", "cli"), ("old_tg", "telegram")]:
            db.create_session(session_id=sid, source=src)
            db.end_session(sid, end_reason="done")
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 200 * 86400, sid),
            )
        db._conn.commit()

        pruned = db.prune_sessions(older_than_days=90, source="cli")
        assert pruned == 1
        assert db.get_session("old_cli") is None
        assert db.get_session("old_tg") is not None





class TestConcurrentWriteSafety:
    def test_create_session_insert_or_ignore_is_idempotent(self, db):
        """create_session with the same ID twice must not raise (INSERT OR IGNORE)."""
        db.create_session(session_id="dup-1", source="cli", model="m")
        # Second call should be silent — no IntegrityError
        db.create_session(session_id="dup-1", source="gateway", model="m2")
        session = db.get_session("dup-1")
        # Row should exist (first write wins with OR IGNORE)
        assert session is not None
        assert session["source"] == "cli"

    def test_ensure_session_creates_missing_row(self, db):
        """ensure_session must create a minimal row when the session doesn't exist."""
        assert db.get_session("orphan-session") is None
        db.ensure_session("orphan-session", source="gateway", model="test-model")
        row = db.get_session("orphan-session")
        assert row is not None
        assert row["source"] == "gateway"
        assert row["model"] == "test-model"

    def test_ensure_session_allows_append_message_after_failed_create(self, db):
        """Messages can be flushed even when create_session failed at startup.

        Simulates the #3139 scenario: create_session raises (lock), then
        ensure_session is called during flush, then append_message succeeds.
        """
        # Simulate failed create_session — row absent
        db.ensure_session("late-session", source="gateway", model="gpt-4")
        db.append_message(
            session_id="late-session",
            role="user",
            content="hello after lock",
        )
        msgs = db.get_messages("late-session")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello after lock"

    def test_ensure_session_is_idempotent(self, db):
        """ensure_session on an existing row must be a no-op (no overwrite)."""
        db.create_session(session_id="existing", source="cli", model="original-model")
        db.ensure_session("existing", source="gateway", model="overwrite-model")
        row = db.get_session("existing")
        # First write wins — ensure_session must not overwrite
        assert row["source"] == "cli"
        assert row["model"] == "original-model"

    def test_sqlite_timeout_is_at_least_30s(self, db):
        """Connection timeout should be >= 30s to survive CLI/gateway contention."""
        # Access the underlying connection timeout via sqlite3 introspection.
        # There is no public API, so we check the kwarg via the module default.
        import inspect
        from hermes_state import SessionDB as _SessionDB
        src = inspect.getsource(_SessionDB.__init__)
        assert "30" in src, (
            "SQLite timeout should be at least 30s to handle CLI/gateway lock contention"
        )





# =========================================================================
# Auto-maintenance: state_meta + vacuum + maybe_auto_prune_and_vacuum
# =========================================================================

class TestReconcileColumnsErrorHandling:
    """_reconcile_columns must not bury migration failures (#79531/#80037).

    A locked ALTER used to be swallowed at DEBUG: startup "succeeded" with a
    half-reconciled schema and every session-list read then 500ed with
    "no such column" until an unrelated writable open. The contract now:
    duplicate-column races stay quiet, lock/busy propagates (so the open-time
    lock patience retries the whole init), everything else warns.
    """

    class _FailingAlterCursor:
        """Pass through to a real cursor, failing ALTER TABLE with ``exc``."""

        def __init__(self, real_cursor, exc):
            self._real = real_cursor
            self._exc = exc

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("ALTER TABLE"):
                raise self._exc
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _db_missing_column(self, tmp_path):
        """A store whose sessions table lacks last_read_at."""
        db_path = tmp_path / "state.db"
        seed = SessionDB(db_path=db_path)
        seed.close()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("ALTER TABLE sessions DROP COLUMN last_read_at")
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_locked_alter_propagates(self, tmp_path):
        """database-is-locked must escape _reconcile_columns, not vanish.

        Propagation is what lets _connect_and_init_with_lock_patience retry
        the whole init with jittered backoff instead of serving a store
        that is silently behind SCHEMA_SQL.
        """
        db_path = self._db_missing_column(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            stale = SessionDB.__new__(SessionDB)
            stale._conn = conn
            cursor = self._FailingAlterCursor(
                conn.cursor(),
                sqlite3.OperationalError("database is locked"),
            )
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                stale._reconcile_columns(cursor)
        finally:
            conn.close()

    def test_duplicate_column_race_stays_quiet(self, tmp_path, caplog):
        """A duplicate-column race is expected and must not warn or raise."""
        import logging

        db_path = self._db_missing_column(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            stale = SessionDB.__new__(SessionDB)
            stale._conn = conn
            cursor = self._FailingAlterCursor(
                conn.cursor(),
                sqlite3.OperationalError(
                    "duplicate column name: last_read_at"
                ),
            )
            with caplog.at_level(logging.WARNING, logger="hermes_state"):
                stale._reconcile_columns(cursor)
        finally:
            conn.close()
        assert not [
            r for r in caplog.records if "reconcile" in r.getMessage()
        ]

    def test_other_alter_failures_warn(self, tmp_path, caplog):
        """Schema mistakes (e.g. un-ADDable NOT NULL) log at WARNING."""
        import logging

        db_path = self._db_missing_column(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            stale = SessionDB.__new__(SessionDB)
            stale._conn = conn
            cursor = self._FailingAlterCursor(
                conn.cursor(),
                sqlite3.OperationalError(
                    "Cannot add a NOT NULL column with default value NULL"
                ),
            )
            with caplog.at_level(logging.WARNING, logger="hermes_state"):
                stale._reconcile_columns(cursor)
        finally:
            conn.close()
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "reconcile" in r.getMessage()
        ]
        assert warnings, "un-ADDable column failure must be logged at WARNING+"

    def test_locked_alter_is_retried_by_open_lock_patience(self, tmp_path, monkeypatch):
        """End-to-end: a transiently locked ALTER heals on open retry.

        The lock-patience wrapper retries on OperationalError raised out of
        _connect_and_init; before this fix _reconcile_columns caught the
        error internally so the retry never saw it and the store stayed
        stale forever.
        """
        db_path = self._db_missing_column(tmp_path)

        original = SessionDB._reconcile_columns
        calls = {"n": 0}

        def flaky_reconcile(self, cursor):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(self, cursor)

        monkeypatch.setattr(SessionDB, "_reconcile_columns", flaky_reconcile)
        # Keep the retry fast — patience budget is 20s by default.
        monkeypatch.setattr(SessionDB, "_WRITE_RETRY_SLOW_MIN_S", 0.001)
        monkeypatch.setattr(SessionDB, "_WRITE_RETRY_SLOW_MAX_S", 0.005)

        healed = SessionDB(db_path=db_path)
        try:
            cols = {
                r[1]
                for r in healed._conn.execute(
                    'PRAGMA table_info("sessions")'
                ).fetchall()
            }
        finally:
            healed.close()
        assert calls["n"] >= 2, "lock patience must retry the init"
        assert "last_read_at" in cols


class TestSessionTitleIndexRepair:
    @staticmethod
    def _seed_legacy_database(tmp_path, *, duplicate_titles):
        db_path = tmp_path / "legacy_titles.db"
        session_db = SessionDB(db_path=db_path)
        session_db.create_session("older", "cli")
        session_db.append_message("older", role="user", content="keep older message")
        session_db.create_session("newer", "cli")
        session_db.append_message(
            "newer", role="assistant", content="keep newer message"
        )
        session_db.create_session("unique", "cli")
        session_db.set_session_title("unique", "unique-title")
        session_db.close()

        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP INDEX idx_sessions_title_unique")
            if duplicate_titles:
                conn.execute(
                    "UPDATE sessions SET title = 'shared-title' "
                    "WHERE id IN ('older', 'newer')"
                )

        return db_path

    def test_duplicate_titles_are_repaired_without_deleting_sessions(self, tmp_path):
        db_path = self._seed_legacy_database(tmp_path, duplicate_titles=True)

        reopened = SessionDB(db_path=db_path)
        try:
            conn = reopened._conn
            assert conn is not None
            rows = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT id, title FROM sessions ORDER BY rowid"
                ).fetchall()
            }
            assert set(rows) == {"older", "newer", "unique"}
            assert rows["older"]["title"] is None
            assert rows["newer"]["title"] == "shared-title"
            assert rows["unique"]["title"] == "unique-title"
            assert reopened.get_messages("older")[0]["content"] == "keep older message"
            assert reopened.get_messages("newer")[0]["content"] == "keep newer message"
            index = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_sessions_title_unique'"
            ).fetchone()
            assert index is not None
        finally:
            reopened.close()

    def test_clean_legacy_database_keeps_existing_titles(self, tmp_path):
        db_path = self._seed_legacy_database(tmp_path, duplicate_titles=False)

        reopened = SessionDB(db_path=db_path)
        try:
            assert reopened.get_session_title("unique") == "unique-title"
            assert reopened.get_session_title("older") is None
            assert reopened.get_session_title("newer") is None
        finally:
            reopened.close()

    def test_repaired_index_rejects_future_duplicate_title(self, tmp_path):
        db_path = self._seed_legacy_database(tmp_path, duplicate_titles=True)

        reopened = SessionDB(db_path=db_path)
        try:
            reopened.create_session("future", "cli")
            with pytest.raises(ValueError, match="already in use"):
                reopened.set_session_title("future", "shared-title")
        finally:
            reopened.close()




class TestDisplayMetadataPersistence:
    """Round-trip display_kind/display_metadata through every write path."""

    def test_append_message_round_trips_display_fields(self, db):
        db.create_session("s1", source="cli")
        meta = {"task_count": 2, "delegation_id": "del-1"}
        db.append_message(
            "s1", "user", "event text",
            display_kind="async_delegation_complete",
            display_metadata=meta,
        )
        conv = db.get_messages_as_conversation("s1")
        assert conv[0]["display_kind"] == "async_delegation_complete"
        assert conv[0]["display_metadata"] == meta

    def test_replace_messages_preserves_display_metadata(self, db):
        db.create_session("s1", source="cli")
        meta = {"task_count": 3, "delegation_id": "del-2", "duration_seconds": 12.5}
        db.append_message(
            "s1", "user", "event",
            display_kind="async_delegation_complete",
            display_metadata=meta,
        )
        # Reload via get_messages_as_conversation (which decodes display fields)
        # then replace_messages (which re-inserts via _insert_message_rows).
        conv = db.get_messages_as_conversation("s1")
        db.replace_messages("s1", conv)
        reloaded = db.get_messages_as_conversation("s1")
        assert reloaded[0]["display_kind"] == "async_delegation_complete"
        assert reloaded[0]["display_metadata"] == meta

    def test_archive_and_compact_preserves_display_metadata(self, db):
        db.create_session("s1", source="cli")
        meta = {"model": "test-model", "provider": "test-provider"}
        db.append_message(
            "s1", "user", "switch event",
            display_kind="model_switch",
            display_metadata=meta,
        )
        db.append_message("s1", "assistant", "reply")
        conv = db.get_messages_as_conversation("s1")
        db.archive_and_compact("s1", conv)
        reloaded = db.get_messages_as_conversation("s1")
        switched = [m for m in reloaded if m.get("display_kind") == "model_switch"]
        assert len(switched) == 1
        assert switched[0]["display_metadata"] == meta



class TestResolveSessionByNameOrId:
    """Tests for the main.py helper that resolves names or IDs."""

    def test_resolve_by_id(self, db):
        db.create_session("test-id-123", "cli")
        session = db.get_session("test-id-123")
        assert session is not None
        assert session["id"] == "test-id-123"

    def test_resolve_by_title_falls_back(self, db):
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        result = db.resolve_session_by_title("my project")
        assert result == "s1"



# =========================================================================
# Concurrent write safety / lock contention fixes (#3139)
# =========================================================================

class TestPerformancePragmasEndToEnd:
    """E2E guard for PR #71755: config-gated cache_size / mmap_size /
    temp_store must reach EVERY connection type (writer, read-only
    cross-profile attach, WAL per-thread reader) — and default installs
    (no ``database:`` keys) must see byte-identical SQLite defaults.

    NOTE: SQLite's compiled-in default for ``cache_size`` is already
    ``-2000``, so the configured value here is ``-16000`` — a value the
    test can actually discriminate from the default (a reverted prod
    change must FAIL this test, not accidentally pass it).
    """

    PRAGMAS = ("cache_size", "mmap_size", "temp_store")
    CONFIGURED = {"cache_size": -16000, "mmap_size": 1048576, "temp_store": 2}

    @staticmethod
    def _read(conn):
        return {
            name: conn.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("cache_size", "mmap_size", "temp_store")
        }

    @staticmethod
    def _sqlite_defaults(tmp_path):
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "baseline.db"))
        try:
            return {
                name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                for name in ("cache_size", "mmap_size", "temp_store")
            }
        finally:
            conn.close()

    def _fresh_home(self, tmp_path, monkeypatch, config_text=None):
        import hermes_state

        # Local venvs may bundle a WAL-reset-vulnerable SQLite (e.g. 3.46.0),
        # which would silently disable WAL and skip the per-thread reader
        # path. Force WAL eligibility so _get_read_conn is truly exercised
        # (established pattern used by the WAL tests above).
        monkeypatch.setattr(
            hermes_state,
            "is_sqlite_wal_reset_vulnerable",
            lambda version_info=None: False,
        )
        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        if config_text is not None:
            (home / "config.yaml").write_text(config_text)
        return home

    def test_configured_pragmas_reach_all_connection_types(
        self, tmp_path, monkeypatch
    ):
        from hermes_state import SessionDB

        home = self._fresh_home(
            tmp_path,
            monkeypatch,
            "database:\n"
            "  cache_size: -16000\n"
            "  temp_store: 2\n"
            "  mmap_size: 1048576\n",
        )
        db_path = home / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            # Writer connection.
            assert self._read(db._conn) == self.CONFIGURED
            # WAL per-thread reader.
            rconn = db._get_read_conn()
            assert rconn is not None, "WAL reader expected on local filesystem"
            assert self._read(rconn) == self.CONFIGURED
        finally:
            db.close()

        # Read-only cross-profile attach.
        ro = SessionDB(db_path=db_path, read_only=True)
        try:
            assert self._read(ro._conn) == self.CONFIGURED
        finally:
            ro.close()

    def test_defaults_unchanged_without_config(self, tmp_path, monkeypatch):
        """No database: keys in config.yaml → SQLite defaults untouched."""
        from hermes_state import SessionDB

        defaults = self._sqlite_defaults(tmp_path)
        home = self._fresh_home(tmp_path, monkeypatch, config_text=None)
        db_path = home / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            assert self._read(db._conn) == defaults
            rconn = db._get_read_conn()
            if rconn is not None:
                assert self._read(rconn) == defaults
        finally:
            db.close()

        ro = SessionDB(db_path=db_path, read_only=True)
        try:
            assert self._read(ro._conn) == defaults
        finally:
            ro.close()


# --------------------------------------------------------------------------
# fork-only coverage (parity merge 2026-08-07)
# Kept verbatim from the fork blob: the other side's suite-wide
# prune removed these, but they guard fork-owned behavior.
# --------------------------------------------------------------------------


def test_find_session_by_origin_matching_rules(db):
    db.create_session(
        "gw-o1", "telegram", user_id="u1",
        session_key="agent:main:telegram:group:c9:u1", chat_id="c9", chat_type="group",
    )
    db.create_session(
        "gw-o2", "telegram", user_id="u2",
        session_key="agent:main:telegram:group:c9:u2", chat_id="c9", chat_type="group",
    )

    # Exact user match wins.
    assert db.find_session_by_origin(
        platform="telegram", chat_id="c9", user_id="u2"
    ) == "gw-o2"
    # Unknown user among multiple distinct users -> None (no contamination).
    assert db.find_session_by_origin(
        platform="telegram", chat_id="c9", user_id="u3"
    ) is None
    # No user given + multiple distinct users -> None.
    assert db.find_session_by_origin(platform="telegram", chat_id="c9") is None
    # Ended sessions are ignored: only gw-o1 remains as a live candidate.
    # A single remaining candidate is returned even without an exact user
    # match — mirrors the original sessions.json scan semantics.
    db.end_session("gw-o2", "session_reset")
    assert db.find_session_by_origin(
        platform="telegram", chat_id="c9", user_id="u2"
    ) == "gw-o1"
    # Single remaining candidate resolves without user_id.
    assert db.find_session_by_origin(platform="telegram", chat_id="c9") == "gw-o1"
    # Thread filter.
    db.create_session(
        "gw-th", "discord", user_id="u9",
        session_key="agent:main:discord:thread:t7", chat_id="ch7",
        chat_type="thread", thread_id="t7",
    )
    assert db.find_session_by_origin(
        platform="discord", chat_id="ch7", thread_id="t7"
    ) == "gw-th"
    assert db.find_session_by_origin(
        platform="discord", chat_id="ch7", thread_id="other"
    ) is None












def test_compression_fallback_streak_round_trips(db):
    db.create_session("s1", "cli")

    assert db.get_compression_fallback_streak("s1") == 0
    db.set_compression_fallback_streak("s1", 2)
    assert db.get_compression_fallback_streak("s1") == 2


def test_gateway_session_recovery_reopens_legacy_agent_close_rows(db):
    db.create_session(
        "closed-gw-session",
        "telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    db.append_message("closed-gw-session", "user", "hello")
    db.end_session("closed-gw-session", "agent_close")

    recovered = db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    assert recovered["id"] == "closed-gw-session"

    db.end_session("closed-gw-session", "session_reset")
    # First end reason wins, so force explicit reset state for this branch.
    db._conn.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
        (time.time(), "session_reset", "closed-gw-session"),
    )
    db._conn.commit()

    assert db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    ) is None


def test_messages_cursor_query_uses_session_id_index(db):
    db.create_session("s1", source="cli")
    db.append_message("s1", "user", "one")

    plan_rows = db._conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT * FROM messages
        WHERE session_id = ? AND id > ?
        ORDER BY id
        """,
        ("s1", 0),
    ).fetchall()
    plan = "\n".join(str(tuple(row)) for row in plan_rows)

    assert "USING INDEX idx_messages_session_id" in plan


