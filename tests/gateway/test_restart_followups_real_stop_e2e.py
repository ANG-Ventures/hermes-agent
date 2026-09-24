"""Adapter-parked follow-ups must survive the REAL stop(restart=True) -> boot path.

Argus r3 (t_e253d9d5): stop() ran ``_bounded_adapter_teardown()`` first, whose
``BasePlatformAdapter.cancel_background_tasks()`` drained ``_pending_messages``
into the #72680 shutdown flush file (not replayable as a turn: no session_id),
and only afterwards swept the (now empty) slot into the restart spool.  Result:
spool 0, boot adapter receipts 0.

Oracle is independent of the production spool/loader: the boot adapter's
``handle_message`` receipts, plus the on-disk spool and flush dirs.
"""

import asyncio
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.fork_ext import restart_followups as rf
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    TextDebounceState,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.shutdown_flush import _get_flush_dir


class _RecordingAdapter(BasePlatformAdapter):
    def __init__(self, platform, received, events=None):
        super().__init__(PlatformConfig(enabled=True, token="t"), platform)
        self._received = received
        self._events = events

    async def connect(self, *, is_reconnect: bool = False):
        if hasattr(self, "_mark_connected"):
            self._mark_connected()
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def handle_message(self, event):
        self._received.append((event.source.chat_id, event.text))
        if self._events is not None:
            self._events.append(event)


def _runner(tmp_path, received, events=None):
    runner = GatewayRunner(
        GatewayConfig(
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")},
            sessions_dir=tmp_path / "sessions",
        )
    )
    runner._create_adapter = lambda platform, cfg: _RecordingAdapter(platform, received, events)

    async def _no_secondary():
        return 0

    runner._start_secondary_profile_adapters = _no_secondary
    return runner


def _event(chat_id, text):
    src = SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id="u" + chat_id)
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=src)


async def _restart_and_boot(tmp_path, park, events=None):
    first = _runner(tmp_path, [])
    await asyncio.wait_for(first.start(), timeout=60)
    park(first.adapters[Platform.TELEGRAM], first)
    await asyncio.wait_for(first.stop(restart=True, service_restart=False), timeout=60)
    spooled = len(list(rf.spool_dir().glob("*.json")))
    flushed = len(list(_get_flush_dir().glob("*.json")))

    received: list = []
    boot = _runner(tmp_path, received, events)
    try:
        await asyncio.wait_for(boot.start(), timeout=60)
        for _ in range(100):
            if received and not list(rf.spool_dir().glob("*.json")):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)  # a duplicate replay would land here
    finally:
        await asyncio.wait_for(boot.stop(), timeout=30)
    return spooled, flushed, received


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    (tmp_path / "logs").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_pending_slot_followup_survives_real_stop_and_replays_once(home):
    def park(adapter, _runner):
        adapter._pending_messages["agent:main:telegram:dm:101"] = _event("101", "queued during restart")

    spooled, flushed, received = await _restart_and_boot(home, park)

    assert received == [("101", "queued during restart")], (spooled, flushed, received)
    assert spooled == 1
    assert flushed == 0, "follow-up went to the non-replayable flush file"
    assert list(rf.spool_dir().glob("*.json")) == []


@pytest.mark.asyncio
async def test_debounce_buffered_followup_survives_real_stop(home):
    """Sibling carrier: queue-mode busy text still in the debounce buffer."""

    def park(adapter, _runner):
        now = time.monotonic()
        adapter._text_debounce_store()["agent:main:telegram:dm:202"] = TextDebounceState(
            event=_event("202", "debounced burst"), task=None, first_ts=now, last_ts=now,
        )

    spooled, flushed, received = await _restart_and_boot(home, park)

    assert received == [("202", "debounced burst")], (spooled, flushed, received)
    assert flushed == 0


# --- Argus r5/r6: overflow tail + event identity across the real restart ----

import dataclasses
import logging

_KEY = "agent:main:telegram:dm:{}"


@pytest.mark.asyncio
async def test_queue_head_and_overflow_tail_both_replay_in_order_once(home, caplog):
    """r5: the 2nd /queue item lives in the runner's SessionFieldView overflow.

    ``_queued_events`` is a MutableMapping view, not a dict; a dict-only gate
    skipped it, so the user was told "(2 queued)" and only the head survived.
    """
    key = _KEY.format("101")

    def park(adapter, runner):
        runner._enqueue_fifo(key, _event("101", "head follow-up"), adapter)
        runner._enqueue_fifo(key, _event("101", "tail follow-up"), adapter)
        assert runner._queue_depth(key, adapter=adapter) == 2

    with caplog.at_level(logging.WARNING):
        spooled, flushed, received = await _restart_and_boot(home, park)

    assert received == [("101", "head follow-up"), ("101", "tail follow-up")], (
        spooled, flushed, received,
    )
    assert spooled == 2
    assert "restart_followup_lost" not in caplog.text


