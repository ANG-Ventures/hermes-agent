"""End-to-end /queue release through the REAL adapter door and REAL turns (spec I9/I14, Momus p3 B4').

A real ``BasePlatformAdapter`` subclass (real guards, slot, drains, session tasks) feeds a thin
message handler that does what ``GatewayRunner._handle_message`` does at its core for these cases:
/queue → the real ``_busy_queue_command`` / ``_hm_cmd_queue``; /stop → the real
``_busy_stop_command``; anything else → a fresh run generation + the real ``_run_agent`` with a stub
agent. The runner's ``_interrupt_and_clear_session`` is stubbed to its queue-relevant effects (bump
the generation, then ``hold_user_items`` — the exact order the real function now uses) plus releasing
the blocked turn. Each case must run a → b → c exactly once, one turn at a time, with NO further
inbound message needed to trigger dispatch.
"""

import asyncio
import importlib
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from gateway import queue_mgmt as qm
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
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
    return SessionSource(platform=Platform.TELEGRAM, chat_id="314", chat_type="dm", user_id="u1")


KEY = build_session_key(_source())


def _msg(text):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(), message_id=text)


class _Rig:
    def __init__(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
        self.calls, self.live, self.max_live = [], 0, 0
        self.lock = threading.Lock()
        self.block_first = threading.Event()
        self.first_started = threading.Event()
        rig = self

        class StubAgent:
            def __init__(self, **kwargs):
                self.tools = []
                self._interrupt_requested = False

            @property
            def is_interrupted(self):
                return self._interrupt_requested

            def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
                with rig.lock:
                    rig.live += 1
                    rig.max_live = max(rig.max_live, rig.live)
                    rig.calls.append(message)
                try:
                    if message == "first":
                        rig.first_started.set()
                        rig.block_first.wait(10)
                        return {"final_response": "", "messages": [], "api_calls": 1, "interrupted": True}
                    return {"final_response": f"answer {message}", "messages": [], "api_calls": 1}
                finally:
                    with rig.lock:
                        rig.live -= 1

        fake_run_agent = types.ModuleType("run_agent")
        fake_run_agent.AIAgent = StubAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
        gateway_run = importlib.import_module("gateway.run")
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

        self.adapter = adapter = _Adapter()
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
        runner._delivery_adapter_for = lambda source: adapter

        async def interrupt_and_clear(key, source, *, interrupt_reason, invalidation_reason,
                                      release_running_state=True):
            runner._invalidate_session_run_generation(key, reason=invalidation_reason)
            qm.hold_user_items(runner, key, adapter)            # same order as the real function
            rig.block_first.set()

        runner._interrupt_and_clear_session = interrupt_and_clear
        self.runner = runner
        self.turn_running = 0

        async def handler(event):
            text = event.text or ""
            if text.startswith("/queue"):
                if self.turn_running:
                    return await runner._busy_queue_command(event, KEY, _source())
                handled, reply = await runner._hm_cmd_queue(event, _source(), KEY)
                if handled:
                    return reply
                text = event.text
            if text.startswith("/stop"):
                reply = await runner._busy_stop_command(event, KEY, _source())
                return getattr(reply, "text", None) or str(reply)
            generation = runner._begin_session_run_generation(KEY)
            self.turn_running += 1
            try:
                await runner._run_agent(message=text, context_prompt="", history=[], source=_source(),
                                        session_id="sess-i14", session_key=KEY, run_generation=generation)
            finally:
                self.turn_running -= 1
            return None

        adapter.set_message_handler(handler)

    async def start_first_turn(self):
        self.adapter._start_session_processing(_msg("first"), KEY)
        for _ in range(200):
            if self.first_started.is_set():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("first turn never started")

    async def settle(self, expect):
        for _ in range(500):
            if len(self.calls) >= expect and not any(
                    not t.done() for t in list(self.adapter._session_tasks.values())):
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_busy_queue_run_releases_each_item_once_in_order(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    await rig.start_first_turn()
    for text in ("/queue a", "/queue b", "/queue c"):
        await rig.adapter.handle_message(_msg(text))
    await rig.adapter.handle_message(_msg("/queue run"))
    await rig.settle(expect=4)
    assert rig.calls == ["first", "a", "b", "c"]
    assert rig.max_live == 1
    assert any("releasing 3 queued" in s for s in rig.adapter.sent), rig.adapter.sent


@pytest.mark.asyncio
async def test_idle_queue_run_releases_held_items_without_another_message(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    assert qm.rewrite_active(rig.runner, KEY, rig.adapter, [_msg("a"), _msg("b"), _msg("c")])
    qm.hold_user_items(rig.runner, KEY, rig.adapter)                        # state after a /stop
    await rig.adapter.handle_message(_msg("/queue run"))
    await rig.settle(expect=3)
    assert rig.calls == ["a", "b", "c"]
    assert rig.max_live == 1
    assert rig.runner._session_state(KEY).conversation.held_queue == []


@pytest.mark.asyncio
async def test_knob_on_stop_releases_each_item_once_in_order(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    rig.runner._stop_releases_queue = True
    await rig.start_first_turn()
    for text in ("/queue a", "/queue b", "/queue c"):
        await rig.adapter.handle_message(_msg(text))
    await rig.adapter.handle_message(_msg("/stop"))
    await rig.settle(expect=4)
    assert rig.calls == ["first", "a", "b", "c"]
    assert rig.max_live == 1


@pytest.mark.asyncio
async def test_default_stop_holds_and_nothing_runs(monkeypatch, tmp_path):
    """D12 end to end: after /stop's tail and the stopped turn's unwind, nothing has run."""
    rig = _Rig(monkeypatch, tmp_path)
    await rig.start_first_turn()
    for text in ("/queue a", "/queue b"):
        await rig.adapter.handle_message(_msg(text))
    await rig.adapter.handle_message(_msg("/stop"))
    await rig.settle(expect=1)
    await asyncio.sleep(0.3)
    assert rig.calls == ["first"]
    assert [e.text for e in rig.runner._session_state(KEY).conversation.held_queue] == ["a", "b"]
    assert any("2 queued prompt(s) held" in s for s in rig.adapter.sent), rig.adapter.sent
