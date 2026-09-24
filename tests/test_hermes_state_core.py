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


class TestSessionLifecycle:
    def test_create_and_get_session(self, db):
        sid = db.create_session(
            session_id="s1",
            source="cli",
            model="test-model",
        )
        assert sid == "s1"

        session = db.get_session("s1")
        assert session is not None
        assert session["source"] == "cli"
        assert session["model"] == "test-model"
        assert session["ended_at"] is None


    def test_branch_resume_does_not_include_parent_messages_added_after_fork(self, db):
        """A branch owns its copied transcript, not the parent's later turns."""
        db.create_session("parent", source="tui")
        db.append_message("parent", role="user", content="before branch")
        db.append_message("parent", role="assistant", content="initial answer")

        db.create_session(
            "branch",
            source="tui",
            parent_session_id="parent",
            model_config={"_branched_from": "parent"},
        )
        db.append_message("branch", role="user", content="before branch")
        db.append_message("branch", role="assistant", content="initial answer")

        # The original conversation can be resumed after the fork. Those new
        # rows must not leak into the already-created branch's transcript.
        db.append_message("parent", role="user", content="after branch")
        db.append_message("parent", role="assistant", content="later answer")

        _, display_history = db.get_resume_conversations("branch")

        assert [message["content"] for message in display_history] == [
            "before branch",
            "initial answer",
        ]
        assert [
            message["content"]
            for message in db.get_messages_as_conversation("branch", include_ancestors=True)
        ] == ["before branch", "initial answer"]
        assert db.get_ancestor_display_prefix("branch") == []





    def test_update_session_cwd_persists_git_branch(self, db):
        db.create_session(session_id="s1", source="cli")
        db.update_session_cwd("s1", "/work/repo", git_branch="pets-feature")

        session = db.get_session("s1")
        assert session["cwd"] == "/work/repo"
        assert session["git_branch"] == "pets-feature"


















    def test_end_session_first_reason_wins_across_concurrent_connections(
        self, db
    ):
        """Concurrent finalizers perform one transition, not last-write-wins."""
        import threading

        db.create_session(session_id="s1", source="cron")
        db._conn.execute(
            "CREATE TABLE session_end_audit (reason TEXT NOT NULL)"
        )
        db._conn.execute(
            """
            CREATE TRIGGER audit_session_end
            AFTER UPDATE OF ended_at ON sessions
            WHEN OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL
            BEGIN
                INSERT INTO session_end_audit(reason) VALUES (NEW.end_reason);
            END
            """
        )

        peer = SessionDB(db_path=db.db_path)
        barrier = threading.Barrier(2)
        errors = []

        def _end(session_db, reason):
            try:
                barrier.wait(timeout=5)
                session_db.end_session("s1", reason)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_end, args=(db, "compression")),
            threading.Thread(target=_end, args=(peer, "cron_complete")),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            assert all(not thread.is_alive() for thread in threads)
            assert errors == []
            audit_rows = db._conn.execute(
                "SELECT reason FROM session_end_audit"
            ).fetchall()
            assert len(audit_rows) == 1
            assert db.get_session("s1")["end_reason"] == audit_rows[0]["reason"]
        finally:
            peer.close()











    def test_update_session_model_clears_browser_lock_and_preserves_lineage(self, db):
        """A later /model switch must replace, not compete with, a Browser lock."""
        db.create_session(
            session_id="s1",
            source="hermes_browser",
            model="x-ai/grok-4.5",
            model_config={
                "_branched_from": "parent-session",
                "browser_model_lock": {
                    "provider": "nous",
                    "model": "x-ai/grok-4.5",
                    "confirmed": True,
                },
            },
        )

        db.update_session_model("s1", "anthropic/claude-opus-4.8")

        session = db.get_session("s1")
        model_config = json.loads(session["model_config"])
        assert session["model"] == "anthropic/claude-opus-4.8"
        assert "browser_model_lock" not in model_config
        assert model_config["_branched_from"] == "parent-session"








    def test_first_accounted_route_replaces_all_route_fields_atomically(self, db):
        db.create_session(session_id="route", source="cli", model="primary")
        db.update_session_billing_route(
            "route", provider="primary-provider",
            base_url="https://primary.example/v1", billing_mode="api_key",
        )
        db.update_token_counts(
            "route", model="fallback", billing_provider="fallback-provider",
            billing_base_url=None, billing_mode=None, api_call_count=1,
        )
        row = db.get_session("route")
        assert row["model"] == "fallback"
        assert row["billing_provider"] == "fallback-provider"
        assert row["billing_base_url"] is None
        assert row["billing_mode"] is None












    def test_cjk_search_falls_back_to_like_when_trigram_unavailable(
        self, tmp_path, monkeypatch
    ):
        """Regression: long CJK queries must fall back to LIKE when trigram is missing."""
        real_connect = sqlite3.connect
        db_path = tmp_path / "state.db"

        def connect_without_trigram(*args, **kwargs):
            kwargs["factory"] = _NoTrigramConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_trigram)
        db = SessionDB(db_path=db_path)
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="大别山项目计划书")
            db.append_message("s1", role="user", content="长江大桥设计方案")

            # 3+ CJK chars would normally use trigram, but it's unavailable.
            # Must fall back to LIKE and still return results.
            results = db.search_messages("大别山")
            assert len(results) == 1
            # Note: search_messages strips 'content' from results; use 'snippet'.
            assert "content" not in results[0]
            assert "大别山" in results[0]["snippet"]
        finally:
            db.close()

    def test_absolute_update_does_not_record_per_model(self, db):
        """absolute=True overwrites the cumulative summary row (gateway path)
        and must NOT add per-model rows — those are accumulated from the
        per-call incremental path, so recording here would double-count.
        """
        db.create_session(session_id="s1", source="cli", model="gpt-4o")
        db.update_token_counts("s1", input_tokens=500, output_tokens=200,
                               model="gpt-4o", absolute=True)

        rows = db._conn.execute(
            "SELECT COUNT(*) AS n FROM session_model_usage WHERE session_id = 's1'"
        ).fetchone()
        assert rows["n"] == 0

    def test_accounted_primary_route_is_not_rewritten_by_later_fallback(self, db):
        """A mixed-provider session keeps its first accounted route in the legacy row."""
        db.create_session(session_id="s1", source="cli", model="gpt-5.6-sol")
        db.update_token_counts(
            "s1", input_tokens=10, output_tokens=5, model="gpt-5.6-sol",
            billing_provider="openai-codex", api_call_count=1,
        )
        db.update_token_counts(
            "s1", input_tokens=10, output_tokens=5, model="glm-5.2",
            billing_provider="custom:zai", api_call_count=1,
        )

        session = db.get_session("s1")
        assert session["model"] == "gpt-5.6-sol"
        assert session["billing_provider"] == "openai-codex"
        assert session["api_call_count"] == 2

    def test_backfill_repo_roots_fills_only_empty(self, db):
        db.create_session("s1", "cli", cwd="/repo/a")
        db.create_session("s2", "cli", cwd="/repo/b")
        db.update_session_cwd("s2", "/repo/b", git_repo_root="/already")

        db.backfill_repo_roots({"/repo/a": "/repo", "/repo/b": "/repo"})

        assert db.get_session("s1")["git_repo_root"] == "/repo"
        # Pre-existing root is preserved, not clobbered.
        assert db.get_session("s2")["git_repo_root"] == "/already"

    def test_base_fts_rebuilds_after_trigger_repair_without_trigram(
        self, tmp_path, monkeypatch
    ):
        """Trigger repair must rebuild base FTS even when trigram is unavailable."""
        db_path = tmp_path / "state.db"
        seeded = SessionDB(db_path=db_path)
        try:
            seeded.create_session(session_id="s1", source="cli")
            seeded.append_message("s1", role="user", content="already indexed")
            for trigger in (
                "messages_fts_insert",
                "messages_fts_delete",
                "messages_fts_update",
                "messages_fts_trigram_insert",
                "messages_fts_trigram_delete",
                "messages_fts_trigram_update",
            ):
                seeded._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            seeded._conn.commit()
            seeded.append_message("s1", role="assistant", content="repair only base needle")
        finally:
            seeded.close()

        real_connect = sqlite3.connect

        def connect_without_trigram(*args, **kwargs):
            kwargs["factory"] = _NoTrigramConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_trigram)
        restored = SessionDB(db_path=db_path)
        try:
            assert restored._fts_enabled is True
            assert restored._trigram_available is False
            assert restored._fts_table_exists("messages_fts") is True
            assert len(restored.search_messages("needle")) == 1
        finally:
            restored.close()

    def test_child_session_explicit_cwd_is_not_overwritten_by_parent(self, db):
        """A child that explicitly sets its own cwd/git_repo_root keeps it —
        parent inheritance only fills in NULLs, never clobbers."""
        db.create_session(session_id="parent", source="cli")
        db.update_session_cwd("parent", "/work/repo-a", git_repo_root="/work/repo-a")

        db.create_session(
            session_id="child", source="cli", parent_session_id="parent",
            cwd="/work/repo-b", git_repo_root="/work/repo-b",
        )

        child = db.get_session("child")
        assert child["cwd"] == "/work/repo-b"
        assert child["git_repo_root"] == "/work/repo-b"

    def test_child_session_inherits_cwd_and_git_repo_root_from_parent(self, db):
        """A parent_session_id child born without cwd/git_repo_root (e.g. the
        compression-fork path) must inherit both from its parent, so it
        doesn't silently drop out of the project sidebar (#64709)."""
        db.create_session(session_id="parent", source="cli")
        db.update_session_cwd("parent", "/work/repo", git_repo_root="/work/repo")

        db.create_session(session_id="child", source="cli", parent_session_id="parent")

        child = db.get_session("child")
        assert child["cwd"] == "/work/repo"
        assert child["git_repo_root"] == "/work/repo"

    def test_child_session_inherits_git_branch_from_parent(self, db):
        """git_branch travels with cwd/git_repo_root — the Desktop sidebar
        shows the branch chip per session, so a compression child born
        without it loses the chip even though the workspace didn't change."""
        db.create_session(session_id="parent", source="cli")
        db.update_session_cwd(
            "parent", "/work/repo", git_branch="feature-x", git_repo_root="/work/repo"
        )

        db.create_session(session_id="child", source="cli", parent_session_id="parent")

        child = db.get_session("child")
        assert child["git_branch"] == "feature-x"

    def test_compression_child_inherits_gateway_origin_columns(self, db):
        """A compression fork's child inherits gateway routing metadata
        (session_key/chat_id/...) from the ended parent, so a crash before
        the gateway re-records the peer can't strand it (#59527)."""
        db.create_session(
            session_id="parent", source="telegram",
            user_id="u1", session_key="telegram:u1:c1",
            chat_id="c1", chat_type="private", thread_id="t1",
        )
        db.record_gateway_session_peer(
            "parent", source="telegram", user_id="u1",
            session_key="telegram:u1:c1", chat_id="c1", chat_type="private",
            thread_id="t1", display_name="Chat One", origin_json='{"p":"telegram"}',
        )
        # Rotation path: parent is ended with 'compression' BEFORE the child
        # row is created (agent/conversation_compression.py).
        db.end_session("parent", "compression")

        db.create_session(
            session_id="child", source="telegram", parent_session_id="parent"
        )

        child = db.get_session("child")
        assert child["user_id"] == "u1"
        assert child["session_key"] == "telegram:u1:c1"
        assert child["chat_id"] == "c1"
        assert child["chat_type"] == "private"
        assert child["thread_id"] == "t1"
        assert child["display_name"] == "Chat One"
        assert child["origin_json"] == '{"p":"telegram"}'

    def test_create_session_does_not_overwrite_existing_metadata(self, db):
        """A later bare write (source='unknown', model=...) must not overwrite
        a model/source an earlier writer already set."""
        db.create_session("s1", source="cli", model="real-model")
        db.create_session("s1", source="unknown", model="should-not-win")
        session = db.get_session("s1")
        assert session["model"] == "real-model"
        assert session["source"] == "cli"

    def test_create_session_enriches_null_metadata_on_conflict(self, db):
        """Gateway creates a bare row first; the agent's later create_session
        must backfill model/model_config/system_prompt without clobbering the
        gateway's source/user_id/chat_id. Regression for NULL gateway metadata
        (sessions with NULL billing_provider/model)."""
        # Gateway bare row (source + user_id only), before the agent exists.
        db.create_session("s1", source="telegram", user_id="u1", chat_id="c1")
        bare = db.get_session("s1")
        assert bare["model"] is None
        # Agent enriches — passes source="cli" but real metadata.
        db.create_session(
            "s1", source="cli", model="claude-opus-4-6",
            model_config={"max_iterations": 90}, system_prompt="SYS",
        )
        enriched = db.get_session("s1")
        assert enriched["model"] == "claude-opus-4-6"
        assert enriched["system_prompt"] == "SYS"
        # Gateway-owned fields preserved (NOT clobbered by source="cli").
        assert enriched["source"] == "telegram"
        assert enriched["user_id"] == "u1"
        assert enriched["chat_id"] == "c1"

    def test_db_initializes_without_fts5_module(self, tmp_path, monkeypatch):
        real_connect = sqlite3.connect

        def connect_without_fts(*args, **kwargs):
            kwargs["factory"] = _NoFtsConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_fts)

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            assert db._fts_enabled is False
            # Neither FTS5 virtual table should have been created on a build
            # that lacks the fts5 module — both init paths must degrade.
            assert db._fts_table_exists("messages_fts") is False
            assert db._fts_table_exists("messages_fts_trigram") is False

            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="hello from sqlite without fts")

            messages = db.get_messages("s1")
            assert len(messages) == 1
            assert messages[0]["content"] == "hello from sqlite without fts"
            assert db.search_messages("hello") == []
        finally:
            db.close()

    def test_db_initializes_without_trigram_tokenizer(self, tmp_path, monkeypatch):
        """SessionDB must not crash when FTS5 exists but trigram tokenizer is missing."""
        real_connect = sqlite3.connect

        def connect_without_trigram(*args, **kwargs):
            kwargs["factory"] = _NoTrigramConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_trigram)

        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            # Base FTS5 should still work (trigram is optional).
            assert db._fts_enabled is True
            assert db._fts_table_exists("messages_fts") is True
            # Trigram table should NOT have been created.
            assert db._fts_table_exists("messages_fts_trigram") is False

            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="hello without trigram")

            messages = db.get_messages("s1")
            assert len(messages) == 1
            assert messages[0]["content"] == "hello without trigram"

            # FTS5 keyword search should still work.
            assert len(db.search_messages("hello")) == 1
        finally:
            db.close()

    def test_distinct_session_cwds_aggregates_history(self, db):
        db.create_session("s1", "cli", cwd="/repo")
        db.create_session("s2", "cli", cwd="/repo")
        db.create_session("s3", "cli", cwd="/other")
        db.create_session("s4", "cli")  # no cwd — excluded

        rows = {r["cwd"]: r["sessions"] for r in db.distinct_session_cwds()}
        assert rows == {"/repo": 2, "/other": 1}

    def test_end_session(self, db):
        db.create_session(session_id="s1", source="cli")
        db.end_session("s1", end_reason="user_exit")

        session = db.get_session("s1")
        assert isinstance(session["ended_at"], float)
        assert session["end_reason"] == "user_exit"

    def test_end_session_after_reopen_allows_re_end(self, db):
        """reopen_session() is the explicit escape hatch for re-ending a
        closed session. After reopen, end_session() takes effect again.
        """
        db.create_session(session_id="s1", source="cli")
        db.end_session("s1", end_reason="compression")
        db.reopen_session("s1")
        db.end_session("s1", end_reason="user_exit")

        session = db.get_session("s1")
        assert session["end_reason"] == "user_exit"

    def test_end_session_preserves_original_end_reason(self, db):
        """The first end_reason wins — compression splits must not be
        overwritten when a later stale ``end_session()`` call lands on the
        same row (e.g. from a CLI session_id that desynced after compression
        and then tried to /resume another session).
        """
        db.create_session(session_id="s1", source="cli")
        db.end_session("s1", end_reason="compression")
        first_ended_at = db.get_session("s1")["ended_at"]

        # Simulate a stale CLI holding the old session_id and calling
        # end_session() again with a different reason.
        time.sleep(0.01)
        db.end_session("s1", end_reason="resumed_other")

        session = db.get_session("s1")
        assert session["end_reason"] == "compression"
        assert session["ended_at"] == first_ended_at

    def test_existing_fts_tables_do_not_break_without_fts5(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "state.db"
        seeded = SessionDB(db_path=db_path)
        try:
            seeded.create_session(session_id="s1", source="cli")
            seeded.append_message("s1", role="user", content="before runtime change")
        finally:
            seeded.close()

        real_connect = sqlite3.connect

        def connect_without_fts(*args, **kwargs):
            kwargs["factory"] = _NoFtsExistingTableConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_fts)

        db = SessionDB(db_path=db_path)
        try:
            assert db._fts_enabled is False
            assert db.get_session("s1") is not None
            assert len(db.get_messages("s1")) == 1

            # Existing FTS triggers must be disabled too; otherwise this write
            # would try to insert into an unusable FTS virtual table.
            db.append_message("s1", role="assistant", content="after runtime change")
            messages = db.get_messages("s1")
            assert len(messages) == 2
            assert messages[1]["content"] == "after runtime change"
        finally:
            db.close()

    def test_first_accounted_fallback_replaces_requested_primary_route(self, db):
        """First successful fallback usage must persist one coherent route pair."""
        db.create_session(session_id="s1", source="cli", model="gpt-5.6-sol")

        db.update_token_counts(
            "s1",
            input_tokens=10,
            output_tokens=5,
            model="glm-5.2",
            billing_provider="custom:zai",
            billing_base_url="https://api.z.ai/api/coding/paas/v4/",
            api_call_count=1,
        )

        session = db.get_session("s1")
        assert session["model"] == "glm-5.2"
        assert session["billing_provider"] == "custom:zai"
        assert session["billing_base_url"] == "https://api.z.ai/api/coding/paas/v4/"
        assert session["api_call_count"] == 1

    def test_fts_runtime_restores_triggers_after_no_fts_open(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "state.db"
        seeded = SessionDB(db_path=db_path)
        try:
            seeded.create_session(session_id="s1", source="cli")
            seeded.append_message("s1", role="user", content="first searchable")
        finally:
            seeded.close()

        real_connect = sqlite3.connect

        def connect_without_fts(*args, **kwargs):
            kwargs["factory"] = _NoFtsExistingTableConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_fts)
        no_fts = SessionDB(db_path=db_path)
        try:
            no_fts.append_message("s1", role="assistant", content="not indexed yet")
        finally:
            no_fts.close()

        monkeypatch.setattr("hermes_state.sqlite3.connect", real_connect)
        restored = SessionDB(db_path=db_path)
        try:
            assert restored._fts_enabled is True
            restored.append_message("s1", role="assistant", content="indexed again")
            assert len(restored.search_messages("not indexed yet")) == 1
            assert len(restored.search_messages("indexed")) == 2
        finally:
            restored.close()

    def test_get_nonexistent_session(self, db):
        assert db.get_session("nonexistent") is None

    def test_is_fts5_unavailable_error_catches_trigram_tokenizer(self):
        """Unit test: _is_fts5_unavailable_error matches 'no such tokenizer: trigram'."""
        fts5_err = sqlite3.OperationalError("no such module: fts5")
        trigram_err = sqlite3.OperationalError("no such tokenizer: trigram")
        generic_tokenizer_err = sqlite3.OperationalError("no such tokenizer: foo")
        unrelated_err = sqlite3.OperationalError("no such table: foo")

        assert SessionDB._is_fts5_unavailable_error(fts5_err) is True
        assert SessionDB._is_fts5_unavailable_error(trigram_err) is True
        # Generic tokenizer errors should NOT match — only trigram.
        assert SessionDB._is_fts5_unavailable_error(generic_tokenizer_err) is False
        assert SessionDB._is_fts5_unavailable_error(unrelated_err) is False

    def test_is_trigram_unavailable_error(self):
        """Unit test: _is_trigram_unavailable_error is scoped to trigram."""
        trigram_err = sqlite3.OperationalError("no such tokenizer: trigram")
        generic_err = sqlite3.OperationalError("no such tokenizer: foo")
        fts5_err = sqlite3.OperationalError("no such module: fts5")

        assert SessionDB._is_trigram_unavailable_error(trigram_err) is True
        assert SessionDB._is_trigram_unavailable_error(generic_err) is False
        assert SessionDB._is_trigram_unavailable_error(fts5_err) is False

    def test_live_parent_child_does_not_inherit_gateway_origin(self, db):
        """Delegate/subagent children (parent still live) must NOT inherit
        routing keys — peer recovery could otherwise repoint gateway traffic
        into a subagent's session."""
        db.create_session(
            session_id="parent", source="telegram",
            user_id="u1", session_key="telegram:u1:c1",
            chat_id="c1", chat_type="private",
        )

        db.create_session(
            session_id="sub", source="telegram", parent_session_id="parent"
        )

        sub = db.get_session("sub")
        assert sub["session_key"] is None
        assert sub["chat_id"] is None
        assert sub["user_id"] is None
        # Workspace metadata still inherits — that part is safe for any child.
        db.update_session_cwd("parent", "/work/repo", git_repo_root="/work/repo")
        db.create_session(
            session_id="sub2", source="telegram", parent_session_id="parent"
        )
        assert db.get_session("sub2")["cwd"] == "/work/repo"

    def test_metadata_only_update_does_not_replace_requested_route(self, db):
        db.create_session(session_id="metadata", source="cli", model="primary")
        db.update_token_counts(
            "metadata", model="fallback", billing_provider="fallback-provider",
            api_call_count=0,
        )
        row = db.get_session("metadata")
        assert row["model"] == "primary"
        assert row["billing_provider"] is None

    def test_mid_session_switch_splits_per_model_usage(self, db):
        """The headline #51607 case: tokens after a /model switch are
        attributed to the new model, not the session's initial model.

        The ``sessions`` summary row still holds combined totals + the latest
        model, but session_model_usage keeps an accurate per-model split.
        """
        db.create_session(session_id="s1", source="cli",
                          model="deepseek/deepseek-v4-pro")
        # Pre-switch calls on deepseek.
        db.update_token_counts("s1", input_tokens=40_000, output_tokens=8_000,
                               model="deepseek/deepseek-v4-pro",
                               billing_provider="deepseek", api_call_count=2)
        # User runs /model — the gateway persists the new model …
        db.update_session_model("s1", "anthropic/claude-opus-4.8")
        # … and subsequent per-call deltas carry the new model/provider.
        db.update_token_counts("s1", input_tokens=50_000, output_tokens=4_000,
                               model="anthropic/claude-opus-4.8",
                               billing_provider="openrouter", api_call_count=3)

        rows = {
            r["model"]: r
            for r in db._conn.execute(
                "SELECT model, billing_provider, input_tokens, output_tokens, "
                "api_call_count FROM session_model_usage WHERE session_id = 's1'"
            ).fetchall()
        }
        assert set(rows) == {"deepseek/deepseek-v4-pro",
                             "anthropic/claude-opus-4.8"}
        assert rows["deepseek/deepseek-v4-pro"]["input_tokens"] == 40_000
        assert rows["deepseek/deepseek-v4-pro"]["api_call_count"] == 2
        assert rows["anthropic/claude-opus-4.8"]["input_tokens"] == 50_000
        assert rows["anthropic/claude-opus-4.8"]["billing_provider"] == "openrouter"
        assert rows["anthropic/claude-opus-4.8"]["api_call_count"] == 3

        # Summary row: latest model + combined totals (unchanged behaviour).
        session = db.get_session("s1")
        assert session["model"] == "anthropic/claude-opus-4.8"
        assert session["input_tokens"] == 90_000
        assert session["output_tokens"] == 12_000

    def test_multi_generation_lineage_inherits_cwd(self, db):
        """cwd/git_repo_root propagate through a multi-hop compression chain
        (root -> gen1 -> gen2), mirroring the multi-generation lineage from
        the reported issue where a single conversation forked repeatedly."""
        db.create_session(session_id="root", source="cli")
        db.update_session_cwd("root", "/work/repo", git_repo_root="/work/repo")

        db.create_session(session_id="gen1", source="cli", parent_session_id="root")
        db.create_session(session_id="gen2", source="cli", parent_session_id="gen1")

        assert db.get_session("gen1")["cwd"] == "/work/repo"
        assert db.get_session("gen2")["cwd"] == "/work/repo"

    def test_old_schema_without_fts5_does_not_crash(self, tmp_path, monkeypatch):
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (9,))
        conn.commit()
        conn.close()

        real_connect = sqlite3.connect

        def connect_without_fts(*args, **kwargs):
            kwargs["factory"] = _NoFtsConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_fts)

        db = SessionDB(db_path=db_path)
        try:
            assert db._fts_enabled is False
            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="legacy no fts")
            assert db.get_messages("s1")[0]["content"] == "legacy no fts"
            assert db.search_messages("legacy") == []

            # Leave the FTS migration version in place so a future FTS-capable
            # runtime can still rebuild and backfill the indexes.
            row = db._conn.execute("SELECT version FROM schema_version").fetchone()
            assert row["version"] == 9
        finally:
            db.close()

    def test_parent_session(self, db):
        db.create_session(session_id="parent", source="cli")
        db.create_session(session_id="child", source="cli", parent_session_id="parent")

        child = db.get_session("child")
        assert child["parent_session_id"] == "parent"

    def test_per_model_usage_falls_back_to_session_model(self, db):
        """When a call omits the model, attribute it to the session's
        recorded model — matches the COALESCE-from-session summary behaviour
        and keeps existing callers (which pass no model) working.
        """
        db.create_session(session_id="s1", source="cli",
                          model="gpt-4o", )
        db.update_token_counts("s1", input_tokens=10, output_tokens=5)

        rows = db._conn.execute(
            "SELECT model FROM session_model_usage WHERE session_id = 's1'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["model"] == "gpt-4o"

    def test_per_model_usage_keeps_distinct_billing_routes(self, db):
        """The same model through distinct billing routes must not collapse."""
        db.create_session(session_id="routes", source="cli", model="shared-model")
        db.update_token_counts(
            "routes", input_tokens=10, model="shared-model",
            billing_provider="custom", billing_base_url="https://one.example/v1",
            billing_mode="api_key", estimated_cost_usd=0.01, api_call_count=1,
        )
        db.update_token_counts(
            "routes", input_tokens=20, model="shared-model",
            billing_provider="custom", billing_base_url="https://two.example/v1",
            billing_mode="subscription_included", estimated_cost_usd=0.0,
            cost_status="included", api_call_count=1,
        )

        rows = db._conn.execute(
            "SELECT billing_base_url, billing_mode, input_tokens "
            "FROM session_model_usage WHERE session_id = 'routes' "
            "ORDER BY billing_base_url"
        ).fetchall()
        assert [(r["billing_base_url"], r["billing_mode"], r["input_tokens"])
                for r in rows] == [
            ("https://one.example/v1", "api_key", 10),
            ("https://two.example/v1", "subscription_included", 20),
        ]

    def test_per_model_usage_recorded_for_single_model(self, db):
        """Each per-call delta lands in session_model_usage (#51607)."""
        db.create_session(session_id="s1", source="cli")
        db.update_token_counts("s1", input_tokens=200, output_tokens=100,
                               model="anthropic/claude-opus-4.8",
                               billing_provider="anthropic", api_call_count=1)
        db.update_token_counts("s1", input_tokens=100, output_tokens=50,
                               model="anthropic/claude-opus-4.8",
                               billing_provider="anthropic", api_call_count=1)

        rows = db._conn.execute(
            "SELECT model, billing_provider, api_call_count, input_tokens, "
            "output_tokens FROM session_model_usage WHERE session_id = 's1'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["model"] == "anthropic/claude-opus-4.8"
        assert row["billing_provider"] == "anthropic"
        assert row["api_call_count"] == 2
        assert row["input_tokens"] == 300
        assert row["output_tokens"] == 150

    def test_trigram_config_default_is_enabled(self, tmp_path):
        """With no config override, the trigram index builds as before."""
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            assert db._fts_table_exists("messages_fts_trigram") is True
            assert db._trigram_available is True
        finally:
            db.close()

    def test_trigram_config_disabled_drops_existing_index(
        self, tmp_path, monkeypatch
    ):
        """Flipping trigram_fts to false drops an existing trigram index on reopen."""
        db_path = tmp_path / "state.db"
        # Phase 1: default config — trigram index built and populated.
        seeded = SessionDB(db_path=db_path)
        try:
            seeded.create_session(session_id="s1", source="cli")
            seeded.append_message("s1", role="user", content="alpha beta gamma")
            assert seeded._fts_table_exists("messages_fts_trigram") is True
            assert seeded._trigram_available is True
        finally:
            seeded.close()

        # Phase 2: reopen with the config gate off.
        monkeypatch.setattr(
            "hermes_state_schema._trigram_fts_config_enabled", lambda: False
        )
        reopened = SessionDB(db_path=db_path)
        try:
            assert reopened._fts_table_exists("messages_fts_trigram") is False
            assert reopened._trigram_available is False
            # Existing content still searchable via base FTS; writes still work.
            assert len(reopened.search_messages("alpha")) == 1
            reopened.append_message("s1", role="assistant", content="delta epsilon")
            assert len(reopened.search_messages("delta")) == 1
        finally:
            reopened.close()

    def test_trigram_config_disabled_skips_creation(self, tmp_path, monkeypatch):
        """session_store.trigram_fts=false must skip trigram table creation."""
        monkeypatch.setattr(
            "hermes_state_schema._trigram_fts_config_enabled", lambda: False
        )
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            # Base FTS5 fully functional.
            assert db._fts_enabled is True
            assert db._fts_table_exists("messages_fts") is True
            # Trigram table not created; CJK search routes to LIKE fallback.
            assert db._fts_table_exists("messages_fts_trigram") is False
            assert db._trigram_available is False

            db.create_session(session_id="s1", source="cli")
            db.append_message("s1", role="user", content="hello gated world")
            # Message writes work (no dangling trigram triggers) and base
            # keyword search still finds them.
            assert len(db.search_messages("gated")) == 1
            # No trigram triggers were installed.
            trigger_names = {
                row[0]
                for row in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            assert not any("trigram" in name for name in trigger_names)
        finally:
            db.close()

    def test_trigram_config_reenabled_rebuilds_and_backfills(
        self, tmp_path, monkeypatch
    ):
        """Re-enabling trigram_fts after a disabled period rebuilds + backfills."""
        db_path = tmp_path / "state.db"
        monkeypatch.setattr(
            "hermes_state_schema._trigram_fts_config_enabled", lambda: False
        )
        gated = SessionDB(db_path=db_path)
        try:
            gated.create_session(session_id="s1", source="cli")
            gated.append_message("s1", role="user", content="written while gated")
        finally:
            gated.close()

        # Reopen with the gate back on (default) — trigger repair recreates
        # the trigram table and backfills the rows written while it was off.
        monkeypatch.setattr(
            "hermes_state_schema._trigram_fts_config_enabled", lambda: True
        )
        restored = SessionDB(db_path=db_path)
        try:
            assert restored._fts_table_exists("messages_fts_trigram") is True
            assert restored._trigram_available is True
            count = restored._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_trigram"
            ).fetchone()[0]
            assert count == 1
        finally:
            restored.close()

    def test_trigram_fts_config_enabled_fail_open(self, monkeypatch):
        """A broken/absent config layer must fail open (trigram stays enabled)."""
        import hermes_state as hs

        def boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly", boom, raising=False
        )
        assert hs._trigram_fts_config_enabled() is True

    def test_update_session_billing_route_overwrites_after_switch(self, db):
        """A mid-session provider switch must overwrite the billing route.

        update_token_counts writes billing fields with
        COALESCE(billing_provider, ?) (first-writer-wins), so after a
        provider switch the dashboard kept attributing cost to the original
        provider (#48248). update_session_billing_route sets them
        unconditionally. The prompt remains until the next turn checks its
        runtime identity and rebuilds only if Model:/Provider: changed (#48173).
        """
        db.create_session(session_id="s1", source="telegram")
        # First token update seeds the billing route.
        db.update_token_counts("s1", input_tokens=10, output_tokens=5,
                               billing_provider="openrouter",
                               billing_base_url="https://openrouter.ai/api/v1",
                               billing_mode="api_key")
        sess = db.get_session("s1")
        assert sess["billing_provider"] == "openrouter"
        # A later token update never changes it (COALESCE first-writer-wins).
        db.update_token_counts("s1", input_tokens=10, output_tokens=5,
                               billing_provider="ollama",
                               billing_base_url="http://localhost:11434/v1",
                               billing_mode="local")
        assert db.get_session("s1")["billing_provider"] == "openrouter"

        # Seed a stale prompt snapshot, then switch the billing route.
        db.update_system_prompt("s1", "Model: x/old\nProvider: openrouter")
        assert db.get_session("s1")["system_prompt"] is not None
        db.update_session_billing_route(
            "s1", provider="ollama",
            base_url="http://localhost:11434/v1", billing_mode="local",
        )
        sess = db.get_session("s1")
        assert sess["billing_provider"] == "ollama"
        assert sess["billing_base_url"] == "http://localhost:11434/v1"
        assert sess["billing_mode"] == "local"
        assert sess["system_prompt"] == "Model: x/old\nProvider: openrouter"

        # billing_mode defaults to COALESCE — omitting it preserves the value.
        db.update_session_billing_route(
            "s1", provider="openai",
            base_url="https://api.openai.com/v1",
        )
        sess = db.get_session("s1")
        assert sess["billing_provider"] == "openai"
        assert sess["billing_mode"] == "local"  # preserved (COALESCE on None)

    def test_update_session_cwd_empty_branch_does_not_clobber(self, db):
        """A failed branch probe (empty string) must not wipe a branch we
        already captured — only the cwd updates."""
        db.create_session(session_id="s1", source="cli")
        db.update_session_cwd("s1", "/work/repo", git_branch="main")
        db.update_session_cwd("s1", "/work/repo", git_branch="")

        session = db.get_session("s1")
        assert session["git_branch"] == "main"

    def test_update_session_cwd_empty_repo_root_does_not_clobber(self, db):
        db.create_session(session_id="s1", source="cli")
        db.update_session_cwd("s1", "/work/repo", git_repo_root="/work/repo")
        db.update_session_cwd("s1", "/work/repo", git_repo_root="")

        assert db.get_session("s1")["git_repo_root"] == "/work/repo"

    def test_update_session_cwd_persists_git_repo_root(self, db):
        db.create_session(session_id="s1", source="cli")
        db.update_session_cwd("s1", "/work/repo/src", git_repo_root="/work/repo")

        assert db.get_session("s1")["git_repo_root"] == "/work/repo"

    def test_update_session_cwd_without_branch_arg(self, db):
        """Back-compat: callers that pass only (id, cwd) still work."""
        db.create_session(session_id="s1", source="cli")
        db.update_session_cwd("s1", "/work/repo")

        session = db.get_session("s1")
        assert session["cwd"] == "/work/repo"
        assert session["git_branch"] is None

    def test_update_session_model_overwrites_existing(self, db):
        """A mid-session /model switch must overwrite the stored model.

        update_token_counts uses COALESCE(model, ?) (first-writer-wins), so
        the dashboard kept showing the original model after a switch (#34850).
        update_session_model sets the column unconditionally.
        """
        db.create_session(session_id="s1", source="telegram",
                          model="xiaomi/mimo-v2.5-pro")
        # Token updates never change the model once set.
        db.update_token_counts("s1", input_tokens=10, output_tokens=5,
                               model="xiaomi/mimo-v2.5-pro")
        assert db.get_session("s1")["model"] == "xiaomi/mimo-v2.5-pro"

        # Explicit switch overwrites it.
        db.update_session_model("s1", "xiaomi/mimo-v2.5")
        assert db.get_session("s1")["model"] == "xiaomi/mimo-v2.5"

        # And a subsequent token update does NOT revert it (COALESCE no-ops
        # because the column is now non-NULL).
        db.update_token_counts("s1", input_tokens=10, output_tokens=5,
                               model="xiaomi/mimo-v2.5-pro")
        assert db.get_session("s1")["model"] == "xiaomi/mimo-v2.5"

    def test_update_system_prompt(self, db):
        db.create_session(session_id="s1", source="cli")
        db.update_system_prompt("s1", "You are a helpful assistant.")

        session = db.get_session("s1")
        assert session["system_prompt"] == "You are a helpful assistant."

    def test_update_token_counts(self, db):
        db.create_session(session_id="s1", source="cli")
        db.update_token_counts("s1", input_tokens=200, output_tokens=100)
        db.update_token_counts("s1", input_tokens=100, output_tokens=50)

        session = db.get_session("s1")
        assert session["input_tokens"] == 300
        assert session["output_tokens"] == 150

    def test_update_token_counts_api_call_count_absolute(self, db):
        """absolute mode sets api_call_count directly."""
        db.create_session(session_id="s1", source="cli")
        db.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)
        db.update_token_counts("s1", input_tokens=300, output_tokens=150,
                               api_call_count=5, absolute=True)

        session = db.get_session("s1")
        assert session["api_call_count"] == 5
        assert session["input_tokens"] == 300

    def test_update_token_counts_backfills_model_when_null(self, db):
        db.create_session(session_id="s1", source="telegram")
        db.update_token_counts("s1", input_tokens=10, output_tokens=5, model="openai/gpt-5.4")

        session = db.get_session("s1")
        assert session["model"] == "openai/gpt-5.4"

    def test_update_token_counts_preserves_existing_model(self, db):
        db.create_session(session_id="s1", source="cli", model="anthropic/claude-opus-4.6")
        db.update_token_counts("s1", input_tokens=10, output_tokens=5, model="openai/gpt-5.4")

        session = db.get_session("s1")
        assert session["model"] == "anthropic/claude-opus-4.6"

    def test_update_token_counts_tracks_api_call_count(self, db):
        """api_call_count increments with each update_token_counts call."""
        db.create_session(session_id="s1", source="cli")
        db.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)
        db.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)
        db.update_token_counts("s1", input_tokens=100, output_tokens=50, api_call_count=1)

        session = db.get_session("s1")
        assert session["api_call_count"] == 3

    def test_v11_migration_backfills_base_fts_when_trigram_unavailable(
        self, tmp_path, monkeypatch
    ):
        """A legacy inline-FTS DB opened under a no-trigram runtime keeps its
        base FTS searchable (and is flagged optimizable) without crashing.

        Opt-in model: opening never auto-migrates. The legacy single-column
        index keeps working for content search; the trigram tokenizer being
        unavailable must not break base FTS or the open itself.
        """
        real_connect = sqlite3.connect
        db_path = tmp_path / "state.db"

        # Phase 1: build a genuine legacy inline DB by hand (single-column
        # messages_fts, no trigram table), at an old schema version.
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.executescript("""
            DROP TABLE IF EXISTS messages_fts;
            DROP TABLE IF EXISTS messages_fts_trigram;
            DROP VIEW IF EXISTS messages_fts_trigram_src;
            CREATE VIRTUAL TABLE messages_fts USING fts5(content);
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, COALESCE(new.content,''));
            END;
        """)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (10)")
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', ?)",
            (time.time(),),
        )
        for role, content in (
            ("user", "legacy message alpha"),
            ("assistant", "legacy reply beta"),
        ):
            conn.execute(
                "INSERT INTO messages (session_id, timestamp, role, content) "
                "VALUES ('s1', ?, ?, ?)",
                (time.time(), role, content),
            )
        conn.commit()
        conn.close()

        # Phase 2: reopen with trigram disabled — must NOT crash, base FTS
        # keeps working, and the DB is flagged optimizable (opt-in, so no
        # auto-migration and the version stays put).
        def connect_without_trigram(*args, **kwargs):
            kwargs["factory"] = _NoTrigramConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_without_trigram)
        migrated_db = SessionDB(db_path=db_path)
        try:
            assert migrated_db._fts_enabled is True
            assert migrated_db._trigram_available is False
            assert migrated_db._fts_table_exists("messages_fts") is True
            assert migrated_db._fts_table_exists("messages_fts_trigram") is False
            assert migrated_db.fts_optimize_available() is True

            # Existing messages must be searchable via base FTS.
            results = migrated_db.search_messages("legacy message")
            assert len(results) == 1
            # snippet has FTS5 highlight markers (>>>...<<<); check raw content via get_messages
            msgs = migrated_db.get_messages("s1")
            assert any("legacy message" in m["content"] for m in msgs)
        finally:
            migrated_db.close()

    def test_v17_backfill_seeds_existing_session_usage(self, tmp_path):
        """A DB upgraded from <17 seeds one usage row per historical session
        from its aggregate totals, so insights read uniformly from the table.
        """
        db_path = tmp_path / "legacy.db"
        db = SessionDB(db_path=db_path)
        db.create_session(session_id="legacy1", source="cli", model="gpt-4o")
        db.update_token_counts("legacy1", input_tokens=1234, output_tokens=567,
                               model="gpt-4o", billing_provider="openai")
        # Simulate a pre-v17 database: drop the per-model rows and roll the
        # recorded schema version back so the backfill migration re-runs.
        db._conn.execute("DELETE FROM session_model_usage")
        db._conn.execute("UPDATE schema_version SET version = 16")
        db._conn.commit()
        db.close()

        # Reopen — _init_schema should backfill from the sessions aggregate.
        db2 = SessionDB(db_path=db_path)
        try:
            rows = db2._conn.execute(
                "SELECT model, billing_provider, input_tokens, output_tokens "
                "FROM session_model_usage WHERE session_id = 'legacy1'"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["model"] == "gpt-4o"
            assert rows[0]["billing_provider"] == "openai"
            assert rows[0]["input_tokens"] == 1234
            assert rows[0]["output_tokens"] == 567
        finally:
            db2.close()


# =========================================================================
# Message storage
# =========================================================================

class TestSchemaInit:
    def test_wal_mode(self, db):
        """Prefer WAL on fixed SQLite; DELETE on WAL-reset-vulnerable builds (#69784)."""
        from hermes_state import is_sqlite_wal_reset_vulnerable

        cursor = db._conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0].lower()
        if is_sqlite_wal_reset_vulnerable():
            assert mode == "delete"
        else:
            assert mode == "wal"







    def test_telegram_topic_binding_roundtrip_requires_explicit_schema(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )

        assert db.get_telegram_topic_binding(chat_id="208214988", thread_id="17585") is None

        db.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="telegram:dm:208214988:thread:17585",
            session_id="topic-session",
        )

        binding = db.get_telegram_topic_binding(chat_id="208214988", thread_id="17585")
        assert binding is not None
        assert binding["chat_id"] == "208214988"
        assert binding["thread_id"] == "17585"
        assert binding["user_id"] == "208214988"
        assert binding["session_key"] == "telegram:dm:208214988:thread:17585"
        assert binding["session_id"] == "topic-session"
        assert db.get_meta("telegram_dm_topic_schema_version") == "2"
        db.close()







    def test_schema_sql_is_source_of_truth(self, db):
        """Every column in SCHEMA_SQL exists in the live database.

        This is the architectural invariant: SCHEMA_SQL declares the
        desired schema, _reconcile_columns ensures it matches reality.
        """
        from hermes_state import SCHEMA_SQL

        expected = SessionDB._parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            live_cols = {
                r[1]
                for r in db._conn.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }
            for col_name in declared_cols:
                assert col_name in live_cols, (
                    f"Column {col_name} declared in SCHEMA_SQL for {table_name} "
                    f"but missing from live DB. Live columns: {live_cols}"
                )

    def test_apply_telegram_topic_migration_creates_topic_tables_explicitly(self, tmp_path):
        """The /topic opt-in path owns the DB migration for Telegram topic mode."""
        old_db = tmp_path / "old.db"
        import sqlite3

        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT
            );
            """
        )
        conn.close()

        db = SessionDB(db_path=old_db)
        db.apply_telegram_topic_migration()

        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "telegram_dm_topic_mode" in tables
        assert "telegram_dm_topic_bindings" in tables
        assert db.get_meta("telegram_dm_topic_schema_version") == "2"
        db.close()

    def test_foreign_keys_enabled(self, db):
        cursor = db._conn.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1

    def test_list_unlinked_telegram_sessions_for_user_excludes_bound_and_other_users(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            session_id="old-unlinked",
            source="telegram",
            user_id="208214988",
        )
        db.set_session_title("old-unlinked", "Old research")
        db.append_message("old-unlinked", "user", "first prompt")
        db.create_session(
            session_id="already-linked",
            source="telegram",
            user_id="208214988",
        )
        db.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="already-linked",
        )
        db.create_session(
            session_id="other-user",
            source="telegram",
            user_id="someone-else",
        )

        sessions = db.list_unlinked_telegram_sessions_for_user(
            chat_id="208214988",
            user_id="208214988",
        )

        assert [s["id"] for s in sessions] == ["old-unlinked"]
        assert sessions[0]["title"] == "Old research"
        assert sessions[0]["preview"] == "first prompt"
        db.close()

    def test_migration_from_v2(self, tmp_path):
        """Simulate a v2 database and verify migration adds title column."""
        import sqlite3

        db_path = tmp_path / "migrate_test.db"
        conn = sqlite3.connect(str(db_path))
        # Create v2 schema (without title column)
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (2);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT
            );
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("existing", "cli", 1000.0),
        )
        conn.commit()
        conn.close()

        # Open with SessionDB — should migrate to v9
        migrated_db = SessionDB(db_path=db_path)

        # Verify migration
        from hermes_state import SCHEMA_VERSION
        cursor = migrated_db._conn.execute("SELECT version FROM schema_version")
        assert cursor.fetchone()[0] == SCHEMA_VERSION

        # Verify title column exists and is NULL for existing sessions
        session = migrated_db.get_session("existing")
        assert session is not None
        assert session["title"] is None

        # Verify api_call_count column was added with default 0
        cursor = migrated_db._conn.execute(
            "SELECT api_call_count FROM sessions WHERE id = 'existing'"
        )
        assert cursor.fetchone()[0] == 0

        # Verify we can set title on migrated session
        assert migrated_db.set_session_title("existing", "Migrated Title") is True
        session = migrated_db.get_session("existing")
        assert session["title"] == "Migrated Title"

        migrated_db.close()

    def test_reconciliation_adds_missing_columns(self, tmp_path):
        """Columns present in SCHEMA_SQL but missing from the live table
        are added by _reconcile_columns regardless of schema_version.

        Regression test: commit a7d78d3b inserted a new v7 migration
        (reasoning_content) and renumbered the old v7 (api_call_count)
        to v8.  Users already at the old v7 had schema_version >= 7,
        so the new v7 block was skipped and reasoning_content was never
        created — causing 'no such column' on /continue.
        """
        import sqlite3

        db_path = tmp_path / "gap_test.db"
        conn = sqlite3.connect(str(db_path))
        # Simulate the old v7 state: api_call_count exists, reasoning_content does NOT
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (7);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0
            );

            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT
            );
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "cli", 1000.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "assistant", "hello", 1001.0),
        )
        conn.commit()
        # Verify reasoning_content is absent
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        assert "reasoning_content" not in cols
        conn.close()

        # Open with SessionDB — reconciliation should add the missing column
        migrated_db = SessionDB(db_path=db_path)

        msg_cols = {
            r[1]
            for r in migrated_db._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert "reasoning_content" in msg_cols

        # The query that used to crash must now work
        cursor = migrated_db._conn.execute(
            "SELECT role, content, reasoning, reasoning_content, "
            "reasoning_details, codex_reasoning_items "
            "FROM messages WHERE session_id = ?",
            ("s1",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "assistant"
        assert row[3] is None  # reasoning_content NULL for old rows

        migrated_db.close()

    def test_reconciliation_is_idempotent(self, tmp_path):
        """Opening the same database twice doesn't error or duplicate columns."""
        db_path = tmp_path / "idempotent.db"
        db1 = SessionDB(db_path=db_path)
        cols1 = {r[1] for r in db1._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db1.close()

        db2 = SessionDB(db_path=db_path)
        cols2 = {r[1] for r in db2._conn.execute("PRAGMA table_info(messages)").fetchall()}
        db2.close()

        assert cols1 == cols2

    def test_schema_version(self, db):
        from hermes_state import SCHEMA_VERSION
        cursor = db._conn.execute("SELECT version FROM schema_version")
        version = cursor.fetchone()[0]
        assert version == SCHEMA_VERSION

    def test_tables_exist(self, db):
        cursor = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert "sessions" in tables
        assert "messages" in tables
        assert "schema_version" in tables

    def test_telegram_topic_binding_refuses_to_relink_session_to_another_topic(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(
            session_id="topic-session",
            source="telegram",
            user_id="208214988",
        )
        db.bind_telegram_topic(
            chat_id="208214988",
            thread_id="17585",
            user_id="208214988",
            session_key="key-17585",
            session_id="topic-session",
        )

        with pytest.raises(ValueError, match="already linked"):
            db.bind_telegram_topic(
                chat_id="208214988",
                thread_id="99999",
                user_id="208214988",
                session_key="key-99999",
                session_id="topic-session",
            )
        db.close()

    def test_title_column_exists(self, db):
        """Verify the title column was created in the sessions table."""
        cursor = db._conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "title" in columns

    def test_topic_mode_schema_is_not_auto_migrated_on_open(self, tmp_path):
        """Opening an old DB should not add topic-mode columns until /topic opts in.

        The gateway must remain rollback-safe: simply upgrading Hermes and starting
        the old bot should not eagerly mutate the state DB for this feature.
        """
        old_db = tmp_path / "old.db"
        import sqlite3

        conn = sqlite3.connect(old_db)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                billing_provider TEXT,
                billing_base_url TEXT,
                billing_mode TEXT,
                estimated_cost_usd REAL,
                actual_cost_usd REAL,
                cost_status TEXT,
                cost_source TEXT,
                pricing_version TEXT,
                title TEXT,
                api_call_count INTEGER DEFAULT 0,
                FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT
            );
            """
        )
        conn.close()

        db = SessionDB(db_path=old_db)
        cursor = db._conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        assert {"telegram_dm_topic_mode", "telegram_topic_thread_id"}.isdisjoint(columns)
        db.close()

    def test_v9_migration_skips_v10_trigram_backfill_before_v11_rebuild(self, tmp_path, monkeypatch):
        """Direct v9→current migration should do only the v23 FTS rebuild.

        v10 backfilled ``messages_fts_trigram`` with content-only rows. The
        current migration immediately drops and rebuilds both FTS tables in
        external-content form, so running the v10 insert first is wasted work.

        v23 contract: tool rows are excluded from the trigram index (they
        remain fully searchable via the standard index); non-tool rows are
        indexed in both.
        """
        db_path = tmp_path / "v9_fts.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (9)")
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "cli", 1000.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "tool", "plain content", "browser_snapshot", '{"name":"browser_snapshot"}', 1001.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "assistant", "assistant summary of the snapshot", 1002.0),
        )
        conn.commit()
        conn.close()

        trigram_content_only_inserts = []
        real_connect = sqlite3.connect

        def connect_with_trace(*args, **kwargs):
            conn = real_connect(*args, **kwargs)

            def trace(sql):
                text = " ".join(str(sql).split())
                if (
                    "INSERT INTO messages_fts_trigram" in text
                    and "SELECT id, content FROM messages" in text
                ):
                    trigram_content_only_inserts.append(text)

            conn.set_trace_callback(trace)
            return conn

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect_with_trace)
        migrated_db = SessionDB(db_path=db_path)
        try:
            assert trigram_content_only_inserts == []
            version = migrated_db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            # This DB was built via SCHEMA_SQL, so its FTS is already the v23
            # external-content shape — not a legacy inline install. Opening it
            # therefore advances the version to current (no opt-in gate) and
            # runs no backfill (rows were indexed live by the v23 triggers).
            assert version == SCHEMA_VERSION
            assert migrated_db.fts_optimize_available() is False
            assert migrated_db.fts_rebuild_status() is None
            # Standard FTS indexes every row, including tool output (MATCH
            # probes the index; COUNT(*) on external-content tables doesn't).
            normal_count = migrated_db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'snapshot'"
            ).fetchone()[0]
            assert normal_count == 2
            # Trigram excludes role='tool' rows (v23) but keeps non-tool rows.
            trigram_count = migrated_db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'snapshot'"
            ).fetchone()[0]
            assert trigram_count == 1
            # Tool metadata stays searchable via the standard index (#16751).
            tool_hit = migrated_db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts "
                "WHERE messages_fts MATCH 'browser_snapshot'"
            ).fetchone()[0]
            assert tool_hit == 1
            # And is intentionally absent from the trigram index.
            tri_tool_hit = migrated_db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH 'browser_snapshot'"
            ).fetchone()[0]
            assert tri_tool_hit == 0
        finally:
            migrated_db.close()


class TestCompactRows:
    """list_sessions_rich and _get_session_rich_row with compact_rows=True
    must omit system_prompt but return all other metadata fields."""

    def _create(self, db, sid, *, system_prompt="big blob " * 500):
        db.create_session(session_id=sid, source="cli", model="m")
        db.update_system_prompt(sid, system_prompt)
        return sid

    def test_compact_rows_omits_system_prompt(self, db):
        self._create(db, "s1")
        rows = db.list_sessions_rich(compact_rows=True)
        assert len(rows) == 1
        assert "system_prompt" not in rows[0]




    def test_get_session_rich_row_compact_omits_system_prompt(self, db):
        self._create(db, "s1", system_prompt="should be gone")
        row = db._get_session_rich_row("s1", compact_rows=True)
        assert row is not None
        assert "system_prompt" not in row
        assert row["id"] == "s1"

    def test_batch_compact_rows_omits_system_prompt_keeps_git_fields(self, db):
        """_get_session_rich_rows_batch(compact_rows=True) must apply the same
        schema-derived compact projection as the single-row path: no
        system_prompt blob, but git_branch/git_repo_root still present."""
        self._create(db, "s1", system_prompt="should be gone")
        db.update_session_cwd("s1", "/tmp/w1", git_branch="main", git_repo_root="/tmp/w1")
        rows = db._get_session_rich_rows_batch(["s1"], compact_rows=True)
        assert set(rows) == {"s1"}
        row = rows["s1"]
        assert "system_prompt" not in row
        assert row["git_branch"] == "main"
        assert row["git_repo_root"] == "/tmp/w1"

    def test_compression_tip_projection_threads_compact_rows(self, db):
        """list_sessions_rich(compact_rows=True) must thread compact_rows
        through the batched tip-row fetch: the projected tip row must lack
        system_prompt but keep git metadata (guards the call site at the
        projection loop, not just the batch helper)."""
        import time as _time

        t0 = _time.time() - 3600
        db.create_session("rootc", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "rootc"))
        db.append_message("rootc", "user", "start")
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t0 + 100, "compression", "rootc"),
        )
        db.create_session("tipc", "cli", parent_session_id="rootc")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 101, "tipc"))
        db.append_message("tipc", "user", "continuation")
        db.update_system_prompt("tipc", "big blob " * 500)
        db.update_session_cwd("tipc", "/tmp/w2", git_branch="dev", git_repo_root="/tmp/w2")
        db._conn.commit()

        rows = db.list_sessions_rich(source="cli", compact_rows=True)
        tip = next(s for s in rows if s["id"] == "tipc")
        assert tip["_lineage_root_id"] == "rootc"
        assert "system_prompt" not in tip
        assert tip["git_branch"] == "dev"
        assert tip["git_repo_root"] == "/tmp/w2"

    def test_compact_projection_tracks_schema(self, db):
        """Behavior contract: compact rows carry EVERY sessions column except
        the excluded blob — including gateway/desktop fields (git_branch,
        session_key) and any column added later via declarative
        reconciliation. Guards against a hardcoded column list going stale."""
        self._create(db, "s1")
        live_cols = {
            row[1] for row in db._conn.execute("PRAGMA table_info(sessions)")
        }
        row = db.list_sessions_rich(compact_rows=True)[0]
        # Hardcode the sanctioned exclusions: if the excluded set ever
        # widens (or the projection silently drops a column), this fails and
        # forces a conscious review of what list consumers lose.
        #   * system_prompt — payload-heavy blob no list consumer renders.
        #   * effective_last_active — fork denorm/ordering column stripped from
        #     list rows on purpose (consumers use the computed ``last_active``);
        #     the raw stored value is internal to the recency-ordering CTEs.
        #   * system_prompt_hash — fork-parity: upstream widened the excluded
        #     set to cover the prompt HASH alongside the prompt blob (it rides
        #     with system_prompt and no list consumer renders it). The merge
        #     took upstream's _SESSION_COMPACT_EXCLUDED; this sanctioned set is
        #     the conscious review that widening is supposed to force.
        sanctioned_exclusions = {
            "system_prompt",
            "effective_last_active",
            "system_prompt_hash",
            # parity 2026-08-29: upstream added git_metadata_generation (an
            # internal write-ordering counter for async git-metadata updates,
            # e89532d97e) and excludes it from compact rows alongside the
            # prompt blob — no list consumer renders a generation counter.
            "git_metadata_generation",
        }
        missing = live_cols - set(row) - sanctioned_exclusions
        assert not missing, f"compact projection lost schema columns: {missing}"
        assert "system_prompt" not in row

    def test_compact_rows_default_is_false(self, db):
        """Default behaviour (compact_rows not passed) is unchanged — full rows."""
        self._create(db, "s1", system_prompt="present")
        rows = db.list_sessions_rich()
        assert "system_prompt" in rows[0]

    def test_compact_rows_order_by_last_active(self, db):
        """compact_rows=True also works with the CTE / order_by_last_active path."""
        self._create(db, "s1")
        self._create(db, "s2")
        rows = db.list_sessions_rich(compact_rows=True, order_by_last_active=True)
        assert len(rows) == 2
        assert all("system_prompt" not in r for r in rows)

    def test_compact_rows_preserves_metadata_fields(self, db):
        self._create(db, "s1")
        rows = db.list_sessions_rich(compact_rows=True)
        row = rows[0]
        for field in ("id", "source", "model", "started_at", "message_count",
                      "input_tokens", "output_tokens", "title", "cwd",
                      "archived", "preview", "last_active"):
            assert field in row, f"missing field: {field}"

    def test_compact_rows_tip_projection_omits_system_prompt(self, db):
        """Compression-tip projection must not reintroduce the blob: the
        merged tip row is fetched with the same compact_rows flag (salvage
        follow-up for #47437)."""
        import time as _time
        t0 = _time.time() - 3600
        db.create_session("root", "cli")
        db.update_system_prompt("root", "root blob " * 200)
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
            (t0 + 100, "root"),
        )
        db.create_session("tip", "cli", parent_session_id="root")
        db.update_system_prompt("tip", "tip blob " * 200)
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 200, "tip"))
        db._conn.commit()

        rows = db.list_sessions_rich(compact_rows=True)
        projected = [r for r in rows if r.get("_lineage_root_id") == "root"]
        assert projected, "compression root should be projected to its tip"
        assert all("system_prompt" not in r for r in rows)

    def test_full_rows_include_system_prompt(self, db):
        self._create(db, "s1", system_prompt="keep me")
        rows = db.list_sessions_rich(compact_rows=False)
        assert rows[0]["system_prompt"] == "keep me"

    def test_get_session_rich_row_full_includes_system_prompt(self, db):
        self._create(db, "s1", system_prompt="stay")
        row = db._get_session_rich_row("s1", compact_rows=False)
        assert row["system_prompt"] == "stay"






