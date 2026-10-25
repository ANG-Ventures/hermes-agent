"""Ack at consumption: an admitted async-delegation wake is confirmed from the transcript or re-injected
exactly once (t_97a879af).

Real JSON registry + real SessionDB in a temp HERMES_HOME; the delivery path is the real
``_deliver_completion_notification`` through an admitting fake transport.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import admitted_completion_sweep as sweep
from tests.gateway.test_completion_delivery import (
    AdmittingHandler,
    _async_event,
    _json_outbox_states,
    _runner,
    _seed_json_outbox,
    isolated_registry,  # noqa: F401 — autouse fixture for real temp storage
)
from tools import async_delegation as ad

KEY = "agent:main:telegram:dm:12345:678"


def _admitted(tmp_path, isolated_registry, *, parent="parent-sess"):
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.create_session(parent, source="telegram", session_key=KEY)
    db.close()
    event = dict(_async_event("deleg_lost"), parent_session_id=parent)
    _seed_json_outbox([event], tmp_path)
    adapter = SimpleNamespace(handle_message=AdmittingHandler(), _pending_messages={}, _active_sessions={})
    runner = _runner(adapter)
    runner._session_db = None
    runner._classify_completion_target = AsyncMock(return_value="deliver")
    runner._gateway_started_at = 0.0
    runner._is_session_running = lambda key: False
    runner._peek_session_state = lambda key: None
    assert asyncio.run(runner._deliver_completion_notification(
        f"[ASYNC DELEGATION BATCH COMPLETE — {event['delegation_id']}] verdict\nEvent: {event['event_id']}",
        event)) is True
    assert _json_outbox_states(tmp_path) == ["admitted"]
    return runner, adapter, event


def _persist_user_row(tmp_path, session_id, text):
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.append_message(session_id, "user", text)
    db.close()


def _run(runner, now):
    return asyncio.run(sweep.sweep_admitted_completions(runner, now=now))


def test_admitted_wake_in_transcript_is_confirmed_delivered(tmp_path, isolated_registry):
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    _persist_user_row(tmp_path, "parent-sess", f"[Sat] [ASYNC DELEGATION BATCH COMPLETE — deleg_lost] verdict\nEvent: {event['event_id']}")
    assert _run(runner, time.time() + 600) == 0
    assert _json_outbox_states(tmp_path) == ["delivered"]
    assert isolated_registry.completion_queue.empty()


def test_admitted_wake_missing_from_transcript_is_reinjected_exactly_once(tmp_path, isolated_registry):
    """The 10-24 shape: admitted behind a busy turn, then lost in memory. Red before t_97a879af: the row
    read ``delivered`` at admission and nothing ever re-offered it."""
    runner, adapter, event = _admitted(tmp_path, isolated_registry)
    later = time.time() + 600
    assert _run(runner, later) == 0                    # first miss: suspect only
    assert _run(runner, later + 60) == 1               # second miss: re-queued
    assert _json_outbox_states(tmp_path) == ["pending"]
    queued = isolated_registry.completion_queue.get_nowait()
    assert queued["event_id"] == event["event_id"] and isolated_registry.completion_queue.empty()
    # The normal delivery path takes it again (lifecycle dedupe cleared) and admits it once more.
    assert asyncio.run(runner._deliver_completion_notification("again", queued)) is True
    assert adapter.handle_message.await_count == 2
    assert _json_outbox_states(tmp_path) == ["admitted"]
    _persist_user_row(tmp_path, "parent-sess", f"[ASYNC DELEGATION BATCH COMPLETE — deleg_lost] again\nEvent: {event['event_id']}")
    assert _run(runner, later + 120) == 0
    assert _json_outbox_states(tmp_path) == ["delivered"]
    assert isolated_registry.completion_queue.empty()


def test_wake_still_parked_in_the_adapter_slot_is_left_alone(tmp_path, isolated_registry):
    runner, adapter, _event = _admitted(tmp_path, isolated_registry)
    adapter._pending_messages[KEY] = SimpleNamespace(text=f"x\nEvent: {_event['event_id']}")
    later = time.time() + 600
    assert _run(runner, later) == 0 and _run(runner, later + 60) == 0
    assert _json_outbox_states(tmp_path) == ["admitted"]


def test_running_session_defers_the_verdict(tmp_path, isolated_registry):
    runner, _adapter, _event = _admitted(tmp_path, isolated_registry)
    runner._is_session_running = lambda key: key == KEY
    later = time.time() + 600
    assert _run(runner, later) == 0 and _run(runner, later + 60) == 0
    assert _json_outbox_states(tmp_path) == ["admitted"]


def test_young_admission_is_not_judged(tmp_path, isolated_registry):
    runner, _adapter, _event = _admitted(tmp_path, isolated_registry)
    now = time.time()
    assert _run(runner, now) == 0 and _run(runner, now + 1) == 0
    assert _json_outbox_states(tmp_path) == ["admitted"]


def test_reinject_cap_drops_instead_of_looping(tmp_path, isolated_registry, monkeypatch):
    runner, _adapter, _event = _admitted(tmp_path, isolated_registry)
    monkeypatch.setattr(sweep, "REINJECT_CAP", 0)
    later = time.time() + 600
    _run(runner, later)
    assert _run(runner, later + 60) == 0
    assert _json_outbox_states(tmp_path) == ["dropped"]
    assert isolated_registry.completion_queue.empty()


def test_admitted_is_never_replayed_by_the_boot_outbox_scan(tmp_path, isolated_registry):
    _admitted(tmp_path, isolated_registry)
    assert ad.enqueue_pending_outbox(current_boot_id="next-boot", profile_home=tmp_path) == 0


def test_previous_generation_row_does_not_confirm_a_lost_new_generation(tmp_path, isolated_registry):
    """Prism dab588615af4: the receipt marker is generation-specific."""
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    _persist_user_row(tmp_path, "parent-sess", "[ASYNC DELEGATION BATCH COMPLETE — deleg_lost] old\n"
                      "Event: deleg_lost:terminal:g0")
    later = time.time() + 600
    _run(runner, later)
    assert _run(runner, later + 60) == 1
    assert _json_outbox_states(tmp_path) == ["pending"]


def test_receipt_found_in_the_keys_current_session_after_idle_retarget(tmp_path, isolated_registry):
    """Prism 16dd0448c670: an idle-ended parent's wake runs in the key's older-but-current session."""
    from hermes_state import SessionDB
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("current-sess", source="telegram", session_key=KEY)
    db.close()
    _persist_user_row(tmp_path, "current-sess", f"[x]\nEvent: {event['event_id']}")
    assert _run(runner, time.time() + 600) == 0
    assert _json_outbox_states(tmp_path) == ["delivered"]


def test_unreadable_candidate_does_not_starve_the_rest(tmp_path, isolated_registry, monkeypatch):
    """Prism 2d3fbc7a4c3e: a lookup failure skips that candidate only and is not evidence of loss."""
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    bad = tmp_path / "bad-home"
    real = sweep._load_admitted
    monkeypatch.setattr(sweep, "_load_admitted", lambda homes: [(bad, dict(event))] + real(homes))
    real_probe = sweep._in_transcript

    def probe(home, ev):
        if home == bad:
            raise RuntimeError("database disk image is malformed")
        return real_probe(home, ev)

    monkeypatch.setattr(sweep, "_in_transcript", probe)
    later = time.time() + 600
    _run(runner, later)
    assert _run(runner, later + 60) == 1  # the healthy profile's lost wake still recovers
    assert _json_outbox_states(tmp_path) == ["pending"]


def test_same_event_id_in_two_profiles_keeps_separate_suspects(tmp_path, isolated_registry, monkeypatch):
    """Prism dffb6cec8d92: a parked wake in profile B must not reset profile A's suspect marker."""
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    other = tmp_path / "other-home"
    other_event = {**event, "admitted_session_key": "agent:main:telegram:dm:999:1",
                   "payload": {**event.get("payload", {}), "session_key": "agent:main:telegram:dm:999:1"}}
    real = sweep._load_admitted
    monkeypatch.setattr(sweep, "_load_admitted", lambda homes: real(homes) + [(other, other_event)])
    monkeypatch.setattr(sweep, "_in_transcript", lambda home, ev: False)
    runner._is_session_running = lambda key: key == "agent:main:telegram:dm:999:1"
    later = time.time() + 600
    _run(runner, later)
    assert _run(runner, later + 60) == 1


def test_missing_transcript_store_is_unknown_not_lost(tmp_path, isolated_registry):
    """Prism a54f28d4d21a: no state.db means consumption is unverifiable; never re-inject on it."""
    runner, _adapter, _event = _admitted(tmp_path, isolated_registry)
    (tmp_path / "state.db").rename(tmp_path / "state.db.gone")
    later = time.time() + 600
    assert _run(runner, later) == 0 and _run(runner, later + 60) == 0
    assert _json_outbox_states(tmp_path) == ["admitted"]


def test_admitted_at_is_taken_before_the_adapter_hand_off(tmp_path, isolated_registry):
    """Prism 9a4d46e375b4: the stamp predates the receipt write, so a row persisted during a slow
    receipt still falls inside the probe window."""
    from tools import async_delegation_store as store
    before = time.time()
    _admitted(tmp_path, isolated_registry)
    (event,) = store.admitted_events(tmp_path)
    assert before <= event["admitted_at"] <= time.time()
    payload_free = {k: v for k, v in event.items() if k != "payload"}
    assert payload_free["admitted_session_key"] == KEY
