"""Mid-turn follow-ups in steer mode: images steer like text, in arrival order (t_086da8be).

Incident (session 20261124_055307_d1a590ed, 2026-11-25 18:20 PT): an image-only Discord
message arrived mid-turn and was queued for the next turn (image-only follow-ups were not
steerable); a voice note sent 23 s later was steered straight into the running turn, so the
model read "this screenshot..." before the screenshot existed for it.

Ace's ruling on the first fix (#569, which held later follow-ups behind the queued image):
"I don't understand why a picture sent by itself can't be injected mid-task. I want it to work
that way." So:

* An image (with or without a caption or a voice note) arriving mid-turn in steer mode is
  steered into the running turn as the same block a queued image gets at turn start: the
  vision description plus the cached path (``_describe_images_as_text``).
* Steers are serialized per session in arrival order: a later follow-up waits while an earlier
  image's description is being prepared, so it never lands first.
* Fallback (the #569 guard): when a message from a person is still QUEUED (a file, a video, an
  image whose download or description failed, or anything queued while the agent was starting),
  a later follow-up queues behind it instead of steering. Internal wakes never hold a steer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL
from gateway.session import SessionSource

SK = "agent:main:discord:dm:chat-1"
HELD_LOG = "held behind queued follow-up"
IMAGE_QUEUED_LOG = "steer image queued for the next turn"
DESC = "A pinball table; the top paddle is off its rail."


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


class _Vision:
    """Stands in for tools.vision_tools.vision_analyze_tool (the real JSON contract)."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail = False
        self.gate: asyncio.Event | None = None   # when set, the call blocks until released

    async def __call__(self, image_url, user_prompt=None, **_kw):
        self.calls.append(image_url)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            return json.dumps({"success": False, "error": "vision backend down"})
        return json.dumps({"success": True, "analysis": f"{DESC} ({image_url.rsplit('/', 1)[-1]})"})


def _source() -> SessionSource:
    return SessionSource(platform=Platform.DISCORD, chat_id="chat-1", chat_type="dm", user_id="ace")


def _text(text: str, mid: str) -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id=mid, message_type=MessageType.TEXT)


def _photo(path, mid: str = "img", caption: str = "") -> MessageEvent:
    return MessageEvent(text=caption, source=_source(), message_id=mid, message_type=MessageType.PHOTO,
                        media_urls=[str(path)], media_types=["image/png"])


def _document(mid: str = "doc") -> MessageEvent:
    return MessageEvent(text="", source=_source(), message_id=mid, message_type=MessageType.DOCUMENT,
                        media_urls=["/tmp/report.pdf"], media_types=["application/pdf"])


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
    vision = _Vision()
    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", vision)
    shots = []
    for name in ("shot.png", "shot2.png"):
        path = tmp_path / name
        path.write_bytes(b"\x89PNG fake")
        shots.append(path)
    return runner, adapter, agent, stt, vision, shots


async def _busy(runner, event, mode, agent):
    """The busy handler's steer-or-queue step (run_busy._handle_active_session_busy_message)."""
    outcome = await runner._resolve_busy_steer_or_redirect(event, SK, mode, agent)
    if not outcome.steered and not outcome.redirected:
        runner._queue_or_replace_pending_event(SK, event)
    return outcome


def _lines(caplog, needle):
    return [r for r in caplog.records if needle in r.getMessage() and r.levelno == logging.INFO]


def _body(steer_payload: str) -> str:
    """The steered text without the gateway's origin header."""
    return steer_payload.split("\n\n", 1)[1]


# ── Images steer into the running turn ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_image_only_follow_up_is_steered_with_description_and_cached_path(rig):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    outcome = await _busy(runner, _photo(shot), "steer", agent)
    assert outcome.steered is True and outcome.effective_mode == "steer"
    assert vision.calls == [str(shot)]
    body = _body(agent.steered[0])
    assert body.startswith("[The user sent an image~ Here's what I can see:\n" + DESC)
    assert f"use vision_analyze with image_url: {shot} ~]" in body
    assert adapter._pending_messages == {} and not runner._overflow_queue(SK)


