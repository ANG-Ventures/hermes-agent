"""Prism P1s on ANG-Ventures/hermes-agent#1573 (pre-merge 25a555f6, post-merge
f9d4df5a): one test per finding.

Real findings are RED on f9d4df5a and GREEN with the per-child steer ledger
and the single teardown door (docs/dev/delegate-child-lifecycle.md). The one
false positive (:3000 / :3590, "repeated corrections") gets a proof test that
is GREEN on both heads: ``_apply_output_schema`` runs at most one correction
turn, so a second ``correct`` edge cannot be requested.

The stubs reuse the REAL ``AIAgent`` code the findings name instead of
modelling it: ``AIAgent.clear_interrupt`` (which empties ``_pending_steer``),
``conversation_loop._return_interrupted`` and ``AIAgent.close`` (which closes
every active child).
"""
from __future__ import annotations

import threading
import time
import types

import pytest

from tests.tools.test_delegate_child_lifecycle import (  # noqa: F401
    _SCHEMA,
    _real_agent_methods,
    fast_late,
)
from tests.tools.test_delegate_late_completion import (  # noqa: F401
    _Agent,
    _clean_registry,
    _late_results,
    _wait_until,
    fleet_home,
)


def _missed(late):
    return [s for s in (late.get("missed_steer") or "").split("\n") if s]


