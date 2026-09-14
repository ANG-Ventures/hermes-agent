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


class TestMessageStorage:
    def test_append_and_get_messages(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi there!")

        messages = db.get_messages("s1")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"



    def test_startup_heals_null_active_rows(self, tmp_path):
        """Rows written as active=NULL before the fix are un-hidden on startup.

        The repair UPDATE used to be gated at schema_version < 12, so
        already-v12+ databases (the exact population hit by #51646) never
        healed their historical NULL rows. It now runs on every startup.
        """
        db_path = tmp_path / "legacy_state.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER);
            INSERT INTO schema_version VALUES (12);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL,
                message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
                title TEXT, parent_session_id TEXT, model_config TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
                tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
                timestamp REAL NOT NULL, token_count INTEGER, finish_reason TEXT,
                reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
                codex_reasoning_items TEXT, codex_message_items TEXT,
                platform_message_id TEXT, observed INTEGER DEFAULT 0
            );
            CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # Default-less active column, as seen in the wild (#51646 PRAGMA).
        conn.execute("ALTER TABLE messages ADD COLUMN active INTEGER")
        conn.execute("ALTER TABLE messages ADD COLUMN compacted INTEGER DEFAULT 0")
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'discord', 1.0)"
        )
        # A row written by the pre-fix INSERT: active is NULL.
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES ('s1', 'user', 'old hidden turn', 1.0)"
        )
        conn.commit()
        conn.close()

        session_db = SessionDB(db_path=db_path)
        try:
            active = session_db._conn.execute(
                "SELECT active FROM messages WHERE content = 'old hidden turn'"
            ).fetchone()[0]
            assert active == 1
            assert len(session_db.get_messages_as_conversation("s1")) == 1
        finally:
            session_db.close()


























    def test_get_messages_as_conversation_strips_leaked_memory_context(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content=(
                "<memory-context>\n"
                "[System note: The following is recalled memory context, NOT new user input. Treat as informational background data.]\n\n"
                "## Honcho Context\n"
                "stale memory\n"
                "</memory-context>\n\n"
                "Visible answer"
            ),
        )

        conv = db.get_messages_as_conversation("s1", include_timestamp=True)
        assert len(conv) == 1
        assert conv[0]["role"] == "assistant"
        assert conv[0]["content"] == "Visible answer"
        assert isinstance(conv[0].get("timestamp"), float)

    def test_reasoning_persisted_and_restored(self, db):
        """Reasoning text is stored for assistant messages and restored by
        get_messages_as_conversation() so providers receive coherent multi-turn
        reasoning context."""
        db.create_session(session_id="s1", source="telegram")
        db.append_message("s1", role="user", content="create a cron job")
        db.append_message(
            "s1",
            role="assistant",
            content=None,
            tool_calls=[{"function": {"name": "cronjob", "arguments": "{}"}, "id": "c1", "type": "function"}],
            reasoning="I should call the cronjob tool to schedule this.",
        )
        db.append_message("s1", role="tool", content='{"job_id": "abc"}', tool_call_id="c1")

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 3
        # reasoning must be present on the assistant message
        assistant = conv[1]
        assert assistant["role"] == "assistant"
        assert assistant.get("reasoning") == "I should call the cronjob tool to schedule this."
        # user and tool messages must NOT carry reasoning
        assert "reasoning" not in conv[0]
        assert "reasoning" not in conv[2]

    def test_append_message_accepts_explicit_timestamp(self, db):
        db.create_session(session_id="s1", source="telegram")
        event_ts = 1777383653.0

        db.append_message("s1", role="user", content="Hello", timestamp=event_ts)

        messages = db.get_messages_as_conversation("s1", include_timestamp=True)
        assert messages[0]["timestamp"] == event_ts

    def test_append_message_active_one_when_column_has_no_default(self, tmp_path):
        """Legacy DBs may have active added without a working INSERT default."""
        db_path = tmp_path / "legacy_state.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL,
                message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
                title TEXT, parent_session_id TEXT, model_config TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
                tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
                timestamp REAL NOT NULL, token_count INTEGER, finish_reason TEXT,
                reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
                codex_reasoning_items TEXT, codex_message_items TEXT,
                platform_message_id TEXT, observed INTEGER DEFAULT 0
            );
            CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'discord', 1.0)"
        )
        conn.execute("ALTER TABLE messages ADD COLUMN active INTEGER")
        conn.execute("ALTER TABLE messages ADD COLUMN compacted INTEGER DEFAULT 0")
        conn.commit()
        conn.close()

        session_db = SessionDB(db_path=db_path)
        try:
            mid = session_db.append_message("s1", role="user", content="gateway turn")
            active = session_db._conn.execute(
                "SELECT active FROM messages WHERE id = ?", (mid,)
            ).fetchone()[0]
            assert active == 1
            assert len(session_db.get_messages_as_conversation("s1")) == 1
        finally:
            session_db.close()

    def test_append_message_sets_active_for_transcript_loader(self, db):
        """Regression #51646: gateway loaders filter on active = 1."""
        db.create_session(session_id="s1", source="discord")
        mid = db.append_message("s1", role="user", content="Hello")
        active = db._conn.execute(
            "SELECT active FROM messages WHERE id = ?", (mid,)
        ).fetchone()[0]
        assert active == 1
        assert len(db.get_messages_as_conversation("s1")) == 1

    def test_assistant_tool_calls_increment_by_count(self, db):
        """An assistant message with N tool_calls should increment by N."""
        db.create_session(session_id="s1", source="cli")
        tool_calls = [
            {"id": "call_1", "function": {"name": "web_search", "arguments": "{}"}},
        ]
        db.append_message("s1", role="assistant", content="", tool_calls=tool_calls)

        session = db.get_session("s1")
        assert session["tool_call_count"] == 1

    def test_codex_message_items_persisted_and_restored(self, db):
        """codex_message_items must round-trip through JSON serialization."""
        db.create_session(session_id="s1", source="cli")
        items = [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "id": "msg_123",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Thinking..."}],
            },
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "id": "msg_456",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Done!"}],
            },
        ]
        db.append_message("s1", role="assistant", content="Done!", codex_message_items=items)

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0].get("codex_message_items") == items

    def test_codex_reasoning_items_persisted_and_restored(self, db):
        """codex_reasoning_items (encrypted blobs for Codex Responses API) are
        round-tripped through JSON serialization in the DB."""
        db.create_session(session_id="s1", source="cli")
        codex_items = [
            {"type": "reasoning", "id": "rs_abc", "encrypted_content": "enc_blob_123"},
            {"type": "reasoning", "id": "rs_def", "encrypted_content": "enc_blob_456"},
        ]
        db.append_message(
            "s1",
            role="assistant",
            content="Done",
            codex_reasoning_items=codex_items,
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0]["codex_reasoning_items"] == codex_items
        assert conv[0]["codex_reasoning_items"][0]["encrypted_content"] == "enc_blob_123"

    def test_dict_content_round_trip(self, db):
        """Dict-shaped content (e.g. provider wrappers) also round-trips."""
        db.create_session(session_id="s1", source="cli")
        content = {"parts": [{"text": "hi"}]}

        db.append_message("s1", role="user", content=content)
        msgs = db.get_messages("s1")
        assert msgs[0]["content"] == content

    def test_finish_reason_restored_by_get_messages_as_conversation(self, db):
        """finish_reason on assistant messages must survive conversation replay.

        Without this, /branch copies and other transcript round-trips silently
        drop the provider's stop signal.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content="Done",
            finish_reason="tool_calls",
        )
        db.append_message("s1", role="user", content="next")

        conv = db.get_messages_as_conversation("s1")
        assert conv[0]["role"] == "assistant"
        assert conv[0]["finish_reason"] == "tool_calls"
        # Non-assistant rows should not have a finish_reason key added.
        assert "finish_reason" not in conv[1]

    def test_finish_reason_stored(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="assistant", content="Done", finish_reason="stop")

        messages = db.get_messages("s1")
        assert messages[0]["finish_reason"] == "stop"

    def test_get_ancestor_display_prefix_returns_ancestor_only_messages(self, db):
        """The prefix contains ONLY ancestor messages, not tip messages.

        Previously the prefix was calculated as
        display_history[:len(display) - len(raw)], which overcounts when
        repair_message_sequence removes messages from the MIDDLE of the
        tip history — the length difference includes both ancestor messages
        AND repair-removed tip messages, but the slice captures the first N
        display messages (tip messages when there are no ancestors),
        causing duplication in _live_session_payload. (#65919)
        """
        db.create_session("root", "tui")
        db.append_message("root", role="user", content="ancestor prompt")
        db.append_message("root", role="assistant", content="ancestor reply")
        db.create_session("child", "tui", parent_session_id="root")
        db.append_message("child", role="user", content="tip prompt")
        db.append_message("child", role="assistant", content="tip reply")
        # A verification candidate that repair_message_sequence collapses
        # (consecutive-assistant merge replaces it with the next assistant).
        db.append_message(
            "child",
            role="assistant",
            content="verification candidate",
            finish_reason="verification_required",
        )
        db.append_message("child", role="assistant", content="post-verification reply")

        prefix = db.get_ancestor_display_prefix("child")
        # Only the ancestor messages, not any tip messages.
        assert len(prefix) == 2
        assert prefix[0]["role"] == "user"
        assert prefix[0]["content"] == "ancestor prompt"
        assert prefix[1]["role"] == "assistant"
        assert prefix[1]["content"] == "ancestor reply"

        # The old broken calculation would produce a non-empty prefix
        # (because repair collapses the verification candidate, making
        # len(display) > len(raw)), even though there are 2 ancestor
        # messages — it would overcount.
        raw, display = db.get_resume_conversations("child")
        old_prefix_len = max(0, len(display) - len(raw))
        assert len(prefix) <= old_prefix_len

    def test_get_ancestor_display_prefix_single_session_returns_empty(self, db):
        """A session with no compression ancestors has an empty prefix."""
        db.create_session("solo", "cli")
        db.append_message("solo", role="user", content="hi")
        db.append_message("solo", role="assistant", content="hello")

        assert db.get_ancestor_display_prefix("solo") == []

    def test_get_messages_as_conversation(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi!")

        conv = db.get_messages_as_conversation("s1", include_timestamp=True)
        assert len(conv) == 2
        assert conv[0]["role"] == "user"
        assert conv[0]["content"] == "Hello"
        assert isinstance(conv[0]["timestamp"], float)
        assert conv[1]["role"] == "assistant"
        assert conv[1]["content"] == "Hi!"
        assert isinstance(conv[1]["timestamp"], float)

    def test_get_messages_as_conversation_avoids_repeated_resume_prompts_from_ancestors(self, db):
        db.create_session("root", "tui")
        db.append_message("root", role="user", content="same prompt")
        db.append_message("root", role="user", content="same prompt")
        db.append_message("root", role="assistant", content="answer")
        db.create_session("child", "tui", parent_session_id="root")
        db.append_message("child", role="user", content="next prompt")

        conv = db.get_messages_as_conversation("child", include_ancestors=True)

        assert [m["content"] for m in conv if m["role"] == "user"] == ["same prompt", "next prompt"]

    def test_get_messages_as_conversation_includes_ancestor_chain(self, db):
        db.create_session("root", "tui")
        db.append_message("root", role="user", content="first prompt")
        db.append_message("root", role="assistant", content="first answer")
        db.create_session("child", "tui", parent_session_id="root")
        db.append_message("child", role="user", content="second prompt")
        db.append_message("child", role="assistant", content="second answer")

        conv = db.get_messages_as_conversation("child", include_ancestors=True)

        assert [m["content"] for m in conv] == [
            "first prompt",
            "first answer",
            "second prompt",
            "second answer",
        ]

    def test_get_messages_as_conversation_orders_by_id_not_timestamp(self, db):
        """Replay must follow AUTOINCREMENT id (insertion order), never the
        wall-clock timestamp.

        ``append_message`` stamps each row with ``time.time()``, which is not
        monotonic — on WSL2, after an NTP step, or when a VM/laptop resumes
        from sleep the clock can jump backwards mid-conversation. A later
        row then carries an *earlier* timestamp than the row before it. If
        ``get_messages_as_conversation`` ordered by ``timestamp`` it would
        sort an assistant ``tool_calls`` row after its ``tool`` response,
        orphaning the tool call and triggering an HTTP 400 on the next
        completion. Ordering by ``id`` keeps the real insertion order
        regardless of clock skew. See c03acca50.
        """
        db.create_session(session_id="s1", source="cli")

        # Simulate a clock regression across a single tool round-trip: the
        # assistant tool_calls row is inserted first but stamped LATER than
        # the tool response that follows it.
        tool_calls = [
            {"id": "call_1", "function": {"name": "web_search", "arguments": "{}"}},
        ]
        db.append_message(
            "s1", role="assistant", content="", tool_calls=tool_calls,
            timestamp=1000.0,
        )
        db.append_message(
            "s1", role="tool", content="result", tool_name="web_search",
            tool_call_id="call_1", timestamp=999.0,
        )
        db.append_message("s1", role="user", content="thanks", timestamp=998.0)

        conv = db.get_messages_as_conversation("s1")

        # Insertion order is preserved even though timestamps decrease.
        assert [m["role"] for m in conv] == ["assistant", "tool", "user"]
        # The tool response stays immediately after the assistant tool_calls
        # row — the adjacency invariant the model API enforces.
        assert conv[0]["tool_calls"][0]["id"] == "call_1"
        assert conv[1]["role"] == "tool"
        assert conv[1]["tool_call_id"] == "call_1"

    def test_get_messages_as_conversation_timestamp_opt_in(self, db):
        """include_timestamp surfaces the durable arrival time for LCM ingest;
        default OFF keeps the byte-stable legacy shape for every other caller."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="hi")
        # default: no timestamp key (legacy shape, byte-stable)
        default_conv = db.get_messages_as_conversation("s1")
        assert "timestamp" not in default_conv[0]
        # opt-in: timestamp present and numeric
        ts_conv = db.get_messages_as_conversation("s1", include_timestamp=True)
        assert "timestamp" in ts_conv[0]
        assert isinstance(ts_conv[0]["timestamp"], (int, float))
        assert ts_conv[0]["timestamp"] > 0

    def test_get_resume_conversations_dedupes_replayed_ancestor_user(self, db):
        db.create_session("root", "tui")
        db.append_message("root", role="user", content="same prompt")
        db.append_message("root", role="user", content="same prompt")
        db.append_message("root", role="assistant", content="answer")
        db.create_session("child", "tui", parent_session_id="root")
        db.append_message("child", role="user", content="next prompt")

        # Same include_row_ids parity note as the sibling tests below.
        model_expected = db.get_messages_as_conversation(
            "child", repair_alternation=True, include_row_ids=True
        )
        display_expected = db.get_messages_as_conversation(
            "child", include_ancestors=True, include_row_ids=True
        )
        model_history, display_history = db.get_resume_conversations("child")

        assert model_history == model_expected
        assert display_history == display_expected

    def test_get_resume_conversations_matches_separate_reads(self, db):
        """The one-fetch resume projections must be byte-identical to the two
        separate get_messages_as_conversation reads they replace — the whole
        point of the single-SELECT optimization (desktop audit P1). Includes a
        dangling tool-call tail so repair_alternation drops rows and the model /
        display lengths diverge (exercises session.resume's prefix computation).
        """
        db.create_session("root", "tui")
        db.append_message("root", role="user", content="first prompt")
        db.append_message("root", role="assistant", content="first answer")
        db.create_session("child", "tui", parent_session_id="root")
        db.append_message("child", role="user", content="second prompt")
        db.append_message(
            "child", role="assistant", content="second answer", finish_reason="stop"
        )
        # Dangling assistant(tool_calls) tail with no tool response → repair
        # drops it, so model_history is shorter than display_history.
        db.append_message(
            "child",
            role="assistant",
            content="",
            tool_calls=[
                {"id": "t1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ],
        )

        # Parity note (2026-08-08): upstream added an OPT-IN ``include_row_ids``
        # (durable per-message identity for desktop reactions) and made
        # get_resume_conversations pass include_row_ids=True on BOTH projections.
        # The invariant under test is that the one-fetch resume equals the two
        # separate reads it replaces — so the comparison reads must ask for the
        # same shape. Without this the test compares a row-id-carrying dict to a
        # bare one and fails on a difference that is purely opt-in metadata.
        model_expected = db.get_messages_as_conversation(
            "child", repair_alternation=True, include_row_ids=True
        )
        display_expected = db.get_messages_as_conversation(
            "child", include_ancestors=True, include_row_ids=True
        )

        model_history, display_history = db.get_resume_conversations("child")

        assert model_history == model_expected
        assert display_history == display_expected
        # Sanity: the tail really did diverge the two projections.
        assert len(display_history) > len(model_history)

    def test_get_resume_conversations_single_session_no_ancestors(self, db):
        db.create_session("solo", "cli")
        db.append_message("solo", role="user", content="hi")
        db.append_message("solo", role="assistant", content="hello")

        # Same include_row_ids parity note as the sibling test above.
        model_expected = db.get_messages_as_conversation(
            "solo", repair_alternation=True, include_row_ids=True
        )
        display_expected = db.get_messages_as_conversation(
            "solo", include_ancestors=True, include_row_ids=True
        )
        model_history, display_history = db.get_resume_conversations("solo")

        assert model_history == model_expected
        assert display_history == display_expected

    def test_message_increments_session_count(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi")

        session = db.get_session("s1")
        assert session["message_count"] == 2

    def test_multimodal_list_content_round_trip(self, db):
        """Multimodal ``content`` (list of parts) must survive the SQLite
        round-trip.  sqlite3 cannot bind Python lists directly, so the DB
        layer JSON-encodes structured content on write and decodes on read.

        Regression test for the "Error binding parameter 3: type 'list' is
        not supported" crash users hit when pasting screenshots into the
        TUI (issue #17522).
        """
        db.create_session(session_id="s1", source="cli")
        content = [
            {"type": "text", "text": "describe this screenshot"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,iVBORw0KG..."},
            },
        ]

        # Write must not raise
        db.append_message("s1", role="user", content=content)

        # get_messages decodes back to the original list
        msgs = db.get_messages("s1")
        assert len(msgs) == 1
        assert msgs[0]["content"] == content

        # get_messages_as_conversation decodes back to the original list
        conv = db.get_messages_as_conversation("s1", include_timestamp=True)
        assert len(conv) == 1
        assert conv[0]["role"] == "user"
        assert conv[0]["content"] == content
        assert isinstance(conv[0].get("timestamp"), float)

    def test_observed_flag_round_trips_for_gateway_replay(self, db):
        db.create_session(session_id="s1", source="telegram:-100")
        db.append_message(
            "s1",
            role="user",
            content="[Alice|111]\nside chatter",
            observed=True,
        )
        db.append_message("s1", role="assistant", content="ack")

        messages = db.get_messages("s1")
        assert messages[0]["observed"] == 1
        assert messages[1]["observed"] == 0

        conversation = db.get_messages_as_conversation("s1", include_timestamp=True)
        assert conversation[0]["role"] == "user"
        assert conversation[0]["content"] == "[Alice|111]\nside chatter"
        assert conversation[0]["observed"] is True
        assert isinstance(conversation[0].get("timestamp"), float)
        assert "observed" not in conversation[1]

    def test_platform_message_id_round_trips(self, db):
        """Platform-side message ids (yuanbao msg_id, telegram update_id, …)
        survive append → get_messages_as_conversation under the
        ``message_id`` key so platform recall flows can match by exact id."""
        db.create_session(session_id="s_pmi", source="yuanbao")
        db.append_message(
            "s_pmi",
            role="user",
            content="hi",
            platform_message_id="abc-123",
        )
        db.append_message("s_pmi", role="assistant", content="hello")

        conv = db.get_messages_as_conversation("s_pmi")
        user_msg = next(m for m in conv if m["role"] == "user")
        assistant_msg = next(m for m in conv if m["role"] == "assistant")
        assert user_msg.get("message_id") == "abc-123"
        # Assistant row had no platform id — must not gain one spuriously.
        assert "message_id" not in assistant_msg

    def test_reasoning_content_empty_string_restored_for_assistant(self, db):
        """Empty reasoning_content still needs to round-trip for strict replays."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "date", "arguments": "{}"}}],
            reasoning_content="",
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert "reasoning_content" in conv[0]
        assert conv[0]["reasoning_content"] == ""

    def test_reasoning_content_persisted_and_restored(self, db):
        """reasoning_content must survive session replay as its own field."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1",
            role="assistant",
            content="Hello",
            reasoning="Short summary",
            reasoning_content="Longer provider-native scratchpad",
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        assert conv[0]["reasoning"] == "Short summary"
        assert conv[0]["reasoning_content"] == "Longer provider-native scratchpad"

    def test_reasoning_details_persisted_and_restored(self, db):
        """reasoning_details (structured array) is round-tripped through JSON
        serialization in the DB."""
        db.create_session(session_id="s1", source="telegram")
        details = [
            {"type": "reasoning.summary", "summary": "Thinking about tools"},
            {"type": "reasoning.encrypted_content", "encrypted_content": "abc123"},
        ]
        db.append_message(
            "s1",
            role="assistant",
            content="Hello",
            reasoning="Thinking about what to say",
            reasoning_details=details,
        )

        conv = db.get_messages_as_conversation("s1")
        assert len(conv) == 1
        msg = conv[0]
        assert msg["reasoning"] == "Thinking about what to say"
        assert msg["reasoning_details"] == details

    def test_reasoning_empty_string_not_restored(self, db):
        """Empty string reasoning is treated as absent."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="assistant", content="hi", reasoning="")

        conv = db.get_messages_as_conversation("s1")
        assert "reasoning" not in conv[0]

    def test_reasoning_not_set_for_non_assistant(self, db):
        """reasoning is never leaked onto user or tool messages."""
        db.create_session(session_id="s1", source="telegram")
        db.append_message("s1", role="user", content="hi")
        db.append_message("s1", role="assistant", content="hello", reasoning=None)

        conv = db.get_messages_as_conversation("s1")
        assert "reasoning" not in conv[0]
        assert "reasoning" not in conv[1]

    def test_replace_messages_handles_multimodal_content(self, db):
        """`replace_messages` (used by /retry, /undo, /compress) must also
        handle list content without crashing."""
        db.create_session(session_id="s1", source="cli")
        content = [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]

        db.replace_messages(
            "s1",
            [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "I see a screenshot."},
            ],
        )

        msgs = db.get_messages("s1")
        assert len(msgs) == 2
        assert msgs[0]["content"] == content
        assert msgs[1]["content"] == "I see a screenshot."

    def test_replace_messages_persists_tool_name(self, db):
        """`replace_messages` (used by /retry, /undo, /compress) must write
        tool_name to the DB for messages built by make_tool_result_message."""
        from agent.tool_dispatch_helpers import make_tool_result_message
        db.create_session(session_id="s1", source="cli")
        db.replace_messages(
            "s1",
            [
                {"role": "user", "content": "do something"},
                make_tool_result_message("web_search", "some results", "c1"),
            ],
        )

        msgs = db.get_messages("s1")
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["tool_name"] == "web_search"

    def test_replace_messages_preserves_platform_message_id(self, db):
        """``rewrite_transcript`` (which goes through replace_messages) must
        keep the platform_message_id round-trip working for /retry, /undo,
        /compress and yuanbao's recall rewrite path."""
        db.create_session(session_id="s_rep", source="yuanbao")
        db.replace_messages(
            "s_rep",
            [
                {"role": "user", "content": "x", "message_id": "ext-1"},
                {"role": "assistant", "content": "y"},
            ],
        )
        conv = db.get_messages_as_conversation("s_rep")
        assert next(m for m in conv if m["role"] == "user").get("message_id") == "ext-1"
        assert "message_id" not in next(m for m in conv if m["role"] == "assistant")

    def test_string_content_unchanged_by_encoding(self, db):
        """Plain strings must not be wrapped — FTS search and legacy
        consumers depend on raw-string storage for text content.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="plain text")

        # Peek at the raw column to confirm no encoding was applied
        with db._lock:
            row = db._conn.execute(
                "SELECT content FROM messages WHERE session_id = ?", ("s1",)
            ).fetchone()
        assert row["content"] == "plain text"

    def test_tool_call_count_matches_actual_calls(self, db):
        """tool_call_count should equal the number of tool calls made, not messages."""
        db.create_session(session_id="s1", source="cli")

        # Assistant makes 2 parallel tool calls in one message
        tool_calls = [
            {"id": "call_1", "function": {"name": "ha_call_service", "arguments": "{}"}},
            {"id": "call_2", "function": {"name": "ha_call_service", "arguments": "{}"}},
        ]
        db.append_message("s1", role="assistant", content="", tool_calls=tool_calls)

        # Two tool responses come back
        db.append_message("s1", role="tool", content="ok", tool_name="ha_call_service")
        db.append_message("s1", role="tool", content="ok", tool_name="ha_call_service")

        session = db.get_session("s1")
        # Should be 2 (the actual number of tool calls), not 3
        assert session["tool_call_count"] == 2, (
            f"Expected 2 tool calls but got {session['tool_call_count']}. "
            "tool responses are double-counted and multi-call messages are under-counted"
        )

    def test_tool_calls_serialization(self, db):
        db.create_session(session_id="s1", source="cli")
        tool_calls = [{"id": "call_1", "function": {"name": "web_search", "arguments": "{}"}}]
        db.append_message("s1", role="assistant", tool_calls=tool_calls)

        messages = db.get_messages("s1")
        assert messages[0]["tool_calls"] == tool_calls

    def test_tool_effect_disposition_round_trips_through_session_db(self, db):
        from agent.tool_dispatch_helpers import make_tool_result_message

        db.create_session(session_id="s1", source="cli")
        db.replace_messages(
            "s1",
            [make_tool_result_message(
                "write_file", "worker detached", "c1", effect_disposition="unknown"
            )],
        )

        assert db.get_messages_as_conversation("s1")[0]["effect_disposition"] == "unknown"

    def test_tool_response_does_not_increment_tool_count(self, db):
        """Tool responses (role=tool) should not increment tool_call_count.

        Only assistant messages with tool_calls should count.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="tool", content="result", tool_name="web_search")

        session = db.get_session("s1")
        assert session["tool_call_count"] == 0










# =========================================================================
# Timestamp preservation
# =========================================================================


class TestSanitizeTitle:
    """Tests for SessionDB.sanitize_title() validation and cleaning."""

    def test_normal_title_unchanged(self):
        assert SessionDB.sanitize_title("My Project") == "My Project"







    def test_control_chars_stripped(self):
        # Null byte, bell, backspace, etc.
        assert SessionDB.sanitize_title("hello\x00world") == "helloworld"
        assert SessionDB.sanitize_title("\x07\x08test\x1b") == "test"







    def test_exceeds_max_length_raises(self):
        title = "A" * 101
        with pytest.raises(ValueError, match="too long"):
            SessionDB.sanitize_title(title)

    def test_accented_characters_allowed(self):
        assert SessionDB.sanitize_title("Résumé éditing") == "Résumé éditing"

    def test_bom_stripped(self):
        # Byte order mark (U+FEFF)
        assert SessionDB.sanitize_title("\ufeffhello") == "hello"

    def test_cjk_characters_allowed(self):
        assert SessionDB.sanitize_title("我的项目") == "我的项目"

    def test_collapses_internal_whitespace(self):
        assert SessionDB.sanitize_title("hello   world") == "hello world"

    def test_del_char_stripped(self):
        assert SessionDB.sanitize_title("hello\x7fworld") == "helloworld"

    def test_empty_string_returns_none(self):
        assert SessionDB.sanitize_title("") is None

    def test_max_length_allowed(self):
        title = "A" * 100
        assert SessionDB.sanitize_title(title) == title

    def test_none_returns_none(self):
        assert SessionDB.sanitize_title(None) is None

    def test_only_control_chars_returns_none(self):
        assert SessionDB.sanitize_title("\x00\x01\x02\u200b\ufeff") is None

    def test_rtl_override_stripped(self):
        # Right-to-left override (U+202E) — used in filename spoofing attacks
        assert SessionDB.sanitize_title("hello\u202eworld") == "helloworld"

    def test_sanitize_applied_in_set_session_title(self, db):
        """set_session_title applies sanitize_title internally."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "  hello\x00  world  ")
        assert db.get_session("s1")["title"] == "hello world"

    def test_special_punctuation_allowed(self):
        title = "PR #438 — fixing the 'auth' middleware"
        assert SessionDB.sanitize_title(title) == title

    def test_strips_whitespace(self):
        assert SessionDB.sanitize_title("  hello world  ") == "hello world"

    def test_tabs_and_newlines_collapsed(self):
        assert SessionDB.sanitize_title("hello\t\nworld") == "hello world"

    def test_too_long_title_rejected_by_set(self, db):
        """set_session_title raises ValueError for overly long titles."""
        db.create_session("s1", "cli")
        with pytest.raises(ValueError, match="too long"):
            db.set_session_title("s1", "X" * 150)

    def test_unicode_emoji_allowed(self):
        assert SessionDB.sanitize_title("🚀 My Project 🎉") == "🚀 My Project 🎉"

    def test_whitespace_only_returns_none(self):
        assert SessionDB.sanitize_title("   \t\n  ") is None

    def test_zero_width_chars_stripped(self):
        # Zero-width space (U+200B), zero-width joiner (U+200D)
        assert SessionDB.sanitize_title("hello\u200bworld") == "helloworld"
        assert SessionDB.sanitize_title("hello\u200dworld") == "helloworld"








class TestGetMessagesPagination:
    """get_messages(limit=, offset=) pages in insertion order; the default
    (limit=None) returns the full transcript unchanged."""

    def _seed(self, db, n=10):
        db.create_session(session_id="s1", source="cli")
        # One write transaction for the whole seed: per-row append_message
        # pays a commit (and, off WAL, an fsync) per message, which at
        # n=3000 was ~10s of pure seeding before the query under test ran.
        db.append_messages_batch(
            "s1",
            [
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"msg-{i}",
                }
                for i in range(n)
            ],
        )

    def test_default_returns_all_messages(self, db):
        self._seed(db)
        messages = db.get_messages("s1")
        assert [m["content"] for m in messages] == [f"msg-{i}" for i in range(10)]


    def test_window_query_bounded_work(self, db):
        """Perf contract: get_messages_around must seek by index, not scan
        the session's whole message history. Measured behaviorally via
        SQLite progress-handler steps (behavior contracts over snapshots,
        AGENTS.md — no EXPLAIN text). Calibrated on this seed (3000
        messages): indexed = ~12 handler calls, unindexed full-session
        scan = ~855. Threshold 300: >25x headroom above the indexed path,
        ~3x below the scan. Same pattern as the loader call-count pins in
        tests/tools/test_approval_config_readonly.py."""
        self._seed(db, n=3000)
        mid = db.get_messages("s1", limit=1, offset=1500)[0]["id"]
        steps = [0]

        def progress():
            steps[0] += 1
            return 0

        db._conn.set_progress_handler(progress, 100)
        try:
            db.get_messages_around("s1", mid, window=20)
        finally:
            db._conn.set_progress_handler(None, 0)
        assert steps[0] < 300, (
            f"get_messages_around executed {steps[0]}x100 VM steps — the "
            "session-history scan is back (idx_messages_session_id missing "
            "or unused)")


    def test_window_results_identical_with_and_without_index(self, db):
        """The index must not change results: identical windows at probe
        points across the session, with and without it."""
        self._seed(db, n=500)
        ids = [m["id"] for m in db.get_messages("s1")]
        probes = (ids[0], ids[len(ids) // 2], ids[-1])
        with_index = [db.get_messages_around("s1", mid, window=5)
                      for mid in probes]
        db._conn.execute("DROP INDEX idx_messages_session_id")
        without_index = [db.get_messages_around("s1", mid, window=5)
                         for mid in probes]
        assert with_index == without_index

    def test_limit_pages_in_insertion_order(self, db):
        self._seed(db)
        page1 = db.get_messages("s1", limit=4, offset=0)
        page2 = db.get_messages("s1", limit=4, offset=4)
        page3 = db.get_messages("s1", limit=4, offset=8)
        assert [m["content"] for m in page1] == ["msg-0", "msg-1", "msg-2", "msg-3"]
        assert [m["content"] for m in page2] == ["msg-4", "msg-5", "msg-6", "msg-7"]
        assert [m["content"] for m in page3] == ["msg-8", "msg-9"]

    def test_offset_past_end_returns_empty(self, db):
        self._seed(db, n=3)
        assert db.get_messages("s1", limit=5, offset=10) == []

    def test_offset_without_limit_pages(self, db):
        """offset alone must not be silently ignored (review finding):
        SQLite needs LIMIT for OFFSET, emitted as LIMIT -1."""
        self._seed(db, n=5)
        rows = db.get_messages("s1", offset=3)
        assert [m["content"] for m in rows] == ["msg-3", "msg-4"]

    def test_pagination_respects_active_flag(self, db):
        """Soft-deleted (inactive) rows must not consume page slots."""
        self._seed(db, n=6)
        # Soft-delete the first two rows the way rewind does.
        db._conn.execute(
            "UPDATE messages SET active = 0 WHERE session_id = 's1' "
            "AND id IN (SELECT id FROM messages WHERE session_id = 's1' ORDER BY id LIMIT 2)"
        )
        db._conn.commit()
        page = db.get_messages("s1", limit=3, offset=0)
        assert [m["content"] for m in page] == ["msg-2", "msg-3", "msg-4"]

    def test_latest_pages_count_back_from_newest_but_remain_chronological(self, db):
        self._seed(db)
        page1 = db.get_messages("s1", limit=4, offset=0, latest=True)
        page2 = db.get_messages("s1", limit=4, offset=4, latest=True)
        page3 = db.get_messages("s1", limit=4, offset=8, latest=True)
        assert [m["content"] for m in page1] == ["msg-6", "msg-7", "msg-8", "msg-9"]
        assert [m["content"] for m in page2] == ["msg-2", "msg-3", "msg-4", "msg-5"]
        assert [m["content"] for m in page3] == ["msg-0", "msg-1"]

    def test_after_id_keyset_pages_forward_in_insertion_order(self, db):
        self._seed(db)
        page1 = db.get_messages("s1", limit=4, after_id=0)
        assert [m["content"] for m in page1] == ["msg-0", "msg-1", "msg-2", "msg-3"]
        page2 = db.get_messages("s1", limit=4, after_id=page1[-1]["id"])
        assert [m["content"] for m in page2] == ["msg-4", "msg-5", "msg-6", "msg-7"]
        page3 = db.get_messages("s1", limit=4, after_id=page2[-1]["id"])
        assert [m["content"] for m in page3] == ["msg-8", "msg-9"]
        with pytest.raises(ValueError):
            db.get_messages("s1", limit=4, after_id=0, latest=True)
        with pytest.raises(ValueError):
            db.get_messages("s1", limit=4, after_id=0, offset=2)

    def test_resume_safety_counts_active_rows_across_lineage(self, db):
        db.create_session(session_id="root", source="cli")
        db.append_messages_batch(
            "root",
            [{"role": "user", "content": f"root-{i}"} for i in range(3)],
        )
        db.create_session(
            session_id="tip",
            source="compression",
            parent_session_id="root",
        )
        db.append_messages_batch(
            "tip",
            [{"role": "assistant", "content": f"tip-{i}"} for i in range(2)],
        )

        assert db.get_resume_message_count("tip") == 5
        with pytest.raises(hermes_state.SessionResumeTooLargeError) as exc_info:
            db.assert_resume_safe("tip", max_messages=4)
        assert exc_info.value.message_count == 5
        assert exc_info.value.limit == 4

    def test_export_safety_is_bounded_to_the_requested_active_segment(self, db):
        db.create_session(session_id="root", source="cli")
        db.append_messages_batch(
            "root",
            [{"role": "user", "content": f"root-{i}"} for i in range(3)],
        )
        db.create_session(
            session_id="tip",
            source="compression",
            parent_session_id="root",
        )
        db.append_messages_batch(
            "tip",
            [{"role": "assistant", "content": f"tip-{i}"} for i in range(2)],
        )

        assert db.assert_export_safe("tip", max_messages=2) == 2
        with pytest.raises(hermes_state.SessionExportTooLargeError) as exc_info:
            db.assert_export_safe("root", max_messages=2)
        assert exc_info.value.session_id == "root"
        assert exc_info.value.message_count == 3
        assert exc_info.value.limit == 2

    def test_zero_limit_disables_resume_and_export_guards(self, db, monkeypatch):
        """sessions.max_*_messages: 0 disables the guard entirely."""
        db.create_session(session_id="big", source="cli")
        db.append_messages_batch(
            "big",
            [{"role": "user", "content": f"msg-{i}"} for i in range(5)],
        )

        # A small explicit limit rejects...
        with pytest.raises(hermes_state.SessionResumeTooLargeError):
            db.assert_resume_safe("big", max_messages=2)
        with pytest.raises(hermes_state.SessionExportTooLargeError):
            db.assert_export_safe("big", max_messages=2)

        # ...but a config-resolved limit of 0 disables both guards: no raise,
        # and no counting work at all (returns 0 — callers use the raise side
        # effect only).
        monkeypatch.setattr(hermes_state, "resolved_max_resume_messages", lambda: 0)
        monkeypatch.setattr(hermes_state, "resolved_max_export_messages", lambda: 0)
        assert db.assert_resume_safe("big") == 0
        assert db.assert_export_safe("big") == 0
        # An explicit 0 disables too, independent of config.
        assert db.assert_resume_safe("big", max_messages=0) == 0
        assert db.assert_export_safe("big", max_messages=0) == 0

    def test_guard_limits_resolve_from_config_at_call_time(self, db, monkeypatch):
        db.create_session(session_id="cfg", source="cli")
        db.append_messages_batch(
            "cfg",
            [{"role": "user", "content": f"msg-{i}"} for i in range(4)],
        )

        monkeypatch.setattr(hermes_state, "resolved_max_resume_messages", lambda: 3)
        monkeypatch.setattr(hermes_state, "resolved_max_export_messages", lambda: 3)
        with pytest.raises(hermes_state.SessionResumeTooLargeError) as resume_exc:
            db.assert_resume_safe("cfg")
        assert resume_exc.value.limit == 3
        with pytest.raises(hermes_state.SessionExportTooLargeError) as export_exc:
            db.assert_export_safe("cfg")
        assert export_exc.value.limit == 3





# =========================================================================
# Lone-surrogate persistence
# =========================================================================

class TestTimestampPreservation:
    """Tests for the timestamp preservation feature.

    ``append_message()`` and ``replace_messages()`` now accept/forward an
    optional ``timestamp`` parameter.  These tests verify custom timestamps
    survive the round trip through the DB and fall back to ``time.time()``
    when omitted.
    """

    @staticmethod
    def _build_messages(ts_list, contents=None, roles=None):
        """Build message dicts with explicit timestamps for testing."""
        if contents is None:
            contents = [f"msg-{i}" for i in range(len(ts_list))]
        if roles is None:
            roles = ["user", "assistant"] * (len(ts_list) // 2 + 1)
        return [
            {"role": roles[i], "content": contents[i], "timestamp": ts}
            for i, ts in enumerate(ts_list)
        ]

    def _raw_timestamps(self, db, session_id):
        """Query timestamp column directly from SQLite for verification."""
        rows = db._conn.execute(
            "SELECT timestamp FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def test_append_message_with_explicit_timestamp(self, db):
        """A caller-supplied timestamp is stored and round-tripped."""
        db.create_session(session_id="s1", source="cli")
        ts = 1_234_567.0
        mid = db.append_message("s1", role="user", content="hello",
                                timestamp=ts)
        msgs = db.get_messages("s1")
        assert len(msgs) == 1
        assert msgs[0]["timestamp"] == ts
        assert msgs[0]["id"] == mid
        raw = self._raw_timestamps(db, "s1")
        assert raw == [ts]




    def test_replace_messages_preserves_timestamps(self, db):
        """Message dicts with ``timestamp`` passed to ``replace_messages``
        retain those timestamps after the rewrite."""
        db.create_session(session_id="s1", source="cli")
        msgs_in = [
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 200.0},
            {"role": "user", "content": "third", "timestamp": 300.0},
        ]
        db.replace_messages("s1", msgs_in)
        msgs_out = db.get_messages("s1")
        assert [m["timestamp"] for m in msgs_out] == [100.0, 200.0, 300.0]
        assert self._raw_timestamps(db, "s1") == [100.0, 200.0, 300.0]





    def test_compression_replace_roundtrip_preserves_timestamps(self, db):
        """Compression-style rewrite: replace_messages with dicts loaded from
        get_messages_as_conversation must keep the surviving messages'
        original timestamps (#28841)."""
        timestamps = [1_500_000_000.0, 1_500_000_100.0, 1_500_000_200.0]
        db.create_session(session_id="s1", source="cli")
        for i, ts in enumerate(timestamps):
            db.append_message(
                "s1",
                role="user" if i % 2 == 0 else "assistant",
                content=f"msg-{i}",
                timestamp=ts,
            )

        history = db.get_messages_as_conversation("s1", include_timestamp=True)
        # Simulate a compression that keeps the last two turns verbatim and
        # prepends a fresh summary message (no timestamp — falls back to now).
        compressed = [{"role": "user", "content": "[summary]"}] + history[-2:]
        db.replace_messages("s1", compressed)

        raw = self._raw_timestamps(db, "s1")
        assert len(raw) == 3
        assert raw[1:] == timestamps[-2:]
        assert raw[0] > timestamps[-1]  # summary stamped with a current time

    def test_append_message_mixed_timestamps(self, db):
        """Messages with and without explicit timestamps — those without
        get a current time, those with keep their value."""
        db.create_session(session_id="s1", source="cli")
        explicit_ts = 500_000.0
        db.append_message("s1", role="user", content="explicit",
                          timestamp=explicit_ts)
        before = time.time()
        db.append_message("s1", role="user", content="default")
        after = time.time()
        msgs = db.get_messages("s1")
        assert msgs[0]["timestamp"] == explicit_ts
        assert before <= msgs[1]["timestamp"] <= after
        raw = self._raw_timestamps(db, "s1")
        assert raw[0] == explicit_ts
        assert before <= raw[1] <= after

    def test_append_message_multiple_timestamps(self, db):
        """Multiple messages with different explicit timestamps."""
        db.create_session(session_id="s1", source="cli")
        timestamps = [1_000_000.0, 2_000_000.0, 3_000_000.0]
        for i, ts in enumerate(timestamps, 1):
            db.append_message("s1", role="user", content=f"msg {i}",
                              timestamp=ts)
        msgs = db.get_messages("s1")
        assert [m["timestamp"] for m in msgs] == timestamps
        assert self._raw_timestamps(db, "s1") == timestamps

    def test_append_message_without_timestamp_defaults(self, db):
        """Omitting timestamp stores a recent time.time() value."""
        db.create_session(session_id="s1", source="cli")
        before = time.time()
        db.append_message("s1", role="user", content="hello")
        after = time.time()
        msgs = db.get_messages("s1")
        stored = msgs[0]["timestamp"]
        assert before <= stored <= after, (
            f"Expected timestamp between {before} and {after}, got {stored}"
        )
        raw_stored = self._raw_timestamps(db, "s1")[0]
        assert before <= raw_stored <= after

    def test_branch_copy_roundtrip_preserves_timestamps(self, db):
        """End-to-end branch copy: load the parent transcript via
        get_messages_as_conversation(include_timestamp=True) — the same shape
        the CLI/gateway/TUI branch restore paths use (session.py load_transcript
        + cli_agent_setup_mixin both pass include_timestamp=True; timestamp is
        opt-in so it never reaches a model payload / prompt-cache key) — and
        re-append into a child forwarding ``msg.get("timestamp")``: the copies
        must keep the originals instead of being restamped with time.time()
        (#28841).
        """
        timestamps = [1_600_000_000.0, 1_600_000_060.0, 1_600_000_120.0]
        db.create_session(session_id="parent", source="cli")
        for i, ts in enumerate(timestamps):
            db.append_message(
                "parent",
                role="user" if i % 2 == 0 else "assistant",
                content=f"msg-{i}",
                timestamp=ts,
            )

        history = db.get_messages_as_conversation("parent", include_timestamp=True)
        assert [m.get("timestamp") for m in history] == timestamps

        db.create_session(session_id="child", source="cli",
                          parent_session_id="parent")
        # Mirrors the branch copy loops in gateway/slash_commands.py,
        # hermes_cli/cli_commands_mixin.py and tui_gateway/server.py.
        for msg in history:
            db.append_message(
                "child",
                role=msg.get("role", "user"),
                content=msg.get("content"),
                timestamp=msg.get("timestamp"),
            )

        assert self._raw_timestamps(db, "child") == timestamps

    def test_fork_chain_preserves_timestamps(self, db):
        """Simulate a /branch fork: copy messages from parent to child,
        verify timestamps are identical in both via raw SQL."""
        base_ts = 1_700_000_000.0
        timestamps = [base_ts + i * 20 for i in range(5)]
        contents = [
            "how do I fix a TypeError?",
            "show me the traceback",
            "TypeError at line 42",
            "issue in utils.py",
            "try int(...)",
        ]
        roles = ["user", "assistant", "tool", "user", "assistant"]
        parent_msgs = self._build_messages(timestamps, contents, roles)

        db.create_session(session_id="parent", source="cli")
        for msg in parent_msgs:
            db.append_message("parent", role=msg["role"],
                              content=msg["content"],
                              timestamp=msg["timestamp"])

        db.create_session(session_id="child", source="cli",
                          parent_session_id="parent")
        for msg in parent_msgs:
            db.append_message("child", role=msg["role"],
                              content=msg["content"],
                              timestamp=msg["timestamp"])

        parent_raw = self._raw_timestamps(db, "parent")
        child_raw = self._raw_timestamps(db, "child")
        assert parent_raw == timestamps
        assert child_raw == timestamps
        assert parent_raw == child_raw

    def test_replace_messages_fallback_when_no_timestamp(self, db):
        """Message dicts without ``timestamp`` get auto-incrementing
        fallback values (starting from ~time.time())."""
        db.create_session(session_id="s1", source="cli")
        db.replace_messages("s1", [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
        ])
        msgs = db.get_messages("s1")
        assert len(msgs) == 2
        t0, t1 = msgs[0]["timestamp"], msgs[1]["timestamp"]
        assert t1 > t0
        raw = self._raw_timestamps(db, "s1")
        assert len(raw) == 2
        assert raw[1] > raw[0]

    def test_replace_messages_mixed_timestamps(self, db):
        """Some messages with timestamp, some without — a message without
        timestamp uses the fallback clock, which is larger than any
        explicit historical timestamp."""
        db.create_session(session_id="s1", source="cli")
        old_ts = 1_000.0
        db.replace_messages("s1", [
            {"role": "user", "content": "old", "timestamp": old_ts},
            {"role": "user", "content": "new"},
        ])
        msgs = db.get_messages("s1")
        assert msgs[0]["timestamp"] == old_ts
        assert msgs[1]["timestamp"] > old_ts
        raw = self._raw_timestamps(db, "s1")
        assert raw[0] == old_ts
        assert raw[1] > old_ts


# =========================================================================
# FTS5 search
# =========================================================================

class TestLoneSurrogatePersistence:
    """sqlite3 encodes bound str params as UTF-8 and raises UnicodeEncodeError
    on lone surrogates (U+D800..U+DFFF). Tool results scraped from the web can
    carry them, so a single such code point aborted the whole message write —
    and because run_agent swallows the failure with a warning, the session then
    silently stopped persisting for the rest of its life.
    """

    DIRTY = "scraped \ud835 price"

    def test_append_message_survives_lone_surrogate_content(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", "assistant", "hello world")
        db.append_message("s1", "tool", self.DIRTY, tool_name="web_search")

        rows = db.get_messages("s1")
        assert len(rows) == 2
        # Surrogate replaced with U+FFFD; the surrounding text is intact.
        assert rows[1]["content"] == "scraped � price"




    # -- sibling raw-str bind sites (follow-up widening of the same bug class)




    def test_set_latest_user_api_content_survives_lone_surrogate(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", "user", "turn text")
        assert db.set_latest_user_api_content("s1", "turn text", self.DIRTY) == 1

    def test_append_message_survives_lone_surrogate_api_content(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", "user", "clean", api_content=self.DIRTY)
        assert db.get_messages("s1")[0]["api_content"] == "scraped \ufffd price"

    def test_append_message_survives_lone_surrogate_reasoning(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", "assistant", "fine", reasoning=self.DIRTY)
        assert len(db.get_messages("s1")) == 1

    def test_append_message_survives_lone_surrogate_tool_name(self, db):
        db.create_session("s1", source="cli")
        db.append_message("s1", "tool", "ok", tool_name="web\ud835search")
        assert len(db.get_messages("s1")) == 1

    def test_replace_messages_keeps_persisting_after_dirty_row(self, db):
        """The regression that mattered: one poisoned row froze the session.

        replace_messages re-sends the full history each turn, so once a dirty
        tool result entered it, every later save raised and nothing after it
        was ever written.
        """
        db.create_session("s1", source="cli")
        history = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "tool", "content": self.DIRTY, "tool_name": "web_search"},
            {"role": "assistant", "content": "answer 2"},
        ]
        db.replace_messages("s1", history)
        assert len(db.get_messages("s1")) == 4

        # Later turns still persist rather than freezing at the poisoned row.
        history += [
            {"role": "user", "content": "turn 3"},
            {"role": "assistant", "content": "answer 3"},
        ]
        db.replace_messages("s1", history)
        rows = db.get_messages("s1")
        assert len(rows) == 6
        assert rows[-1]["content"] == "answer 3"

    def test_replace_messages_survives_lone_surrogate_api_content(self, db):
        db.create_session("s1", source="cli")
        db.replace_messages(
            "s1", [{"role": "user", "content": "u1", "api_content": self.DIRTY}]
        )
        assert db.get_messages("s1")[0]["api_content"] == "scraped \ufffd price"

    def test_session_title_survives_lone_surrogate(self, db):
        db.create_session("s1", source="cli")
        assert db.set_session_title("s1", "title \ud835 bad") is True
        assert db.get_session("s1")["title"] == "title \ufffd bad"

    def test_well_formed_unicode_is_unchanged(self, db):
        """Accents, CJK and emoji must round-trip byte-identically."""
        db.create_session("s1", source="cli")
        benign = "Ünïcödé ok — 日本語 🎉 emoji fine"
        db.append_message("s1", "assistant", benign)
        assert db.get_messages("s1")[0]["content"] == benign



class TestOptimizeFts:
    def test_optimize_returns_index_count(self, db):
        """A fresh DB has both FTS indexes; optimize merges both."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(session_id="s1", role="user", content="hello world")
        statements = []
        db._conn.set_trace_callback(statements.append)
        try:
            assert db.optimize_fts() == 2
        finally:
            db._conn.set_trace_callback(None)
        optimize_sql = [sql for sql in statements if "'optimize'" in sql]
        assert len(optimize_sql) == 2
        assert not any("'merge'" in sql for sql in optimize_sql)




    def test_incremental_merge_bounded_commands_per_present_index(self, db):
        """Each pass issues bounded 'merge' commands, never 'optimize'."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(session_id="s1", role="user", content="bounded merge")
        statements = []
        db._conn.set_trace_callback(statements.append)
        try:
            executed = db._merge_fts_incrementally(max_pages=37)
        finally:
            db._conn.set_trace_callback(None)

        # At least one merge command per present FTS index, and never more
        # than the per-pass command cap per index.
        present = [t for t in db._FTS_TABLES if db._fts_table_exists(t)]
        assert len(present) >= 2  # messages_fts + trigram on a fresh DB
        merge_sql = [sql for sql in statements if "VALUES('merge', 37)" in sql]
        assert len(merge_sql) == executed
        assert len(present) <= executed <= (
            len(present) * db._FTS_MERGE_COMMANDS_PER_PASS
        )
        for tbl in present:
            n = sum(f"{tbl}({tbl}, rank)" in sql for sql in merge_sql)
            assert 1 <= n <= db._FTS_MERGE_COMMANDS_PER_PASS
        # The usermerge floor is applied so positive merges can make
        # progress on levels with >= 2 segments (SQLite FTS5 §6.8).
        assert any("VALUES('usermerge', 2)" in sql for sql in statements)
        assert not any("'optimize'" in sql for sql in statements)





    def test_write_path_merges_fts_only_at_cadence_boundary(self, db, monkeypatch):
        """Routine writes use bounded merge and never full optimize."""
        db._FTS_MERGE_EVERY_N_WRITES = 5
        calls = []

        def _counting_merge(*, max_pages):
            calls.append(max_pages)
            return 0

        def _unexpected_optimize():
            raise AssertionError("routine cadence must not call optimize")

        monkeypatch.setattr(db, "_merge_fts_incrementally", _counting_merge)
        monkeypatch.setattr(db, "optimize_fts", _unexpected_optimize)
        db.create_session(session_id="s1", source="cli")
        for i in range(3):
            db.append_message(session_id="s1", role="user", content=f"needle {i}")
        assert calls == []  # Four successful writes are below the boundary.
        db.append_message(session_id="s1", role="user", content="needle 3")
        assert calls == [500]  # The fifth write gets the production page budget.
        for i in range(4, 8):
            db.append_message(session_id="s1", role="user", content=f"needle {i}")
        assert calls == [500]
        db.append_message(session_id="s1", role="user", content="needle 8")
        assert calls == [500, 500]  # The tenth write is the next boundary.
        assert len(db.search_messages("needle")) == 9

    def test_optimize_idempotent(self, db):
        """Running optimize twice is safe (second pass is a no-op merge)."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(session_id="s1", role="user", content="repeat me")
        assert db.optimize_fts() == 2
        assert db.optimize_fts() == 2
        # Search still works after repeated optimization.
        assert len(db.search_messages("repeat")) == 1

    def test_optimize_preserves_search_and_snippet(self, db):
        """Optimize is layout-only: MATCH results + snippets are unchanged."""
        db.create_session(session_id="s1", source="cli")
        for i in range(50):
            db.append_message(
                session_id="s1",
                role="user",
                content=f"needle alpha bravo charlie message {i}",
            )
        before = db.search_messages("needle")
        n = db.optimize_fts()
        assert n == 2
        after = db.search_messages("needle")
        assert len(after) == len(before)
        assert len(after) > 0
        # Snippet must still be populated (would be empty/None if the FTS
        # content shadow were lost during optimize).
        assert all(row.get("snippet") for row in after)
        # IDs and snippets are identical before/after — pure layout change.
        assert [r["id"] for r in after] == [r["id"] for r in before]
        assert [r["snippet"] for r in after] == [r["snippet"] for r in before]

    def test_optimize_skips_missing_trigram_table(self, db):
        """When the trigram index is absent, optimize handles only the porter
        index and does not raise."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(session_id="s1", role="user", content="hello")
        # Drop the trigram table + triggers to simulate a disabled/absent index.
        with db._lock:
            for trig in (
                "messages_fts_trigram_insert",
                "messages_fts_trigram_delete",
                "messages_fts_trigram_update",
            ):
                db._conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
            db._conn.execute("DROP TABLE IF EXISTS messages_fts_trigram")
        assert db._fts_table_exists("messages_fts_trigram") is False
        assert db._fts_table_exists("messages_fts") is True
        # Only the porter index remains -> 1 optimized, no error.
        assert db.optimize_fts() == 1

    def test_write_path_optimize_failure_never_breaks_write(self, db, monkeypatch):
        """A failing periodic optimize must not fail the surrounding write."""
        db._FTS_MERGE_EVERY_N_WRITES = 2

        def _boom():
            raise sqlite3.OperationalError("simulated optimize failure")

        # Parity merge: cadence maintenance is now _try_incremental_merge_fts.
        monkeypatch.setattr(db, "_merge_fts_incrementally", lambda **_kw: _boom())
        db.create_session(session_id="s1", source="cli")  # write #1
        # write #2 trips the cadence; the swallowed failure must not propagate.
        db.append_message(session_id="s1", role="user", content="still persists")
        assert len(db.get_messages("s1")) == 1

    def test_write_path_optimizes_fts_on_cadence(self, db, monkeypatch):
        """Writes periodically merge FTS segments so they never accumulate
        into the tens-of-thousands that lengthen the write-lock hold and
        starve competing writers ("database is locked").

        Parity merge 2026-08-08: upstream REPLACED the periodic full
        ``optimize_fts()`` with a bounded ``_try_incremental_merge_fts()`` on
        ``_FTS_MERGE_EVERY_N_WRITES`` — strictly better (capped pages per pass,
        and it logs unexpected sqlite errors instead of swallowing them). The
        invariant under test is unchanged: the WRITE PATH must merge segments on
        a cadence. Assert that behaviour, not the old method name.
        """
        db._FTS_MERGE_EVERY_N_WRITES = 5
        calls = {"n": 0}
        real_merge = db._try_incremental_merge_fts

        def _counting_merge():
            calls["n"] += 1
            return real_merge()

        monkeypatch.setattr(db, "_try_incremental_merge_fts", _counting_merge)
        # create_session is write #1; appends are #2.. -> #5 and #10 trigger.
        db.create_session(session_id="s1", source="cli")
        for i in range(9):
            db.append_message(session_id="s1", role="user", content=f"needle {i}")
        assert calls["n"] == 2
        # The auto-merge is layout-only: search is unaffected.
        assert len(db.search_messages("needle")) == 9



class TestFts5SanitizerCharacterClass:
    """Every character FTS5 rejects outside a quoted phrase must be stripped.

    A survivor reaches MATCH raw and raises, which the execute site swallows
    into zero results — so the search silently finds nothing rather than
    erroring. Assertions run the sanitized text against a real FTS5 table.
    """

    @staticmethod
    def _fts_table():
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
        conn.execute(
            "INSERT INTO t (content) VALUES "
            "('meet me at user host about gateway run py it s 50 a b')"
        )
        return conn

    @staticmethod
    def _sanitize(query):
        from hermes_state_search import SessionSearchMixin

        return SessionSearchMixin._sanitize_fts5_query(query)

    @pytest.mark.parametrize(
        "query",
        [
            "it's",                 # apostrophe — ordinary prose
            "gateway/run.py",       # path separator
            "user@host",            # email / handle
            "a,b",                  # comma
            "why?",                 # question mark
            "e=mc2",                # equals
            "a;b", "a!b", "a&b", "a|b", "x~y",
            "#tag", "$dollar", "[bracket]", "<tag>",
            r"C:\path\file",        # backslash
        ],
    )
    def test_query_stays_parsable(self, query):
        conn = self._fts_table()
        sanitized = self._sanitize(query)
        if not sanitized.strip():
            return
        # Raises sqlite3.OperationalError if a special character survived.
        conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (sanitized,)).fetchone()

    def test_plain_terms_are_untouched(self):
        assert self._sanitize("hello world").split() == ["hello", "world"]

    def test_quoted_phrase_survives(self):
        assert '"exact phrase"' in self._sanitize('"exact phrase"')

    def test_hyphen_dotted_term_still_quoted(self):
        # Step 5's behaviour must not regress: my-app.config.ts stays one term.
        assert '"my-app.config.ts"' in self._sanitize("my-app.config.ts")

    def test_prefix_star_still_works(self):
        conn = self._fts_table()
        sanitized = self._sanitize("gate*")
        rows = conn.execute(
            "SELECT count(*) FROM t WHERE t MATCH ?", (sanitized,)
        ).fetchone()
        assert rows[0] == 1

    def test_percent_stripped_for_non_cjk_query(self):
        # % is kept only for the CJK LIKE fallback; a non-CJK query never
        # reaches that fallback, so % must be stripped before MATCH.
        conn = self._fts_table()
        sanitized = self._sanitize("50%")
        assert "%" not in sanitized
        conn.execute(
            "SELECT count(*) FROM t WHERE t MATCH ?", (sanitized,)
        ).fetchone()

    def test_percent_preserved_for_cjk_query(self):
        # The CJK LIKE fallback builds its own pattern from the sanitized
        # text; keep % intact there (pre-existing contract).
        sanitized = self._sanitize("完成50%")
        assert "%" in sanitized
class TestApplyDatabasePragmas:
    """Config-driven WAL-sizing pragma application (database: section)."""

    @staticmethod
    def _patch_cfg(monkeypatch, cfg):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: cfg,
        )

    def test_honors_wal_autocheckpoint_from_config(self, tmp_path, monkeypatch):
        import sqlite3
        from hermes_state import apply_database_pragmas

        conn = sqlite3.connect(str(tmp_path / "pragmas.db"))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            self._patch_cfg(monkeypatch, {"database": {"wal_autocheckpoint": 250}})
            apply_database_pragmas(conn, db_label="test.db")
            assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 250
        finally:
            conn.close()

    def test_honors_journal_size_limit_from_config(self, tmp_path, monkeypatch):
        import sqlite3
        from hermes_state import apply_database_pragmas

        conn = sqlite3.connect(str(tmp_path / "pragmas.db"))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            self._patch_cfg(
                monkeypatch, {"database": {"journal_size_limit": 10485760}}
            )
            apply_database_pragmas(conn, db_label="test.db")
            assert (
                conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 10485760
            )
        finally:
            conn.close()

    def test_noop_when_database_section_missing(self, tmp_path, monkeypatch):
        import sqlite3
        from hermes_state import apply_database_pragmas

        conn = sqlite3.connect(str(tmp_path / "pragmas.db"))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            self._patch_cfg(monkeypatch, {})
            apply_database_pragmas(conn, db_label="test.db")
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            conn.close()

    def test_never_touches_journal_mode(self, tmp_path, monkeypatch):
        """journal_mode is owned by apply_wal_with_fallback — a database:
        journal_mode entry must NOT cause a second, unguarded mode switch."""
        import sqlite3
        from hermes_state import apply_database_pragmas

        conn = sqlite3.connect(str(tmp_path / "pragmas.db"))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            self._patch_cfg(monkeypatch, {"database": {"journal_mode": "delete"}})
            apply_database_pragmas(conn, db_label="test.db")
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()

    def test_ignores_non_integer_values(self, tmp_path, monkeypatch):
        import sqlite3
        from hermes_state import apply_database_pragmas

        conn = sqlite3.connect(str(tmp_path / "pragmas.db"))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            before = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            self._patch_cfg(
                monkeypatch, {"database": {"wal_autocheckpoint": "lots"}}
            )
            apply_database_pragmas(conn, db_label="test.db")
            assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == before
        finally:
            conn.close()

    def test_ignores_non_integer_performance_values(self, tmp_path, monkeypatch):
        """Garbage cache_size/mmap_size/temp_store values must be rejected."""
        import sqlite3
        from hermes_state import apply_database_pragmas

        conn = sqlite3.connect(str(tmp_path / "pragmas.db"))
        try:
            before = {
                name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                for name in ("cache_size", "mmap_size", "temp_store")
            }
            self._patch_cfg(
                monkeypatch,
                {
                    "database": {
                        "cache_size": "big",
                        "mmap_size": [256],
                        "temp_store": "ram please",
                    }
                },
            )
            apply_database_pragmas(conn, db_label="test.db")
            after = {
                name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                for name in ("cache_size", "mmap_size", "temp_store")
            }
            assert after == before
        finally:
            conn.close()


class TestSessionTitleLineage:
    """Renaming a compression continuation back to its base title must succeed
    by transferring the title off the ended, hidden predecessor.

    After a context compaction the original session is ended and projected
    behind its live tip in the session list (list_sessions_rich), so the user
    cannot see or free it. Without lineage-aware handling, renaming the visible
    tip back to the base name dead-ends with "already in use by <session they
    can't find>".
    """

    def _make_compression_chain(self, db, t0, *, root="root", tip="tip"):
        db.create_session(root, "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, root))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
            (t0 + 100, root),
        )
        db.create_session(tip, "cli", parent_session_id=root)
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 200, tip))
        db._conn.commit()

    def test_rename_continuation_back_to_base_transfers_title(self, db):
        import time as _time
        self._make_compression_chain(db, _time.time() - 3600)
        db.set_session_title("root", "fingerprint-scanner")
        db.set_session_title("tip", "fingerprint-scanner #2")

        # User renames the visible tip back to the base name — must succeed.
        assert db.set_session_title("tip", "fingerprint-scanner") is True
        assert db.get_session("tip")["title"] == "fingerprint-scanner"
        # Title transferred off the hidden ancestor — no duplicate titles.
        assert db.get_session("root")["title"] is None


    def test_unrelated_session_still_conflicts(self, db):
        db.create_session("a", "cli")
        db.create_session("b", "cli")
        db.set_session_title("a", "shared")
        with pytest.raises(ValueError, match="already in use"):
            db.set_session_title("b", "shared")
        # The unrelated holder keeps its title.
        assert db.get_session("a")["title"] == "shared"

    def test_non_compression_child_still_conflicts(self, db):
        """A child whose parent did NOT end via compression (delegate/branch
        spawned while the parent was live) is not a continuation, so renaming it
        to the parent's title must still raise."""
        import time as _time
        t0 = _time.time() - 3600
        db.create_session("parent", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "parent"))
        db.create_session("child", "cli", parent_session_id="parent")
        # Child started BEFORE parent ended, and parent ended for a non-
        # compression reason — not a continuation edge.
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 10, "child"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='user_exit' WHERE id=?",
            (t0 + 100, "parent"),
        )
        db._conn.commit()
        db.set_session_title("parent", "shared")
        with pytest.raises(ValueError, match="already in use"):
            db.set_session_title("child", "shared")

    def test_transfer_walks_multi_level_chain(self, db):
        import time as _time
        t0 = _time.time() - 7200
        # root (compression) -> mid (compression) -> tip
        self._make_compression_chain(db, t0, root="root", tip="mid")
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
            (t0 + 300, "mid"),
        )
        db.create_session("tip", "cli", parent_session_id="mid")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 400, "tip"))
        db._conn.commit()

        db.set_session_title("root", "deep-dive")
        assert db.set_session_title("tip", "deep-dive") is True
        assert db.get_session("tip")["title"] == "deep-dive"
        assert db.get_session("root")["title"] is None



