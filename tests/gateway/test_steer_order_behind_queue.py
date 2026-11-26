"""A busy steer may not overtake an earlier queued message from a person (t_086da8be).

Incident (session 20261124_055307_d1a590ed, 2026-11-25 18:20 PT): an image-only Discord
message arrived mid-turn and was queued for the next turn (image-only follow-ups are not
steerable); a voice note sent 23 s later was steered straight into the running turn, so the
model read "this screenshot..." before the screenshot existed for it.

The rule under test: when the session's pending slot or overflow queue already holds a
non-internal event, a later follow-up joins the queue behind it instead of steering (or
redirecting). Internal wakes (kanban, cron, delegation completions) never hold a steer.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL
from gateway.session import SessionSource

SK = "agent:main:discord:dm:chat-1"
HELD_LOG = "held behind queued follow-up"


class _Agent:
    """A live running agent that records steer / redirect / interrupt calls."""

    _supports_active_turn_redirect = True

    def __init__(self):
        self.steered: list[str] = []
        self.redirected: list[str] = []
        self.interrupted: list[str] = []

    def steer(self, text):
        self.steered.append(text)
        return True

    def redirect(self, text):
        self.redirected.append(text)
        return True

    def interrupt(self, text=None):
        self.interrupted.append(text)


class _Adapter:
    def __init__(self):
        self._pending_messages: dict = {}


def _source() -> SessionSource:
    return SessionSource(platform=Platform.DISCORD, chat_id="chat-1", chat_type="dm", user_id="ace")


def _text(text: str, mid: str) -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id=mid, message_type=MessageType.TEXT)


def _photo(mid: str = "img") -> MessageEvent:
    return MessageEvent(text="", source=_source(), message_id=mid, message_type=MessageType.PHOTO,
                        media_urls=["/tmp/shot.png"], media_types=["image/png"])


def _voice(mid: str = "voice") -> MessageEvent:
    return MessageEvent(text="", source=_source(), message_id=mid, message_type=MessageType.VOICE,
                        media_urls=["/tmp/note.ogg"], media_types=["audio/ogg"])


def _kanban_wake() -> MessageEvent:
    return MessageEvent(text="[kanban] card t_1 moved", source=_source(), message_id="wake",
                        message_type=MessageType.TEXT, internal=True, wake_kind="kanban",
                        metadata={"kanban_card_id": "t_1"})


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    adapter = _Adapter()
    runner.adapters = {Platform.DISCORD: adapter}
    agent = _Agent()
    runner._session_state(SK).turn.agent = agent
    # Voice STT is out of scope: a fake transcript, and a handle to prove it was (not) called.
    stt = AsyncMock(return_value=("this screenshot shows the paddle", ["this screenshot shows the paddle"]))
    monkeypatch.setattr(runner, "_transcribe_and_echo_pending_voice", stt)
    return runner, adapter, agent, stt


async def _busy(runner, event, mode, agent):
    """The busy handler's steer-or-queue step (run_busy._handle_active_session_busy_message)."""
    outcome = await runner._resolve_busy_steer_or_redirect(event, SK, mode, agent)
    if not outcome.steered and not outcome.redirected:
        runner._queue_or_replace_pending_event(SK, event)
    return outcome


def _held_lines(caplog):
    return [r for r in caplog.records if HELD_LOG in r.getMessage() and r.levelno == logging.INFO]


# 1. The incident: image queued mid-turn, then a text follow-up.
@pytest.mark.asyncio
async def test_text_after_queued_image_queues_behind_it(rig, caplog):
    runner, adapter, agent, _stt = rig
    photo = _photo()
    runner._queue_or_replace_pending_event(SK, photo)       # image-only: not steerable, queued
    caplog.set_level(logging.INFO, logger="gateway")

    outcome = await _busy(runner, _text("this screenshot, see the rail", "m3"), "steer", agent)

    assert agent.steered == [], "the follow-up overtook the queued image"
    assert outcome.steered is False and outcome.effective_mode == "queue"
    head = adapter._pending_messages[SK]
    assert head is photo and head.media_urls == ["/tmp/shot.png"]
    assert "this screenshot, see the rail" in head.text      # folded into the photo's turn
    assert not runner._overflow_queue(SK)
    assert len(_held_lines(caplog)) == 1


