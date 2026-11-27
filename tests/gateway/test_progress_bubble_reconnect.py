"""The turn's progress bubble survives a mid-turn adapter reconnect (t_8783804a).

2026-11-09 04:56:45 PST: Discord's liveness probe forced a reconnect mid-turn ("Session is closed").
The reconnect watcher installed a NEW adapter object, but the heartbeat line and the tool-lines bubble
kept the one captured at turn start: every later edit hit the dead adapter, the bubble lost its
editable state, and for 12+ minutes no #131 line (elapsed, still-inside notice) reached the chat.
Both painters now re-resolve the live adapter and keep editing the SAME message.
"""

import asyncio
import queue
import threading
import time
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.turn_context import TurnContext
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter


class _DeadAfterReconnect(ProgressCaptureAdapter):
    """The adapter the turn started on; ``kill()`` is the liveness probe closing its session."""

    def __init__(self):
        super().__init__(Platform.DISCORD)
        self.dead = False

    def kill(self):
        self.dead = True
        self._set_fatal_error("discord_websocket_health_stale", "health check failed", retryable=True)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if self.dead:
            return SendResult(success=False, error="send_path_degraded", retryable=True)
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def edit_message(self, chat_id, message_id, content):
        if self.dead:
            return SendResult(success=False, error="Session is closed")
        return await super().edit_message(chat_id, message_id, content)


class _Registry:
    """Stands in for ``GatewayRunner.adapters``: the reconnect watcher swaps the object in place."""

    def __init__(self, adapter):
        self.adapter = adapter

    def _delivery_adapter_for(self, source):
        return self.adapter


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


def _long_call_agent(elapsed):
    return SimpleNamespace(_current_tool="terminal", _current_tool_started_at=time.time() - elapsed,
                           _active_children=[], _active_children_lock=threading.Lock(), _delegate_children_log=[])


def _turn(monkeypatch, registry, agent):
    import gateway.run_turn_runner as rtr
    from gateway.run_turn_runner import TurnRunner

    clock = _Clock()
    monkeypatch.setattr(rtr, "time", clock)
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform=Platform.DISCORD), progress_queue=queue.Queue(),
                      _run_still_current=lambda: True, progress_grouping="accumulate")
    ctx.agent_holder = [agent]
    ctx.user_config = {}
    ctx.resolve_display_setting = lambda cfg, pk, key: 300 if key == "tool_inflight_notice_seconds" else None
    tr = TurnRunner(registry, ctx)
    st = tr._progress_edit_state(registry.adapter)
    st.progress_lines = ["💻 terminal: sleep 335"]
    st.progress_msg_id = "bubble-1"
    return tr, st, clock


@pytest.mark.asyncio
async def test_progress_loop_keeps_painting_after_reconnect(monkeypatch):
    """End to end through ``send_progress_messages``: the incident's exact sequence."""
    old = _DeadAfterReconnect()
    registry = _Registry(old)
    agent = _long_call_agent(5)
    tr, st, clock = _turn(monkeypatch, registry, agent)
    monkeypatch.setattr(tr, "_progress_edit_state", lambda _a: st)
    task = asyncio.create_task(tr.send_progress_messages())
    try:
        await asyncio.sleep(0.05)
        old.kill()                                   # session closed mid-turn
        tr._ctx.progress_queue.put("💻 terminal: sleep 540")
        await asyncio.sleep(0.5)                     # a tick or two against the dead adapter
        new = ProgressCaptureAdapter(Platform.DISCORD)
        registry.adapter = new                       # adapter healthy again
        agent._current_tool_started_at -= 400        # the long call crosses 300 s
        clock.t += 61
        for _ in range(40):
            if any("still inside" in e["content"] for e in new.edits):
                break
            await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert new.sent == [], "the bubble is edited in place, never re-posted"
    assert new.edits and {e["message_id"] for e in new.edits} == {"bubble-1"}
    painted = [e["content"] for e in new.edits if "still inside" in e["content"]]
    assert painted and painted[0].split("\n")[:2] == ["💻 terminal: sleep 335", "💻 terminal: sleep 540"]


@pytest.mark.asyncio
async def test_bubble_edits_same_message_through_reconnected_adapter(monkeypatch):
    old = _DeadAfterReconnect()
    registry = _Registry(old)
    agent = _long_call_agent(200)
    tr, st, clock = _turn(monkeypatch, registry, agent)

    old.kill()                                   # 04:56:45: session closed mid-turn
    agent._current_tool_started_at -= 120        # 04:58:16: the call crosses 300 s
    await tr._rebind_progress_adapter(st)
    await tr._paint_inflight_notice(st)
    assert st.can_edit, "a dead transport must not permanently disable editing"

    new = ProgressCaptureAdapter(Platform.DISCORD)
    registry.adapter = new                       # reconnect watcher installs the fresh adapter
    clock.t += 61                                # next repaint window
    await tr._rebind_progress_adapter(st)
    await tr._paint_inflight_notice(st)

    assert new.sent == [], "the bubble is edited in place, never re-posted"
    assert new.edits and all(e["message_id"] == "bubble-1" for e in new.edits)
    assert new.edits[-1]["content"].split("\n")[-1].startswith("⏳ still inside terminal for 5m")


