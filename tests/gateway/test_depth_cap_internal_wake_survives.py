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


HUMAN_STEER = "human steer mid-turn"


async def _drive_chain(monkeypatch, tmp_path, delivery, *, steer_on="h3"):
    """Run t0 (-> h1 -> h2 -> h3 when ``steer_on`` is the depth-cap turn); on ``steer_on`` deliver the
    completion (``queued``/``steered``), or a human steer then the completion (``mixed``). Returns
    ``(calls, kinds)``: each turn's message and ``display_kind_for_event`` of the event it ran from."""
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
            self._steered = f"{self._steered}\n{text}" if self._steered else text
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
            if message == steer_on and delivery == "queued":
                _admit_from_thread(lambda: admit_internal_event(adapter, _wake()))
            elif message == steer_on:
                # The real busy fast path steers the completion into this running agent. Let the
                # tracker promote THIS agent into the slot first (a real turn runs for minutes).
                time.sleep(0.3)
                if delivery == "mixed":
                    _admit_from_thread(lambda: holder["runner"]._hm_handle_running_session_message(
                        _msg(HUMAN_STEER), _source(), holder["key"]))
                _admit_from_thread(lambda: holder["runner"]._hm_handle_running_session_message(
                    _wake(), _source(), holder["key"]))
            elif nxt is not None:
                _admit_from_thread(lambda: admit_internal_event(adapter, _wake(nxt)))
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

    # Every turn's persisted display kind: the handler's fresh turns and the drain's recursive
    # follow-ups both enter ``_run_agent`` with it.
    kinds = []
    real_run_agent = runner._run_agent

    async def recording_run_agent(*args, **kwargs):
        kinds.append(kwargs.get("persist_user_display_kind"))
        return await real_run_agent(*args, **kwargs)

    runner._run_agent = recording_run_agent

    async def handler(event):
        from gateway.response_filters import display_kind_for_event
        generation = runner._begin_session_run_generation(key)
        await runner._run_agent(message=event.text or "", context_prompt="", history=[], source=_source(),
                                session_id="sess-depth", session_key=key, run_generation=generation,
                                persist_user_display_kind=display_kind_for_event(event))
        return None

    adapter.set_message_handler(handler)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    await adapter.handle_message(_msg("t0"))
    for _ in range(500):
        if any(WAKE in c for c in calls) and not any(not t.done() for t in list(adapter._session_tasks.values())):
            break
        await asyncio.sleep(0.01)
    return calls, kinds


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["queued", "steered"])
async def test_internal_wake_admitted_at_depth_cap_runs_as_a_later_turn(monkeypatch, tmp_path, delivery):
    calls, kinds = await _drive_chain(monkeypatch, tmp_path, delivery)
    assert calls[:4] == ["t0", "h1", "h2", "h3"], calls
    assert len(calls) == 5 and WAKE in calls[4], calls
    # The 5th turn runs as the internal wake it was, never as a human turn (t_506536db): before, the
    # re-seated pending_steer was a bare internal=False event (visible fallback, auth path).
    assert len(kinds) == 5 and kinds[-1] == "internal_notification", kinds


@pytest.mark.asyncio
@pytest.mark.parametrize("steer_on", ["t0", "h3"], ids=["depth0", "depth_cap"])
@pytest.mark.parametrize("delivery,expected_kind", [("steered", "internal_notification"), ("mixed", None)])
async def test_leftover_steer_provenance_follows_every_steered_text(monkeypatch, tmp_path, steer_on, delivery,
                                                                     expected_kind):
    """t_506536db: a re-seated leftover steer runs internal only when EVERY text steered into the turn was
    internal. One human steer beside the completion makes the next turn a human turn, at depth 0 and at
    the depth cap; a completion steered alone stays internal at depth 0 too."""
    calls, kinds = await _drive_chain(monkeypatch, tmp_path, delivery, steer_on=steer_on)
    chain = ["t0"] if steer_on == "t0" else ["t0", "h1", "h2", "h3"]
    assert calls[:-1] == chain and WAKE in calls[-1], calls
    assert (HUMAN_STEER in calls[-1]) == (delivery == "mixed"), calls
    assert len(kinds) == len(calls) and kinds[-1] == expected_kind, kinds
