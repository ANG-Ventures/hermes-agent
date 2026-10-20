"""A voice note whose turn is cancelled (``/stop``, ``/new``) mid-STT must still be heard.

Incident 2026-10-19 (Discord thread 1561583142199103579): a 281 s voice note started a turn,
STT was in flight during inbound preprocessing, and ``/stop`` 2 s later cancelled the handler
task. ``CancelledError`` skipped every ``except Exception``: no ``stt`` log line, no user row,
and the next turn ("Continue") carried no trace of the note. It went unheard for 1.5 h.

Contract (Ace 2026-10-19 22:36): a cancel never abandons the transcription. It completes, the
note is persisted as the stopped turn's user row in the session the turn STARTED in, and no model
call is made for it. The next human row then follows it as user;user, which
``repair_message_sequence`` merges, so the agent hears the note with "Continue".

Invariant (Apollo ruling on t_61b3a5a1, 2026-10-20): the note row exists BEFORE the cancelled
handler returns, carrying the could-not-be-transcribed marker and the cached audio path; the
detached task only upgrades that row to the transcript in place, keyed by message_id. Whatever
loses the upgrade (shutdown, timeout, /retry rewrite, compression reroute) leaves the marker.
Runs against a real SessionStore + SessionDB: a dict stand-in hides the store's routing.
"""

import asyncio
import logging
import sqlite3
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
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB

_STT = "tools.transcription_tools.transcribe_audio"
_LOCAL = "tools.transcription_tools.transcribe_audio_local_fallback"
_MARKER = "could not be transcribed"


def _source(profile=None):
    return SessionSource(
        platform=Platform.DISCORD, chat_id="1", chat_type="thread", user_id="42", user_name="Ace", profile=profile,
    )


