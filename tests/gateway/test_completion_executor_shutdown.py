"""Loop-wide task cancellation must not cancel a mutating claim future."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_constants import get_hermes_home
from tests.gateway.test_completion_claim_cancellation import _events, _assert_reclaimable
from tests.gateway.test_completion_delivery import _runner, isolated_registry  # noqa: F401
from tools import async_delegation as ad


def test_loop_shutdown_recovers_claim_and_preserves_producer_scope(tmp_path, monkeypatch):
    secondary = tmp_path / "secondary"
    root = tmp_path / "root"
    secondary.mkdir()
    root.mkdir()
    events = _events(secondary, 1)
    monkeypatch.setenv("HERMES_HOME", str(root))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    seen_homes = []
    original = ad.claim_event_delivery
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    def blocked(event, consumer):
        entered.set()
        assert release.wait(10)
        try:
            seen_homes.append(get_hermes_home())
            return original(event, consumer)
        finally:
            finished.set()

    monkeypatch.setattr(ad, "claim_event_delivery", blocked)

    async def exercise():
        delivery = asyncio.create_task(runner._deliver_async_delegation_group(events))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            # Model the shutdown sweep, not only awaiter cancellation.
            for task in asyncio.all_tasks():
                if task is not asyncio.current_task():
                    task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await delivery
            assert await asyncio.to_thread(finished.wait, 10)
            assert seen_homes == [secondary]
            monkeypatch.setattr(ad, "claim_event_delivery", original)
            _assert_reclaimable(events, secondary)
            assert get_hermes_home() == root
        finally:
            release.set()
            await asyncio.gather(delivery, return_exceptions=True)

    asyncio.run(exercise())
    adapter.handle_message.assert_not_awaited()
