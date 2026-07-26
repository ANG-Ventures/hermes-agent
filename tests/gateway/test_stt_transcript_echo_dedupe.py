"""Regression: one voice message must produce exactly one 🎙️ transcript echo.

A single platform voice message can reach the gateway's echo path as two
DISTINCT ``MessageEvent`` objects: the busy/interrupt path echoes the inbound
object, then the pending-slot object drains through
``_prepare_inbound_message_text`` later.  The old per-event
``_gateway_pending_stt_echo_sent`` flag only deduped within one object, and
``_prepare_inbound_message_text`` sent inline without consulting it at all, so
users saw the same transcript posted twice for one voice note.

These tests assert the invariant (one platform message -> one echo) rather
than freezing any particular implementation of the dedupe.
"""
import types

import pytest


def _make_runner():
    from gateway.run import GatewayRunner

    return GatewayRunner.__new__(GatewayRunner)


class _EchoAdapter:
    """Minimal adapter capturing what the gateway would post to chat."""

    def __init__(self):
        self.sent = []
        self._pending_messages = {}

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(content)
        return True


def _wire(runner, adapter, transcript="hello once"):
    runner._should_echo_stt_transcripts = lambda: True
    runner._pending_event_audio_paths = lambda ev: list(
        getattr(ev, "media_urls", []) or []
    )
    runner._adapter_for_source = lambda src: adapter
    runner._thread_metadata_for_source = lambda src, anchor=None: None
    runner._reply_anchor_for_event = lambda ev: None
    runner._consume_pending_native_image_paths = lambda key: []
    runner.config = types.SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        multiplex_profiles=False,
    )

    async def _fake_enrich(text, audio_paths):
        return f"{text or ''}\n[voice]: {transcript}", [transcript]

    runner._enrich_message_with_transcription = _fake_enrich


def _source():
    from gateway.platforms.base import Platform
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan-1",
        chat_type="channel",
        user_id="u1",
        user_name="Ace",
    )


def _voice_event(source, message_id: "str | None" = "MSG-1", path="/tmp/voice-1.ogg"):
    from gateway.platforms.base import MessageEvent, MessageType

    return MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        message_id=message_id,
        media_urls=[path],
        media_types=["audio/ogg"],
    )


def _echoes(adapter):
    return [m for m in adapter.sent if m.startswith("🎙️")]


@pytest.mark.asyncio
async def test_one_voice_message_echoes_once_across_distinct_event_objects():
    """Interrupt path + drain path see different objects for the same message."""
    runner, adapter, source = _make_runner(), _EchoAdapter(), _source()
    _wire(runner, adapter)

    interrupt_copy = _voice_event(source)
    drain_copy = _voice_event(source)  # same platform message, new object

    await runner._transcribe_and_echo_pending_voice(
        interrupt_copy, adapter, source, "", log_context="Voice-busy-interrupt"
    )
    await runner._transcribe_and_echo_pending_voice(
        drain_copy, adapter, source, "", log_context="Voice-drain"
    )

    assert _echoes(adapter) == ['🎙️ "hello once"']


@pytest.mark.asyncio
async def test_prepare_inbound_does_not_double_echo_after_interrupt_echo():
    """The preprocessing path must not re-post an already-echoed transcript."""
    runner, adapter, source = _make_runner(), _EchoAdapter(), _source()
    _wire(runner, adapter)

    event = _voice_event(source)

    await runner._transcribe_and_echo_pending_voice(
        event, adapter, source, "", log_context="Voice-busy-interrupt"
    )
    # The drain re-prepares an equivalent event for the next user turn.
    await runner._prepare_inbound_message_text(
        event=_voice_event(source), source=source, history=[], session_key="sk"
    )

    assert _echoes(adapter) == ['🎙️ "hello once"']


@pytest.mark.asyncio
async def test_prepare_inbound_still_echoes_a_fresh_voice_message():
    """Dedupe must not silence the normal (non-interrupt) voice path."""
    runner, adapter, source = _make_runner(), _EchoAdapter(), _source()
    _wire(runner, adapter)

    await runner._prepare_inbound_message_text(
        event=_voice_event(source), source=source, history=[], session_key="sk"
    )

    assert _echoes(adapter) == ['🎙️ "hello once"']


@pytest.mark.asyncio
async def test_distinct_voice_messages_each_echo():
    """Dedupe is per-message, not global — two voice notes get two echoes."""
    runner, adapter, source = _make_runner(), _EchoAdapter(), _source()
    _wire(runner, adapter)

    first = _voice_event(source, message_id="MSG-1", path="/tmp/voice-1.ogg")
    second = _voice_event(source, message_id="MSG-2", path="/tmp/voice-2.ogg")

    await runner._transcribe_and_echo_pending_voice(
        first, adapter, source, "", log_context="Voice-drain"
    )
    await runner._transcribe_and_echo_pending_voice(
        second, adapter, source, "", log_context="Voice-drain"
    )

    assert len(_echoes(adapter)) == 2


@pytest.mark.asyncio
async def test_dedupe_falls_back_to_audio_path_without_message_id():
    """Platforms that omit message_id still dedupe on the downloaded audio."""
    runner, adapter, source = _make_runner(), _EchoAdapter(), _source()
    _wire(runner, adapter)

    first = _voice_event(source, message_id=None, path="/tmp/voice-9.ogg")
    second = _voice_event(source, message_id=None, path="/tmp/voice-9.ogg")

    await runner._transcribe_and_echo_pending_voice(
        first, adapter, source, "", log_context="Voice-busy-interrupt"
    )
    await runner._transcribe_and_echo_pending_voice(
        second, adapter, source, "", log_context="Voice-drain"
    )

    assert _echoes(adapter) == ['🎙️ "hello once"']


@pytest.mark.asyncio
async def test_echo_disabled_sends_nothing():
    """Quiet-STT users keep getting no transcript echoes."""
    runner, adapter, source = _make_runner(), _EchoAdapter(), _source()
    _wire(runner, adapter)
    runner._should_echo_stt_transcripts = lambda: False

    await runner._prepare_inbound_message_text(
        event=_voice_event(source), source=source, history=[], session_key="sk"
    )

    assert _echoes(adapter) == []


@pytest.mark.asyncio
async def test_echo_key_cache_is_bounded():
    """The dedupe LRU must not grow without bound on a long-lived gateway."""
    runner, adapter, source = _make_runner(), _EchoAdapter(), _source()
    _wire(runner, adapter)

    cap = runner._STT_ECHO_KEY_CACHE_MAX
    for i in range(cap + 25):
        await runner._transcribe_and_echo_pending_voice(
            _voice_event(source, message_id=f"MSG-{i}", path=f"/tmp/v{i}.ogg"),
            adapter,
            source,
            "",
            log_context="Voice-drain",
        )

    assert len(runner._stt_echo_sent_keys) <= cap