def _runner(tmp_path, *, store=None, source=None):
    """A runner over a real store; ``runner.key`` / ``runner.sid`` are the chat's route and session."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")}, stt_enabled=True,
    )
    runner.adapters = {}
    if store is None:
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        store._db = SessionDB(db_path=tmp_path / "state.db")
    runner.session_store = store
    runner._has_setup_skill = lambda: False
    runner._should_echo_stt_transcripts = lambda: False
    entry = store.get_or_create_session(source or _source())
    runner.key, runner.sid = entry.session_key, entry.session_id
    return runner


def _rows(runner, session_id=None):
    """The live rows ``load_transcript`` would replay, unrepaired."""
    return runner.session_store.load_transcript_rows(session_id or runner.sid)


def _voice(paths=("/cache/audio/long.ogg",)):
    return MessageEvent(
        text="(The user sent a message with no text content)", message_type=MessageType.VOICE,
        source=_source(), media_urls=list(paths), media_types=["audio/ogg"] * len(paths), message_id="m-voice",
    )


def _start_turn(runner, event):
    """What _handle_message_with_agent does: the session id is captured when the turn starts."""
    return asyncio.ensure_future(runner._prepare_inbound_message_text(
        event=event, source=event.source, history=[], session_key=runner.key, session_id=runner.sid,
    ))


def _slow_stt(started, transcripts, delay=0.3, fail=False):
    def stt(path, *a, **k):
        started.set()
        time.sleep(delay)
        if fail:
            return {"success": False, "error": "503"}
        return {"success": True, "transcript": transcripts[path]}
    return stt


async def _cancel_once_started(task, started, runner=None, session_id=None):
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()  # what cancel_session_processing does on /stop
    with pytest.raises(asyncio.CancelledError):
        await task
    if runner is not None:  # what the session's next turn does before load_transcript
        await runner._await_voice_persist_barrier(session_id or runner.sid)


def _stt_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("stt")]


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


def _pending_adapter(key, event):
    class _Pending:
        _pending_messages = {key: event}

        def get_pending_message(self, k):
            return self._pending_messages.pop(k, None)
    return _Pending()


@pytest.mark.asyncio
async def test_stop_mid_stt_persists_note_and_continue_hears_it(tmp_path, caplog):
    runner = _runner(tmp_path)
    started = asyncio.Event()
    stt = _slow_stt(started, {"/cache/audio/long.ogg": "my four questions"})
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(_STT, side_effect=stt):
        task = _start_turn(runner, _voice())
        await _cancel_once_started(task, started, runner)

    rows = _rows(runner)
    assert len(rows) == 1 and rows[0]["role"] == "user"
    assert '"my four questions"' in rows[0]["content"] and _MARKER not in rows[0]["content"]
    assert rows[0]["message_id"] == "m-voice"
    lines = _stt_lines(caplog)
    assert any(line.startswith("stt ok (completed after /stop): chat=1") for line in lines)
    assert not any("stt FAILED" in line for line in lines)
    assert any(f"persisted to session {runner.sid}" in line for line in lines)

    # "Continue" lands as the next user row; the provider-bound history carries the note with it.
    history = [*rows, {"role": "user", "content": "Continue"}]
    repair_message_sequence(None, history)
    assert len(history) == 1
    assert '"my four questions"' in history[0]["content"] and history[0]["content"].endswith("Continue")


@pytest.mark.asyncio
async def test_stop_during_second_clip_persists_both(tmp_path):
    runner = _runner(tmp_path)
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
    rows = _rows(runner)
    assert len(rows) == 1
    assert '"first clip"' in rows[0]["content"] and '"second clip"' in rows[0]["content"]


@pytest.mark.asyncio
async def test_new_mid_stt_lands_in_old_session_only(tmp_path):
    runner = _runner(tmp_path)
    store = runner.session_store
    old_sid = runner.sid
    started = asyncio.Event()
    adapter = _Adapter()

    async def reset_handler(event):
        store.reset_session(runner.key)  # /new rotates the session BEFORE the old task is cancelled
        return "fresh session"

    adapter._message_handler = reset_handler
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "old instructions"})):
        task = _start_turn(runner, _voice())
        adapter._session_tasks[runner.key] = task
        await asyncio.wait_for(started.wait(), 5)
        new_cmd = MessageEvent(text="/new", message_type=MessageType.TEXT, source=_source())
        await adapter._dispatch_active_session_command(new_cmd, runner.key, "new")  # real order: reply, then cancel
        with pytest.raises(asyncio.CancelledError):
            await task
        await runner._await_voice_persist_barrier(old_sid)
    new_sid = store.get_or_create_session(_source()).session_id
    assert new_sid != old_sid
    assert '"old instructions"' in _rows(runner, old_sid)[0]["content"]
    assert _rows(runner, new_sid) == []

    with patch(_STT, side_effect=AssertionError("no STT on a text turn")):
        text = await runner._prepare_inbound_message_text(
            event=MessageEvent(text="hello", message_type=MessageType.TEXT, source=_source()),
            source=_source(), history=[], session_key=runner.key, session_id=new_sid,
        )
    assert "old instructions" not in text


@pytest.mark.asyncio
async def test_stt_failure_after_stop_persists_marker(tmp_path, caplog):
    runner = _runner(tmp_path)
    started = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="gateway.run"), patch(
        _STT, side_effect=_slow_stt(started, {}, fail=True),
    ), patch(_LOCAL, return_value={"success": False, "error": "no local"}):
        task = _start_turn(runner, _voice())
        await _cancel_once_started(task, started, runner)
    content = _rows(runner)[0]["content"]
    assert _MARKER in content and "long.ogg" in content
    assert any(line.startswith("stt FAILED (after /stop): chat=1") for line in _stt_lines(caplog))


@pytest.mark.asyncio
async def test_stt_past_the_bound_persists_marker_and_exits(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(run_inbound, "STT_AFTER_STOP_TIMEOUT_S", 0.2)
    runner = _runner(tmp_path)
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
    rows = _rows(runner)
    assert len(rows) == 1 and _MARKER in rows[0]["content"] and "long.ogg" in rows[0]["content"]
    assert any("timeout after stop" in line for line in _stt_lines(caplog))


@pytest.mark.asyncio
async def test_second_cancel_during_post_stop_wait_still_persists(tmp_path):
    """Prism 071af0f232bd (round 2): /stop twice (or /stop then /reset) must not abandon the note."""
    runner = _runner(tmp_path)
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "keep me"}, delay=0.5)):
        task = _start_turn(runner, _voice())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await runner._await_voice_persist_barrier(runner.sid)
    assert '"keep me"' in _rows(runner)[0]["content"]


@pytest.mark.asyncio
async def test_next_turn_waits_for_the_transcript_before_loading_history(tmp_path):
    """The marker row is there at once; the next turn's bounded barrier wait lets it read the words."""
    runner = _runner(tmp_path)
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "first"}, delay=0.5)):
        task = _start_turn(runner, _voice())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _MARKER in _rows(runner)[0]["content"]  # STT still running: the marker holds the place
        await runner._await_voice_persist_barrier(runner.sid)  # what the next turn does before load_transcript
        rows = _rows(runner)
        assert len(rows) == 1 and '"first"' in rows[0]["content"] and _MARKER not in rows[0]["content"]
    await runner._await_voice_persist_barrier(runner.sid)  # nothing pending: returns at once


