from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.blackbox import commands


class FakeStore:
    def __init__(self):
        self.last_turn = None
        self.turn = None
        self.tool_calls = []
        self.rollup = {}
        self.top = []

    def get_last_turn(self, platform, chat_id):
        self.last_args = (platform, chat_id)
        return self.last_turn

    def get_turn(self, turn_id):
        self.turn_id = turn_id
        return self.turn

    def get_tool_calls(self, turn_id):
        self.tool_turn_id = turn_id
        return self.tool_calls

    def session_rollup(self, platform, chat_id, limit=50):
        self.rollup_args = (platform, chat_id, limit)
        return self.rollup

    def top_turns(self, n, since_days):
        self.top_args = (n, since_days)
        return self.top[:n]


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(commands, "store", store)
    monkeypatch.setattr(
        commands,
        "card",
        SimpleNamespace(render=lambda record: f"CARD:{record['turn_id']}"),
    )
    monkeypatch.setattr(commands, "_current_channel", lambda: ("telegram", "chat-1"))
    return store


def test_cost_no_arg_with_last_turn_returns_card(fake_store):
    fake_store.last_turn = {"turn_id": "turn_1", "platform": "telegram", "chat_id": "chat-1"}

    assert commands.handle_cost("") == "CARD:turn_1"


def test_cost_no_arg_with_no_channel_context_is_graceful(fake_store, monkeypatch):
    monkeypatch.setattr(commands, "_current_channel", lambda: ("", ""))

    assert "No turns recorded" in commands.handle_cost("")


def test_cost_turn_matching_channel_includes_details(fake_store):
    fake_store.turn = {
        "turn_id": "turn_2",
        "platform": "telegram",
        "chat_id": "chat-1",
        "user_text": "how much did that cost?",
        "final_text": "about a cent",
    }
    fake_store.tool_calls = [
        {
            "name": "exec",
            "args_preview": "pytest -q",
            "result_preview": "1 passed",
        }
    ]

    result = commands.handle_cost("turn_2")

    assert "CARD:turn_2" in result
    assert "how much did that cost?" in result
    assert "about a cent" in result
    assert "exec" in result
    assert "pytest -q" in result
    assert "1 passed" in result


def test_cost_turn_wrong_channel_enforces_acl(fake_store):
    fake_store.turn = {
        "turn_id": "turn_3",
        "platform": "telegram",
        "chat_id": "other-chat",
    }

    assert commands.handle_cost("turn_3") == "Turn not found in this channel."


def test_cost_session_rollup(fake_store):
    fake_store.rollup = {
        "total_usd": 1.25,
        "count": 4,
        "avg_usd": 0.3125,
        "max_turn": {"turn_id": "turn_expensive", "cost_usd": 0.75},
    }

    result = commands.handle_cost("session")

    assert result == (
        "Session spend: $1.25 over 4 turns (avg $0.3125). "
        "Priciest: turn_expensive $0.75"
    )


def test_cost_top_three(fake_store):
    fake_store.top = [
        {"turn_id": "turn_a", "cost_usd": 3, "model": "m1", "datetime": "2026-06-01"},
        {"turn_id": "turn_b", "cost_usd": 2, "model": "m2", "datetime": "2026-06-02"},
        {"turn_id": "turn_c", "cost_usd": 1, "model": "m3", "datetime": "2026-06-03"},
        {"turn_id": "turn_d", "cost_usd": 0.5, "model": "m4", "datetime": "2026-06-04"},
    ]

    lines = commands.handle_cost("top 3").splitlines()

    assert len(lines) == 3
    assert lines[0] == "1. turn_a — $3 — m1 — 2026-06-01"
    assert lines[2] == "3. turn_c — $1 — m3 — 2026-06-03"
    assert fake_store.top_args == (3, 30)


def test_handler_never_raises_when_store_errors(fake_store):
    def boom(_turn_id):
        raise RuntimeError("database unavailable")

    fake_store.get_turn = boom

    assert commands.handle_cost("turn_boom").startswith("⚠️ /cost error: database unavailable")
