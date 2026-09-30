"""Behavior contracts for the timed_out_running late-completion path (#1535 P1s).

Prism review of ANG-Ventures/hermes-agent#1535 left six P1s on the path
that owns a child after its delegate_task wait hit child_timeout_seconds
while the child was still working. One test per finding:

1. a child that made API calls and then HANGS is still bounded: no progress
   for child_timeout -> reaped, lease released, registry cleared;
2./3. the late result lands DURABLY (a result file + ``action='list'``
   ``late_results``) even when the parent steer is refused, raises, or the
   parent has no steer at all -- never steer-only;
4. an async batch is not finalized while a timed_out_running child still
   runs: the completion event carries that child's real late result;
5. a steer accepted by the child but never consumed is reported back as
   ``missed_steer`` on the late result, not silently dropped;
6. ``output_schema`` is validated on the late path with the same single
   bounded correction retry as the normal path.

Real imports of tools.delegate_tool against a temp fleet home; only the
agents are stubs.
"""
from __future__ import annotations

import json
import threading
import time
import weakref

import pytest


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _clean_registry():
    from tools import delegate_tool

    def _clear():
        with delegate_tool._active_subagents_lock:
            delegate_tool._active_subagents.clear()
        lock = getattr(delegate_tool, "_late_results_lock", None)
        if lock is not None:
            with lock:
                delegate_tool._late_results.clear()

    _clear()
    yield
    _clear()


class _Pool:
    def __init__(self):
        self.leased = set()
        self.released = []

    def acquire_lease(self):
        self.leased.add("cred-1")
        return "cred-1"

    def current(self):
        return None

    def release_lease(self, cred_id):
        self.leased.discard(cred_id)
        self.released.append(cred_id)


class _Agent:
    """Minimal AIAgent stand-in: interruptible, steerable, tree-linked."""

    def __init__(self, sid, parent=None, depth=1, api_calls=1, behavior=None):
        self._subagent_id = sid
        self._parent_subagent_id = getattr(parent, "_subagent_id", None)
        self._delegate_parent_ref = weakref.ref(parent) if parent is not None else None
        self._delegate_depth = depth
        self._delegate_role = "leaf"
        self.model = "test/model"
        self.session_id = f"sess-{sid or 'root'}"
        self._api_calls = api_calls
        self._behavior = behavior
        self.frozen_activity_ts = None  # set -> activity never advances (hung)
        self.interrupt_seen = threading.Event()
        self.steered = []
        self._pending_steer = None
        self.calls = []
        self.events = []
        self.tool_progress_callback = self._record

    def _record(self, event, **kw):
        self.events.append((event, kw))

    def get_activity_summary(self):
        return {
            "api_call_count": self._api_calls,
            "max_iterations": 10,
            "current_tool": None,
            "last_activity_ts": (
                self.frozen_activity_ts
                if self.frozen_activity_ts is not None
                else time.time()
            ),
        }

    def hard_interrupt(self, message=None, **_kw):
        self.interrupt_seen.set()

    def steer(self, text):
        self.steered.append(text)
        self._pending_steer = (
            f"{self._pending_steer}\n{text}" if self._pending_steer else text
        )
        return True

    def _drain_pending_steer(self):
        pending, self._pending_steer = self._pending_steer, None
        return pending

    def close(self):
        pass

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        self.calls.append(user_message)
        return self._behavior(self)


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _registered(sid):
    from tools import delegate_tool

    with delegate_tool._active_subagents_lock:
        return sid in delegate_tool._active_subagents


def _late_results(parent):
    from tools import delegate_tool

    listing = json.loads(delegate_tool._handle_control_action("list", None, None, parent))
    return listing.get("late_results") or []


