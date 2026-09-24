"""Post-#951 follow-ups: pings carry the card's home, and ``task_events``
record actor provenance. (The session-first ``list`` view is #998's.)"""

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


def test_notifier_delivers_home_line_in_ping_and_wake(tmp_path, monkeypatch):
    """Drive one real notifier tick (stub push adapter, notify+wake sub):
    the delivered text ping AND the injected wake turn both carry the card's
    resolved ``home:`` line (Argus r1 C1)."""
    import asyncio

    from gateway.config import Platform
    from gateway.kanban_watchers import _resolve_home_line
    from gateway.run import GatewayRunner
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "notify-home.db"))
    kb.init_db()
    sid = "agent:main:telegram:dm:chat-1"
    db = SessionDB()
    try:
        db.create_session(
            session_id=sid,
            source="telegram",
            origin_json=json.dumps({"platform": "telegram", "chat_name": "ops"}),
        )
    finally:
        db.close()
    expected = f"home: telegram #ops · session {sid}"
    assert _resolve_home_line(sid) == expected

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="home ping", assignee="worker", session_id=sid)
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                          chat_type="dm", delivery_mode="notify+wake")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    class Adapter:
        def __init__(self):
            self.sent, self.handled = [], []

        async def send(self, chat_id, text, metadata=None):
            self.sent.append(text)

        async def handle_message(self, event):
            self.handled.append(event)

    adapter = Adapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))

    assert len(adapter.sent) == 1 and tid in adapter.sent[0]
    assert expected in adapter.sent[0]
    assert len(adapter.handled) == 1
    assert expected in adapter.handled[0].text


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


def test_events_resolve_running_profile_for_session_callers(kanban_home, monkeypatch):
    """Chat/gateway caller: session bound in-process, no profile env (only
    worker spawns export it) -> actor_profile is the running profile, not
    NULL (Argus r1 C2)."""
    from gateway import session_context as sc
    from hermes_cli import profiles

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setattr(sc, "resolve_current_session_id", lambda: HOME)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "apollo")
    with kb.connect_closing() as conn:
        tid = _card(conn, HOME)
        kb.add_comment(conn, tid, author="apollo", body="hi")
        rows = _events(conn, tid)
        assert rows
        assert all(r["actor_profile"] == "apollo" for r in rows)
        assert all(r["actor_session_id"] == HOME for r in rows)


def test_events_without_session_do_not_guess_profile(kanban_home, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "apollo")
    with kb.connect_closing() as conn:
        tid = _card(conn, HOME)
        assert all(r["actor_profile"] is None for r in _events(conn, tid))