@pytest.mark.asyncio
async def test_image_with_caption_is_steered_block_then_caption(rig):
    runner, adapter, agent, _stt, _vision, (shot, _) = rig
    outcome = await _busy(runner, _photo(shot, caption="compare it with the rail"), "steer", agent)
    assert outcome.steered is True
    body = _body(agent.steered[0])
    assert body.index("Here's what I can see") < body.index(str(shot)) < body.index("compare it with the rail")
    assert body.endswith("\n\ncompare it with the rail")
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_image_then_text_while_description_runs_arrive_in_that_order(rig):
    """Image first; a text arrives while its description is still being prepared (Ace's
    2-seconds-later case, driven by a gate instead of the clock). The text must wait."""
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    vision.gate = asyncio.Event()
    image_task = asyncio.create_task(_busy(runner, _photo(shot), "steer", agent))
    for _ in range(200):                           # until the image is inside its vision call
        if vision.calls:
            break
        await asyncio.sleep(0)
    assert vision.calls, "the image never reached the vision step"
    text_task = asyncio.create_task(_busy(runner, _text("see the rail", "m2"), "steer", agent))
    for _ in range(20):
        await asyncio.sleep(0)
    assert agent.steered == [], "the text overtook the image whose description is still running"
    vision.gate.set()
    image_out, text_out = await asyncio.gather(image_task, text_task)
    assert image_out.steered and text_out.steered
    assert [_body(s).startswith("[The user sent an image~") for s in agent.steered] == [True, False]
    assert _body(agent.steered[1]) == "see the rail"
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_text_then_image_arrive_in_that_order(rig):
    runner, adapter, agent, _stt, _vision, (shot, _) = rig
    await _busy(runner, _text("look at this", "m1"), "steer", agent)
    await _busy(runner, _photo(shot), "steer", agent)
    assert _body(agent.steered[0]) == "look at this"
    assert _body(agent.steered[1]).startswith("[The user sent an image~")
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_two_images_are_both_steered_in_order(rig):
    runner, adapter, agent, _stt, vision, (shot, shot2) = rig
    vision.gate = asyncio.Event()
    first = asyncio.create_task(_busy(runner, _photo(shot, "i1"), "steer", agent))
    second = asyncio.create_task(_busy(runner, _photo(shot2, "i2"), "steer", agent))
    for _ in range(20):
        await asyncio.sleep(0)
    assert vision.calls == [str(shot)], "the second image's description started before the first finished"
    vision.gate.set()
    await asyncio.gather(first, second)
    assert vision.calls == [str(shot), str(shot2)]
    assert [str(shot) in s for s in agent.steered] == [True, False]
    assert [str(shot2) in s for s in agent.steered] == [False, True]


@pytest.mark.asyncio
async def test_voice_note_then_image_arrive_in_that_order(rig):
    runner, adapter, agent, stt, _vision, (shot, _) = rig
    await _busy(runner, _voice(), "steer", agent)
    await _busy(runner, _photo(shot), "steer", agent)
    stt.assert_awaited_once()
    assert _body(agent.steered[0]).startswith("this screenshot shows the paddle")
    assert _body(agent.steered[1]).startswith("[The user sent an image~")


# ── An image that cannot be described falls back to the queue, and later follow-ups hold ───────

@pytest.mark.asyncio
async def test_image_whose_description_fails_queues_and_later_text_queues_behind_it(rig, caplog):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    vision.fail = True
    caplog.set_level(logging.INFO, logger="gateway")
    photo = _photo(shot)
    outcome = await _busy(runner, photo, "steer", agent)
    assert outcome.steered is False and agent.steered == []
    assert adapter._pending_messages[SK] is photo                  # vision retried at turn start
    assert len(_lines(caplog, IMAGE_QUEUED_LOG)) == 1
    later = await _busy(runner, _text("this screenshot", "m2"), "steer", agent)
    assert later.steered is False and agent.steered == []
    assert "this screenshot" in adapter._pending_messages[SK].text  # folds into the image's turn
    assert len(_lines(caplog, HELD_LOG)) == 1


@pytest.mark.asyncio
async def test_image_not_downloaded_queues_without_a_vision_call(rig, caplog):
    runner, adapter, agent, _stt, vision, _shots = rig
    caplog.set_level(logging.INFO, logger="gateway")
    # The Discord adapter leaves the CDN URL in media_urls when caching fails.
    photo = _photo("https://cdn.discordapp.com/attachments/1/2/shot.png")
    outcome = await _busy(runner, photo, "steer", agent)
    assert outcome.steered is False and vision.calls == [] and agent.steered == []
    assert adapter._pending_messages[SK] is photo
    assert len(_lines(caplog, IMAGE_QUEUED_LOG)) == 1


