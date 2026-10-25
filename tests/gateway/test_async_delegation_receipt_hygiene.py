"""Receipt hygiene after #2291 (t_3572d4df): one test per Prism key.

Real JSON registry + real SessionDB + real legacy SQLite mirror in a temp HERMES_HOME (the
``isolated_registry`` autouse fixture); the steer buffer is the real ``InterruptControlMixin``.
"""
import asyncio
import threading
import time

import pytest

from gateway import admitted_completion_sweep as sweep
from tests.gateway.test_admitted_completion_sweep import KEY, _admitted, _persist_user_row, _run, _stamp_updated_at
from tests.gateway.test_completion_delivery import (
    _async_event,
    _json_outbox_states,
    _persist_pending_completion,
    _seed_json_outbox,
    isolated_registry,  # noqa: F401 — autouse fixture for real temp storage
)
from tools import async_delegation as ad
from tools import async_delegation_store as store


# --- (a) 2fcb0b3d9b47: the sweep owns expiry; the boot replay never drops a consumed wake ----------

def test_consumed_wake_past_the_ceiling_is_confirmed_not_dropped_by_the_boot_replay(
        tmp_path, isolated_registry, monkeypatch):
    """Admitted, its rendered Event row persisted, gateway down past 48 h: the boot replay leaves it to
    the sweep, which confirms ``delivered``. Red at #2291: the replay's store-level expiry dropped it."""
    runner, _adapter, event = _admitted(tmp_path, isolated_registry)
    _persist_user_row(tmp_path, "parent-sess", f"[x] verdict\nEvent: {event['event_id']}")
    later = time.time() + store.ADMITTED_MAX_AGE_SECONDS + 3600
    monkeypatch.setattr(store.time, "time", lambda: later)
    assert ad.enqueue_pending_outbox(current_boot_id="next-boot", profile_home=tmp_path) == 0
    assert _json_outbox_states(tmp_path) == ["admitted"]
    assert _run(runner, later) == 0
    assert _json_outbox_states(tmp_path) == ["delivered"]


def test_unreadable_transcript_past_the_ceiling_is_dropped_unverified_by_the_sweep(tmp_path, isolated_registry):
    runner, _adapter, _event = _admitted(tmp_path, isolated_registry)
    (tmp_path / "state.db").rename(tmp_path / "state.db.gone")
    assert _run(runner, time.time() + store.ADMITTED_MAX_AGE_SECONDS + 60) == 0
    (ev,) = [e for r in store.read_registry(profile_home=tmp_path)["records"].values() for e in r["outbox"]]
    assert (ev["state"], ev["drop_reason"]) == ("dropped", "admitted_age_ceiling_unverified")


def test_store_expiry_stays_off_while_a_sweeper_serves_the_home(tmp_path, isolated_registry, monkeypatch):
    _admitted(tmp_path, isolated_registry)
    later = time.time() + store.ADMITTED_MAX_AGE_SECONDS + 60
    monkeypatch.setattr(store.time, "time", lambda: later)
    store.note_sweeper_serves(tmp_path)  # a sweeper ticked recently (every 60 s while served)
    _stamp_updated_at(tmp_path, later)  # seeded rows carry no updated_at: keep _prune's retention off
    store.persist_dispatch(delegation_id="deleg_other", durable_spec={}, boot_id="b",
                           dispatched_at=later, profile_home=tmp_path)
    assert _json_outbox_states(tmp_path) == ["admitted"]


# --- 7aaf4b17dca7: admitted_at goes through the numeric/quarantine boundary ---------------------

def test_unconvertible_admitted_at_is_quarantined_and_healthy_siblings_replay(tmp_path, isolated_registry):
    """Red at #2291: ``now - admitted_at`` raised OverflowError and aborted the whole profile's replay."""
    bad = dict(_async_event("deleg_bad"))
    good = dict(_async_event("deleg_good"))
    good["created_at"] = None
    _seed_json_outbox([bad, good], tmp_path)
    with store.locked_registry(tmp_path) as registry:
        (event,) = registry["records"]["deleg_bad"]["outbox"]
        event.update(state="admitted", admitted_at=-(10 ** 400))
    assert ad.enqueue_pending_outbox(current_boot_id="next-boot", profile_home=tmp_path) == 1
    assert isolated_registry.completion_queue.get_nowait()["delegation_id"] == "deleg_good"
    # The unserved-home expiry (a CLI dispatch) does not trip on it either.
    (tmp_path / "state" / "async-delegations.sweep").unlink()
    record, _ = store.persist_dispatch(delegation_id="deleg_new", durable_spec={}, boot_id="b",
                                       dispatched_at=time.time(), profile_home=tmp_path)
    assert record is not None


