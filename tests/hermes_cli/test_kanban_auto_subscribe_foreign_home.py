"""Auto-subscribe follows the card's HOME, never the calling chat (t_ea40ff83).

Incident 2026-11-03: a cron script queued from #github-alternatives minted
main-red fix cards with ``--session <the SLA lane's #apollo session>``. With
``kanban.cli_auto_subscribe`` on, the create subscribed the CALLING chat, so every
"handed off for review" line and wake reached #github-alternatives, whose Apollo
(correctly) refuses foreign cards, and the home session heard nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn

CALLER_SID = "20261018_220948_caller"
HOME_SID = "20260927_135034_home"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from gateway.session_context import reset_session_vars

    reset_session_vars()
    kb.init_db()
    (home / "config.yaml").write_text("kanban:\n  cli_auto_subscribe: true\n")
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        for sid, chat in ((CALLER_SID, "chat-caller"), (HOME_SID, "chat-home")):
            db.create_session(session_id=sid, source="discord", origin_json=json.dumps({
                "platform": "discord", "chat_id": chat, "chat_type": "group",
                "user_id": "u1", "scope_id": "g1"}))
    finally:
        db.close()
    # The caller is a turn in chat-caller (the detached-run / terminal bridge shape).
    monkeypatch.setenv("HERMES_SESSION_ID", CALLER_SID)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-caller")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "u1")
    return home


def _create(session: str) -> str:
    out = kc.run_slash(f'--board default create "card" --assignee worker1 --session {session}')
    m = re.search(r"Created\s+(t_[0-9a-f]+)\b", out)
    assert m, out
    return m.group(1)


def _chats(task_id: str) -> list[str]:
    conn = kb.connect()
    try:
        return sorted(s["chat_id"] for s in kbn.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def test_foreign_home_subscribes_home_chat_not_caller(kanban_home):
    tid = _create(HOME_SID)
    assert _chats(tid) == ["chat-home"]


def test_own_home_subscribes_caller_chat(kanban_home):
    tid = _create(CALLER_SID)
    assert _chats(tid) == ["chat-caller"]


def test_operator_home_keeps_caller(kanban_home):
    """Any operator session may act on an operator pseudo-home: the minting chat stays."""
    tid = _create("operator:apollo")
    assert _chats(tid) == ["chat-caller"]


def test_foreign_home_in_another_thread_never_reaches_caller(kanban_home, monkeypatch):
    """The acceptance shape: N cards minted from chat-caller for a foreign home
    put zero subscriptions on chat-caller."""
    tids = [_create(HOME_SID) for _ in range(3)]
    assert all("chat-caller" not in _chats(t) for t in tids)


def test_unknown_home_session_subscribes_nobody(kanban_home):
    tid = _create("20260101_000000_gone")
    assert _chats(tid) == []


def test_tool_create_with_foreign_session_id_subscribes_home(kanban_home, monkeypatch):
    """Same rule on the agent-tool path (kanban_create with an explicit session_id)."""
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card", "assignee": "peer", "session_id": HOME_SID}))
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, created["task_id"]).session_id == HOME_SID
    assert created["ok"], created
    assert _chats(created["task_id"]) == ["chat-home"]


def test_tool_create_without_session_id_subscribes_caller(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card 2", "assignee": "peer"}))
    assert created["ok"], created
    assert _chats(created["task_id"]) == ["chat-caller"]


def _add_session(sid: str, origin: dict) -> None:
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session(session_id=sid, source=origin["platform"], origin_json=json.dumps(origin))
    finally:
        db.close()


def _subs(task_id: str) -> list[dict]:
    conn = kb.connect()
    try:
        return [dict(s) for s in kbn.list_notify_subs(conn, task_id)]
    finally:
        conn.close()


def test_home_sub_carries_full_routing_identity(kanban_home):
    """Prism 99063f040047: alt participant id and delivery anchors come from the home."""
    _add_session("20261103_000000_feishu", {
        "platform": "feishu", "chat_id": "group-B", "chat_type": "group",
        "user_id": "open-1", "user_id_alt": "union-1", "parent_chat_id": "parent-B"})
    tid = _create("20261103_000000_feishu")
    (sub,) = _subs(tid)
    assert (sub["chat_id"], sub["user_id"], sub["user_id_alt"]) == ("group-B", "open-1", "union-1")
    meta = sub["delivery_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta["parent_chat_id"] == "parent-B"


def test_same_chat_other_participant_home_keeps_home_user(kanban_home):
    """Prism b594b6ccfed9: a home in the caller's chat but another user's session is
    subscribed with the HOME's participant, never the caller's."""
    _add_session("20261103_000000_u2", {
        "platform": "discord", "chat_id": "chat-caller", "chat_type": "group",
        "user_id": "u2", "scope_id": "g1"})
    tid = _create("20261103_000000_u2")
    (sub,) = _subs(tid)
    assert (sub["chat_id"], sub["user_id"]) == ("chat-caller", "u2")


def test_tool_idempotency_hit_subscribes_persisted_home(kanban_home, monkeypatch):
    """Prism 8efbb3c7ece2: a retry that names another session but hits an existing
    card subscribes that card's real home, never the argument's chat."""
    from tools import kanban_tools as kt

    _add_session("20261103_000000_other", {
        "platform": "discord", "chat_id": "chat-other", "chat_type": "group", "user_id": "u3"})
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    first = json.loads(kt._handle_create({"board": "default", "title": "idem", "assignee": "peer",
                                          "session_id": HOME_SID, "idempotency_key": "K1"}))
    again = json.loads(kt._handle_create({"board": "default", "title": "idem", "assignee": "peer",
                                          "session_id": "20261103_000000_other",
                                          "idempotency_key": "K1"}))
    assert again["task_id"] == first["task_id"]
    assert "chat-other" not in _chats(first["task_id"])


def _meta(sub: dict) -> dict:
    meta = sub["delivery_metadata"]
    return (json.loads(meta) if isinstance(meta, str) else meta) or {}


def test_tui_caller_discord_home_wake_true_registers_waker(kanban_home, monkeypatch):
    """Prism 227e930fa156 repro: the wake mode is resolved for the HOME's platform, not
    copied from the TUI caller (whose own target suppresses the wake)."""
    from tools import kanban_tools as kt

    for var in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID",
                "HERMES_SESSION_CHAT_TYPE", "HERMES_SESSION_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-key")
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    created = json.loads(kt._handle_create({
        "board": "default", "title": "foreign home callback", "assignee": "peer",
        "session_id": HOME_SID, "wake": True, "boomerang_wake": False}))
    assert created["ok"], created
    (sub,) = _subs(created["task_id"])
    assert (sub["platform"], sub["chat_id"]) == ("discord", "chat-home")
    assert sub["delivery_mode"] in kbn.NOTIFY_WAKE_MODES


def test_telegram_dm_topic_home_keeps_private_topic_flags(kanban_home):
    """Prism ef5ec124c1bf repro: the home's private-topic flags and reply anchor are
    derived from its stored origin (``message_id``), as the home itself would."""
    _add_session("20261103_000000_tg", {
        "platform": "telegram", "chat_id": "123", "chat_type": "dm",
        "thread_id": "20197", "message_id": "462"})
    tid = _create("20261103_000000_tg")
    (sub,) = _subs(tid)
    assert (sub["platform"], sub["chat_id"], sub["thread_id"]) == ("telegram", "123", "20197")
    meta = _meta(sub)
    assert meta["telegram_dm_topic_reply_fallback"] is True
    assert meta["direct_messages_topic_id"] == "20197"
    assert meta["telegram_reply_to_message_id"] == "462"


@pytest.mark.parametrize("wake", [None, True, False])
def test_redirect_target_equals_what_the_home_itself_subscribes(kanban_home, monkeypatch, wake):
    """The redirect and the home's own create go through ONE resolver: a create from the
    home session with the same wake flag yields the identical sub kwargs."""
    from gateway.session import SessionSource
    from tools import kanban_tools as kt

    origin = {"platform": "telegram", "chat_id": "123", "chat_type": "dm",
              "thread_id": "20197", "message_id": "462", "user_id": "u9",
              "user_id_alt": "alt9", "scope_id": "s9", "parent_chat_id": "p9", "profile": "apollo"}
    _add_session("20261103_000000_eq", origin)
    redirected = kt._home_target("t_x", "20261103_000000_eq", CALLER_SID,
                                 {"chat_id": "chat-caller", "delivery_mode": None}, wake=wake)

    src = SessionSource.from_dict(origin)
    for name, value in (("PLATFORM", src.platform.value), ("CHAT_ID", src.chat_id),
                        ("CHAT_TYPE", src.chat_type), ("THREAD_ID", src.thread_id),
                        ("USER_ID", src.user_id), ("USER_ID_ALT", src.user_id_alt),
                        ("SCOPE_ID", src.scope_id), ("PARENT_CHAT_ID", src.parent_chat_id),
                        ("MESSAGE_ID", src.message_id), ("PROFILE", src.profile)):
        monkeypatch.setenv(f"HERMES_SESSION_{name}", value)
    as_home = kt._resolve_notify_target(require_platform_identity=True, wake=wake)
    assert redirected == as_home


def test_create_from_another_profiles_turn_subscribes_the_owning_profile(kanban_home, monkeypatch):
    """t_e861b07c: an aegis Telegram turn ran ``hermes -p default kanban create --session <its own
    sid>``. The bridged env names the chat but no profile, the process profile read "default", and the
    default gateway claimed every wake of the card and could never deliver it. The sub must name the
    profile whose state.db holds the session."""
    import sqlite3

    aegis_sid = "20261120_035316_c0a4b23b"
    d = kanban_home / "profiles" / "aegis"
    d.mkdir(parents=True)
    c = sqlite3.connect(d / "state.db")
    c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    c.execute("INSERT INTO sessions (id) VALUES (?)", (aegis_sid,))
    c.commit()
    c.close()
    for name, value in (("ID", aegis_sid), ("PLATFORM", "telegram"), ("CHAT_ID", "571820863"),
                        ("CHAT_TYPE", "dm"), ("USER_ID", "571820863")):
        monkeypatch.setenv(f"HERMES_SESSION_{name}", value)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    tid = _create(aegis_sid)
    with kb.connect_closing() as conn:
        subs = kbn.list_notify_subs(conn, tid)
    assert [(s["chat_id"], s["notifier_profile"]) for s in subs] == [("571820863", "aegis")]
