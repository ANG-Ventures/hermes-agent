"""A child that FAILS BY RETURNING is failed, and its live subtree is reaped.

Argus milestone QA t_86717544, scenario ``t_orch_fail``: an orchestrator child
spawned a grandchild, then its next API call got a non-retryable HTTP 400.
``run_conversation`` RETURNS for that (``failed=True``, the error text in
``final_response``), it does not raise. Two defects followed:

- F4: the child was recorded ``status=completed, exit_reason=max_iterations``
  because the error text counted as a summary.
- F1: only the raise path reaped the subtree, so the grandchild ran on to its
  own wall cap.

Both the direct path (``_run_single_child``) and the late path (after
``timed_out_running``) are pinned here, plus the positive control: a child that
completes normally does NOT reap the grandchild it left running.

Real imports of tools.delegate_tool against a temp fleet profile home; only the
agents are stubs.
"""
from __future__ import annotations

import json
import threading
import time
import weakref

import pytest

_HTTP_400 = "HTTP 400: qa forced failure"


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "qa"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _clean_registry():
    from tools import delegate_tool

    with delegate_tool._active_subagents_lock:
        delegate_tool._active_subagents.clear()
    yield
    with delegate_tool._active_subagents_lock:
        delegate_tool._active_subagents.clear()


class _Agent:
    """Minimal AIAgent stand-in: interruptible, steerable, tree-linked."""

    def __init__(self, sid, parent=None, depth=1, behavior=None):
        self._subagent_id = sid
        self._parent_subagent_id = getattr(parent, "_subagent_id", None)
        self._delegate_parent_ref = weakref.ref(parent) if parent is not None else None
        self._delegate_depth = depth
        self._delegate_role = "orchestrator"
        self.model = "test/model"
        self.session_id = f"sess-{sid or 'root'}"
        self._behavior = behavior
        self.interrupt_seen = threading.Event()
        self.stopped = threading.Event()
        self.steered = []
        self.events = []
        self.tool_progress_callback = self._record

    def _record(self, event, **kw):
        self.events.append((event, kw))

    def get_activity_summary(self):
        return {
            "api_call_count": 1,
            "max_iterations": 10,
            "current_tool": None,
            "last_activity_ts": time.time(),
        }

    def hard_interrupt(self, message=None, **_kw):
        self.interrupt_seen.set()

    def steer(self, text):
        self.steered.append(text)
        return True

    def close(self):
        pass

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        return self._behavior(self)


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _registered(*ids):
    from tools import delegate_tool

    with delegate_tool._active_subagents_lock:
        return all(i in delegate_tool._active_subagents for i in ids)


def _spin(self):
    # Runs until interrupted, then records the stop (the FOREVER grandchild).
    while not self.interrupt_seen.wait(0.02):
        pass
    self.stopped.set()
    return {"final_response": "", "interrupted": True, "api_calls": 1}


def _failed_return():
    # The exact shape conversation_loop returns on a non-retryable client error.
    return {
        "final_response": _HTTP_400,
        "messages": [],
        "api_calls": 2,
        "completed": False,
        "failed": True,
        "error": _HTTP_400,
    }


def _spawn_grandchild(owner, sid):
    from tools import delegate_tool

    g = _Agent(sid, parent=owner, depth=2, behavior=_spin)
    t = threading.Thread(
        target=delegate_tool._run_single_child,
        args=(0, "grandchild", g, owner),
        daemon=True,
    )
    t.start()
    assert _wait_until(lambda: _registered(sid))
    return g, t


@pytest.mark.parametrize(
    "result, expected",
    [
        (_failed_return(), ("failed", "error")),
        ({"final_response": "", "failed": True, "error": "x"}, ("failed", "error")),
        ({"final_response": "done", "completed": True}, ("completed", "completed")),
        ({"final_response": "partial", "completed": False}, ("completed", "max_iterations")),
        ({"final_response": "(empty)", "completed": False}, ("failed", "max_iterations")),
        ({"final_response": "", "interrupted": True}, ("interrupted", "interrupted")),
    ],
)
def test_terminal_outcome_classifier(result, expected):
    from tools import delegate_tool

    assert delegate_tool._classify_child_outcome(result) == expected


def test_returned_api_failure_is_failed_and_reaps_subtree(fleet_home, monkeypatch):
    """F1+F4, direct path: failed/error, and the live grandchild is stopped."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    parent = _Agent(None, depth=0)
    box = {}

    def _spawn_then_400(self):
        box["g"], box["t"] = _spawn_grandchild(self, "sa-0-gc1")
        return _failed_return()

    child = _Agent("sa-0-orch", parent=parent, behavior=_spawn_then_400)
    entry = delegate_tool._run_single_child(0, "orch", child, parent)

    assert entry["status"] == "failed"
    assert entry["exit_reason"] == "error"
    assert entry["truncated"] is False
    assert entry["error"] == _HTTP_400
    assert box["g"].stopped.wait(2), "grandchild outlived its failed-by-return parent"
    box["t"].join(5)
    assert not _registered("sa-0-gc1")


def test_late_returned_api_failure_is_failed_and_reaps_subtree(fleet_home, monkeypatch):
    """F1+F4, late path (Argus t_orch_fail shape): after timed_out_running."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    parent = _Agent(None, depth=0)
    box = {}
    release = threading.Event()

    def _spawn_wait_then_400(self):
        box["g"], box["t"] = _spawn_grandchild(self, "sa-0-gc1")
        release.wait(10)
        return _failed_return()

    child = _Agent("sa-0-orch", parent=parent, behavior=_spawn_wait_then_400)
    entry = delegate_tool._run_single_child(0, "orch", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING

    release.set()
    assert box["g"].stopped.wait(5), "grandchild outlived its failed-by-return parent"
    box["t"].join(5)
    assert _wait_until(lambda: not _registered("sa-0-orch", "sa-0-gc1"))
    assert _wait_until(lambda: parent.steered), "late result never delivered"
    assert "status=failed" in parent.steered[0]
    late_dir = fleet_home / "cache" / "delegation" / "late"
    assert _wait_until(lambda: any(late_dir.glob("late-sa-0-orch-*.json")))
    rec = json.loads(next(late_dir.glob("late-sa-0-orch-*.json")).read_text())
    rec = rec.get("entry", rec)
    assert (rec["status"], rec["exit_reason"]) == ("failed", "error")


def test_completed_child_leaves_its_running_grandchild_alone(fleet_home, monkeypatch):
    """Positive control: a legitimate completion is not a failure; no reap."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    parent = _Agent(None, depth=0)
    box = {}

    def _spawn_then_finish(self):
        box["g"], box["t"] = _spawn_grandchild(self, "sa-0-gc2")
        return {"final_response": "DONE ORCH2", "completed": True, "api_calls": 2}

    child = _Agent("sa-0-orch", parent=parent, behavior=_spawn_then_finish)
    entry = delegate_tool._run_single_child(0, "orch", child, parent)
    try:
        assert (entry["status"], entry["exit_reason"]) == ("completed", "completed")
        assert not box["g"].interrupt_seen.wait(0.5), "a completed child reaped its grandchild"
        assert _registered("sa-0-gc2")
    finally:
        box["g"].interrupt_seen.set()
        box["t"].join(5)
