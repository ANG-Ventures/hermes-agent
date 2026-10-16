"""A turn invalidated by /stop must not consume the /queue (spec 2026-10-12 D15/I12).

Driven through the REAL ``GatewayRunner._run_agent`` (stub agent), so the stopped turn actually
unwinds through its post-turn drain — the path Momus pass 1 (B2) showed popping the queue head and
recursing under the dead generation. ``test_stale_turn_drain_consumes_nothing`` uses only APIs that
exist on unmodified fork/main, so it is the RED-on-base measurement the #814 revert said was missing.
"""

import importlib
import logging
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

SESSION_KEY = "agent:main:telegram:dm:42"


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id="m1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm")


def _event(text):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source())


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False,
                                    stt_enabled=False)
    return runner


async def _drive(monkeypatch, tmp_path, on_first_turn):
    """Run one tracked turn; ``on_first_turn(runner, adapter)`` plays /stop mid-turn."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    calls = []
    adapter = _Adapter()
    runner = _make_runner(adapter)

    class StubAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self._interrupt_requested = False

        @property
        def is_interrupted(self):
            return self._interrupt_requested

        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            calls.append(message)
            if len(calls) == 1:
                on_first_turn(runner, adapter)
                return {"final_response": "", "messages": [], "api_calls": 1, "interrupted": True}
            return {"final_response": f"answer to {message}", "messages": [], "api_calls": 1}

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = StubAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    generation = runner._begin_session_run_generation(SESSION_KEY)
    await runner._run_agent(message="first", context_prompt="", history=[], source=_source(),
                            session_id="sess-stale-drain", session_key=SESSION_KEY,
                            run_generation=generation)
    return runner, adapter, calls


@pytest.mark.asyncio
async def test_stale_turn_drain_consumes_nothing(monkeypatch, tmp_path, caplog):
    """Generation bumped mid-turn with `a` in the slot: the dead turn must neither run nor pop it."""
    def stop_mid_turn(runner, adapter):
        adapter._pending_messages[SESSION_KEY] = _event("a")
        runner._invalidate_session_run_generation(SESSION_KEY, reason="stop_command")

    with caplog.at_level(logging.INFO):
        runner, adapter, calls = await _drive(monkeypatch, tmp_path, stop_mid_turn)

    assert calls == ["first"], f"stale turn ran a queued item as a zombie: {calls}"
    slot = adapter._pending_messages.get(SESSION_KEY)
    assert slot is not None and slot.text == "a", "stale turn consumed the queue head"
    assert "PHASE=stale_drain_skipped" in caplog.text


@pytest.mark.asyncio
async def test_stop_mid_turn_holds_everything_and_runs_nothing(monkeypatch, tmp_path):
    """The /stop hold (D14) + the drain gate (D15) together: a, b, c held in order, nothing runs."""
    from gateway import queue_mgmt

    def stop_mid_turn(runner, adapter):
        adapter._pending_messages[SESSION_KEY] = _event("a")
        runner._session_state(SESSION_KEY).conversation.queued_events.extend([_event("b"), _event("c")])
        runner._invalidate_session_run_generation(SESSION_KEY, reason="stop_command")
        queue_mgmt.hold_user_items(runner, SESSION_KEY, adapter)

    runner, adapter, calls = await _drive(monkeypatch, tmp_path, stop_mid_turn)

    assert calls == ["first"]
    held = runner._session_state(SESSION_KEY).conversation.held_queue
    assert [e.text for e in held] == ["a", "b", "c"]
    assert SESSION_KEY not in adapter._pending_messages
    assert list(runner._session_state(SESSION_KEY).conversation.queued_events) == []


@pytest.mark.asyncio
async def test_current_turn_still_drains_its_queue(monkeypatch, tmp_path):
    """Negative control: with NO generation bump the queued follow-up runs exactly as before."""
    def queue_only(runner, adapter):
        adapter._pending_messages[SESSION_KEY] = _event("a")

    _runner, _adapter, calls = await _drive(monkeypatch, tmp_path, queue_only)
    assert calls == ["first", "a"]
