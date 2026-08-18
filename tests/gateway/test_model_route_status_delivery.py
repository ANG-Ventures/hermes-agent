"""Gateway delivery guarantees for model-route change announcements."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import _emit_fallback_announce
from gateway.config import Platform
from gateway.run import TurnRunner, _prepare_gateway_status_message
from gateway.turn_context import TurnContext
from run_agent import AIAgent


ROUTE_MESSAGE = (
    "🔄 Model fallback (sub pool capped): "
    "claude-apr/claude-fable-5 → claude-apx-1/claude-opus-5"
)


class _RecordingAdapter:
    def __init__(self, *, result=None, error=None):
        self.send_calls = []
        self.update_calls = []
        self.result = result or SimpleNamespace(success=True, message_id="42", error=None)
        self.error = error

    async def send(self, chat_id, content, metadata=None):
        self.send_calls.append((chat_id, content, metadata))
        if self.error:
            raise self.error
        return self.result

    async def send_or_update_status(
        self, chat_id, status_key, content, *, metadata=None
    ):
        self.update_calls.append((chat_id, status_key, content, metadata))
        if self.error:
            raise self.error
        return self.result


class _BubbleAdapter:
    """Minimal Telegram status-bubble model with editable keyed messages."""

    def __init__(self):
        self.messages = {}
        self._status_message_ids = {}

    async def send(self, chat_id, content, metadata=None):
        message_id = str(len(self.messages) + 1)
        self.messages[message_id] = content
        return SimpleNamespace(success=True, message_id=message_id, error=None)

    async def send_or_update_status(
        self, chat_id, status_key, content, *, metadata=None
    ):
        key = (chat_id, status_key)
        message_id = self._status_message_ids.get(key)
        if message_id is None:
            result = await self.send(chat_id, content, metadata=metadata)
            self._status_message_ids[key] = result.message_id
            return result
        self.messages[message_id] = content
        return SimpleNamespace(success=True, message_id=message_id, error=None)


class _StubGatewayRunner:
    def __init__(self, adapter):
        self.adapter = adapter

    def _adapter_for_source(self, source):
        return self.adapter


def _make_turn_runner(
    adapter,
    *,
    snapshot=None,
    current=True,
    loop=None,
):
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="571820863")
    owner = _StubGatewayRunner(adapter)
    ctx = TurnContext(
        source=source,
        _run_still_current=lambda: current,
        _loop_for_step=loop,
        _status_adapter=adapter if snapshot is None else snapshot,
        _current_status_adapter=lambda: owner._adapter_for_source(source),
        _status_chat_id=source.chat_id,
        _status_thread_metadata={"thread_id": "dm"},
    )
    return TurnRunner(owner, ctx)  # type: ignore[arg-type]


async def _wait_until(predicate, *, attempts=100):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("scheduled status delivery did not finish")


def test_exact_fallback_announcement_survives_chat_noise_filter():
    assert (
        _prepare_gateway_status_message(Platform.TELEGRAM, "lifecycle", ROUTE_MESSAGE)
        == ROUTE_MESSAGE
    )


def test_failover_mid_turn_late_binds_adapter_and_sends_durable_message():
    """A reconnect during an active retry must not retain or edit through the old adapter."""
    stale = _RecordingAdapter()
    live = _RecordingAdapter()

    async def scenario():
        runner = _make_turn_runner(
            live,
            snapshot=stale,
            current=True,
            loop=asyncio.get_running_loop(),
        )
        accepted = await asyncio.to_thread(
            runner._status_callback_sync, "lifecycle", ROUTE_MESSAGE
        )
        await _wait_until(lambda: bool(live.send_calls))
        return accepted

    accepted = asyncio.run(scenario())

    assert accepted is True
    assert live.send_calls == [
        ("571820863", ROUTE_MESSAGE, {"thread_id": "dm"})
    ]
    assert live.update_calls == []
    assert stale.send_calls == []
    assert stale.update_calls == []


def test_recovery_producer_message_reaches_durable_delivery_path():
    adapter = _RecordingAdapter()

    async def scenario():
        runner = _make_turn_runner(adapter, loop=asyncio.get_running_loop())
        agent = SimpleNamespace(
            _last_fallback_announced=None,
            _emit_status=lambda message: runner._status_callback_sync(
                "lifecycle", message
            ),
        )
        await asyncio.to_thread(
            _emit_fallback_announce,
            agent,
            "claude-opus-5",
            "claude-fable-5",
            "claude-apr",
            old_provider="claude-apx-1",
            kind="recovery",
            recovery_via="restore",
            record_event=False,
        )
        await _wait_until(lambda: bool(adapter.send_calls))

    asyncio.run(scenario())

    assert adapter.send_calls == [
        (
            "571820863",
            "🔄 Model recovery (restore): "
            "claude-apx-1/claude-opus-5 → claude-apr/claude-fable-5",
            {"thread_id": "dm"},
        )
    ]
    assert adapter.update_calls == []


def test_route_change_does_not_use_mutable_lifecycle_status_bubble():
    """Later lifecycle statuses must not overwrite a delivered route change."""
    adapter = _BubbleAdapter()
    later_status = "still working"

    async def scenario():
        runner = _make_turn_runner(adapter, loop=asyncio.get_running_loop())
        await asyncio.to_thread(
            runner._status_callback_sync, "lifecycle", ROUTE_MESSAGE
        )
        await _wait_until(lambda: ROUTE_MESSAGE in adapter.messages.values())
        await asyncio.to_thread(
            runner._status_callback_sync, "lifecycle", later_status
        )
        await _wait_until(lambda: later_status in adapter.messages.values())

    asyncio.run(scenario())

    assert adapter.messages == {"1": ROUTE_MESSAGE, "2": later_status}
    assert adapter._status_message_ids == {("571820863", "lifecycle"): "2"}


def test_route_drop_missing_adapter_warns_exact_reason(caplog):
    runner = _make_turn_runner(None)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        accepted = runner._status_callback_sync("lifecycle", ROUTE_MESSAGE)

    assert accepted is False
    assert "route-change status dropped: reason=no_status_adapter" in caplog.text


def test_route_drop_adapter_resolution_failure_warns_exact_reason(caplog):
    runner = _make_turn_runner(_RecordingAdapter())

    def fail_resolution():
        raise RuntimeError("registry unavailable")

    runner._ctx._current_status_adapter = fail_resolution
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        accepted = runner._status_callback_sync("lifecycle", ROUTE_MESSAGE)

    assert accepted is False
    assert (
        "route-change status dropped: reason=adapter_resolution_failed"
        in caplog.text
    )
    assert "reason=no_status_adapter" not in caplog.text


def test_route_drop_stale_run_warns_exact_reason(caplog):
    runner = _make_turn_runner(_RecordingAdapter(), current=False)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        accepted = runner._status_callback_sync("lifecycle", ROUTE_MESSAGE)

    assert accepted is False
    assert "route-change status dropped: reason=run_not_current" in caplog.text


def test_route_drop_filter_warns_exact_reason(caplog, monkeypatch):
    adapter = _RecordingAdapter()
    runner = _make_turn_runner(adapter)
    monkeypatch.setattr("gateway.run._prepare_gateway_status_message", lambda *_: None)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        accepted = runner._status_callback_sync("lifecycle", ROUTE_MESSAGE)

    assert accepted is False
    assert "route-change status dropped: reason=status_filtered" in caplog.text


def test_route_drop_schedule_failure_warns_exact_reason(caplog, monkeypatch):
    adapter = _RecordingAdapter()
    runner = _make_turn_runner(adapter)

    def reject_schedule(coro, *_args, **_kwargs):
        coro.close()
        return None

    monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", reject_schedule)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        accepted = runner._status_callback_sync("lifecycle", ROUTE_MESSAGE)

    assert accepted is False
    assert "route-change status dropped: reason=schedule_failed" in caplog.text


@pytest.mark.parametrize(
    ("adapter", "reason"),
    [
        (
            _RecordingAdapter(
                result=SimpleNamespace(
                    success=False, message_id=None, error="transport unavailable"
                )
            ),
            "adapter_send_failed",
        ),
        (_RecordingAdapter(error=RuntimeError("loop closed")), "adapter_send_exception"),
    ],
)
def test_route_drop_async_adapter_failure_warns_exact_reason(
    caplog, adapter, reason
):
    async def scenario():
        runner = _make_turn_runner(adapter, loop=asyncio.get_running_loop())
        accepted = await asyncio.to_thread(
            runner._status_callback_sync, "lifecycle", ROUTE_MESSAGE
        )
        await _wait_until(lambda: reason in caplog.text)
        return accepted

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        accepted = asyncio.run(scenario())

    assert accepted is True
    assert f"route-change status dropped: reason={reason}" in caplog.text


def test_emit_status_propagates_explicit_callback_rejection():
    agent = SimpleNamespace(
        log_prefix="",
        quiet_mode=True,
        _vprint=lambda *_args, **_kwargs: None,
        status_callback=lambda *_args, **_kwargs: False,
    )

    assert AIAgent._emit_status(agent, ROUTE_MESSAGE) is False  # type: ignore[arg-type]


def test_emit_status_preserves_legacy_success_for_non_route_suppression():
    runner = _make_turn_runner(None)
    agent = SimpleNamespace(
        log_prefix="",
        quiet_mode=True,
        _vprint=lambda *_args, **_kwargs: None,
        status_callback=runner._status_callback_sync,
    )

    assert AIAgent._emit_status(agent, "ordinary lifecycle status") is True  # type: ignore[arg-type]