# =========================================================================
# get_messages pagination (salvage follow-up for #60347)
# =========================================================================

class TestSearchSessionsByTitle:
    def _make_discord_thread(self, db, session_id, display_name):
        db.create_session(session_id=session_id, source="discord")
        db.record_gateway_session_peer(
            session_id,
            source="discord",
            session_key=f"agent:main:discord:thread:{session_id}",
            chat_id="123",
            display_name=display_name,
        )

    def test_channel_and_thread_names_match(self, db):
        self._make_discord_thread(
            db, "s1", "Daemonarchy / #voice-assitant / Desktop App"
        )
        db.create_session(session_id="s2", source="cli")
        db.set_session_title("s2", "Unrelated work")

        # Channel name (with its typo) found per-token — no title needed.
        hits = db.search_sessions_by_title("voice")
        assert [h["id"] for h in hits] == ["s1"]
        assert hits[0]["display_name"] == "Daemonarchy / #voice-assitant / Desktop App"

        # Thread name findable the same way.
        hits = db.search_sessions_by_title("desktop app")
        assert "s1" in [h["id"] for h in hits]
    def test_empty_query_returns_nothing(self, db):
        db.create_session(session_id="s1", source="cli")
        db.set_session_title("s1", "Anything")
        assert db.search_sessions_by_title("") == []
        assert db.search_sessions_by_title("   ") == []
    def test_like_wildcards_escaped(self, db):
        db.create_session(session_id="s1", source="cli")
        db.set_session_title("s1", "100% coverage plan")
        db.create_session(session_id="s2", source="cli")
        db.set_session_title("s2", "1000 things")

        hits = db.search_sessions_by_title("100%")
        assert [h["id"] for h in hits] == ["s1"]
    def test_platform_only_query_matches_platform_sessions(self, db):
        self._make_discord_thread(db, "dc", "Daemonarchy / #random")
        db.create_session(session_id="cli1", source="cli")
        db.set_session_title("cli1", "No platform words here")

        hits = db.search_sessions_by_title("discord")
        assert [h["id"] for h in hits] == ["dc"]
    def test_platform_token_boosts_platform_sessions(self, db):
        self._make_discord_thread(db, "dc", "Daemonarchy / #general")
        db.create_session(session_id="tg", source="telegram")
        db.set_session_title("tg", "General chatter")

        # "general discord": both match "general", but the discord session
        # matches BOTH tokens (channel + platform) so it ranks first.
        hits = db.search_sessions_by_title("general discord")
        assert [h["id"] for h in hits][:2] == ["dc", "tg"]
    def test_ranking_exact_prefix_substring(self, db):
        db.create_session(session_id="sub", source="cli")
        db.set_session_title("sub", "About deploy stuff")
        db.create_session(session_id="pre", source="cli")
        db.set_session_title("pre", "Deploy pipeline")
        db.create_session(session_id="exact", source="cli")
        db.set_session_title("exact", "Deploy")

        hits = db.search_sessions_by_title("deploy")
        assert [h["id"] for h in hits] == ["exact", "pre", "sub"]
    def test_subagent_children_hidden(self, db):
        db.create_session(session_id="parent", source="cli")
        db.set_session_title("parent", "Visible needle session")
        db.create_session(
            session_id="child", source="subagent", parent_session_id="parent"
        )
        db.set_session_title("child", "Hidden needle child")

        hits = db.search_sessions_by_title("needle")
        assert [h["id"] for h in hits] == ["parent"]
    def test_substring_case_insensitive(self, db):
        db.create_session(session_id="s1", source="discord")
        db.set_session_title("s1", "DNS Blocking Portal Investigation")
        db.create_session(session_id="s2", source="cli")
        db.set_session_title("s2", "Unrelated work")

        hits = db.search_sessions_by_title("blocking portal")
        assert [h["id"] for h in hits] == ["s1"]
        assert hits[0]["title"] == "DNS Blocking Portal Investigation"
        assert hits[0]["source"] == "discord"
    def test_title_hits_still_outrank_channel_hits(self, db):
        self._make_discord_thread(db, "chan", "Daemonarchy / #deploy-notes")
        db.create_session(session_id="titled", source="cli")
        db.set_session_title("titled", "Deploy pipeline")

        hits = db.search_sessions_by_title("deploy")
        assert [h["id"] for h in hits] == ["titled", "chan"]
    def test_untitled_and_nonmatching_excluded(self, db):
        db.create_session(session_id="s1", source="cli")  # no title
        db.create_session(session_id="s2", source="cli")
        db.set_session_title("s2", "Something else entirely")

        assert db.search_sessions_by_title("needle") == []