# 2. Text only, nothing queued: still steers.
@pytest.mark.asyncio
async def test_text_with_empty_queue_still_steers(rig, caplog):
    runner, adapter, agent, _stt = rig
    caplog.set_level(logging.INFO, logger="gateway")
    outcome = await _busy(runner, _text("also check the rail", "m1"), "steer", agent)
    assert outcome.steered is True
    assert len(agent.steered) == 1 and agent.steered[0].endswith("also check the rail")
    assert adapter._pending_messages == {}
    assert _held_lines(caplog) == []


# 3. The image OPENED the turn (it is the turn's trigger, not queued): a follow-up still steers.
@pytest.mark.asyncio
async def test_text_after_image_that_opened_the_turn_still_steers(rig):
    runner, adapter, agent, _stt = rig
    runner._session_state(SK).turn.event = _photo("opener")
    outcome = await _busy(runner, _text("see the top paddle", "m2"), "steer", agent)
    assert outcome.steered is True and len(agent.steered) == 1
    assert adapter._pending_messages == {}


# 4. Only an internal wake is queued: a human follow-up still steers.
@pytest.mark.asyncio
async def test_queued_internal_wake_does_not_hold_a_steer(rig, caplog):
    runner, adapter, agent, _stt = rig
    wake = _kanban_wake()
    runner._queue_or_replace_pending_event(SK, wake)
    assert adapter._pending_messages[SK] is wake
    caplog.set_level(logging.INFO, logger="gateway")
    outcome = await _busy(runner, _text("ship it", "m4"), "steer", agent)
    assert outcome.steered is True and len(agent.steered) == 1
    assert adapter._pending_messages[SK] is wake and not runner._overflow_queue(SK)
    assert _held_lines(caplog) == []


# 5. Voice note behind a queued image (the literal incident shape): queued after the image, no STT now.
@pytest.mark.asyncio
async def test_voice_note_after_queued_image_queues_behind_it_without_stt(rig):
    runner, adapter, agent, stt = rig
    photo = _photo()
    runner._queue_or_replace_pending_event(SK, photo)
    voice = _voice()
    outcome = await _busy(runner, voice, "steer", agent)
    assert agent.steered == [] and outcome.steered is False
    stt.assert_not_awaited()                     # transcribed when its own turn runs, not here
    assert adapter._pending_messages[SK] is photo
    assert list(runner._overflow_queue(SK)) == [voice]
    # Drain order after the turn: the slot runs next (image) and the overflow head (voice note) is
    # staged into the slot for the turn after it.
    first = runner._promote_queued_event(SK, adapter, adapter._pending_messages.pop(SK))
    second = adapter._pending_messages.pop(SK)
    assert [first, second] == [photo, voice]
    assert not runner._overflow_queue(SK)


# 5b. No regression: a voice note with nothing queued still steers (#58780).
@pytest.mark.asyncio
async def test_voice_note_with_empty_queue_still_steers(rig):
    runner, adapter, agent, stt = rig
    outcome = await _busy(runner, _voice(), "steer", agent)
    assert outcome.steered is True and len(agent.steered) == 1
    stt.assert_awaited_once()


# 6. A human message sitting in the OVERFLOW queue (slot holds an internal wake) also holds the steer.
@pytest.mark.asyncio
async def test_human_message_in_overflow_queue_holds_a_steer(rig, caplog):
    runner, adapter, agent, _stt = rig
    wake, photo = _kanban_wake(), _photo()
    runner._queue_or_replace_pending_event(SK, wake)       # slot: internal wake
    runner._queue_or_replace_pending_event(SK, photo)      # overflow: human image
    assert adapter._pending_messages[SK] is wake and list(runner._overflow_queue(SK)) == [photo]
    caplog.set_level(logging.INFO, logger="gateway")
    text = _text("look at the image", "m6")
    outcome = await _busy(runner, text, "steer", agent)
    assert agent.steered == [] and outcome.steered is False
    assert list(runner._overflow_queue(SK)) == [photo, text]
    assert len(_held_lines(caplog)) == 1


