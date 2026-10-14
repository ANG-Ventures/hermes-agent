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


def _durable_stop(db, store, key) -> bool:
    """Whether state.db's routing row for ``key`` carries a stop marker."""
    import json

    raw = db.load_gateway_routing_entries(scope=store._routing_scope()).get(key)
    return raw is not None and json.loads(raw).get("user_stopped_at") is not None


def test_late_older_stop_waits_for_blocked_newer_write(tmp_path, monkeypatch):
    """The older stop's True is a /stop ack: it must not return while the newer marker is unwritten."""
    import threading

    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    key = store.get_or_create_session(_source()).session_key
    real_replace = db.replace_gateway_routing_entries
    entered, release = threading.Event(), threading.Event()

    def blocking_replace(*args, **kwargs):
        if not release.is_set():
            entered.set()
            release.wait(10)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(db, "replace_gateway_routing_entries", blocking_replace)
    newer = threading.Thread(target=store.mark_user_stopped, args=(key,), kwargs={"generation": 6})
    newer.start()
    assert entered.wait(10)
    seen = {}

    def older():
        seen["ok"] = store.mark_user_stopped(key, generation=5)
        seen["durable"] = _durable_stop(db, store, key)

    late = threading.Thread(target=older)
    late.start()
    late.join(0.5)
    release.set()
    late.join(10)
    newer.join(10)
    assert seen == {"ok": True, "durable": True}
    db.close()


def test_late_older_stop_persists_after_newer_write_failed(tmp_path, monkeypatch):
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    key = store.get_or_create_session(_source()).session_key
    real_replace = db.replace_gateway_routing_entries
    real_json = store._save_sessions_json
    failing = {"on": True}

    def maybe_fail(real):
        def wrapper(*args, **kwargs):
            if failing["on"]:
                raise OSError("disk full")
            return real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(db, "replace_gateway_routing_entries", maybe_fail(real_replace))
    monkeypatch.setattr(store, "_save_sessions_json", maybe_fail(real_json))
    with pytest.raises(OSError):
        store.mark_user_stopped(key, generation=6)
    assert not _durable_stop(db, store, key)
    failing["on"] = False
    assert store.mark_user_stopped(key, generation=5) is True
    assert _durable_stop(db, store, key)
    db.close()