# :3000 (25a555f6) and :3590 (f9d4df5a) -- FALSE POSITIVE --------------------
def test_f3000_f3590_one_correction_turn_even_when_the_correction_fails_too(
    fleet_home, fast_late
):
    """Both findings need a second ``fire("correct")``. ``_apply_output_schema``
    calls ``run_turn`` at most once (``retries = 1`` under a single ``if``,
    no loop), and the late thread calls it once. A correction answer that
    also fails the schema ends the path; nothing raises IllegalTransition."""
    dt = fast_late
    release = threading.Event()

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
        return {"final_response": "not json", "completed": True, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-f3590", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    closed = []
    child.close = lambda: closed.append(1)
    entry = dt._run_single_child(0, "f3590 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    release.set()
    assert _wait_until(lambda: _late_results(parent) and closed, timeout=10.0)
    (late,) = _late_results(parent)
    assert len(child.calls) == 2, child.calls  # first turn + ONE correction
    assert late["schema_valid"] is False
    assert late["schema_retries"] == 1
    assert closed == [1]


# :3862 (25a555f6) ------------------------------------------------------------
def test_f3862_turn_that_raises_after_the_stop_drain_keeps_its_steer(
    fleet_home, fast_late
):
    """The finalizer drains an accepted steer into its local result, the
    post-turn hooks outlive the stop drain, then the turn RAISES. The steer
    is neither consumed nor readable from the future; it must still be
    reported, not erased together with ``steer_fate_unknown``."""
    dt = fast_late
    steered = threading.Event()
    unblock = threading.Event()

    def _turn(self):
        steered.wait(10)
        self._drain_pending_steer()  # turn_finalizer.py:845, into a local
        self.frozen_activity_ts = time.time() - 60  # hooks: no progress
        unblock.wait(30)  # outlives the stop drain
        raise RuntimeError("post-turn hook failed")

    parent = _Agent(None, depth=0)
    child = _real_agent_methods(
        _Agent("sa-0-f3862", parent=parent, api_calls=1, behavior=_turn)
    )
    closed = []
    child.close = lambda: closed.append(1)
    try:
        entry = dt._run_single_child(0, "f3862 goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        assert dt.steer_subagent("sa-0-f3862", "route via the replica") is True
        steered.set()
        assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    finally:
        unblock.set()
    assert _wait_until(lambda: closed, timeout=5.0)
    (late,) = _late_results(parent)
    assert "route via the replica" in _missed(late), late


# :3623 (25a555f6) ------------------------------------------------------------
def test_f3623_interrupted_turn_exit_keeps_the_accepted_steer(fleet_home, fast_late):
    """A parent interrupt ends the turn through the real early return
    ``_return_interrupted``, which calls ``clear_interrupt()`` and so empties
    the pending steer slot. The accepted steer must still be reported."""
    dt = fast_late
    steered = threading.Event()

    def _turn(self):
        steered.wait(10)
        self.interrupt_seen.wait(10)
        from agent.conversation_loop import _return_interrupted

        return _return_interrupted(
            self, [], None, 1, "Interrupted before the next model call"
        )

    parent = _Agent(None, depth=0)
    child = _real_agent_methods(
        _Agent("sa-0-f3623", parent=parent, api_calls=1, behavior=_turn)
    )
    entry = dt._run_single_child(0, "f3623 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    assert dt.steer_subagent("sa-0-f3623", "stop after the schema step") is True
    steered.set()
    dt.request_hard_interrupt(child, "parent exited")
    assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    (late,) = _late_results(parent)
    assert late["status"] == "interrupted", late
    assert "stop after the schema step" in _missed(late), late


# :3035 (25a555f6) and :3045 (f9d4df5a) ---------------------------------------
def test_f3035_f3045_steer_accepted_between_finalizer_drain_and_reset(
    fleet_home, fast_late
):
    """turn_finalizer.py drains pending steer (:845), then calls
    ``clear_interrupt()`` (:855), which empties the slot. A steer accepted
    between the two is in neither the turn's result nor the closure drain."""
    dt = fast_late
    go = threading.Event()
    drained = threading.Event()
    steered = threading.Event()

    def _turn(self):
        go.wait(10)
        pending = self._drain_pending_steer()  # :845
        drained.set()
        steered.wait(10)
        self.clear_interrupt()  # :855
        return {
            "final_response": "done",
            "completed": True,
            "api_calls": 1,
            "pending_steer": pending,
        }

    parent = _Agent(None, depth=0)
    child = _real_agent_methods(
        _Agent("sa-0-f3045", parent=parent, api_calls=1, behavior=_turn)
    )
    entry = dt._run_single_child(0, "f3045 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    go.set()
    assert drained.wait(5)
    assert dt.steer_subagent("sa-0-f3045", "also run the migration") is True
    steered.set()
    assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    (late,) = _late_results(parent)
    assert late["status"] == "completed", late
    assert "also run the migration" in _missed(late), late


# :3057 (f9d4df5a) -------------------------------------------------------------
def test_f3057_two_accepted_steers_with_the_same_text_are_both_reported(
    fleet_home, fast_late
):
    """One "recheck" is drained by the first turn's finalizer, a second,
    separately accepted "recheck" by the correction turn's finalizer. Both
    were accepted and neither landed: the record reports both."""
    dt = fast_late
    release = threading.Event()
    second = threading.Event()

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {
                "final_response": "not json",
                "completed": True,
                "api_calls": 2,
                "pending_steer": self._drain_pending_steer(),
            }
        second.wait(10)
        return {
            "final_response": '{"answer": 1}',
            "completed": True,
            "api_calls": 1,
            "pending_steer": self._drain_pending_steer(),
        }

    parent = _Agent(None, depth=0)
    child = _real_agent_methods(
        _Agent("sa-0-f3057", parent=parent, api_calls=2, behavior=_answers)
    )
    child._delegate_output_schema = _SCHEMA
    entry = dt._run_single_child(0, "f3057 goal", child, parent)
    assert entry["status"] == dt.TIMED_OUT_RUNNING
    assert dt.steer_subagent("sa-0-f3057", "recheck") is True
    release.set()
    assert _wait_until(lambda: len(child.calls) == 2)
    assert dt.steer_subagent("sa-0-f3057", "recheck") is True
    second.set()
    assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    (late,) = _late_results(parent)
    assert _missed(late) == ["recheck", "recheck"], late


# :3564 (f9d4df5a) and :4963 (f9d4df5a) ---------------------------------------
class _LiveTurnChild(_Agent):
    """Records close() while its turn is live (the SessionDB-under-a-live-turn
    hazard) and once its turn has exited."""

    def __init__(self, sid, parent):
        super().__init__(sid, parent=parent, api_calls=1, behavior=self._turn)
        self.unblock = threading.Event()
        self.live = False
        self.closes = []

    def _turn(self, _self):
        self.live = True
        try:
            self.unblock.wait(30)  # busy, ignores the interrupt
            return {"final_response": "done", "completed": True, "api_calls": 1}
        finally:
            self.live = False

    def close(self):
        self.closes.append("live" if self.live else "exited")


def test_f3564_parent_close_defers_a_live_late_child(fleet_home, fast_late):
    """A gateway reset closes the parent with the real ``AIAgent.close()``,
    which closes every ``_active_children`` entry. A timed_out_running child
    is still in that list; its close must wait for its turn to exit."""
    from run_agent import AIAgent

    dt = fast_late
    parent = _real_agent_methods(_Agent(None, depth=0))
    child = _LiveTurnChild("sa-0-f3564", parent)
    parent._active_children.append(child)  # what _build_child_agent does
    try:
        entry = dt._run_single_child(0, "f3564 goal", child, parent)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        assert _wait_until(lambda: child.live)
        AIAgent.close(parent)
        assert "live" not in child.closes, child.closes
    finally:
        child.unblock.set()
    assert _wait_until(lambda: child.closes, timeout=10.0)
    time.sleep(0.2)
    assert child.closes == ["exited"], child.closes


def test_f4963_ancestor_teardown_defers_a_live_late_grandchild(fleet_home, fast_late):
    """A delegated orchestrator is torn down by delegate_task while its own
    late child still runs. Its real ``AIAgent.close()`` recurses into that
    grandchild; the grandchild's close must wait for its turn to exit."""
    from run_agent import AIAgent

    dt = fast_late
    root = _Agent(None, depth=0)
    orchestrator = _real_agent_methods(_Agent("sa-0-orch", parent=root, depth=1))
    orchestrator.close = types.MethodType(AIAgent.close, orchestrator)
    grandchild = _LiveTurnChild("sa-1-f4963", orchestrator)
    orchestrator._active_children.append(grandchild)
    try:
        entry = dt._run_single_child(0, "f4963 goal", grandchild, orchestrator)
        assert entry["status"] == dt.TIMED_OUT_RUNNING
        assert _wait_until(lambda: grandchild.live)
        # The orchestrator's own run is over: delegate_task releases it.
        dt._release_child_resources(orchestrator, root, None, None, None)
        assert "live" not in grandchild.closes, grandchild.closes
    finally:
        grandchild.unblock.set()
    assert _wait_until(lambda: grandchild.closes, timeout=10.0)
    time.sleep(0.2)
    assert grandchild.closes == ["exited"], grandchild.closes
