"""Per-run / per-session provenance on kanban comments.

Incident: two concurrent sessions running the SAME profile (``apollo``) issued
contradictory intent on one card — one parked it, the other granted a waiver and
re-dispatched it. Both comments rendered as author ``apollo``, so neither the
board nor a human reader could tell the two sessions apart.

Fix under test: ``task_comments`` carries ``run_id`` (the dispatcher run that
wrote it) and ``session_ref`` (a bounded, non-reversible fingerprint of the
originating session id), and every read surface renders them.

Hermeticity (AC6): the fixture strips every ``HERMES_KANBAN*`` pin and asserts
the resolved DB is under ``tmp_path`` and NOT under a live board directory
before anything writes. ``HERMES_KANBAN_DB`` is checked *before* anything
``HERMES_HOME``-derived, so ``HERMES_HOME=<tmp>`` alone does not sandbox kanban.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with every kanban path pin stripped."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN")]:
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()

    # Fail closed before any write: the resolved DB must live in the sandbox.
    resolved = kb.kanban_db_path()
    assert str(resolved).startswith(str(tmp_path)), (
        f"kanban DB escaped the sandbox: {resolved}"
    )
    assert "/.hermes/kanban/boards/" not in str(resolved), (
        f"kanban DB resolved onto a live board: {resolved}"
    )
    return home


# ---------------------------------------------------------------------------
# session_ref derivation (bounded, non-secret — AC4)
# ---------------------------------------------------------------------------


class TestDeriveSessionRef:
    def test_stable_and_bounded(self):
        a = kb.derive_session_ref("20260809_190412_a1b2c3")
        b = kb.derive_session_ref("20260809_190412_a1b2c3")
        assert a == b
        assert len(a) == 12
        assert all(ch in "0123456789abcdef" for ch in a)

    def test_distinct_sessions_get_distinct_refs(self):
        assert kb.derive_session_ref("session-A") != kb.derive_session_ref("session-B")

    def test_does_not_leak_the_raw_session_id(self):
        """A gateway session id can embed PII (``sms:+15551234567``). The stored
        ref must not contain any substring of it."""
        raw = "sms:+15551234567"
        ref = kb.derive_session_ref(raw)
        assert raw not in ref
        assert "5551234567" not in ref
        assert "+" not in ref and ":" not in ref

    def test_empty_or_none_is_none(self):
        assert kb.derive_session_ref(None) is None
        assert kb.derive_session_ref("") is None
        assert kb.derive_session_ref("   ") is None


# ---------------------------------------------------------------------------
# Persistence (AC1) + write-side validation (AC2/AC4)
# ---------------------------------------------------------------------------


class TestAddCommentPersistsProvenance:
    def test_same_profile_two_sessions_are_distinguishable(self, fresh_home):
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="contended card")
            ref_a = kb.derive_session_ref("apollo-session-A")
            ref_b = kb.derive_session_ref("apollo-session-B")
            kb.add_comment(
                conn, tid, author="apollo", body="parking this, unassigning",
                session_ref=ref_a,
            )
            kb.add_comment(
                conn, tid, author="apollo", body="waiver granted, re-dispatching",
                session_ref=ref_b,
            )

            comments = kb.list_comments(conn, tid)
            assert [c.author for c in comments] == ["apollo", "apollo"]
            # The whole point: same author, different persisted provenance.
            assert comments[0].session_ref == ref_a
            assert comments[1].session_ref == ref_b
            assert comments[0].session_ref != comments[1].session_ref
        finally:
            conn.close()

    def test_run_id_is_persisted_and_read_back(self, fresh_home):
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="worker card")
            kb.add_comment(conn, tid, author="daedalus", body="handoff", run_id=327)
            c = kb.list_comments(conn, tid)[0]
            assert c.run_id == 327
        finally:
            conn.close()

    def test_provenance_visible_in_comments_after_cursor(self, fresh_home):
        """The live worker bridge reads through ``list_comments_after`` — it must
        carry provenance too, or a mid-run injected note is unattributable."""
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="bridge card")
            ref = kb.derive_session_ref("operator-session")
            kb.add_comment(conn, tid, author="apollo", body="note", session_ref=ref)
            rows = kb.list_comments_after(conn, tid, after_id=0)
            assert rows[0].session_ref == ref
        finally:
            conn.close()

    def test_commented_event_carries_the_run_id(self, fresh_home):
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="evented")
            kb.add_comment(conn, tid, author="daedalus", body="x", run_id=42)
            commented = [e for e in kb.list_events(conn, tid) if e.kind == "commented"]
            assert commented and commented[-1].run_id == 42
        finally:
            conn.close()

    def test_malformed_session_ref_is_rejected(self, fresh_home):
        """The write choke point only accepts the bounded fingerprint shape, so
        no caller can smuggle a long / structured / PII-bearing value in."""
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="guarded")
            for bad in ("sms:+15551234567", "APOLLO", "z" * 12, "abc", "a" * 13):
                with pytest.raises(ValueError):
                    kb.add_comment(
                        conn, tid, author="apollo", body="x", session_ref=bad,
                    )
            assert kb.list_comments(conn, tid) == []
        finally:
            conn.close()

    def test_malformed_run_id_is_rejected(self, fresh_home):
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="guarded2")
            for bad in ("not-an-int", 0, -1):
                with pytest.raises(ValueError):
                    kb.add_comment(conn, tid, author="apollo", body="x", run_id=bad)
            assert kb.list_comments(conn, tid) == []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Shared author formatter (one renderer, every surface)
# ---------------------------------------------------------------------------


class TestFormatCommentAuthor:
    def test_run_and_session(self):
        out = kb.format_comment_author("apollo", run_id=327, session_ref="3f2a9c1b0d44")
        assert "apollo" in out and "327" in out and "3f2a9c1b0d44" in out

    def test_session_only(self):
        out = kb.format_comment_author("apollo", session_ref="3f2a9c1b0d44")
        assert "3f2a9c1b0d44" in out
        assert "run" not in out

    def test_run_only(self):
        out = kb.format_comment_author("daedalus", run_id=327)
        assert "327" in out
        assert "sess" not in out

    def test_legacy_row_is_explicitly_unknown(self):
        """AC3: a pre-migration comment must render an explicit unknown marker,
        never a bare author that reads as attributed."""
        out = kb.format_comment_author("apollo")
        assert out.startswith("apollo")
        assert "unknown" in out

    def test_two_sessions_render_differently(self):
        a = kb.format_comment_author("apollo", session_ref="aaaaaaaaaaaa")
        b = kb.format_comment_author("apollo", session_ref="bbbbbbbbbbbb")
        assert a != b


# ---------------------------------------------------------------------------
# Legacy compatibility (AC3)
# ---------------------------------------------------------------------------


def _legacy_comments_db(path: Path, *, drifted: bool) -> str:
    """Downgrade a real board's ``task_comments`` to its pre-provenance shape.

    Built by initializing the CURRENT schema and then replacing only
    ``task_comments`` — a hand-rolled minimal ``tasks`` table is not a faithful
    legacy board (``claim_lock`` / ``worker_pid`` predate this change and the
    run-backfill migration reads them), and testing against a fake shape proves
    nothing about a real upgrade.

    ``drifted=True`` also reproduces the pre-AUTOINCREMENT ``TEXT PRIMARY KEY``
    id, which sends the table through ``_rebuild_drifted_tables``.

    Returns the created task id.
    """
    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect(db_path=path)
    try:
        tid = kb.create_task(conn, title="old card")
    finally:
        conn.close()

    raw = sqlite3.connect(path)
    id_ddl = "id TEXT PRIMARY KEY" if drifted else (
        "id INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    raw.executescript(
        f"""
        DROP TABLE task_comments;
        CREATE TABLE task_comments (
            {id_ddl},
            task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_comments_task
            ON task_comments(task_id, created_at);
        """
    )
    raw.execute(
        "INSERT INTO task_comments (id, task_id, author, body, created_at) "
        "VALUES (?, ?, 'apollo', 'historical note', 100)",
        ("c1" if drifted else 1, tid),
    )
    raw.commit()
    raw.close()
    kb._INITIALIZED_PATHS.clear()
    return tid


@pytest.mark.parametrize("drifted", [False, True])
def test_legacy_comments_survive_migration(fresh_home, drifted):
    db = kb.kanban_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    tid = _legacy_comments_db(db, drifted=drifted)

    conn = kb.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_comments)")}
        assert {"run_id", "session_ref"} <= cols

        comments = kb.list_comments(conn, tid)
        assert len(comments) == 1, "migration dropped a historical comment"
        c = comments[0]
        assert c.author == "apollo"
        assert c.body == "historical note"
        assert c.run_id is None and c.session_ref is None
        assert "unknown" in kb.format_comment_author(
            c.author, run_id=c.run_id, session_ref=c.session_ref
        )

        # New comments on the migrated board still record provenance.
        ref = kb.derive_session_ref("post-migration-session")
        kb.add_comment(conn, tid, author="apollo", body="new", session_ref=ref)
        assert kb.list_comments(conn, tid)[-1].session_ref == ref
    finally:
        conn.close()


def test_rebuild_spec_keeps_the_provenance_columns(fresh_home):
    """A drifted-table rebuild copies only columns present in BOTH shapes, so
    the rebuild spec must carry the new columns or a rebuild silently erases
    provenance that the additive pass just added."""
    create_sql, _indexes = kb._REBUILD_SPECS["task_comments"]
    assert "run_id" in create_sql and "session_ref" in create_sql


def test_drifted_rebuild_preserves_provenance_values(fresh_home):
    """End-to-end: a drifted board whose comments already carry provenance keeps
    the values through the rebuild."""
    db = kb.kanban_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    tid = _legacy_comments_db(db, drifted=True)
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE task_comments ADD COLUMN run_id INTEGER")
    raw.execute("ALTER TABLE task_comments ADD COLUMN session_ref TEXT")
    raw.execute(
        "UPDATE task_comments SET run_id = 7, session_ref = 'abcdef123456'"
    )
    raw.commit()
    raw.close()
    kb._INITIALIZED_PATHS.clear()

    conn = kb.connect()
    try:
        # The rebuild must have happened (drifted TEXT id → INTEGER pk).
        id_col = next(
            r for r in conn.execute("PRAGMA table_info(task_comments)")
            if r["name"] == "id"
        )
        assert (id_col["type"] or "").upper() == "INTEGER" and id_col["pk"]
        c = kb.list_comments(conn, tid)[0]
        assert c.run_id == 7
        assert c.session_ref == "abcdef123456"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read surfaces: worker context + CLI
# ---------------------------------------------------------------------------


def test_worker_context_distinguishes_same_profile_sessions(fresh_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="contended card")
        ref_a = kb.derive_session_ref("apollo-session-A")
        ref_b = kb.derive_session_ref("apollo-session-B")
        kb.add_comment(conn, tid, author="apollo", body="parking this",
                       session_ref=ref_a)
        kb.add_comment(conn, tid, author="apollo", body="waiver granted",
                       session_ref=ref_b)
        ctx = kb.build_worker_context(conn, tid)
    finally:
        conn.close()

    assert ref_a in ctx and ref_b in ctx
    # Backtick stripping (the author-forgery hardening) must survive.
    assert "`" not in ref_a


def test_worker_context_marks_legacy_comments_unknown(fresh_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy thread")
        kb.add_comment(conn, tid, author="apollo", body="no provenance here")
        ctx = kb.build_worker_context(conn, tid)
    finally:
        conn.close()
    assert "unknown" in ctx


def test_worker_context_strips_backticks_from_provenance_render(fresh_home):
    """Author is operator-controlled (HERMES_PROFILE); the render must keep
    stripping backticks so it can't break out of the code span."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="quoting")
        kb.add_comment(conn, tid, author="ap`ollo", body="x",
                       session_ref=kb.derive_session_ref("s"))
        ctx = kb.build_worker_context(conn, tid)
    finally:
        conn.close()
    line = next(ln for ln in ctx.splitlines() if "comment from worker" in ln)
    assert "ap`ollo" not in line
    assert "apollo" in line


def test_cli_show_json_exposes_comment_provenance(fresh_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cli card")
        ref = kb.derive_session_ref("cli-session")
        kb.add_comment(conn, tid, author="apollo", body="note",
                       run_id=None, session_ref=ref)
    finally:
        conn.close()

    out = json.loads(kc.run_slash(f"show {tid} --json"))
    c = out["comments"][0]
    assert c["session_ref"] == ref
    assert c["run_id"] is None
    assert ref in c["author_display"]


def test_cli_show_human_output_renders_provenance(fresh_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cli card 2")
        ref = kb.derive_session_ref("cli-session-2")
        kb.add_comment(conn, tid, author="apollo", body="note", session_ref=ref)
    finally:
        conn.close()

    # run_slash CAPTURES stdout and returns it (shared by CLI + gateway), so
    # read the return value rather than capsys.
    printed = kc.run_slash(f"show {tid}")
    assert ref in printed
    assert f"apollo (sess {ref})" in printed


def test_cli_comment_stamps_the_session_when_present(fresh_home, monkeypatch):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cli comment card")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_SESSION_ID", "cli-agent-session")
    kc.run_slash(f"comment {tid} hello there")

    conn = kb.connect()
    try:
        c = kb.list_comments(conn, tid)[0]
    finally:
        conn.close()
    assert c.session_ref == kb.derive_session_ref("cli-agent-session")
