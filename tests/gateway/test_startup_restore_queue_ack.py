"""A message queued behind the startup-restore gate gets one "still starting up" ack per chat.

Without it the user sees silence until the gate releases (fleet measurement: 293 queued messages,
p50 21s / p90 33s / max 71s before replay).
"""
import asyncio

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _gated_runner():
    runner, adapter = make_restart_runner()
    runner._startup_restore_in_progress = True
    runner._startup_restore_queue = []
    runner._startup_restore_tasks = []
    return runner, adapter


def _event(chat_id: str, text: str = "hi") -> MessageEvent:
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=make_restart_source(chat_id=chat_id))


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_queued_message_is_acked_once_per_chat():
    runner, adapter = _gated_runner()
    runner._queue_startup_restore_event(_event("chat-a", "one"))
    runner._queue_startup_restore_event(_event("chat-a", "two"))
    runner._queue_startup_restore_event(_event("chat-b", "three"))
    await _settle()

    acked = sorted(chat for chat, text, _meta in adapter.sent_calls if "Still starting up" in text)
    assert acked == ["chat-a", "chat-b"]
    assert all(meta.get("_interim_send") is True for _c, _t, meta in adapter.sent_calls)
    assert len(runner._startup_restore_queue) == 3


@pytest.mark.asyncio
async def test_no_ack_after_gate_released_before_send():
    runner, adapter = _gated_runner()
    runner._queue_startup_restore_event(_event("chat-a"))
    runner._startup_restore_in_progress = False  # gate released before the fire-and-forget ack ran
    await _settle()
    assert adapter.sent_calls == []


@pytest.mark.asyncio
async def test_missing_adapter_does_not_burn_dedupe_key():
    runner, adapter = _gated_runner()
    real = runner._intake_adapter_for
    runner._intake_adapter_for = lambda source: None
    runner._queue_startup_restore_event(_event("chat-a"))
    await _settle()
    assert adapter.sent_calls == []

    runner._intake_adapter_for = real
    runner._queue_startup_restore_event(_event("chat-a"))
    await _settle()
    assert [chat for chat, _text, _meta in adapter.sent_calls] == ["chat-a"]


@pytest.mark.asyncio
async def test_ack_failure_never_breaks_queueing():
    runner, adapter = _gated_runner()

    async def boom(*_a, **_k):
        raise RuntimeError("send exploded")

    adapter.send = boom
    runner._queue_startup_restore_event(_event("chat-a"))
    await _settle()
    assert len(runner._startup_restore_queue) == 1


def test_no_running_loop_unburns_key():
    runner, _adapter = _gated_runner()
    runner._queue_startup_restore_event(_event("chat-a"))  # no loop: create_task raises RuntimeError
    assert len(runner._startup_restore_queue) == 1
    assert not getattr(runner, "_startup_restore_acked_chats", set())
