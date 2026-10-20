"""A voice note whose turn is cancelled (``/stop``, ``/new``) mid-STT must still be heard.

Incident 2026-10-19 (Discord thread 1561583142199103579): a 281 s voice note started a turn,
STT was in flight during inbound preprocessing, and ``/stop`` 2 s later cancelled the handler
task. ``CancelledError`` skipped every ``except Exception``: no ``stt`` log line, no user row,
and the next turn ("Continue") carried no trace of the note. It went unheard for 1.5 h.

Contract (Ace 2026-10-19 22:36): a cancel never abandons the transcription. It completes, the
note is persisted as the stopped turn's user row in the session the turn STARTED in, and no model
call is made for it. The next human row then follows it as user;user, which
``repair_message_sequence`` merges, so the agent hears the note with "Continue".
"""

import asyncio
import logging
import threading
import time
from unittest.mock import patch

import pytest

import gateway.run_inbound as run_inbound
from agent.agent_runtime_helpers import repair_message_sequence
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource

_KEY = "agent:main:discord:thread:1:1"
_STT = "tools.transcription_tools.transcribe_audio"
_LOCAL = "tools.transcription_tools.transcribe_audio_local_fallback"


class _Store:
    """SessionStore stand-in: records transcript appends; ``current`` is the key's live session."""

    def __init__(self):
        self.current = "sess-old"
        self.rows = {}

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.rows.setdefault(session_id, []).append(dict(message))


def _runner():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")}, stt_enabled=True,
    )
    runner.adapters = {}
    runner.session_store = _Store()
    runner._has_setup_skill = lambda: False
    runner._should_echo_stt_transcripts = lambda: False
    return runner


def _source():
    return SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="thread", user_id="42", user_name="Ace")


def _voice(paths=("/cache/audio/long.ogg",)):
    return MessageEvent(
        text="(The user sent a message with no text content)", message_type=MessageType.VOICE,
        source=_source(), media_urls=list(paths), media_types=["audio/ogg"] * len(paths), message_id="m-voice",
    )


def _start_turn(runner, event):
    """What _handle_message_with_agent does: the session id is captured when the turn starts."""
    session_id = runner.session_store.current
    return asyncio.ensure_future(runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[], session_key=_KEY, session_id=session_id,
    ))


def _slow_stt(started, transcripts, delay=0.3, fail=False):
    def stt(path, *a, **k):
        started.set()
        time.sleep(delay)
        if fail:
            return {"success": False, "error": "503"}
        return {"success": True, "transcript": transcripts[path]}
    return stt


async def _cancel_once_started(task, started):
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()  # what cancel_session_processing does on /stop
    with pytest.raises(asyncio.CancelledError):
        await task


def _stt_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt")]


@pytest.mark.asyncio
async def test_stop_mid_stt_persists_note_and_continue_hears_it(caplog):
    runner = _runner()
    started = asyncio.Event()
    stt = _slow_stt(started, {"/cache/audio/long.ogg": "my four questions"})
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(_STT, side_effect=stt):
        task = _start_turn(runner, _voice())
        await _cancel_once_started(task, started)

    rows = runner.session_store.rows["sess-old"]
    assert len(rows) == 1 and rows[0]["role"] == "user"
    assert '"my four questions"' in rows[0]["content"]
    assert rows[0]["message_id"] == "m-voice"
    lines = _stt_lines(caplog)
    assert any(line.startswith("stt ok (completed after /stop): chat=1") for line in lines)
    assert not any("stt FAILED" in line for line in lines)
    assert any("persisted to session sess-old" in line for line in lines)

    # "Continue" lands as the next user row; the provider-bound history carries the note with it.
    history = [*rows, {"role": "user", "content": "Continue"}]
    repair_message_sequence(None, history)
    assert len(history) == 1
    assert '"my four questions"' in history[0]["content"] and history[0]["content"].endswith("Continue")


@pytest.mark.asyncio
async def test_stop_during_second_clip_persists_both():
    runner = _runner()
    started = asyncio.Event()
    texts = {"/a.ogg": "first clip", "/b.ogg": "second clip"}

    def stt(path, *a, **k):
        if path == "/b.ogg":
            started.set()
            time.sleep(0.3)
        return {"success": True, "transcript": texts[path]}

    with patch(_STT, side_effect=stt):
        task = _start_turn(runner, _voice(("/a.ogg", "/b.ogg")))
        await _cancel_once_started(task, started)
    rows = runner.session_store.rows["sess-old"]
    assert len(rows) == 1
    assert '"first clip"' in rows[0]["content"] and '"second clip"' in rows[0]["content"]


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="r-1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {}


@pytest.mark.asyncio
async def test_new_mid_stt_lands_in_old_session_only():
    runner = _runner()
    store = runner.session_store
    started = asyncio.Event()
    adapter = _Adapter()

    async def reset_handler(event):
        store.current = "sess-new"  # /new rotates the session BEFORE the old task is cancelled
        return "fresh session"

    adapter._message_handler = reset_handler
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "old instructions"})):
        task = _start_turn(runner, _voice())
        adapter._session_tasks[_KEY] = task
        await asyncio.wait_for(started.wait(), 5)
        new_cmd = MessageEvent(text="/new", message_type=MessageType.TEXT, source=_source())
        await adapter._dispatch_active_session_command(new_cmd, _KEY, "new")  # real order: reply, then cancel
        with pytest.raises(asyncio.CancelledError):
            await task
    assert '"old instructions"' in store.rows["sess-old"][0]["content"]
    assert "sess-new" not in store.rows

    with patch(_STT, side_effect=AssertionError("no STT on a text turn")):
        text = await runner._prepare_inbound_message_text(
            event=MessageEvent(text="hello", message_type=MessageType.TEXT, source=_source()),
            source=_source(), history=[], session_key=_KEY, session_id=store.current,
        )
    assert "old instructions" not in text


@pytest.mark.asyncio
async def test_stt_failure_after_stop_persists_marker(caplog):
    runner = _runner()
    started = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(
        _STT, side_effect=_slow_stt(started, {}, fail=True),
    ), patch(_LOCAL, return_value={"success": False, "error": "no local"}):
        task = _start_turn(runner, _voice())
        await _cancel_once_started(task, started)
    content = runner.session_store.rows["sess-old"][0]["content"]
    assert "could not be transcribed" in content and "long.ogg" in content
    assert any(line.startswith("stt FAILED (after /stop): chat=1") for line in _stt_lines(caplog))


@pytest.mark.asyncio
async def test_stt_past_the_bound_persists_marker_and_exits(caplog, monkeypatch):
    monkeypatch.setattr(run_inbound, "STT_AFTER_STOP_TIMEOUT_S", 0.2)
    runner = _runner()
    started = asyncio.Event()
    release = threading.Event()

    def hung_stt(path, *a, **k):
        started.set()
        release.wait(10)
        return {"success": True, "transcript": "too late"}

    try:
        with caplog.at_level(logging.INFO, logger="gateway.run"), patch(_STT, side_effect=hung_stt):
            task = _start_turn(runner, _voice())
            await asyncio.wait_for(_cancel_once_started(task, started), 5)
    finally:
        release.set()
    content = runner.session_store.rows["sess-old"][0]["content"]
    assert "could not be transcribed" in content and "long.ogg" in content
    assert any("timeout after stop" in line for line in _stt_lines(caplog))
