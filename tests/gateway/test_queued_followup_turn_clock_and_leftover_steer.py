"""In-band /queue follow-up recursion: turn clock re-stamp + leftover /steer.

Incident 2026-09-29 (session 20260927_183134_b6f8858a): the queued follow-up
turn recursed through ``_run_agent`` on the parent's slot without re-stamping
``turn.started_ts``, so a steer ack on the 0-second-old follow-up read
"17 min elapsed".  Verifying the leftover-steer consumer also showed that a
``result["pending_steer"]`` is only delivered when NO queued follow-up exists;
with one, it was silently dropped.

Both are driven through the real ``GatewayRunner._run_agent`` recursion with a
stub agent.
"""

import importlib
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

SESSION_KEY = "agent:main:telegram:group:-1001:17585"
PARENT_AGE_S = 17 * 60


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
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )


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
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


async def _drive(monkeypatch, tmp_path, script):
    """Run ``_run_agent`` with a stub agent; ``script(call_index, queue_followup)``
    returns the result dict for each turn (and may queue follow-ups)."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    calls = []

    class StubAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self._interrupt_requested = False

        @property
        def is_interrupted(self):
            return self._interrupt_requested

        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            idx = len(calls)
            calls.append(
                {
                    "message": message,
                    "started_ts": runner._session_state(SESSION_KEY).turn.started_ts,
                    "busy_ack_ts": runner._session_state(SESSION_KEY).turn.busy_ack_ts,
                }
            )
            return script(idx, queue_followup)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = StubAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    adapter = _Adapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    # The parent turn claimed the slot 17 minutes ago and acked once.
    state = runner._session_state(SESSION_KEY)
    state.turn.started_ts = time.time() - PARENT_AGE_S
    state.turn.busy_ack_ts = time.time() - 5

    def queue_followup(text):
        adapter._pending_messages[SESSION_KEY] = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=_source()
        )

    result = await runner._run_agent(
        message="first",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-queue-clock",
        session_key=SESSION_KEY,
    )
    return runner, adapter, calls, result


@pytest.mark.asyncio
async def test_queued_followup_restamps_turn_clock(monkeypatch, tmp_path):
    def script(idx, queue_followup):
        if idx == 0:
            queue_followup("queued follow-up")
            return {"final_response": "first answer", "messages": [], "api_calls": 1}
        return {"final_response": "second answer", "messages": [], "api_calls": 1}

    _runner, _adapter, calls, _result = await _drive(monkeypatch, tmp_path, script)

    assert [c["message"] for c in calls][:2] == ["first", "queued follow-up"]
    now = time.time()
    # Parent turn: the 17-minute-old claim.
    assert now - calls[0]["started_ts"] >= PARENT_AGE_S - 5
    # Follow-up turn: a fresh clock, so a steer ack reads this turn's age.
    assert now - calls[1]["started_ts"] < 60
    # And the ack debounce does not carry over from the parent turn.
    assert calls[1]["busy_ack_ts"] == 0.0


@pytest.mark.asyncio
async def test_leftover_steer_not_dropped_when_followup_is_queued(monkeypatch, tmp_path):
    def script(idx, queue_followup):
        if idx == 0:
            queue_followup("queued follow-up")
            return {
                "final_response": "first answer",
                "messages": [],
                "api_calls": 1,
                "pending_steer": "late steer text",
            }
        return {"final_response": f"answer {idx}", "messages": [], "api_calls": 1}

    _runner, _adapter, calls, _result = await _drive(monkeypatch, tmp_path, script)

    messages = [c["message"] for c in calls]
    assert messages[:2] == ["first", "queued follow-up"]
    assert any("late steer text" in m for m in messages[2:]), messages
