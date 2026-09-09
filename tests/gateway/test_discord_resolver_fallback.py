"""The Discord session resolver always yields a key; only the store guard refuses.

Apollo review on fork #659: a resolver that raises on an unresolvable channel
turns every Discord path with a missing/mock channel into a hard error — "a fix
that destroys the feature it secures". When the channel object is absent it
must derive the type from the source's own fields, then the resolution cache,
then a canonicalized caller label, and never raise.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter(client=None):
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client = client
    return adapter


def _source(**kw):
    base = dict(platform=Platform.DISCORD, chat_id="123", chat_type="channel", user_id="456")
    base.update(kw)
    return SessionSource(**base)


def test_non_snowflake_id_does_not_crash():
    src = _source(chat_id="<AsyncMock name='mock().id'>")
    client = SimpleNamespace(get_channel=lambda _id: None)
    _adapter(client).canonicalize_session_source(src)
    assert src.chat_type in {"dm", "group", "thread"}


def test_missing_client_uses_source_fields_guild_means_group():
    src = _source(chat_type="channel", guild_id="999")
    _adapter(None).canonicalize_session_source(src)
    assert src.chat_type == "group"
    assert src.thread_id is None


def test_missing_client_uses_source_fields_thread_id_means_thread():
    src = _source(chat_type="channel", thread_id="321", parent_chat_id="123")
    _adapter(None).canonicalize_session_source(src)
    assert src.chat_type == "thread"
    assert src.chat_id == src.thread_id == "321"


def test_unresolved_channel_falls_back_to_cache():
    adapter = _adapter(SimpleNamespace(get_channel=lambda _id: None))
    adapter._session_chat_types = {"123": "group"}
    src = _source(chat_type="dm")
    adapter.canonicalize_session_source(src)
    assert src.chat_type == "group"


def test_legacy_channel_label_canonicalizes_to_group_without_evidence():
    src = _source(chat_type="channel")
    _adapter(None).canonicalize_session_source(src)
    assert src.chat_type == "group"


def test_no_evidence_at_all_defaults_to_dm():
    src = _source(chat_type="")
    _adapter(None).canonicalize_session_source(src)
    assert src.chat_type == "dm"


def test_channel_object_still_wins_over_source_label(monkeypatch):
    monkeypatch.setattr(discord, "DMChannel", type("DMChannel", (), {}))
    channel = discord.DMChannel()
    channel.id = 123
    adapter = _adapter(SimpleNamespace(get_channel=lambda _id: channel))
    src = _source(chat_type="channel", guild_id="999")
    adapter.canonicalize_session_source(src)
    assert src.chat_type == "dm"


@pytest.mark.asyncio
async def test_internal_event_with_unresolvable_channel_still_dispatches(monkeypatch):
    adapter = _adapter(SimpleNamespace(get_channel=lambda _id: None,
                                       fetch_channel=AsyncMock(return_value=None)))
    seen = []

    async def capture(self, event):
        seen.append(event.source.chat_type)

    from gateway.platforms.base import BasePlatformAdapter
    monkeypatch.setattr(BasePlatformAdapter, "handle_message", capture)
    await adapter.handle_message(MessageEvent(text="", source=_source(guild_id="999"), internal=True))
    assert seen == ["group"]