# ── Unchanged paths ───────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_text_with_empty_queue_still_steers(rig, caplog):
    runner, adapter, agent, *_ = rig
    caplog.set_level(logging.INFO, logger="gateway")
    outcome = await _busy(runner, _text("also check the rail", "m1"), "steer", agent)
    assert outcome.steered is True
    assert len(agent.steered) == 1 and agent.steered[0].endswith("also check the rail")
    assert adapter._pending_messages == {}
    assert _lines(caplog, HELD_LOG) == []


@pytest.mark.asyncio
async def test_text_after_image_that_opened_the_turn_still_steers(rig):
    runner, adapter, agent, _stt, _vision, (shot, _) = rig
    runner._session_state(SK).turn.event = _photo(shot, "opener")
    outcome = await _busy(runner, _text("see the top paddle", "m2"), "steer", agent)
    assert outcome.steered is True and len(agent.steered) == 1
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_queued_internal_wake_does_not_hold_a_steer(rig, caplog):
    runner, adapter, agent, _stt, _vision, (shot, _) = rig
    wake = _kanban_wake()
    runner._queue_or_replace_pending_event(SK, wake)
    assert adapter._pending_messages[SK] is wake
    caplog.set_level(logging.INFO, logger="gateway")
    assert (await _busy(runner, _text("ship it", "m4"), "steer", agent)).steered is True
    assert (await _busy(runner, _photo(shot), "steer", agent)).steered is True
    assert len(agent.steered) == 2
    assert adapter._pending_messages[SK] is wake and not runner._overflow_queue(SK)
    assert _lines(caplog, HELD_LOG) == []


@pytest.mark.asyncio
async def test_voice_note_with_empty_queue_still_steers(rig):
    runner, adapter, agent, stt, *_ = rig
    outcome = await _busy(runner, _voice(), "steer", agent)
    assert outcome.steered is True and len(agent.steered) == 1
    stt.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_mode_still_queues_an_image_without_vision(rig):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    photo = _photo(shot)
    outcome = await _busy(runner, photo, "queue", agent)
    assert outcome.steered is False and vision.calls == [] and agent.steered == []
    assert adapter._pending_messages[SK] is photo


def _interrupt_rig(runner):
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._agent_has_active_subagents = lambda _agent: False
    runner._session_in_startup_resume = lambda _key: False


@pytest.mark.asyncio
async def test_interrupt_mode_image_is_unchanged_queued_not_steered(rig):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    _interrupt_rig(runner)
    photo = _photo(shot)
    outcome = await _busy(runner, photo, "interrupt", agent)
    assert outcome.redirected is False and outcome.steered is False
    assert agent.steered == [] and agent.redirected == [] and vision.calls == []
    assert adapter._pending_messages[SK] is photo


@pytest.mark.asyncio
async def test_interrupt_mode_redirect_with_empty_queue_still_redirects(rig):
    runner, adapter, agent, *_ = rig
    _interrupt_rig(runner)
    outcome = await _busy(runner, _text("no, the top one", "m7"), "interrupt", agent)
    assert outcome.redirected is True and len(agent.redirected) == 1


# ── Fallback guard: a follow-up never overtakes a QUEUED message from a person (#569) ─────────

@pytest.mark.asyncio
async def test_text_after_queued_document_queues_behind_it(rig, caplog):
    runner, adapter, agent, *_ = rig
    doc = _document()
    outcome = await _busy(runner, doc, "steer", agent)      # a file cannot be steered: queued
    assert outcome.steered is False and adapter._pending_messages[SK] is doc
    caplog.set_level(logging.INFO, logger="gateway")
    text = _text("see page 2", "m3")
    outcome = await _busy(runner, text, "steer", agent)
    assert agent.steered == [], "the follow-up overtook the queued document"
    assert outcome.steered is False and outcome.effective_mode == "queue"
    assert adapter._pending_messages[SK] is doc and list(runner._overflow_queue(SK)) == [text]
    assert len(_lines(caplog, HELD_LOG)) == 1


