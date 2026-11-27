"""Boomerang wake is the default for a card homed to a session with a routable chat.

Ace 2026-11-06 (#inbox): "yes make it the default and automatic". Machine-minted cards
(pager-owner-card, ci-red-owner, landing-queue ...) are created by scripts/cron that pass
``--session <home sid>`` but carry no gateway identity in env, so before this change they
got ``boomerang_wake`` NULL and no subscription row: the home was never woken at all.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn

import agent.delegation_context as _dc
from tools import kanban_tools as _kt

_REAL_DC_PREDICATE = _dc.is_dispatcher_owned_worker_context
_REAL_KT_PREDICATE = _kt._is_dispatcher_owned_worker

HOME_SID = "20261106_091900_home"
CLI_SID = "20261106_091901_cli"  # a session with no routable chat (CLI origin)

_SESSION_ENV = ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_TYPE",
                "HERMES_SESSION_USER_ID", "HERMES_SESSION_KEY", "HERMES_SESSION_ID",
                "HERMES_SESSION_THREAD_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB")


@pytest.fixture
def script_env(tmp_path, monkeypatch):
    """A script/cron caller: no gateway identity, default config, operator (not a worker)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for k in _SESSION_ENV:
        monkeypatch.delenv(k, raising=False)
    from gateway.session_context import reset_session_vars
    reset_session_vars()
    import agent.delegation_context as dc
    from tools import kanban_tools as kt
    monkeypatch.setattr(dc, "is_dispatcher_owned_worker_context", lambda: False)
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    kb.init_db()
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        db.create_session(session_id=HOME_SID, source="discord", origin_json=json.dumps({
            "platform": "discord", "chat_id": "chat-home", "chat_type": "group",
            "user_id": "u1", "scope_id": "g1"}))
        db.create_session(session_id=CLI_SID, source="cli", origin_json=json.dumps({
            "platform": "cli", "chat_id": "local"}))
    finally:
        db.close()
    return home


def _cli_create(extra: str) -> str:
    out = kc.run_slash(f'--board default create "card" --assignee worker1 {extra}')
    m = re.search(r"Created\s+(t_[0-9a-f]+)\b", out)
    assert m, out
    return m.group(1)


def _state(tid: str):
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        subs = [(s["chat_id"], s["delivery_mode"], s["user_id"]) for s in kbn.list_notify_subs(conn, tid)]
    return task.boomerang_wake, subs


# (a) script create with --session of a routable home -> level + notify+wake row on the HOME
def test_cli_script_create_routable_home_defaults_boomerang(script_env):
    level, subs = _state(_cli_create(f"--session {HOME_SID}"))
    assert level is True
    assert subs == [("chat-home", "notify+wake", "u1")]


def test_tool_create_routable_home_defaults_boomerang(script_env):
    from tools import kanban_tools as kt
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card", "assignee": "peer", "session_id": HOME_SID}))
    assert created["ok"], created
    assert created["subscribed"] is True
    assert _state(created["task_id"]) == (True, [("chat-home", "notify+wake", "u1")])


# (b) explicit opt-out: no level
def test_cli_script_create_opt_out(script_env):
    level, subs = _state(_cli_create(f"--session {HOME_SID} --no-boomerang-wake"))
    assert not level
    assert all(mode == "notify" for _, mode, _ in subs)


def test_tool_create_opt_out(script_env):
    from tools import kanban_tools as kt
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card", "assignee": "peer", "session_id": HOME_SID,
         "boomerang_wake": False}))
    assert created["ok"], created
    level, subs = _state(created["task_id"])
    assert not level
    assert all(mode == "notify" for _, mode, _ in subs)


# (c) a home with no routable chat (CLI origin / unknown / operator): the card still gets the
# level (t_c24b1cc7: every producer), but no recipient is invented.
@pytest.mark.parametrize("sid", [CLI_SID, "20260101_000000_gone", "operator:apollo"])
def test_cli_unroutable_home_level_without_waker(script_env, sid):
    assert _state(_cli_create(f"--session {sid}")) == (True, [])


def test_cli_no_session_flag_level_without_waker(script_env):
    assert _state(_cli_create("")) == (True, [])


def test_tool_unroutable_home_level_without_waker(script_env):
    from tools import kanban_tools as kt
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card", "assignee": "peer", "session_id": CLI_SID}))
    assert created["ok"], created
    assert _state(created["task_id"]) == (True, [])


# (d) a dispatcher-owned worker caller: the card gets the level; the worker's create does not
# subscribe the home (worker cards report through the parent lineage).
def test_cli_worker_caller_level_no_home_row(script_env, monkeypatch):
    import agent.delegation_context as dc
    monkeypatch.setattr(dc, "is_dispatcher_owned_worker_context", lambda: True)
    assert _state(_cli_create(f"--session {HOME_SID}")) == (True, [])


def test_tool_worker_caller_level_no_home_row(script_env, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card", "assignee": "peer", "session_id": HOME_SID}))
    assert created["ok"], created
    assert _state(created["task_id"]) == (True, [])


@pytest.fixture
def real_predicate(script_env, monkeypatch):
    """Undo ``script_env``'s worker pin: the caller is classed by the REAL predicate
    over an env with no Kanban grant at all (t_39f5f042: it failed open)."""
    import agent.delegation_context as dc
    from tools import kanban_tools as kt
    for k in (*dc.KANBAN_ENV_KEYS, dc.DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(dc, "is_dispatcher_owned_worker_context", _REAL_DC_PREDICATE)
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", _REAL_KT_PREDICATE)
    return script_env


def test_cli_script_create_real_predicate_defaults_boomerang(real_predicate):
    assert _state(_cli_create(f"--session {HOME_SID}")) == (True, [("chat-home", "notify+wake", "u1")])


def test_cli_script_create_real_predicate_opt_out(real_predicate):
    level, subs = _state(_cli_create(f"--session {HOME_SID} --no-boomerang-wake"))
    assert not level
    assert all(mode == "notify" for _, mode, _ in subs)


def test_config_knob_off_disables_the_default(script_env):
    (script_env / "config.yaml").write_text("kanban:\n  auto_subscribe_boomerang: false\n")
    level, subs = _state(_cli_create(f"--session {HOME_SID}"))
    assert level is False and subs == []
