"""Tests for the session race guard that prevents concurrent agent runs.

The sentinel-based guard ensures that when _handle_message passes the
"is an agent already running?" check and proceeds to the slow async
setup path (vision enrichment, STT, hooks, session hygiene), a second
message for the same session is correctly recognized as "already running"
and routed through the interrupt/queue path instead of spawning a
duplicate agent.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL
from gateway.session import SessionSource, build_session_key


class _FakeAdapter:
    """Minimal adapter stub for testing."""

    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}
        self.interrupted_sessions = []

    async def send(self, chat_id, text, **kwargs):
        pass

    async def interrupt_session_activity(self, session_key, chat_id):
        self.interrupted_sessions.append((session_key, chat_id))
        event = self._active_sessions.get(session_key)
        if event is not None:
            event.set()


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
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


def _make_event(text="hello", chat_id="12345"):
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm",
        user_id="u1",
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)


# ------------------------------------------------------------------
# Test 1: Sentinel is placed before _handle_message_with_agent runs
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sentinel_placed_before_agent_setup():
    """After passing the 'not running' guard, the sentinel must be
    written into _running_agents *before* any await, so that a
    concurrent message sees the session as occupied."""
    runner = _make_runner()
    event = _make_event()
    session_key = build_session_key(event.source)

    # Patch _handle_message_with_agent to capture state at entry
    sentinel_was_set = False

    async def mock_inner(self_inner, ev, src, qk, generation):
        nonlocal sentinel_was_set
        sentinel_was_set = runner._running_agents.get(qk) is _AGENT_PENDING_SENTINEL
        return "ok"

    with patch.object(GatewayRunner, "_handle_message_with_agent", mock_inner):
        await runner._handle_message(event)

    assert sentinel_was_set, (
        "Sentinel must be in _running_agents when _handle_message_with_agent starts"
    )


# ------------------------------------------------------------------
# Test 2: Sentinel is cleaned up after _handle_message_with_agent
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Test 3: Sentinel cleaned up on exception
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Test 4: Second message during sentinel sees "already running"
# ------------------------------------------------------------------


def test_merge_pending_message_event_merges_text_and_photo_followups():
    pending = {}
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="u1",
    )
    session_key = build_session_key(source)

    text_event = MessageEvent(
        text="first follow-up",
        message_type=MessageType.TEXT,
        source=source,
    )
    photo_event = MessageEvent(
        text="see screenshot",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["/tmp/test.png"],
        media_types=["image/png"],
    )

    merge_pending_message_event(pending, session_key, text_event, merge_text=True)
    merge_pending_message_event(pending, session_key, photo_event, merge_text=True)

    merged = pending[session_key]
    assert merged.message_type == MessageType.PHOTO
    assert merged.text == "first follow-up\n\nsee screenshot"
    assert merged.media_urls == ["/tmp/test.png"]
    assert merged.media_types == ["image/png"]


@pytest.mark.asyncio
async def test_recent_telegram_followups_append_in_pending_queue():
    runner = _make_runner()
    first = _make_event(text="part one")
    second = _make_event(text="part two")
    session_key = build_session_key(first.source)

    fake_agent = MagicMock()
    fake_agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
    runner._running_agents[session_key] = fake_agent
    import time as _time
    runner._running_agents_ts[session_key] = _time.time()

    await runner._handle_message(first)
    await runner._handle_message(second)

    fake_agent.interrupt.assert_not_called()
    adapter = runner.adapters[Platform.TELEGRAM]
    assert adapter._pending_messages[session_key].text == "part one\npart two"


# ------------------------------------------------------------------
# Test 5: Sentinel not placed for command messages
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_command_is_noop_during_active_session():
    """A mid-run /start must not interrupt the active agent or show commands."""
    runner = _make_runner()
    event = _make_event(text="/start")
    session_key = build_session_key(event.source)

    fake_agent = MagicMock()
    fake_agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
    runner._running_agents[session_key] = fake_agent
    runner._handle_help_command = AsyncMock(return_value="Help text")

    result = await runner._handle_message(event)

    assert result == ""
    runner._handle_help_command.assert_not_awaited()
    fake_agent.interrupt.assert_not_called()
    assert session_key not in runner.adapters[Platform.TELEGRAM]._pending_messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_text", "handler_attr", "handler_result"),
    [
        ("/help", "_handle_help_command", "Help text"),
        ("/commands", "_handle_commands_command", "Commands text"),
        ("/update", "_handle_update_command", "Update text"),
        ("/profile", "_handle_profile_command", "Profile text"),
    ],
)
async def test_active_session_bypass_commands_dispatch_without_interrupt(
    command_text,
    handler_attr,
    handler_result,
):
    """Gateway-handled bypass commands must return directly while an agent runs."""
    runner = _make_runner()
    event = _make_event(text=command_text)
    session_key = build_session_key(event.source)

    fake_agent = MagicMock()
    fake_agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
    runner._running_agents[session_key] = fake_agent
    setattr(runner, handler_attr, AsyncMock(return_value=handler_result))

    result = await runner._handle_message(event)

    assert result == handler_result
    fake_agent.interrupt.assert_not_called()
    assert session_key not in runner.adapters[Platform.TELEGRAM]._pending_messages


# ------------------------------------------------------------------
# Test 6: /stop during sentinel force-cleans and unlocks session
# ------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("stop_before_inner", [True, False])
async def test_stop_during_sentinel_force_cleans_session(
    tmp_path, monkeypatch, stop_before_inner,
):
    """/stop unlocks both admission and setup without leaking the stopped lease."""
    from hermes_cli import active_sessions
    from tests.gateway.test_max_concurrent_sessions import _silence_global_gateway_hooks

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner()
    runner.config.max_concurrent_sessions = 1
    runner._persist_active_agents = MagicMock()
    runner._mark_user_stopped = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    runner._post_turn_hooks_after_delivery = AsyncMock()
    event1 = _make_event(text="hello")
    session_key = build_session_key(event1.source)
    path = tmp_path / "runtime" / "active_sessions.json"
    entered = asyncio.Event()
    inner_entered = asyncio.Event()
    proceed = threading.Event()
    barrier = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    acquire = runner._claim_active_session_slot
    acquired = []
    released = []
    real_release = active_sessions.ActiveSessionLease.release

    def delayed_claim(*args):
        if stop_before_inner:
            assert threading.get_ident() != loop_thread
            loop.call_soon_threadsafe(entered.set)
            assert proceed.wait(10), "admission barrier was not released"
        result = acquire(*args)
        if result[0] is not None:
            acquired.append(result[0].lease_id)
        return result

    def release(lease):
        released.append(lease.lease_id)
        return real_release(lease)

    monkeypatch.setattr(runner, "_claim_active_session_slot", delayed_claim)
    monkeypatch.setattr(active_sessions.ActiveSessionLease, "release", release)

    async def slow_inner(self_inner, ev, src, qk, generation):
        inner_entered.set()
        await barrier.wait()
        return "ok"

    with patch.object(GatewayRunner, "_handle_message_with_agent", slow_inner):
        task1 = asyncio.create_task(runner._handle_message(event1))
        try:
            async with asyncio.timeout(10):
                await (entered if stop_before_inner else inner_entered).wait()
                assert runner._running_agents.get(session_key) is _AGENT_PENDING_SENTINEL
                result = await runner._handle_message(_make_event(text="/stop"))
                assert result is not None
                assert "stopp" in result.lower()
                assert session_key not in runner._running_agents
                adapter = runner.adapters[Platform.TELEGRAM]
                assert session_key not in adapter._pending_messages
                proceed.set()
                barrier.set()
                if stop_before_inner:
                    # /stop invalidated admission; a late lease must not start setup.
                    with pytest.raises(asyncio.CancelledError):
                        await task1
                    assert task1.cancelled()
                    assert not inner_entered.is_set()
                else:
                    # This fake adapter interrupts activity but does not cancel the task.
                    assert await task1 == "ok"
                await runner._drain_active_session_releases()
                assert len(acquired) == 1
                assert released == acquired
                assert not active_sessions._read_entries(path)
                assert session_key not in runner._running_agents

                # The real cap must admit a successor after the stopped owner drains.
                assert await runner._handle_message(_make_event(text="successor")) == "ok"
                await runner._drain_active_session_releases()
                assert len(acquired) == 2
                assert acquired[0] != acquired[1]
                assert released == acquired
                assert not active_sessions._read_entries(path)
                assert session_key not in runner._running_agents
        finally:
            proceed.set()
            barrier.set()
            if not task1.done():
                task1.cancel()
            await asyncio.gather(task1, return_exceptions=True)
            await runner._drain_active_session_releases()


# ------------------------------------------------------------------
# Test 6b: /stop hard-kills a running agent and unlocks session
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Test 6c: /stop clears pending messages to prevent stale replays
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Test 7: Shutdown skips sentinel entries
# ------------------------------------------------------------------
    # Should not have raised on the sentinel
