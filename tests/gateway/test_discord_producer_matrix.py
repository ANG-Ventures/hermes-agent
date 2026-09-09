"""Real producer paths converge before a session is opened (SDK/network faked)."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.fixture(params=[("TextChannel", "group"), ("DMChannel", "dm"), ("Thread", "thread")])
def lane(request, tmp_path, monkeypatch):
    for name in ("TextChannel", "DMChannel", "Thread"):
        monkeypatch.setattr(discord, name, type(name, (), {}))
    cls, kind = request.param
    channel = getattr(discord, cls)()
    channel.id, channel.name = 123, "test"
    channel.guild = channel.recipient = channel.parent = channel.parent_id = channel.topic = None
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={"require_mention": False, "auto_thread": False}))
    adapter._client = MagicMock()
    adapter._client.get_channel.return_value = channel
    adapter._client.fetch_channel = AsyncMock(return_value=channel)
    adapter._text_batch_delay_seconds = 0
    adapter.send = AsyncMock(return_value=SendResult(success=True))
    adapter._fetch_channel_context = AsyncMock(return_value="")
    adapter._discord_require_mention = lambda: False
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    runner.adapters = {Platform.DISCORD: adapter}
    runner.session_store._db = None
    adapter._session_store = runner.session_store
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type=kind,
                           user_id="456", thread_id="123" if kind == "thread" else None)
    human = runner.session_store.get_or_create_session(source)
    received = []

    async def capture(self, event):
        # Inspect the producer's envelope before the store can repair it, AND
        # prove the durable destination is the same session, not just same type.
        assert event.source.chat_type == kind
        assert build_session_key(event.source) == human.session_key
        entry = runner.session_store.get_or_create_session(event.source)
        assert entry.session_id == human.session_id
        received.append(event)

    monkeypatch.setattr(BasePlatformAdapter, "handle_message", capture)
    return runner, adapter, channel, source, human, received


@pytest.mark.asyncio
async def test_inbound_and_native_model_slash(lane):
    runner, adapter, channel, source, human, received = lane
    author = SimpleNamespace(id=456, name="user", display_name="user", bot=False)
    message = SimpleNamespace(channel=channel, content="hello", author=author, guild=None,
                              id=789, attachments=[], message_snapshots=[], mentions=[],
                              reference=None, created_at=datetime.now(), type=None)
    assert await adapter._handle_message(message)
    interaction = SimpleNamespace(channel=channel, channel_id=123, user=author)
    await adapter.handle_message(adapter._build_slash_event(interaction, "/model pinned"))
    assert len(received) == 2


@pytest.mark.asyncio
async def test_notify_wake_and_restart_resume(lane):
    from gateway.wake import deliver_wake
    runner, adapter, channel, source, human, received = lane
    wrong = replace(source, chat_type="channel", thread_id=None)
    await deliver_wake(adapter, text="wake", source=wrong)
    await runner._run_startup_resume_event(
        adapter, MessageEvent(text="", source=replace(source, chat_type="channel"), internal=True),
        human.session_key,
    )
    assert len(received) == 2


@pytest.mark.asyncio
async def test_delegate_completion(lane):
    runner, adapter, channel, source, human, received = lane
    result = await runner._inject_watch_notification("delegate finished", {
        "type": "async_delegation", "platform": "discord", "chat_id": "123",
        "chat_type": "channel", "user_id": "456",
    })
    assert result == "delivered"
    assert len(received) == 1


def test_store_and_runner_keys_ignore_wrong_type(lane):
    runner, adapter, channel, source, human, received = lane
    for wrong in ("channel", "dm", "group", "thread"):
        candidate = replace(source, chat_type=wrong, thread_id=None)
        assert runner._session_key_for_source(candidate) == human.session_key
        assert runner.session_store.get_or_create_session(candidate).session_id == human.session_id


@pytest.mark.asyncio
async def test_kanban_notify_wake_tick(lane, tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tests.gateway.test_kanban_wake_key_identity import _run_one_notifier_tick
    runner, adapter, channel, source, human, received = lane
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    conn = kb.connect()
    try:
        task = kb.create_task(conn, title="test", assignee="worker", session_id=human.session_key)
        kb.add_notify_sub(conn, task_id=task, platform="discord", chat_id="123",
                          chat_type="channel", user_id="456", delivery_mode="notify+wake")
        kb.complete_task(conn, task, summary="finished")
    finally:
        conn.close()
    runner._running = True
    runner._kanban_dispatcher_lock_handle = object()
    await _run_one_notifier_tick(monkeypatch, runner)
    assert len(received) == 1


def test_cron_seed_converges_to_inbound(lane, monkeypatch):
    from cron.scheduler import _seed_cron_channel_session, _seed_cron_thread_session
    import gateway.mirror
    runner, adapter, channel, source, human, received = lane
    mirror = MagicMock(return_value=True)
    monkeypatch.setattr(gateway.mirror, "mirror_to_session", mirror)
    if source.chat_type == "thread":
        _seed_cron_thread_session({"id": "test"}, adapter, "discord", "999", "123", "brief", is_dm=True)
    else:
        _seed_cron_channel_session({"id": "test"}, adapter, "discord", "123", "brief",
                                   user_id="456", is_dm=source.chat_type != "dm")
    assert mirror.called, "Cron seed was refused before it reached the chat's session"
    assert mirror.call_args.kwargs["session_id"] == human.session_id
    assert {entry.session_key for entry in runner.session_store.snapshot_entries()} == {human.session_key}