class TestTitleLineage:
    """Tests for title lineage resolution and auto-numbering."""

    def test_resolve_exact_title(self, db):
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        assert db.resolve_session_by_title("my project") == "s1"



    def test_resolve_nonexistent_title(self, db):
        assert db.resolve_session_by_title("nonexistent") is None

    def test_next_title_no_existing(self, db):
        """With no existing sessions, base title is returned as-is."""
        assert db.get_next_title_in_lineage("my project") == "my project"

    def test_next_title_first_continuation(self, db):
        """First continuation after the original gets #2."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        assert db.get_next_title_in_lineage("my project") == "my project #2"

    def test_next_title_increments(self, db):
        """Each continuation increments the number."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        db.create_session("s2", "cli")
        db.set_session_title("s2", "my project #2")
        db.create_session("s3", "cli")
        db.set_session_title("s3", "my project #3")
        assert db.get_next_title_in_lineage("my project") == "my project #4"

    def test_next_title_strips_existing_number(self, db):
        """Passing a numbered title strips the number and finds the base."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        db.create_session("s2", "cli")
        db.set_session_title("s2", "my project #2")
        # Even when called with "my project #2", it should return #3
        assert db.get_next_title_in_lineage("my project #2") == "my project #3"

    def test_resolve_exact_numbered(self, db):
        """Resolving an exact numbered title returns that specific session."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        db.create_session("s2", "cli")
        db.set_session_title("s2", "my project #2")
        # Resolving "my project #2" exactly should return s2
        assert db.resolve_session_by_title("my project #2") == "s2"

    def test_resolve_returns_latest_numbered(self, db):
        """When numbered variants exist, return the most recent one."""
        import time
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        time.sleep(0.01)
        db.create_session("s2", "cli")
        db.set_session_title("s2", "my project #2")
        time.sleep(0.01)
        db.create_session("s3", "cli")
        db.set_session_title("s3", "my project #3")
        # Resolving "my project" should return s3 (latest numbered variant)
        assert db.resolve_session_by_title("my project") == "s3"





