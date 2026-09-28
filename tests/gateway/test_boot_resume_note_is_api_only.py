"""C6 (FleetReview backfill, #1123 run.py:7661): the boot auto-resume turn
(empty user text) must persist an EMPTY user row, never the generated
``[System note: ...]``. A persisted note is a non-empty "human" row, so a second
interruption's ``_turn_segment`` starts at it and ``describe_inflight_tool_calls``
can no longer see the ORIGINAL turn's unfinished calls; it also replays as
user-authored guidance. Driven through the real ``_run_agent``; the agent is
faked and records the kwargs the gateway hands it.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from tests.gateway.test_boot_resume_attempt_cap import (
    _INTERRUPTED_TAIL,
    _dispatched_boot,
    _runner,
    _seed,
    _source,
)


def _recording_agent_module(seen: list):
    class _Agent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **kwargs):
            seen.append((message, kwargs.get("persist_user_message")))
            return {"final_response": "ok", "messages": [], "api_calls": 1}

    module = types.ModuleType("run_agent")
    module.AIAgent = _Agent
    return module


async def _drive(tmp_path, monkeypatch, message):
    monkeypatch.setenv("HERMES_RESUME_INTERRUPTED_TURNS", "auto")
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "1")
    runner, adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    _seed(db, entry, _INTERRUPTED_TAIL)
    assert runner.session_store.mark_resume_pending(entry.session_key, "restart_interrupted")
    seen: list = []
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setitem(sys.modules, "run_agent", _recording_agent_module(seen))
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_run_generation = {}
    runner.hooks = types.SimpleNamespace(loaded_hooks=False)
    await runner._run_agent(
        message=message, context_prompt="", history=[],
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="u1", chat_type="dm", user_id="u1"),
        session_id=entry.session_id, session_key=entry.session_key,
    )
    db.close()
    return seen


@pytest.mark.asyncio
async def test_boot_resume_turn_persists_an_empty_user_row(tmp_path, monkeypatch):
    seen = await _drive(tmp_path, monkeypatch, "")
    assert seen, "the agent never ran"
    api_message, persisted = seen[0]
    assert "[System note:" in api_message  # the model still gets the guidance
    assert persisted == ""  # ...but it is never stored as a user row


@pytest.mark.asyncio
async def test_human_message_during_resume_persists_only_the_human_text(tmp_path, monkeypatch):
    seen = await _drive(tmp_path, monkeypatch, "are you still on it?")
    api_message, persisted = seen[0]
    assert "[System note:" in api_message and "are you still on it?" in api_message
    assert persisted == "are you still on it?"