# --- (b) 5723164466ce: internal events never re-run @-reference expansion -----------------------

@pytest.mark.asyncio
async def test_internal_wake_text_is_not_reexpanded_for_context_references(monkeypatch):
    """A rendered completion holding literal ``@file:`` refs is data. Red at #2291: the expansion ran,
    the preprocessor refused (over budget), the prepare step returned None and the wake never ran."""
    from agent.context_references import ContextReferenceResult
    import agent.context_references as ctx_mod
    from gateway.platforms.event import MessageEvent
    from tests.gateway.test_context_ref_expansion_runtime import _make_runner, _patch_runtime_resolution, _source
    runner, source = _make_runner(), _source()
    _patch_runtime_resolution(monkeypatch)
    calls = []

    async def _refuse(message, **_kw):
        calls.append(message)
        return ContextReferenceResult(message=message, original_message=message, blocked=True,
                                      warnings=["context injection refused"])

    monkeypatch.setattr(ctx_mod, "preprocess_context_references_async", _refuse)
    text = "[ASYNC DELEGATION BATCH COMPLETE — d] see @file:a.txt @file:b.txt\nEvent: d:terminal:g1"
    wake = MessageEvent(text=text, source=source, internal=True)
    assert await runner._prepare_inbound_message_text(event=wake, source=source, history=[]) == text
    assert calls == []
    human = MessageEvent(text=text, source=source)
    assert await runner._prepare_inbound_message_text(event=human, source=source, history=[]) is None
    assert calls == [text]


# --- (c) / (d) steer provenance -------------------------------------------------------------------

class _Agent:
    """The real steer buffer and hard-cancel path (``InterruptControlMixin``) and nothing else."""

    def __init__(self):
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._pending_redirect = None
        self._pending_redirect_lock = threading.Lock()
        self._interrupt_requested = False
        self._interrupt_message = self._tool_interrupt_reason = None
        self._hard_interrupt_requested = threading.Event()
        self._interrupt_thread_signal_pending = False
        self._execution_thread_id = None

    from agent.interrupt_control import InterruptControlMixin as _icm
    steer = _icm.steer
    _drain_pending_steer = _icm._drain_pending_steer
    clear_interrupt = _icm.clear_interrupt
    del _icm


def _gateway_steer(agent, text, *, internal):
    from types import SimpleNamespace
    from gateway.run_busy import GatewayBusySessionMixin
    runner = SimpleNamespace(_steer_active_subagents=lambda running_agent, text: 0)
    assert GatewayBusySessionMixin._steer_running_agent(runner, agent, text, internal=internal)


def _raw_steer(agent, text):
    """A buffer write no wrapper sees (the class method itself)."""
    from agent.interrupt_control import InterruptControlMixin
    assert InterruptControlMixin.steer(agent, text)


def _raw_wipe(agent):
    with agent._pending_steer_lock:
        agent._pending_steer = None


def test_direct_agent_steer_is_recorded_human_and_resets_stale_entries():
    """(c): a producer that calls ``agent.steer`` itself (kanban comment-inject, the redirect re-steer)
    into an emptied buffer reads human. Red at #2291: it inherited the stale internal entry."""
    from gateway.run_busy import leftover_steer_is_internal
    agent = _Agent()
    _gateway_steer(agent, "continue", internal=True)
    _raw_wipe(agent)
    agent.steer("continue")
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is False


def test_direct_agent_steer_alongside_an_internal_one_reads_human():
    from gateway.run_busy import leftover_steer_is_internal
    agent = _Agent()
    _gateway_steer(agent, "wake ready", internal=True)
    agent.steer("wake ready")
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is False


def test_hard_cancel_drops_the_entries_of_the_wiped_buffer():
    """7c35492cc023: a hard cancel wipes the buffer without a drain; the next raw write with the same
    text must not inherit the internal entry. Red at #2291."""
    from gateway.run_busy import leftover_steer_is_internal
    agent = _Agent()
    _gateway_steer(agent, "continue", internal=True)
    agent.clear_interrupt(hard_cancel=True)
    assert agent._pending_steer is None
    _raw_steer(agent, "continue")
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is False


