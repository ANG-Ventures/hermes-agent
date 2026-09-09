"""The actual write refusal sends one notice to the affected chat, not home."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,scope", [(Platform.DISCORD, None), (Platform.SLACK, "workspace")])
async def test_collision_delivers_once_from_store_worker_thread(tmp_path, caplog, platform, scope):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path))
    runner._gateway_loop = asyncio.get_running_loop()
    runner.session_store._db = None
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(success=True)
    runner.delivery_router = DeliveryRouter(runner.config, {platform: adapter})
    source = SessionSource(platform=platform, chat_id="123", chat_type="group", user_id="456", scope_id=scope)
    entry = await asyncio.to_thread(runner.session_store.get_or_create_session, source)
    alias = entry.session_key.replace(":group:", ":channel:")
    for _ in range(2):
        runner.session_store._entries[alias] = replace(entry, session_key=alias)
        with pytest.raises(ValueError, match="session-key collision"):
            await asyncio.to_thread(runner.session_store.persist)
    # Drain the scheduled notification, without sleep-based timing assertions.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    adapter.send.assert_awaited_once()
    args = adapter.send.await_args
    assert args.args[0] == "123"
    assert f"session-key collision refused: {platform.value} 123 (group vs channel)" in str(args)
    assert len([r for r in caplog.records if "session-key collision refused" in r.message and r.levelname == "WARNING"]) == 1
