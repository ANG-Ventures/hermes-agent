"""Discord channel metadata, inbound messages and wakes must share a route."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.fixture(autouse=True)
def channel_types(monkeypatch):
    # The gateway conftest deliberately stubs the Discord SDK.
    for name in ("TextChannel", "DMChannel", "Thread"):
        monkeypatch.setattr(discord, name, type(name, (), {}))


def adapter_for(channel):
    channel.name = "test"
    channel.guild = None
    channel.recipient = None
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client = MagicMock()
    adapter._client.get_channel.return_value = channel
    adapter._client.fetch_channel = AsyncMock(return_value=channel)
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [("TextChannel", "group"), ("DMChannel", "dm"), ("Thread", "thread")])
async def test_chat_info_matches_inbound_key(kind, expected):
    channel = MagicMock(spec=getattr(discord, kind))
    channel.id = 123
    channel.name = "test"
    adapter = adapter_for(channel)
    info = await adapter.get_chat_info("123")
    assert info["type"] == expected
    inbound = SessionSource(platform=Platform.DISCORD, chat_id="123", user_id="456", chat_type=expected)
    wake = SessionSource(platform=Platform.DISCORD, chat_id="123", user_id="456", chat_type=info["type"])
    assert build_session_key(wake) == build_session_key(inbound)


def test_legacy_channel_key_cannot_split_guild_session():
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", user_id="456", chat_type="channel")
    assert build_session_key(source) == "agent:main:discord:group:123:456"
    from gateway.routing_identity import effective_routing_lane, routing_key_carries_identity
    assert effective_routing_lane(platform="discord", chat_id="123", chat_type="channel") == ("discord", "123", "group", "")
    assert routing_key_carries_identity(build_session_key(source), platform="discord", chat_id="123", chat_type="channel", user_id="456")


@pytest.mark.asyncio
async def test_adapter_resolves_synthetic_type_from_channel(monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 123
    adapter = adapter_for(channel)
    handle = AsyncMock()
    monkeypatch.setattr(BasePlatformAdapter, "handle_message", handle)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="dm", user_id="456")
    await adapter.handle_message(MessageEvent(text="wake", source=source, internal=True))
    assert handle.await_args.args[0].source.chat_type == "group"


def test_startup_merge_keeps_newest_session_and_older_pin(tmp_path, caplog):
    import json
    now = datetime.now()
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="456")
    group = "agent:main:discord:group:123:456"
    channel = group.replace(":group:", ":channel:")
    pin = {"model": "pinned-model", "provider": "openrouter"}
    older = SessionEntry(session_key=group, session_id="older", created_at=now, updated_at=now, origin=source, model_override=pin)
    newer = SessionEntry(session_key=channel, session_id="newer", created_at=now, updated_at=now + timedelta(seconds=1), origin=source)
    (tmp_path / "sessions.json").write_text(json.dumps({group: older.to_dict(), channel: newer.to_dict()}))
    store = SessionStore(config=GatewayConfig(), sessions_dir=tmp_path)
    store._db = None
    with caplog.at_level("INFO"):
        store._ensure_loaded()
        # The shape-only alias is already folded at load (before any replay
        # can write); the adapter-driven migration finds nothing left to do.
        assert store.migrate_discord_session_keys({"123": "group"}) == 0
    assert store.entry_for(channel) is None
    assert store.entry_for(group).session_id == "newer"
    assert store.entry_for(group).model_override == pin
    assert sum("Redirecting legacy session route" in record.message for record in caplog.records) == 1
    assert sum("Merged Discord session" in record.message for record in caplog.records) == 0
    restored = SessionStore(config=GatewayConfig(), sessions_dir=tmp_path)
    restored._db = None
    restored._ensure_loaded()
    assert restored.entry_for(channel) is None
    assert restored.entry_for(group).model_override == pin


def test_migration_does_not_rekey_prospective_threads(tmp_path):
    store = SessionStore(config=GatewayConfig(), sessions_dir=tmp_path)
    store._db = None
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="group", user_id="456", prospective_thread_id="789")
    entry = store.get_or_create_session(source)
    key = entry.session_key
    assert ":thread:" in key
    assert store.migrate_discord_session_keys({"123": "group"}) == 0
    assert store.entry_for(key) is entry


def test_migration_newer_legacy_pin_beats_older_identity(tmp_path):
    store = SessionStore(config=GatewayConfig(), sessions_dir=tmp_path)
    store._db = None
    store._loaded = True
    now = datetime.now()
    key = "agent:main:discord:group:123:456"
    old = SessionEntry(session_key=key, session_id="old", created_at=now, updated_at=now, model_override_identity={"model": "old", "provider": "openrouter"})
    alias = key.replace(":group:", ":channel:")
    new = SessionEntry(session_key=alias, session_id="new", created_at=now, updated_at=now + timedelta(seconds=1), model_override={"model": "new", "provider": "openrouter"})
    store._entries = {key: old, alias: new}
    store.migrate_discord_session_keys({"123": "group"})
    assert store.lookup_persisted_route_identity(key).identity["model"] == "new"
