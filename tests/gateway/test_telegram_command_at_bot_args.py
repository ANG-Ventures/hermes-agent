"""Group ``/cmd@bot <args>`` must keep the space between command and args.

Live E2E 2026-10-14: in a Telegram group, ``/queue@ace_hermes_aegis_bot Disposable…`` reached the
gateway as ``/queuedisposable`` (unknown command) because the trigger cleaner stripped the handle
AND the whitespace after it, gluing the command to its first argument.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.event import MessageType
from tests.gateway.test_telegram_group_gating import _group_message, _make_adapter

BOT = "ace_hermes_aegis_bot"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (f"/queue@{BOT} alpha", "/queue alpha"),
        (f"/queue@{BOT} Disposable probe two", "/queue Disposable probe two"),
        (f"/queue@{BOT}", "/queue"),
        (f"/QUEUE@{BOT.upper()} alpha", "/QUEUE alpha"),
        (f"@{BOT} hello", "hello"),
        (f"@{BOT}, hello", "hello"),
    ],
)
def test_clean_bot_trigger_text_keeps_command_arg_separator(raw, expected):
    adapter = _make_adapter(bot_username=BOT)
    assert adapter._clean_bot_trigger_text(raw) == expected


def test_group_command_with_handle_dispatches_command_and_args():
    """Through the real command handler: the event parses as ``queue`` + ``alpha``."""
    async def run():
        adapter = _make_adapter(bot_username=BOT, require_mention=True)
        adapter._ensure_forum_commands = AsyncMock()
        events = []
        adapter._enqueue_text_event = events.append
        adapter.handle_message = AsyncMock(side_effect=events.append)
        text = f"/queue@{BOT} alpha"
        cmd_len = len(f"/queue@{BOT}")
        msg = _group_message(text, entities=[SimpleNamespace(type="bot_command", offset=0, length=cmd_len)])
        await adapter._handle_command(SimpleNamespace(update_id=2001, message=msg, effective_message=None), SimpleNamespace())
        assert len(events) == 1
        event = events[0]
        assert event.message_type == MessageType.COMMAND
        assert event.text == "/queue alpha"
        assert event.get_command() == "queue"
        assert event.get_command_args() == "alpha"

    asyncio.run(run())
