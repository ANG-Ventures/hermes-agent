"""Hung-child detection keys on PROGRESS, not liveness (Argus QA t_86717544 F2).

A child blocked inside one API call (non-streaming wait ticker, streaming
wait ticker / keepalive ping) or inside one tool call (tool-activity
heartbeat, ``touch_activity_if_due``) keeps ``last_activity_ts`` fresh
forever. Those ticks prove the process is alive; they are not progress. The
hung-child verdict reads ``last_progress_event_ts`` instead, and the ceiling
(``delegation.hung_child_seconds``) applies with ``child_timeout_seconds: 0``.

Real imports against a temp fleet home. The children run the REAL
``AIAgent._touch_activity`` / ``get_activity_summary`` and, for the tool
shape, the real tool heartbeat + activity-callback helpers; only the blocking
call itself is a stub.
"""
from __future__ import annotations

import threading
import time
import uuid
import weakref
from types import SimpleNamespace

import pytest


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("DELEGATION_CHILD_TIMEOUT_SECONDS", raising=False)
    return home


@pytest.fixture(autouse=True)
def _clean_registry():
    from tools import delegate_tool

    def _clear():
        with delegate_tool._active_subagents_lock:
            delegate_tool._active_subagents.clear()
        with delegate_tool._late_results_lock:
            delegate_tool._late_results.clear()

    _clear()
    yield
    _clear()


class _Pool:
    def __init__(self):
        self.released = []

    def acquire_lease(self):
        return "cred-1"

    def current(self):
        return None

    def release_lease(self, cred_id):
        self.released.append(cred_id)


def _real_agent_cls():
    from run_agent import AIAgent

    class _Child:
        """Stub agent whose activity clocks are the real AIAgent methods."""

        _touch_activity = AIAgent._touch_activity
        _persist_session_activity_if_due = AIAgent._persist_session_activity_if_due
        get_activity_summary = AIAgent.get_activity_summary

        def __init__(self, sid, parent=None, behavior=None, depth=1):
            self._subagent_id = sid
            self._parent_subagent_id = getattr(parent, "_subagent_id", None)
            self._delegate_parent_ref = weakref.ref(parent) if parent is not None else None
            self._delegate_depth = depth
            self._delegate_role = "leaf"
            self.model = "test/model"
            self.session_id = f"sess-{sid or 'root-' + uuid.uuid4().hex[:8]}"
            self._behavior = behavior
            self._current_tool = None
            self._api_call_count = 1
            self.max_iterations = 10
            self.iteration_budget = SimpleNamespace(used=1, max_total=10)
            now = time.time()
            self._last_activity_ts = now
            self._last_progress_ts = now
            self._last_progress_event_ts = now
            self._last_activity_desc = "init"
            self.interrupt_seen = threading.Event()
            self._pending_steer = None
            self.tool_progress_callback = None

        def hard_interrupt(self, message=None, **_kw):
            self.interrupt_seen.set()

        def steer(self, text):
            return False

        def _drain_pending_steer(self):
            return None

        def close(self):
            pass

        def run_conversation(self, user_message, task_id=None, stream_callback=None):
            return self._behavior(self)

    return _Child


TICK = 0.05
HUNG = 0.6
HANG_BOUND = 8.0  # a missing detector lets the hang run this long, then "complete"


def _blocked_api_nonstreaming(agent):
    """direct_api_call's mid-request ticker (progress=False)."""
    agent._touch_activity("starting API call #2")
    end = time.monotonic() + HANG_BOUND
    while time.monotonic() < end and not agent.interrupt_seen.is_set():
        agent._touch_activity("waiting for non-streaming API response", progress=False)
        time.sleep(TICK)
    return {"final_response": "late", "completed": True, "api_calls": 2}


def _blocked_api_streaming(agent):
    """Streaming wait ticker + keepalive ping, no content chunk ever arrives."""
    agent._touch_activity("waiting for provider response (streaming)")
    end = time.monotonic() + HANG_BOUND
    while time.monotonic() < end and not agent.interrupt_seen.is_set():
        agent._touch_activity("waiting for stream response (3s, no chunks yet)", heartbeat=True)
        agent._touch_activity("receiving stream response", heartbeat=True)  # ping
        agent._touch_activity("⏳ waiting on test/model", progress=False)  # _emit_wait_notice
        time.sleep(TICK)
    return {"final_response": "late", "completed": True, "api_calls": 2}


def _blocked_tool(agent):
    """One long tool call: real tool heartbeat thread + real activity callback."""
    from agent.tool_executor import _heartbeat_touch_fn, _run_tool_activity_heartbeat
    from tools.environments.base import set_activity_callback, touch_activity_if_due

    agent._current_tool = "terminal"
    agent._touch_activity("executing tool: terminal")
    stop = threading.Event()
    hb = threading.Thread(
        target=_run_tool_activity_heartbeat,
        args=(agent, stop, "tool running: terminal"),
        kwargs={"interval": TICK},
        daemon=True,
    )
    hb.start()
    set_activity_callback(_heartbeat_touch_fn(agent))
    state = {"last_touch": 0.0, "start": time.monotonic(), "interval": TICK}
    try:
        end = time.monotonic() + HANG_BOUND
        while time.monotonic() < end and not agent.interrupt_seen.is_set():
            touch_activity_if_due(state, "terminal command running")
            time.sleep(TICK / 2)
    finally:
        stop.set()
        set_activity_callback(None)
        agent._current_tool = None
    return {"final_response": "late", "completed": True, "api_calls": 1}


