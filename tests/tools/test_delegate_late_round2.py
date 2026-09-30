"""Behavior contracts for #1542's Prism round-2 P1s on the late-completion path.

Prism pre-merge review of ANG-Ventures/hermes-agent#1542 (head 43a197f9)
left six P1s on the thread that owns a child after its delegate_task wait
returned ``timed_out_running``. One test (or test group) per finding:

1. Infinite polling: a child that ends by RAISING ``TimeoutError`` is
   delivered as an error, not spun on forever; and a child that keeps
   making progress is still bounded by an absolute wall ceiling
   (``delegation.child_max_wall_seconds``, default 4x child_timeout).
2. Unbounded retry: the late ``output_schema`` correction turn runs under the
   same progress/wall supervision; a stalled retry is stopped, its lease
   released and a result recorded.
3. Hidden results: an owned late result on disk stays visible through
   ``action='list'`` no matter how many newer foreign results exist.
4.-6. One class: the late worker runs in the OWNING profile's context. A
   secondary profile selected by the context-local home override (not the
   process env) gets its late result persisted under its own home, the
   advertised ``late_result_path`` is where the file really is, and the
   correction turn runs with that profile and the subagent approval callback.

Real imports of tools.delegate_tool against temp fleet homes; only the
agents are stubs (shared with test_delegate_late_completion).
"""
from __future__ import annotations

import json
import os
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