@pytest.mark.asyncio
async def test_image_after_queued_document_queues_behind_it_without_vision(rig):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    doc, photo = _document(), _photo(shot)
    runner._queue_or_replace_pending_event(SK, doc)
    outcome = await _busy(runner, photo, "steer", agent)
    assert outcome.steered is False and vision.calls == [] and agent.steered == []
    assert adapter._pending_messages[SK] is doc and list(runner._overflow_queue(SK)) == [photo]


@pytest.mark.asyncio
async def test_voice_note_after_queued_document_queues_behind_it_without_stt(rig):
    runner, adapter, agent, stt, *_ = rig
    doc = _document()
    runner._queue_or_replace_pending_event(SK, doc)
    voice = _voice()
    outcome = await _busy(runner, voice, "steer", agent)
    assert agent.steered == [] and outcome.steered is False
    stt.assert_not_awaited()                     # transcribed when its own turn runs, not here
    assert adapter._pending_messages[SK] is doc
    assert list(runner._overflow_queue(SK)) == [voice]
    # Drain order after the turn: the slot runs next and the overflow head is staged into the slot.
    first = runner._promote_queued_event(SK, adapter, adapter._pending_messages.pop(SK))
    second = adapter._pending_messages.pop(SK)
    assert [first, second] == [doc, voice]
    assert not runner._overflow_queue(SK)


@pytest.mark.asyncio
async def test_human_message_in_overflow_queue_holds_a_steer(rig, caplog):
    runner, adapter, agent, *_ = rig
    wake, doc = _kanban_wake(), _document()
    runner._queue_or_replace_pending_event(SK, wake)       # slot: internal wake
    runner._queue_or_replace_pending_event(SK, doc)        # overflow: a person's file
    assert adapter._pending_messages[SK] is wake and list(runner._overflow_queue(SK)) == [doc]
    caplog.set_level(logging.INFO, logger="gateway")
    text = _text("look at the file", "m6")
    outcome = await _busy(runner, text, "steer", agent)
    assert agent.steered == [] and outcome.steered is False
    assert list(runner._overflow_queue(SK)) == [doc, text]
    assert len(_lines(caplog, HELD_LOG)) == 1


@pytest.mark.asyncio
async def test_text_queued_while_agent_pending_holds_the_next_steer(rig):
    runner, adapter, agent, *_ = rig
    first = _text("first", "m1")
    await _busy(runner, first, "steer", _AGENT_PENDING_SENTINEL)   # not live yet: queues
    assert adapter._pending_messages[SK] is first
    second = _text("second", "m2")
    outcome = await _busy(runner, second, "steer", agent)
    assert agent.steered == [] and outcome.steered is False
    assert adapter._pending_messages[SK] is first and list(runner._overflow_queue(SK)) == [second]


@pytest.mark.asyncio
async def test_interrupt_mode_redirect_held_behind_queued_document(rig, caplog):
    runner, adapter, agent, *_ = rig
    _interrupt_rig(runner)
    doc = _document()
    runner._queue_or_replace_pending_event(SK, doc)
    caplog.set_level(logging.INFO, logger="gateway")
    text = _text("no, the other one", "m7")
    outcome = await _busy(runner, text, "interrupt", agent)
    assert agent.redirected == [] and outcome.redirected is False
    assert adapter._pending_messages[SK] is doc and list(runner._overflow_queue(SK)) == [text]
    assert len(_lines(caplog, HELD_LOG)) == 1


# ── Sibling site: the runner fast path (run_inbound) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_fast_path_image_is_not_forced_to_the_queue_in_steer_mode(rig, monkeypatch):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    monkeypatch.setattr(runner, "_effective_busy_input_mode", lambda _src: "steer")
    photo = _photo(shot)
    handled, _ = await runner._hm_busy_slash_or_photo(photo, _source(), SK)
    assert handled is False and adapter._pending_messages == {}
    await runner._hm_busy_steer(photo, agent, SK)
    assert len(agent.steered) == 1 and str(shot) in agent.steered[0]
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_fast_path_image_still_queues_without_interrupt_outside_steer_mode(rig, monkeypatch):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    monkeypatch.setattr(runner, "_effective_busy_input_mode", lambda _src: "interrupt")
    photo = _photo(shot)
    handled, _ = await runner._hm_busy_slash_or_photo(photo, _source(), SK)
    assert handled is True and adapter._pending_messages[SK] is photo and vision.calls == []


