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

    def _hang(self):
        self.frozen_activity_ts = time.time() + 0.1
        unblock.wait(30)
        try:
            return {
                "final_response": "",
                "completed": False,
                "api_calls": 1,
                "pending_steer": "drained in the window",
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
                "pending_steer": "use the staging db",
            }
        self.frozen_activity_ts = time.time() - 60
        self.interrupt_seen.wait(10)
        return {"final_response": "", "completed": False, "api_calls": 0}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3434", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    entry = dt._run_single_child(0, "f3434 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
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
                "pending_steer": "first-turn steer",
            }
        self.interrupt_seen.wait(10)  # busy (activity advances) until stopped
        return {"final_response": "", "completed": False, "api_calls": 0}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3494", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    entry = dt._run_single_child(0, "f3494 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
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
def test_steer_returned_after_persist_amends_the_one_record(fleet_home, fast_late):
    dt = fast_late
    unblock = threading.Event()

    def _hang(self):
        self.frozen_activity_ts = time.time() + 0.1
        unblock.wait(30)  # ignores the interrupt, outlives the stop drain
        return {
            "final_response": "",
            "completed": False,
            "api_calls": 1,
            "pending_steer": "drained before the stall",
        }

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-amend", parent=parent, api_calls=1, behavior=_hang)
    closed = []
    child.close = lambda: closed.append(1)
    try:
        entry = dt._run_single_child(0, "amend goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        assert _wait_until(lambda: _late_results(parent), timeout=10.0)
        (late,) = _late_results(parent)
        assert not late.get("missed_steer")
        assert not closed
    finally:
        unblock.set()
    assert _wait_until(lambda: closed, timeout=5.0)
    (late,) = _late_results(parent)
    assert late.get("missed_steer") == "drained before the stall", late
    assert _late_files(fleet_home) == [f"{late['late_result_id']}.json"]
    on_disk = json.loads(open(late["result_path"], encoding="utf-8").read())
    assert on_disk["entry"]["missed_steer"] == "drained before the stall"
    assert _wait_until(
        lambda: any("drained before the stall" in s for s in parent.steered)
    )


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
    seen = []

    def _hang(self):
        self.frozen_activity_ts = time.time() + 0.1
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
        assert _wait_until(lambda: _late_results(parent), timeout=10.0)
        if exit_path == "worker_thread":
            assert not seen
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
    (late,) = _late_results(parent)
    assert bool(late.get("steer_fate_unknown")) is (exit_path == "worker_thread")


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


def test_teardown_is_refused_while_a_turn_is_live():
    from concurrent.futures import Future

    from tools.delegate_tool import IllegalTransition, _ChildLifecycle

    fut: Future = Future()
    lc = _ChildLifecycle(subagent_id=None, child=None, state="persisted")
    lc.live = fut
    with pytest.raises(IllegalTransition):
        lc.fire("teardown")
    fut.set_result({})
    assert lc.fire("teardown") == "torn_down"


# Interleaving property ---------------------------------------------------------
class _ScriptedChild(_Agent):
    """A child whose turns are driven by the test, one control flag per turn.

    Tracks live turns so close() can prove I2, and every steer text it
    consumed at a tool boundary so I1 can be checked exactly.
    """

    def __init__(self, sid, parent, rng):
        super().__init__(sid, parent=parent, api_calls=2, behavior=self._turn)
        self.rng = rng
        self.lock = threading.Lock()
        self.live_turns = 0
        self.violations = []
        self.consumed = []
        self.close_calls = 0
        self.finish = {1: threading.Event(), 2: threading.Event()}
        self.freeze = {1: False, 2: False}
        self.schema_fail = False
        self.cooperative = rng.random() < 0.5

    def close(self):
        with self.lock:
            self.close_calls += 1
            if self.live_turns:
                self.violations.append(f"I2: close() with {self.live_turns} live turn(s)")

    def _turn(self, _self):
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
                        self.consumed.append(self._drain_pending_steer())
                time.sleep(0.01)
            # The real finalizer drains pending steer into the return value
            # BEFORE its post-turn hooks run; hooks make no progress and may
            # outlive the hang ceiling and the stop drain.
            pending = self._drain_pending_steer()
            hooks = self.rng.choice((0.0, 0.0, 0.1, 0.4, 0.8))
            if hooks:
                self.frozen_activity_ts = time.time() - 60
                time.sleep(hooks)
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


_EVENTS = ("timeout", "steer", "schema-fail", "correction-timeout", "late-finish", "parent-exit")


def _split(text):
    return [s for s in (text or "").split("\n") if s]


@pytest.mark.parametrize("seed", range(int(os.environ.get("DELEGATE_LIFECYCLE_SEEDS", "32"))))
def test_interleavings_preserve_i1_i2_i3(fleet_home, fast_late, monkeypatch, seed):
    dt = fast_late
    rng = random.Random(seed)
    monkeypatch.setattr(dt, "_get_child_timeout", lambda: 0.2)
    monkeypatch.setattr(dt, "_LATE_STOP_DRAIN_SECONDS", 0.3)
    monkeypatch.setattr(dt, "_get_child_max_wall_seconds", lambda ct: 4.0)

    parent = _Agent(None, depth=0)
    child = _ScriptedChild(f"sa-0-prop{seed}", parent, rng)
    child._delegate_output_schema = _SCHEMA
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
    events = [rng.choice(_EVENTS) for _ in range(rng.randint(3, 8))]
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
            check_step(ev)
    finally:
        for f in child.finish.values():
            f.set()

    assert _wait_until(lambda: _late_results(parent), timeout=15.0), (seed, history)
    assert _wait_until(lambda: child.close_calls >= 1, timeout=10.0), (seed, history)
    time.sleep(0.1)
    check_step("terminal")
    assert child.close_calls == 1, (seed, history, child.close_calls)
    assert child.live_turns == 0
    (late,) = _late_results(parent)
    # I3: exactly one durable record under the owning profile.
    assert _late_files(fleet_home) == [f"{late['late_result_id']}.json"], seed
    assert str(fleet_home) in late["result_path"]
    # I1: every accepted steer was consumed or is reported as missed_steer.
    missed = _split(late.get("missed_steer"))
    consumed = [s for c in child.consumed for s in _split(c)]
    lost = [s for s in accepted if s not in missed and s not in consumed]
    assert not lost, (seed, history, lost, late)
    on_disk = json.loads(open(late["result_path"], encoding="utf-8").read())
    assert _split(on_disk["entry"].get("missed_steer")) == missed
