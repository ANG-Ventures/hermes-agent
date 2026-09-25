import asyncio
import json
import sys
from pathlib import Path
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

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class _UnknownChannel(Exception):
    """Shape of discord.NotFound for a deleted channel (code 10003)."""

    code = 10003

    def __str__(self):
        return "404 Not Found (error code: 10003): Unknown Channel"


def _adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    fetch = AsyncMock(side_effect=_UnknownChannel())
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=None),
        fetch_channel=fetch,
    )
    return adapter, fetch


@pytest.mark.asyncio
async def test_send_to_deleted_thread_is_negative_cached_across_restart(
    caplog, monkeypatch, tmp_path
):
    adapter, fetch = _adapter(monkeypatch, tmp_path)
    await adapter._threads.mark_async("777")
    assert "777" in adapter._threads

    with caplog.at_level("WARNING"):
        first = await adapter.send("555", "hi", metadata={"thread_id": "777"})
        second = await adapter.send("555", "hi again", metadata={"thread_id": "777"})

    assert first.success is False and first.error_kind == "not_found"
    assert "10003" in first.error
    assert second.success is False and second.error_kind == "not_found"
    # Only the first send reached the API; the second short-circuited.
    assert fetch.await_count == 1
    # No ERROR+traceback for a known-gone target; one WARNING names it.
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert sum("777" in r.getMessage() and "negative-cached" in r.getMessage()
               for r in caplog.records) == 1
    # Evicted from thread participation (discord_threads.json).
    assert "777" not in adapter._threads
    saved = json.loads((tmp_path / "discord_threads.json").read_text())
    assert "777" not in saved

    # Restart: a fresh adapter loads the persisted negative cache.
    adapter._dead_channels.flush()
    restarted, fetch2 = _adapter(monkeypatch, tmp_path)
    again = await restarted.send("555", "x", metadata={"thread_id": "777"})
    info = await restarted.get_chat_info("777")
    assert again.success is False and again.error_kind == "not_found"
    assert info.get("error")
    fetch2.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_chat_info_negative_caches_unknown_channel(caplog, monkeypatch, tmp_path):
    adapter, fetch = _adapter(monkeypatch, tmp_path)
    with caplog.at_level("WARNING"):
        first = await adapter.get_chat_info("888")
        second = await adapter.get_chat_info("888")
    assert "10003" in first["error"]
    assert second.get("error")
    assert fetch.await_count == 1
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    # The send path honours the same cache.
    res = await adapter.send("888", "hi")
    assert res.error_kind == "not_found"
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_other_send_errors_are_not_negative_cached(monkeypatch, tmp_path):
    adapter, fetch = _adapter(monkeypatch, tmp_path)
    fetch.side_effect = RuntimeError("503 Service Unavailable")
    first = await adapter.send("999", "hi")
    second = await adapter.send("999", "hi")
    assert first.success is False and first.error_kind is None
    assert second.success is False
    assert fetch.await_count == 2
    assert "999" not in adapter._dead_channels


def test_channel_directory_skips_dead_session_targets(monkeypatch, tmp_path):
    from gateway import channel_directory as cd

    monkeypatch.setattr(cd, "_build_from_sessions", lambda name: [
        {"id": "100:777", "name": "dead thread", "type": "thread", "thread_id": "777"},
        {"id": "777", "name": "dead as chat", "type": "thread", "thread_id": None},
        {"id": "200", "name": "live dm", "type": "dm", "thread_id": None},
    ])
    adapter = SimpleNamespace(
        _client=SimpleNamespace(guilds=[]),
        _is_dead_channel=lambda cid: str(cid) == "777",
    )
    ids = [c["id"] for c in cd._build_discord(adapter)]
    assert ids == ["200"]