@pytest.mark.parametrize(
    "behavior",
    [_blocked_api_nonstreaming, _blocked_api_streaming, _blocked_tool],
    ids=["hangapi-nonstreaming", "hangapi-streaming", "hangtool"],
)
def test_blocked_child_is_reaped_at_hung_child_seconds_with_no_budget(
    fleet_home, monkeypatch, behavior
):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(delegate_tool, "_get_hung_child_seconds", lambda: HUNG)
    _Child = _real_agent_cls()
    parent = _Child(None, depth=0)
    child = _Child("sa-0-blocked", parent=parent, behavior=behavior)
    pool = _Pool()
    child._credential_pool = pool

    t0 = time.monotonic()
    entry = delegate_tool._run_single_child(0, "blocked goal", child, parent)
    took = time.monotonic() - t0

    s = child.get_activity_summary()
    # Liveness kept flowing the whole time; progress stopped.
    assert time.time() - s["last_activity_ts"] < HUNG, s
    assert time.time() - s["last_progress_event_ts"] >= HUNG, s
    assert entry["status"] == "timeout", entry
    assert entry["timeout_phase"] == "no_progress", entry
    assert "hung_child_seconds" in entry["error"]
    assert child.interrupt_seen.is_set(), "blocked child was never stopped"
    assert pool.released == ["cred-1"]
    assert took < HANG_BOUND / 2, f"reaped only after {took:.1f}s"


def test_progressing_child_is_not_reaped(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(delegate_tool, "_get_hung_child_seconds", lambda: HUNG)

    def _streaming_tokens(agent):
        end = time.monotonic() + 4 * HUNG
        while time.monotonic() < end:
            agent._touch_activity("receiving stream response")
            time.sleep(TICK)
        return {"final_response": "done", "completed": True, "api_calls": 1}

    _Child = _real_agent_cls()
    parent = _Child(None, depth=0)
    child = _Child("sa-0-progress", parent=parent, behavior=_streaming_tokens)
    entry = delegate_tool._run_single_child(0, "progress goal", child, parent)
    assert entry["status"] == "completed", entry
    assert not child.interrupt_seen.is_set()


def test_wall_cap_still_applies_when_child_timeout_is_set(fleet_home, monkeypatch):
    """child_timeout>0: a child that keeps progressing is still stopped at the wall."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    monkeypatch.setattr(delegate_tool, "_get_hung_child_seconds", lambda: 60.0)
    monkeypatch.setattr(delegate_tool, "_get_child_max_wall_seconds", lambda _c: 1.0)

    def _forever(agent):
        end = time.monotonic() + HANG_BOUND
        while time.monotonic() < end and not agent.interrupt_seen.is_set():
            agent._touch_activity("receiving stream response")
            time.sleep(TICK)
        return {"final_response": "", "completed": False, "api_calls": 1}

    _Child = _real_agent_cls()
    parent = _Child(None, depth=0)
    child = _Child("sa-0-forever", parent=parent, behavior=_forever)
    entry = delegate_tool._run_single_child(0, "forever goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING, entry
    assert child.interrupt_seen.wait(HANG_BOUND / 2), "wall cap never stopped the child"


def test_heartbeat_ticks_are_liveness_not_progress_events():
    _Child = _real_agent_cls()
    a = _Child("sa-x")
    a._last_activity_ts = a._last_progress_ts = a._last_progress_event_ts = 0.0
    a._touch_activity("tool running: terminal", heartbeat=True)
    assert a._last_activity_ts > 0 and a._last_progress_ts > 0  # kanban: still progress
    assert a._last_progress_event_ts == 0.0
    a._touch_activity("waiting for non-streaming API response", progress=False)
    assert a._last_progress_event_ts == 0.0
    a._touch_activity("tool completed: terminal (1.0s)")
    assert a._last_progress_event_ts == a._last_activity_ts > 0
    assert a.get_activity_summary()["last_progress_event_ts"] == a._last_progress_event_ts


def test_tool_heartbeat_survives_agents_without_the_kwarg():
    from agent.tool_executor import _touch_heartbeat

    seen = []

    class _Old:
        def _touch_activity(self, desc):
            seen.append(desc)

    _touch_heartbeat(_Old(), "tick")
    assert seen == ["tick"]


@pytest.mark.parametrize(
    "cfg,want",
    [
        ({}, "default"),
        ({"child_timeout_seconds": 0}, "default"),
        ({"hung_child_seconds": 0}, None),
        ({"hung_child_seconds": -5}, None),
        ({"hung_child_seconds": 10}, "floor"),
        ({"hung_child_seconds": 1200}, 1200.0),
        ({"hung_child_seconds": "soon"}, "default"),
    ],
)
def test_hung_child_seconds_config(fleet_home, monkeypatch, cfg, want):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    got = delegate_tool._get_hung_child_seconds()
    expected = {
        "default": delegate_tool.DEFAULT_HUNG_CHILD_SECONDS,
        "floor": delegate_tool._CHILD_TIMEOUT_FLOOR_S,
    }.get(want, want)
    assert got == expected