# 1 ---------------------------------------------------------------------------
def test_hung_child_after_one_api_call_is_reaped_at_child_timeout(fleet_home, monkeypatch):
    """Active at the wait, then hangs: the late-completion ceiling reaps it.

    No stopwatch: a wall-clock bound from before ``_run_single_child`` mostly
    measured setup and the wait phase (1.0-1.6 s of a 1.5-2.4 s run on an idle
    box; 3.9-4.7 s on PR CI). The behaviour is asserted instead: the lease is
    released and the child interrupted WHILE the child is still hung (so the
    ceiling reaped it, not the child finishing), and the reap names the
    child_timeout as its ceiling.
    """
    from tools import delegate_tool

    child_timeout = 0.3
    hang_seconds = 30.0
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: child_timeout)
    forever = threading.Event()
    child_returned = threading.Event()

    def _hang(self):
        # One API call; last activity 0.2 s into the wait, then blocked for
        # hang_seconds (ignores interrupts).
        try:
            self.frozen_activity_ts = time.time() + 0.2
            forever.wait(hang_seconds)
            return {"final_response": "", "completed": False, "api_calls": 1}
        finally:
            child_returned.set()

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-hung", parent=parent, api_calls=1, behavior=_hang)
    pool = _Pool()
    child._credential_pool = pool
    try:
        entry = delegate_tool._run_single_child(0, "hung goal", child, parent)
        assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
        assert pool.released == [], "lease must stay with the still-active child"
        # Deadlock backstop only: well below the child's own hang, so a missing
        # ceiling fails here instead of the child returning and releasing.
        assert _wait_until(lambda: pool.released == ["cred-1"], timeout=hang_seconds / 3), (
            "hung child kept its credential lease"
        )
        assert not child_returned.is_set(), (
            "lease was released only after the hung child returned on its own; "
            "the hang ceiling never reaped it"
        )
        assert child.interrupt_seen.is_set(), "hung child was never stopped"
        assert not _registered("sa-0-hung")
        assert _wait_until(lambda: _late_results(parent))
        (late,) = _late_results(parent)
        assert late["status"] == "timeout"
        assert "hung" in late["error"]
        assert f"no progress for {child_timeout}s" in late["error"], (
            f"hang ceiling is not child_timeout: {late['error']!r}"
        )
    finally:
        forever.set()


