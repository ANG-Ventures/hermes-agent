"""Post-#951 follow-ups: default ``list`` view splits home vs other sessions,
pings carry the card's home, and ``task_events`` record actor provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

HOME = "20260922_000000_home"
OTHER = "20260922_000000_other"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME", "_HERMES_GATEWAY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_caller_session_lineage", lambda sid: ())
    monkeypatch.setattr(kb, "_UNSTAMPED_WARNED", [False])
    kb.init_db()
    return home


def _card(conn, session_id, title="card"):
    return kb.create_task(conn, title=title, assignee="w", session_id=session_id)


# --- item 2: list default view ---------------------------------------------


def test_list_default_shows_home_then_collapsed_others(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        mine = _card(conn, HOME, "mine")
        t1 = _card(conn, OTHER, "theirs1")
        t2 = _card(conn, None, "legacy")
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    out = kc.run_slash("list")
    assert mine in out
    assert t1 not in out and t2 not in out
    assert "2 cards from other sessions (--all to show)" in out
    # the collapsed line comes after the home rows
    assert out.index(mine) < out.index("from other sessions")


def test_list_all_restores_flat_listing(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        ids = [_card(conn, HOME), _card(conn, OTHER), _card(conn, None)]
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    out = kc.run_slash("list --all")
    assert all(i in out for i in ids)
    assert "from other sessions" not in out


def test_list_without_session_is_unchanged(kanban_home):
    with kb.connect_closing() as conn:
        ids = [_card(conn, HOME), _card(conn, OTHER)]
    out = kc.run_slash("list")
    assert all(i in out for i in ids)
    assert "from other sessions" not in out


def test_list_home_and_session_flags_stay_explicit(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        mine = _card(conn, HOME)
        theirs = _card(conn, OTHER)
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    out = kc.run_slash("list --home")
    assert mine in out and theirs not in out
    assert "from other sessions" not in out
    out = kc.run_slash(f"list --session {OTHER}")
    assert theirs in out and mine not in out
    assert "from other sessions" not in out


def test_list_json_is_unfiltered_by_default_view(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        ids = {_card(conn, HOME), _card(conn, OTHER)}
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    got = {t["id"] for t in json.loads(kc.run_slash("list --json"))}
    assert ids <= got


def test_list_no_home_cards_says_so(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        _card(conn, OTHER)
    monkeypatch.setenv("HERMES_SESSION_ID", HOME)
    out = kc.run_slash("list")
    assert "(no cards from this session)" in out
    assert "1 card from other sessions (--all to show)" in out


# --- item 3: pings carry home ----------------------------------------------


def test_format_home_line_from_origin():
    from gateway.kanban_watchers import format_home_line

    row = {"origin_json": json.dumps({"platform": "discord",
                                      "chat_name": "sub-vps-n",
                                      "chat_id": "155"}),
           "source": "discord"}
    assert format_home_line(HOME, row) == (
        f"home: discord #sub-vps-n · session {HOME}"
    )


def test_format_home_line_fallbacks():
    from gateway.kanban_watchers import format_home_line

    assert format_home_line(None) == ""
    assert format_home_line("  ") == ""
    assert format_home_line(HOME) == f"home: session {HOME}"
    assert format_home_line(HOME, {"source": "cli"}) == (
        f"home: cli · session {HOME}"
    )
    assert format_home_line(
        HOME, {"source": "telegram", "display_name": "#ops", "origin_json": "{bad"}
    ) == f"home: telegram #ops · session {HOME}"


def test_resolve_home_line_reads_state_db(kanban_home):
    from gateway.kanban_watchers import _resolve_home_line
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session(
            session_id=HOME,
            source="discord",
            origin_json=json.dumps({"platform": "discord", "chat_name": "sub-vps-n"}),
        )
    finally:
        db.close()
    assert _resolve_home_line(HOME) == f"home: discord #sub-vps-n · session {HOME}"
    assert _resolve_home_line("unknown_sid") == "home: session unknown_sid"
    assert _resolve_home_line(None) == ""


def test_notifier_appends_home_to_ping_and_wake_text():
    """Source contract: both the passive ping and the wake turn append the
    resolved home line, which is computed in the worker-thread collector."""
    import inspect

    import gateway.kanban_watchers as kw

    src = inspect.getsource(kw)
    assert '"home": home_line' in src
    assert 'msg += "\\n" + d["home"]' in src
    assert '_synth += "\\n" + d["home"]' in src


# --- item 4: task_events actor provenance ----------------------------------


def _events(conn, tid):
    return conn.execute(
        "SELECT kind, actor_profile, actor_session_id FROM task_events "
        "WHERE task_id = ? ORDER BY id", (tid,)
    ).fetchall()


def test_events_carry_bound_actor(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, HOME)
        with kb.mutation_actor(session_ids=(HOME,), profile="apollo"):
            assert kb.block_task(conn, tid, reason="needs input")
        rows = [r for r in _events(conn, tid) if r["kind"] == "blocked"]
        assert rows and rows[-1]["actor_profile"] == "apollo"
        assert rows[-1]["actor_session_id"] == HOME


def test_events_fall_back_to_env(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    monkeypatch.setenv("HERMES_PROFILE", "daedalus")
    with kb.connect_closing() as conn:
        tid = _card(conn, HOME)
        kb.add_comment(conn, tid, author="daedalus", body="hi")
        rows = _events(conn, tid)
        assert rows
        assert all(r["actor_profile"] == "daedalus" for r in rows)
        assert all(r["actor_session_id"] == OTHER for r in rows)


def test_events_null_without_identity(kanban_home):
    with kb.connect_closing() as conn:
        tid = _card(conn, HOME)
        rows = _events(conn, tid)
        assert rows
        assert all(r["actor_profile"] is None for r in rows)
        assert all(r["actor_session_id"] is None for r in rows)


def test_in_gateway_bound_contextvar_wins_over_env(kanban_home, monkeypatch):
    from gateway import session_context as sc

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SESSION_ID", OTHER)
    monkeypatch.setattr(sc, "resolve_current_session_id", lambda: HOME)
    with kb.connect_closing() as conn:
        tid = _card(conn, HOME)
        assert all(r["actor_session_id"] == HOME for r in _events(conn, tid))


def test_migration_adds_actor_columns_to_legacy_db(kanban_home):
    with kb.connect_closing() as conn:
        conn.execute("DROP TABLE task_events")
        conn.execute(
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, "
            "payload TEXT, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES ('t_x', 'created', 1)"
        )
        conn.commit()
    kb.init_db()
    with kb.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_events)")}
        assert {"actor_profile", "actor_session_id"} <= cols
        legacy = conn.execute(
            "SELECT actor_profile, actor_session_id FROM task_events WHERE task_id='t_x'"
        ).fetchone()
        assert legacy["actor_profile"] is None and legacy["actor_session_id"] is None