@pytest.mark.asyncio
async def test_cancelled_handler_exits_fast_with_the_note_row_already_written(tmp_path):
    """Apollo round 2 (A) + the t_61b3a5a1 invariant: the handler re-raises without waiting for STT,
    and by the time it has returned, the note row (marker + audio path) is already in the store."""
    runner = _runner(tmp_path)
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "late words"}, delay=1.5)):
        task = _start_turn(runner, _voice())
        await asyncio.wait_for(started.wait(), 5)
        t0 = time.monotonic()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)  # cancel_session_processing's wait
        exit_s = time.monotonic() - t0
        at_exit = _rows(runner)
        await runner._await_voice_persist_barrier(runner.sid)
    assert exit_s < 0.5, exit_s
    assert len(at_exit) == 1 and _MARKER in at_exit[0]["content"] and "long.ogg" in at_exit[0]["content"]
    assert at_exit[0]["message_id"] == "m-voice"
    assert '"late words"' in _rows(runner)[0]["content"]


@pytest.mark.asyncio
async def test_stop_during_queued_voice_drain_stt_persists_note(tmp_path):
    """Prism 2f704a13d404 (B): a voice note popped off the queue by ``_run_agent_drain_pending`` and
    /stopped during its STT is persisted to the draining turn's session; no follow-up turn runs."""
    runner = _runner(tmp_path)
    started = asyncio.Event()
    pending = _pending_adapter(runner.key, _voice())
    runner._overflow_queue = lambda key: []
    runner._draining = False
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "queued words"})):
        drain = asyncio.ensure_future(runner._run_agent_drain_pending(
            {"final_response": "done"}, pending, _source(), runner.key, runner.sid,
        ))
        await _cancel_once_started(drain, started, runner)
    assert runner.key not in pending._pending_messages
    rows = _rows(runner)
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
async def test_persisted_note_row_carries_the_post_stop_flag(tmp_path):
    from gateway.auto_resume import POST_STOP_VOICE_NOTE_KEY

    runner = _runner(tmp_path)
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "w"})):
        await _cancel_once_started(_start_turn(runner, _voice()), started, runner)
    assert _rows(runner)[0]["display_metadata"] == {POST_STOP_VOICE_NOTE_KEY: True}


@pytest.mark.asyncio
async def test_queued_voice_drain_echo_keeps_thread_routing(tmp_path):
    """Apollo review (round 3): threading ``persist_session_id`` through the drain must not change
    the echo's send metadata. Adapters route a thread reply by ``metadata["session_id"]``."""
    runner = _runner(tmp_path)
    runner._should_echo_stt_transcripts = lambda: True
    src = SessionSource(
        platform=Platform.DISCORD, chat_id="1", chat_type="thread", user_id="42", user_name="Ace", thread_id="t-9",
    )
    queued = _voice()
    queued.source = src
    sent = []

    async def _capture(adapter, source, transcripts, *, metadata=None, log_context="Transcript"):
        sent.append(metadata)

    runner._overflow_queue = lambda key: []
    runner._draining = False
    runner._echo_stt_transcripts = _capture
    with patch(_STT, return_value={"success": True, "transcript": "queued words"}):
        await runner._run_agent_drain_pending(
            {"final_response": "done"}, _pending_adapter(runner.key, queued), src, runner.key, runner.sid,
        )
    assert sent and sent[0] == {"thread_id": "t-9"}


# ---- t_61b3a5a1: the deferred Prism keys, each red on the #2145 merge commit (4e83a75d2f) ----