class TestVacuum:
    def test_vacuum_runs_without_error(self, db):
        """VACUUM must succeed on a fresh DB (no rows to reclaim)."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(session_id="s1", role="user", content="hi")
        # Should not raise, even though there's nothing significant to reclaim.
        db.vacuum()

    def test_auto_maintenance_records_successful_vacuum(self, db, monkeypatch):
        monkeypatch.setattr(db, "prune_sessions", lambda **_kwargs: 3)
        vacuum_calls = []
        monkeypatch.setattr(db, "vacuum", lambda: vacuum_calls.append(True))

        result = db.maybe_auto_prune_and_vacuum(min_interval_hours=0)

        assert result["vacuumed"] is True
        assert vacuum_calls == [True]
        assert db.get_meta("last_vacuum") is not None

    def test_auto_maintenance_skips_recent_vacuum(self, db, monkeypatch):
        monkeypatch.setattr(db, "prune_sessions", lambda **_kwargs: 3)
        db.set_meta("last_vacuum", str(time.time()))
        vacuum_calls = []
        monkeypatch.setattr(db, "vacuum", lambda: vacuum_calls.append(True))

        result = db.maybe_auto_prune_and_vacuum(
            min_interval_hours=0,
            min_vacuum_interval_days=30,
        )

        assert result["vacuumed"] is False
        assert vacuum_calls == []

    def test_auto_maintenance_retries_after_vacuum_interval(self, db, monkeypatch):
        monkeypatch.setattr(db, "prune_sessions", lambda **_kwargs: 3)
        db.set_meta("last_vacuum", str(time.time() - 31 * 86400))
        vacuum_calls = []
        monkeypatch.setattr(db, "vacuum", lambda: vacuum_calls.append(True))

        result = db.maybe_auto_prune_and_vacuum(
            min_interval_hours=0,
            min_vacuum_interval_days=30,
        )

        assert result["vacuumed"] is True
        assert vacuum_calls == [True]

    def test_auto_maintenance_retries_after_failed_vacuum(self, db, monkeypatch):
        monkeypatch.setattr(db, "prune_sessions", lambda **_kwargs: 3)
        vacuum_calls = []

        def fail_first_vacuum():
            vacuum_calls.append(True)
            if len(vacuum_calls) == 1:
                raise RuntimeError("vacuum failed")

        monkeypatch.setattr(db, "vacuum", fail_first_vacuum)

        first = db.maybe_auto_prune_and_vacuum(min_interval_hours=0)

        assert first["vacuumed"] is False
        assert db.get_meta("last_vacuum") is None

        second = db.maybe_auto_prune_and_vacuum(min_interval_hours=0)

        assert second["vacuumed"] is True
        assert vacuum_calls == [True, True]
        assert db.get_meta("last_vacuum") is not None

    def test_wal_size_limit_is_bounded(self, db):
        """journal_size_limit must be a finite bound, not SQLite's -1 default.

        Contract, not a snapshot: assert the limit is positive (so the WAL is
        truncated back at checkpoints) rather than pinning the exact byte
        count, which is a tunable.
        """
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            pytest.skip("WAL unavailable on this filesystem")
        limit = db._conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        assert limit > 0, "unbounded WAL: state.db-wal never returns disk to the OS"

    def test_vacuum_leaves_wal_truncated(self, db, tmp_path):
        """VACUUM must not strand a giant WAL beside the database.

        VACUUM rewrites every page through the write-ahead log. Without a
        checkpoint *after* it, a 3 GB database leaves a 3 GB state.db-wal
        behind — `sessions optimize` then consumes far more disk than it
        frees, which is the opposite of its purpose.
        """
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            pytest.skip("WAL unavailable on this filesystem")

        db.create_session(session_id="s1", source="cli")
        for i in range(500):
            db.append_message(
                session_id="s1", role="user", content=f"padding message {i} " * 20
            )
        db.vacuum()

        wal = Path(str(db.db_path) + "-wal")
        if wal.exists():
            limit = db._conn.execute("PRAGMA journal_size_limit").fetchone()[0]
            assert wal.stat().st_size <= max(limit, 0) or wal.stat().st_size == 0, (
                f"WAL left at {wal.stat().st_size} bytes after VACUUM"
            )


class TestFtsRebuildLoopWithoutTrigram:
    """A trigram-less SQLite build must not re-index the store on every open.

    The three ``messages_fts_trigram_*`` triggers are declared only by the
    trigram DDL, whose ``CREATE VIRTUAL TABLE ... tokenize='trigram'`` needs a
    tokenizer SQLite only gained in 3.34 — Ubuntu 20.04 (3.31), RHEL/CentOS 8
    (3.26) and Amazon Linux 2 all ship older. ``_ensure_fts_schema``
    soft-fails that DDL there by design, so those three triggers can never
    exist, and startup's "are all six canonical triggers present?" check was
    therefore permanently unsatisfiable: the full FTS repair ran on every
    single ``SessionDB`` open, holding the write lock, and never converged.

    The v23 repair also clears the deferred-rebuild resume markers, so an
    interrupted ``hermes sessions optimize-storage`` silently lost its place
    every time the store was reopened.
    """

    @staticmethod
    def _trace(monkeypatch, statements, *, trigram):
        """Record every statement SessionDB executes during an open.

        ``trigram=False`` additionally routes connections through the
        module's existing ``_NoTrigramConnection``, which raises
        ``no such tokenizer: trigram`` for the trigram DDL exactly as an
        older SQLite does.
        """
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            if not trigram:
                kwargs["factory"] = _NoTrigramConnection
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr("hermes_state.sqlite3.connect", connect)

    @staticmethod
    def _rebuilds(statements):
        """External-content 'rebuild' commands seen in *statements*."""
        return [sql for sql in statements if "VALUES('rebuild')" in "".join(sql.split())]

    @staticmethod
    def _legacy_wipes(statements):
        """Legacy inline repair wipes the index before reinserting every row."""
        return [
            sql for sql in statements
            if "".join(sql.split()).upper().startswith("DELETEFROMMESSAGES_FTS")
        ]

    @staticmethod
    def _seed(db_path):
        db = SessionDB(db_path=db_path)
        try:
            db.create_session(session_id="s1", source="cli")
            for i in range(5):
                db.append_message("s1", role="user", content=f"payload {i} zebra")
        finally:
            db.close()

    @staticmethod
    def _build_legacy_inline_db(db_path):
        """A v22 store as it exists on a host that never had the tokenizer.

        Only the three base inline triggers were ever creatable there, so —
        unlike the migration fixtures elsewhere in this file — this build
        deliberately has no trigram table and no trigram triggers.
        """
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(SCHEMA_SQL)
            conn.executescript("""
                DROP TABLE IF EXISTS messages_fts;
                DROP TABLE IF EXISTS messages_fts_trigram;
                DROP VIEW IF EXISTS messages_fts_trigram_src;

                CREATE VIRTUAL TABLE messages_fts USING fts5(content);

                CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content) VALUES (
                        new.id,
                        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')
                        || ' ' || COALESCE(new.tool_calls, '')
                    );
                END;

                CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
                    DELETE FROM messages_fts WHERE rowid = old.id;
                END;

                CREATE TRIGGER messages_fts_update
                AFTER UPDATE OF content, tool_name, tool_calls ON messages BEGIN
                    DELETE FROM messages_fts WHERE rowid = old.id;
                    INSERT INTO messages_fts(rowid, content) VALUES (
                        new.id,
                        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')
                        || ' ' || COALESCE(new.tool_calls, '')
                    );
                END;
            """)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (22)")
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', ?)",
                (time.time(),),
            )
            for i in range(5):
                conn.execute(
                    "INSERT INTO messages (session_id, timestamp, role, content) "
                    "VALUES ('s1', ?, 'user', ?)",
                    (time.time(), f"legacy payload {i} zebra"),
                )
            conn.commit()
        finally:
            conn.close()

    def test_fts_trigger_subsets_match_the_ddl(self):
        """The split must track the DDL each trigger actually comes from.

        The gate is only correct while every trigger classified as "trigram"
        is one the trigram DDL creates, and every other one is created by DDL
        that always works. Renaming a trigger without updating its DDL would
        otherwise silently reintroduce an unsatisfiable check.
        """
        from hermes_state_common import (
            FTS_SQL,
            FTS_TRIGRAM_SQL,
            LEGACY_FTS_SQL,
            LEGACY_FTS_TRIGRAM_SQL,
            _FTS_TRIGGERS,
        )
        from hermes_state_schema import _FTS_BASE_TRIGGERS, _FTS_TRIGRAM_TRIGGERS

        # Exhaustive and disjoint: nothing may fall out of the classification.
        assert set(_FTS_BASE_TRIGGERS) | set(_FTS_TRIGRAM_TRIGGERS) == set(_FTS_TRIGGERS)
        assert not set(_FTS_BASE_TRIGGERS) & set(_FTS_TRIGRAM_TRIGGERS)

        for name in _FTS_TRIGRAM_TRIGGERS:
            assert name in FTS_TRIGRAM_SQL and name in LEGACY_FTS_TRIGRAM_SQL, (
                f"{name} is classified as trigram-only but the trigram DDL "
                f"does not create it"
            )
        for name in _FTS_BASE_TRIGGERS:
            assert name in FTS_SQL and name in LEGACY_FTS_SQL, (
                f"{name} is classified as always-creatable but the base DDL "
                f"does not create it"
            )
            assert name not in FTS_TRIGRAM_SQL

    def test_missing_trigram_tokenizer_does_not_rebuild_fts_on_every_open(
        self, tmp_path, monkeypatch
    ):
        """v23 branch: the repair must converge instead of firing forever."""
        db_path = tmp_path / "state.db"
        statements = []
        self._trace(monkeypatch, statements, trigram=False)

        self._seed(db_path)

        # Second and third opens of an already-initialised store. The trigram
        # triggers are still absent and always will be, but nothing is
        # actually broken, so there is nothing to repair.
        for _ in range(2):
            statements.clear()
            db = SessionDB(db_path=db_path)
            try:
                assert db._trigram_available is False
                assert self._rebuilds(statements) == []
                # The narrowed gate must not have cost us a working index.
                assert len(db.search_messages("zebra")) == 5
            finally:
                db.close()

    def test_legacy_inline_fts_without_trigram_does_not_rebuild_on_every_open(
        self, tmp_path, monkeypatch
    ):
        """Legacy (pre-v23) branch: same gate, same permanent repair loop.

        This path is the more expensive of the two — inline tables have no
        external-content 'rebuild' source, so the repair deletes the index and
        reinserts a concatenation of every row in ``messages``.
        """
        db_path = tmp_path / "legacy.db"
        self._build_legacy_inline_db(db_path)

        statements = []
        self._trace(monkeypatch, statements, trigram=False)

        for _ in range(2):
            statements.clear()
            db = SessionDB(db_path=db_path)
            try:
                assert db._db_has_legacy_inline_fts(db._conn.cursor()) is True
                assert db._trigram_available is False
                assert self._legacy_wipes(statements) == []
                assert len(db.search_messages("zebra")) == 5
            finally:
                db.close()

    def test_pending_fts_rebuild_markers_survive_a_trigramless_open(
        self, tmp_path, monkeypatch
    ):
        """An interrupted optimize-storage must keep its resume point.

        ``_rebuild_fts_indexes`` clears both markers because a full rebuild
        genuinely does cover every row. Running it unconditionally on a
        trigram-less host therefore threw away the progress of a chunked,
        throttled backfill on the very next open.
        """
        db_path = tmp_path / "state.db"
        statements = []
        self._trace(monkeypatch, statements, trigram=False)

        self._seed(db_path)

        db = SessionDB(db_path=db_path)
        try:
            for key, value in (
                ("fts_rebuild_high_water", "30"),
                ("fts_rebuild_progress", "10"),
            ):
                db._conn.execute(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            db._conn.commit()
        finally:
            db.close()

        db = SessionDB(db_path=db_path)
        try:
            assert db.get_meta("fts_rebuild_high_water") == "30"
            assert db.get_meta("fts_rebuild_progress") == "10"
        finally:
            db.close()

    def test_missing_base_trigger_still_repairs_once(self, tmp_path, monkeypatch):
        """Control: narrowing the gate must not disable genuine repair.

        A base trigger really can go missing (an earlier no-FTS5 runtime drops
        them to keep writes alive), and rows written meanwhile are absent from
        the index. That still has to be repaired — once, and then converge.
        """
        db_path = tmp_path / "state.db"
        statements = []
        self._trace(monkeypatch, statements, trigram=False)

        self._seed(db_path)

        db = SessionDB(db_path=db_path)
        try:
            db._conn.execute("DROP TRIGGER messages_fts_insert")
            db._conn.commit()
        finally:
            db.close()

        statements.clear()
        db = SessionDB(db_path=db_path)
        try:
            assert len(self._rebuilds(statements)) == 1
            assert db._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'messages_fts_insert'"
            ).fetchone()[0] == 1
        finally:
            db.close()

        # …and having repaired it, the next open is quiet again.
        statements.clear()
        db = SessionDB(db_path=db_path)
        try:
            assert self._rebuilds(statements) == []
        finally:
            db.close()

    def test_missing_trigram_trigger_still_repairs_where_the_tokenizer_exists(
        self, tmp_path, monkeypatch
    ):
        """Control: on a capable host a missing trigram trigger is real damage.

        Only the permanently-unsatisfiable case changes. Where the trigram DDL
        can run, a gap in those triggers means the index missed rows and must
        still be rebuilt.
        """
        db_path = tmp_path / "state.db"
        statements = []
        self._trace(monkeypatch, statements, trigram=True)

        self._seed(db_path)

        db = SessionDB(db_path=db_path)
        trigram_available = db._trigram_available
        try:
            if not trigram_available:
                pytest.skip("this SQLite build has no trigram tokenizer")
            db._conn.execute("DROP TRIGGER messages_fts_trigram_insert")
            db._conn.commit()
        finally:
            db.close()

        statements.clear()
        db = SessionDB(db_path=db_path)
        try:
            assert db._trigram_available is True
            assert len(self._rebuilds(statements)) > 0
        finally:
            db.close()


class TestListCronJobRuns:
    """``list_cron_job_runs`` powers the desktop cron run-history endpoint.

    It must scope to exactly one job's runs via an id prefix range (not a
    substring), order newest-first, enrich with preview/last_active, and stay
    bounded by the requested window rather than the whole cron history.
    """

    def _seed_run(self, db, job_id: str, idx: int, started_at: float):
        sid = f"cron_{job_id}_{idx:08d}"
        db.create_session(session_id=sid, source="cron")
        db.append_message(sid, role="user", content=f"run {idx} for {job_id}")
        db.append_message(sid, role="assistant", content="done")
        db.end_session(sid, "completed")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, sid)
        )
        db._conn.commit()
        return sid

    def test_scopes_to_job_newest_first_and_enriched(self, db):
        base = 1_700_000_000.0
        # Target job: 5 runs, ascending started_at.
        for i in range(5):
            self._seed_run(db, "alpha", i, base + i * 60)
        # A different job that must not leak in.
        for i in range(3):
            self._seed_run(db, "beta", i, base + i * 60)

        runs = db.list_cron_job_runs("alpha", limit=20)

        assert len(runs) == 5
        assert all(r["id"].startswith("cron_alpha_") for r in runs)
        # Newest started_at first.
        sts = [r["started_at"] for r in runs]
        assert sts == sorted(sts, reverse=True)
        # Enriched like list_sessions_rich.
        assert runs[0]["preview"].startswith("run 4 for alpha")
        assert runs[0]["last_active"] >= runs[0]["started_at"]



    def test_limit_and_offset_paging(self, db):
        base = 1_700_000_000.0
        for i in range(10):
            self._seed_run(db, "alpha", i, base + i * 60)

        page1 = db.list_cron_job_runs("alpha", limit=4, offset=0)
        page2 = db.list_cron_job_runs("alpha", limit=4, offset=4)

        assert len(page1) == 4
        assert len(page2) == 4
        assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})
        # Combined window is still newest-first and contiguous.
        combined = [r["started_at"] for r in page1 + page2]
        assert combined == sorted(combined, reverse=True)

    def test_ignores_non_cron_sessions(self, db):
        base = 1_700_000_000.0
        self._seed_run(db, "alpha", 0, base)
        # A non-cron session whose id happens to share the prefix shape.
        db.create_session(session_id="cron_alpha_99999999", source="cli")
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (base + 100, "cron_alpha_99999999"),
        )
        db._conn.commit()

        runs = db.list_cron_job_runs("alpha", limit=20)

        assert [r["id"] for r in runs] == ["cron_alpha_00000000"]

    def test_prefix_match_excludes_substring_collision(self, db):
        """A job whose id contains the target id as a substring must not leak.

        The old code used a leading-wildcard ``LIKE %cron_<id>_%`` which would
        also match ``cron_xalpha_...``; the range scan binds to the true prefix.
        """
        base = 1_700_000_000.0
        self._seed_run(db, "alpha", 0, base)
        # Collision: id is "xalpha", which contains "alpha".
        self._seed_run(db, "xalpha", 0, base + 10)
        # Collision the other way: id "alpha2" extends past the underscore.
        self._seed_run(db, "alpha2", 0, base + 20)

        runs = db.list_cron_job_runs("alpha", limit=20)

        assert [r["id"] for r in runs] == ["cron_alpha_00000000"]

    def test_uses_index_range_scan(self, db):
        """The query must use the (source, id) index, not a full table scan."""
        prefix = "cron_alpha_"
        prefix_hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        plan = db._conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT s.* FROM sessions s "
            "WHERE s.source = 'cron' AND s.id >= ? AND s.id < ? "
            "ORDER BY s.started_at DESC LIMIT 20",
            (prefix, prefix_hi),
        ).fetchall()
        detail = " ".join(row[-1] for row in plan)
        assert "USING INDEX" in detail or "USING COVERING INDEX" in detail, detail
        assert "idx_sessions_source" in detail, detail



class TestSearchSessions:
    def test_list_all_sessions(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")

        sessions = db.search_sessions()
        assert len(sessions) == 2


    def test_pagination(self, db):
        for i in range(5):
            db.create_session(session_id=f"s{i}", source="cli")

        page1 = db.search_sessions(limit=2)
        page2 = db.search_sessions(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0]["id"] != page2[0]["id"]

    def test_filter_by_source(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")

        sessions = db.search_sessions(source="cli")
        assert len(sessions) == 1
        assert sessions[0]["source"] == "cli"


# =========================================================================
# Counts
# =========================================================================

class TestTitleSqlWildcards:
    """Titles containing SQL LIKE wildcards (%, _) must not cause false matches."""

    def test_resolve_title_with_underscore(self, db):
        """A title like 'test_project' should not match 'testXproject #2'."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "test_project")
        db.create_session("s2", "cli")
        db.set_session_title("s2", "testXproject #2")
        # Resolving "test_project" should return s1 (exact), not s2
        assert db.resolve_session_by_title("test_project") == "s1"

    def test_next_lineage_with_underscore(self, db):
        """get_next_title_in_lineage with underscores doesn't match wrong sessions."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "test_project")
        db.create_session("s2", "cli")
        db.set_session_title("s2", "testXproject #2")
        # Only "test_project" exists, so next should be "test_project #2"
        assert db.get_next_title_in_lineage("test_project") == "test_project #2"

    def test_resolve_title_with_percent(self, db):
        """A title with '%' should not wildcard-match unrelated sessions."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "100% done")
        db.create_session("s2", "cli")
        db.set_session_title("s2", "100X done #2")
        # Should resolve to s1 (exact), not s2
        assert db.resolve_session_by_title("100% done") == "s1"




