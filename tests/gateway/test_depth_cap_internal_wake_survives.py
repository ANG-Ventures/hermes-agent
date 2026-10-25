"""An internal wake admitted behind a busy chain that hits the interrupt-depth cap still runs (t_97a879af).

2026-10-24 15:25:44 (session 20261007_215000_ac99b02a, discord): deleg_3b87f5fe's ASYNC DELEGATION
BATCH COMPLETE was admitted behind a turn already three queued follow-ups deep; the outbox row was
acked ``delivered`` on admission; at 15:26:30 the chain hit ``_MAX_INTERRUPT_DEPTH`` and the wake
never ran as a turn. Live evidence: the two earlier completions on that session (deleg_a22f7aa7,
deleg_e1744e45) reached the transcript as OUT-OF-BAND steer rows, i.e. completions on that busy
session are steered into the running agent. A steer the agent never applies (no tool batch after it)
comes back as ``result["pending_steer"]``; at the depth cap the drain handed it to
``adapter.queue_message``, which no adapter defines, so it vanished.
"""

import asyncio
import importlib
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource
from gateway.wake import admit_internal_event


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(str(content))
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
    return SessionSource(platform=Platform.DISCORD, chat_id="1535", chat_type="group", user_id="u1")


WAKE = "[ASYNC DELEGATION BATCH COMPLETE — deleg_test] verdict ready"


def _msg(text):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(), message_id=text)


def _wake(text=WAKE):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(), internal=True,
                        metadata={"notification_origin": "process_registry_synthetic"})


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["queued", "steered"])
async def test_internal_wake_admitted_at_depth_cap_runs_as_a_later_turn(monkeypatch, tmp_path, delivery):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    calls = []
    loop = asyncio.get_running_loop()
    holder = {}

    def _admit_from_thread(coro_fn):
        asyncio.run_coroutine_threadsafe(coro_fn(), loop).result(10)

    class StubAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self._interrupt_requested = False
            self._steered = None

        def steer(self, text):
            self._steered = text
            return True

        @property
        def is_interrupted(self):
            return self._interrupt_requested

        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            calls.append(message)
            adapter = holder["adapter"]
            # Each turn of the chain gets the next queued wake (10-24: kanban/deadman wakes); the
            # depth-3 turn gets the delegation completion, exactly as on 10-24.
            nxt = {"t0": "h1", "h1": "h2", "h2": "h3"}.get(message)
            if nxt is not None:
                _admit_from_thread(lambda: admit_internal_event(adapter, _wake(nxt)))
            elif message == "h3" and delivery == "queued":
                _admit_from_thread(lambda: admit_internal_event(adapter, _wake()))
            elif message == "h3":
                # The real busy fast path steers the completion into this running agent. Let the
                # tracker promote THIS agent into the slot first (a real turn runs for minutes).
                time.sleep(0.3)
                _admit_from_thread(lambda: holder["runner"]._hm_handle_running_session_message(
                    _wake(), _source(), holder["key"]))
            result = {"final_response": f"answer {message}", "messages": [], "api_calls": 1}
            if self._steered:
                # Final answer already produced: the finalizer hands the unapplied steer back.
                result["pending_steer"], self._steered = self._steered, None
            return result

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = StubAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    adapter = holder["adapter"] = _Adapter()
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    for name, value in (("_voice_mode", {}), ("_prefill_messages", []), ("_ephemeral_system_prompt", ""),
                        ("_reasoning_config", None), ("_provider_routing", {}), ("_fallback_model", None),
                        ("_session_db", None), ("_running_agents", {}), ("_session_run_generation", {}),
                        ("_draining", False)):
        setattr(runner, name, value)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=True,
                                    stt_enabled=False)
    runner._delivery_adapter_for = lambda source: adapter
    key = holder["key"] = adapter._event_session_key(_msg("probe"))
    holder["runner"] = runner
    runner._effective_busy_input_mode = lambda source: "steer"
    monkeypatch.setattr(type(runner), "_MAX_INTERRUPT_DEPTH", 3)

    kinds = []

    async def handler(event):
        from gateway.response_filters import display_kind_for_event
        kinds.append(display_kind_for_event(event))
        generation = runner._begin_session_run_generation(key)
        await runner._run_agent(message=event.text or "", context_prompt="", history=[], source=_source(),
                                session_id="sess-depth", session_key=key, run_generation=generation)
        return None

    adapter.set_message_handler(handler)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    await adapter.handle_message(_msg("t0"))
    for _ in range(500):
        if any(WAKE in c for c in calls) and not any(not t.done() for t in list(adapter._session_tasks.values())):
            break
        await asyncio.sleep(0.01)
    assert calls[:4] == ["t0", "h1", "h2", "h3"], calls
    assert len(calls) == 5 and WAKE in calls[4], calls
    # The 5th turn runs as the internal wake it was, never as a human turn (t_506536db): before, the
    # re-seated pending_steer was a bare internal=False event (visible fallback, auth path).
    assert kinds[-1] == "internal_notification", kinds
