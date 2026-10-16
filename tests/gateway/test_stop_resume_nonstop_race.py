"""A resume racing another invalidation must end the old turn and leave the human correctly placed.

``gate_stopped_event`` awaits ``clear_user_stopped``. Whatever bumps the run generation during that
await (newer /stop, /queue run, /new) must find the human in the held store and dispose of it like
any queued item, and the old turn must not continue with its stale session entry.
"""
from unittest.mock import AsyncMock

import pytest

from gateway import queue_mgmt as qm
from tests.gateway.test_interrupt_keeps_parked_internal_wake import _gateway, _event
from tests.gateway.test_user_stopped_never_resumes import _runner, _source


def _paused(runner, key):
    state = runner._session_state(key)
    state.conversation.automatic_turns_paused = True
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    return state


@pytest.mark.asyncio
async def test_concurrent_release_does_not_strand_human():
    adapter, runner, source, key = _gateway()
    state = _paused(runner, key)

    async def release_during_clear(_key, **_kw):
        state.persistent.run_generation += 1
        qm.restage_held(runner, key, adapter)
        state.conversation.automatic_turns_paused = False

    runner._async_session_store.clear_user_stopped.side_effect = release_during_clear
    human = _event(source, 'continue', internal=False)
    consumed = await qm.gate_stopped_event(runner, key, human, adapter)
    # The old turn must stop; the accepted event must be runnable by a fresh turn.
    assert consumed is True
    assert human not in state.conversation.held_queue
    assert human in qm.active_items(runner, key, adapter)


@pytest.mark.asyncio
async def test_release_during_clear_keeps_fifo_behind_earlier_held_items():
    adapter, runner, source, key = _gateway()
    state = _paused(runner, key)
    earlier = _event(source, 'queued first', internal=False)
    state.conversation.held_queue.append(earlier)

    async def release_during_clear(_key, **_kw):
        state.persistent.run_generation += 1
        qm.restage_held(runner, key, adapter)

    runner._async_session_store.clear_user_stopped.side_effect = release_during_clear
    human = _event(source, 'continue', internal=False)
    assert await qm.gate_stopped_event(runner, key, human, adapter) is True
    assert qm.active_items(runner, key, adapter) == [earlier, human]
    assert state.conversation.held_queue == []


@pytest.mark.asyncio
async def test_reset_during_clear_drops_and_counts_human_not_leaked_into_new_conversation():
    adapter, runner, source, key = _gateway()
    state = _paused(runner, key)

    async def reset_during_clear(_key, **_kw):
        state.persistent.run_generation += 1
        qm.note_reset_drop(runner, key, len(state.conversation.held_queue))
        state.conversation.clear()  # what _clear_conversation_scope(reason="session_reset") does

    runner._async_session_store.clear_user_stopped.side_effect = reset_during_clear
    human = _event(source, 'continue', internal=False)
    assert await qm.gate_stopped_event(runner, key, human, adapter) is True
    assert state.conversation.held_queue == []
    assert qm.active_items(runner, key, adapter) == []
    assert qm.take_reset_drop_suffix(runner, key) != ""


@pytest.mark.asyncio
async def test_clear_failure_leaves_human_where_it_was():
    adapter, runner, source, key = _gateway()
    state = _paused(runner, key)
    runner._async_session_store.clear_user_stopped.side_effect = OSError('disk busy')
    human = _event(source, 'continue', internal=False)
    with pytest.raises(OSError):
        await qm.gate_stopped_event(runner, key, human, adapter)
    assert state.conversation.held_queue == []
    assert state.conversation.automatic_turns_paused is True


@pytest.mark.asyncio
async def test_uncontended_resume_admits_human_once():
    adapter, runner, source, key = _gateway()
    state = _paused(runner, key)
    human = _event(source, 'continue', internal=False)
    assert await qm.gate_stopped_event(runner, key, human, adapter) is False
    assert state.conversation.held_queue == []
    assert qm.active_items(runner, key, adapter) == []
    assert state.conversation.automatic_turns_paused is False
    runner._async_session_store.clear_user_stopped.assert_awaited_once_with(key, generation=0)


def test_older_resume_does_not_erase_newer_stop(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    assert store.mark_user_stopped(entry.session_key, generation=5) is True
    assert store.clear_user_stopped(entry.session_key, generation=4) is False
    assert store._entries[entry.session_key].user_stopped_at is not None
    assert store.clear_user_stopped(entry.session_key, generation=5) is True
    assert store._entries[entry.session_key].user_stopped_at is None
    db.close()


def test_stale_stop_landing_after_resume_does_not_relatch(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    store.clear_user_stopped(entry.session_key, generation=7)
    assert store.mark_user_stopped(entry.session_key, generation=6) is False
    assert store._entries[entry.session_key].user_stopped_at is None
    assert store.mark_user_stopped(entry.session_key, generation=8) is True
    db.close()


@pytest.mark.asyncio
async def test_mark_user_stopped_carries_its_generation(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    runner._session_state(entry.session_key).persistent.run_generation = 3
    runner.session_store.clear_user_stopped(entry.session_key, generation=3)
    # A stop claimed at generation 3 lost to a resume already at 3.
    assert await runner._mark_user_stopped(entry.session_key) is False
    runner._session_state(entry.session_key).persistent.run_generation = 4
    assert await runner._mark_user_stopped(entry.session_key) is True
    db.close()