class TestDisplayMetadataReadPaths:
    """Every message read path must hand back the decoded dict.

    Returning the raw column instead reaches the desktop as a string, where
    ``'task_count' in meta`` throws and fails the whole session resume.
    """

    META = {
        "delegation_id": "deleg_0d84d484",
        "task_count": 1,
        "completed_count": 1,
        "failed_count": 0,
        "duration_seconds": 193.55,
    }

    @staticmethod
    def _seed(db):
        db.create_session("s1", source="desktop")
        message_id = db.append_message(
            "s1", "user", "event",
            display_kind="async_delegation_complete",
            display_metadata=TestDisplayMetadataReadPaths.META,
        )
        return message_id, db.append_message("s1", "assistant", "anchor")

    @staticmethod
    def _read(db, reader, message_id, anchor_id):
        if reader == "get_messages":
            return db.get_messages("s1")[0]
        if reader == "get_messages_around":
            return db.get_messages_around("s1", message_id, window=0)["window"][0]
        if reader == "get_anchored_view":
            view = db.get_anchored_view("s1", anchor_id, window=0, bookend=1)
            return view["bookend_start"][0]
        return db.get_messages_as_conversation("s1")[0]

    READERS = ("get_messages", "get_messages_around", "get_anchored_view", "conversation")

    @pytest.mark.parametrize("reader", READERS)
    def test_every_reader_decodes_display_metadata(self, db, reader):
        message_id, anchor_id = self._seed(db)
        assert self._read(db, reader, message_id, anchor_id)["display_metadata"] == self.META


    @pytest.mark.parametrize("reader", READERS)
    @pytest.mark.parametrize("raw", ["", "{not-json", "[]", '"text"', "0"])
    def test_every_reader_drops_unusable_display_metadata(self, db, reader, raw):
        """Bad presentation metadata must not take the message down with it."""
        message_id, anchor_id = self._seed(db)

        def _corrupt(conn):
            conn.execute(
                "UPDATE messages SET display_metadata = ? WHERE id = ?",
                (raw, message_id),
            )

        db._execute_write(_corrupt)
        message = self._read(db, reader, message_id, anchor_id)
        assert message.get("display_metadata") is None
        assert message["content"] == "event"

    def test_export_import_round_trip_keeps_metadata_decodable(self, db, tmp_path):
        """The read leak used to write a permanently double-encoded row here.

        ``export_session`` reads through ``get_messages``, so an undecoded
        string went back through ``_insert_message_rows`` and got re-dumped.
        """
        self._seed(db)
        blob = db.export_session("s1")
        assert isinstance(blob["messages"][0]["display_metadata"], dict)

        target = SessionDB(db_path=tmp_path / "imported.db")
        try:
            target.import_sessions([json.loads(json.dumps(blob))])
            assert target.get_messages_as_conversation("s1")[0]["display_metadata"] == self.META
            assert target.get_messages("s1")[0]["display_metadata"] == self.META
        finally:
            target.close()




