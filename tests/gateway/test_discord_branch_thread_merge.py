"""Tests for the Discord /branch → thread and /merge commands (gateway path).

Covers:
- /branch on Discord in a CHANNEL spawns a thread, binds the BRANCH session to
  the thread's key, and leaves the parent channel's key untouched.
- /branch on Discord INSIDE a thread spawns a SIBLING thread under the same
  parent channel.
- /branch falls back to the classic in-place switch when thread creation fails
  (adapter returns None) and for non-Discord platforms.
- /merge folds a summary into the PARENT session's transcript as one labeled
  user-role message, posts a note to the parent channel, and archives the thread.
- /merge no-op guards: non-Discord, not-a-thread, not-a-branch, empty branch,
  and summary-unavailable.
"""

import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_state import AsyncSessionDB, SessionDB


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def session_db(tmp_path):
    os.environ["HERMES_HOME"] = str(tmp_path / ".hermes")
    os.makedirs(tmp_path / ".hermes", exist_ok=True)
    db = SessionDB(db_path=tmp_path / ".hermes" / "test_sessions.db")
    yield db
    db.close()


def _entry(session_key, session_id, source):
    return SessionEntry(
        session_key=session_key,
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
    )


def _make_runner(session_db, adapter=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter} if adapter is not None else {}
    runner.config = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner._session_db = AsyncSessionDB(session_db)

    # Real-ish session store backed by a MagicMock we control per-test.
    runner.session_store = MagicMock()

    # Neutralize side-effect helpers we don't assert on.
    runner._clear_session_boundary_security_state = MagicMock()
    runner._evict_cached_agent = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._session_key_for_source = lambda src: build_session_key(src)
    # Owner guard + origin resolution — permissive by default; tests override.
    runner._resume_target_allowed = AsyncMock(return_value=True)
    runner._gateway_session_origin_for_id = lambda sid: None
    runner._resume_row_visible = AsyncMock(return_value=True)
    return runner


def _discord_source(chat_type="group", chat_id="parent_chan", thread_id=None,
                    parent_chat_id=None):
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id=chat_id,
        user_name="tester",
        chat_type=chat_type,
        thread_id=thread_id,
        parent_chat_id=parent_chat_id,
    )


def _event(text, source):
    return MessageEvent(text=text, source=source, message_id="m1")


def _seed_session(db, session_id, title="Work", parent=None, model_config=None):
    db.create_session(
        session_id=session_id,
        source="discord",
        model="anthropic/claude-sonnet-4.6",
        model_config=model_config,
        parent_session_id=parent,
    )
    if title:
        db.set_session_title(session_id, title)


# --------------------------------------------------------------------------- #
# /branch — Discord thread spawn
# --------------------------------------------------------------------------- #

