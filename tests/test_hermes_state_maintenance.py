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


class TestFTS5Search:
    def test_search_finds_content(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="How do I deploy with Docker?")
        db.append_message("s1", role="assistant", content="Use docker compose up.")

        results = db.search_messages("docker")
        assert len(results) == 2
        # At least one result should mention docker
        snippets = [r.get("snippet", "") for r in results]
        assert any("docker" in s.lower() or "Docker" in s for s in snippets)
        # Results never carry full content; snippet + metadata only.
        assert all("content" not in r for r in results)






    def test_search_returns_context(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Tell me about Kubernetes")
        db.append_message("s1", role="assistant", content="Kubernetes is an orchestrator.")

        results = db.search_messages("Kubernetes")
        assert len(results) == 2
        assert "context" in results[0]
        assert isinstance(results[0]["context"], list)
        assert len(results[0]["context"]) > 0

    def test_search_fields_project_results_without_changing_default(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Tell me about Kubernetes")
        db.append_message("s1", role="assistant", content="Kubernetes is an orchestrator.")

        projected = db.search_messages(
            "Kubernetes", fields=("session_id", "role", "snippet")
        )
        default = db.search_messages("Kubernetes")

        assert len(projected) == len(default) == 2
        assert all(set(row) == {"session_id", "role", "snippet"} for row in projected)
        assert [
            (row["session_id"], row["role"], row["snippet"])
            for row in projected
        ] == [
            (row["session_id"], row["role"], row["snippet"])
            for row in default
        ]
        assert all("context" in row and row["context"] for row in default)

    def test_search_projection_skips_context_enrichment_queries(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="before")
        db.append_message("s1", role="assistant", content="projectionneedle")
        db.append_message("s1", role="user", content="after")

        statements = []
        # Borrow the read connection THROUGH the pool so the traced
        # connection is the one _read_ctx hands back out (LIFO pool,
        # single thread → deterministic reuse). Calling _get_read_conn()
        # directly opens a fresh connection that never enters the pool,
        # so the enrichment queries would run on an untraced sibling.
        with db._read_ctx() as pooled:
            read_conn = pooled
        traced_connections = [db._conn]
        if read_conn is not db._conn:
            traced_connections.append(read_conn)
        for conn in traced_connections:
            conn.set_trace_callback(statements.append)

        def context_query_count():
            normalized = (" ".join(sql.upper().split()) for sql in statements)
            return sum("WITH TARGET AS (" in sql for sql in normalized)

        try:
            projected = db.search_messages(
                "projectionneedle", fields=("session_id", "snippet")
            )
            assert len(projected) == 1
            assert context_query_count() == 0

            full = db.search_messages(
                "projectionneedle", fields=("session_id", "context")
            )
            assert len(full) == 1
            assert full[0]["context"]
            assert context_query_count() == 1

            default = db.search_messages("projectionneedle")
            assert len(default) == 1
            assert default[0]["context"]
            assert context_query_count() == 2
        finally:
            for conn in traced_connections:
                conn.set_trace_callback(None)

    def test_sanitize_fts5_query_strips_dangerous_chars(self):
        """Unit test for _sanitize_fts5_query static method."""
        from hermes_state import SessionDB
        s = SessionDB._sanitize_fts5_query
        assert s('hello world') == 'hello world'
        assert '+' not in s('C++')
        assert '"' not in s('"unterminated')
        assert '(' not in s('(problem')
        assert '{' not in s('{test}')
        # Dangling operators removed
        assert s('hello AND') == 'hello'
        assert s('OR world') == 'world'
        # Leading bare * removed
        assert s('***') == ''
        # Valid prefix kept
        assert s('deploy*') == 'deploy*'
        # Colon (FTS5 column-filter operator) stripped, both terms preserved
        assert ':' not in s('TODO: fix')
        assert s('TODO: fix').split() == ['TODO', 'fix']
        assert ':' not in s('error:timeout')






    def test_long_search_query_is_capped_and_does_not_crash(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="bounded sanitizer target")

        query = ('"' * 50_000) + (" bounded" * 10_000)
        start = time.perf_counter()
        results = db.search_messages(query)
        elapsed = time.perf_counter() - start

        assert isinstance(results, list)
        assert elapsed < 1.0

    def test_sanitize_fts5_preserves_quoted_phrases(self):
        """Properly paired double-quoted phrases should be preserved."""
        from hermes_state import SessionDB
        s = SessionDB._sanitize_fts5_query
        # Simple quoted phrase
        assert s('"exact phrase"') == '"exact phrase"'
        # Quoted phrase alongside unquoted terms
        assert '"docker networking"' in s('"docker networking" setup')
        # Multiple quoted phrases
        result = s('"hello world" OR "foo bar"')
        assert '"hello world"' in result
        assert '"foo bar"' in result
        # Unmatched quote still stripped
        assert '"' not in s('"unterminated')

    def test_sanitize_fts5_query_runtime_is_bounded(self):
        """Adversarial quote/special-char runs should sanitize quickly."""
        from hermes_state import MAX_FTS5_QUERY_CHARS, SessionDB

        s = SessionDB._sanitize_fts5_query
        query = ('"' * 100_000) + ("a." * 100_000) + ("*" * 100_000)

        start = time.perf_counter()
        result = s(query)
        elapsed = time.perf_counter() - start

        assert isinstance(result, str)
        assert len(result) <= MAX_FTS5_QUERY_CHARS * 2
        assert elapsed < 0.5

    def test_sanitize_fts5_quotes_dotted_terms(self):
        """Dotted terms should be wrapped in quotes to avoid FTS5 query parse edge cases."""
        from hermes_state import SessionDB
        s = SessionDB._sanitize_fts5_query

        assert s('P2.2') == '"P2.2"'
        assert s('simulate.p2') == '"simulate.p2"'
        assert s('simulate.p2.test.ts') == '"simulate.p2.test.ts"'

        # Already quoted — no double quoting
        assert s('"P2.2"') == '"P2.2"'

        # Works with boolean syntax
        result = s('P2.2 OR simulate.p2')
        assert '"P2.2"' in result
        assert '"simulate.p2"' in result

        # Mixed dots and hyphens — single pass avoids double-quoting
        assert s('my-app.config') == '"my-app.config"'
        assert s('my-app.config.ts') == '"my-app.config.ts"'

    def test_sanitize_fts5_quotes_hyphenated_terms(self):
        """Hyphenated terms should be wrapped in quotes for exact matching."""
        from hermes_state import SessionDB
        s = SessionDB._sanitize_fts5_query
        # Simple hyphenated term
        assert s('chat-send') == '"chat-send"'
        # Multiple hyphens
        assert s('docker-compose-up') == '"docker-compose-up"'
        # Hyphenated term with other words
        result = s('fix chat-send bug')
        assert '"chat-send"' in result
        assert 'fix' in result
        assert 'bug' in result
        # Multiple hyphenated terms with OR
        result = s('chat-send OR deploy-prod')
        assert '"chat-send"' in result
        assert '"deploy-prod"' in result
        # Already-quoted hyphenated term — no double quoting
        assert s('"chat-send"') == '"chat-send"'
        # Hyphenated inside a quoted phrase stays as-is
        assert s('"my chat-send thing"') == '"my chat-send thing"'

    def test_sanitize_fts5_quotes_underscored_terms(self):
        """Underscored terms should be wrapped in quotes for exact matching.

        FTS5 default tokenizer splits 'sp_new1' into tokens 'sp' and 'new1'.
        Without quoting, a search for 'sp_new' becomes an AND query
        ('sp AND new') that fails to match rows indexed as 'sp_new1'.
        """
        from hermes_state import SessionDB
        s = SessionDB._sanitize_fts5_query
        # Simple underscored term
        assert s('sp_new') == '"sp_new"'
        # Multiple underscores
        assert s('a_b_c') == '"a_b_c"'
        # Mixed underscores and hyphens/dots — single pass avoids double-quoting
        assert s('sp_new1') == '"sp_new1"'
        assert s('docker-compose_up') == '"docker-compose_up"'
        assert s('my.app_config.ts') == '"my.app_config.ts"'
        # Already-quoted — no double quoting
        assert s('"sp_new"') == '"sp_new"'
        # Mixed with other words
        result = s('sp_new and 血管瘤')
        assert '"sp_new"' in result
        assert '血管瘤' in result

    def test_search_colon_query_still_finds_content(self, db):
        """Queries containing ':' must not silently return empty.

        ':' is FTS5's column-filter operator. With a single-column FTS table an
        unquoted query like 'TODO: fix' parses as 'column:term', raises
        "no such column: TODO", and the swallowed error turns into zero results
        even though the content is present. Regression for that silent-empty bug.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="TODO fix the deployment script")

        # Control: the same content is found without the colon.
        assert len(db.search_messages("deployment")) >= 1

        # The colon query must find the message, not silently return [].
        results = db.search_messages("TODO: fix")
        assert isinstance(results, list)
        assert len(results) >= 1
        assert any("deployment" in (r.get("snippet") or r.get("content", "")).lower()
                   for r in results)

    def test_search_context_uses_session_neighbors_when_ids_are_interleaved(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="cli")

        db.append_message("s1", role="user", content="before needle")
        db.append_message("s2", role="user", content="other session message")
        db.append_message("s1", role="assistant", content="needle match")
        db.append_message("s2", role="assistant", content="another other session message")
        db.append_message("s1", role="user", content="after needle")

        results = db.search_messages('"needle match"')
        needle_result = next(r for r in results if r["session_id"] == "s1" and "needle match" in r["snippet"])

        assert [msg["content"] for msg in needle_result["context"]] == [
            "before needle",
            "needle match",
            "after needle",
        ]

    def test_search_default_includes_all_platforms(self, db):
        """Default search (no source_filter) should find sessions from any platform."""
        for src in ("cli", "telegram", "signal", "homeassistant", "acp", "matrix"):
            sid = f"s-{src}"
            db.create_session(session_id=sid, source=src)
            db.append_message(sid, role="user", content=f"universal search test from {src}")

        results = db.search_messages("universal search test")
        found_sources = {r["source"] for r in results}
        assert found_sources == {"cli", "telegram", "signal", "homeassistant", "acp", "matrix"}

    def test_search_default_sources_include_acp(self, db):
        db.create_session(session_id="s1", source="acp")
        db.append_message("s1", role="user", content="ACP question about Python")

        results = db.search_messages("Python")
        sources = [r["source"] for r in results]
        assert "acp" in sources

    def test_search_dotted_term_does_not_crash(self, db):
        """Dotted terms like 'P2.2' or 'simulate.p2.test.ts' should not crash FTS5."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Working on P2.2 session_search edge cases")
        db.append_message("s1", role="assistant", content="See simulate.p2.test.ts for details")

        results = db.search_messages("P2.2")
        assert isinstance(results, list)
        assert len(results) >= 1

        results2 = db.search_messages("simulate.p2.test.ts")
        assert isinstance(results2, list)
        assert len(results2) >= 1

    def test_search_empty_query(self, db):
        assert db.search_messages("") == []
        assert db.search_messages("   ") == []

    def test_search_hyphenated_term_does_not_crash(self, db):
        """Hyphenated terms like 'chat-send' must not crash FTS5."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Run the chat-send command")

        results = db.search_messages("chat-send")
        assert isinstance(results, list)
        assert len(results) >= 1
        assert any("chat-send" in (r.get("snippet") or r.get("content", "")).lower()
                    for r in results)

    def test_search_quoted_phrase_preserved(self, db):
        """User-provided quoted phrases should be preserved for exact matching."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="docker networking is complex")
        db.append_message("s1", role="assistant", content="networking docker tips")

        # Quoted phrase should match only the exact order
        results = db.search_messages('"docker networking"')
        assert isinstance(results, list)
        # Should find the user message (exact phrase) but may or may not find
        # the assistant message depending on FTS5 phrase matching
        assert len(results) >= 1

    def test_search_sanitized_query_still_finds_content(self, db):
        """Sanitization must not break normal keyword search."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Learning C++ templates today")

        # "C++" sanitized to "C" should still match "C++"
        results = db.search_messages("C++")
        # The word "C" appears in the content, so FTS5 should find it
        assert isinstance(results, list)

    def test_search_special_chars_do_not_crash(self, db):
        """FTS5 special characters in queries must not raise OperationalError."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="How do I use C++ templates?")

        # Each of these previously caused sqlite3.OperationalError
        dangerous_queries = [
            'C++',              # + is FTS5 column filter
            '"unterminated',    # unbalanced double-quote
            '(problem',         # unbalanced parenthesis
            'hello AND',        # dangling boolean operator
            '***',              # repeated wildcard
            '{test}',           # curly braces (column reference)
            'OR hello',         # leading boolean operator
            'a AND OR b',       # adjacent operators
        ]
        for query in dangerous_queries:
            # Must not raise — should return list (possibly empty)
            results = db.search_messages(query)
            assert isinstance(results, list), f"Query {query!r} did not return a list"

    def test_search_with_role_filter(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="What is FastAPI?")
        db.append_message("s1", role="assistant", content="FastAPI is a web framework.")

        results = db.search_messages("FastAPI", role_filter=["assistant"])
        roles = [r["role"] for r in results]
        assert all(r == "assistant" for r in roles)

    def test_search_with_source_filter(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="CLI question about Python")

        db.create_session(session_id="s2", source="telegram")
        db.append_message("s2", role="user", content="Telegram question about Python")

        results = db.search_messages("Python", source_filter=["telegram"])
        # Should only find the telegram message
        sources = [r["source"] for r in results]
        assert all(s == "telegram" for s in sources)


# =========================================================================
# CJK (Chinese/Japanese/Korean) LIKE fallback
# =========================================================================

class TestPruneSessionFilters:
    """Extended filter surface shared by prune/archive/list_prune_candidates."""

    @staticmethod
    def _mk(db, sid, *, source="cli", age_seconds=0, title=None,
            end_reason="done", message_count=0, cwd=None):
        db.create_session(session_id=sid, source=source, cwd=cwd)
        db.end_session(sid, end_reason=end_reason)
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, message_count = ?, title = ? "
            "WHERE id = ?",
            (time.time() - age_seconds, message_count, title, sid),
        )
        db._conn.commit()

    def test_started_after_window_prunes_only_recent(self, db):
        self._mk(db, "recent1", age_seconds=3600)       # 1h ago
        self._mk(db, "recent2", age_seconds=2 * 3600)   # 2h ago
        self._mk(db, "old", age_seconds=10 * 3600)      # 10h ago

        cutoff = time.time() - 5 * 3600
        pruned = db.prune_sessions(older_than_days=None, started_after=cutoff)
        assert pruned == 2
        assert db.get_session("old") is not None
        assert db.get_session("recent1") is None


    def test_title_and_message_count_filters(self, db):
        self._mk(db, "smoke1", age_seconds=60, title="Codex Smoke Test 1",
                 message_count=2)
        self._mk(db, "smoke2", age_seconds=60, title="codex smoke test 2",
                 message_count=8)
        self._mk(db, "real", age_seconds=60, title="Debugging auth",
                 message_count=8)

        rows = db.list_prune_candidates(title_like="smoke")
        assert {r["id"] for r in rows} == {"smoke1", "smoke2"}

        pruned = db.prune_sessions(
            older_than_days=None, title_like="Smoke", max_messages=3
        )
        assert pruned == 1
        assert db.get_session("smoke1") is None
        assert db.get_session("smoke2") is not None
        assert db.get_session("real") is not None






    @staticmethod
    def _mk_rich(db, sid, **cols):
        """Create an ended session then set arbitrary sessions columns."""
        db.create_session(session_id=sid, source=cols.pop("source", "cli"))
        db.end_session(sid, end_reason=cols.pop("end_reason", "done"))
        cols.setdefault("started_at", time.time() - 60)
        sets = ", ".join(f"{k} = ?" for k in cols)
        db._conn.execute(
            f"UPDATE sessions SET {sets} WHERE id = ?", (*cols.values(), sid)
        )
        db._conn.commit()






    def test_title_like_underscore_is_literal_not_a_wildcard(self, db):
        """``_`` is a single-character wildcard in SQL LIKE, so an unescaped
        filter deletes sessions the operator never selected. The filters are
        documented (and shown in the CLI confirmation) as substring matches.
        """
        self._mk(db, "target", title="user_auth refactor")
        self._mk(db, "bystander1", title="user-auth review")
        self._mk(db, "bystander2", title="userXauth notes")
        self._mk(db, "bystander3", title="user auth meeting")

        rows = db.list_prune_candidates(title_like="user_auth")
        assert {r["id"] for r in rows} == {"target"}

        pruned = db.prune_sessions(older_than_days=None, title_like="user_auth")
        assert pruned == 1
        for survivor in ("bystander1", "bystander2", "bystander3"):
            assert db.get_session(survivor) is not None

    def test_percent_in_filter_does_not_select_everything(self, db):
        """``%`` matches any run of characters — a bare one would delete the
        whole table."""
        self._mk(db, "a", title="alpha")
        self._mk(db, "b", title="beta")
        self._mk(db, "pct", title="100% coverage run")

        # Only the title that really contains a percent sign matches.
        assert {r["id"] for r in db.list_prune_candidates(title_like="%")} == {"pct"}
        assert {r["id"] for r in db.list_prune_candidates(title_like="100%")} == {"pct"}

    def test_branch_like_underscore_is_literal(self, db):
        """Branch names carry underscores routinely."""
        self._mk_rich(db, "want", git_branch="fix/session_prune")
        self._mk_rich(db, "other", git_branch="fix/session-prune")

        rows = db.list_prune_candidates(branch_like="session_prune")
        assert {r["id"] for r in rows} == {"want"}

    def test_model_like_underscore_is_literal(self, db):
        self._mk_rich(db, "want", model="vendor/model_mini")
        self._mk_rich(db, "other", model="vendor/model-mini")

        rows = db.list_prune_candidates(model_like="model_mini")
        assert {r["id"] for r in rows} == {"want"}

    def test_plain_substring_filters_still_match(self, db):
        """Guard against over-escaping: ordinary filters keep working, and a
        literal backslash in the needle is matched as itself."""
        self._mk(db, "smoke", title="Codex Smoke Test")
        self._mk_rich(db, "winpath", title=r"build C:\tmp artifacts")

        assert {r["id"] for r in db.list_prune_candidates(title_like="smoke")} == {"smoke"}
        assert {r["id"] for r in db.list_prune_candidates(title_like=r"c:\tmp")} == {"winpath"}

    def test_cwd_prefix_underscore_is_literal_not_a_wildcard(self, db):
        """``_`` is a LIKE wildcard but an ordinary character in a path, so an
        unescaped prefix also matched a same-length sibling directory — and
        prune_sessions deletes what it matches."""
        self._mk(db, "target", cwd="/home/me/my_project/src")
        self._mk(db, "sibling", cwd="/home/me/myXproject/src")

        rows = db.list_prune_candidates(cwd_prefix="/home/me/my_project")
        assert {r["id"] for r in rows} == {"target"}

        pruned = db.prune_sessions(older_than_days=None, cwd_prefix="/home/me/my_project")
        assert pruned == 1
        assert db.get_session("sibling") is not None

    def test_cwd_prefix_percent_does_not_select_everything(self, db):
        self._mk(db, "a", cwd="/home/me/one")
        self._mk(db, "b", cwd="/home/me/two")

        assert db.list_prune_candidates(cwd_prefix="/home/me/%") == []

    def test_cwd_prefix_still_matches_the_directory_and_its_children(self, db):
        """Control: the prefix must keep matching itself and anything under it."""
        self._mk(db, "root", cwd="/home/me/proj")
        self._mk(db, "child", cwd="/home/me/proj/src")
        self._mk(db, "outside", cwd="/home/me/other")

        rows = db.list_prune_candidates(cwd_prefix="/home/me/proj")
        assert {r["id"] for r in rows} == {"root", "child"}

    def test_cwd_prefix_windows_separator_arm(self, db):
        """The backslash child arm (``{esc}\\\\%`` in the pattern) must keep
        matching Windows children while ``_`` stays literal — a guard against
        'simplifying' the quadruple backslash."""
        self._mk(db, "win_root", cwd=r"C:\Users\me\my_project")
        self._mk(db, "win_child", cwd=r"C:\Users\me\my_project\src")
        self._mk(db, "win_sibling", cwd=r"C:\Users\me\myXproject\src")

        rows = db.list_prune_candidates(cwd_prefix=r"C:\Users\me\my_project")
        assert {r["id"] for r in rows} == {"win_root", "win_child"}

    def test_unknown_filter_rejected(self, db):
        import pytest as _pytest
        with _pytest.raises(TypeError):
            db.prune_sessions(older_than_days=None, bogus_filter="x")

    def test_archive_sessions_bulk(self, db):
        self._mk(db, "a1", age_seconds=3600)
        self._mk(db, "a2", age_seconds=2 * 3600)
        self._mk(db, "keep", age_seconds=10 * 3600)
        # Active session in the window must never be touched
        db.create_session(session_id="live", source="cli")

        cutoff = time.time() - 5 * 3600
        count = db.archive_sessions(started_after=cutoff)
        assert count == 2
        assert db.get_session("a1")["archived"] == 1
        assert db.get_session("a2")["archived"] == 1
        assert db.get_session("keep")["archived"] == 0
        assert db.get_session("live")["archived"] == 0
        # Idempotent: already-archived rows aren't re-selected
        assert db.archive_sessions(started_after=cutoff) == 0

    def test_before_after_window(self, db):
        self._mk(db, "inside", age_seconds=5 * 3600)
        self._mk(db, "too_new", age_seconds=1 * 3600)
        self._mk(db, "too_old", age_seconds=20 * 3600)

        now = time.time()
        pruned = db.prune_sessions(
            older_than_days=None,
            started_after=now - 10 * 3600,
            started_before=now - 2 * 3600,
        )
        assert pruned == 1
        assert db.get_session("inside") is None
        assert db.get_session("too_new") is not None
        assert db.get_session("too_old") is not None

    def test_branch_like_filter(self, db):
        self._mk_rich(db, "b1", git_branch="feature/old-experiment")
        self._mk_rich(db, "b2", git_branch="main")

        assert db.prune_sessions(older_than_days=None, branch_like="experiment") == 1
        assert db.get_session("b1") is None
        assert db.get_session("b2") is not None

    def test_default_signature_unchanged(self, db):
        """Legacy positional call keeps working with identical semantics."""
        self._mk(db, "ancient", age_seconds=200 * 86400)
        self._mk(db, "fresh", age_seconds=60)
        assert db.prune_sessions(90) == 1
        assert db.get_session("ancient") is None
        assert db.get_session("fresh") is not None

    def test_end_reason_and_cwd_filters(self, db):
        self._mk(db, "s1", age_seconds=60, end_reason="done",
                 cwd="/home/u/scratch/x")
        self._mk(db, "s2", age_seconds=60, end_reason="error",
                 cwd="/home/u/scratch")
        self._mk(db, "s3", age_seconds=60, end_reason="done",
                 cwd="/home/u/work")

        rows = db.list_prune_candidates(cwd_prefix="/home/u/scratch")
        assert {r["id"] for r in rows} == {"s1", "s2"}

        pruned = db.prune_sessions(
            older_than_days=None, end_reason="done",
            cwd_prefix="/home/u/scratch",
        )
        assert pruned == 1
        assert db.get_session("s1") is None

    def test_list_prune_candidates_matches_prune(self, db):
        self._mk(db, "c1", age_seconds=3600, source="cli")
        self._mk(db, "c2", age_seconds=3600, source="telegram")
        rows = db.list_prune_candidates(started_after=0, source="cli")
        assert [r["id"] for r in rows] == ["c1"]
        pruned = db.prune_sessions(older_than_days=None, started_after=0,
                                   source="cli")
        assert pruned == 1

    def test_model_like_filter(self, db):
        self._mk_rich(db, "m1", model="anthropic/claude-sonnet-4.6")
        self._mk_rich(db, "m2", model="openai/gpt-5.4")
        self._mk_rich(db, "m3", model=None)

        rows = db.list_prune_candidates(model_like="Sonnet")
        assert [r["id"] for r in rows] == ["m1"]
        assert db.prune_sessions(older_than_days=None, model_like="gpt-5") == 1
        assert db.get_session("m2") is None
        assert db.get_session("m1") is not None
        assert db.get_session("m3") is not None

    def test_provider_filter(self, db):
        self._mk_rich(db, "p1", billing_provider="openrouter")
        self._mk_rich(db, "p2", billing_provider="Anthropic")
        self._mk_rich(db, "p3", billing_provider=None)

        assert db.prune_sessions(older_than_days=None, provider="anthropic") == 1
        assert db.get_session("p2") is None
        assert db.get_session("p1") is not None
        assert db.get_session("p3") is not None

    def test_prune_excludes_archived_when_requested(self, db):
        self._mk(db, "arch", age_seconds=60)
        self._mk(db, "plain", age_seconds=60)
        db.set_session_archived("arch", True)

        pruned = db.prune_sessions(older_than_days=None, started_after=0,
                                   archived=False)
        assert pruned == 1
        assert db.get_session("arch") is not None
        assert db.get_session("plain") is None

    def test_token_cost_toolcall_bounds(self, db):
        self._mk_rich(db, "cheap", input_tokens=100, output_tokens=50,
                      actual_cost_usd=0.001, tool_call_count=0)
        self._mk_rich(db, "mid", input_tokens=5000, output_tokens=2000,
                      actual_cost_usd=None, estimated_cost_usd=0.5,
                      tool_call_count=12)
        self._mk_rich(db, "big", input_tokens=90000, output_tokens=30000,
                      actual_cost_usd=4.2, tool_call_count=80)

        rows = db.list_prune_candidates(max_tokens=200)
        assert [r["id"] for r in rows] == ["cheap"]
        rows = db.list_prune_candidates(min_tokens=7000, max_tokens=10000)
        assert [r["id"] for r in rows] == ["mid"]
        # Cost falls back to estimated when actual is NULL
        rows = db.list_prune_candidates(min_cost=0.4, max_cost=1.0)
        assert [r["id"] for r in rows] == ["mid"]
        rows = db.list_prune_candidates(min_tool_calls=50)
        assert [r["id"] for r in rows] == ["big"]
        assert db.prune_sessions(older_than_days=None, max_tool_calls=0) == 1
        assert db.get_session("cheap") is None

    def test_user_chat_filters(self, db):
        self._mk_rich(db, "u1", user_id="alice", chat_id="c-1", chat_type="dm")
        self._mk_rich(db, "u2", user_id="bob", chat_id="c-2", chat_type="group")

        assert db.prune_sessions(older_than_days=None, user_id="alice") == 1
        assert db.get_session("u1") is None
        assert db.prune_sessions(
            older_than_days=None, chat_id="c-2", chat_type="group"
        ) == 1
        assert db.get_session("u2") is None


