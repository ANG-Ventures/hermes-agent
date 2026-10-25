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


def test_g1_receipt_is_not_confirmed_by_a_g10_row(tmp_path, isolated_registry):
    """Prism 16dd0448c670 (t_506536db): the needle is line-terminated, so an admitted ``...:g1`` wake is
    not confirmed by a transcript row that carries only ``...:g10``."""
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    assert event["event_id"].endswith(":g1")
    _persist_user_row(tmp_path, "parent-sess", f"[x] other attempt\nEvent: {event['event_id']}0")
    _persist_user_row(tmp_path, "parent-sess", f"[x] other attempt\nEvent: {event['event_id']}0\ntrailer")
    later = time.time() + 600
    _run(runner, later)
    assert _run(runner, later + 60) == 1
    assert _json_outbox_states(tmp_path) == ["pending"]


def test_receipt_needle_matches_at_end_of_content_and_mid_content(tmp_path, isolated_registry):
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    _persist_user_row(tmp_path, "parent-sess", f"[x]\nEvent: {event['event_id']}\nmore text")
    assert _run(runner, time.time() + 600) == 0
    assert _json_outbox_states(tmp_path) == ["delivered"]


def test_wake_parked_as_g10_does_not_shield_a_lost_g1(tmp_path, isolated_registry):
    runner, adapter, event = _admitted(tmp_path, isolated_registry)
    adapter._pending_messages[KEY] = SimpleNamespace(text=f"x\nEvent: {event['event_id']}0")
    later = time.time() + 600
    _run(runner, later)
    assert _run(runner, later + 60) == 1


def test_admitted_row_with_missing_store_is_dropped_at_the_age_ceiling(tmp_path, isolated_registry, caplog):
    """t_506536db: unverifiable (no state.db) stays admitted only until the ceiling, then dropped + logged."""
    from tools import async_delegation_store as store
    runner, _adapter, _event = _admitted(tmp_path, isolated_registry)
    (tmp_path / "state.db").rename(tmp_path / "state.db.gone")
    past = time.time() + store.ADMITTED_MAX_AGE_SECONDS + 60
    with caplog.at_level("WARNING"):
        assert _run(runner, past) == 0
    assert _json_outbox_states(tmp_path) == ["dropped"]
    assert "async_delegation_wake_lost" in caplog.text
    assert isolated_registry.completion_queue.empty()


def test_admitted_row_on_an_unserved_profile_is_dropped_by_the_registry_scan(tmp_path, isolated_registry, monkeypatch, caplog):
    """No sweeper serves this home any more; the next registry write/scan expires it at the ceiling."""
    from tools import async_delegation_store as store
    _admitted(tmp_path, isolated_registry)
    real_time = time.time
    monkeypatch.setattr(store.time, "time", lambda: real_time() + store.ADMITTED_MAX_AGE_SECONDS + 60)
    with caplog.at_level("WARNING"):
        assert ad.enqueue_pending_outbox(current_boot_id="next-boot", profile_home=tmp_path) == 0
    assert _json_outbox_states(tmp_path) == ["dropped"]
    assert "async_delegation_wake_lost" in caplog.text


def test_admitted_row_under_the_ceiling_is_kept(tmp_path, isolated_registry):
    from tools import async_delegation_store as store
    _admitted(tmp_path, isolated_registry)
    assert ad.enqueue_pending_outbox(current_boot_id="next-boot", profile_home=tmp_path) == 0
    assert _json_outbox_states(tmp_path) == ["admitted"]
    assert store.ADMITTED_MAX_AGE_SECONDS > sweep.MIN_AGE_S


def test_failed_legacy_reset_leaves_the_wake_retryable_as_admitted(tmp_path, isolated_registry, monkeypatch):
    """Prism 56af331e5986: a SQLite mirror reset that raises must not strand the JSON event as pending."""
    import sqlite3
    runner, _adapter, _event = _admitted(tmp_path, isolated_registry)
    real = ad._update_delivery

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ad, "_update_delivery", locked)
    later = time.time() + 600
    _run(runner, later)
    assert _run(runner, later + 60) == 0
    assert _json_outbox_states(tmp_path) == ["admitted"]
    assert isolated_registry.completion_queue.empty()
    monkeypatch.setattr(ad, "_update_delivery", real)
    _run(runner, later + 120)
    assert _run(runner, later + 180) == 1
    assert _json_outbox_states(tmp_path) == ["pending"]


# Prism 515bc357c1a8 (t_506536db r2): the batch formatter glues "   Total duration: Ns" onto the
# ``Event: <id>`` line, so the needle must also end at a space, not only at a newline.

def _batch_wake_event(event_id):
    return {"type": "async_delegation", "delegation_id": event_id.split(":")[0], "event_id": event_id,
            "is_batch": True, "goals": ["g"], "results": [{"task_index": 0, "status": "completed", "summary": "ok"}],
            "total_duration_seconds": 42, "role": "leaf", "model": "m", "completed_at": time.time()}


def test_rendered_BATCH_wake_is_confirmed(tmp_path):
    from tools.process_registry_notifications import _format_async_delegation
    from hermes_state import SessionDB
    evt = _batch_wake_event("deleg_x:terminal:g1")
    text = _format_async_delegation(evt)
    assert f"{ad.admission_receipt_needle(evt)}\n" not in text  # the glued shape this test pins
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1", source="telegram", session_key="k1")
    db.append_message("s1", "user", text)
    assert db.user_message_contains(["s1"], ad.admission_receipt_needle(evt), session_key="k1")
    assert not db.user_message_contains(["s1"], "Event: deleg_x:terminal:g", session_key="k1")


def test_parked_batch_wake_is_seen():
    from tools.process_registry_notifications import _format_async_delegation
    evt = _batch_wake_event("deleg_x:terminal:g1")
    ev = SimpleNamespace(text=_format_async_delegation(evt))
    runner = SimpleNamespace(
        _is_session_running=lambda k: False,
        _peek_session_state=lambda k: SimpleNamespace(conversation=SimpleNamespace(queued_events=[ev], held_queue=[])),
        adapters={}, _profile_adapters={})
    assert sweep._parked_in_memory(runner, "k1", ad.admission_receipt_needle(evt))
    assert not sweep._parked_in_memory(runner, "k1", "Event: deleg_x:terminal:g")


def test_consumed_batch_wake_is_delivered_with_zero_reinjects(tmp_path, isolated_registry):
    """Sweep level: an admitted batch wake whose rendered text is in the transcript is confirmed, never
    re-injected, across two sweeps (the second sweep is where a miss would re-queue it)."""
    from tools.process_registry_notifications import _format_async_delegation
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    rendered = _format_async_delegation(dict(_batch_wake_event(event["event_id"]),
                                             delegation_id=event["delegation_id"]))
    assert f"Event: {event['event_id']}   Total duration:" in rendered
    _persist_user_row(tmp_path, "parent-sess", rendered)
    later = time.time() + 600
    assert _run(runner, later) == 0 and _run(runner, later + 60) == 0
    assert _json_outbox_states(tmp_path) == ["delivered"]
    assert isolated_registry.completion_queue.empty()
