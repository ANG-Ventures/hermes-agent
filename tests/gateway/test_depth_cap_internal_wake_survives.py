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


async def _drive_chain(monkeypatch, tmp_path, delivery, *, steer_on="h3", sent=None):
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
            if delivery == "promotion_race":
                return self._promotion_race(message, adapter)
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

        def _promotion_race(self, message, adapter):
            # Prism e08366573530: C1 (t0) queues a follow-up; C2 (h1) reuses this cached agent, and
            # its tracker has not yet published C2's context, so the slot still holds (A, C1). A human
            # steer lands in that window, an internal completion after promotion; both come back as
            # one leftover, which must run as a HUMAN turn and must not be silenced.
            result = {"final_response": f"answer {message}", "messages": [], "api_calls": 1}
            if message == "t0":
                _admit_from_thread(lambda: admit_internal_event(adapter, _wake("h1")))
            elif message == "h1":
                assert not holder["promoted"].is_set()
                _admit_from_thread(lambda: holder["runner"]._hm_handle_running_session_message(
                    _msg(HUMAN_STEER), _source(), holder["key"]))
                loop.call_soon_threadsafe(holder["gate"].set)
                assert holder["promoted"].wait(5)
                _admit_from_thread(lambda: holder["runner"]._hm_handle_running_session_message(
                    _wake(), _source(), holder["key"]))
                assert self._steered == f"{HUMAN_STEER}\n{WAKE}" or (
                    HUMAN_STEER in self._steered and WAKE in self._steered), self._steered
                result["pending_steer"], self._steered = self._steered, None
            else:
                result["final_response"] = "NO_REPLY"
            return result

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = StubAgent
    if delivery == "promotion_race":
        _cached = []
        # One cached agent A for every turn, as the gateway's agent cache hands a follow-up.
        fake_run_agent.AIAgent = lambda **kw: _cached[0] if _cached else (_cached.append(StubAgent(**kw))
                                                                          or _cached[0])
        holder["gate"], holder["promoted"] = asyncio.Event(), threading.Event()
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
    if delivery == "promotion_race":
        real_track = runner._run_agent_track_agent
        tracks = []

        async def gated_track(turn_ctx):
            tracks.append(turn_ctx)
            if len(tracks) == 2:  # C2: hold publication until the human steer has landed
                await holder["gate"].wait()
                await real_track(turn_ctx)
                holder["promoted"].set()
                return
            await real_track(turn_ctx)

        runner._run_agent_track_agent = gated_track

    async def handler(event):
        from gateway.response_filters import display_kind_for_event
        generation = runner._begin_session_run_generation(key)
        response = await runner._run_agent(message=event.text or "", context_prompt="", history=[],
                                           source=_source(), session_id="sess-depth", session_key=key,
                                           run_generation=generation,
                                           persist_user_display_kind=display_kind_for_event(event))
        if sent is not None:
            sent.append(response)
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


@pytest.mark.asyncio
async def test_steer_provenance_binds_to_the_receiving_turn_not_the_slot_context(monkeypatch, tmp_path):
    """Prism e08366573530 (t_506536db r3): a human steer accepted while the slot still holds the
    previous context of the cached agent, plus an internal completion after promotion, re-seat as one
    leftover. That turn is HUMAN (display kind None) and its NO_REPLY is not silently suppressed."""
    from gateway.response_filters import silence_allowed
    responses = []
    calls, kinds = await _drive_chain(monkeypatch, tmp_path, "promotion_race", sent=responses)
    assert calls[:2] == ["t0", "h1"] and len(calls) == 3, calls
    assert HUMAN_STEER in calls[2] and WAKE in calls[2], calls
    assert len(kinds) == 3 and kinds[-1] is None, kinds
    # The handler judges the chain's terminal turn by these keys (run_turn.py silence verdict): a human
    # terminal turn's NO_REPLY is replaced by a visible reply, an internal one stays silent.
    (final,) = responses
    assert final["final_response"] == "NO_REPLY", final
    assert final["queued_terminal_display_kind"] is None, final
    assert not silence_allowed(final["queued_terminal_display_kind"], final.get("queued_terminal_reply_expected"))


class _BufferAgent:
    """The real steer buffer (``InterruptControlMixin.steer`` / ``_drain_pending_steer``) and nothing else."""

    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()

    from agent.interrupt_control import InterruptControlMixin as _icm
    steer = _icm.steer
    _drain_pending_steer = _icm._drain_pending_steer
    del _icm


def _steer(agent, text, *, internal):
    from gateway.run_busy import GatewayBusySessionMixin
    runner = SimpleNamespace(_steer_active_subagents=lambda running_agent, text: 0)
    assert GatewayBusySessionMixin._steer_running_agent(runner, agent, text, internal=internal)


def test_internal_steer_drained_without_leftover_does_not_mark_a_later_human_steer():
    """Prism c87ed8b8f07d (t_506536db r4): an internal steer consumed by a tool batch leaves no
    leftover, so nothing classifies it. A later HUMAN steer with the same text is still human."""
    from gateway.run_busy import leftover_steer_is_internal
    agent = _BufferAgent()
    _steer(agent, "continue", internal=True)
    assert agent._drain_pending_steer() == "continue"  # delivered mid-turn, no leftover
    _steer(agent, "continue", internal=False)
    leftover = agent._drain_pending_steer()  # the finalizer's leftover drain
    assert leftover == "continue"
    assert leftover_steer_is_internal(agent, leftover) is False


def test_mixed_leftover_retires_its_internal_entries():
    """Prism 18ae9a407387 (t_506536db r4): a mixed leftover is human AND retires its entries, so a
    later human-only leftover equal to the internal part is still human."""
    from gateway.run_busy import leftover_steer_is_internal
    agent = _BufferAgent()
    _steer(agent, "done", internal=True)
    _steer(agent, "please explain", internal=False)
    leftover = agent._drain_pending_steer()
    assert leftover == "done\nplease explain"
    assert leftover_steer_is_internal(agent, leftover) is False
    _steer(agent, "done", internal=False)
    leftover = agent._drain_pending_steer()
    assert leftover == "done"
    assert leftover_steer_is_internal(agent, leftover) is False


def test_all_internal_buffer_entries_classify_internal():
    from gateway.run_busy import leftover_steer_is_internal
    agent = _BufferAgent()
    _steer(agent, "human earlier", internal=False)
    agent._drain_pending_steer()  # drained by a tool batch: not part of the next buffer
    _steer(agent, "wake one", internal=True)
    _steer(agent, "wake two", internal=True)
    leftover = agent._drain_pending_steer()
    assert leftover_steer_is_internal(agent, leftover) is True
    # Retired: the same text steered again by a human is human.
    _steer(agent, "wake one", internal=False)
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is False


def test_turn_end_without_leftover_retires_entries_before_a_non_gateway_steer():
    """t_506536db r4: the turn-end call (no leftover) retires an internal entry a tool batch drained, so
    an identical steer that bypasses the gateway ledger (kanban note, api_server) reads human."""
    from gateway.run_busy import leftover_steer_is_internal
    agent = _BufferAgent()
    _steer(agent, "continue", internal=True)
    agent._drain_pending_steer()  # delivered mid-turn
    assert leftover_steer_is_internal(agent, None) is False  # turn end, no leftover
    agent.steer("continue")  # producer with no gateway entry
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is False
