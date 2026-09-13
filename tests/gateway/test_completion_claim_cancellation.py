"""Unadopted executor claims must be recovered before cancellation exits."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import _profile_runtime_scope
from hermes_constants import get_hermes_home
from tests.gateway.test_completion_delivery import (
    _async_event,
    _json_outbox_states,
    _persist_pending_completion,
    _runner,
    _seed_json_outbox,
    isolated_registry,  # noqa: F401
)
from tools import async_delegation as ad


def _events(home, count):
    events = [_async_event(f"deleg_claim_{i}") for i in range(count)]
    _seed_json_outbox(events, home)
    with _profile_runtime_scope(home):
        for event in events:
            _persist_pending_completion(event)
    return events


def _assert_reclaimable(events, home):
    assert _json_outbox_states(home) == ["pending"] * len(events)
    with _profile_runtime_scope(home):
        for event in events:
            claim = ad.claim_event_delivery(event, "next-consumer")
            assert claim
            ad.release_event_delivery(event, claim)


@pytest.mark.parametrize("blocked_index", [None, 0, 2])
@pytest.mark.parametrize("cancel_count", [1, 2])
def test_cancel_claim_recovers_worker_and_prior_siblings(
    tmp_path, monkeypatch, blocked_index, cancel_count,
):
    secondary = tmp_path / "secondary"
    root = tmp_path / "root"
    secondary.mkdir()
    root.mkdir()
    events = _events(secondary, 1 if blocked_index is None else 3)
    monkeypatch.setenv("HERMES_HOME", str(root))
    blocked_event = events[blocked_index or 0]
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    entered = threading.Event()
    release = threading.Event()
    original = ad.claim_event_delivery

    def blocked(event, consumer):
        if event is blocked_event:
            entered.set()
            assert release.wait(10)
        return original(event, consumer)

    monkeypatch.setattr(ad, "claim_event_delivery", blocked)

    async def exercise():
        task = asyncio.create_task(runner._deliver_async_delegation_group(events))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Cancellation itself must finish cleanup, not loop teardown.
            assert get_hermes_home() == root
            _assert_reclaimable(events, secondary)
            assert get_hermes_home() == root
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    adapter.handle_message.assert_not_awaited()
    _assert_reclaimable(events, secondary)


def test_batch_claim_exception_releases_prior_siblings(tmp_path, monkeypatch):
    events = _events(tmp_path, 3)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    original = ad.claim_event_delivery

    def fail(event, consumer):
        if event is events[-1]:
            raise OSError("claim storage unavailable")
        return original(event, consumer)

    monkeypatch.setattr(ad, "claim_event_delivery", fail)
    with pytest.raises(OSError, match="claim storage unavailable"):
        asyncio.run(runner._deliver_async_delegation_group(events))
    adapter.handle_message.assert_not_awaited()
    monkeypatch.setattr(ad, "claim_event_delivery", original)
    _assert_reclaimable(events, tmp_path)


@pytest.mark.parametrize("outcome", ["occupied", "raises"])
def test_cancel_claim_preserves_other_owner_and_cancellation(
    tmp_path, monkeypatch, caplog, outcome,
):
    events = _events(tmp_path, 1)
    event = events[0]
    owner = ad.claim_event_delivery(event, "other-consumer")
    assert owner
    entered = threading.Event()
    release = threading.Event()
    original = ad.claim_event_delivery
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    def blocked(evt, consumer):
        entered.set()
        assert release.wait(10)
        if outcome == "raises":
            raise OSError("cancelled worker storage failure")
        return original(evt, consumer)

    monkeypatch.setattr(ad, "claim_event_delivery", blocked)

    async def exercise():
        task = asyncio.create_task(runner._deliver_async_delegation_group(events))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    adapter.handle_message.assert_not_awaited()
    assert original(event, "competitor") is None
    assert ad.release_completion_delivery(event["delegation_id"], owner)
    monkeypatch.setattr(ad, "claim_event_delivery", original)
    _assert_reclaimable(events, tmp_path)
    if outcome == "raises":
        assert "cancelled worker storage failure" in caplog.text