@pytest.mark.asyncio
async def test_bubble_repaints_pending_lines_as_soon_as_adapter_is_back(monkeypatch):
    """Lines absorbed while the transport was dead must land on the next tick, not wait for another event."""
    old = _DeadAfterReconnect()
    registry = _Registry(old)
    tr, st, _ = _turn(monkeypatch, registry, _long_call_agent(5))
    old.kill()
    tr._progress_absorb(st, "💻 terminal: rm -rf /tmp/x")
    assert await tr._progress_send_or_edit(st, "💻 terminal: rm -rf /tmp/x") is False  # transient: retry
    assert st.can_edit

    new = ProgressCaptureAdapter(Platform.DISCORD)
    registry.adapter = new
    await tr._rebind_progress_adapter(st)
    assert new.sent == []
    assert new.edits[-1] == {"chat_id": "c", "message_id": "bubble-1",
                             "content": "💻 terminal: sleep 335\n💻 terminal: rm -rf /tmp/x"}


@pytest.mark.asyncio
async def test_bubble_recreated_once_when_original_cannot_be_edited(monkeypatch):
    class _Gone(ProgressCaptureAdapter):
        async def edit_message(self, chat_id, message_id, content):
            self.edits.append({"message_id": message_id, "content": content})
            return SendResult(success=False, error="404 Not Found (error code: 10008): Unknown Message")

    old = _DeadAfterReconnect()
    registry = _Registry(old)
    tr, st, _ = _turn(monkeypatch, registry, _long_call_agent(5))
    old.kill()
    new = _Gone(Platform.DISCORD)
    registry.adapter = new
    await tr._rebind_progress_adapter(st)
    assert len(new.sent) == 1 and new.sent[0]["content"] == "💻 terminal: sleep 335"
    assert st.progress_msg_id == "progress-1" and st.can_edit


@pytest.mark.asyncio
async def test_permanent_failure_on_healthy_adapter_still_disables_editing(monkeypatch):
    """Unchanged behavior: a non-retryable failure from the LIVE, healthy adapter is about the message."""
    class _Gone(ProgressCaptureAdapter):
        async def edit_message(self, chat_id, message_id, content):
            return SendResult(success=False, error="404 Not Found (error code: 10008): Unknown Message")

    adapter = _Gone(Platform.DISCORD)
    tr, st, _ = _turn(monkeypatch, _Registry(adapter), _long_call_agent(5))
    assert await tr._progress_send_or_edit(st, "x") is True
    assert st.can_edit is False and st.repaint_pending is False
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_heartbeat_edits_through_reconnected_adapter(monkeypatch):
    from tests.gateway.test_wake_progress_header import _bare_runner

    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0.01")
    runner = _bare_runner()
    registry = _Registry(None)

    class _New(ProgressCaptureAdapter):
        async def edit_message(self, chat_id, message_id, content):
            runner._restart_requested = True     # one heartbeat after the reconnect, then stop
            return await super().edit_message(chat_id, message_id, content)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            runner._restart_requested = True
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    new = _New(Platform.DISCORD)

    class _Old(_DeadAfterReconnect):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
            if result.success:                   # first heartbeat posted; then the session dies
                self.kill()
                registry.adapter = new           # reconnect watcher installs the fresh adapter
            return result

    old = _Old()
    registry.adapter = old
    runner._delivery_adapter_for = registry._delivery_adapter_for
    runner._agent_activity_summary = staticmethod(lambda a: {"api_call_count": 5, "max_iterations": 300,
                                                             "current_tool": "terminal"})
    agent = _long_call_agent(200)
    runner._running_agents["sess"] = agent
    disp = MagicMock()
    disp._display_surface_mode.return_value = "on"
    disp.resolve_display_setting.return_value = True
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform="discord"), session_key="sess")
    ctx.agent_holder[0] = agent
    await asyncio.wait_for(runner._run_agent_notify_long_running(disp, ctx, [None]), 5)

    assert len(old.sent) == 1                    # first heartbeat posted on the original adapter
    assert new.sent == [], "the heartbeat line is edited in place after the reconnect, not re-posted"
    assert [e["message_id"] for e in new.edits] == ["progress-1"]