@pytest.mark.asyncio
async def test_fast_path_image_then_text_arrive_in_that_order(rig):
    runner, adapter, agent, _stt, vision, (shot, _) = rig
    vision.gate = asyncio.Event()
    image_task = asyncio.create_task(runner._hm_busy_steer(_photo(shot), agent, SK))
    for _ in range(200):                           # until the image is inside its vision call
        if vision.calls:
            break
        await asyncio.sleep(0)
    assert vision.calls, "the image never reached the vision step"
    text_task = asyncio.create_task(runner._hm_busy_steer(_text("see the rail", "m2"), agent, SK))
    for _ in range(20):
        await asyncio.sleep(0)
    assert agent.steered == []
    vision.gate.set()
    await asyncio.gather(image_task, text_task)
    assert [str(shot) in s for s in agent.steered] == [True, False]
    assert _body(agent.steered[1]) == "see the rail"


@pytest.mark.asyncio
async def test_fast_path_steer_held_behind_queued_document(rig, caplog):
    runner, adapter, agent, *_ = rig
    doc = _document()
    runner._queue_or_replace_pending_event(SK, doc)
    caplog.set_level(logging.INFO, logger="gateway")
    text = _text("see page 2", "m3")
    await runner._hm_busy_steer(text, agent, SK)
    assert agent.steered == []
    assert adapter._pending_messages[SK] is doc and list(runner._overflow_queue(SK)) == [text]
    assert len(_lines(caplog, HELD_LOG)) == 1


@pytest.mark.asyncio
async def test_fast_path_steer_with_empty_queue_still_steers(rig):
    runner, adapter, agent, *_ = rig
    await runner._hm_busy_steer(_text("this screenshot", "m3"), agent, SK)
    assert len(agent.steered) == 1 and adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_fast_path_redirect_held_behind_queued_document(rig):
    runner, adapter, agent, *_ = rig
    doc = _document()
    runner._queue_or_replace_pending_event(SK, doc)
    text = _text("no, the other one", "m7")
    await runner._hm_busy_interrupt(text, _source(), agent, SK)
    assert agent.redirected == []
    assert agent.interrupted == ["no, the other one"]      # the existing interrupt path, not a redirect
    # The interrupt text alone is dropped when a pending event exists (run_turn uses the pending
    # event first), so the follow-up must also be queued, behind the document.
    assert adapter._pending_messages[SK] is doc and list(runner._overflow_queue(SK)) == [text]


@pytest.mark.asyncio
async def test_fast_path_redirect_with_empty_queue_still_redirects(rig):
    runner, adapter, agent, *_ = rig
    await runner._hm_busy_interrupt(_text("no, the top one", "m7"), _source(), agent, SK)
    assert len(agent.redirected) == 1 and agent.interrupted == []
    assert adapter._pending_messages == {}


# ── pr.md gap: interrupt mode, voice/media follow-up while something is already pending ───────

@pytest.mark.asyncio
async def test_fast_path_voice_interrupt_with_a_pending_wake_keeps_the_voice_note(rig):
    """run_turn reads the bare interrupt text only when nothing is pending; with a queued wake the
    voice transcript passed to interrupt() alone was dropped. The event must be queued too."""
    runner, adapter, agent, stt, *_ = rig
    wake = _kanban_wake()
    runner._queue_or_replace_pending_event(SK, wake)
    voice = _voice()
    await runner._hm_busy_interrupt(voice, _source(), agent, SK)
    assert len(agent.interrupted) == 1                     # still interrupts the turn
    assert adapter._pending_messages[SK] is wake and list(runner._overflow_queue(SK)) == [voice]


@pytest.mark.asyncio
async def test_fast_path_voice_interrupt_with_nothing_pending_is_unchanged(rig):
    runner, adapter, agent, stt, *_ = rig
    await runner._hm_busy_interrupt(_voice(), _source(), agent, SK)
    assert agent.interrupted == ["this screenshot shows the paddle"]
    assert adapter._pending_messages == {} and not runner._overflow_queue(SK)
