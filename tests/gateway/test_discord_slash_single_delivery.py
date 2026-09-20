"""Native slash interactions must deliver a command reply EXACTLY ONCE.

Regression guard for the duplicate ``/queue`` bug: a Discord NATIVE slash
command produced "Queued for the next turn." TWICE — once as the ephemeral
interaction reply (a HARDCODED string in ``_run_simple_slash``) and again as
a PUBLIC channel message (``BasePlatformAdapter.handle_message``'s
busy-bypass path publishing the gateway's own return value).

The fix hands the gateway's reply back on the event
(``suppress_public_echo`` / ``deferred_reply_text``) so the interaction
carries the REAL text and nothing is published publicly. These tests drive
the real ``_run_simple_slash`` -> ``handle_message`` -> busy-bypass chain
and count deliveries on both surfaces.
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig

from tests.gateway.test_discord_slash_commands import _ensure_discord_mock

_ensure_discord_mock()

from gateway.platforms.base import build_session_key  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class _FakeTextChannel:
    """A channel that is NOT a discord.Thread or discord.DMChannel."""

    def __init__(self, channel_id=123, name="general", guild_name="TestGuild"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name=guild_name, id=1)
        self.topic = None


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="x")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(
        tree=MagicMock(),
        get_channel=lambda _id: None,
        fetch_channel=AsyncMock(),
        user=SimpleNamespace(id=99999, name="HermesBot"),
    )
    a._text_batch_delay_seconds = 0
    a._check_slash_authorization = AsyncMock(return_value=True)
    a._send_with_retry = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="m1")
    )
    a._busy_session_handler = None
    return a


def _interaction():
    return SimpleNamespace(
        channel=_FakeTextChannel(),
        channel_id=123,
        guild_id=456,
        user=SimpleNamespace(id=42, name="Jezza", display_name="Jezza"),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


def _mark_busy(adapter, interaction, command_text):
    """Install an active-session guard for the session this slash targets."""
    probe = adapter._build_slash_event(interaction, command_text)
    adapter.canonicalize_session_source(probe.source)
    session_key = build_session_key(
        probe.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get(
            "thread_sessions_per_user", False
        ),
        profile=adapter._session_key_profile(probe.source),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    return session_key


def _ephemeral_text(interaction):
    if interaction.edit_original_response.await_args is None:
        return None
    return interaction.edit_original_response.await_args.kwargs.get("content")


# ---------------------------------------------------------------------------
# A1 — exactly one delivery on the busy-bypass path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_queue_while_busy_delivers_exactly_once(adapter):
    """Native /queue on a BUSY session: ONE delivery, on the interaction.

    Before the fix this asserted 2 deliveries (public _send_with_retry from
    base.py's bypass path + ephemeral edit_original_response from the
    adapter's hardcoded followup).
    """
    interaction = _interaction()
    _mark_busy(adapter, interaction, "/queue do the thing")
    adapter._message_handler = AsyncMock(return_value="Queued for the next turn.")

    await adapter._run_simple_slash(
        interaction, "/queue do the thing", "Queued for the next turn."
    )

    deliveries = (
        adapter._send_with_retry.await_count
        + interaction.edit_original_response.await_count
        + interaction.delete_original_response.await_count
    )
    assert deliveries == 1, (
        f"expected exactly ONE delivery, got {deliveries} "
        f"(public={adapter._send_with_retry.await_count}, "
        f"ephemeral={interaction.edit_original_response.await_count})"
    )
    # The single delivery is the EPHEMERAL interaction reply.
    adapter._send_with_retry.assert_not_awaited()
    assert _ephemeral_text(interaction) == "Queued for the next turn."


# ---------------------------------------------------------------------------
# A2 — the ephemeral carries the gateway's REAL text, incl. the depth suffix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_queue_ephemeral_carries_gateway_depth_suffix(adapter):
    """The hardcoded followup DROPPED the "(N queued)" suffix — the ephemeral
    said "Queued for the next turn." even when the gateway said "(2 queued)".
    The interaction must render the gateway's own text.
    """
    interaction = _interaction()
    _mark_busy(adapter, interaction, "/queue second thing")
    adapter._message_handler = AsyncMock(
        return_value="Queued for the next turn. (2 queued)"
    )

    await adapter._run_simple_slash(
        interaction, "/queue second thing", "Queued for the next turn."
    )

    assert _ephemeral_text(interaction) == "Queued for the next turn. (2 queued)"
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_slash_ephemeral_shows_gateway_usage_error(adapter):
    """A usage error from the gateway must reach the user, not be masked by
    the optimistic hardcoded followup."""
    interaction = _interaction()
    _mark_busy(adapter, interaction, "/queue")
    adapter._message_handler = AsyncMock(return_value="Usage: /queue <prompt>")

    await adapter._run_simple_slash(
        interaction, "/queue", "Queued for the next turn."
    )

    assert _ephemeral_text(interaction) == "Usage: /queue <prompt>"
    adapter._send_with_retry.assert_not_awaited()


# ---------------------------------------------------------------------------
# A3 — text-typed (non-interaction) /queue is byte-identical: one PUBLIC msg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_typed_queue_still_publishes_one_public_message(adapter):
    """A text-typed /queue has no interaction to answer, so the public echo
    must survive untouched — exactly one public message, same content."""
    interaction = _interaction()
    session_key = _mark_busy(adapter, interaction, "/queue typed")

    event = adapter._build_slash_event(interaction, "/queue typed")
    event.suppress_public_echo = False  # a typed message never sets this
    adapter._message_handler = AsyncMock(return_value="Queued for the next turn.")

    await adapter.handle_message(event)

    assert session_key in adapter._active_sessions
    assert adapter._send_with_retry.await_count == 1
    assert (
        adapter._send_with_retry.await_args.kwargs["content"]
        == "Queued for the next turn."
    )
    interaction.edit_original_response.assert_not_awaited()


# ---------------------------------------------------------------------------
# A4 — class sweep: every _run_simple_slash caller with a hardcoded followup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command_text,followup_msg,gateway_reply",
    [
        ("/queue x", "Queued for the next turn.", "Queued for the next turn."),
        ("/reset", "New conversation started~", "✨ New session started!"),
        ("/reset", "Session reset~", "✨ New session started!"),
        ("/retry", "Retrying~", "⏳ Agent is running — `/retry` can't run mid-turn."),
        ("/status", "Status sent~", "Status: running"),
        ("/stop", "Stop requested~", "Stopped."),
        ("/update", "Update initiated~", "Updating..."),
        ("/restart", "Restart requested~", "Restarting..."),
        ("/bg x", "Background task started~", "Background task started."),
        ("/btw x", "Side question dispatched~", "Side question dispatched."),
    ],
)
@pytest.mark.asyncio
async def test_every_hardcoded_followup_caller_delivers_once_when_busy(
    adapter, command_text, followup_msg, gateway_reply
):
    """The whole class, not just /queue: every _run_simple_slash caller that
    passes a hardcoded followup_msg doubled on the busy-bypass path."""
    interaction = _interaction()
    _mark_busy(adapter, interaction, command_text)
    adapter._message_handler = AsyncMock(return_value=gateway_reply)

    await adapter._run_simple_slash(interaction, command_text, followup_msg)

    deliveries = (
        adapter._send_with_retry.await_count
        + interaction.edit_original_response.await_count
        + interaction.delete_original_response.await_count
    )
    assert deliveries == 1, f"{command_text}: {deliveries} deliveries"
    assert _ephemeral_text(interaction) == gateway_reply


@pytest.mark.asyncio
async def test_hardcoded_followup_survives_when_gateway_returns_nothing(adapter):
    """followup_msg is NOT dead code: when the gateway produces no text the
    hardcoded string is the user's only feedback and must still render."""
    interaction = _interaction()
    _mark_busy(adapter, interaction, "/queue x")
    adapter._message_handler = AsyncMock(return_value=None)

    await adapter._run_simple_slash(
        interaction, "/queue x", "Queued for the next turn."
    )

    adapter._send_with_retry.assert_not_awaited()
    assert _ephemeral_text(interaction) == "Queued for the next turn."


@pytest.mark.asyncio
async def test_no_followup_and_no_gateway_text_deletes_placeholder(adapter):
    """A caller with no followup_msg and no gateway text keeps the existing
    behavior: delete the deferred placeholder rather than leave "thinking"."""
    interaction = _interaction()
    _mark_busy(adapter, interaction, "/model")
    adapter._message_handler = AsyncMock(return_value=None)

    await adapter._run_simple_slash(interaction, "/model")

    adapter._send_with_retry.assert_not_awaited()
    interaction.edit_original_response.assert_not_awaited()
    interaction.delete_original_response.assert_awaited_once()


# ---------------------------------------------------------------------------
# Expired interaction: the public echo is the user's ONLY delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_interaction_keeps_public_echo(adapter):
    """When defer() fails (interaction expired) there is no ephemeral surface
    left, so suppressing the public echo would deliver NOTHING. The public
    message must still be sent."""

    class UnknownInteraction(Exception):
        status = 404
        code = 10062

    interaction = _interaction()
    interaction.response.defer = AsyncMock(
        side_effect=UnknownInteraction("Unknown interaction")
    )
    _mark_busy(adapter, interaction, "/queue x")
    adapter._message_handler = AsyncMock(return_value="Queued for the next turn.")

    await adapter._run_simple_slash(
        interaction, "/queue x", "Queued for the next turn."
    )

    assert adapter._send_with_retry.await_count == 1
    assert (
        adapter._send_with_retry.await_args.kwargs["content"]
        == "Queued for the next turn."
    )
    interaction.edit_original_response.assert_not_awaited()


# ---------------------------------------------------------------------------
# Oversized gateway reply: falls back to a chunked public send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_gateway_reply_publishes_instead_of_truncating(adapter):
    """Discord rejects an interaction response over 2000 chars (error 50035).
    An oversized reply (e.g. a long /status) must be published on the channel
    — where send() chunks it — not dropped or truncated."""
    interaction = _interaction()
    _mark_busy(adapter, interaction, "/status")
    long_reply = "x" * (adapter.MAX_MESSAGE_LENGTH + 500)
    adapter._message_handler = AsyncMock(return_value=long_reply)

    await adapter._run_simple_slash(interaction, "/status", "Status sent~")

    assert adapter._send_with_retry.await_count == 1
    assert adapter._send_with_retry.await_args.kwargs["content"] == long_reply
    # Interaction falls back to the short hardcoded ack.
    assert _ephemeral_text(interaction) == "Status sent~"


# ---------------------------------------------------------------------------
# The seam itself: a non-interaction event is unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_inline_command_reply_publishes_by_default(adapter):
    """_deliver_inline_command_reply must be a no-behavior-change wrapper for
    any event that did not opt in."""
    interaction = _interaction()
    event = adapter._build_slash_event(interaction, "/status")

    await adapter._deliver_inline_command_reply(event, "hello", thread_meta=None)

    assert adapter._send_with_retry.await_count == 1
    assert adapter._send_with_retry.await_args.kwargs["content"] == "hello"
    assert event.deferred_reply_text is None


@pytest.mark.asyncio
async def test_deliver_inline_command_reply_hands_back_when_suppressed(adapter):
    interaction = _interaction()
    event = adapter._build_slash_event(interaction, "/status")
    event.suppress_public_echo = True

    await adapter._deliver_inline_command_reply(event, "hello", thread_meta=None)

    adapter._send_with_retry.assert_not_awaited()
    assert event.deferred_reply_text == "hello"
