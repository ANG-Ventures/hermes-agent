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
from contextlib import suppress
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


async def _cancel_once_started(task, started, runner=None, session_id="sess-old"):
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()  # what cancel_session_processing does on /stop
    with pytest.raises(asyncio.CancelledError):
        await task
    if runner is not None:  # what the session's next turn does before load_transcript
        await runner._await_voice_persist_barrier(session_id)


def _stt_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt")]


@pytest.mark.asyncio
async def test_stop_mid_stt_persists_note_and_continue_hears_it(caplog):
    runner = _runner()
    started = asyncio.Event()
    stt = _slow_stt(started, {"/cache/audio/long.ogg": "my four questions"})
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(_STT, side_effect=stt):
        task = _start_turn(runner, _voice())
        await _cancel_once_started(task, started, runner)

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
        await _cancel_once_started(task, started, runner)
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
        await runner._await_voice_persist_barrier("sess-old")
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
        await _cancel_once_started(task, started, runner)
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
            await asyncio.wait_for(_cancel_once_started(task, started, runner), 5)
    finally:
        release.set()
    content = runner.session_store.rows["sess-old"][0]["content"]
    assert "could not be transcribed" in content and "long.ogg" in content
    assert any("timeout after stop" in line for line in _stt_lines(caplog))


@pytest.mark.asyncio
async def test_second_cancel_during_post_stop_wait_still_persists():
    """Prism 071af0f232bd: /stop twice (or /stop then /reset) must not abandon the note."""
    runner = _runner()
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "keep me"}, delay=0.5)):
        task = _start_turn(runner, _voice())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.05)  # handler is now waiting on the persist task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await runner._await_voice_persist_barrier("sess-old")
    assert '"keep me"' in runner.session_store.rows["sess-old"][0]["content"]


@pytest.mark.asyncio
async def test_next_turn_waits_for_pending_note_before_loading_history():
    """Prism 5acffc356efe: the adapter stops waiting for a cancelled task after 5 s, so the next
    turn can start while STT is still running. It must wait for the note before reading history."""
    runner = _runner()
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "first"}, delay=0.5)):
        task = _start_turn(runner, _voice())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()  # the handler is gone; only the persist task remains
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "sess-old" not in runner.session_store.rows  # STT still running
        await runner._await_voice_persist_barrier("sess-old")  # what the next turn does before load_transcript
        assert '"first"' in runner.session_store.rows["sess-old"][0]["content"]
    await runner._await_voice_persist_barrier("sess-old")  # nothing pending: returns at once


@pytest.mark.asyncio
async def test_cancelled_handler_exits_without_waiting_for_stt():
    """Apollo round 2 (A): ``cancel_session_processing`` waits 5 s for the cancelled task. A long
    note's STT outlives that, so the handler must re-raise at once and leave STT + persist to the
    runner-owned task, not hold /stop's command path until transcription ends."""
    runner = _runner()
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "late words"}, delay=1.5)):
        task = _start_turn(runner, _voice())
        await asyncio.wait_for(started.wait(), 5)
        t0 = time.monotonic()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)  # cancel_session_processing's wait
        exit_s = time.monotonic() - t0
        assert "sess-old" not in runner.session_store.rows  # STT still running in the background
        await runner._await_voice_persist_barrier("sess-old")
    assert exit_s < 0.5, exit_s
    assert '"late words"' in runner.session_store.rows["sess-old"][0]["content"]


@pytest.mark.asyncio
async def test_stop_during_queued_voice_drain_stt_persists_note():
    """Prism 2f704a13d404 (B): a voice note queued behind a running turn is popped off the queue by
    ``_run_agent_drain_pending`` and transcribed there. A /stop during that STT must persist it to
    the draining turn's session; the drain never returns, so no follow-up turn (model call) runs."""
    runner = _runner()
    started = asyncio.Event()
    queued = _voice()

    class _Pending:
        _pending_messages = {_KEY: queued}

        def get_pending_message(self, key):
            return self._pending_messages.pop(key, None)

    runner._overflow_queue = lambda key: []
    runner._draining = False
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "queued words"})):
        drain = asyncio.ensure_future(runner._run_agent_drain_pending(
            {"final_response": "done"}, _Pending(), _source(), _KEY, "sess-old",
        ))
        await _cancel_once_started(drain, started, runner)
    assert _KEY not in _Pending._pending_messages  # off the queue: the persisted row is all that's left
    rows = runner.session_store.rows["sess-old"]
    assert len(rows) == 1 and '"queued words"' in rows[0]["content"]


def test_post_stop_note_row_keeps_the_stop_marker():
    """Apollo round 2 (C): the note row lands after ``user_stopped.last_message_id``; it must not
    count as the user speaking after /stop, or a restart auto-resumes (and replies to) the turn."""
    from gateway.auto_resume import POST_STOP_VOICE_NOTE_KEY, user_stop_blocks_resume

    marker = 10
    note = {"id": 11, "role": "user", "content": 'voice: "x"', "display_metadata": {POST_STOP_VOICE_NOTE_KEY: True}}
    assert user_stop_blocks_resume(marker, [note]) is True
    assert user_stop_blocks_resume(marker, [note, {"id": 12, "role": "user", "content": "Continue"}]) is False


@pytest.mark.asyncio
async def test_persisted_note_row_carries_the_post_stop_flag():
    from gateway.auto_resume import POST_STOP_VOICE_NOTE_KEY

    runner = _runner()
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "w"})):
        await _cancel_once_started(_start_turn(runner, _voice()), started, runner)
    assert runner.session_store.rows["sess-old"][0]["display_metadata"] == {POST_STOP_VOICE_NOTE_KEY: True}


@pytest.mark.asyncio
async def test_queued_voice_drain_echo_keeps_thread_routing():
    """Apollo review (round 3): threading ``persist_session_id`` through the drain must not change
    the echo's send metadata. Adapters route a thread reply by ``metadata["thread_id"]``; a
    ``session_id`` key sends the transcript echo to the parent channel instead of the thread."""
    runner = _runner()
    runner._should_echo_stt_transcripts = lambda: True
    src = SessionSource(
        platform=Platform.DISCORD, chat_id="1", chat_type="thread", user_id="42", user_name="Ace", thread_id="t-9",
    )
    queued = _voice()
    queued.source = src
    sent = []

    class _Pending:
        _pending_messages = {_KEY: queued}

        def get_pending_message(self, key):
            return self._pending_messages.pop(key, None)

    async def _capture(adapter, source, transcripts, *, metadata=None, log_context="Transcript"):
        sent.append(metadata)

    runner._overflow_queue = lambda key: []
    runner._draining = False
    runner._echo_stt_transcripts = _capture
    with patch(_STT, return_value={"success": True, "transcript": "queued words"}):
        await runner._run_agent_drain_pending({"final_response": "done"}, _Pending(), src, _KEY, "sess-old")
    assert sent and sent[0] == {"thread_id": "t-9"}