class TestGatewayRoutingPkHeal:
    """Legacy gateway_routing tables (session_key-only PK) get rebuilt on open.

    Early builds of the #59203 routing-index migration created gateway_routing
    with ``session_key TEXT PRIMARY KEY`` and no ``scope`` column. The column
    reconciler ADDs ``scope`` but cannot change the PK, so on those databases
    every routing save failed ("ON CONFLICT clause does not match any PRIMARY
    KEY or UNIQUE constraint" / "UNIQUE constraint failed:
    gateway_routing.session_key") and spammed warnings on each save.
    """

    LEGACY_SQL = """
        CREATE TABLE gateway_routing (
            session_key TEXT PRIMARY KEY,
            entry_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        , "scope" TEXT DEFAULT '')
    """

    def _make_legacy_db(self, tmp_path, rows=()):
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(db_path)
        conn.execute(self.LEGACY_SQL)
        conn.executemany(
            "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            list(rows),
        )
        conn.commit()
        conn.close()
        return db_path

    def _pk_cols(self, db):
        rows = db._conn.execute('PRAGMA table_info("gateway_routing")').fetchall()
        cols = sorted(
            ((r["pk"], r["name"]) for r in rows if r["pk"]),
        )
        return [name for _, name in cols]

    def test_legacy_pk_rebuilt_to_composite(self, tmp_path):
        db_path = self._make_legacy_db(
            tmp_path, rows=[("/home/u/.hermes/sessions", "agent:main:telegram:dm:1", "{}", 1.0)]
        )
        db = SessionDB(db_path=db_path)
        try:
            assert self._pk_cols(db) == ["scope", "session_key"]
            # Existing rows survive the rebuild.
            entries = db.load_gateway_routing_entries(scope="/home/u/.hermes/sessions")
            assert entries == {"agent:main:telegram:dm:1": "{}"}
        finally:
            db.close()



    def test_current_shape_left_untouched(self, tmp_path, db):
        """A DB born with the composite PK is not rebuilt (idempotence)."""
        db.save_gateway_routing_entry("k1", "{}", scope="s")
        assert self._pk_cols(db) == ["scope", "session_key"]
        # Re-running the heal is a no-op.
        cur = db._conn.cursor()
        db._heal_gateway_routing_pk(cur)
        assert db.load_gateway_routing_entries(scope="s") == {"k1": "{}"}