class TestCJKSearchFallback:
    """Regression tests for CJK search (see #11511).

    SQLite FTS5's default tokenizer treats contiguous CJK runs as a single
    token ("和其他agent的聊天记录" → one token), so substring queries like
    "记忆断裂" return 0 rows despite the data being present. SessionDB falls
    back to LIKE substring matching whenever FTS5 returns no results and
    the query contains CJK characters.
    """

    def test_cjk_detection_covers_all_ranges(self):
        from hermes_state import SessionDB
        f = SessionDB._contains_cjk
        # Chinese (CJK Unified Ideographs)
        assert f("记忆断裂") is True
        # Japanese Hiragana + Katakana
        assert f("こんにちは") is True
        assert f("カタカナ") is True
        # Korean Hangul syllables (both early and late — guards against
        # the \ud7a0-\ud7af typo seen in one of the duplicate PRs)
        assert f("안녕하세요") is True
        assert f("기억") is True
        # Non-CJK
        assert f("hello world") is False
        assert f("日本語mixedwithenglish") is True
        assert f("") is False









        # No CJK in query → LIKE fallback must not run. We don't assert this
        # directly (no instrumentation), but the FTS5 path produces an
        # FTS5-style snippet with highlight markers when the term is short.
        # At minimum: english queries must still match.


    def test_mixed_cjk_english_query(self, db):
        """Mixed queries should still fall back to LIKE when FTS5 misses."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="讨论Agent通信协议")
        # "Agent通信" is CJK+English — FTS5 default tokenizer indexes the
        # whole CJK run with embedded "agent" as separate tokens; the LIKE
        # fallback handles the substring correctly.
        results = db.search_messages("Agent通信")
        assert len(results) == 1



    def test_cjk_like_escapes_wildcards(self, db):
        """Special characters (%, _) in CJK queries are treated as literals."""
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="cli")
        db.append_message("s1", role="user", content="达成100%完成率")
        db.append_message("s2", role="user", content="达成100完成率是目标")
        # The % in the query must be literal — should only match s1
        results = db.search_messages("100%完成")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"

    def test_chinese_bigram_query(self, db):
        db.create_session(session_id="s1", source="telegram")
        db.append_message("s1", role="user", content="今天讨论A2A通信协议的实现")
        results = db.search_messages("通信")
        assert len(results) == 1

    def test_chinese_multichar_query_returns_results(self, db):
        """The headline bug: multi-char Chinese query must not return []."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1", role="user",
            content="昨天和其他Agent的聊天记录，记忆断裂问题复现了",
        )
        results = db.search_messages("记忆断裂")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"

    def test_cjk_fallback_preserves_exclude_sources(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="tool")
        db.append_message("s1", role="user", content="记忆断裂在CLI")
        db.append_message("s2", role="assistant", content="记忆断裂在tool")

        results = db.search_messages("记忆断裂", exclude_sources=["tool"])
        sources = {r["source"] for r in results}
        assert "tool" not in sources
        assert "cli" in sources

    def test_cjk_fallback_preserves_role_filter(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="用户说的记忆断裂")
        db.append_message("s1", role="assistant", content="助手说的记忆断裂")

        results = db.search_messages("记忆断裂", role_filter=["assistant"])
        assert len(results) == 1
        assert results[0]["role"] == "assistant"

    def test_cjk_fallback_preserves_source_filter(self, db):
        """Guards against the SQL-builder bug where filter clauses land
        after LIMIT/OFFSET (seen in one of the duplicate PRs)."""
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")
        db.append_message("s1", role="user", content="记忆断裂在CLI")
        db.append_message("s2", role="user", content="记忆断裂在Telegram")

        results = db.search_messages("记忆断裂", source_filter=["telegram"])
        assert len(results) == 1
        assert results[0]["source"] == "telegram"

    def test_cjk_like_dedup_no_duplicates(self, db):
        """When FTS5 and LIKE both find the same message, no duplicates."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="测试去重逻辑")
        results = db.search_messages("测试")
        assert len(results) == 1

    def test_cjk_or_combined_short_tokens_returns_results(self, db):
        """Regression test for #20494.

        OR-combined 2-char CJK tokens (e.g. "广西 OR 桂林 OR 漓江 OR 旅游")
        previously returned 0 results because _count_cjk of the whole query
        was >=3 (8 chars here), selecting the trigram path, but each individual
        token is only 2 CJK chars and trigram requires >=3 chars per token.
        The per-token check must route such queries to the LIKE fallback.
        """
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")
        db.create_session(session_id="s3", source="cli")
        db.append_message("s1", role="user", content="广西是个好地方，去过桂林")
        db.append_message("s2", role="user", content="漓江风景很美，值得旅游")
        db.append_message("s3", role="user", content="unrelated English content")

        results = db.search_messages("广西 OR 桂林 OR 漓江 OR 旅游")
        session_ids = {r["session_id"] for r in results}
        assert "s1" in session_ids, "广西/桂林 terms not matched"
        assert "s2" in session_ids, "漓江/旅游 terms not matched"
        assert "s3" not in session_ids, "unrelated message must not match"

    def test_cjk_partial_fts5_results_supplemented_by_like(self, db):
        """When FTS5 returns *some* CJK results, LIKE must still find all matches.

        Regression test for #15500 / #14829: FTS5 unicode61 tokenizer drops
        certain CJK characters, so multi-character queries may return partial
        results.  The LIKE path must always run for CJK queries.
        """
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")
        db.append_message("s1", role="user", content="昨晚讨论了记忆系统")
        db.append_message("s2", role="user", content="昨晚的会议纪要已发送")
        results = db.search_messages("昨晚")
        assert len(results) == 2
        session_ids = {r["session_id"] for r in results}
        assert session_ids == {"s1", "s2"}

    def test_cjk_query_with_no_matches_returns_empty(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="unrelated English content")
        results = db.search_messages("记忆断裂")
        assert results == []

    def test_cjk_short_token_or_query_preserves_filters(self, db):
        """Source filter applies correctly in the short-token LIKE path (#20494)."""
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")
        db.append_message("s1", role="user", content="广西旅游攻略cli")
        db.append_message("s2", role="user", content="广西旅游攻略telegram")

        results = db.search_messages("广西 OR 旅游", source_filter=["telegram"])
        assert len(results) == 1
        assert results[0]["source"] == "telegram"

    def test_cjk_snippet_is_centered_on_match(self, db):
        """Snippet should contain the search term, not just the first N chars."""
        db.create_session(session_id="s1", source="cli")
        long_prefix = "这是一段很长的前缀用来把匹配位置推到文档中间" * 3
        long_suffix = "这是一段很长的后缀内容填充剩余空间" * 3
        db.append_message(
            "s1", role="user",
            content=f"{long_prefix}记忆断裂{long_suffix}",
        )
        results = db.search_messages("记忆断裂")
        assert len(results) == 1
        # The centered substr() snippet must include the matched term.
        assert "记忆断裂" in results[0]["snippet"]

    def test_cjk_trigram_preserves_boolean_operators(self, db):
        """Boolean operators (OR, AND, NOT) work in CJK trigram queries."""
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="cli")
        db.append_message("s1", role="user", content="记忆系统很好用")
        db.append_message("s2", role="user", content="断裂连接需要修复")
        results = db.search_messages("记忆系统 OR 断裂连接")
        assert len(results) == 2
        session_ids = {r["session_id"] for r in results}
        assert session_ids == {"s1", "s2"}

    def test_english_query_still_uses_fts5_fast_path(self, db):
        """English queries must not trigger the LIKE fallback (fast path regression)."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Deploy docker containers")
        results = db.search_messages("docker")
        assert len(results) == 1

    def test_japanese_query_returns_results(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="こんにちは世界")
        assert len(db.search_messages("こんにちは")) == 1
        assert len(db.search_messages("世界")) == 1

    def test_korean_query_returns_results(self, db):
        """Guards against Hangul range typos (\\uac00-\\ud7af, not \\ud7a0-)."""
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="안녕하세요 반갑습니다")
        results = db.search_messages("안녕")
        assert len(results) == 1





# =========================================================================
# Session search and listing
# =========================================================================

class TestApplyWalProbe:
    """Unit tests for the journal_mode probe in apply_wal_with_fallback."""

    @pytest.fixture(autouse=True)
    def _assume_fixed_sqlite(self, monkeypatch):
        """These cases cover the fixed-SQLite WAL path (not the #69784 gate)."""
        import hermes_state

        monkeypatch.setattr(
            hermes_state, "is_sqlite_wal_reset_vulnerable", lambda version_info=None: False
        )


    def test_sets_wal_on_fresh_connection(self, tmp_path):
        """Probe sees 'delete', then set-pragma runs and returns 'wal'."""
        import sqlite3
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        db_path = tmp_path / "fresh.db"
        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        assert any("journal_mode=WAL" in sql for sql in conn.executed), (
            "set-pragma must fire on a fresh (non-WAL) connection"
        )






    def test_apply_wal_concurrent_connects_no_eio(self, tmp_path):
        """20 threads calling connect() on the same DB must not see disk I/O error."""
        import sys
        import threading
        import sqlite3
        from hermes_state import apply_wal_with_fallback

        db_path = tmp_path / "concurrent.db"
        errors = []

        def _connect_cycle():
            for _ in range(5):
                try:
                    conn = sqlite3.connect(str(db_path))
                    apply_wal_with_fallback(conn)
                    conn.close()
                except sqlite3.OperationalError as exc:
                    if "disk i/o error" in str(exc).lower():
                        errors.append(exc)

        threads = [threading.Thread(target=_connect_cycle) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"disk I/O errors from concurrent connects: {errors}"

        # Linux-only: no (deleted) WAL/SHM FDs should accumulate.
        if sys.platform == "linux":
            import os

            fd_dir = f"/proc/{os.getpid()}/fd"
            deleted_fds = []
            for fd_name in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd_name))
                    if "(deleted)" in target and (
                        "wal" in target.lower() or "shm" in target.lower()
                    ):
                        deleted_fds.append(target)
                except OSError:
                    pass
            assert not deleted_fds, f"stale deleted WAL/SHM FDs: {deleted_fds}"




    def test_returns_wal_not_delete_from_probe(self, tmp_path):
        """Early-return only on 'wal'; 'delete' or 'memory' must fall through to set-pragma."""
        import sqlite3
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        # Fresh DB is in "delete" mode — probe returns "delete", must NOT early-return.
        db_path = tmp_path / "delete_mode.db"
        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        assert any("journal_mode=WAL" in sql for sql in conn.executed), (
            "set-pragma must fire when probe returns 'delete'"
        )

    def test_checkpoint_fullsync_barrier_skipped_off_darwin(self, tmp_path, monkeypatch):
        """Non-macOS platforms must NOT issue the macOS-only PRAGMA."""
        import sqlite3
        import hermes_state
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        monkeypatch.setattr(hermes_state.sys, "platform", "linux")

        db_path = tmp_path / "linux_fresh.db"
        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        assert not any("checkpoint_fullfsync" in sql for sql in conn.executed), (
            "checkpoint_fullfsync must not be issued off macOS"
        )
        assert not any("synchronous=FULL" in sql for sql in conn.executed), (
            "synchronous=FULL must not be issued off macOS"
        )

    def test_fallback_to_delete_still_works(self, tmp_path):
        """When set-pragma raises a WAL-incompat error, falls back to DELETE."""
        import sqlite3
        from hermes_state import apply_wal_with_fallback

        class _IncompatConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._call_count = 0

            def execute(self, sql, params=()):
                self._call_count += 1
                # First call is the read probe; let it return "delete".
                # Second call is the set-pragma; raise a WAL-incompat error.
                if "journal_mode=WAL" in sql:
                    raise sqlite3.OperationalError("locking protocol")
                return super().execute(sql, params)

        db_path = tmp_path / "incompat.db"
        conn = _IncompatConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn, db_label="test.db")
        finally:
            conn.close()

        assert result == "delete"

    def test_macos_barrier_applied_when_already_wal(self, tmp_path, monkeypatch):
        """The Darwin barrier fires on the already-WAL early-return path too."""
        import sqlite3
        import hermes_state
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        db_path = tmp_path / "macos_wal.db"
        with sqlite3.connect(str(db_path)) as seed:
            seed.execute("PRAGMA journal_mode=WAL")

        monkeypatch.setattr(hermes_state.sys, "platform", "darwin")

        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        assert any("checkpoint_fullfsync=1" in sql for sql in conn.executed), (
            "checkpoint_fullfsync barrier must fire on the already-WAL path"
        )

    def test_macos_checkpoint_fullsync_barrier_applied(self, tmp_path, monkeypatch):
        """On Darwin, apply_wal_with_fallback sets checkpoint_fullfsync=1 (issue #30636)."""
        import sqlite3
        import hermes_state
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        monkeypatch.setattr(hermes_state.sys, "platform", "darwin")

        db_path = tmp_path / "macos_fresh.db"
        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        assert any("checkpoint_fullfsync=1" in sql for sql in conn.executed), (
            "checkpoint_fullfsync barrier must be applied on macOS"
        )

    def test_macos_synchronous_full_enforced_already_wal(self, tmp_path, monkeypatch):
        """synchronous=FULL is enforced even when DB is already in WAL mode (issue #63531)."""
        import sqlite3
        import hermes_state
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        # Prime the file into WAL mode first (simulating an existing WAL DB).
        db_path = tmp_path / "macos_wal_sync.db"
        with sqlite3.connect(str(db_path)) as seed:
            seed.execute("PRAGMA journal_mode=WAL")

        monkeypatch.setattr(hermes_state.sys, "platform", "darwin")

        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        # The early-return path for existing WAL must also enforce synchronous=FULL.
        assert any("synchronous=FULL" in sql for sql in conn.executed), (
            "synchronous=FULL must be enforced even on existing WAL DBs"
        )
        assert not any("journal_mode=WAL" in sql for sql in conn.executed), (
            "set-pragma must not run when already in WAL mode"
        )

    def test_macos_synchronous_full_enforced_fresh(self, tmp_path, monkeypatch):
        """On Darwin, apply_wal_with_fallback enforces synchronous=FULL (issue #63531)."""
        import sqlite3
        import hermes_state
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        monkeypatch.setattr(hermes_state.sys, "platform", "darwin")

        db_path = tmp_path / "macos_fresh_sync.db"
        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        assert any("synchronous=FULL" in sql for sql in conn.executed), (
            "synchronous=FULL must be enforced on macOS"
        )

    def test_no_downgrade_from_wal_to_delete_on_eio(self, tmp_path):
        """OperationalError NOT in _WAL_INCOMPAT_MARKERS must propagate, not downgrade."""
        import sqlite3
        import pytest
        from hermes_state import apply_wal_with_fallback

        class _EIOConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._first = True

            def execute(self, sql, params=()):
                # Let the probe succeed (returns "delete" for fresh DB).
                if "journal_mode=WAL" in sql:
                    raise sqlite3.OperationalError("some unexpected hardware failure")
                return super().execute(sql, params)

        db_path = tmp_path / "eio.db"
        conn = _EIOConn(str(db_path))
        try:
            with pytest.raises(
                sqlite3.OperationalError, match="some unexpected hardware failure"
            ):
                apply_wal_with_fallback(conn)
        finally:
            conn.close()

    def test_probe_failure_falls_through_to_set_pragma(self, tmp_path):
        """When the read probe raises OperationalError, fall through to set-pragma."""
        import sqlite3
        from hermes_state import apply_wal_with_fallback

        class _ProbeFails(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._first = True

            def execute(self, sql, params=()):
                if self._first and "journal_mode" in sql and "WAL" not in sql:
                    self._first = False
                    raise sqlite3.OperationalError("simulated probe failure")
                return super().execute(sql, params)

        db_path = tmp_path / "probe_fail.db"
        conn = _ProbeFails(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        # Despite probe failure, set-pragma must still run and succeed.
        assert result == "wal"

    def test_skips_set_pragma_when_already_wal(self, tmp_path):
        """Already-WAL connection must not trigger the set-pragma."""
        import sqlite3
        from hermes_state import apply_wal_with_fallback

        class _TracingConn(sqlite3.Connection):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.executed = []

            def execute(self, sql, params=()):
                self.executed.append(sql)
                return super().execute(sql, params)

        db_path = tmp_path / "wal.db"
        # Prime the file into WAL mode first.
        with sqlite3.connect(str(db_path)) as seed:
            seed.execute("PRAGMA journal_mode=WAL")

        conn = _TracingConn(str(db_path))
        try:
            result = apply_wal_with_fallback(conn)
        finally:
            conn.close()

        assert result == "wal"
        # Only the probe should have fired; the set-pragma must NOT appear.
        assert any("PRAGMA journal_mode" == sql.strip() for sql in conn.executed), (
            "probe PRAGMA should have run"
        )
        assert not any("journal_mode=WAL" in sql for sql in conn.executed), (
            "set-pragma must not run when already in WAL mode"
        )


class TestCompressionChainProjection:
    """Tests for lineage-aware list_sessions_rich — compressed conversations
    surface as their live continuation tip, not the dead parent root.
    """

    def _build_compression_chain(self, db, t0: float):
        """Helper: builds root -> delegate -> compression-child -> tip chain.

        Returns (root_id, delegate_id, mid_id, tip_id).
        """
        # Root that gets compressed
        db.create_session("root1", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "root1"))
        db.append_message("root1", "user", "help me refactor auth")

        # Delegate subagent spawned while root1 was live (before it ended)
        db.create_session("delegate1", "cli", parent_session_id="root1")
        db._conn.execute(
            "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
            (t0 + 600, t0 + 650, "delegate1"),
        )
        db.append_message("delegate1", "user", "delegate task")

        # root1 compressed at t0+1800
        t_compress_root = t0 + 1800
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t_compress_root, "compression", "root1"),
        )

        # Continuation mid created 1s after parent ended
        db.create_session("mid1", "cli", parent_session_id="root1")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?",
            (t_compress_root + 1, "mid1"),
        )
        db.append_message("mid1", "user", "continuing")

        # mid1 also compressed
        t_compress_mid = t_compress_root + 1800
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t_compress_mid, "compression", "mid1"),
        )

        # Tip — latest continuation
        db.create_session("tip1", "cli", parent_session_id="mid1")
        db._conn.execute(
            "UPDATE sessions SET started_at=? WHERE id=?",
            (t_compress_mid + 1, "tip1"),
        )
        db.append_message("tip1", "user", "latest message")

        db._conn.commit()
        return ("root1", "delegate1", "mid1", "tip1")

    def test_get_compression_tip_walks_full_chain(self, db):
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        assert db.get_compression_tip("root1") == "tip1"
        assert db.get_compression_tip("mid1") == "tip1"
        assert db.get_compression_tip("tip1") == "tip1"



    def test_list_surfaces_tip_for_compressed_root(self, db):
        """The list must show the tip's id/message_count/preview in place of
        the root row, so users can see and resume the live conversation.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        # Add an uncompressed root for comparison.
        db.create_session("solo", "cli")
        db.append_message("solo", "user", "standalone")
        db._conn.commit()

        sessions = db.list_sessions_rich(source="cli", limit=20)
        ids = [s["id"] for s in sessions]
        # Only top-level conversations appear: tip1 (projected from root1) + solo.
        # Delegate children, mid1, and the dead root1 must NOT be in the list.
        assert "tip1" in ids
        assert "solo" in ids
        assert "root1" not in ids
        assert "mid1" not in ids
        assert "delegate1" not in ids

        tip_row = next(s for s in sessions if s["id"] == "tip1")
        # The row surfaces the tip's identity but preserves the root's start
        # timestamp for stable ordering and lineage tracking.
        assert tip_row["_lineage_root_id"] == "root1"
        assert tip_row["preview"].startswith("latest message")
        assert tip_row["ended_at"] is None  # tip is still live
        assert tip_row["end_reason"] is None

    def test_list_projects_multiple_independent_chains_in_one_call(self, db):
        """Two unrelated compression chains in the same page must each
        resolve to their own tip, not get cross-mixed by the batched tip-row
        fetch (regression test for the single-query batch in
        _get_session_rich_rows_batch — a wrong id->row mapping there would
        silently swap one chain's data onto the other)."""
        import time as _time

        t0 = _time.time() - 7200
        self._build_compression_chain(db, t0)

        # Second, independent chain — same shape, different ids/content.
        db.create_session("root2", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 100, "root2"))
        db.append_message("root2", "user", "second conversation start")
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t0 + 200, "compression", "root2"),
        )
        db.create_session("tip2", "cli", parent_session_id="root2")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 201, "tip2"))
        db.append_message("tip2", "user", "second conversation continuation")
        db.update_session_cwd("tip2", "/tmp/workspaces/second")
        db._conn.commit()

        sessions = db.list_sessions_rich(source="cli", limit=20)
        ids = [s["id"] for s in sessions]
        assert "root1" not in ids and "root2" not in ids
        assert "tip1" in ids and "tip2" in ids

        tip1_row = next(s for s in sessions if s["id"] == "tip1")
        tip2_row = next(s for s in sessions if s["id"] == "tip2")
        assert tip1_row["_lineage_root_id"] == "root1"
        assert tip1_row["preview"].startswith("latest message")
        assert tip2_row["_lineage_root_id"] == "root2"
        assert tip2_row["preview"].startswith("second conversation continuation")
        assert tip2_row["cwd"] == "/tmp/workspaces/second"

    def test_list_batches_tip_row_fetch_into_one_query(self, db, monkeypatch):
        """Projection must resolve tip rows for a whole page in one batched
        query, not one _get_session_rich_row() call per compression root."""
        import time as _time

        t0 = _time.time() - 7200
        self._build_compression_chain(db, t0)
        db.create_session("root2", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 100, "root2"))
        db.append_message("root2", "user", "second conversation start")
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t0 + 200, "compression", "root2"),
        )
        db.create_session("tip2", "cli", parent_session_id="root2")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0 + 201, "tip2"))
        db.append_message("tip2", "user", "second continuation")
        db._conn.commit()

        batch_calls = []
        single_calls = []
        original_batch = db._get_session_rich_rows_batch
        original_single = db._get_session_rich_row

        def counting_batch(session_ids, **kwargs):
            batch_calls.append(list(session_ids))
            return original_batch(session_ids, **kwargs)

        def counting_single(session_id, **kwargs):
            single_calls.append(session_id)
            return original_single(session_id, **kwargs)

        monkeypatch.setattr(db, "_get_session_rich_rows_batch", counting_batch)
        monkeypatch.setattr(db, "_get_session_rich_row", counting_single)

        sessions = db.list_sessions_rich(source="cli", limit=20)
        assert len(sessions) >= 2  # sanity: both chains actually surfaced

        # Two compression roots resolved with exactly one batched call, and
        # zero single-row calls — not one single-row call per root.
        assert len(batch_calls) == 1
        assert set(batch_calls[0]) == {"tip1", "tip2"}
        assert single_calls == []




    def test_list_handles_broken_chain_gracefully(self, db):
        """A compression root with no child (e.g. DB corruption or a partial
        end_session call that didn't finish creating the child) must not
        crash the list — it should fall back to surfacing the root as-is.
        """
        import time as _time
        t0 = _time.time() - 100
        db.create_session("orphan", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t0, "orphan"))
        db._conn.execute(
            "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
            (t0 + 10, "compression", "orphan"),
        )
        db._conn.commit()

        sessions = db.list_sessions_rich(source="cli", limit=10)
        ids = [s["id"] for s in sessions]
        assert "orphan" in ids
        row = next(s for s in sessions if s["id"] == "orphan")
        # No tip means no projection — row stays raw.
        assert "_lineage_root_id" not in row
        assert row["end_reason"] == "compression"

    def test_get_compression_tip_returns_self_for_uncompressed(self, db):
        db.create_session("solo", "cli")
        assert db.get_compression_tip("solo") == "solo"

    def test_get_compression_tip_skips_delegate_children(self, db):
        """Delegate subagents have parent_session_id set but were created
        BEFORE the parent ended. They must not be followed as compression
        continuations — the started_at >= ended_at guard handles this.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        # delegate1 is a child of root1 but NOT a compression continuation.
        # root1's tip must be tip1 (via mid1), not delegate1.
        assert db.get_compression_tip("root1") == "tip1"

    def test_list_preserves_sort_by_started_at(self, db):
        """Chronological ordering uses the ROOT's started_at (conversation
        start), not the tip's. This keeps lineage entries stable in the list
        even as new compressions push the tip forward in time.
        """
        import time as _time
        t0 = _time.time() - 3600
        self._build_compression_chain(db, t0)

        # Create a newer standalone session that should sort above the lineage
        # if we used tip.started_at, but below if we correctly use root.started_at.
        t_between = t0 + 120  # between root1 and its compression
        db.create_session("newer", "cli")
        db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (t_between, "newer"))
        db.append_message("newer", "user", "newer session started after root1")
        db._conn.commit()

        sessions = db.list_sessions_rich(source="cli", limit=20)
        ids_in_order = [s["id"] for s in sessions]
        # 'newer' started AFTER root1 but BEFORE tip1's actual started_at.
        # Correct ordering (by root started_at): newer > tip1's lineage entry.
        assert ids_in_order.index("newer") < ids_in_order.index("tip1")

    def test_list_projection_uses_tip_cwd(self, db):
        """Projected lineage rows should carry cwd from the live tip row.

        Without this, compressed conversations can lose workspace grouping
        even after the continuation session persists its cwd.
        """
        import time as _time

        self._build_compression_chain(db, _time.time() - 3600)
        db.update_session_cwd("tip1", "/tmp/workspaces/tip")
        db._conn.commit()

        sessions = db.list_sessions_rich(source="cli", limit=20)
        tip_row = next(s for s in sessions if s["id"] == "tip1")

        assert tip_row["_lineage_root_id"] == "root1"
        assert tip_row["cwd"] == "/tmp/workspaces/tip"

    def test_list_without_projection_returns_raw_root(self, db):
        """project_compression_tips=False returns the raw parent-NULL root
        rows — useful for admin/debug UIs.
        """
        import time as _time
        self._build_compression_chain(db, _time.time() - 3600)
        sessions = db.list_sessions_rich(
            source="cli", limit=20, project_compression_tips=False
        )
        ids = [s["id"] for s in sessions]
        assert "root1" in ids
        assert "tip1" not in ids

        root_row = next(s for s in sessions if s["id"] == "root1")
        assert root_row["end_reason"] == "compression"
        assert "_lineage_root_id" not in root_row


# =========================================================================
# Session source exclusion (--source flag for third-party isolation)
# =========================================================================

class TestCounts:

    def test_session_count_by_source(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")
        db.create_session(session_id="s3", source="cli")
        assert db.session_count(source="cli") == 2
        assert db.session_count(source="telegram") == 1






    def test_session_count_ge_empty(self, db):
        """session_count_ge should return False for 0 sessions."""
        assert db.session_count_ge(1) is False
        assert db.session_count_ge(2) is False

    def test_session_count_ge_at_threshold(self, db):
        """session_count_ge should True when count >= n."""
        db.create_session("s1", "cli")
        assert db.session_count_ge(1) is True
        assert db.session_count_ge(2) is False

        db.create_session("s2", "telegram")
        assert db.session_count_ge(1) is True
        assert db.session_count_ge(2) is True
        assert db.session_count_ge(3) is False

    def test_message_count_total(self, db):
        assert db.message_count() == 0
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", role="user", content="Hello")
        db.append_message("s1", role="assistant", content="Hi")
        assert db.message_count() == 2

    def test_message_count_per_session(self, db):
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="cli")
        db.append_message("s1", role="user", content="A")
        db.append_message("s2", role="user", content="B")
        db.append_message("s2", role="user", content="C")
        assert db.message_count(session_id="s1") == 1
        assert db.message_count(session_id="s2") == 2

    def test_session_count(self, db):
        assert db.session_count() == 0
        db.create_session(session_id="s1", source="cli")
        db.create_session(session_id="s2", source="telegram")
        assert db.session_count() == 2

    def test_session_count_by_cwd_prefix(self, db):
        db.create_session("s1", "cli", cwd="/repo")
        db.create_session("s2", "cli", cwd="/repo-wt-feature")
        db.create_session("s3", "cli", cwd="/repo/subdir")

        assert db.session_count(cwd_prefix="/repo") == 2

    def test_session_counts_by_source_matches_list_sessions_rich_histogram(self, db):
        db.create_session(session_id="cli-1", source="cli")
        db.create_session(session_id="blank-source", source="")
        db.create_session(session_id="telegram-1", source="telegram")
        db.create_session(session_id="archived-1", source="slack")
        db.create_session(
            session_id="delegate-child",
            source="tool",
            parent_session_id="cli-1",
            model_config={"_delegate_from": "cli-1"},
        )
        db.set_session_archived("archived-1", True)

        def legacy_histogram(*, include_archived=False):
            counts = {}
            for session in db.list_sessions_rich(
                limit=10000,
                include_archived=include_archived,
            ):
                source = str(session.get("source") or "cli")
                counts[source] = counts.get(source, 0) + 1
            return counts

        assert db.session_counts_by_source() == legacy_histogram()
        assert db.session_counts_by_source(include_archived=True) == legacy_histogram(
            include_archived=True
        )



# =========================================================================
# Delete and export
# =========================================================================

class TestTitleUniqueness:
    """Tests for unique title enforcement and title-based lookups."""

    def test_duplicate_title_raises(self, db):
        """Setting a title already used by another session raises ValueError."""
        db.create_session("s1", "cli")
        db.create_session("s2", "cli")
        db.set_session_title("s1", "my project")
        with pytest.raises(ValueError, match="already in use"):
            db.set_session_title("s2", "my project")


    def test_null_titles_not_unique(self, db):
        """Multiple sessions can have NULL titles (no constraint violation)."""
        db.create_session("s1", "cli")
        db.create_session("s2", "cli")
        # Both have NULL titles — no error
        assert db.get_session("s1")["title"] is None
        assert db.get_session("s2")["title"] is None

    def test_get_session_by_title(self, db):
        db.create_session("s1", "cli")
        db.set_session_title("s1", "refactoring auth")
        result = db.get_session_by_title("refactoring auth")
        assert result is not None
        assert result["id"] == "s1"

    def test_get_session_by_title_not_found(self, db):
        assert db.get_session_by_title("nonexistent") is None

    def test_get_session_title(self, db):
        db.create_session("s1", "cli")
        assert db.get_session_title("s1") is None
        db.set_session_title("s1", "my title")
        assert db.get_session_title("s1") == "my title"

    def test_get_session_title_nonexistent(self, db):
        assert db.get_session_title("nonexistent") is None

    def test_same_session_can_keep_title(self, db):
        """A session can re-set its own title without error."""
        db.create_session("s1", "cli")
        db.set_session_title("s1", "my project")
        # Should not raise — it's the same session
        assert db.set_session_title("s1", "my project") is True






class TestConnectionLifecycle:
    def test_failed_writable_open_does_not_leak_tracked_connection(
        self, tmp_path, monkeypatch
    ):
        """A failed schema init must close the connection opened before it."""
        from hermes_cli.sqlite_safe_read import has_live_connection

        db_path = tmp_path / "state.db"
        opened = []
        real_connect = hermes_state._connect_tracked_db

        def capture_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        monkeypatch.setattr(hermes_state, "_connect_tracked_db", capture_connect)
        monkeypatch.setattr(
            SessionDB,
            "_init_schema",
            mock.Mock(side_effect=RuntimeError("schema init failed")),
        )

        try:
            with pytest.raises(RuntimeError, match="schema init failed"):
                SessionDB(db_path=db_path)
            assert has_live_connection(db_path) is False
        finally:
            for conn in opened:
                try:
                    conn.close()
                except Exception:
                    pass

    def test_failed_wal_read_open_does_not_leak_tracked_connection(
        self, tmp_path, monkeypatch
    ):
        """A post-open read setup failure must close its unregistered conn."""
        from hermes_cli import sqlite_safe_read

        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        opened = []
        real_connect = hermes_state._connect_tracked_db
        real_pragmas = hermes_state.apply_database_pragmas

        def capture_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        def fail_pragmas(*args, **kwargs):
            raise RuntimeError("read setup failed")

        monkeypatch.setattr(hermes_state, "_connect_tracked_db", capture_connect)
        monkeypatch.setattr(hermes_state, "apply_database_pragmas", fail_pragmas)
        before = dict(sqlite_safe_read._live_connections)
        db._wal_active = True

        try:
            with pytest.raises(RuntimeError, match="read setup failed"):
                db._get_read_conn()
            assert sqlite_safe_read._live_connections == before
        finally:
            monkeypatch.setattr(
                hermes_state, "apply_database_pragmas", real_pragmas
            )
            for conn in opened:
                try:
                    conn.close()
                except Exception:
                    pass
            db.close()

    def test_read_only_close_never_requests_wal_checkpoint(self, tmp_path):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("s1", source="cli")
        writable.close()

        executed = []
        read_only = SessionDB(db_path=db_path, read_only=True)
        read_only._conn.set_trace_callback(executed.append)
        read_only.close()

        assert not any("wal_checkpoint" in sql.lower() for sql in executed)

    def test_writable_close_uses_passive_checkpoint(self, tmp_path):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        executed = []
        writable._conn.set_trace_callback(executed.append)

        writable.close()

        # close() must NOT TRUNCATE: transient per-cron-run connections firing
        # full WAL resets race the gateway's live writer and corrupt B-tree
        # pages (issue #45383). It uses PASSIVE instead.
        assert not any(
            "pragma wal_checkpoint(truncate)" == " ".join(sql.lower().split())
            for sql in executed
        )
        assert any(
            "pragma wal_checkpoint(passive)" == " ".join(sql.lower().split())
            for sql in executed
        )

    def test_read_only_connection_keeps_fts_search_available(self, tmp_path):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("fts-read-only", source="cli")
        writable.append_message(
            "fts-read-only",
            role="user",
            content="readonlywoodpecker 大别山项目",
        )
        writable.close()

        read_only = SessionDB(db_path=db_path, read_only=True)
        try:
            base_matches = read_only.search_messages("readonlywoodpecker")
            trigram_matches = read_only.search_messages("大别山")
        finally:
            read_only.close()

        assert [match["session_id"] for match in base_matches] == [
            "fts-read-only"
        ]
        assert [match["session_id"] for match in trigram_matches] == [
            "fts-read-only"
        ]

    def test_failed_read_only_open_does_not_leak_tracked_connection(
        self, tmp_path
    ):
        """A malformed store makes the RO FTS probe raise DatabaseError.
        The connection must be closed on that failure path: a leaked tracked
        connection blocks _backup_db_file's raw-copy for the process
        lifetime, so the writable heal that follows would repair WITHOUT its
        forensic backup."""
        import sqlite3

        from hermes_cli.sqlite_safe_read import has_live_connection

        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("s1", source="cli")
        writable.append_message("s1", role="user", content="leak probe")
        writable.close()

        # Corrupt sqlite_master: duplicate messages_fts definition. Any
        # statement on a fresh connection then raises "malformed database
        # schema" (DatabaseError, not the OperationalError the probe eats).
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA writable_schema=ON")
        row = conn.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master "
            "WHERE name='messages_fts'"
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO sqlite_master (type,name,tbl_name,rootpage,sql) "
            "VALUES (?,?,?,?,?)",
            row,
        )
        conn.execute("PRAGMA writable_schema=OFF")
        conn.close()

        with pytest.raises(sqlite3.DatabaseError):
            SessionDB(db_path=db_path, read_only=True)

        assert has_live_connection(db_path) is False

        # The writable heal must still take its forensic backup.
        healed = SessionDB(db_path=db_path, read_only=False)
        healed.close()
        assert list(tmp_path.glob("*malformed-backup*"))


# =========================================================================
# Session lifecycle
# =========================================================================

class TestDeleteEmptySessions:
    """``delete_empty_sessions`` sweeps every ended, non-archived session
    whose ``message_count`` is 0. Backs the dashboard's "Delete empty"
    button — see ``SessionsPage.tsx`` + ``DELETE /api/sessions/empty``
    in ``hermes_cli/web_server.py``.

    Invariants this class locks in:

    1. Only ``message_count = 0`` rows are touched.
    2. Active (un-ended) sessions are skipped even if they're empty —
       the agent might be mid-handshake, and yanking the row would
       race the live runtime.
    3. Archived sessions are skipped — the user already filed them away.
    4. Children of a deleted parent are orphaned (parent_session_id →
       NULL) rather than cascade-deleted, matching the
       ``delete_session`` / ``prune_sessions`` contract.
    5. The pre-DB count matches the post-DB delete return value.
    """

    def test_count_and_delete_empties_only(self, db):
        # Two empty + ended sessions → both should be in the kill list.
        db.create_session(session_id="empty1", source="cli")
        db.end_session("empty1", end_reason="done")
        db.create_session(session_id="empty2", source="cli")
        db.end_session("empty2", end_reason="done")

        # One non-empty + ended session → must survive.
        db.create_session(session_id="hasmsg", source="cli")
        db.append_message("hasmsg", role="user", content="Hello")
        db.end_session("hasmsg", end_reason="done")

        assert db.count_empty_sessions() == 2

        deleted = db.delete_empty_sessions()
        assert deleted == 2
        assert db.get_session("empty1") is None
        assert db.get_session("empty2") is None
        assert db.get_session("hasmsg") is not None
        assert db.count_empty_sessions() == 0





    def test_cleans_up_on_disk_transcript_files(self, db, tmp_path):
        """When ``sessions_dir`` is provided, transcript files left
        behind by a crashed gateway (``request_dump_*.json``) are swept
        too. Empty sessions rarely have ``{id}.json`` / ``.jsonl``
        transcripts, but the request-dump path is real — the gateway
        writes one before the first reply lands, so a crash mid-reply
        produces an empty session with a non-empty dump file."""
        db.create_session(session_id="empty_with_dump", source="cli")
        db.end_session("empty_with_dump", end_reason="done")

        dump = tmp_path / "request_dump_empty_with_dump_0.json"
        dump.write_text("{}")
        transcript = tmp_path / "empty_with_dump.jsonl"
        transcript.write_text("")

        deleted = db.delete_empty_sessions(sessions_dir=tmp_path)
        assert deleted == 1
        assert not dump.exists()
        assert not transcript.exists()

    def test_orphans_children_of_deleted_empty_parent(self, db):
        """Even an empty parent can have a child (e.g. a branch session
        spawned before the parent received any messages). The sweep
        must orphan that child, not cascade-delete it — same contract
        as ``delete_session`` and ``prune_sessions``."""
        db.create_session(session_id="empty_parent", source="cli")
        db.end_session("empty_parent", end_reason="done")
        db.create_session(
            session_id="child", source="cli", parent_session_id="empty_parent"
        )
        db.append_message("child", role="user", content="something")
        db.end_session("child", end_reason="done")

        deleted = db.delete_empty_sessions()
        assert deleted == 1
        assert db.get_session("empty_parent") is None
        child = db.get_session("child")
        assert child is not None
        assert child["parent_session_id"] is None

    def test_returns_zero_when_nothing_to_delete(self, db):
        """No-op path: no candidate rows → return 0, no error."""
        db.create_session(session_id="hasmsg", source="cli")
        db.append_message("hasmsg", role="user", content="Hello")
        db.end_session("hasmsg", end_reason="done")

        assert db.count_empty_sessions() == 0
        assert db.delete_empty_sessions() == 0
        assert db.get_session("hasmsg") is not None

    def test_skips_active_empty_sessions(self, db):
        """A live (un-ended) empty session is what you get during the
        race between session-create and the first message landing. The
        sweep must not delete it — that would yank a session out from
        under the agent before its first reply persists."""
        db.create_session(session_id="live", source="cli")
        # Deliberately no end_session() — session is "active".

        assert db.count_empty_sessions() == 0
        assert db.delete_empty_sessions() == 0
        assert db.get_session("live") is not None

    def test_skips_archived_empty_sessions(self, db):
        """Archived = soft-hidden by the user. They explicitly chose to
        keep the row around (even though it's empty), so the bulk sweep
        must not surprise them by deleting it. Restoring an archived
        session is one click; resurrecting one we deleted is impossible."""
        db.create_session(session_id="archived_empty", source="cli")
        db.end_session("archived_empty", end_reason="done")
        db.set_session_archived("archived_empty", True)

        assert db.count_empty_sessions() == 0
        assert db.delete_empty_sessions() == 0
        assert db.get_session("archived_empty") is not None


# =========================================================================
# Schema and WAL mode
# =========================================================================

# =========================================================================
# Session title
# =========================================================================

class TestFTS5ToolCallIndexing:
    """Regression tests: search_messages must see tool_name and tool_calls.

    Before #16751's fix, `messages_fts` only indexed `messages.content`, so
    tokens that only appeared in `tool_name` or the serialized `tool_calls`
    JSON were invisible to session_search even though the row was in the DB.
    """

    def test_tool_name_is_searchable(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1", role="assistant", content="",
            tool_name="UNIQUETOOLNAME",
        )
        results = db.search_messages("UNIQUETOOLNAME")
        assert len(results) == 1

    def test_tool_calls_args_are_searchable(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1", role="assistant", content="",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": '{"query": "UNIQUESEARCHTOKEN"}',
                },
            }],
        )
        results = db.search_messages("UNIQUESEARCHTOKEN")
        assert len(results) == 1

    def test_delete_message_row_does_not_crash(self, db):
        """DELETE on messages must not raise when FTS rows reference tool fields.

        Previously the messages_fts_delete trigger passed old.content to the
        FTS5 delete-command but the inserted row was the concatenation of
        content || tool_name || tool_calls, so FTS5 rejected the delete with
        'SQL logic error' and every session delete path broke.
        """
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1", role="assistant", content="hello",
            tool_name="web_search",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"q": "x"}'},
            }],
        )
        # end_session + end-time prune path would exercise DELETE; hit the
        # row directly through the write helper to keep the regression focused.
        def _delete(conn):
            conn.execute("DELETE FROM messages WHERE session_id = ?", ("s1",))
        db._execute_write(_delete)  # must not raise

        assert db.search_messages("hello") == []
        assert db.search_messages("web_search") == []

    def test_tool_function_name_in_tool_calls_is_searchable(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1", role="assistant", content="",
            tool_calls=[{
                "id": "c1",
                "type": "function",
                "function": {"name": "UNIQUEFUNCNAME", "arguments": "{}"},
            }],
        )
        results = db.search_messages("UNIQUEFUNCNAME")
        assert len(results) == 1

    def test_update_message_reindexes_tool_fields(self, db):
        """UPDATE must refresh the FTS row so old tokens drop out and new tokens appear."""
        db.create_session(session_id="s1", source="cli")
        db.append_message(
            "s1", role="assistant", content="",
            tool_name="ORIGINALTOOL",
        )
        assert len(db.search_messages("ORIGINALTOOL")) == 1

        def _update(conn):
            conn.execute(
                "UPDATE messages SET tool_name = ? WHERE session_id = ?",
                ("RENAMEDTOOL", "s1"),
            )
        db._execute_write(_update)

        assert db.search_messages("ORIGINALTOOL") == []
        assert len(db.search_messages("RENAMEDTOOL")) == 1





class TestSessionIdSearch:
    """Session id search backs Desktop's Search Sessions UX."""

    def _seed(self, db, sid, *, content="ordinary message", archived=False, source="cli"):
        db.create_session(session_id=sid, source=source, model="test-model")
        db.append_message(session_id=sid, role="user", content=content)
        if archived:
            db.set_session_archived(sid, True)

    def test_search_sessions_by_id_matches_exact_prefix_and_substring(self, db):
        self._seed(db, "20260603_090200_abcd12", content="content without id")
        self._seed(db, "20260602_111111_other99", content="other content")

        assert [s["id"] for s in db.search_sessions_by_id("20260603_090200_abcd12")] == [
            "20260603_090200_abcd12"
        ]
        assert [s["id"] for s in db.search_sessions_by_id("20260603")] == ["20260603_090200_abcd12"]
        assert [s["id"] for s in db.search_sessions_by_id("ABCD12")] == ["20260603_090200_abcd12"]

    def test_search_sessions_by_id_can_include_or_exclude_archived(self, db):
        self._seed(db, "20260603_090200_live")
        self._seed(db, "20260603_090200_archived", archived=True)

        included = {s["id"] for s in db.search_sessions_by_id("20260603_090200", include_archived=True)}
        excluded = {s["id"] for s in db.search_sessions_by_id("20260603_090200", include_archived=False)}

        assert included == {"20260603_090200_live", "20260603_090200_archived"}
        assert excluded == {"20260603_090200_live"}

    def test_search_sessions_by_id_matches_projected_lineage_root_id(self, db):
        root = "20260602_235959_root99"
        tip = "20260603_010000_tip01"
        db.create_session(session_id=root, source="cli")
        db.append_message(root, role="user", content="root conversation")
        db.end_session(root, "compression")
        db.create_session(session_id=tip, source="cli", parent_session_id=root)
        db.append_message(tip, role="user", content="continued conversation")

        matches = db.search_sessions_by_id("root99")

        assert [s["id"] for s in matches] == [tip]
        assert matches[0]["_lineage_root_id"] == root

    def test_search_sessions_by_id_respects_limit_and_prioritizes_exact_matches(self, db):
        self._seed(db, "20260603_090200_abcd12")
        self._seed(db, "20260603_090200_abcd12_child")
        self._seed(db, "x_20260603_090200_abcd12")

        ids = [s["id"] for s in db.search_sessions_by_id("20260603_090200_abcd12", limit=2)]

        assert ids == ["20260603_090200_abcd12", "20260603_090200_abcd12_child"]






class TestSessionPinAndStaleArchive:
    """Pin as a durable keep flag + last-activity-based stale auto-archive."""

    def _pinned(self, db, sid):
        row = db._conn.execute(
            "SELECT pinned FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        return row["pinned"] if row is not None else None

    def _make_idle(self, db, sid, *, days_idle, source="cli"):
        """A session whose latest activity was ``days_idle`` days ago."""
        db.create_session(session_id=sid, source=source)
        db.append_message(session_id=sid, role="user", content=f"msg {sid}")
        old = time.time() - days_idle * 86400
        db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (old, sid))
        db._conn.execute(
            "UPDATE messages SET timestamp = ? WHERE session_id = ?", (old, sid)
        )
        db._conn.commit()

    # ── pin flag ──────────────────────────────────────────────────────────
    def test_set_session_pinned_roundtrip(self, db):
        db.create_session(session_id="s1", source="cli")
        assert db.set_session_pinned("s1", True) is True
        assert self._pinned(db, "s1") == 1
        assert db.set_session_pinned("s1", False) is True
        assert self._pinned(db, "s1") == 0



    # ── pinned back-fill past the page window ─────────────────────────────
    def test_pinned_session_survives_the_limit_window(self, db):
        """A pin outlives recency: paging must not evict a pinned row.

        Without ``include_pinned`` the desktop's Pinned section renders empty
        for any conversation that has aged off the sidebar page.
        """
        for i in range(6):
            self._make_idle(db, f"s{i}", days_idle=6 - i)
        db.set_session_pinned("s0", True)  # the oldest — off a 3-row page

        def ids(**kw):
            return [
                s["id"]
                for s in db.list_sessions_rich(
                    limit=3, min_message_count=1, order_by_last_active=True, **kw
                )
            ]

        page = ids()
        assert "s0" not in page, "precondition: the pin is off the page"

        with_pins = ids(include_pinned=True)
        assert "s0" in with_pins
        # The page itself is untouched; the pin is additive.
        assert with_pins[:3] == page
        assert len(with_pins) == len(page) + 1




    # ── stale archive ─────────────────────────────────────────────────────


    def test_pinned_sessions_are_spared(self, db):
        self._make_idle(db, "keep", days_idle=10)
        db.set_session_pinned("keep", True)

        assert db.archive_stale_sessions(3) == 0
        assert db.get_session("keep")["archived"] == 0
        # Opting out of the pin guard sweeps it.
        assert db.archive_stale_sessions(3, exclude_pinned=False) == 1
        assert db.get_session("keep")["archived"] == 1




    # ── throttled wrapper ─────────────────────────────────────────────────



class TestSessionPinned:
    def test_pinned_defaults_false_and_roundtrips_through_reopen(self, tmp_path):
        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            db.create_session(session_id="s1", source="cli")
            assert db.get_session("s1")["pinned"] == 0

            assert db.set_session_pinned("s1", True) is True
        finally:
            db.close()

        reopened = SessionDB(db_path=db_path)
        try:
            session = reopened.get_session("s1")
            assert session["pinned"] == 1
            rich = reopened.list_sessions_rich(limit=10)
            assert [(row["id"], bool(row["pinned"])) for row in rich] == [("s1", True)]
        finally:
            reopened.close()
    def test_projected_compression_tip_keeps_root_pin(self, db):
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session("tip", source="cli", parent_session_id="root")
        db.append_message("tip", "user", "hello")

        assert db.set_session_pinned("root", True) is True

        [row] = db.list_sessions_rich(limit=10, order_by_last_active=True)
        assert row["id"] == "tip"
        assert row["_lineage_root_id"] == "root"
        assert bool(row["pinned"]) is True
    def test_set_pinned_nonexistent_session(self, db):
        assert db.set_session_pinned("missing", True) is False


class TestDeleteSessionOrphansChildren:
    def test_delete_orphans_children(self, db):
        """Deleting a parent session orphans its children."""
        db.create_session(session_id="parent", source="cli")
        db.create_session(session_id="child", source="cli", parent_session_id="parent")
        db.create_session(session_id="grandchild", source="cli", parent_session_id="child")

        # Should not raise IntegrityError
        result = db.delete_session("parent")
        assert result is True
        assert db.get_session("parent") is None
        # Child is orphaned, not deleted
        child = db.get_session("child")
        assert child is not None
        assert child["parent_session_id"] is None
        # Grandchild is untouched
        grandchild = db.get_session("grandchild")
        assert grandchild is not None
        assert grandchild["parent_session_id"] == "child"


class TestFTS5ToolCallMigration:
    """v11 migration: pre-existing state.db with old external-content FTS tables
    must be re-indexed so tool_name / tool_calls become searchable after upgrade."""

    def test_v10_to_v11_upgrade_backfills_tool_fields(self, tmp_path):
        """Simulate an existing user: build a v10-shaped DB by hand, insert a
        row with tool_calls, then open via SessionDB (which runs migrations).
        After upgrade, the tool_calls token must be searchable."""
        import sqlite3

        db_path = tmp_path / "legacy.db"

        # Build the pre-v11 schema by hand: external-content FTS tables +
        # old triggers that only reference new.content.
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (10);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                started_at REAL,
                ended_at REAL,
                title TEXT,
                parent_session_id TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                api_call_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_name TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                token_count INTEGER,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT,
                reasoning_details TEXT,
                codex_reasoning_items TEXT,
                codex_message_items TEXT
            );

            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content, content=messages, content_rowid=id
            );
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;

            CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
                content, content=messages, content_rowid=id, tokenize='trigram'
            );
            CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, new.content);
            END;
        """)
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "cli", time.time()),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role, content, tool_name, tool_calls) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", time.time(), "assistant", "", "LEGACYTOOL",
             '{"function":{"name":"web_search","arguments":"{\\"q\\":\\"LEGACYARG\\"}"}}'),
        )
        conn.commit()

        # Verify the legacy FTS rows don't contain the tool tokens yet.
        legacy_hits = conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'LEGACYTOOL'"
        ).fetchall()
        assert legacy_hits == [], "sanity: legacy FTS must NOT contain tool_name"
        conn.close()

        # Open via SessionDB — the legacy DB is detected as optimizable but
        # NOT auto-migrated (opt-in). Its old content-only index still works
        # for content, but doesn't yet cover tool_name/tool_calls (#16751).
        session_db = SessionDB(db_path=db_path)
        try:
            assert session_db.fts_optimize_available() is True

            # `hermes db optimize` performs the v23 transition; afterwards the
            # tool fields are searchable.
            result = session_db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert len(session_db.search_messages("LEGACYTOOL")) == 1, \
                "v23 optimize must index tool_name into FTS"
            assert len(session_db.search_messages("LEGACYARG")) == 1, \
                "v23 optimize must index tool_calls JSON into FTS"
            # schema_version bumped once the FTS layer is v23
            from hermes_state import SCHEMA_VERSION
            row = session_db._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            version = row["version"] if hasattr(row, "keys") else row[0]
            assert version == SCHEMA_VERSION
        finally:
            session_db.close()


def test_refresh_compression_lock_requires_holder_and_preserves_reclaimability(db, monkeypatch):
    db.create_session("s1", "cli")

    monkeypatch.setattr(hermes_state.time, "time", lambda: 1000.0)
    assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=10.0) is True

    original_expires = db._conn.execute(
        "SELECT expires_at FROM compression_locks WHERE session_id = ?",
        ("s1",),
    ).fetchone()[0]

    monkeypatch.setattr(hermes_state.time, "time", lambda: 1005.0)
    assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=10.0) is True
    refreshed_expires = db._conn.execute(
        "SELECT expires_at FROM compression_locks WHERE session_id = ?",
        ("s1",),
    ).fetchone()[0]
    assert refreshed_expires > original_expires

    assert db.refresh_compression_lock("s1", "holder-b", ttl_seconds=10.0) is False

    monkeypatch.setattr(hermes_state.time, "time", lambda: 1016.0)
    assert db.try_acquire_compression_lock("s1", "holder-b", ttl_seconds=10.0) is True




def test_compression_ineffective_count_round_trips(db):
    db.create_session("s1", "cli")

    assert db.get_compression_ineffective_count("s1") == 0
    db.set_compression_ineffective_count("s1", 2)
    assert db.get_compression_ineffective_count("s1") == 2
    # Clearing (real usage dipped below the threshold) round-trips too.
    db.set_compression_ineffective_count("s1", 0)
    assert db.get_compression_ineffective_count("s1") == 0
    # Negative and missing-session inputs are normalized/ignored.
    db.set_compression_ineffective_count("s1", -3)
    assert db.get_compression_ineffective_count("s1") == 0
    assert db.get_compression_ineffective_count("nope") == 0
    assert db.get_compression_ineffective_count("") == 0


def test_gateway_session_recovery_reopens_ws_orphan_reap_rows(db):
    """Rows wrongly ended by the TUI ws-orphan reaper must be recoverable (#63207)."""
    db.create_session(
        "reaped-gw-session",
        "telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    db.append_message("reaped-gw-session", "user", "hello")
    db.end_session("reaped-gw-session", "ws_orphan_reap")

    recovered = db.find_latest_gateway_session_for_peer(
        source="telegram",
        user_id="user-1",
        session_key="agent:main:telegram:dm:chat-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    assert recovered["id"] == "reaped-gw-session"

    db.reopen_session("reaped-gw-session")
    row = db.get_session("reaped-gw-session")
    assert row["ended_at"] is None
    assert row["end_reason"] is None


def test_set_expiry_finalized_round_trip(db):
    db.create_session("gw-exp", "telegram", session_key="agent:main:telegram:dm:x")
    row = db.get_session("gw-exp")
    assert not row["expiry_finalized"]
    db.set_expiry_finalized("gw-exp")
    assert db.get_session("gw-exp")["expiry_finalized"] == 1
    db.set_expiry_finalized("gw-exp", False)
    assert db.get_session("gw-exp")["expiry_finalized"] == 0


