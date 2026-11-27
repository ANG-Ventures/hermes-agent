"""A slash command sent right after /stop runs once the stop settles instead of bouncing.

Live 2026-11-08 11:27 (#fix-bottlenecks): /stop at :04, ``/model fable`` at :08 got
"Agent is running — wait or /stop first", while the stop released the slot only at :41.
/stop releases the running slot at the END of its cleanup (durable user-stopped marker,
adapter interrupt), so every reject-policy command in that window bounced even though the
user had already stopped the turn. These tests drive the real ``_handle_message`` and the
real ``_interrupt_and_clear_session``.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}

    async def send(self, chat_id, text, **kwargs):
        pass

    async def interrupt_session_activity(self, session_key, chat_id):
        event = self._active_sessions.get(session_key)
        if event is not None:
            event.set()


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._running_agent_tasks = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.delivery_router = MagicMock()
    return runner


def _event(text):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1")
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


def _start_running_turn(runner, key):
    agent = MagicMock()
    agent.session_id = "sess-1"
    runner._running_agents[key] = agent
    # A live turn: an ancient started_ts would let the stale-slot reaper evict it, and the
    # command would then pass for the wrong reason.
    runner._session_state(key).turn.started_ts = time.time()
    runner._running_agents_ts[key] = time.time()
    return agent


def _record_model(runner, calls):
    async def _model(event):
        calls.append(("model", runner._is_session_running(build_session_key(event.source))))
        return "switched"
    runner._handle_model_command = _model


async def _stop(runner, key, source, delay):
    async def _slow_marker(_key):  # the durable marker write that took ~31 s live
        await asyncio.sleep(delay)
    runner._mark_user_stopped = _slow_marker
    await runner._interrupt_and_clear_session(
        key, source, interrupt_reason="stop", invalidation_reason="stop_command")


@pytest.mark.asyncio
async def test_model_sent_during_stop_runs_after_the_slot_frees():
    runner = _make_runner()
    model_event = _event("/model fable")
    key = build_session_key(model_event.source)
    _start_running_turn(runner, key)
    calls = []
    _record_model(runner, calls)

    stop_task = asyncio.create_task(_stop(runner, key, model_event.source, delay=0.3))
    await asyncio.sleep(0.05)  # /stop has bumped the generation; the slot is still held
    assert runner._is_session_running(key)

    reply = await runner._handle_message(model_event)
    await stop_task

    assert reply == "switched", reply
    # The handler ran on the idle path, after /stop released the slot, not mid-stop.
    assert calls == [("model", False)]


@pytest.mark.asyncio
async def test_reject_command_without_a_stop_still_rejects_immediately():
    runner = _make_runner()
    model_event = _event("/model fable")
    key = build_session_key(model_event.source)
    _start_running_turn(runner, key)
    calls = []
    _record_model(runner, calls)

    reply = await asyncio.wait_for(runner._handle_message(model_event), timeout=1.0)

    assert "wait or /stop first" in str(reply)
    assert calls == []


@pytest.mark.asyncio
async def test_stop_that_never_settles_gets_an_honest_reply(monkeypatch):
    import gateway.run_inbound as run_inbound
    monkeypatch.setattr(run_inbound, "_STOP_SETTLE_WAIT_S", 0.2, raising=False)
    runner = _make_runner()
    model_event = _event("/model fable")
    key = build_session_key(model_event.source)
    _start_running_turn(runner, key)
    calls = []
    _record_model(runner, calls)

    stop_task = asyncio.create_task(_stop(runner, key, model_event.source, delay=2.0))
    await asyncio.sleep(0.05)

    reply = await asyncio.wait_for(runner._handle_message(model_event), timeout=1.5)
    stop_task.cancel()

    assert calls == []
    text = str(reply)
    assert "still finishing" in text and "/model" in text, text
    assert "wait or /stop first" not in text
