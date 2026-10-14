"""A transport replacement must not strand output owned by an older turn."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.stream_consumer import GatewayStreamConsumer
from gateway.kanban_lifecycle_digest import LifecycleDigest
from gateway.run_adapters import GatewayAdapterLifecycleMixin


def transport(ok=True):
    return SimpleNamespace(
        platform=Platform.DISCORD, name="Discord",
        send=AsyncMock(return_value=SendResult(success=ok, error=None if ok else "send_path_degraded")),
    )


@pytest.mark.asyncio
async def test_running_commentary_follows_replacement_without_duplicate():
    old, new = transport(False), transport()
    current = [old]
    consumer = GatewayStreamConsumer(old, "thread", adapter_resolver=lambda: current[0])
    current[0] = new
    assert await consumer._send_commentary("still working")
    old.send.assert_not_called()
    new.send.assert_awaited_once()
    assert consumer._delivered_commentary_texts == ["still working"]


@pytest.mark.asyncio
async def test_existing_preview_final_edit_uses_replacement():
    old, new = transport(), transport()
    for adapter in (old, new):
        adapter.MAX_MESSAGE_LENGTH = 2000
        adapter.send.return_value = SendResult(success=True, message_id="preview")
        adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="preview"))
    current = [old]
    consumer = GatewayStreamConsumer(old, "thread", adapter_resolver=lambda: current[0])
    await consumer._send_or_edit("partial")
    current[0] = new
    await consumer._send_or_edit("complete answer", finalize=True)
    old.send.assert_awaited_once()
    old.edit_message.assert_not_called()
    new.edit_message.assert_awaited_once()
    new.send.assert_not_called()


@pytest.mark.asyncio
async def test_missing_replacement_does_not_borrow_another_bot():
    old = transport(False)
    consumer = GatewayStreamConsumer(old, "thread", adapter_resolver=lambda: None)
    assert not await consumer._send_commentary("not delivered")
    assert consumer._delivered_commentary_texts == []


@pytest.mark.asyncio
async def test_reconnect_rebinds_held_digest_and_fallback_only_for_old_owner():
    old, new, other = transport(False), transport(), transport()
    digest = LifecycleDigest()
    digest.add(("discord", "thread"), old, "first", 0, 0,
               fallback=(("discord", "logs"), old, 0))
    digest.add(("discord", "other-profile"), other, "second", 0, 0)
    runner = SimpleNamespace(
        _failed_platforms={Platform.DISCORD: {"retired_adapter": old}},
        _kanban_lifecycle_digest=digest, adapters={},
        delivery_router=SimpleNamespace(adapters={}),
        _publish_primary_adapter=Mock(), _update_platform_runtime_status=Mock(),
        _schedule_planned_restart_replay=Mock(),
        _redeliver_failed_obligations_for_platform=AsyncMock(),
        _prepare_auto_resume_decisions=AsyncMock(), _schedule_resume_pending_sessions=Mock(),
    )
    new.send_path_degraded = False
    await GatewayAdapterLifecycleMixin._install_reconnected_adapter(runner, Platform.DISCORD, new)
    first = digest._held[("discord", "thread", "")]
    assert first["adapter"] is new
    assert first["fallback"][1] is new
    assert digest._held[("discord", "other-profile", "")]["adapter"] is other
    assert await digest.flush(1) == 2
    assert await digest.flush(2) == 0
    old.send.assert_not_called()
    new.send.assert_awaited_once()
    other.send.assert_awaited_once()