def test_gateway_session_peer_round_trip_and_recovery(db):
    db.create_session(
        "gw-session",
        "telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
        thread_id=None,
    )
    db.append_message("gw-session", "user", "hello")

    row = db.get_session("gw-session")
    assert row["session_key"] == "agent:main:telegram:dm:chat-1"
    assert row["chat_id"] == "chat-1"
    assert row["chat_type"] == "dm"

    recovered = db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    assert recovered["id"] == "gw-session"


def test_refresh_cannot_resurrect_a_lock_already_reclaimed(db, monkeypatch):
    """Once a competitor owns the row, the old holder's refresh must fail.

    The guard is the ``holder`` match, not the clock: a reclaim replaces
    ``holder``, so the previous owner's UPDATE matches nothing.
    """
    db.create_session("s1", "cli")

    monkeypatch.setattr(hermes_state.time, "time", lambda: 1000.0)
    assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=10.0) is True

    # holder-a's lease lapses and holder-b legitimately reclaims it.
    monkeypatch.setattr(hermes_state.time, "time", lambda: 1020.0)
    assert db.try_acquire_compression_lock("s1", "holder-b", ttl_seconds=10.0) is True

    # holder-a coming back late must NOT steal it back.
    assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=10.0) is False
    current = db._conn.execute(
        "SELECT holder FROM compression_locks WHERE session_id = ?",
        ("s1",),
    ).fetchone()[0]
    assert current == "holder-b"


