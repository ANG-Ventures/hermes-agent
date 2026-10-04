"""A saved turn handoff must not cross a conversation boundary.

Handoffs are keyed by the stable gateway chat key, which outlives a
conversation: ``/new``/``/reset``, idle/daily auto-reset and ``/resume`` all
change the session id under the same key. Before this guard, the first turn of
the new conversation consumed and injected the discarded conversation's
request, tool results and an instruction to re-issue unfinished tools.
"""

import types
from datetime import datetime, timedelta

import pytest

import agent.turn_handoff as turn_handoff
from agent.turn_handoff import (
    capture_turn_handoff,
    consume_handoff_context,
    handoff_path_for,
)
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


class _Store:
    def read(self):
        return [{"id": "t1", "content": "finish the old audit", "status": "pending"}]


def _agent(session_key, session_id):
    a = types.SimpleNamespace()
    a._gateway_session_key = session_key
    a.session_id = session_id
    a.provider = "p"
    a.model = "m"
    a._todo_store = _Store()
    return a


def _messages():
    return [{"role": "user", "content": "audit the OLD conversation's cron jobs"}]


@pytest.fixture
def handoff_root(tmp_path, monkeypatch):
    root = tmp_path / "handoffs"
    monkeypatch.setattr(turn_handoff, "_default_root", lambda: root)
    return root


def _store(tmp_path):
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    store._db = None
    return store


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")


def _capture(entry):
    notice = capture_turn_handoff(
        _agent(entry.session_key, entry.session_id),
        _messages(),
        turn_start_idx=0,
        reason="rate limited",
    )
    assert notice, "precondition: a handoff was written"
    assert handoff_path_for(entry.session_key).exists()


def test_manual_reset_discards_the_old_conversations_handoff(tmp_path, handoff_root):
    store = _store(tmp_path)
    old = store.get_or_create_session(_source())
    _capture(old)

    new = store.reset_session(old.session_key)

    assert new is not None and new.session_id != old.session_id
    assert not handoff_path_for(old.session_key).exists()
    assert consume_handoff_context(_agent(new.session_key, new.session_id)) == ""


def test_resume_switch_discards_the_old_conversations_handoff(tmp_path, handoff_root):
    store = _store(tmp_path)
    old = store.get_or_create_session(_source())
    _capture(old)

    switched = store.switch_session(old.session_key, "20260101_000000_deadbeef")

    assert switched is not None and switched.session_id == "20260101_000000_deadbeef"
    assert consume_handoff_context(_agent(old.session_key, switched.session_id)) == ""


def test_auto_reset_discards_the_old_conversations_handoff(tmp_path, handoff_root):
    # Upstream (parity 2026-10-01) retired timed resets (SessionResetPolicy -> plugin); at the
    # store, only an explicit suspension replaces a routed conversation (session_lifecycle
    # _route_reset_reason). That rotation is the one that must discard the old handoff.
    store = _store(tmp_path)
    old = store.get_or_create_session(_source())
    _capture(old)
    store._entries[old.session_key].suspended = True

    new = store.get_or_create_session(_source())

    assert new.session_id != old.session_id
    assert new.was_auto_reset
    assert consume_handoff_context(_agent(new.session_key, new.session_id)) == ""


def test_same_conversation_keeps_its_handoff(tmp_path, handoff_root):
    """The feature: the NEXT turn of the SAME conversation still resumes."""
    store = _store(tmp_path)
    entry = store.get_or_create_session(_source())
    _capture(entry)

    again = store.get_or_create_session(_source())

    assert again.session_id == entry.session_id
    ctx = consume_handoff_context(_agent(again.session_key, again.session_id))
    assert "audit the OLD conversation's cron jobs" in ctx
    assert "finish the old audit" in ctx