def test_child_already_idle_for_child_timeout_is_not_timed_out_running(
    fleet_home, monkeypatch
):
    """Idle for the whole wait after its API call: a failure, not live work."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    forever = threading.Event()

    def _hang(self):
        forever.wait(30)
        return {"final_response": "", "completed": False, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-idle", parent=parent, api_calls=1, behavior=_hang)
    child.frozen_activity_ts = time.time() - 60
    pool = _Pool()
    child._credential_pool = pool
    try:
        entry = delegate_tool._run_single_child(0, "idle goal", child, parent)
        assert entry["status"] == "timeout"
        assert entry["timeout_phase"] == "after_llm_calls"
        assert pool.released == ["cred-1"]
        assert child.interrupt_seen.is_set()
        assert not _registered("sa-0-idle")
    finally:
        forever.set()


def test_working_child_is_not_reaped_by_the_hang_ceiling(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.2)
    # Isolate the HANG ceiling: the absolute wall ceiling (default 4x
    # child_timeout, #1542 round 2) is covered in test_delegate_late_round2.
    monkeypatch.setattr(delegate_tool, "_get_child_max_wall_seconds", lambda ct: 60.0)
    release = threading.Event()

    def _slow(self):
        release.wait(10)
        return {"final_response": "slow but alive", "completed": True, "api_calls": 4}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-alive", parent=parent, api_calls=2, behavior=_slow)
    entry = delegate_tool._run_single_child(0, "alive goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    time.sleep(0.8)  # 4x child_timeout of continuous activity
    assert not child.interrupt_seen.is_set()
    assert _registered("sa-0-alive")
    release.set()
    assert _wait_until(lambda: any(r["status"] == "completed" for r in _late_results(parent)))


# 2 + 3 -----------------------------------------------------------------------
@pytest.mark.parametrize("parent_steer", ["refused", "raises", "absent"])
def test_late_result_is_durable_when_parent_steer_is_lost(
    fleet_home, monkeypatch, parent_steer
):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()

    def _slow(self):
        release.wait(10)
        return {"final_response": "the late answer", "completed": True, "api_calls": 3}

    parent = _Agent(None, depth=0)
    if parent_steer == "refused":  # idle / finished parent
        parent.steer = lambda text: False
    elif parent_steer == "raises":  # interrupted / torn-down parent

        def _boom(text):
            raise RuntimeError("parent gone")

        parent.steer = _boom
    else:
        parent.steer = None
    child = _Agent("sa-0-late", parent=parent, api_calls=2, behavior=_slow)

    entry = delegate_tool._run_single_child(0, "late goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    release.set()

    assert _wait_until(lambda: _late_results(parent)), "late result not recorded"
    (late,) = _late_results(parent)
    assert late["subagent_id"] == "sa-0-late"
    assert late["status"] == "completed"
    assert "the late answer" in late["summary"]
    on_disk = json.loads(open(late["result_path"], encoding="utf-8").read())
    assert on_disk["entry"]["summary"] == "the late answer"
    assert str(fleet_home) in late["result_path"]


# 4 ---------------------------------------------------------------------------
def test_async_batch_waits_for_timed_out_running_child(fleet_home, monkeypatch):
    from unittest.mock import MagicMock

    from tools import async_delegation as ad
    from tools import delegate_tool as dt
    from tools.process_registry import process_registry

    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()

    monkeypatch.setattr(dt, "_get_child_timeout", lambda: 0.3)
    # The 1.0 s "still open" window must not race the default wall ceiling
    # (4x child_timeout = 1.2 s from child start).
    monkeypatch.setattr(dt, "_get_child_max_wall_seconds", lambda ct: 60.0)
    release = threading.Event()

    def _slow(self):
        release.wait(10)
        return {"final_response": "batch late answer", "completed": True, "api_calls": 3}

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess-batch"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    child = _Agent("sa-0-batch", parent=None, api_calls=2, behavior=_slow)
    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)

    def _drain(timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not process_registry.completion_queue.empty():
                return process_registry.completion_queue.get_nowait()
            time.sleep(0.02)
        return None

    try:
        out = json.loads(
            dt.delegate_task(goal="batch goal", background=True, parent_agent=parent)
        )
        assert out["status"] == "dispatched", out
        # Well past child_timeout: the batch must still be open.
        early = _drain(1.0)
        assert early is None, f"batch finalized with a live child: {early}"
        release.set()
        evt = _drain(5.0)
        assert evt is not None
        (res,) = evt["results"]
        assert res["status"] == "completed"
        assert "batch late answer" in res["summary"]
        assert evt["status"] == "completed"
    finally:
        release.set()
        deadline = time.monotonic() + 2.0
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.02)
        ad._reset_for_tests()
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()


# 5 ---------------------------------------------------------------------------
def test_accepted_steer_survives_late_completion(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()

    def _slow(self):
        release.wait(10)
        # Finishes without a tool boundary: the queued steer is never consumed.
        return {"final_response": "done anyway", "completed": True, "api_calls": 3}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-steer", parent=parent, api_calls=2, behavior=_slow)
    entry = delegate_tool._run_single_child(0, "steer goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    assert delegate_tool.steer_subagent("sa-0-steer", "focus on the tests") is True
    release.set()

    assert _wait_until(lambda: _late_results(parent))
    (late,) = _late_results(parent)
    assert late["missed_steer"] == "focus on the tests"
    assert "focus on the tests" in late["summary"]
    assert _wait_until(lambda: parent.steered)
    assert "focus on the tests" in parent.steered[0]


# 6 ---------------------------------------------------------------------------
def test_output_schema_validated_with_one_retry_on_late_path(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {"final_response": "not json at all", "completed": True, "api_calls": 3}
        return {"final_response": '{"answer": 42}', "completed": True, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-schema", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    entry = delegate_tool._run_single_child(0, "schema goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    release.set()

    assert _wait_until(lambda: _late_results(parent))
    (late,) = _late_results(parent)
    assert len(child.calls) == 2, "exactly one bounded correction retry"
    assert late["schema_valid"] is True
    assert late["schema_retries"] == 1
    assert json.loads(late["summary"]) == {"answer": 42}