# 1a --------------------------------------------------------------------------
def test_child_raising_timeout_error_is_delivered_not_polled_forever(
    fleet_home, monkeypatch
):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()

    def _raise(self):
        release.wait(10)
        raise TimeoutError("provider read timed out")

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-raise", parent=parent, api_calls=2, behavior=_raise)
    pool = _Pool()
    child._credential_pool = pool
    entry = delegate_tool._run_single_child(0, "raise goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    release.set()

    assert _wait_until(lambda: _late_results(parent), timeout=10.0), (
        "a child that raised TimeoutError was polled forever"
    )
    (late,) = _late_results(parent)
    assert late["status"] == "error"
    assert "TimeoutError" in late["error"]
    assert _wait_until(lambda: pool.released == ["cred-1"])
    assert not _registered("sa-0-raise")


# 1b --------------------------------------------------------------------------
def test_progressing_child_is_bounded_by_absolute_wall_ceiling(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.2)
    monkeypatch.setattr(
        delegate_tool, "_get_child_max_wall_seconds", lambda ct: 0.8, raising=False
    )
    forever = threading.Event()

    def _busy(self):
        # Activity timestamp advances on every read: always "making progress".
        forever.wait(30)
        return {"final_response": "never", "completed": True, "api_calls": 9}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-busy", parent=parent, api_calls=2, behavior=_busy)
    pool = _Pool()
    child._credential_pool = pool
    try:
        start = time.monotonic()
        entry = delegate_tool._run_single_child(0, "busy goal", child, parent)
        assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
        assert _wait_until(lambda: _late_results(parent), timeout=10.0), (
            "a child that keeps making progress was polled without a wall ceiling"
        )
        assert time.monotonic() - start < 10.0
        (late,) = _late_results(parent)
        assert late["status"] == "timeout"
        assert "wall" in late["error"]
        assert child.interrupt_seen.is_set()
        assert _wait_until(lambda: pool.released == ["cred-1"])
        assert not _registered("sa-0-busy")
        rec = json.loads(open(late["result_path"], encoding="utf-8").read())
        assert rec["entry"]["timeout_phase"] == "wall_ceiling_after_timed_out_running"
    finally:
        forever.set()


def test_child_max_wall_seconds_config_contract(monkeypatch):
    from tools import delegate_tool

    cfg = {}
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    get = delegate_tool._get_child_max_wall_seconds
    assert get(None) is None  # no child_timeout -> no late path to bound
    assert get(60.0) == 240.0  # unset: 4x child_timeout
    assert get(0.3) == 120.0  # never below 4x child_timeout's own 30 s floor
    for val, want in ((0, 240.0), (-5, 240.0), ("junk", 240.0), (100, 100.0), (10, 60.0)):
        cfg["child_max_wall_seconds"] = val
        assert get(60.0) == want, val

    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["child_max_wall_seconds"] == 0


# 2 ---------------------------------------------------------------------------
def test_stalled_late_schema_retry_is_supervised_and_bounded(fleet_home, monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()
    forever = threading.Event()

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {"final_response": "not json", "completed": True, "api_calls": 3}
        # Correction turn: wedged, no activity, ignores interrupts.
        self.frozen_activity_ts = time.time() - 60
        forever.wait(30)
        return {"final_response": '{"answer": 1}', "completed": True, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-retry", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    pool = _Pool()
    child._credential_pool = pool
    try:
        entry = delegate_tool._run_single_child(0, "retry goal", child, parent)
        assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
        release.set()
        assert _wait_until(lambda: _late_results(parent), timeout=10.0), (
            "a stalled schema-correction turn blocked the late result"
        )
        (late,) = _late_results(parent)
        assert len(child.calls) == 2
        assert late["status"] == "timeout"
        assert "schema-correction" in late["error"]
        assert "not json" in (late["summary"] or ""), "first answer was dropped"
        assert child.interrupt_seen.is_set()
        assert _wait_until(lambda: pool.released == ["cred-1"])
        assert not _registered("sa-0-retry")
    finally:
        forever.set()


def test_stalled_retry_drains_the_correction_turn_and_keeps_first_turn_steer(
    fleet_home, monkeypatch
):
    """Prism #1549 r1: teardown waits on the RUNNING correction turn (not the
    finished first turn), and the first turn's finalizer-drained steer is
    still reported as missed_steer."""
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    release = threading.Event()
    order = []

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {
                "final_response": "not json",
                "completed": True,
                "api_calls": 3,
                "pending_steer": "use the staging db",
            }
        # Correction turn: no activity until interrupted, then unwinds
        # cooperatively (persisting takes a moment) before returning.
        self.frozen_activity_ts = time.time() - 60
        self.interrupt_seen.wait(10)
        time.sleep(0.3)
        order.append("retry_unwound")
        return {"final_response": "", "completed": False, "api_calls": 0}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-drain", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA
    child.close = lambda: order.append("close")
    entry = delegate_tool._run_single_child(0, "drain goal", child, parent)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    release.set()

    assert _wait_until(lambda: _late_results(parent), timeout=10.0)
    (late,) = _late_results(parent)
    assert late["status"] == "timeout"
    assert late.get("missed_steer") == "use the staging db", late
    assert _wait_until(lambda: "close" in order, timeout=5.0)
    assert order.index("retry_unwound") < order.index("close"), (
        f"child closed under a still-running correction turn: {order}"
    )


# 3 ---------------------------------------------------------------------------
def test_owned_late_result_listed_despite_many_newer_foreign_results(fleet_home):
    from tools import delegate_tool

    d = delegate_tool._late_results_dir()
    d.mkdir(parents=True, exist_ok=True)
    parent = _Agent(None, depth=0)
    owned = {
        "late_result_id": "late-mine",
        "owner_agent_session_id": parent.session_id,
        "finished_at": time.time() - 3600,
        "entry": {"subagent_id": "sa-mine", "status": "completed", "summary": "mine"},
    }
    (d / "late-mine.json").write_text(json.dumps(owned), encoding="utf-8")
    old = time.time() - 3600
    os.utime(d / "late-mine.json", (old, old))
    for i in range(250):
        rec = {
            "late_result_id": f"late-other-{i}",
            "owner_agent_session_id": f"sess-other-{i}",
            "finished_at": time.time(),
            "entry": {"subagent_id": f"sa-o{i}", "status": "completed", "summary": "x"},
        }
        (d / f"late-other-{i}.json").write_text(json.dumps(rec), encoding="utf-8")

    listed = _late_results(parent)
    assert [r["late_result_id"] for r in listed] == ["late-mine"]
    assert listed[0]["summary"] == "mine"


# 4-6 -------------------------------------------------------------------------
def test_late_result_persists_under_the_owning_profile_context(
    fleet_home, tmp_path, monkeypatch
):
    """Secondary profile via the context-local override, NOT the process env."""
    from tools import delegate_tool
    from tools import terminal_tool
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    profile_home = tmp_path / "profiles" / "secondary"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 0.3)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    release = threading.Event()
    seen = {}

    def _answers(self):
        if len(self.calls) == 1:
            release.wait(10)
            return {"final_response": "not json", "completed": True, "api_calls": 3}
        seen["home"] = str(get_hermes_home())
        seen["approval"] = terminal_tool._get_approval_callback()
        return {"final_response": '{"answer": 7}', "completed": True, "api_calls": 1}

    parent = _Agent(None, depth=0)
    child = _Agent("sa-0-prof", parent=parent, api_calls=2, behavior=_answers)
    child._delegate_output_schema = _SCHEMA

    token = set_hermes_home_override(profile_home)
    try:
        entry = delegate_tool._run_single_child(0, "profile goal", child, parent)
    finally:
        reset_hermes_home_override(token)
    assert entry["status"] == delegate_tool.TIMED_OUT_RUNNING
    advertised = entry["late_result_path"]
    assert advertised.startswith(str(profile_home))
    release.set()

    assert _wait_until(lambda: os.path.exists(advertised), timeout=10.0), (
        "late result was not written where it was advertised (owning profile)"
    )
    rec = json.loads(open(advertised, encoding="utf-8").read())
    assert json.loads(rec["entry"]["summary"]) == {"answer": 7}
    default_late = list(fleet_home.rglob("late-*.json"))
    assert default_late == [], f"late result leaked into the default home: {default_late}"
    assert seen["home"] == str(profile_home), "correction turn ran in the wrong profile"
    assert seen["approval"] is delegate_tool._subagent_auto_deny, (
        "correction turn ran without the subagent approval callback"
    )

    # Durable read-back from the owning profile after in-memory eviction.
    with delegate_tool._late_results_lock:
        delegate_tool._late_results.clear()
    token = set_hermes_home_override(profile_home)
    try:
        listed = _late_results(parent)
    finally:
        reset_hermes_home_override(token)
    assert [r["result_path"] for r in listed] == [advertised]
