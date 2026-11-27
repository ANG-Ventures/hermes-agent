"""Regression tests for the Discord typing-indicator loop lifecycle.

Guards two stuck-"is typing…"-bubble bugs in DiscordAdapter._typing_loop:

1. ``stop_typing`` must terminate a loop even when it is parked in a 429
   back-off sleep — a plain ``task.cancel()`` races that sleep, so without
   the stop-event the bubble can survive past the sent response.
2. A channel that is persistently 429-rate-limited must make the loop GIVE
   UP after a bounded number of consecutive failures instead of spinning
   forever (re-POSTing every ~1s), which is what leaves a permanent bubble.

Without the fix these tests fail: test 1 times out waiting for the task to
finish, and test 2 never stops (the old loop had no failure cap).
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    """Minimal mock ``discord`` module exposing ``http.Route`` for the loop."""
    existing = sys.modules.get("discord")
    if existing is not None and getattr(existing, "_typing_test_ready", False):
        return
    discord_mod = existing if existing is not None else MagicMock()
    # The typing loop calls discord.http.Route("POST", "...", channel_id=...).
    discord_mod.http = SimpleNamespace(Route=lambda *a, **k: SimpleNamespace(**k))
    discord_mod._typing_test_ready = True
    sys.modules["discord"] = discord_mod


_ensure_discord_mock()

from plugins.platforms.discord import adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class _FakeRateLimit(Exception):
    """Looks like a Discord 429 to ``_extract_discord_retry_after``."""

    def __init__(self, retry_after=0.05):
        super().__init__("rate limited")
        self.retry_after = retry_after


def _make_adapter(request_side_effect):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    http = SimpleNamespace(request=AsyncMock(side_effect=request_side_effect))
    adapter._client = SimpleNamespace(http=http, is_closed=lambda: False, is_ready=lambda: True)
    return adapter


@pytest.mark.asyncio
async def test_stop_typing_interrupts_429_backoff():
    """stop_typing must end the loop promptly even mid-429-backoff.

    The mock always 429s with a long retry_after, so the loop is parked in
    its back-off sleep when stop_typing fires. The stop-event must wake it.
    """
    adapter = _make_adapter(request_side_effect=_FakeRateLimit(retry_after=30.0))

    await adapter.send_typing("chan-1")
    task = adapter._typing_tasks.get("chan-1")
    assert task is not None and not task.done()

    # Let the loop reach its first 429 back-off sleep.
    await asyncio.sleep(0.05)

    # stop_typing must return quickly and the task must be finished — NOT
    # blocked for the full 30s retry_after.
    await asyncio.wait_for(adapter.stop_typing("chan-1"), timeout=2.0)
    assert task.done()
    assert "chan-1" not in adapter._typing_tasks
    assert "chan-1" not in adapter._typing_stop_events


@pytest.mark.asyncio
async def test_typing_loop_gives_up_after_persistent_rate_limit(monkeypatch):
    """A persistently 429'd channel must stop on its own (bounded failures)."""
    adapter = _make_adapter(request_side_effect=_FakeRateLimit(retry_after=0.01))
    # Discord clamps retry_after to >= 1.0s; override so the loop runs fast
    # under test without depending on wall-clock pacing.
    monkeypatch.setattr(
        DiscordAdapter, "_extract_discord_retry_after",
        staticmethod(lambda exc: 0.01),
    )

    await adapter.send_typing("chan-2")
    task = adapter._typing_tasks.get("chan-2")
    assert task is not None

    # With the failure cap the loop self-terminates. Without the cap (old
    # code) this never completes and the wait_for raises TimeoutError.
    await asyncio.wait_for(task, timeout=3.0)
    assert task.done()
    # Gave up exactly at the cap.
    assert adapter._client.http.request.await_count == (
        discord_platform._DISCORD_TYPING_MAX_CONSECUTIVE_FAILURES
    )


