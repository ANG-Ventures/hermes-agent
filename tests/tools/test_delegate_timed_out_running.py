"""Behavior contracts: timed out != dead, subtree reap, tree-shaped list.

2026-09-08 web-call burst: a root's delegate_task wait timed out, the child
was reported dead while its descendant tree kept running, and the root
relaunched the same brief, so two trees ran at once. These tests pin:

- a wait that hits child_timeout_seconds while the child works returns
  ``timed_out_running`` (no failure text) and the child's real result still
  arrives later through the completion path;
- a child that genuinely fails has its whole live subtree cancelled;
- ``action='list'`` reports depth + parent id for every live descendant.

Real imports of tools.delegate_tool against a temp fleet home; only the
agents are stubs.
"""
from __future__ import annotations

import json
import logging
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

    with delegate_tool._active_subagents_lock:
        delegate_tool._active_subagents.clear()
    yield
    with delegate_tool._active_subagents_lock:
        delegate_tool._active_subagents.clear()


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
        self.interrupt_seen = threading.Event()
        self.stopped = threading.Event()
        self.steered = []
        self.events = []
        self.tool_progress_callback = self._record

    def _record(self, event, **kw):
        self.events.append((event, kw))

    def get_activity_summary(self):
        return {
            "api_call_count": self._api_calls,
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


def test_timeout_while_working_returns_timed_out_running_and_late_result_arrives(
    fleet_home, monkeypatch, caplog
):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()

    def _slow(self):
        release.wait(10)
        return {"final_response": "late answer", "completed": True, "api_calls": 3}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-slow", parent=parent, api_calls=2, behavior=_slow)

    with caplog.at_level(logging.INFO, logger=delegate_tool.logger.name):
        entry = delegate_tool._run_single_child(0, "slow goal", child, parent)

    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    assert "error" not in entry
    assert entry["subagent_id"] == "sa-0-slow"
    assert [e["subagent_id"] for e in entry["live_subagents"]] == ["sa-0-slow"]
    assert entry["control"]["stop"] == {"action": "stop", "subagent_id": "sa-0-slow"}
    assert "timed out ≠ dead" in entry["note"].lower()
    assert not child.interrupt_seen.is_set(), "a working child must not be stopped"
    assert any("timed_out_running" in r.getMessage() for r in caplog.records)

    # Still live and controllable after the owner returned.
    listing = json.loads(delegate_tool._handle_control_action("list", None, None, parent))
    assert [s["subagent_id"] for s in listing["subagents"]] == ["sa-0-slow"]

    release.set()
    assert _wait_until(lambda: parent.steered), "late result never delivered"
    assert "late answer" in parent.steered[0]
    assert "status=completed" in parent.steered[0]
    assert _wait_until(lambda: not _registered("sa-0-slow"))
    completes = [kw for ev, kw in child.events if ev == "subagent.complete"]
    assert [c["status"] for c in completes] == ["completed"]


def test_parent_failure_reaps_grandchildren(fleet_home, monkeypatch, caplog):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)

    def _spin(self):
        # Test-only sentinel: runs until interrupted, then records the stop.
        while not self.interrupt_seen.wait(0.02):
            pass
        self.stopped.set()
        return {"final_response": "", "interrupted": True, "api_calls": 1}

    parent = _Agent(None, depth=0)
    grandkids = []
    threads = []

    def _spawn_then_raise(self):
        for i in range(2):
            g = _Agent(f"sa-{i}-gk", parent=self, depth=2, behavior=_spin)
            grandkids.append(g)
            t = threading.Thread(
                target=delegate_tool._run_single_child,
                args=(i, "grandchild", g, self),
                daemon=True,
            )
            t.start()
            threads.append(t)
        assert _wait_until(lambda: _registered("sa-0-gk", "sa-1-gk"))
        raise RuntimeError("parent subagent crashed")

    child = _Agent("sa-0-mid", parent=parent, behavior=_spawn_then_raise)
    with caplog.at_level(logging.INFO, logger=delegate_tool.logger.name):
        entry = delegate_tool._run_single_child(0, "mid", child, parent)

    assert entry["status"] == "error"
    for g in grandkids:
        # One poll interval of the batch runner (0.5 s).
        assert g.stopped.wait(0.5), f"{g._subagent_id} outlived its failed parent"
    for t in threads:
        t.join(5)
    assert not _registered("sa-0-gk") and not _registered("sa-1-gk")
    assert any("reap" in r.getMessage() and "sa-0-gk" in r.getMessage() for r in caplog.records)


def test_detached_descendant_is_not_reaped(fleet_home):
    from tools import delegate_tool

    parent = _Agent(None, depth=0)
    mid = _Agent("sa-0-mid", parent=parent)
    kept = _Agent("sa-0-kept", parent=mid, depth=2)
    kept._delegate_detach = True
    gone = _Agent("sa-1-gone", parent=mid, depth=2)
    for a in (kept, gone):
        delegate_tool._register_subagent(
            {"subagent_id": a._subagent_id, "parent_id": "sa-0-mid", "agent": a}
        )
    assert delegate_tool._reap_subtree(mid, "error") == ["sa-1-gone"]
    assert gone.interrupt_seen.is_set() and not kept.interrupt_seen.is_set()


def test_list_shows_depth_and_parent_for_two_level_tree(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    release = threading.Event()

    def _wait(self):
        release.wait(10)
        return {"final_response": "done", "completed": True, "api_calls": 1}

    parent = _Agent(None, depth=0)
    gthreads = []

    def _spawn_and_wait(self):
        g = _Agent("sa-0-leaf", parent=self, depth=2, behavior=_wait)
        t = threading.Thread(
            target=delegate_tool._run_single_child,
            args=(0, "leaf", g, self),
            daemon=True,
        )
        t.start()
        gthreads.append(t)
        return _wait(self)

    child = _Agent("sa-0-lead", parent=parent, behavior=_spawn_and_wait)
    ct = threading.Thread(
        target=delegate_tool._run_single_child,
        args=(0, "lead", child, parent),
        daemon=True,
    )
    ct.start()
    try:
        assert _wait_until(lambda: _registered("sa-0-lead", "sa-0-leaf"))
        listing = json.loads(
            delegate_tool._handle_control_action("list", None, None, parent)
        )
        by_id = {s["subagent_id"]: s for s in listing["subagents"]}
        assert by_id["sa-0-lead"]["depth"] == 1
        assert by_id["sa-0-lead"]["parent_id"] is None
        assert by_id["sa-0-leaf"]["depth"] == 2
        assert by_id["sa-0-leaf"]["parent_id"] == "sa-0-lead"
    finally:
        release.set()
        ct.join(5)
        for t in gthreads:
            t.join(5)

