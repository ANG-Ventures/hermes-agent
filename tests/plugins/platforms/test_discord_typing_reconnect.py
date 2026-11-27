"""Typing survives transport replacement without bypassing turn ownership."""

import asyncio
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Real SDK lifecycle states; skip where the optional discord.py dep is absent.
discord = pytest.importorskip("discord", reason="discord.py optional dep not installed")
commands = pytest.importorskip("discord.ext.commands")

from gateway.config import PlatformConfig
from plugins.platforms.discord import adapter as discord_platform
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_state", ["missing", "uninitialized", "closed", "inflight"])
async def test_typing_resumes_after_client_replacement(monkeypatch, replacement_state):
    """Drive real SDK lifecycle states between two ticks, with no network I/O."""
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    old_request = AsyncMock()
    new_request = AsyncMock()

    def healthy(request):
        return SimpleNamespace(
            http=SimpleNamespace(request=request),
            is_closed=lambda: False, is_ready=lambda: True,
        )

    adapter._client = healthy(old_request)
    replacement = None
    if replacement_state in ("uninitialized", "closed"):
        replacement = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        if replacement_state == "closed":
            await replacement.close()

    sleeps = asyncio.Queue()
    errors = []
    real_warning = discord_platform.logger.warning

    def capture_warning(*args, **kwargs):
        if isinstance(sys.exc_info()[1], AttributeError):
            errors.append(traceback.format_exc())
        real_warning(*args, **kwargs)

    async def controlled_wait(awaitable, *, timeout):
        release = asyncio.Event()
        sleeps.put_nowait(release)
        try:
            await release.wait()
            raise asyncio.TimeoutError
        finally:
            awaitable.close()

    monkeypatch.setattr(discord_platform.logger, "warning", capture_warning)
    monkeypatch.setattr(
        discord_platform, "asyncio",
        SimpleNamespace(**{**vars(asyncio), "wait_for": controlled_wait}),
    )
    chat = "123456789012345678"
    token = adapter._issue_typing_token(chat)
    await adapter.send_typing(chat, token=token)
    task = adapter._typing_tasks[chat]

    async def next_sleep():
        waiter = asyncio.create_task(sleeps.get())
        try:
            done, _ = await asyncio.wait(
                (task, waiter), timeout=2.0, return_when=asyncio.FIRST_COMPLETED,
            )
            assert waiter in done, "Typing loop ended during replacement:\n" + "".join(errors)
            assert not task.done(), "Typing loop ended during replacement:\n" + "".join(errors)
            return waiter.result()
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    try:
        tick = await next_sleep()
        assert old_request.await_count == 1
        if replacement_state == "inflight":
            async def retire_during_request(*args, **kwargs):
                adapter._client = healthy(new_request)
                raise RuntimeError("retired HTTP session")

            old_request.side_effect = retire_during_request
        else:
            adapter._client = replacement
        tick.set()
        tick = await next_sleep()
        # A reconnect gap must retain the same owner, not arm duplicate loops.
        await adapter.send_typing(chat, token=token)
        assert adapter._typing_tasks[chat] is task
        adapter._client = healthy(new_request)
        tick.set()
        await next_sleep()
        assert new_request.await_count == 1
        assert old_request.await_count == (2 if replacement_state == "inflight" else 1)
        assert not errors
    finally:
        await asyncio.wait_for(adapter.stop_typing(chat, token=token), timeout=2.0)
        if replacement is not None:
            await replacement.close()
    assert task.done()
    assert not adapter._typing_tasks
    assert not adapter._typing_stop_events
    assert not adapter._typing_loop_token
    assert adapter._typing_owner[chat] == token
    adapter._typing_owner.pop(chat)
    await adapter.send_typing(chat, token=token)
    assert not adapter._typing_tasks  # a late refresh cannot resurrect the stopped turn
