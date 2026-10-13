"""Stop preserves notifications without restarting the stopped conversation."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway import queue_mgmt as qm
from tests.gateway.test_interrupt_keeps_parked_internal_wake import (
    _event, _gateway, _run_command, _COMMAND_REASONS,
)


@pytest.mark.asyncio
async def test_stop_then_adapter_drain_does_not_start_parked_wake():
    adapter, runner, source, key = _gateway()
    wake = _event(source, 'completion', internal=True)
    adapter._pending_messages[key] = wake
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    assert adapter.restarted == []
    assert runner._session_state(key).conversation.held_queue == [wake]
    assert not runner._is_session_running(key)


@pytest.mark.asyncio
async def test_late_wake_and_human_resume_preserve_human_queue():
    adapter, runner, source, key = _gateway()
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    human = _event(source, 'held human', internal=False)
    wake = _event(source, 'late completion', internal=True)
    runner._session_state(key).conversation.held_queue.append(human)
    assert await qm.gate_stopped_event(runner, key, wake, adapter)
    assert runner._session_state(key).conversation.held_queue == [human, wake]
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    assert not await qm.gate_stopped_event(runner, key, _event(source, 'continue', internal=False), adapter)
    assert runner._session_state(key).conversation.held_queue == [human]
    assert qm.active_items(runner, key, adapter) == [wake]


@pytest.mark.asyncio
async def test_control_command_does_not_resume_or_consume_notifications():
    adapter, runner, source, key = _gateway()
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    for text in ('/model astra', '/status', '/queue list'):
        assert not await qm.gate_stopped_event(runner, key, _event(source, text, internal=False), adapter)
        assert runner._session_state(key).conversation.automatic_turns_paused is True


@pytest.mark.asyncio
async def test_queue_run_releases_internal_only_hold_once():
    adapter, runner, source, key = _gateway()
    wake = _event(source, 'completion', internal=True)
    adapter._pending_messages[key] = wake
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    await qm.release(runner, key, source, adapter, busy=False)
    await adapter._drain_pending_after_session_command(key, asyncio.Event())
    assert adapter.restarted == [wake]
    assert runner._session_state(key).conversation.held_queue == []
    assert runner._session_state(key).conversation.automatic_turns_paused is False


@pytest.mark.asyncio
async def test_new_internal_arrival_is_held_by_real_inbound_before_busy_dispatch():
    adapter, runner, source, key = _gateway()
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    wake = _event(source, 'new notification', internal=True)
    runner._hm_admit_event = AsyncMock(return_value=(wake, source, True))
    runner._hm_estop_gate = lambda *args: None
    runner._hm_pending_reply_intercepts = AsyncMock(side_effect=AssertionError('passed pause gate'))
    assert await runner._handle_message(wake) is None
    assert runner._session_state(key).conversation.held_queue == [wake]
    runner._hm_pending_reply_intercepts.assert_not_called()


@pytest.mark.asyncio
async def test_restart_hydrates_stop_marker_and_other_session_stays_unpaused():
    from types import SimpleNamespace
    adapter, runner, source, key = _gateway()
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    runner._async_session_store.lookup_by_session_key.return_value = SimpleNamespace(user_stopped_at=1)
    wake = _event(source, 'restored notification', internal=True)
    assert await qm.gate_stopped_event(runner, key, wake, adapter)
    runner._async_session_store.lookup_by_session_key.return_value = None
    assert not await qm.gate_stopped_event(runner, key + ':other', wake, adapter)
    assert runner._session_state(key).conversation.held_queue == [wake]


@pytest.mark.asyncio
async def test_failed_resume_write_preserves_held_events():
    adapter, runner, source, key = _gateway()
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    wake = _event(source, 'completion', internal=True)
    runner._session_state(key).conversation.held_queue.append(wake)
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    runner._async_session_store.clear_user_stopped.side_effect = OSError('disk busy')
    with pytest.raises(OSError):
        await qm.release(runner, key, source, adapter, busy=False)
    assert runner._session_state(key).conversation.automatic_turns_paused is True
    assert runner._session_state(key).conversation.held_queue == [wake]
    assert qm.active_items(runner, key, adapter) == []


@pytest.mark.asyncio
async def test_internal_arrival_during_stop_await_is_held():
    adapter, runner, source, key = _gateway()
    wake = _event(source, 'arrives during stop', internal=True)
    async def mark(_key):
        adapter._pending_messages[key] = wake
        return True
    runner._mark_user_stopped = mark
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    assert adapter.restarted == []
    assert runner._session_state(key).conversation.held_queue == [wake]


@pytest.mark.asyncio
async def test_stop_followed_by_real_model_handler_keeps_notifications_held(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    from hermes_cli.model_switch import ModelSwitchResult
    adapter, runner, source, key = _gateway()
    runner._session_db = None
    runner._voice_mode = {}
    runner._pending_model_notes = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._evict_cached_agent = lambda key: None
    runner.session_store = None
    (tmp_path / 'config.yaml').write_text('model:\n  default: old-model\n  provider: custom\n  persist_switch_by_default: false\n')
    monkeypatch.setattr(gateway_run, '_hermes_home', tmp_path)
    monkeypatch.setattr('hermes_cli.config.get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr('agent.models_dev.fetch_models_dev', lambda: {})
    monkeypatch.setattr('hermes_cli.model_switch.switch_model', lambda **kw: ModelSwitchResult(
        success=True, new_model='gpt-6-astra', target_provider='custom',
        api_key='test-only', base_url='http://localhost:1/v1', api_mode='chat_completions',
        provider_label='Test', is_global=False))
    wake = _event(source, 'completion', internal=True)
    adapter._pending_messages[key] = wake
    await _run_command(runner, adapter, source, key, _COMMAND_REASONS[0])
    reply = await runner._handle_model_command(_event(source, '/model astra --session', internal=False))
    assert reply is not None
    assert runner._session_model_overrides[key]['model'] == 'gpt-6-astra'
    assert runner._session_state(key).conversation.automatic_turns_paused is True
    assert runner._session_state(key).conversation.held_queue == [wake]
    assert adapter.restarted == []