class TestSessionArchive:
    """Soft-archiving hides a session from default listings without deleting it."""

    def _seed(self, db, sid, *, archived=False):
        db.create_session(session_id=sid, source="cli")
        db.append_message(session_id=sid, role="user", content=f"hello from {sid}")
        if archived:
            db.set_session_archived(sid, True)

    def test_set_session_archived_roundtrip(self, db):
        self._seed(db, "s1")
        assert db.set_session_archived("s1", True) is True
        assert db.get_session("s1")["archived"] == 1
        assert db.set_session_archived("s1", False) is True
        assert db.get_session("s1")["archived"] == 0


    def test_archived_excluded_by_default(self, db):
        self._seed(db, "live")
        self._seed(db, "hidden", archived=True)

        ids = [s["id"] for s in db.list_sessions_rich()]
        assert ids == ["live"]
        assert db.session_count() == 1

    def test_archived_only_and_include(self, db):
        self._seed(db, "live")
        self._seed(db, "hidden", archived=True)

        only = [s["id"] for s in db.list_sessions_rich(archived_only=True)]
        assert only == ["hidden"]
        assert db.session_count(archived_only=True) == 1

        both = {s["id"] for s in db.list_sessions_rich(include_archived=True)}
        assert both == {"live", "hidden"}
        assert db.session_count(include_archived=True) == 2

    def test_set_session_archived_missing_row(self, db):
        assert db.set_session_archived("nope", True) is False