# 6b. Text queued during the agent-pending window, then a second text once the agent is live.
@pytest.mark.asyncio
async def test_text_queued_while_agent_pending_holds_the_next_steer(rig):
    runner, adapter, agent, _stt = rig
    first = _text("first", "m1")
    await _busy(runner, first, "steer", _AGENT_PENDING_SENTINEL)   # not live yet: queues
    assert adapter._pending_messages[SK] is first
    second = _text("second", "m2")
    outcome = await _busy(runner, second, "steer", agent)
    assert agent.steered == [] and outcome.steered is False
    assert adapter._pending_messages[SK] is first and list(runner._overflow_queue(SK)) == [second]


# 7. Interrupt mode: a redirect may not overtake a queued image either (it interrupts, then queues).
@pytest.mark.asyncio
async def test_interrupt_mode_redirect_held_behind_queued_image(rig, caplog):
    runner, adapter, agent, _stt = rig
    monkey_false = AsyncMock(return_value=False)
    runner._session_has_compression_in_flight = monkey_false
    runner._agent_has_active_subagents = lambda _agent: False
    runner._session_in_startup_resume = lambda _key: False
    photo = _photo()
    runner._queue_or_replace_pending_event(SK, photo)
    caplog.set_level(logging.INFO, logger="gateway")
    outcome = await _busy(runner, _text("no, the top one", "m7"), "interrupt", agent)
    assert agent.redirected == [] and outcome.redirected is False
    head = adapter._pending_messages[SK]
    assert head is photo and "no, the top one" in head.text
    assert len(_held_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_interrupt_mode_redirect_with_empty_queue_still_redirects(rig):
    runner, adapter, agent, _stt = rig
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._agent_has_active_subagents = lambda _agent: False
    runner._session_in_startup_resume = lambda _key: False
    outcome = await _busy(runner, _text("no, the top one", "m7"), "interrupt", agent)
    assert outcome.redirected is True and len(agent.redirected) == 1


# Sibling site: the runner fast path (run_inbound._hm_busy_steer / _hm_busy_interrupt).
@pytest.mark.asyncio
async def test_fast_path_steer_held_behind_queued_image(rig, caplog):
    runner, adapter, agent, _stt = rig
    photo = _photo()
    runner._queue_or_replace_pending_event(SK, photo)
    caplog.set_level(logging.INFO, logger="gateway")
    runner._hm_busy_steer(_text("this screenshot", "m3"), agent, SK)
    assert agent.steered == []
    head = adapter._pending_messages[SK]
    assert head is photo and "this screenshot" in head.text
    assert len(_held_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_fast_path_steer_with_empty_queue_still_steers(rig):
    runner, adapter, agent, _stt = rig
    runner._hm_busy_steer(_text("this screenshot", "m3"), agent, SK)
    assert len(agent.steered) == 1 and adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_fast_path_redirect_held_behind_queued_image(rig):
    runner, adapter, agent, _stt = rig
    photo = _photo()
    runner._queue_or_replace_pending_event(SK, photo)
    await runner._hm_busy_interrupt(_text("no, the top one", "m7"), _source(), agent, SK)
    assert agent.redirected == []
    assert agent.interrupted == ["no, the top one"]      # the existing interrupt path, not a redirect
    # The interrupt text alone is dropped when a pending event exists (run_turn uses the pending
    # event first), so the follow-up must also be queued, behind the image.
    head = adapter._pending_messages[SK]
    assert head is photo and "no, the top one" in head.text


@pytest.mark.asyncio
async def test_fast_path_redirect_with_empty_queue_still_redirects(rig):
    runner, adapter, agent, _stt = rig
    await runner._hm_busy_interrupt(_text("no, the top one", "m7"), _source(), agent, SK)
    assert len(agent.redirected) == 1 and agent.interrupted == []
    assert adapter._pending_messages == {}
