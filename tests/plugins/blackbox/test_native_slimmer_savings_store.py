"""PRD #1.5 Phase 1 — native_slimmer_savings schema + migration + UPSERT tests."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from plugins.blackbox import native_slimmer_store as nss
from plugins.blackbox.native_slimmer_schema import build_native_slimmer_event
from plugins.blackbox.native_slimmer_schema import RAW_SOURCE_TOOL_RESULT_RETURNED


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "turns.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    nss.ensure_schema(conn)
    return conn


def _event(action: str, *, saved_raw: int, saved_sq: int, key: str = "s|c|sha|art") -> dict:
    ev = build_native_slimmer_event(
        mode="active_lossless" if action == "replace" else "shadow",
        action=action,
        tool_name="terminal",
        session_id="s",
        tool_call_id="c",
        artifact_id="art",
        raw_sha256="sha",
        raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
        original_bytes=20000 + saved_raw,
        emitted_bytes=20000,
        classification_reason="large_text",
        status_quo_baseline_bytes=20000 + saved_sq,
    )
    ev["savings_key"] = key
    return ev


def test_migration_idempotent_and_additive(tmp_path: Path):
    conn = _conn(tmp_path)
    # second run is a no-op
    nss.ensure_schema(conn)
    assert nss.has_table(conn)
    assert nss.count_rows(conn) == 0


def test_old_db_without_table_gains_it(tmp_path: Path):
    # simulate an old turns.db that has `turns` but not the savings table
    db = tmp_path / "turns.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE turns (turn_id TEXT PRIMARY KEY)")
    conn.commit()
    assert not nss.has_table(conn)
    nss.ensure_schema(conn)
    assert nss.has_table(conn)
    # fetch on a fresh table returns empty, not a crash
    assert nss.fetch_between(0, time.time() + 1, conn=conn) == []


def test_insert_and_fetch(tmp_path: Path):
    conn = _conn(tmp_path)
    now = time.time()
    nss.insert_event(_event("would_replace", saved_raw=5000, saved_sq=5000),
                     model="claude-opus-4-8", provider="claude-api-proxy", created_at=now, conn=conn)
    rows = nss.fetch_between(now - 1, now + 1, conn=conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "would_replace"
    assert r["model"] == "claude-opus-4-8"
    assert r["saved_vs_status_quo_bytes"] == 5000
    assert r["created_at"] == pytest.approx(now)


def test_upsert_dedupe_single_row(tmp_path: Path):
    conn = _conn(tmp_path)
    now = time.time()
    # same savings_key twice (a retry) → exactly one row
    nss.insert_event(_event("would_replace", saved_raw=5000, saved_sq=5000), created_at=now, conn=conn)
    nss.insert_event(_event("would_replace", saved_raw=5000, saved_sq=5000), created_at=now, conn=conn)
    assert nss.count_rows(conn) == 1


def test_replace_supersedes_would_replace_full_row(tmp_path: Path):
    conn = _conn(tmp_path)
    first = time.time()
    nss.insert_event(_event("would_replace", saved_raw=5000, saved_sq=5000),
                     model="cheap-model", created_at=first, conn=conn)
    later = first + 100
    # realized replace with DIFFERENT savings + model supersedes the whole row
    nss.insert_event(_event("replace", saved_raw=9000, saved_sq=9000),
                     model="claude-opus-4-8", created_at=later, conn=conn)
    rows = nss.fetch_between(first - 1, later + 1, conn=conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "replace"
    assert r["saved_vs_status_quo_bytes"] == 9000  # byte cols superseded
    assert r["model"] == "claude-opus-4-8"          # model superseded
    assert r["created_at"] == pytest.approx(first)  # created_at is FIRST-SEEN, not moved


def test_would_replace_does_not_overwrite_replace(tmp_path: Path):
    conn = _conn(tmp_path)
    t0 = time.time()
    nss.insert_event(_event("replace", saved_raw=9000, saved_sq=9000),
                     model="claude-opus-4-8", created_at=t0, conn=conn)
    # a later shadow would_replace must NOT clobber the realized row
    nss.insert_event(_event("would_replace", saved_raw=1, saved_sq=1),
                     model="cheap-model", created_at=t0 + 50, conn=conn)
    rows = nss.fetch_between(t0 - 1, t0 + 100, conn=conn)
    assert len(rows) == 1
    assert rows[0]["action"] == "replace"
    assert rows[0]["saved_vs_status_quo_bytes"] == 9000  # unchanged


def test_prune_older_than(tmp_path: Path):
    conn = _conn(tmp_path)
    now = time.time()
    old = now - 40 * 86400
    nss.insert_event(_event("would_replace", saved_raw=1, saved_sq=1, key="old"), created_at=old, conn=conn)
    nss.insert_event(_event("would_replace", saved_raw=1, saved_sq=1, key="new"), created_at=now, conn=conn)
    cutoff = now - 30 * 86400
    deleted = nss.prune_older_than(cutoff, conn=conn)
    assert deleted == 1
    assert nss.count_rows(conn) == 1
    remaining = nss.fetch_between(0, now + 1, conn=conn)
    assert remaining[0]["savings_key"] == "new"


def test_migration_on_copy_of_live_db(tmp_path: Path):
    """If a live turns.db exists, migrating a COPY adds the table without harm."""
    from hermes_constants import get_hermes_home

    live = get_hermes_home() / "blackbox" / "turns.db"
    if not live.exists():
        pytest.skip("no live turns.db on this host")
    copy = tmp_path / "turns.db"
    shutil.copy2(live, copy)
    conn = sqlite3.connect(str(copy))
    conn.row_factory = sqlite3.Row
    # pre-existing turns rows survive; table is added
    pre_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    nss.ensure_schema(conn)
    assert nss.has_table(conn)
    post_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert post_turns == pre_turns  # migration did not touch existing data