class TestStateMeta:
    def test_get_meta_missing_returns_none(self, db):
        assert db.get_meta("nonexistent") is None

    def test_set_then_get_meta(self, db):
        db.set_meta("foo", "bar")
        assert db.get_meta("foo") == "bar"

    def test_set_meta_upsert(self, db):
        """set_meta overwrites existing value (ON CONFLICT DO UPDATE)."""
        db.set_meta("key", "v1")
        db.set_meta("key", "v2")
        assert db.get_meta("key") == "v2"



class TestInsightsToolCallIndex:
    """The Insights assistant tool-call scan has a predicate-aligned index.

    ``InsightsEngine._get_tool_usage`` / ``_get_skill_usage`` filter messages by
    ``role = 'assistant' AND tool_calls IS NOT NULL``.  A partial index over that
    predicate keeps the scan off the full ``messages`` table on a large state.db.
    """

    _INDEX = "idx_messages_assistant_calls_by_session"

    def _index_defn(self, conn):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (self._INDEX,),
        ).fetchone()
        return row["sql"] if row else None

    def test_index_created_on_fresh_db(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            sql = self._index_defn(db._conn)
            assert sql is not None, "partial index missing on a fresh database"
            # Partial predicate must match the queried rows exactly.
            assert "role = 'assistant'" in sql
            assert "tool_calls IS NOT NULL" in sql
        finally:
            db.close()

    def test_index_created_on_existing_db(self, tmp_path):
        """Reopening a DB that predates the index must create it (SCHEMA_SQL is
        re-run on every open; role/tool_calls are original base columns)."""
        db_path = tmp_path / "legacy.db"
        db = SessionDB(db_path=db_path)
        # Simulate a database created before the index shipped.
        db._conn.execute(f"DROP INDEX IF EXISTS {self._INDEX}")
        db._conn.commit()
        assert self._index_defn(db._conn) is None
        db.close()

        db2 = SessionDB(db_path=db_path)
        try:
            assert self._index_defn(db2._conn) is not None, (
                "index not recreated when reopening an existing database"
            )
        finally:
            db2.close()

    def test_index_predicate_is_partial(self, db):
        """The index covers only the assistant tool-call rows Insights reads.

        Query-plan coverage (that the Insights queries actually select this
        index, for both scopes, without ANALYZE) lives with the queries in
        tests/agent/test_insights.py.
        """
        sql = self._index_defn(db._conn)
        assert sql is not None
        assert "WHERE" in sql
        assert "role = 'assistant'" in sql
        assert "tool_calls IS NOT NULL" in sql
class TestFtsRebuildFinishWithoutTrigram:
    """An FTS index that the runtime cannot maintain must not wedge the store.

    Two independent failure sites shared one root shape: code that writes to
    ``messages_fts_trigram`` without first checking the table is actually
    present. It is legitimately absent whenever the trigram index is
    unavailable (SQLite build without the tokenizer), and it can also be left
    absent by an interrupted migration or a partially-applied schema change.
    """

    @staticmethod
    def _seed(db_path, n=60):
        seeded = SessionDB(db_path=db_path)
        try:
            seeded.create_session(session_id="s1", source="cli")
            for i in range(n):
                seeded.append_message(
                    "s1",
                    role=("user" if i % 3 == 0
                          else "assistant" if i % 3 == 1 else "tool"),
                    content=f"sentinel payload {i} zebra",
                )
            high_water = seeded._conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages"
            ).fetchone()[0]
        finally:
            seeded.close()
        return high_water

    def test_rebuild_finish_skips_trigram_when_unavailable(
        self, tmp_path, monkeypatch
    ):
        """optimize_fts_storage() completes when the trigram index is absent.

        ``fts_rebuild_step()`` already guards its backfill INSERT on
        ``_trigram_available``; ``_fts_rebuild_finish()``'s boundary sweep did
        not, so finishing a deferred rebuild on a trigram-less runtime raised
        ``no such table: messages_fts_trigram`` and aborted the whole
        optimization. The base index must still be swept and the markers
        cleared.
        """
        db_path = tmp_path / "state.db"
        high_water = self._seed(db_path)

        real_connect = sqlite3.connect

        def connect_without_trigram(*args, **kwargs):
            kwargs["factory"] = _NoTrigramConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(
            "hermes_state.sqlite3.connect", connect_without_trigram
        )
        db = SessionDB(db_path=db_path)
        try:
            assert db._trigram_available is False
            # A trigram-less runtime leaves no trigram index on disk.
            db._conn.execute("DROP TABLE IF EXISTS messages_fts_trigram")
            db._conn.commit()
            assert db._fts_table_exists("messages_fts_trigram") is False

            # Put the DB in the pending-deferred-rebuild state.
            for key, value in (
                ("fts_rebuild_high_water", str(high_water)),
                ("fts_rebuild_progress", str(high_water)),
            ):
                db._conn.execute(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            db._conn.commit()

            # Pre-fix this raised OperationalError("no such table: ...").
            db._fts_rebuild_finish()

            # The sweep ran to completion: markers cleared…
            assert db.get_meta("fts_rebuild_high_water") is None
            assert db.get_meta("fts_rebuild_progress") is None
            # …and the base index is still usable (the fix must not disable
            # real search to dodge the error).
            assert db.search_messages("zebra")
        finally:
            db.close()

    def test_optimize_fts_storage_succeeds_without_trigram(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: the public optimize entry point returns ok=True."""
        db_path = tmp_path / "state.db"
        high_water = self._seed(db_path)

        real_connect = sqlite3.connect

        def connect_without_trigram(*args, **kwargs):
            kwargs["factory"] = _NoTrigramConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(
            "hermes_state.sqlite3.connect", connect_without_trigram
        )
        db = SessionDB(db_path=db_path)
        try:
            db._conn.execute("DROP TABLE IF EXISTS messages_fts_trigram")
            db._conn.commit()
            assert db._trigram_available is False
            for key, value in (
                ("fts_rebuild_high_water", str(high_water)),
                ("fts_rebuild_progress", "0"),
            ):
                db._conn.execute(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            db._conn.commit()

            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert db.get_meta("fts_rebuild_high_water") is None
            assert db.search_messages("zebra")
        finally:
            db.close()



@pytest.mark.parametrize(
    "persisted_session_key",
    ["agent:main:telegram:dm:chat-1", None],
    ids=["exact-key", "peer-fallback"],
)
def test_gateway_session_recovery_does_not_cross_newer_reset_boundary(
    db, persisted_session_key
):
    """A newer session_reset row fences recovery for the peer (#68539).

    Recovery must never reach *behind* an intentional /new boundary and
    resurrect an older still-open row — if the newest boundary row for the
    peer is reset-ended, recovery returns nothing.
    """
    peer = {
        "user_id": "user-1",
        "session_key": persisted_session_key,
        "chat_id": "chat-1",
        "chat_type": "dm",
    }
    db.create_session("gw-before-reset", "telegram", **peer)
    db.append_message("gw-before-reset", "user", "old context")
    db.create_session("gw-reset", "telegram", **peer)
    db.append_message("gw-reset", "user", "/new")
    db.end_session("gw-reset", "session_reset")

    assert db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    ) is None












def test_compression_failure_cooldown_round_trips_and_clears(db):
    db.create_session("s1", "cli")

    cooldown_until = time.time() + 60.0
    db.record_compression_failure_cooldown("s1", cooldown_until, "timeout")

    state = db.get_compression_failure_cooldown("s1")
    assert state is not None
    assert state["cooldown_until"] == cooldown_until
    assert state["error"] == "timeout"

    db.clear_compression_failure_cooldown("s1")
    assert db.get_compression_failure_cooldown("s1") is None

    row = db.get_session("s1")
    assert row["compression_failure_cooldown_until"] is None
    assert row["compression_failure_error"] is None


def test_gateway_metadata_display_name_origin_round_trip(db):
    """record_gateway_session_peer persists display_name/origin_json (#9006)."""
    db.create_session("gw-meta", "telegram", user_id="u1")
    origin = {"platform": "telegram", "chat_id": "c1", "chat_name": "Alice", "chat_type": "dm"}
    db.record_gateway_session_peer(
        "gw-meta",
        source="telegram",
        user_id="u1",
        session_key="agent:main:telegram:dm:c1",
        chat_id="c1",
        chat_type="dm",
        thread_id=None,
        display_name="Alice",
        origin_json=json.dumps(origin),
    )
    row = db.get_session("gw-meta")
    assert row["display_name"] == "Alice"
    assert json.loads(row["origin_json"])["chat_name"] == "Alice"

    # None values must not clobber existing metadata.
    db.record_gateway_session_peer(
        "gw-meta",
        source="telegram",
        user_id="u1",
        session_key="agent:main:telegram:dm:c1",
        chat_id="c1",
        chat_type="dm",
    )
    row = db.get_session("gw-meta")
    assert row["display_name"] == "Alice"
    assert row["origin_json"] is not None


def test_message_rendered_content_is_append_only_source_contract():
    source = hermes_state.__file__
    with open(source, encoding="utf-8") as f:
        text = f.read()

    rendered_columns = {
        "content",
        "tool_name",
        "tool_calls",
        "tool_call_id",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
        "platform_message_id",
    }
    # PARITY-MERGE CARVE-OUT (2026-08-08): upstream added
    # ``purge_stale_tool_call_markers`` (#78148), an EXPLICIT, opt-in repair
    # tool that clears rows whose content is nothing but a stale tool-call
    # marker. It is not a rendering write-back — the class this invariant
    # exists to forbid — and it is guarded three ways: dry_run by default, a
    # ``VACUUM INTO`` backup before writing, and a fullmatch against
    # _STALE_TOOL_CALL_MARKER_RE so only marker-only rows qualify. Exclude that
    # ONE method's body; every other UPDATE remains bound by the contract.
    _repair_tool = re.search(
        r"\n    def purge_stale_tool_call_markers\(.*?(?=\n    def )",
        text,
        flags=re.DOTALL,
    )
    if _repair_tool:
        text = text.replace(_repair_tool.group(0), "\n    def purge_stale_tool_call_markers():\n        pass\n")

    statements = re.findall(
        r"UPDATE\s+messages\s+SET\s+(.*?)(?:\s+WHERE\b|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    offenders = []
    for statement in statements:
        for column in rendered_columns:
            if re.search(rf"\b{re.escape(column)}\s*=", statement, flags=re.IGNORECASE):
                offenders.append(column)

    assert offenders == []


