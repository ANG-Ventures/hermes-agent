"""Discover bypasses of the canonical session-key builder, across all producers."""
import ast
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.routing_identity import SessionKeyConflict
from gateway.session import SessionSource, SessionStore

ROOT = Path(__file__).resolve().parents[2]

# Every ``chat_type="channel"`` literal a platform adapter is allowed to carry,
# keyed ``(path, qualified function)``. Each entry is a platform whose OWN
# canonical type for the chat is ``channel`` (Teams ``conversation_type``,
# Telegram ``ChatType.CHANNEL``, the HA event bus), produced by exactly ONE
# inbound producer — so no second spelling of the same chat can exist. The
# per-platform tests below pin that. A new literal anywhere else (a slash,
# wake, kanban, cron or restart producer re-labelling a chat) is the #659
# Discord bug class and must route through the adapter resolver contract
# (``canonicalize_session_source``) instead of being added here.
ALLOWED_CHANNEL_LITERALS = {
    ("plugins/platforms/teams/adapter.py", "TeamsAdapter._on_message"),
    ("plugins/platforms/telegram/adapter.py", "TelegramAdapter.get_chat_info"),
    ("plugins/platforms/telegram/adapter.py", "TelegramAdapter._build_message_event"),
    ("plugins/platforms/homeassistant/adapter.py", "HomeAssistantAdapter._handle_ha_event"),
}


def _channel_literal_sites(path: Path):
    """Yield ``(lineno, qualified function)`` for every chat_type="channel" literal."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{owner}.{child.name}" if owner else child.name
            if isinstance(child, ast.Call) and any(
                k.arg == "chat_type" and isinstance(k.value, ast.Constant)
                and k.value.value == "channel" for k in child.keywords
            ):
                yield child.lineno, owner
            elif (
                isinstance(child, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "chat_type" for t in child.targets)
                and isinstance(child.value, ast.Constant) and child.value.value == "channel"
            ):
                yield child.lineno, owner
            yield from walk(child, name)

    yield from walk(tree, "")


def test_no_producer_interpolates_literal_session_namespace():
    root = Path(__file__).resolve().parents[2]
    bypasses = []
    for directory in ("gateway", "cron", "tools", "plugins", "hermes_cli"):
        for path in (root / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr) or not node.values:
                    continue
                first = node.values[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("agent:main:"):
                    bypasses.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not bypasses, f"Session-key producers must use build_session_key: {bypasses}"


def test_discord_sources_do_not_hardcode_legacy_channel_type():
    root = Path(__file__).resolve().parents[2]
    bypasses = []
    for file in ("gateway/run.py", "gateway/kanban_watchers.py", "cron/scheduler.py",
                 "plugins/platforms/discord/adapter.py"):
        tree = ast.parse((root / file).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if any(k.arg == "chat_type" and isinstance(k.value, ast.Constant)
                   and k.value.value == "channel" for k in node.keywords):
                bypasses.append(f"{file}:{node.lineno}")
    assert not bypasses, f"Resolve channel types from platform metadata: {bypasses}"


def test_platform_adapters_channel_literals_are_allowlisted():
    """LINT: a chat_type="channel" literal in any platform adapter is either an
    allowlisted single-producer site or the #659 bug class. Fails naming file:line."""
    seen = set()
    offenders = []
    for path in sorted((ROOT / "plugins" / "platforms").glob("*/adapter.py")):
        rel = str(path.relative_to(ROOT))
        for lineno, owner in _channel_literal_sites(path):
            seen.add((rel, owner))
            if (rel, owner) not in ALLOWED_CHANNEL_LITERALS:
                offenders.append(f"{rel}:{lineno} ({owner})")
    assert not offenders, (
        "chat_type=\"channel\" literal outside the allowlisted single producers — "
        "route it through the adapter's canonicalize_session_source resolver "
        f"(see #659) instead of labelling the chat: {offenders}"
    )
    stale = ALLOWED_CHANNEL_LITERALS - seen
    assert not stale, f"Allowlist entries no longer present in source: {sorted(stale)}"