# =========================================================================
# compact_rows — lightweight column projection (issue #47414)
# =========================================================================

def test_expired_compression_failure_cooldown_is_ignored(db):
    db.create_session("s1", "cli")

    db.record_compression_failure_cooldown("s1", time.time() - 60.0, "stale")

    assert db.get_compression_failure_cooldown("s1") is None


def test_list_gateway_sessions_filters_and_dedupes(db):
    # Two rows on the same session_key: only the newest should be returned.
    db.create_session(
        "gw-old", "telegram",
        session_key="agent:main:telegram:dm:c1", chat_id="c1", chat_type="dm",
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = started_at - 100 WHERE id = 'gw-old'"
    )
    db._conn.commit()
    db.create_session(
        "gw-new", "telegram",
        session_key="agent:main:telegram:dm:c1", chat_id="c1", chat_type="dm",
    )
    db.create_session(
        "gw-discord", "discord",
        session_key="agent:main:discord:group:g1:u1", chat_id="g1", chat_type="group",
    )
    # Non-gateway session (no session_key) must never appear.
    db.create_session("cli-session", "cli")
    # Ended gateway session excluded when active_only.
    db.create_session(
        "gw-ended", "slack",
        session_key="agent:main:slack:dm:s1", chat_id="s1", chat_type="dm",
    )
    db.end_session("gw-ended", "session_reset")

    rows = db.list_gateway_sessions(active_only=True)
    ids = {r["id"] for r in rows}
    assert ids == {"gw-new", "gw-discord"}

    tg_rows = db.list_gateway_sessions(platform="telegram", active_only=True)
    assert [r["id"] for r in tg_rows] == ["gw-new"]

    all_rows = db.list_gateway_sessions(active_only=False)
    assert "gw-ended" in {r["id"] for r in all_rows}
    assert "cli-session" not in {r["id"] for r in all_rows}


def test_v18_backfill_from_sessions_json(tmp_path, monkeypatch):
    """Migration backfills display_name/origin_json/expiry_finalized from sessions.json."""
    import hermes_state as hs

    home = tmp_path / ".hermes"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hs, "DEFAULT_DB_PATH", home / "state.db")

    # Seed a pre-v18 database: create schema, downgrade version, add a bare row.
    db = hs.SessionDB(home / "state.db")
    db.create_session("legacy-gw", "telegram", user_id="u1")
    db._conn.execute("UPDATE schema_version SET version = 17")
    db._conn.execute(
        "UPDATE sessions SET session_key = NULL, display_name = NULL, "
        "origin_json = NULL WHERE id = 'legacy-gw'"
    )
    db._conn.commit()
    db.close()

    origin = {"platform": "telegram", "chat_id": "123", "chat_name": "Alice",
              "chat_type": "dm", "user_id": "u1"}
    (home / "sessions" / "sessions.json").write_text(json.dumps({
        "_README": "sentinel",
        "agent:main:telegram:dm:123": {
            "session_id": "legacy-gw",
            "display_name": "Alice",
            "chat_type": "dm",
            "expiry_finalized": True,
            "origin": origin,
        },
    }))

    db = hs.SessionDB(home / "state.db")
    row = db.get_session("legacy-gw")
    db.close()
    assert row["session_key"] == "agent:main:telegram:dm:123"
    assert row["display_name"] == "Alice"
    assert row["chat_id"] == "123"
    assert json.loads(row["origin_json"])["chat_name"] == "Alice"
    assert row["expiry_finalized"] == 1


