"""Call-site tests for the fork's free-response quoted-bot-mention exemption (t_2206d24f).

The fork rule lives inline in ``DiscordAdapter._discord_message_admission`` (the
``other_bots_mentioned and not raw_self_mention`` block), reached from the live ``on_message``
handler through ``_dispatch_incoming_message``. In a free-response channel, a message that only
QUOTES another bot (``<@OTHER>`` mid-body) is still for us; a message that STARTS with
``<@OTHER>`` or the legacy nickname form ``<@!OTHER>`` addresses that bot and is suppressed; a
message that also mentions us is always for us. Upstream drops every other-bot mention.

The previous canary (tests/fork_canaries/test_fork_canary_discord_free_response.py) replicated
that rule in a local helper, so a parity merge that dropped it from the adapter stayed green.
Every test here drives the REAL entry point with a fake discord message and asserts the
observable decision: ``handle_message`` dispatched or not. No test reads source text.

1. ``test_wiring_*``: one input per fork case, decision matches the fork rule.
2. ``test_effect_*``: only the content (or only the channel) varies, and dispatch flips the
   way the fork specifies. Outside a free-response channel the quoted case is suppressed,
   which is the upstream shape the exemption overrides.
Mutation proofs (each test goes red on its own one-line regression) are in the PR body.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.Object = lambda *, id: SimpleNamespace(id=id)
    discord_mod.Message = type("Message", (), {})
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402

SELF_ID = 999
OTHER_ID = 555
FREE_CHANNEL = 321
PLAIN_CHANNEL = 654
_WHERE = "gate = DiscordAdapter._discord_message_admission (plugins/platforms/discord/adapter.py), via _dispatch_incoming_message"


class _DMChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id


class _Thread:
    def __init__(self, channel_id=1, parent=None):
        self.id = channel_id
        self.parent = parent
        self.parent_id = getattr(parent, "id", None)


class _ForumChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id


class _TextChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.name = f"chan-{channel_id}"
        self.guild = SimpleNamespace(name="Hermes Server", id=7)
        self.topic = None

    def history(self, *, limit, before, after=None, oldest_first=None):
        async def _iter():
            return
            yield
        return _iter()


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", _DMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", _Thread, raising=False)
    monkeypatch.setattr(discord_platform.discord, "ForumChannel", _ForumChannel, raising=False)
    for var in (
        "DISCORD_REQUIRE_MENTION", "DISCORD_THREAD_REQUIRE_MENTION", "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_FREE_RESPONSE_AUTO_THREAD", "DISCORD_NO_THREAD_CHANNELS", "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS", "DISCORD_HISTORY_BACKFILL", "DISCORD_ALLOW_BOTS",
        "DISCORD_IGNORE_NO_MENTION", "DISCORD_ALLOWED_USERS", "DISCORD_ALLOWED_ROLES",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    ad = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token",
                                       extra={"free_response_channels": str(FREE_CHANNEL)}))
    ad._client = SimpleNamespace(user=SimpleNamespace(id=SELF_ID, bot=True))
    ad._allowed_user_ids = {"42"}
    ad._text_batch_delay_seconds = 0
    ad._ready_event.set()
    ad.handle_message = AsyncMock()
    return ad


_OTHER_BOT = SimpleNamespace(id=OTHER_ID, bot=True)
_msg_ids = iter(range(10_000, 99_999))


def _message(content, *, channel_id=FREE_CHANNEL, mentions=(_OTHER_BOT,)):
    return SimpleNamespace(
        id=next(_msg_ids),
        content=content,
        mentions=list(mentions),
        attachments=[],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=_TextChannel(channel_id),
        guild=SimpleNamespace(id=7, name="Hermes Server"),
        author=SimpleNamespace(id=42, display_name="Ace", name="ace", bot=False),
        type=discord_platform.discord.MessageType.default,
    )


async def _dispatched(adapter, message) -> bool:
    """Drive the live ingress entry point; True iff the message reached handle_message."""
    adapter.handle_message.reset_mock()
    await adapter._dispatch_incoming_message(message)
    return adapter.handle_message.await_count == 1


def _self(adapter):
    return adapter._client.user


# --------------------------------------------------------------------------- #
# 1. Wiring: each fork case, decided by the real gate
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_wiring_quoted_other_bot_mention_dispatched(adapter):
    content = f"earlier <@{OTHER_ID}> said the migration was done, is that right?"
    assert await _dispatched(adapter, _message(content)), (
        f"a QUOTED other-bot mention in a free-response channel was suppressed ({_WHERE}); "
        "the fork's leading-mention exemption is gone and the bot is mute on quoted context"
    )


@pytest.mark.asyncio
async def test_wiring_leading_other_bot_mention_suppressed(adapter):
    content = f"  <@{OTHER_ID}> can you handle this?"
    assert not await _dispatched(adapter, _message(content)), (
        f"a message STARTING with <@OTHER> was dispatched ({_WHERE}); it addresses the other bot"
    )


@pytest.mark.asyncio
async def test_wiring_leading_nickname_form_mention_suppressed(adapter):
    content = f"<@!{OTHER_ID}> legacy nickname mention form"
    assert not await _dispatched(adapter, _message(content)), (
        f"a message STARTING with the nickname form <@!OTHER> was dispatched ({_WHERE}); "
        "the <@!ID> half of the direct-address test is gone"
    )


@pytest.mark.asyncio
async def test_wiring_other_bot_plus_self_mention_dispatched(adapter):
    content = f"<@{OTHER_ID}> and <@{SELF_ID}> both take a look"
    msg = _message(content, mentions=(_OTHER_BOT, _self(adapter)))
    assert await _dispatched(adapter, msg), (
        f"a message mentioning us AND leading with another bot was suppressed ({_WHERE}); "
        "the self-mention escape (`and not raw_self_mention`) is gone"
    )


@pytest.mark.asyncio
async def test_wiring_no_mention_dispatched(adapter):
    assert await _dispatched(adapter, _message("plain free-response question", mentions=())), (
        f"an unmentioned free-response message was suppressed ({_WHERE})"
    )


# --------------------------------------------------------------------------- #
# 2. Visible effect: vary one input, the decision flips the way the fork says
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["<@{id}>", "<@!{id}>"])
async def test_effect_only_mention_position_flips_dispatch(adapter, form):
    mention = form.format(id=OTHER_ID)
    quoted = await _dispatched(adapter, _message(f"what did {mention} mean by lane headers?"))
    leading = await _dispatched(adapter, _message(f"{mention} what did you mean by lane headers?"))
    assert (quoted, leading) == (True, False), (
        f"same channel, same mention {mention}, only its position differs: expected "
        f"(quoted dispatched, leading suppressed), got {(quoted, leading)} ({_WHERE})"
    )


@pytest.mark.asyncio
async def test_effect_quoted_mention_outside_free_response_is_upstream_shape(adapter):
    content = f"earlier <@{OTHER_ID}> said the migration was done, is that right?"
    in_free = await _dispatched(adapter, _message(content, channel_id=FREE_CHANNEL))
    elsewhere = await _dispatched(adapter, _message(content, channel_id=PLAIN_CHANNEL))
    assert (in_free, elsewhere) == (True, False), (
        "the quoted-mention exemption must apply ONLY in free-response channels; outside them an "
        f"other-bot mention means 'not for us' (upstream). got (free, plain)={(in_free, elsewhere)} ({_WHERE})"
    )


@pytest.mark.asyncio
async def test_effect_exemption_is_scoped_to_free_response_even_without_require_mention(adapter, monkeypatch):
    # require_mention off opens every channel downstream (_handle_message), so the admission
    # gate's own free-response scope is the only thing that can tell these two apart.
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    unmentioned = await _dispatched(adapter, _message("plain question", channel_id=PLAIN_CHANNEL, mentions=()))
    quoted = await _dispatched(adapter, _message(f"what did <@{OTHER_ID}> mean?", channel_id=PLAIN_CHANNEL))
    assert (unmentioned, quoted) == (True, False), (
        "outside free-response channels an other-bot mention must still mean 'not for us' at the "
        f"admission gate; got (unmentioned, quoted)={(unmentioned, quoted)} ({_WHERE})"
    )


@pytest.mark.asyncio
async def test_effect_self_mention_rescues_leading_other_bot(adapter):
    content = f"<@{OTHER_ID}> and <@{SELF_ID}> both take a look"
    without_self = await _dispatched(adapter, _message(content.replace(f" and <@{SELF_ID}>", "")))
    with_self = await _dispatched(adapter, _message(content, mentions=(_OTHER_BOT, _self(adapter))))
    assert (without_self, with_self) == (False, True), (
        f"adding a self mention to an other-bot-addressed message must flip it to dispatched; "
        f"got (without_self, with_self)={(without_self, with_self)} ({_WHERE})"
    )
