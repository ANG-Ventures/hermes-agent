"""A message for an idle session waits for an earlier follow-up still being prepared (t_04c0a6b0).

Sequence (Prism bd085075ed3d on #569): an image arrives mid-turn and the busy handler starts
describing it (seconds of vision). The turn ends meanwhile, so the session goes idle. A text sent
after the image then finds no active session and starts a new turn at once; the image's handler
finishes, sees the turn gone, queues the image, and the image runs AFTER the later text.

Owner's requirement (Discord 1575444644152614992): everything arrives in the order sent. The
image goes first; the text is handled after it (steered into the image's turn, or queued).
"""
from __future__ import annotations

import asyncio

import pytest

from gateway.platforms.base import MessageEvent, MessageType
from tests.gateway.test_queued_followup_turn_clock_and_leftover_steer import _Adapter, _source

SK = "agent:main:telegram:group:-1001:17585"


def _rig():
    adapter = _Adapter()
    adapter._message_handler = lambda event: None
    adapter._event_session_key = lambda event: SK
    adapter._drop_unresolved = lambda event: False
    adapter._heal_stale_session_lock = lambda session_key: None
    log = []
    gate = asyncio.Event()

    def start(event, session_key, **_kw):
        log.append(("start", event.message_id))
        adapter._active_sessions[session_key] = asyncio.Event()
        return True

    adapter._start_session_processing = start

    async def busy_handler(event, session_key):
        log.append(("busy", event.message_id))
        if event.message_type == MessageType.PHOTO:
            await gate.wait()                       # the image is being described
            # the turn ended meanwhile: the image cannot be steered, so it is queued
            adapter._pending_messages[session_key] = event
            event._gateway_accepted = True
        else:
            adapter._pending_messages.setdefault(session_key, event)
        return True

    adapter._busy_session_handler = busy_handler
    return adapter, log, gate


def _photo():
    return MessageEvent(text="", message_type=MessageType.PHOTO, source=_source(), message_id="img",
                        media_urls=["/tmp/img.png"], media_types=["image/png"])


def _text():
    return MessageEvent(text="sent after the image", message_type=MessageType.TEXT, source=_source(),
                        message_id="txt")


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_text_after_an_image_in_preparation_does_not_start_a_turn_ahead_of_it():
    adapter, log, gate = _rig()
    adapter._active_sessions[SK] = asyncio.Event()           # a turn is running

    image_task = asyncio.create_task(adapter.handle_message(_photo()))
    await _settle()
    assert log == [("busy", "img")]

    del adapter._active_sessions[SK]                         # the turn ends during the description
    text_task = asyncio.create_task(adapter.handle_message(_text()))
    await _settle()
    assert ("start", "txt") not in log, "the later text started a new turn ahead of the image"

    gate.set()                                               # the description finishes
    await asyncio.wait_for(asyncio.gather(image_task, text_task), 5)

    assert log[:2] == [("busy", "img"), ("start", "img")], log
    assert ("start", "txt") not in log                       # it joins behind the image's turn
    assert ("busy", "txt") in log and log.index(("busy", "txt")) > log.index(("start", "img"))


@pytest.mark.asyncio
async def test_idle_message_with_nothing_in_preparation_starts_at_once():
    """Control: the ordinary idle path is unchanged."""
    adapter, log, _gate = _rig()
    await asyncio.wait_for(adapter.handle_message(_text()), 5)
    assert log == [("start", "txt")]


@pytest.mark.asyncio
async def test_busy_handler_bookkeeping_is_released_when_it_raises():
    adapter, log, _gate = _rig()
    adapter._active_sessions[SK] = asyncio.Event()

    async def boom(event, session_key):
        raise RuntimeError("prep failed")

    adapter._busy_session_handler = boom
    await asyncio.wait_for(adapter.handle_message(_photo()), 5)
    assert not getattr(adapter, "_busy_followups_inflight", {})
    del adapter._active_sessions[SK]
    adapter._busy_session_handler = None
    await asyncio.wait_for(adapter.handle_message(_text()), 5)
    assert ("start", "txt") in log
