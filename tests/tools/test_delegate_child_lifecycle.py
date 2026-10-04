"""Child lifecycle state machine (docs/dev/delegate-child-lifecycle.md).

Prism pre-merge review of ANG-Ventures/hermes-agent#1549 (head 064c531a,
three findings carried from 0d3d7d85) left five P1s on the late path. One
test per finding, then the invariants I1-I3 as properties:

- the edge table: only the documented ``(state, event)`` pairs are legal;
- seeded interleavings of {timeout, steer, schema-fail, correction-timeout,
  late-finish, parent-exit} through the real ``_run_single_child`` and late
  thread, asserting I2 and at-most-one-record (I3) at every step and I1 plus
  exactly-one-record (I3) at the terminal state.

Real imports of tools.delegate_tool against temp fleet homes; only the
agents are stubs (shared with test_delegate_late_completion).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import types

import pytest

from tests.tools.test_delegate_late_completion import (  # noqa: F401
    _Agent,
    _Pool,
    _clean_registry,
    _late_results,
    _registered,
    _wait_until,
    fleet_home,
)

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}


@pytest.fixture
def fast_late(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    # Bounded stop drain (production 5 s). raising=False: the head this
    # file is RED on has no such knob and drains its flat 5 s.
    monkeypatch.setattr(
        delegate_tool, "_LATE_STOP_DRAIN_SECONDS", 0.5, raising=False
    )
    return delegate_tool


def _late_files(home):
    d = home / "cache" / "delegation" / "late"
    return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


# #1549 :3491 ---------------------------------------------------------------
def test_f3491_steer_drained_by_the_stalled_correction_turn_is_kept(
    fleet_home, fast_late
):
    """The correction turn's finalizer drained an accepted steer, then the
    hang ceiling fired; the turn returns that steer inside the stop drain.
    Its result must be read, not discarded."""
    dt = fast_late
    release = threading.Event()
    steered = threading.Event()

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {"final_response": "not json", "completed": True, "api_calls": 3}
        self.frozen_activity_ts = time.time() - 60
        steered.wait(10)
        drained = self._drain_pending_steer()  # the real finalizer's drain
        self.interrupt_seen.wait(10)
        return {
            "final_response": "",
            "completed": False,
            "api_calls": 0,
            "pending_steer": drained,
        }

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3491", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    entry = dt._run_single_child(0, "f3491 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    release.set()
    assert _wait_until(lambda: len(child.calls) == 2)
    assert dt.steer_subagent("sa-0-f3491", "check the replica") is True
    steered.set()

    assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    (late,) = _late_results(parent)
    assert late["status"] == "timeout"
    assert "check the replica" in (late.get("missed_steer") or ""), late


# #1549 :3638 ---------------------------------------------------------------
def test_f3638_teardown_waits_for_a_turn_that_outlives_the_stop_drain(
    fleet_home, fast_late
):
    """The drain expires with the correction turn still in non-cancellable
    I/O. The result is delivered on time, but child.close() (which closes
    the child's SessionDB) waits for the turn to exit."""
    dt = fast_late
    release = threading.Event()
    unblock = threading.Event()
    order = []

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {"final_response": "not json", "completed": True, "api_calls": 3}
        self.frozen_activity_ts = time.time() - 60
        unblock.wait(30)  # ignores the interrupt
        # Unwinding persists the rest of the transcript (real AIAgent:
        # its owned SessionDB, which close() closes).
        if db["closed"]:
            db["lost_writes"] += 1
        order.append("turn_exited")
        return {"final_response": "", "completed": False, "api_calls": 0}

    db = {"closed": False, "lost_writes": 0}

    def _close():
        db["closed"] = True
        order.append("close")

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3638", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    child.close = _close
    pool = _Pool()
    child._credential_pool = pool
    try:
        entry = dt._run_single_child(0, "f3638 goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        release.set()
        assert _wait_until(lambda: _late_results(parent), timeout=15.0)
        (late,) = _late_results(parent)
        assert late["status"] == "timeout"
        # Lease and registry go at the stop decision (the #1535 contract).
        assert _wait_until(lambda: pool.released == ["cred-1"])
        assert not _registered("sa-0-f3638")
        time.sleep(0.3)
        assert "close" not in order, f"closed under a live turn: {order}"
    finally:
        unblock.set()
    assert _wait_until(lambda: "close" in order, timeout=5.0)
    assert order == ["turn_exited", "close"], order
    assert db["lost_writes"] == 0


# #1549 :3491 window (Momus r1 CB-1) ------------------------------------------
def test_turn_exiting_between_drain_expiry_and_teardown_keeps_its_steer(
    fleet_home, fast_late, monkeypatch
):
    """The live turn misses the stop drain, then exits while the result is
    being persisted, returning a finalizer-drained steer. Nothing may skip
    reading it."""
    dt = fast_late
    unblock = threading.Event()
    exited = threading.Event()
    steered = threading.Event()

    def _hang(self):
        steered.wait(10)
        pending = self._drain_pending_steer()  # the finalizer's drain
        self.frozen_activity_ts = time.time() - 60
        unblock.wait(30)
        try:
            return {
                "final_response": "",
                "completed": False,
                "api_calls": 1,
                "pending_steer": pending,
            }
        finally:
            exited.set()

    real_record = dt._record_late_result

    def _slow_record(*a, **kw):
        unblock.set()  # the turn exits during persist
        exited.wait(5)
        time.sleep(0.1)
        return real_record(*a, **kw)

    monkeypatch.setattr(dt, "_record_late_result", _slow_record)
    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-window", parent=parent, api_calls=1, behavior=_hang)
    closed = []
    child.close = lambda: closed.append(1)
    try:
        entry = dt._run_single_child(0, "window goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        assert dt.steer_subagent("sa-0-window", "drained in the window") is True
        steered.set()
        assert _wait_until(lambda: closed, timeout=10.0)
    finally:
        unblock.set()
    (late,) = _late_results(parent)
    assert late.get("missed_steer") == "drained in the window", late
    assert _late_files(fleet_home) == [f"{late['late_result_id']}.json"]


# #1549 :3478 ---------------------------------------------------------------
def test_f3478_stop_drain_targets_the_running_correction_turn(fleet_home, fast_late):
    """The first turn is long finished; the correction turn unwinds
    cooperatively after the interrupt. Teardown follows ITS exit."""
    dt = fast_late
    release = threading.Event()
    order = []

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {"final_response": "not json", "completed": True, "api_calls": 3}
        self.frozen_activity_ts = time.time() - 60
        self.interrupt_seen.wait(10)
        time.sleep(0.2)
        order.append("turn_exited")
        return {"final_response": "", "completed": False, "api_calls": 0}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3478", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    child.close = lambda: order.append("close")
    entry = dt._run_single_child(0, "f3478 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    release.set()
    assert _wait_until(lambda: "close" in order, timeout=10.0)
    assert order == ["turn_exited", "close"], order


# #1549 :3434 ---------------------------------------------------------------
def test_f3434_first_turn_pending_steer_kept_when_correction_hangs(
    fleet_home, fast_late
):
    dt = fast_late
    release = threading.Event()

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {
                "final_response": "not json",
                "completed": True,
                "api_calls": 3,
                "pending_steer": self._drain_pending_steer(),
            }
        self.frozen_activity_ts = time.time() - 60
        self.interrupt_seen.wait(10)
        return {"final_response": "", "completed": False, "api_calls": 0}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3434", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    entry = dt._run_single_child(0, "f3434 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    assert dt.steer_subagent("sa-0-f3434", "use the staging db") is True
    release.set()
    assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    (late,) = _late_results(parent)
    assert late["status"] == "timeout"
    assert "schema-correction" in late["error"]
    assert late.get("missed_steer") == "use the staging db", late


# #1549 :3494 ---------------------------------------------------------------
def test_f3494_accepted_steer_kept_when_correction_hits_the_wall(
    fleet_home, fast_late, monkeypatch
):
    """Correction turn keeps making progress until the WALL ceiling. Both the
    first turn's finalizer steer and a steer still queued in the child are
    reported."""
    dt = fast_late
    monkeypatch.setattr(dt, "_get_child_max_wall_seconds", lambda ct: 1.5)
    release = threading.Event()

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {
                "final_response": "not json",
                "completed": True,
                "api_calls": 3,
                "pending_steer": self._drain_pending_steer(),
            }
        self.interrupt_seen.wait(10)  # busy (activity advances) until stopped
        return {"final_response": "", "completed": False, "api_calls": 0}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3494", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    entry = dt._run_single_child(0, "f3494 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    assert dt.steer_subagent("sa-0-f3494", "first-turn steer") is True
    release.set()
    assert _wait_until(lambda: len(child.calls) == 2)
    assert dt.steer_subagent("sa-0-f3494", "queued steer") is True
    assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    (late,) = _late_results(parent)
    assert late["status"] == "timeout"
    assert "wall" in late["error"]
    missed = late.get("missed_steer") or ""
    assert "first-turn steer" in missed and "queued steer" in missed, late


# I1 amendment: a turn that exits after persist -------------------------------
def test_record_written_under_a_live_turn_is_settled_when_it_exits(
    fleet_home, fast_late
):
    """The steer is reported missed at the FIRST write (the ledger already
    holds it) with steer_fate_unknown; when the turn exits without having
    delivered it, the one record keeps missed_steer and drops the flag."""
    dt = fast_late
    unblock = threading.Event()
    steered = threading.Event()

    def _hang(self):
        steered.wait(10)
        self._drain_pending_steer()  # into a local the turn never returns
        self.frozen_activity_ts = time.time() - 60
        unblock.wait(30)  # ignores the interrupt, outlives the stop drain
        return {"final_response": "", "completed": False, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-amend", parent=parent, api_calls=1, behavior=_hang)
    closed = []
    child.close = lambda: closed.append(1)
    try:
        entry = dt._run_single_child(0, "amend goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        assert dt.steer_subagent("sa-0-amend", "drained before the stall") is True
        steered.set()
        assert _wait_until(lambda: _late_results(parent), timeout=10.0)
        (late,) = _late_results(parent)
        assert late.get("missed_steer") == "drained before the stall", late
        assert late.get("steer_fate_unknown") is True
        assert not closed
    finally:
        unblock.set()
    assert _wait_until(lambda: closed, timeout=5.0)
    assert _wait_until(lambda: "steer_fate_unknown" not in _late_results(parent)[0])
    (late,) = _late_results(parent)
    assert late.get("missed_steer") == "drained before the stall", late
    assert _late_files(fleet_home) == [f"{late['late_result_id']}.json"]
    on_disk = json.loads(open(late["result_path"], encoding="utf-8").read())
    assert on_disk["entry"]["missed_steer"] == "drained before the stall"
    assert "steer_fate_unknown" not in on_disk["entry"]


def test_steer_delivered_after_persist_is_removed_from_the_one_record(
    fleet_home, fast_late
):
    """The live turn outlives the stop drain and THEN writes the steer into a
    tool result (the real delivery path calls the sink). The record said
    missed; the amendment must say delivered, in the same file."""
    from agent.agent_runtime_helpers import note_steer_delivered

    dt = fast_late
    unblock = threading.Event()
    steered = threading.Event()

    def _hang(self):
        steered.wait(10)
        held = self._drain_pending_steer()  # pre-API drain, not yet injected
        self.frozen_activity_ts = time.time() - 60
        unblock.wait(30)
        note_steer_delivered(self, held)  # injected into the tool result
        return {"final_response": "", "completed": False, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-late-deliver", parent=parent, api_calls=1, behavior=_hang)
    closed = []
    child.close = lambda: closed.append(1)
    try:
        entry = dt._run_single_child(0, "deliver goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        assert dt.steer_subagent("sa-0-late-deliver", "check the replica") is True
        steered.set()
        assert _wait_until(lambda: _late_results(parent), timeout=10.0)
        assert _late_results(parent)[0].get("missed_steer") == "check the replica"
    finally:
        unblock.set()
    assert _wait_until(lambda: closed, timeout=5.0)
    assert _wait_until(lambda: "missed_steer" not in _late_results(parent)[0])
    (late,) = _late_results(parent)
    assert "steer_fate_unknown" not in late
    assert "steer did not land" not in (late.get("summary") or "")
    assert _late_files(fleet_home) == [f"{late['late_result_id']}.json"]
    on_disk = json.loads(open(late["result_path"], encoding="utf-8").read())
    assert "missed_steer" not in on_disk["entry"]


# Momus r2 RC-6: the exit callback runs in a fresh copy of the owner context --
@pytest.mark.parametrize("exit_path", ["immediate", "worker_thread"])
def test_exit_callback_runs_in_the_owner_context(fleet_home, fast_late, exit_path):
    """Immediate: the turn already exited when the callback is registered, so
    it runs on the late thread (inside the owner context copy). Worker
    thread: the turn exits later and the callback runs there. Both must
    reach teardown with the owner's context-local state."""
    import contextvars

    dt = fast_late
    marker = contextvars.ContextVar("owner_marker", default=None)
    token = marker.set("owner")
    unblock = threading.Event()
    handed = threading.Event()
    seen = []

    def _hang(self):
        # Active until the late thread owns the child, then stalled: no
        # race between the wait's idle check and a fixed freeze time.
        handed.wait(10)
        self.frozen_activity_ts = time.time() - 60
        if exit_path == "immediate":
            self.interrupt_seen.wait(10)  # exits inside the stop drain
        else:
            unblock.wait(30)  # outlives the stop drain
        return {"final_response": "", "completed": False, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent(f"sa-0-ctx-{exit_path}", parent=parent, api_calls=1, behavior=_hang)
    child.close = lambda: seen.append((marker.get(), threading.current_thread().name))
    try:
        entry = dt._run_single_child(0, "ctx goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        handed.set()
        assert _wait_until(lambda: _late_results(parent), timeout=10.0)
        if exit_path == "worker_thread":
            assert not seen
            (late,) = _late_results(parent)
            assert late.get("steer_fate_unknown") is True
            unblock.set()
        assert _wait_until(lambda: seen, timeout=5.0), exit_path
    finally:
        unblock.set()
        marker.reset(token)
    ((value, thread_name),) = seen
    assert value == "owner"
    if exit_path == "immediate":
        assert thread_name.startswith("delegate-late-")
    else:
        assert not thread_name.startswith("delegate-late-")
    # Momus r3 RC-9: a clean exit resolves the fate; no second nudge.
    (late,) = _late_results(parent)
    assert "steer_fate_unknown" not in late, late
    on_disk = json.loads(open(late["result_path"], encoding="utf-8").read())
    assert "steer_fate_unknown" not in on_disk["entry"]
    assert not any("amended" in s for s in parent.steered), parent.steered


# Edge table ------------------------------------------------------------------
def test_lifecycle_edge_table_is_exactly_the_documented_one():
    from tools.delegate_tool import IllegalTransition, _ChildLifecycle

    legal = {
        ("running", "timeout"): "timed_out_running",
        ("timed_out_running", "correct"): "correcting",
        ("timed_out_running", "finish"): "late_completed",
        ("correcting", "finish"): "late_completed",
        ("timed_out_running", "stall"): "reaped",
        ("correcting", "stall"): "reaped",
        ("late_completed", "persist"): "persisted",
        ("reaped", "persist"): "persisted",
        ("persisted", "amend"): "persisted",
        ("persisted", "teardown"): "torn_down",
    }
    assert dict(_ChildLifecycle.EDGES) == legal
    for state in _ChildLifecycle.STATES:
        for event in _ChildLifecycle.EVENTS:
            lc = _ChildLifecycle(subagent_id=None, child=None, state=state)
            if (state, event) in legal:
                assert lc.fire(event) == legal[(state, event)]
                assert lc.state == legal[(state, event)]
            else:
                with pytest.raises(IllegalTransition):
                    lc.fire(event)
                assert lc.state == state


def test_teardown_door_defers_while_held_and_closes_exactly_once(fleet_home):
    """I2 by construction: the run hold and every live turn keep the one door
    shut; the deferred close runs once, when the last hold goes."""
    from concurrent.futures import ThreadPoolExecutor

    from tools import delegate_tool as dt

    class _C:
        def __init__(self):
            self.closes = []

        def close(self):
            self.closes.append(threading.current_thread().name)

    baseline = dt._count_deferred_teardown(0)
    c = _C()
    dt._hold_run(c)
    assert dt._teardown(c, "parent_close") is False  # the run holds it
    assert c.closes == []
    gate = threading.Event()
    ex = ThreadPoolExecutor(1, thread_name_prefix="turn")
    try:
        fut = dt._submit_turn(ex, c, gate.wait, 5)
        assert dt._teardown(c, "run_end", owner=True) is False  # a turn is live
        with dt._inline_turn(c):
            pass
        assert c.closes == []
        gate.set()
        fut.result(timeout=5)
        assert _wait_until(lambda: c.closes)
    finally:
        gate.set()
        ex.shutdown(wait=True)
    assert len(c.closes) == 1 and c.closes[0].startswith("turn")
    assert dt._teardown(c, "again") is False
    assert dt._teardown(c, "again", owner=True) is False
    assert len(c.closes) == 1
    assert dt._count_deferred_teardown(0) == baseline


def test_teardown_door_closes_an_unheld_child_immediately(fleet_home):
    from tools import delegate_tool as dt

    closes = []
    c = types.SimpleNamespace(close=lambda: closes.append(1))
    assert dt._teardown(c, "construction_failed", owner=True) is True
    assert closes == [1]


def test_steer_ledger_counts_duplicates_and_settles_only_on_delivery(tmp_path):
    from tools.delegate_tool import _SteerLedger

    path = tmp_path / "steer.jsonl"
    led = _SteerLedger(path)
    a = led.accept("stop")
    led.accept("stop now")
    led.accept("stop")
    led.withdraw(a)
    led.deliver("stop now")  # longest first: does not settle "stop"
    assert led.missed() == "stop"
    led.deliver("unrelated")
    assert led.missed() == "stop"
    led.deliver("stop")
    assert led.missed() is None
    ops = [json.loads(line)["op"] for line in path.read_text().splitlines()]
    assert ops == ["accept", "accept", "accept", "withdraw", "deliver", "deliver"]


# Interleaving property ---------------------------------------------------------
def _real_agent_methods(agent):
    """Bind the real AIAgent steer/drain/clear_interrupt code to a stub, so
    stub turns empty the pending slot exactly the way the real agent does."""
    from run_agent import AIAgent

    agent._pending_steer_lock = threading.Lock()
    agent._execution_thread_id = None
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    # _close_active_children is the teardown door AIAgent.close() walks (parity
    # 2026-10-01: upstream 477a9b46e3c split close() into phase methods that are
    # looked up eagerly, so the stub binds the real door and no-ops the rest).
    for name in ("steer", "_drain_pending_steer", "clear_interrupt", "_close_active_children"):
        setattr(agent, name, types.MethodType(getattr(AIAgent, name), agent))
    agent._persist_session = lambda *a, **k: None
    agent._cleanup_task_resources = lambda *a, **k: None
    for name in (
        "shutdown_memory_provider", "_close_task_resources", "_drop_shared_client",
        "_close_request_clients", "_close_codex_session", "_trim_process_memory",
        "_finalize_owned_session_row",
    ):
        setattr(agent, name, lambda *a, **k: None)
    return agent


class _ScriptedChild(_Agent):
    """A child whose turns are driven by the test, one control flag per turn.

    Its steer slot is the real AIAgent's. Tracks live turns so close() can
    prove I2, and every steer text it delivered at a tool boundary (through
    the real delivery sink) so I1 can be checked exactly. Turn exits model
    the real finalizer (drain, then clear_interrupt), a turn that raises
    after the drain, and the interrupted early return.
    """

    def __init__(self, sid, parent, rng):
        super().__init__(sid, parent=parent, api_calls=2, behavior=self._turn)
        _real_agent_methods(self)
        self.rng = rng
        self.lock = threading.Lock()
        self.live_turns = 0
        self.violations = []
        self.consumed = []
        self.close_calls = 0
        self.finish = {1: threading.Event(), 2: threading.Event()}
        self.freeze = {1: False, 2: False}
        self.schema_fail = False
        self.exit_mode = "return"  # | "raise" | "interrupted"
        self.cooperative = rng.random() < 0.5

    def close(self):
        with self.lock:
            self.close_calls += 1
            if self.live_turns:
                self.violations.append(f"I2: close() with {self.live_turns} live turn(s)")

    def _turn(self, _self):
        from agent.agent_runtime_helpers import note_steer_delivered

        n = len(self.calls)
        with self.lock:
            self.live_turns += 1
        try:
            while not self.finish[n].is_set():
                if self.cooperative and self.interrupt_seen.is_set():
                    break
                if self.freeze[n]:
                    self.frozen_activity_ts = time.time() - 60
                else:
                    self.frozen_activity_ts = None
                    # A tool boundary: sometimes delivers queued steer.
                    if self._pending_steer and self.rng.random() < 0.003:
                        text = self._drain_pending_steer()
                        if text:
                            self.consumed.append(text)
                            note_steer_delivered(self, text)
                time.sleep(0.01)
            if self.exit_mode == "interrupted":
                from agent.conversation_loop import _return_interrupted

                return _return_interrupted(self, [], None, 1, "interrupted")
            # The real finalizer drains pending steer into the return value,
            # then resets (clear_interrupt empties the slot), then runs its
            # post-turn hooks, which make no progress and may outlive the
            # hang ceiling and the stop drain.
            pending = self._drain_pending_steer()
            time.sleep(self.rng.choice((0.0, 0.0, 0.05)))  # steer can land here
            self.clear_interrupt()
            hooks = self.rng.choice((0.0, 0.0, 0.1, 0.4, 0.8))
            if hooks:
                self.frozen_activity_ts = time.time() - 60
                time.sleep(hooks)
            if self.exit_mode == "raise":
                raise RuntimeError("post-turn hook failed")
            if n == 1 and self.schema_fail:
                answer = "not json"
            else:
                answer = '{"answer": 1}'
            return {
                "final_response": answer,
                "completed": True,
                "api_calls": 1,
                "interrupted": self.interrupt_seen.is_set(),
                "pending_steer": pending,
            }
        finally:
            with self.lock:
                self.live_turns -= 1


_EVENTS = (
    "timeout",
    "steer",
    "steer",
    "schema-fail",
    "correction-timeout",
    "late-finish",
    "parent-exit",
    "future-raise",
    "interrupted-exit",
    "parent-close",
    "ancestor-cleanup",
)


def _split(text):
    return [s for s in (text or "").split("\n") if s]


@pytest.mark.parametrize("seed", range(int(os.environ.get("DELEGATE_LIFECYCLE_SEEDS", "32"))))
def test_interleavings_preserve_i1_i2_i3(fleet_home, fast_late, monkeypatch, seed):
    from run_agent import AIAgent

    dt = fast_late
    rng = random.Random(seed)
    monkeypatch.setattr(dt, "_get_child_timeout", lambda: 0.2)
    monkeypatch.setattr(dt, "_LATE_STOP_DRAIN_SECONDS", 0.3)
    monkeypatch.setattr(dt, "_get_child_max_wall_seconds", lambda ct: 4.0)

    root = _Agent(None, depth=0)
    parent = _real_agent_methods(_Agent("sa-0-orch", parent=root, depth=1))
    parent.close = types.MethodType(AIAgent.close, parent)
    child = _ScriptedChild(f"sa-1-prop{seed}", parent, rng)
    child._delegate_output_schema = _SCHEMA
    parent._active_children.append(child)
    sid = child._subagent_id
    accepted = []
    history = []

    def check_step(label):
        history.append(label)
        assert not child.violations, (seed, history, child.violations)
        files = _late_files(fleet_home)
        assert len(files) <= 1, (seed, history, files)

    # Random words over the alphabet (with repetition), so that orders like
    # steer < late-finish < (hooks outlive the ceiling) are common.
    events = [rng.choice(_EVENTS) for _ in range(rng.randint(3, 9))]
    try:
        entry = dt._run_single_child(0, f"prop goal {seed}", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING, (seed, entry)
        check_step("timed_out_running")
        for i, ev in enumerate(events):
            time.sleep(rng.uniform(0.0, 0.25))
            n = max(1, len(child.calls))
            if ev == "timeout":
                child.freeze[n] = True
            elif ev == "steer":
                text = f"steer-{seed}-{i}"
                if dt.steer_subagent(sid, text):
                    accepted.append(text)
            elif ev == "schema-fail":
                child.schema_fail = True
            elif ev == "correction-timeout":
                child.freeze[2] = True
            elif ev == "late-finish":
                child.finish[n].set()
            elif ev == "parent-exit":
                parent.steer = lambda text: False
                dt.request_hard_interrupt(child, "parent exited")
            elif ev == "future-raise":
                child.exit_mode = "raise"
            elif ev == "interrupted-exit":
                child.exit_mode = "interrupted"
                dt.request_hard_interrupt(child, "parent interrupted")
            elif ev == "parent-close":
                AIAgent.close(parent)  # gateway reset of the owning agent
            elif ev == "ancestor-cleanup":
                dt._teardown(parent, "ancestor_cleanup")  # recursive close
            check_step(ev)
    finally:
        for f in child.finish.values():
            f.set()

    assert _wait_until(lambda: _late_results(parent), timeout=15.0), (seed, history)
    assert _wait_until(lambda: child.close_calls >= 1, timeout=10.0), (seed, history)
    assert _wait_until(
        lambda: "steer_fate_unknown" not in _late_results(parent)[0], timeout=10.0
    ), (seed, history)
    time.sleep(0.1)
    check_step("terminal")
    assert child.close_calls == 1, (seed, history, child.close_calls)
    assert child.live_turns == 0
    (late,) = _late_results(parent)
    # I3: exactly one durable record under the owning profile, carrying the
    # number of correction turns the late thread started.
    assert _late_files(fleet_home) == [f"{late['late_result_id']}.json"], seed
    assert str(fleet_home) in late["result_path"]
    if "schema_retries" in late:
        assert late["schema_retries"] == len(child.calls) - 1 == 1, (seed, late)
    # I1, exactly: missed_steer is every accepted steer the child did not
    # deliver -- nothing lost, nothing delivered reported as missed.
    missed = _split(late.get("missed_steer"))
    consumed = [s for c in child.consumed for s in _split(c)]
    assert sorted(missed) == sorted(s for s in accepted if s not in consumed), (
        seed,
        history,
        accepted,
        consumed,
        late,
    )
    on_disk = json.loads(open(late["result_path"], encoding="utf-8").read())
    assert _split(on_disk["entry"].get("missed_steer")) == missed
