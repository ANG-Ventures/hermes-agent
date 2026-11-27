"""A turn that ends in a provider error still leaves a ``turns`` row (t_2f2a0eca).

End to end in a scratch profile: the REAL conversation loop, the REAL blackbox
hooks and store, only the provider call is replaced by an injected error. The
invariant is the one ``blackbox-orphan-guard`` pages on: every turn_id in
``turn_api_calls`` has a parent ``turns`` row.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from plugins import blackbox
from plugins.blackbox import orphans, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {
        "enabled": True, "alerts_enabled": False, "record_subagents": True,
        "retention_days": 3650, "store_text": True,
    })
    store._connect().close()
    return tmp_path


def _route(name, **kwargs):
    if name == "on_session_end":
        blackbox._on_session_end(**kwargs)
    elif name == "on_turn_abandoned":
        blackbox._on_turn_abandoned(**kwargs)
    return []


def _agent():
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    return agent


def _status_error(cls, status):
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    body = {"error": {"message": f"injected {status}"}}
    return cls(f"injected {status}", response=httpx.Response(status, request=req, json=body), body=body)


def _rows(db):
    conn = sqlite3.connect(str(db))
    try:
        calls = conn.execute("SELECT turn_id, http_status FROM turn_api_calls").fetchall()
        turns = conn.execute("SELECT turn_id, terminal_error FROM turns").fetchall()
        orph = orphans.orphan_turns(conn, since=0.0, settle_before=1e12)
    finally:
        conn.close()
    return calls, turns, orph


@pytest.mark.parametrize(
    "err",
    [
        _status_error(openai.BadRequestError, 400),
        _status_error(openai.InternalServerError, 503),
        _status_error(openai.RateLimitError, 429),
    ],
    ids=["400", "503", "429"],
)
def test_provider_error_turn_leaves_a_turns_row(home, err):
    agent = _agent()
    attempts = []

    def _fail(*_a, **_k):
        attempts.append(1)
        assert len(attempts) < 20, "retry loop did not terminate"
        raise err

    # Inject at the transport: the real interruptible_api_call wrapper still
    # ledgers the failed attempt (_record_failed_api_call), as in production.
    with (
        patch("hermes_cli.lifecycle.invoke_hook", _route),
        patch("time.sleep"),
        # The backoff loop spins on time.time(); skip the wait itself.
        patch("agent.turn_api_error.interruptible_backoff_sleep", return_value=None),
        patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None),
        patch("agent.chat_completion_helpers.should_use_direct_api_call", return_value=True),
        patch("agent.chat_completion_helpers.direct_api_call", side_effect=_fail),
    ):
        agent._disable_streaming = True
        result = agent.run_conversation("hello")

    assert result.get("completed") is not True
    calls, turns, orph = _rows(store._db_path())
    turn_id = agent._current_turn_id
    assert turn_id in {t for t, _ in calls}, "the failed call was ledgered"
    assert orph == [], f"orphaned turn(s): {orph}"
    assert [t for t, _ in turns] == [turn_id]
