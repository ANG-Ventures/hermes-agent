"""``delegation.child_timeout_seconds`` bounds INACTIVITY, not total runtime (#116001).

A configured cap used to be a dispatch-to-death stopwatch: every child that ran longer than the budget was
abandoned even while the provider was actively serving it. Measured over 219 tasks / 75 deaths, 0 of 75 deaths
happened mid-tool — all of them were waiting on an in-flight LLM completion, so the cap converted transient
provider slowness into lost work (a nearly-finished context discarded every time).

These tests pin both directions of the fix:
- a child whose activity signals keep advancing outlives a cap far shorter than its runtime;
- a child with no progress at all is still abandoned when the cap elapses (the knob keeps its teeth).

Fork contract (parity 2026-10-01; #1535 #1542 supervisor on the hot path): the cap is still the
parent's WAIT budget. A child with recent progress is never killed at the cap — the wait returns a
``timed_out_running`` handle and the child's result lands as a durable late result (``action='list'``
``late_results``); a child whose activity clock has not moved for the whole cap is abandoned there.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from tests.tools.test_delegate_late_completion import (  # noqa: F401
    _clean_registry,
    _late_results,
    _wait_until,
    fleet_home,
)
from tools import delegate_tool

_CAP_SECONDS = 0.4

class _SlowButLiveChild:
    """A provider serving multi-minute completions: the runtime outlasts the cap, but progress never stops.

    ``advance=False`` models the opposite case — a genuinely frozen child (no completed call, no tool change,
    no activity-clock tick).
    """

    def __init__(self, *, total_seconds: float = 1.2, advance: bool = True, initial_calls: int = 0) -> None:
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.session_id = "live-child"
        self.model = "test/model"
        self._total_seconds = total_seconds
        self._advance = advance
        self._calls = initial_calls
        self._activity_ts = time.time()
        self.interrupted = threading.Event()
        self.steers: list[tuple[float, str]] = []  # (monotonic time, text) of every budget warning received

    def steer(self, text: str) -> bool:
        self.steers.append((time.monotonic(), text))
        return True

    def run_conversation(self, **_kwargs):
        deadline = time.monotonic() + self._total_seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if self._advance:
                self._calls += 1
                self._activity_ts = time.time()
        return {
            "final_response": "FINISHED AFTER OUTLIVING THE CAP",
            "completed": True,
            "api_calls": self._calls,
            "messages": [],
        }

    def get_activity_summary(self):
        return {
            "api_call_count": self._calls,
            "current_tool": None,
            "last_activity_ts": self._activity_ts,
            "max_iterations": 50,
        }

    def hard_interrupt(self, *_args, **_kwargs):
        self.interrupted.set()

    def close(self):
        pass

def _run(child, monkeypatch, cap=_CAP_SECONDS):
    parent = SimpleNamespace(
        session_id="parent", _current_task_id=None, _active_children=[child],
        _active_children_lock=threading.Lock(), _touch_activity=lambda _d: None, _interrupt_requested=False,
    )
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: cap)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    # The parent is returned (entry stashed on it) so a late result can be read under the same owner.
    parent.entry = delegate_tool._run_single_child(0, "watch the slow provider", child=child, parent_agent=parent)
    return parent

def test_progressing_child_outlives_a_cap_shorter_than_its_runtime(fleet_home, monkeypatch):
    child = _SlowButLiveChild(total_seconds=1.2, advance=True)
    parent = _run(child, monkeypatch)

    entry = parent.entry
    # The cap really was armed: the parent's wait ended at the budget while the child kept working.
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING, entry
    assert entry["timeout_seconds"] == _CAP_SECONDS
    # ...and the child really did outlive it: never interrupted, result delivered late.
    assert _wait_until(lambda: _late_results(parent), timeout=10.0), "late result never landed"
    (late,) = _late_results(parent)
    assert late["status"] == "completed", late
    assert late["summary"] == "FINISHED AFTER OUTLIVING THE CAP", late
    assert not child.interrupted.is_set()
    # Progress kept the child alive, so no budget warning ever had cause to fire.
    assert child.steers == [], child.steers

def test_frozen_child_is_still_abandoned_when_the_cap_elapses(fleet_home, monkeypatch):
    """The reported death shape: calls completed earlier, then the child stops moving entirely."""
    child = _SlowButLiveChild(total_seconds=1.2, advance=False, initial_calls=49)

    entry = _run(child, monkeypatch).entry

    assert entry["status"] == "timeout", entry
    assert entry["exit_reason"] == "timeout", entry
    assert f"timed out after {_CAP_SECONDS}s" in entry["error"], entry["error"]
    assert entry["timeout_seconds"] == _CAP_SECONDS
    assert entry["last_event_age"] is not None and entry["last_event_age"] > 0.3, entry
    assert child.interrupted.is_set()
