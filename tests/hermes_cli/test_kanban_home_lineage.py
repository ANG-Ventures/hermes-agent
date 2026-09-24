"""Kanban home = session LINEAGE (kanban_db.home_ids) and its consumers:
list --home, show's home label, the home-session guard, kanban.home_guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
import hermes_state
from hermes_state import SessionDB

KEY = "agent:main:discord:group:111"
OTHER_KEY = "agent:main:discord:group:222"
A, B, C = "20260922_000001_a", "20260922_000002_b", "20260922_000003_c"
X1, X2 = "20260922_000011_x1", "20260922_000012_x2"
LONE = "20260922_000021_new"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    monkeypatch.setattr(kb, "_UNSTAMPED_WARNED", [False])
    kb.init_db()
    return h


@pytest.fixture
def lineage(home):
    """A -> B -> C rotations in one chat; X1 -> X2 in another chat whose
    root claims A as parent (cross-key link must not leak); LONE = /new."""
    db = SessionDB(db_path=_state_db())
    try:
        db.create_session(A, "discord", session_key=KEY)
        db.create_session(B, "discord", session_key=KEY, parent_session_id=A)
        db.create_session(C, "discord", session_key=KEY, parent_session_id=B)
        db.create_session(X1, "discord", session_key=OTHER_KEY, parent_session_id=A)
        db.create_session(X2, "discord", session_key=OTHER_KEY, parent_session_id=X1)
        db.create_session(LONE, "discord", session_key=KEY)
    finally:
        db.close()
    return home


def _state_db() -> Path:
    # The gateway resolves state.db via hermes_state._default_db_path (the
    # test conftest pins it under the sandbox) -- write where home_ids reads.
    return Path(hermes_state._default_db_path())


def _card(conn, sid, assignee="worker-a"):
    tid = kb.create_task(conn, title="card", assignee=assignee, session_id=sid)
    assert kb.block_task(conn, tid, reason="needs input")
    return tid


# --- helper ---------------------------------------------------------------


def test_three_deep_chain_is_one_home_from_any_member(lineage):
    for sid in (A, B, C):
        assert kb.home_ids(sid) == {A, B, C}


def test_other_session_key_chain_never_matches(lineage):
    assert kb.home_ids(X2) == {X1, X2}
    assert not (kb.home_ids(A) & {X1, X2})


def test_new_without_parent_is_alone(lineage):
    assert kb.home_ids(LONE) == {LONE}


def test_depth_is_bounded(home):
    db = SessionDB(db_path=_state_db())
    try:
        ids = [f"20260922_{i:06d}_d" for i in range(15)]
        db.create_session(ids[0], "discord", session_key=KEY)
        for prev, cur in zip(ids, ids[1:]):
            db.create_session(cur, "discord", session_key=KEY, parent_session_id=prev)
    finally:
        db.close()
    got = kb.home_ids(ids[0])
    assert ids[kb.HOME_LINEAGE_MAX_DEPTH] in got
    assert ids[kb.HOME_LINEAGE_MAX_DEPTH + 1] not in got


def test_fail_open_without_state_db(home):
    assert not _state_db().exists()
    assert kb.home_ids(A) == {A}
    assert kb.home_ids("") == frozenset()


def test_fail_open_on_unreadable_db(home):
    _state_db().parent.mkdir(parents=True, exist_ok=True)
    _state_db().write_text("not a sqlite database")
    assert kb.home_ids(A) == {A}


# --- guard ----------------------------------------------------------------


def test_guard_allows_across_rotation(lineage):
    with kb.connect_closing() as conn:
        tid = _card(conn, A)
        with kb.mutation_actor(session_ids=(C,), profile="apollo"):
            assert kb.unblock_task(conn, tid)
        assert kb.list_comments(conn, tid) == []


def test_guard_refuses_across_session_keys(lineage):
    with kb.connect_closing() as conn:
        tid = _card(conn, A)
        with kb.mutation_actor(session_ids=(X2,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError):
                kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"


def test_guard_default_is_refuse(home):
    assert kb.home_guard_mode() == "refuse"


def test_guard_refuses_when_state_db_absent(home):
    with kb.connect_closing() as conn:
        tid = _card(conn, A)
        with kb.mutation_actor(session_ids=(C,), profile="apollo"):
            with pytest.raises(kb.ForeignSessionMutationError):
                kb.unblock_task(conn, tid)


def test_home_guard_warn_allows_with_stderr_line(home, monkeypatch, capsys):
    monkeypatch.setattr(kb, "home_guard_mode", lambda: "warn")
    with kb.connect_closing() as conn:
        tid = _card(conn, A)
        with kb.mutation_actor(session_ids=(X2,), profile="apollo"):
            assert kb.unblock_task(conn, tid)
    assert "kanban.home_guard=warn" in capsys.readouterr().err


def test_home_guard_mode_reads_config(home):
    (home / "config.yaml").write_text("kanban:\n  home_guard: warn\n")
    assert kb.home_guard_mode() == "warn"
    (home / "config.yaml").write_text("kanban:\n  home_guard: bogus\n")
    assert kb.home_guard_mode() == "refuse"


# --- CLI ------------------------------------------------------------------


def test_cli_list_home_spans_lineage(lineage, monkeypatch):
    with kb.connect_closing() as conn:
        mine_old = _card(conn, A)
        mine_new = _card(conn, C)
        theirs = _card(conn, X1)
        lone = _card(conn, LONE)
    monkeypatch.setenv("HERMES_SESSION_ID", B)
    ids = {t["id"] for t in json.loads(kc.run_slash("list --home --json"))}
    assert {mine_old, mine_new} <= ids
    assert theirs not in ids and lone not in ids


def test_cli_show_label_spans_lineage(lineage, monkeypatch):
    with kb.connect_closing() as conn:
        mine = _card(conn, A)
        theirs = _card(conn, X1)
    monkeypatch.setenv("HERMES_SESSION_ID", C)
    assert "home:      this-session" in kc.run_slash(f"show {mine}")
    assert f"home:      other ({X1})" in kc.run_slash(f"show {theirs}")


def test_cli_list_default_view_spans_lineage(lineage, monkeypatch):
    """The default split view uses the SAME home definition as ``--home``:
    after a rotation (A -> B -> C, one session_key) a chat's pre-rotation
    cards are still home, not "from other sessions" (Argus r1 B1)."""
    with kb.connect_closing() as conn:
        mine_old = _card(conn, A)
        mine_new = _card(conn, C)
        theirs = _card(conn, X1)
        lone = _card(conn, LONE)
    monkeypatch.setenv("HERMES_SESSION_ID", C)
    out = kc.run_slash("list")
    this, _, other = out.partition("OTHER SESSIONS")
    assert "THIS SESSION (2)" in this
    assert mine_old in this and mine_new in this
    assert theirs in other and lone in other
    assert theirs not in this and lone not in this
    home_view = {t["id"] for t in json.loads(kc.run_slash("list --home --json"))}
    shown = {tid for tid in (mine_old, mine_new, theirs, lone) if tid in this}
    assert shown == home_view & {mine_old, mine_new, theirs, lone}


# --- per-process cache + earliest start (t_11f2cf60) ------------------------


def _spy_connects(monkeypatch):
    calls = []
    real = kb.sqlite3.connect

    def spy(*a, **kw):
        calls.append(a[0] if a else kw.get("database"))
        return real(*a, **kw)

    monkeypatch.setattr(kb.sqlite3, "connect", spy)
    return calls


def test_cache_second_lookup_opens_no_connection(lineage, monkeypatch):
    kb.clear_home_ids_cache()
    calls = _spy_connects(monkeypatch)
    assert kb.home_ids(C) == {A, B, C}
    n = len(calls)
    assert n >= 1
    assert kb.home_ids(C) == {A, B, C}
    assert len(calls) == n  # warm: served from the per-process cache


def test_cache_expires_after_ttl(lineage, monkeypatch):
    kb.clear_home_ids_cache()
    calls = _spy_connects(monkeypatch)
    kb.home_ids(C)
    n = len(calls)
    monkeypatch.setattr(kb, "HOME_IDS_CACHE_TTL_S", 0.0)
    assert kb.home_ids(C) == {A, B, C}
    assert len(calls) > n


def test_fail_open_answer_is_not_cached(home):
    kb.clear_home_ids_cache()
    assert kb.home_ids(B) == {B}  # no state.db yet
    db = SessionDB(db_path=_state_db())
    try:
        db.create_session(A, "discord", session_key=KEY)
        assert kb.home_ids(B) == {B}  # db exists, B's row does not: still exact
        db.create_session(B, "discord", session_key=KEY, parent_session_id=A)
    finally:
        db.close()
    assert kb.home_ids(B) == {A, B}  # neither miss was cached


def test_cache_is_bounded(lineage, monkeypatch):
    kb.clear_home_ids_cache()
    monkeypatch.setattr(kb, "HOME_IDS_CACHE_MAX", 2)
    for sid in (A, B, C, LONE):
        kb.home_ids(sid)
    assert len(kb._HOME_CACHE) == 2
    assert {k[0] for k in kb._HOME_CACHE} == {C, LONE}  # oldest evicted first


def test_home_lineage_started_at_is_earliest_member(lineage):
    kb.clear_home_ids_cache()
    import sqlite3

    conn = sqlite3.connect(_state_db())
    for sid, ts in ((A, 1000.0), (B, 2000.0), (C, 3000.0), (X1, 10.0), (LONE, 5.0)):
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (ts, sid))
    conn.commit()
    conn.close()
    ids, started = kb.home_lineage(C)
    assert ids == {A, B, C} and started == 1000.0  # X1/LONE are other homes
    assert kb.home_lineage(B) == (frozenset({A, B, C}), 1000.0)


def test_home_lineage_start_unknown_on_fail_open(home):
    assert kb.home_lineage(A) == (frozenset({A}), None)
