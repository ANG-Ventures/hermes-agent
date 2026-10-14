"""A newer stop wins over a resume suspended at its persistence boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import queue_mgmt as qm
from tests.gateway.test_interrupt_keeps_parked_internal_wake import _gateway, _event


@pytest.mark.asyncio
async def test_new_stop_blocks_and_preserves_resuming_human():
    adapter, runner, source, key = _gateway()
    state = runner._session_state(key)
    state.conversation.automatic_turns_paused = True
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    async def newer_stop(_key):
        state.persistent.run_generation += 1
    runner._async_session_store.clear_user_stopped.side_effect = newer_stop
    human = _event(source, 'continue', internal=False)
    assert await qm.gate_stopped_event(runner, key, human, adapter)
    assert state.conversation.held_queue == [human]
    assert state.conversation.automatic_turns_paused is True
    assert qm.active_items(runner, key, adapter) == []


@pytest.mark.asyncio
async def test_idle_stop_invalidates_inflight_queue_release():
    adapter, runner, source, key = _gateway()
    state = runner._session_state(key)
    state.conversation.automatic_turns_paused = True
    wake = _event(source, 'completion', internal=True)
    state.conversation.held_queue.append(wake)
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    runner._async_session_store.get_or_create_session.return_value = SimpleNamespace(session_key=key)
    runner._mark_user_stopped = AsyncMock()
    runner._same_chat_runs = lambda *args: []
    runner._sibling_thread_run_keys = lambda *args: []
    runner._chat_scoped_run_keys = lambda *args: []
    async def stop_during_clear(_key):
        await runner._handle_stop_command(_event(source, '/stop', internal=False))
    runner._async_session_store.clear_user_stopped.side_effect = stop_during_clear
    generation = state.persistent.run_generation
    await qm.release(runner, key, source, adapter, busy=False)
    assert state.persistent.run_generation > generation
    assert state.conversation.automatic_turns_paused is True
    assert state.conversation.held_queue == [wake]
    assert qm.active_items(runner, key, adapter) == []