@pytest.mark.parametrize("platform, chat_id, user_id", [
    ("teams", "19:abc@thread.tacv2", "aad-1"),
    ("telegram", "-1003950368353", "-1003950368353"),
    ("homeassistant", "ha_events", "homeassistant"),
])
def test_second_spelling_for_same_chat_is_refused(tmp_path, platform, chat_id, user_id):
    """The #659 store guard is the runtime backstop for these platforms: a
    future producer labelling the same chat under another type is refused,
    not silently split into a second session."""
    store = SessionStore(tmp_path / "sessions", GatewayConfig(sessions_dir=tmp_path / "sessions"))
    store._db = None
    source = SessionSource(platform=Platform(platform), chat_id=chat_id,
                           chat_type="channel", user_id=user_id)
    entry = store.get_or_create_session(source)
    assert entry.session_key.split(":")[3] == "channel"
    with pytest.raises(SessionKeyConflict):
        store.get_or_create_session(replace(source, chat_type="group"))
    assert {e.session_key for e in store.snapshot_entries()} == {entry.session_key}


@pytest.mark.asyncio
async def test_homeassistant_single_producer_matches_chat_info():
    from plugins.platforms.homeassistant.adapter import HomeAssistantAdapter

    adapter = HomeAssistantAdapter(PlatformConfig(enabled=True, token="tok", extra={"watch_all": True, "cooldown_seconds": 0}))
    adapter.handle_message = AsyncMock()
    await adapter._handle_ha_event({"data": {
        "entity_id": "light.kitchen",
        "old_state": {"state": "off", "attributes": {}},
        "new_state": {"state": "on", "attributes": {"friendly_name": "Kitchen"}},
    }})
    source = adapter.handle_message.call_args[0][0].source
    info = await adapter.get_chat_info(source.chat_id)
    assert (source.chat_id, source.chat_type) == ("ha_events", "channel")
    assert info["type"] == source.chat_type


@pytest.mark.asyncio
async def test_telegram_single_producer_matches_chat_info():
    from types import SimpleNamespace
    from gateway.platforms.base import MessageType
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="tok", extra={}))
    chat = SimpleNamespace(id=-1003950368353, type="channel", title="wzrd", full_name=None, is_forum=False)
    message = SimpleNamespace(chat=chat, from_user=None, text="post", caption=None, entities=[],
                              caption_entities=[], message_thread_id=None, is_topic_message=False,
                              message_id=11, reply_to_message=None, quote=None, date=None,
                              forum_topic_created=None)
    source = adapter._build_message_event(message, MessageType.TEXT).source
    assert (source.chat_id, source.chat_type) == ("-1003950368353", "channel")

    adapter._bot = MagicMock()
    adapter._bot.get_chat = AsyncMock(return_value=SimpleNamespace(
        type="channel", title="wzrd", full_name=None, username=None, is_forum=False))
    info = await adapter.get_chat_info(source.chat_id)
    assert info["type"] == source.chat_type


@pytest.mark.asyncio
async def test_teams_single_producer_uses_platform_conversation_type():
    from tests.gateway.test_teams import TeamsAdapter, _make_config

    adapter = TeamsAdapter(_make_config(client_id="bot-id", client_secret="secret", tenant_id="tenant"))
    adapter._app = MagicMock()
    adapter._app.id = "bot-id"
    adapter.handle_message = AsyncMock()
    activity = MagicMock()
    activity.text, activity.id, activity.attachments = "hello", "activity-1", []
    activity.from_ = MagicMock(id="user-1", aad_object_id="aad-1", name="User")
    activity.conversation = MagicMock(id="19:abc@thread.tacv2", conversation_type="channel",
                                      name="General", tenant_id="tenant")
    ctx = MagicMock()
    ctx.activity = activity
    await adapter._on_message(ctx)
    source = adapter.handle_message.call_args[0][0].source
    assert (source.chat_id, source.chat_type) == ("19:abc@thread.tacv2", "channel")
