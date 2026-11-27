"""A refused background delegate_task is a truthful NON-START, never a synchronous join.

Regression for t_8c4608e6 (2026-11-08): a registry-cap refusal made delegate_task run every child
INLINE, so the parent sat inside one tool call for 70+ minutes with no progress and could only be
freed by a /stop that discarded the children's work. The contract now:

* admission is checked BEFORE any child is built: a refusal costs zero child sessions;
* a refusal that only shows up after construction (a race with another dispatch) closes the built
  children unrun;
* either way the tool returns ``{"status": "rejected", "reason": ...}`` at once and no child work runs.

The tests drive the real ``delegate_task`` entry and the real async registry; only the child build
and run seams are doubles, so "zero child sessions" and "zero child execution" are counted, not assumed.
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest
import run_agent  # noqa: F401  Bootstrap recovery before the per-test home-I/O guard.

from gateway.session_context import set_session_vars
from tools import async_delegation, async_delegation_store
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    async_delegation._reset_for_tests()
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)
    async_delegation._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "parent-sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent.api_mode = "chat_completions"
    parent.quiet_mode = True
    return parent


def _seams(monkeypatch):
    """Count child builds, runs and closes through delegate_tool's real seams."""
    import tools.delegate_tool as dt

    seen = {"built": [], "ran": [], "closed": []}

    def build(**kw):
        child = MagicMock()
        child._delegate_role = "leaf"
        child._subagent_id = f"s{len(seen['built'])}"
        child.model = "m"
        child.prefill_messages = None
        seen["built"].append(child)
        return child

    def run(task_index, goal, child=None, parent_agent=None, **kw):
        seen["ran"].append(goal)
        return {"task_index": task_index, "status": "completed", "summary": goal, "api_calls": 1,
                "duration_seconds": 0.0, "model": "m", "exit_reason": "completed"}

    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_run_single_child", run)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None})
    monkeypatch.setattr(dt, "_get_detached_background", lambda: False)
    monkeypatch.setattr(dt, "_teardown", lambda c, reason, **kw: seen["closed"].append((c, reason)) or True)
    return dt, seen


def _occupy_pool(max_children):
    """Fill every async slot with units that block until released."""
    release = threading.Event()
    for i in range(max_children):
        handle = async_delegation.dispatch_async_delegation(
            goal=f"occupier {i}", context=None, toolsets=None, role="leaf", model="m",
            session_key="other-session", runner=lambda: (release.wait(30), {"status": "completed"})[1],
            max_async_children=max_children)
        assert handle["status"] == "dispatched", handle
    return release


def _delegate(dt, parent):
    started = time.monotonic()
    out = json.loads(dt.delegate_task(goal="investigate X", background=True, parent_agent=parent))
    return out, time.monotonic() - started


def test_pool_capacity_refusal_is_a_non_start(monkeypatch):
    dt, seen = _seams(monkeypatch)
    monkeypatch.setattr(dt, "_get_max_async_children", lambda: 1)
    set_session_vars(platform="tui", source="tui", chat_id="c", session_key="parent-sess", session_id="parent-sess",
                     async_delivery=True)
    release = _occupy_pool(1)
    try:
        out, elapsed = _delegate(dt, _parent())
    finally:
        release.set()

    assert out["status"] == "rejected", out
    assert out["reason"] == "pool_capacity"
    assert out["started"] is False
    assert seen["built"] == [], "a refused call must not create a child session"
    assert seen["ran"] == [], "a refused call must not run child work"
    assert elapsed < 5


@pytest.mark.parametrize("failure", ["registry_cap", "registry_error"])
def test_durable_registry_refusal_is_a_non_start(monkeypatch, failure):
    """The 2026-11-08 trigger: a gateway session whose durable registry refuses a new record."""
    dt, seen = _seams(monkeypatch)
    set_session_vars(platform="discord", chat_id="c", session_key="agent:main:discord:c",
                     session_id="parent-sess", async_delivery=True)
    if failure == "registry_cap":
        monkeypatch.setattr(async_delegation, "_MAX_REGISTRY_RECORDS", 0)
    else:
        def broken(*_a, **_k):
            raise OSError("registry unreadable")

        monkeypatch.setattr(async_delegation_store, "locked_registry", broken)

    out, elapsed = _delegate(dt, _parent())

    assert out["status"] == "rejected", out
    assert out["reason"] == failure
    assert seen["built"] == [] and seen["ran"] == []
    assert elapsed < 5


def test_unreadable_session_context_fails_closed(monkeypatch):
    """Prism 3f3a2c174875: a session-context lookup error during the durability decision must refuse
    the background unit (durable_intent_error), never start it without a restart-safe spec."""
    import sys
    import gateway.session_context as sc

    dt, seen = _seams(monkeypatch)
    set_session_vars(platform="discord", chat_id="c", session_key="agent:main:discord:c",
                     session_id="parent-sess", async_delivery=True)
    real = sc.get_session_env

    def flaky(name, default=""):
        if sys._getframe(1).f_code.co_name in ("_durable_intent_expected", "_build_durable_background_spec"):
            raise RuntimeError("context store unavailable")
        return real(name, default)

    monkeypatch.setattr(sc, "get_session_env", flaky)
    out, elapsed = _delegate(dt, _parent())

    assert out["status"] == "rejected", out
    assert out["reason"] == "durable_intent_error"
    assert seen["ran"] == [], "a refused unit must not run child work"
    assert len(seen["closed"]) == len(seen["built"]), "every built child is closed unrun"
    assert elapsed < 5


def test_refusal_after_construction_closes_children_unrun(monkeypatch):
    """Preflight passed, then another dispatch took the slot: the built child is closed, never joined."""
    dt, seen = _seams(monkeypatch)
    monkeypatch.setattr(dt, "_get_max_async_children", lambda: 1)
    monkeypatch.setattr(async_delegation, "preflight_admission", lambda **_k: None)
    set_session_vars(platform="tui", source="tui", chat_id="c", session_key="parent-sess", session_id="parent-sess",
                     async_delivery=True)
    release = _occupy_pool(1)
    try:
        out, elapsed = _delegate(dt, _parent())
    finally:
        release.set()

    assert out["status"] == "rejected", out
    assert out["reason"] == "pool_capacity"
    assert len(seen["built"]) == 1
    assert seen["ran"] == [], "the refused unit was joined synchronously"
    assert [c for c, _ in seen["closed"]] == seen["built"]
    assert elapsed < 5


def test_finite_session_keeps_its_synchronous_contract(monkeypatch):
    """A session that cannot receive a later completion still runs inline (unchanged contract)."""
    dt, seen = _seams(monkeypatch)
    monkeypatch.setattr(dt, "_get_max_async_children", lambda: 1)
    set_session_vars(platform="api_server", chat_id="c", session_key="k", session_id="",
                     async_delivery=False)
    monkeypatch.setattr(async_delegation, "_current_origin_session_id", lambda: "")  # no wake target
    release = _occupy_pool(1)  # capacity is irrelevant: a finite session never asks for admission
    try:
        out, _ = _delegate(dt, _parent())
    finally:
        release.set()

    assert out.get("status") != "rejected", out
    assert seen["ran"] == ["investigate X"]
