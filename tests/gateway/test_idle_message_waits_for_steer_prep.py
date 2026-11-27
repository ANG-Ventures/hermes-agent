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


def _photo_n(mid):
    return MessageEvent(text="", message_type=MessageType.PHOTO, source=_source(), message_id=mid,
                        media_urls=[f"/tmp/{mid}.png"], media_types=["image/png"])


def _text_n(mid):
    return MessageEvent(text=f"text {mid}", message_type=MessageType.TEXT, source=_source(), message_id=mid)


@pytest.mark.asyncio
async def test_a_later_text_does_not_overtake_a_text_still_waiting_once_a_new_turn_starts():
    """Prism c2f91d63d749: photos A and B are being described when turn 1 ends; text C waits.
    A finishes and its orphan start begins turn 2 while B is still being described. Text D,
    arriving now, found the new guard and went straight to the busy path ahead of C."""
    adapter, log, _gate = _rig()
    gates = {"A": asyncio.Event(), "B": asyncio.Event()}

    async def busy_handler(event, session_key):
        log.append(("busy", event.message_id))
        if event.message_type == MessageType.PHOTO:
            await gates[event.message_id].wait()
            adapter._pending_messages[session_key] = event   # turn gone: queue it (orphan-started)
            event._gateway_accepted = True
        else:
            adapter._pending_messages.setdefault(session_key, event)
        return True

    adapter._busy_session_handler = busy_handler
    adapter._active_sessions[SK] = asyncio.Event()           # turn 1 is running
    tasks = [asyncio.create_task(adapter.handle_message(_photo_n("A")))]
    await _settle()
    tasks.append(asyncio.create_task(adapter.handle_message(_photo_n("B"))))
    await _settle()
    del adapter._active_sessions[SK]                         # turn 1 ends during both descriptions
    tasks.append(asyncio.create_task(adapter.handle_message(_text_n("C"))))
    await _settle()
    gates["A"].set()                                         # A queued + orphan-started: turn 2
    await _settle()
    assert ("start", "A") in log and SK in adapter._active_sessions
    tasks.append(asyncio.create_task(adapter.handle_message(_text_n("D"))))
    await _settle()
    gates["B"].set()
    await asyncio.wait_for(asyncio.gather(*tasks), 5)

    assert ("busy", "C") in log and ("busy", "D") in log, log
    assert log.index(("busy", "C")) < log.index(("busy", "D")), f"D overtook C: {log}"


@pytest.mark.asyncio
async def test_idle_goal_wake_holding_a_lock_does_not_wait_on_an_inline_command_needing_it():
    """Prism aa2f4134d3c7: the idle-goal watcher holds the goal lock across handle_message, and an
    inline ``/goal pause`` needs that lock. If the inline command counted as a follow-up in
    preparation, the wake waited for it and it waited for the lock: a deadlock."""
    adapter, log, _gate = _rig()
    goal_lock = asyncio.Lock()

    async def inline_reply(event):
        log.append(("inline", event.message_id))
        async with goal_lock:                                # /goal pause takes the goal lock
            log.append(("inline-done", event.message_id))

    adapter._dispatch_inline_reply = inline_reply
    adapter._active_sessions[SK] = asyncio.Event()           # a turn is running

    async def watcher():
        async with goal_lock:                                # held across handle_message
            await _settle()                                  # the off-loop recheck
            del adapter._active_sessions[SK]                 # the turn has ended
            wake = MessageEvent(text="goal wake", message_type=MessageType.TEXT, source=_source(),
                                message_id="wake", internal=True)
            await adapter.handle_message(wake)

    watcher_task = asyncio.create_task(watcher())
    await asyncio.sleep(0)
    pause = MessageEvent(text="/goal pause", message_type=MessageType.TEXT, source=_source(),
                         message_id="pause")
    pause_task = asyncio.create_task(adapter.handle_message(pause))
    await asyncio.wait_for(asyncio.gather(watcher_task, pause_task), 3)
    assert ("start", "wake") in log and ("inline-done", "pause") in log, log


@pytest.mark.asyncio
async def test_an_idle_command_waits_for_an_image_still_being_prepared():
    """Prism c2f91d63d749 (round 2): an idle ``/restart`` must not start ahead of an acknowledged
    photo whose preparation has not settled (restart's work count does not see that handler)."""
    adapter, log, gate = _rig()
    adapter._active_sessions[SK] = asyncio.Event()
    image_task = asyncio.create_task(adapter.handle_message(_photo()))
    await _settle()
    del adapter._active_sessions[SK]                         # the turn ends during the description
    restart = MessageEvent(text="/restart", message_type=MessageType.TEXT, source=_source(),
                           message_id="rs")
    restart_task = asyncio.create_task(adapter.handle_message(restart))
    await _settle()
    assert ("start", "rs") not in log, "the idle /restart overtook the image in preparation"
    gate.set()
    await asyncio.wait_for(asyncio.gather(image_task, restart_task), 5)
    assert log.index(("start", "img")) < len(log)
    assert ("start", "rs") not in log[: log.index(("start", "img"))], log