# Test-owned oracle, deliberately NOT read from production: the only fields a
# restart may lose. Every other dataclass field must round-trip unchanged.
_MAY_BE_LOST = {"raw_message", "suppress_public_echo", "deferred_reply_text"}


def test_not_carried_set_is_exactly_the_process_bound_fields():
    assert set(rf.NOT_CARRIED_FIELDS) == _MAY_BE_LOST | {"text", "source"}


def _identity(event):
    """Every MessageEvent field a restart must keep, plus the command view."""
    carried = {
        f.name: getattr(event, f.name)
        for f in dataclasses.fields(event)
        if f.name not in _MAY_BE_LOST
    }
    carried["source"] = event.source.to_dict()
    carried["is_command"] = event.is_command()
    return carried


def _src(chat_id):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id="u1")


def _identity_cases(tmp_path):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpg")
    return {
        "user-text": MessageEvent(text="plain user follow-up", source=_src("301")),
        # Plugin injection: untrusted payload text must stay conversational.
        "plugin-injection": MessageEvent(
            text="/new",
            source=_src("302"),
            internal=True,
            allow_gateway_control=False,
            metadata={"plugin_id": "qa", "plugin_injection": True, "gateway_session_strict": True},
        ),
        "goal-continuation": MessageEvent(
            text="Continue toward the goal.", source=_src("303"), internal=True
        ),
        "photo-caption": MessageEvent(
            text="look at this",
            message_type=MessageType.PHOTO,
            source=_src("304"),
            media_urls=[str(photo)],
            media_types=["image/jpeg"],
        ),
        "photo-no-caption": MessageEvent(
            text="",
            message_type=MessageType.PHOTO,
            source=_src("305"),
            media_urls=[str(photo)],
            media_types=["image/jpeg"],
        ),
        "reply-channel-skill": MessageEvent(
            text="and this?",
            source=_src("306"),
            message_id="m-9",
            user_id="u1",
            user_name="Ace",
            reply_to_message_id="m-8",
            reply_to_text="earlier answer",
            reply_to_author_id="bot",
            reply_to_author_name="Apollo",
            reply_to_is_own_message=True,
            auto_skill=["skill-a", "skill-b"],
            channel_prompt="be terse",
            channel_context="ctx line",
            prompt_response={"prompt_id": "p1", "option_id": "o1"},
            platform_update_id=77,
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["user-text", "plugin-injection", "goal-continuation", "photo-caption",
     "photo-no-caption", "reply-channel-skill"],
)
@pytest.mark.parametrize("carrier", ["slot", "overflow"])
async def test_replayed_event_is_the_event_that_was_parked(home, case, carrier):
    """r6: the boot replay must hand the adapter the SAME event, field for field.

    Oracle: the event object the boot adapter receives, compared with the
    parked event over every carried dataclass field.
    """
    parked = _identity_cases(home)[case]
    key = _KEY.format(parked.source.chat_id)
    expected = _identity(parked)

    def park(adapter, runner):
        if carrier == "slot":
            adapter._pending_messages[key] = parked
        else:
            runner._enqueue_fifo(key, _event(parked.source.chat_id, "head"), adapter)
            runner._enqueue_fifo(key, parked, adapter)

    events: list = []
    await _restart_and_boot(home, park, events)

    got = [e for e in events if e.text != "head"]
    assert len(got) == 1, [(e.text, e.internal) for e in events]
    if carrier == "overflow":
        assert [e.text for e in events] == ["head", parked.text]
    assert _identity(got[0]) == expected
    if case == "plugin-injection":
        assert got[0].is_command() is False


@pytest.mark.asyncio
async def test_unstorable_event_field_is_refused_and_logged_not_replayed_altered(home, caplog):
    """An event whose field cannot be stored durably is never spooled as a
    different (stripped) event: it is refused, logged by field name, and the
    slot keeps it for the #72680 flush file."""
    key = _KEY.format("401")

    def park(adapter, _runner):
        adapter._pending_messages[key] = MessageEvent(
            text="has an object in metadata", source=_src("401"), metadata={"obj": object()}
        )

    with caplog.at_level(logging.ERROR):
        spooled, flushed, received = await _restart_and_boot(home, park)

    assert received == []
    assert spooled == 0
    assert flushed == 1
    assert "PHASE=restart_followup_lost" in caplog.text and "field=metadata" in caplog.text
