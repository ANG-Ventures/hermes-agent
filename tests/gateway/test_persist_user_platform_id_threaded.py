"""t_3956b96e regression: gateway turns must persist the inbound platform id.

Measured on the live fleet (2026-10-03): 0 of ~4,900 Discord user rows since
2026-09-27 carried ``platform_message_id`` (last stamped Discord row
2026-09-26). The gateway stopped passing ``persist_user_platform_id`` to
``run_conversation`` after the 2026-08-09 parity merge, so the transcript
authority every dedupe gate queries (Discord restart backfill, Telegram
re-delivery suppression, the #47237 duplicate-turn guard) always answered
"absent". On the 10-03 11:09 boot the Discord restart backfill re-injected two
messages that had been answered before the restart, and one of them was
answered twice.

1. A turn driven through the real ``GatewayRunner._run_agent`` wrapper and the
   admitted runner lands in a temp SessionDB with its platform id, so
   ``has_platform_message_id`` answers True.
2. Both ``_run_agent`` call sites (fresh inbound turn, drained queued
   follow-up) pass the triggering event's ``message_id``.
"""

from __future__ import annotations

import ast
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.turn_context import build_turn_context
from gateway.config import Platform
from gateway.session import SessionSource
from hermes_state import SessionDB
from run_agent import AIAgent as RealAIAgent

from tests.gateway.test_internal_row_display_kind_persisted import (
    SESSION_KEY,
    _Adapter,
    _make_runner,
)

SID = "sess-t3956-platform-id"
MSG_ID = "1556004327716298903"


@pytest.fixture()
def real_agent_db(tmp_path):
    db = SessionDB(Path(tmp_path) / "state.db")
    db.create_session(session_id=SID, source="telegram", model="test-model")
    agent = RealAIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_db=db,
        session_id=SID,
    )
    agent._session_db_created = True
    agent._cached_system_prompt = "SYSTEM"
    agent._skip_mcp_refresh = True
    try:
        yield agent, db
    finally:
        db.close()


def _persisting_agent_cls(real_agent):
    class PersistingAgent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **kwargs):
            with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
                build_turn_context(
                    agent=real_agent,
                    user_message=message,
                    system_message=None,
                    conversation_history=None,
                    task_id=None,
                    stream_callback=None,
                    persist_user_message=kwargs.get("persist_user_message"),
                    persist_user_platform_id=kwargs.get("persist_user_platform_id"),
                    restore_or_build_system_prompt=lambda *a, **k: None,
                    install_safe_stdio=lambda: None,
                    sanitize_surrogates=lambda s: s,
                    summarize_user_message_for_log=lambda s: s,
                    set_session_context=lambda _sid: None,
                    set_current_write_origin=lambda _o: None,
                    ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
                )
            return {"final_response": "ack", "messages": [], "api_calls": 1}

    return PersistingAgent


@pytest.mark.asyncio
async def test_gateway_turn_row_carries_platform_message_id(
    monkeypatch, tmp_path, real_agent_db
):
    agent, db = real_agent_db
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _persisting_agent_cls(agent)
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = _make_runner(_Adapter())
    result = await runner._run_agent(
        message="What are the parked cards?",
        context_prompt="",
        history=[],
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="-1001"),
        session_id=SID,
        session_key=SESSION_KEY,
        event_message_id=MSG_ID,
        persist_user_platform_id=MSG_ID,
    )
    assert result["final_response"] == "ack"

    assert db.has_platform_message_id(SID, MSG_ID) is True
    assert db.has_platform_message_id(SID, "999") is False


def test_every_run_agent_call_site_passes_the_event_platform_id():
    gateway_run = importlib.import_module("gateway.run")
    tree = ast.parse(Path(gateway_run.__file__).read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_run_agent"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert len(calls) >= 2, "expected the fresh-turn and queued-follow-up call sites"
    for node in calls:
        kw = {k.arg: k.value for k in node.keywords}
        assert "persist_user_platform_id" in kw, (
            f"_run_agent call at gateway/run.py:{node.lineno} drops "
            "persist_user_platform_id; restart backfill dedupe goes blind"
        )
        assert "message_id" in ast.unparse(kw["persist_user_platform_id"])