def test_soft_clear_keeps_the_buffer_and_its_provenance():
    from gateway.run_busy import leftover_steer_is_internal
    agent = _Agent()
    _gateway_steer(agent, "continue", internal=True)
    agent.clear_interrupt()
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is True


def test_turn_end_with_an_empty_buffer_retires_live_entries():
    """43b1ce3c30ef: Prism repro. ``_pending_steer = None`` -> turn end (no leftover) -> raw write with the
    same text -> classify: human. Red at #2291."""
    from gateway.run_busy import leftover_steer_is_internal
    agent = _Agent()
    _gateway_steer(agent, "continue", internal=True)
    _raw_wipe(agent)
    assert leftover_steer_is_internal(agent, None) is False
    _raw_steer(agent, "continue")
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is False


def test_requeued_internal_steer_keeps_its_provenance():
    """96e62751d3d9: the pre-API drain on a turn's first iteration has no tool row, so it requeues the
    text; the finalizer's drain must still read it internal. Red at #2291 (entries were left behind)."""
    from agent.agent_runtime_helpers import requeue_pending_steer
    from gateway.run_busy import leftover_steer_is_internal
    agent = _Agent()
    _gateway_steer(agent, "wake ready", internal=True)
    drained = agent._drain_pending_steer()
    requeue_pending_steer(agent, drained)
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is True


def test_requeued_steer_behind_a_newer_human_one_reads_human():
    from agent.agent_runtime_helpers import requeue_pending_steer
    from gateway.run_busy import leftover_steer_is_internal
    agent = _Agent()
    _gateway_steer(agent, "wake ready", internal=True)
    drained = agent._drain_pending_steer()
    _gateway_steer(agent, "please explain", internal=False)
    requeue_pending_steer(agent, drained)
    assert agent._pending_steer == "please explain\nwake ready"
    assert leftover_steer_is_internal(agent, agent._drain_pending_steer()) is False


# --- 9057f657dd27: the reopen also revokes a still-pending mirror claim ---------------------------

def test_reopen_revokes_a_pending_mirror_claim_left_by_a_crash(tmp_path, isolated_registry):
    """Claim, JSON admission committed, crash before the SQLite receipt: the mirror is ``pending`` under
    the dead consumer's claim. The re-injected wake must win its own claim inside the 300 s lease.
    Red at #2291: the reset matched only ``delivered`` rows, the claim failed, the wake was discarded."""
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent-sess", source="telegram", session_key=KEY)
    db.close()
    event = dict(_async_event("deleg_crash"), parent_session_id="parent-sess")
    event["dispatched_at"], event["completed_at"] = time.time() - 12, time.time()
    _seed_json_outbox([event], tmp_path)
    _persist_pending_completion(event)
    assert ad.claim_completion_delivery("deleg_crash", "claim-dead", event=event)
    assert ad.admit_event_outbox(event, session_key=KEY, session_id="parent-sess")
    # crash: complete_completion_delivery never ran
    assert ad.get_durable_delegation("deleg_crash")["delivery_state"] == "pending"
    reopened = ad.reopen_admitted_event(event["event_id"], tmp_path, reason="admitted_not_in_transcript")
    assert reopened is not None and _json_outbox_states(tmp_path) == ["pending"]
    assert ad.claim_completion_delivery("deleg_crash", "claim-new", event=reopened)


def test_reopen_leaves_a_newer_generations_mirror_alone(tmp_path, isolated_registry):
    event = dict(_async_event("deleg_gen"), parent_session_id="parent-sess")
    event["dispatched_at"], event["completed_at"] = time.time() - 12, time.time()
    _seed_json_outbox([event], tmp_path)
    _persist_pending_completion(event)
    assert ad.claim_completion_delivery("deleg_gen", "claim-live", event=event)
    assert ad.admit_event_outbox(event, session_key=KEY, session_id="parent-sess")
    ad._update_delivery("UPDATE async_delegations SET event_json=json_set(event_json, '$.event_id', ?) "
                        "WHERE delegation_id=?", ("deleg_gen:terminal:g2", "deleg_gen"))
    assert ad.reopen_admitted_event(event["event_id"], tmp_path, reason="x") is not None
    assert not ad.claim_completion_delivery("deleg_gen", "claim-other", event=None)
