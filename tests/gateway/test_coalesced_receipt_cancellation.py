"""Cancellation after batch acceptance cannot strand sibling receipts."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_completion_delivery import (
    _async_event,
    _json_outbox_states,
    _runner,
    _persist_pending_completion,
    _seed_json_outbox,
    isolated_registry,  # noqa: F401 — autouse fixture for real temp storage
)
from tools import async_delegation as ad


@pytest.mark.parametrize("with_legacy", [False, True])
@pytest.mark.parametrize("blocked_receipt", ["deleg_primary", "deleg_sibling_1"])
def test_cancel_during_batch_receipt_finishes_all_accepted_receipts(
    blocked_receipt, with_legacy, tmp_path, monkeypatch,
):
    events = [_async_event(name) for name in (
        "deleg_primary", "deleg_sibling_1", "deleg_sibling_2",
    )]
    for event in events:
        event["summary"] = "unique-result-" + event["delegation_id"]
    _seed_json_outbox(events, tmp_path)
    if with_legacy:
        for event in events:
            _persist_pending_completion(event)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    entered = threading.Event()
    release = threading.Event()
    all_finished = threading.Event()
    completed = []
    original = ad.complete_event_delivery_with_retry

    def receipt(event, claim):
        if event["delegation_id"] == blocked_receipt:
            entered.set()
            assert release.wait(10), "test did not release the receipt worker"
        result = original(event, claim)
        completed.append(event["delegation_id"])
        if len(completed) == len(events):
            all_finished.set()
        return result

    monkeypatch.setattr(ad, "complete_event_delivery_with_retry", receipt)

    async def exercise():
        task = asyncio.create_task(runner._deliver_async_delegation_group(events))
        try:
            assert await asyncio.to_thread(entered.wait, 10), "receipt never started"
            adapter.handle_message.assert_awaited_once()
            delivered_text = adapter.handle_message.call_args.args[0].text
            assert all(event["summary"] in delivered_text for event in events)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.to_thread(all_finished.wait, 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert _json_outbox_states(tmp_path) == ["delivered"] * len(events)
    assert set(completed) == {event["delegation_id"] for event in events}
    if with_legacy:
        assert all(ad.get_durable_delegation(event["delegation_id"])["delivery_state"] == "delivered" for event in events)
    assert asyncio.run(runner._deliver_async_delegation_group(events)) is None
    adapter.handle_message.assert_awaited_once()
    assert ad.enqueue_pending_outbox(current_boot_id="after-cancel", profile_home=tmp_path) == 0
