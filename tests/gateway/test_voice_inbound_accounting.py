"""Voice inbound accounting: voice notes must be distinguishable in gateway logs.

Before: a Discord voice note logged ``msg='(The user sent a message with no
text content)'`` and a Telegram one ``msg=''`` — indistinguishable from an
empty message for ace-inbound-watch and the journals, and nothing recorded
whether STT replaced the placeholder.
"""

import logging
import wave
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig
from gateway.platforms.base import MessageEvent, MessageType


def _wav(path, seconds=12, rate=8000):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * rate * seconds)
    return str(path)


async def _enrich(runner, text, paths, chat):
    from gateway.run import _STT_LOG_CHAT

    token = _STT_LOG_CHAT.set(chat)
    try:
        return await runner._enrich_message_with_transcription(text, paths)
    finally:
        _STT_LOG_CHAT.reset(token)


def _runner():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["", "(The user sent a message with no text content)"],
    ids=["telegram-empty", "discord-placeholder"],
)
async def test_voice_event_logs_as_voice_with_duration(tmp_path, text):
    from gateway.run import _inbound_log_preview

    event = MessageEvent(
        text=text,
        message_type=MessageType.VOICE,
        media_urls=[_wav(tmp_path / "v.wav")],
        media_types=["audio/wav"],
    )
    assert await _inbound_log_preview(event) == "[voice 12s]"


@pytest.mark.asyncio
async def test_voice_event_keeps_real_caption(tmp_path):
    from gateway.run import _inbound_log_preview

    event = MessageEvent(
        text="see this",
        message_type=MessageType.VOICE,
        media_urls=[_wav(tmp_path / "v.wav", seconds=3)],
        media_types=["audio/wav"],
    )
    assert await _inbound_log_preview(event) == "[voice 3s] see this"


@pytest.mark.asyncio
async def test_voice_event_unprobeable_duration(tmp_path):
    from gateway.run import _inbound_log_preview

    bogus = tmp_path / "v.bin"
    bogus.write_bytes(b"not audio")
    event = MessageEvent(text="", message_type=MessageType.VOICE, media_urls=[str(bogus)])
    assert await _inbound_log_preview(event) == "[voice ?s]"


@pytest.mark.asyncio
async def test_text_and_audio_file_events_are_not_voice(tmp_path):
    from gateway.run import _inbound_log_preview

    assert await _inbound_log_preview(MessageEvent(text="hi\nthere")) == "hi there"
    # A music/audio FILE is not a voice note and never enters STT.
    audio_file = MessageEvent(
        text="",
        message_type=MessageType.AUDIO,
        media_urls=[_wav(tmp_path / "song.wav")],
        media_types=["audio/wav"],
    )
    assert await _inbound_log_preview(audio_file) == ""


@pytest.mark.asyncio
async def test_stt_success_logs_chat_chars_and_head(caplog):
    runner = _runner()
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "we need to account for voice"},
    ):
        text, transcripts = await _enrich(
            runner, "(The user sent a message with no text content)", ["/tmp/v.ogg"], "42",
        )
    assert transcripts == ["we need to account for voice"]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt")]
    assert len(lines) == 1
    assert lines[0].startswith("stt: chat=42 transcribed 28 chars in ")
    assert "'we need to account for voice'" in lines[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result, why",
    [
        ({"success": False, "error": "provider 503"}, "provider 503"),
        ({"success": True, "transcript": "   "}, "empty transcript"),
    ],
    ids=["provider-error", "empty-transcript"],
)
async def test_stt_failure_logs_failed_line(caplog, result, why):
    runner = _runner()
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(
        "tools.transcription_tools.transcribe_audio", return_value=result,
    ), patch(
        "tools.transcription_tools.transcribe_audio_local_fallback",
        return_value={"success": False, "error": "no local stt"},
    ):
        _, transcripts = await _enrich(runner, "", ["/tmp/v.ogg"], "42")
    assert transcripts == []
    failed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt FAILED")]
    assert len(failed) == 1
    assert failed[0].startswith("stt FAILED: chat=42 after ")
    assert why in failed[0]


@pytest.mark.asyncio
async def test_stt_exception_and_disabled_log_failed(caplog):
    runner = _runner()
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(
        "tools.transcription_tools.transcribe_audio", side_effect=RuntimeError("boom"),
    ):
        await _enrich(runner, "", ["/tmp/v.ogg"], "7")
    runner.config = GatewayConfig(stt_enabled=False)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        await _enrich(runner, "", ["/tmp/v.ogg"], "7")
    failed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt FAILED")]
    assert len(failed) == 2
    assert "RuntimeError: boom" in failed[0]
    assert "stt disabled in config" in failed[1]


@pytest.mark.asyncio
async def test_pending_voice_event_passes_its_chat(caplog):
    """The interrupt/drain path keys the stt line on the event's own chat."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    runner = _runner()
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        media_urls=["/tmp/v.ogg"],
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="99", chat_type="dm"),
    )
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hello"},
    ):
        await runner._transcribe_pending_audio_event_once(event)
    assert any(r.getMessage().startswith("stt: chat=99 transcribed 5 chars")
               for r in caplog.records)
