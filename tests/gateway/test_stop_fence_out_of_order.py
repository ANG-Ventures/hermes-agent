"""Out-of-order stop writes and a failing resume clear must not hand a newer stop's state away.

1. ``mark_user_stopped`` writes can land out of order (each awaits a transcript read first). A
   delayed stop claimed at an OLDER generation must not lower the fence a newer stop raised, or an
   older resume then erases the newer stop.
2. If ``clear_user_stopped`` raises after a newer /stop took ownership of the held store, the
   resuming human belongs to that stop; the failure path must not pull it back out.
"""
from unittest.mock import AsyncMock

import pytest

from gateway import queue_mgmt as qm
from tests.gateway.test_interrupt_keeps_parked_internal_wake import _gateway, _event
from tests.gateway.test_user_stopped_never_resumes import _runner, _source


def test_delayed_older_stop_does_not_lower_newer_fence(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    assert store.mark_user_stopped(entry.session_key, generation=6) is True
    # The stop claimed at generation 5 lands late.
    store.mark_user_stopped(entry.session_key, generation=5)
    # A resume that only observed generation 5 must not erase the generation-6 stop.
    assert store.clear_user_stopped(entry.session_key, generation=5) is False
    assert store._entries[entry.session_key].user_stopped_at is not None
    assert store.clear_user_stopped(entry.session_key, generation=6) is True
    db.close()


def test_untracked_stop_does_not_lower_newer_fence(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    assert store.mark_user_stopped(entry.session_key, generation=6) is True
    assert store.mark_user_stopped(entry.session_key) is True
    assert store.clear_user_stopped(entry.session_key, generation=5) is False
    assert store._entries[entry.session_key].user_stopped_at is not None
    db.close()


@pytest.mark.asyncio
async def test_failed_clear_after_newer_stop_keeps_human_held():
    adapter, runner, source, key = _gateway()
    state = runner._session_state(key)
    state.conversation.automatic_turns_paused = True
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store

    async def newer_stop_then_fail(_key, **_kw):
        state.persistent.run_generation += 1  # the newer /stop now owns the held store
        raise OSError("state.db write failed")

    runner._async_session_store.clear_user_stopped.side_effect = newer_stop_then_fail
    human = _event(source, 'continue', internal=False)
    with pytest.raises(OSError):
        await qm.gate_stopped_event(runner, key, human, adapter)
    assert state.conversation.held_queue == [human]
    assert state.conversation.automatic_turns_paused is True


@pytest.mark.asyncio
async def test_failed_clear_without_invalidation_still_unparks():
    adapter, runner, source, key = _gateway()
    state = runner._session_state(key)
    state.conversation.automatic_turns_paused = True
    runner._async_session_store = AsyncMock()
    runner.session_store = runner._async_session_store._store
    runner._async_session_store.clear_user_stopped.side_effect = OSError("boom")
    human = _event(source, 'continue', internal=False)
    with pytest.raises(OSError):
        await qm.gate_stopped_event(runner, key, human, adapter)
    assert human not in state.conversation.held_queue