@pytest.mark.asyncio
async def test_successful_post_resets_failure_counter(monkeypatch):
    """An intermittent 429 followed by a success must NOT trip the cap."""
    # Fail 3×, succeed, then fail forever — the success resets the counter,
    # so the loop should still be alive (not given up) shortly after.
    calls = {"n": 0}

    async def side_effect(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 4:
            return None  # one success resets consecutive_failures
        raise _FakeRateLimit(retry_after=0.01)

    monkeypatch.setattr(
        DiscordAdapter, "_extract_discord_retry_after",
        staticmethod(lambda exc: 0.01),
    )
    adapter = _make_adapter(request_side_effect=side_effect)
    await adapter.send_typing("chan-3")
    task = adapter._typing_tasks.get("chan-3")

    # After enough ticks to pass the original 5-failure window, the loop is
    # still running because the success at call 4 reset the counter. (The
    # post-success refresh sleep is interruptible, so the loop parks there.)
    await asyncio.sleep(0.2)
    assert not task.done()

    await asyncio.wait_for(adapter.stop_typing("chan-3"), timeout=2.0)
    assert task.done()


@pytest.mark.asyncio
async def test_typing_refresh_precedes_discord_expiry(monkeypatch, caplog):
    """Exercise real loop with fake HTTP latency/time, not real Discord traffic."""
    clock = 0.0
    starts = []
    completions = []
    delays = []
    parked = asyncio.Event()
    real_wait_for = asyncio.wait_for

    async def request(*_a, **_k):
        nonlocal clock
        starts.append(clock)
        clock += 0.4  # transport latency also consumes indicator lifetime
        completions.append(clock)

    async def fake_wait_for(awaitable, *, timeout):
        nonlocal clock
        delays.append(timeout)
        if len(delays) == 4:
            parked.set()
            return await real_wait_for(awaitable, timeout=2.0)
        awaitable.close()
        clock += timeout
        raise asyncio.TimeoutError

    # Replace ONLY the adapter's asyncio reference, not pytest's event loop.
    monkeypatch.setattr(
        discord_platform, "asyncio",
        SimpleNamespace(**{**vars(asyncio), "wait_for": fake_wait_for}),
    )
    caplog.set_level("DEBUG", logger=discord_platform.__name__)
    adapter = _make_adapter(request)
    chat = "cadence-thread"
    token = adapter._issue_typing_token(chat)
    await adapter.send_typing(chat, token=token)
    task = adapter._typing_tasks[chat]
    await adapter.send_typing(chat, token=token)
    assert adapter._typing_tasks[chat] is task  # base refresher must not duplicate
    try:
        await real_wait_for(parked.wait(), timeout=2.0)
        assert len(starts) == 4
        assert all(0 < b - a < 10 for a, b in zip(starts, starts[1:])), starts
        assert all(b - a < 10 for a, b in zip(completions, completions[1:]))
        assert len(set(delays)) == 1  # stable success cadence, not failure backoff
    finally:
        await real_wait_for(adapter.stop_typing(chat, token=token), timeout=2.0)
    assert task.done()
    assert chat not in adapter._typing_tasks
    assert chat not in adapter._typing_stop_events
    assert chat not in adapter._typing_loop_token
    assert adapter._typing_owner[chat] == token  # stop never mutates owner tokens
    assert "Discord typing loop started" in caplog.text
    assert "Discord typing loop ended" in caplog.text


@pytest.mark.asyncio
async def test_typing_failure_is_visible_without_logging_response_body(caplog):
    adapter = _make_adapter(RuntimeError("sensitive response body"))
    caplog.set_level("INFO", logger=discord_platform.__name__)
    await adapter.send_typing("failed-channel")
    task = adapter._typing_tasks["failed-channel"]
    await asyncio.wait_for(task, timeout=2.0)
    assert adapter._client.http.request.await_count == 1
    assert not adapter._typing_tasks
    assert not adapter._typing_stop_events
    assert not adapter._typing_loop_token
    assert "Discord typing indicator failed for failed-channel" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "sensitive response body" not in caplog.text