class TestBranchDiscordThread:

    @pytest.mark.asyncio
    async def test_branch_in_channel_spawns_thread_and_binds_thread_key(self, session_db):
        adapter = MagicMock()
        adapter.create_handoff_thread = AsyncMock(return_value="thread999")
        adapter.send = AsyncMock()
        runner = _make_runner(session_db, adapter)

        source = _discord_source(chat_type="group", chat_id="parent_chan")
        parent_key = build_session_key(source)
        current = _entry(parent_key, "parent_sess", source)
        _seed_session(session_db, "parent_sess", title="Parent Work")

        runner.session_store.get_or_create_session.return_value = current
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
        ]
        # Whatever key switch_session is called with, return a valid entry.
        runner.session_store.switch_session.return_value = _entry(
            "any", "branch_sess", source
        )

        result = await runner._handle_branch_command(_event("/branch idea", source))

        # A thread was created under the PARENT channel.
        adapter.create_handoff_thread.assert_awaited_once()
        args = adapter.create_handoff_thread.await_args.args
        assert args[0] == "parent_chan"  # parent channel id

        # switch_session was called with the THREAD's key, never the parent's.
        switch_keys = [c.args[0] for c in runner.session_store.switch_session.call_args_list]
        assert len(switch_keys) == 1
        thread_key = switch_keys[0]
        assert "thread999" in thread_key
        assert thread_key != parent_key  # parent channel key untouched

        # Confirmation mentions the new thread.
        assert "thread999" in result or "<#thread999>" in result

        # The intro posted INTO the new thread links back to the PARENT
        # conversation (reciprocal to the parent channel's link into the thread).
        intro_calls = [c for c in adapter.send.await_args_list
                       if str(c.args[0]) == "thread999"]
        assert intro_calls, "expected an intro sent into the new thread"
        intro_text = intro_calls[0].args[1]
        assert "<#parent_chan>" in intro_text  # link back to the parent channel
        # Count = RAW transcript rows (2 = 1 user + 1 assistant), matching the
        # footer's message tally — NOT a user-only subset (would say "1").
        assert "2 message" in intro_text

    @pytest.mark.asyncio
    async def test_branch_stamps_branched_from(self, session_db):
        """The branch session row records its parent in ``_branched_from``."""
        adapter = MagicMock()
        adapter.create_handoff_thread = AsyncMock(return_value="thrX")
        adapter.send = AsyncMock()
        runner = _make_runner(session_db, adapter)

        source = _discord_source(chat_type="group", chat_id="parent_chan")
        _seed_session(session_db, "parent_sess", title="Parent Work")
        runner.session_store.get_or_create_session.return_value = _entry(
            build_session_key(source), "parent_sess", source
        )
        # Parent has 3 inherited messages at branch time.
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        runner.session_store.switch_session.return_value = _entry("any", "b", source)

        await runner._handle_branch_command(_event("/branch idea", source))

        # The new branch session row carries the _branched_from marker.
        import json as _json, sqlite3
        conn = sqlite3.connect(str(session_db.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT model_config FROM sessions WHERE parent_session_id = ?",
            ("parent_sess",),
        ).fetchall()
        conn.close()
        assert rows, "no branch child session row found"
        mc = _json.loads(rows[0]["model_config"] or "{}")
        assert mc.get("_branched_from") == "parent_sess"

    @pytest.mark.asyncio
    async def test_native_slash_branch_in_thread_spawns_sibling_e2e(self, session_db):
        """END-TO-END: a NATIVE Discord slash /branch inside a thread must build
        a source WITH parent_chat_id (via the real adapter _build_slash_event)
        and spawn a SIBLING thread under the parent channel — not fall back to a
        classic in-place branch. This is the exact path that regressed (#230):
        the native-slash source-builder dropped parent_chat_id.
        """
        import sys
        discord_mod = sys.modules.get("discord")
        # Build the REAL adapter source-builder path.
        from types import SimpleNamespace
        from plugins.platforms.discord import adapter as adapter_mod

        Adapter = getattr(adapter_mod, "DiscordPlatformAdapter", None) or getattr(adapter_mod, "DiscordAdapter")
        a = object.__new__(Adapter)
        a._get_effective_topic = lambda ch, is_thread=False: None
        a._resolve_channel_prompt = lambda cid, pid=None: None
        # capture what build_source produces, but return a real SessionSource
        captured = {}
        def _bs(**kw):
            captured.update(kw)
            return SessionSource(
                platform=Platform.DISCORD,
                chat_id=kw.get("chat_id"),
                chat_type=kw.get("chat_type", "group"),
                user_id=kw.get("user_id"),
                user_name=kw.get("user_name"),
                thread_id=kw.get("session_id"),
                parent_chat_id=kw.get("parent_chat_id"),
            )
        a.build_source = _bs

        # A thread interaction: patch discord.Thread/DMChannel so isinstance works.
        class StubThread: pass
        class StubDM: pass
        orig_t, orig_dm = discord_mod.Thread, discord_mod.DMChannel
        discord_mod.Thread, discord_mod.DMChannel = StubThread, StubDM
        adapter_mod.discord.Thread, adapter_mod.discord.DMChannel = StubThread, StubDM
        try:
            tc = StubThread()
            tc.id = 555
            tc.name = "existing-thread"
            tc.guild = SimpleNamespace(name="Daemonarchy")
            tc.parent = None
            tc.parent_id = 700  # hosting channel
            inter = SimpleNamespace(channel_id=555, channel=tc,
                                    user=SimpleNamespace(id=1, display_name="Ace"))
            event = a._build_slash_event(inter, "/branch")
        finally:
            discord_mod.Thread, discord_mod.DMChannel = orig_t, orig_dm
            adapter_mod.discord.Thread, adapter_mod.discord.DMChannel = orig_t, orig_dm

        # The native-slash source now carries parent_chat_id — the #230 fix.
        assert event.source.parent_chat_id == "700"
        assert event.source.chat_type == "thread"

        # Feed that real source into the branch handler; it must spawn a SIBLING
        # thread under the parent channel (700), not fall back to in-place.
        branch_adapter = MagicMock()
        branch_adapter.create_handoff_thread = AsyncMock(return_value="sibling888")
        branch_adapter.send = AsyncMock()
        runner = _make_runner(session_db, branch_adapter)
        _seed_session(session_db, "src_sess", title="In Thread")
        runner.session_store.get_or_create_session.return_value = _entry(
            build_session_key(event.source), "src_sess", event.source
        )
        runner.session_store.load_transcript.return_value = [{"role": "user", "content": "x"}]
        runner.session_store.switch_session.return_value = _entry("k", "b", event.source)

        result = await runner._handle_branch_command(event)

        branch_adapter.create_handoff_thread.assert_awaited_once()
        assert branch_adapter.create_handoff_thread.await_args.args[0] == "700"  # sibling under parent
        assert "sibling888" in result

    @pytest.mark.asyncio
    async def test_branch_in_thread_spawns_sibling_under_parent(self, session_db):
        adapter = MagicMock()
        adapter.create_handoff_thread = AsyncMock(return_value="sibling777")
        adapter.send = AsyncMock()
        runner = _make_runner(session_db, adapter)

        # We're inside an existing thread; parent_chat_id is the hosting channel.
        source = _discord_source(
            chat_type="thread", chat_id="existing_thread",
            thread_id="existing_thread", parent_chat_id="host_chan",
        )
        current = _entry(build_session_key(source), "cur_sess", source)
        _seed_session(session_db, "cur_sess", title="Thread Work")

        runner.session_store.get_or_create_session.return_value = current
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hi"},
        ]
        runner.session_store.switch_session.return_value = _entry("any", "b", source)

        await runner._handle_branch_command(_event("/branch", source))

        # Sibling thread is created under the PARENT channel, not the thread.
        args = adapter.create_handoff_thread.await_args.args
        assert args[0] == "host_chan"

    @pytest.mark.asyncio
    async def test_branch_falls_back_when_thread_creation_fails(self, session_db):
        adapter = MagicMock()
        adapter.create_handoff_thread = AsyncMock(return_value=None)  # creation failed
        adapter.send = AsyncMock()
        runner = _make_runner(session_db, adapter)

        source = _discord_source(chat_type="group", chat_id="parent_chan")
        parent_key = build_session_key(source)
        current = _entry(parent_key, "parent_sess", source)
        _seed_session(session_db, "parent_sess", title="Parent Work")

        runner.session_store.get_or_create_session.return_value = current
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hi"},
        ]
        runner.session_store.switch_session.return_value = _entry(
            parent_key, "branch_sess", source
        )

        result = await runner._handle_branch_command(_event("/branch", source))

        # Classic in-place: switch_session called with the CURRENT (parent) key.
        switch_keys = [c.args[0] for c in runner.session_store.switch_session.call_args_list]
        assert parent_key in switch_keys
        # Classic branch confirmation, not the thread one.
        assert "thread" not in result.lower()

    @pytest.mark.asyncio
    async def test_branch_non_discord_uses_classic_path(self, session_db):
        runner = _make_runner(session_db, adapter=None)  # no discord adapter

        source = SessionSource(
            platform=Platform.TELEGRAM, user_id="u", chat_id="c",
            user_name="t", chat_type="dm",
        )
        key = build_session_key(source)
        current = _entry(key, "parent_sess", source)
        _seed_session(session_db, "parent_sess", title="TG Work")

        runner.session_store.get_or_create_session.return_value = current
        runner.session_store.load_transcript.return_value = [
            {"role": "user", "content": "hi"},
        ]
        runner.session_store.switch_session.return_value = _entry(key, "branch_sess", source)

        result = await runner._handle_branch_command(_event("/branch", source))
        switch_keys = [c.args[0] for c in runner.session_store.switch_session.call_args_list]
        assert key in switch_keys
        assert "thread" not in result.lower()


# --------------------------------------------------------------------------- #
# /merge
# --------------------------------------------------------------------------- #