async def _cancel_and_leave_stt_running(runner, event, started):
    task = _start_turn(runner, event)
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_shutdown_with_stt_pending_leaves_the_marker_row(tmp_path):
    """Prism 2ddd4be1bb1a / 5acffc356efe: shutdown cancels the detached task and closes the DB
    while STT still runs. The note must already be on disk with its audio path."""
    runner = _runner(tmp_path)
    started, release = asyncio.Event(), threading.Event()

    def hung_stt(path, *a, **k):
        started.set()
        release.wait(10)
        return {"success": True, "transcript": "never written"}

    try:
        with patch(_STT, side_effect=hung_stt):
            await _cancel_and_leave_stt_running(runner, _voice(), started)
            for tasks in list(runner._voice_persist_barriers.values()):  # event-loop teardown
                for t in list(tasks):
                    t.cancel()
            await asyncio.sleep(0)
    finally:
        release.set()
    db_path = tmp_path / "state.db"
    runner.session_store._db.close()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT content, platform_message_id FROM messages WHERE session_id = ? AND role = 'user'", (runner.sid,),
        ).fetchall()
    assert len(rows) == 1 and _MARKER in rows[0][0] and "long.ogg" in rows[0][0] and rows[0][1] == "m-voice"


@pytest.mark.asyncio
async def test_drain_stop_after_compression_lands_where_the_next_turn_reads(tmp_path):
    """Prism cd320fa3c079: the turn compressed A -> B but still holds A. A /stop during the queued
    voice note's drain STT must leave the note in B, and the next turn's barrier (keyed by B) must
    wait for its transcript."""
    runner = _runner(tmp_path)
    db = runner.session_store._db
    parent = runner.sid
    db.end_session(parent, "compression")
    db.create_session("child-b", source="discord", parent_session_id=parent)
    db.replace_messages("child-b", [{"role": "assistant", "content": "summary"}])
    started = asyncio.Event()
    runner._overflow_queue = lambda key: []
    runner._draining = False
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "after compression"}, delay=0.5)):
        drain = asyncio.ensure_future(runner._run_agent_drain_pending(
            {"final_response": "done"}, _pending_adapter(runner.key, _voice()), _source(), runner.key, parent,
        ))
        await asyncio.wait_for(started.wait(), 5)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain
        next_sid = runner.session_store.get_or_create_session(_source()).session_id
        await runner._await_voice_persist_barrier(next_sid)  # the next turn, before load_transcript
        history = runner.session_store.load_transcript_rows(next_sid)
    assert next_sid == "child-b"
    assert [m["content"] for m in history][-1].endswith('"after compression"')


@pytest.mark.asyncio
async def test_retry_rewrite_before_the_upgrade_keeps_the_note_first(tmp_path):
    """Prism 2aaade0fa3b8 (/retry race): /retry snapshots history and rewrites it before STT ends.
    The note must already be in that snapshot, ahead of the retried exchange, not appended after it."""
    runner = _runner(tmp_path)
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "the note"}, delay=0.5)):
        await _cancel_and_leave_stt_running(runner, _voice(), started)
        snapshot = runner.session_store.load_transcript_rows(runner.sid)
        runner.session_store.rewrite_transcript(runner.sid, [
            *snapshot, {"role": "user", "content": "retried question"}, {"role": "assistant", "content": "answer"},
        ], active_only=True)
        await runner._await_voice_persist_barrier(runner.sid)
    contents = [m["content"] for m in _rows(runner)]
    note = next(i for i, c in enumerate(contents) if "the note" in c or _MARKER in c)
    assert note < contents.index("retried question")
    assert len([c for c in contents if "the note" in c or _MARKER in c]) == 1


@pytest.mark.asyncio
async def test_upgrade_after_the_barrier_timed_out_keeps_the_note_before_the_newer_exchange(tmp_path):
    """Prism 898e91f5e44e: the next turn gave up waiting and persisted its exchange; STT then ends.
    The older note stays where it was said, before that exchange."""
    runner = _runner(tmp_path)
    started = asyncio.Event()
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "older note"}, delay=0.5)):
        await _cancel_and_leave_stt_running(runner, _voice(), started)
        for row in ({"role": "user", "content": "newer message"}, {"role": "assistant", "content": "newer answer"}):
            runner.session_store.append_to_transcript(runner.sid, row)
        await runner._await_voice_persist_barrier(runner.sid)
    contents = [m["content"] for m in _rows(runner)]
    assert '"older note"' in contents[0] and contents[1:] == ["newer message", "newer answer"]


@pytest.mark.asyncio
async def test_multi_clip_timeout_keeps_the_finished_clip(tmp_path, monkeypatch):
    """Prism 071af0f232bd: clip a transcribed, clip b hangs past the bound. The row keeps a's words
    and marks only b."""
    monkeypatch.setattr(run_inbound, "STT_AFTER_STOP_TIMEOUT_S", 0.3)
    runner = _runner(tmp_path)
    started, release = asyncio.Event(), threading.Event()

    def stt(path, *a, **k):
        if path == "/b.ogg":
            started.set()
            release.wait(10)
            return {"success": True, "transcript": "too late"}
        return {"success": True, "transcript": "clip a words"}

    try:
        with patch(_STT, side_effect=stt):
            task = _start_turn(runner, _voice(("/a.ogg", "/b.ogg")))
            await asyncio.wait_for(_cancel_once_started(task, started, runner), 5)
    finally:
        release.set()
    content = _rows(runner)[0]["content"]
    assert '"clip a words"' in content
    assert content.count(_MARKER) == 1 and "b.ogg" in content and "a.ogg" not in content


@pytest.fixture
def multiplex_homes(tmp_path, monkeypatch):
    """Root home + a ``fitness`` profile home, HERMES_HOME on the root (see
    test_multiplex_session_db_profile_scope.py for why DEFAULT_DB_PATH is restored)."""
    import hermes_state

    root = tmp_path / "hermes"
    profile = root / "profiles" / "fitness"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    return root, profile


@pytest.mark.asyncio
async def test_new_mid_stt_on_a_named_profile_lands_in_that_profiles_db(multiplex_homes, tmp_path):
    """Prism 808fada25f72: multiplexed store, named profile, /new mid-STT. The departed session's
    note must land in the profile's state.db, not the launch home's."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root, profile = multiplex_homes
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=root / "sessions", config=GatewayConfig(multiplex_profiles=True))
    store._loaded = True
    token = set_hermes_home_override(str(profile))
    try:
        runner = _runner(tmp_path, store=store, source=_source(profile="fitness"))
    finally:
        reset_hermes_home_override(token)
    assert runner.key.startswith("agent:fitness:")
    started = asyncio.Event()
    event = _voice()
    event.source = _source(profile="fitness")
    with patch(_STT, side_effect=_slow_stt(started, {"/cache/audio/long.ogg": "profile words"})):
        task = _start_turn(runner, event)
        await asyncio.wait_for(started.wait(), 5)
        store.reset_session(runner.key)  # /new: the route moves to a fresh session first
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await runner._await_voice_persist_barrier(runner.sid)

    def _user_rows(db_path):
        if not db_path.exists():
            return []
        with sqlite3.connect(db_path) as conn:
            try:
                return conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'user'", (runner.sid,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
    store.close_all_db_handles()
    assert [r[0] for r in _user_rows(profile / "state.db")] == ['"profile words"']
    assert _user_rows(root / "state.db") == []


@pytest.mark.asyncio
async def test_stop_while_voice_b_waits_on_a_barrier_persists_both(tmp_path):
    """Prism 78cf4ddc6d42: voice A's transcript is still pending; voice B's turn waits on A's
    barrier (run_turn ``_await_voice_persist_barrier_for_turn``) and /stop lands there, before B
    reached STT. Both notes must end up in the session."""
    runner = _runner(tmp_path)
    started = asyncio.Event()
    texts = {"/a.ogg": "note a", "/b.ogg": "note b"}

    def stt(path, *a, **k):
        if path == "/a.ogg":
            started.set()
            time.sleep(0.5)
        return {"success": True, "transcript": texts[path]}

    voice_b = _voice(("/b.ogg",))
    voice_b.message_id = "m-voice-b"
    with patch(_STT, side_effect=stt):
        await _cancel_and_leave_stt_running(runner, _voice(("/a.ogg",)), started)
        turn_b = asyncio.ensure_future(runner._await_voice_persist_barrier_for_turn(voice_b, runner.sid, runner.key))
        await asyncio.sleep(0.05)
        turn_b.cancel()  # /stop while B waits on A
        with pytest.raises(asyncio.CancelledError):
            await turn_b
        for _ in range(2):  # B's own persist registers behind A's
            await runner._await_voice_persist_barrier(runner.sid)
    contents = [m["content"] for m in _rows(runner)]
    assert len(contents) == 2 and '"note a"' in contents[0] and '"note b"' in contents[1]
