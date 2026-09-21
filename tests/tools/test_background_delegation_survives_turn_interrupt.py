"""A mid-turn parent interrupt must NOT kill background delegate_task children.

``delegate_task(background=true)`` detaches its children from the parent turn:
the batch's lifecycle belongs to the async registry, not to the turn that
dispatched it. So a conversational interrupt — a new inbound user message
arriving mid-turn, which the gateway turns into ``AIAgent.interrupt(text)`` —
must leave the batch running and its result must still be delivered as a
normal completion.

These tests drive the REAL ``AIAgent.interrupt`` cascade (unbound, against a
double carrying exactly the attributes it reads) over a REAL background
dispatch, and assert on the completion event that lands on the shared queue.

Contrast with an explicit ``/stop``: that goes through
``async_delegation.interrupt_for_session`` and IS terminal by design. The last
test pins that asymmetry so a future change can't quietly collapse the two.
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from gateway.session_context import set_session_vars
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_queue_and_context(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    _drain_all()
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)
    import os

    os.environ.pop("HERMES_SESSION_ID", None)
    _drain_all()


def _drain_all():
    while not process_registry.completion_queue.empty():
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            break


def _drain_one(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _interruptible_parent():
    """A parent double carrying exactly what ``AIAgent.interrupt`` reads.

    Deliberately NOT a bare MagicMock for the interrupt-relevant slots: the
    real cascade iterates ``_active_children`` and fans out to tool-worker
    tids, and a Mock would make both vacuously truthy.
    """
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "parent-sess"
    parent._interrupt_requested = False
    parent._interrupt_message = None
    parent._tool_interrupt_reason = None
    parent._pending_redirect = None
    parent._pending_redirect_lock = threading.Lock()
    parent._pending_steer = None
    parent._pending_steer_lock = threading.Lock()
    parent._hard_interrupt_requested = threading.Event()
    parent._active_compression_commit_fence = None
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._tool_worker_threads = set()
    parent._tool_worker_threads_lock = threading.Lock()
    parent._execution_thread_id = threading.current_thread().ident
    parent._interrupt_thread_signal_pending = False
    parent._active_request_abort = None
    parent.api_mode = "chat_completions"
    parent.quiet_mode = True
    return parent


def _patch_delegate(monkeypatch, child_body):
    """Patch delegate_tool's child build/run seams; ``child_body`` is the work."""
    import tools.delegate_tool as dt

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"
    fake_child.model = "m"
    fake_child.provider = "openrouter"
    fake_child.custom_provider = None
    fake_child.base_url = "https://parent.invalid/v1"
    fake_child.api_mode = "chat_completions"
    fake_child.acp_command = None
    fake_child.acp_args = None
    fake_child.enabled_toolsets = ["file", "terminal"]
    fake_child.reasoning_config = None
    fake_child.fallback_model = None
    fake_child.service_tier = None
    fake_child.providers_allowed = None
    fake_child.providers_ignored = None
    fake_child.providers_order = None
    fake_child.provider_sort = None
    fake_child.provider_require_parameters = False
    fake_child.provider_data_collection = None
    fake_child.openrouter_min_coding_score = None
    fake_child.prefill_messages = None
    fake_child.terminal_cwd = "/tmp"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }

    def _registering_build_child(**kw):
        # Reproduce the real _build_child_agent's interrupt-propagation
        # registration. Without it, asserting "_active_children is empty after
        # dispatch" would be vacuous — nothing ever put the child there, so the
        # assertion would pass even if delegate_tool's detach were deleted.
        parent = kw.get("parent_agent")
        if parent is not None and hasattr(parent, "_active_children"):
            lock = getattr(parent, "_active_children_lock", None)
            if lock:
                with lock:
                    parent._active_children.append(fake_child)
            else:
                parent._active_children.append(fake_child)
        return fake_child

    monkeypatch.setattr(dt, "_build_child_agent", _registering_build_child)
    monkeypatch.setattr(dt, "_run_single_child", child_body)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    return dt


def _bind_tui_session(session_key="parent-sess"):
    set_session_vars(
        platform="tui",
        chat_id="chat-1",
        session_key=session_key,
        session_id=session_key,
        async_delivery=True,
    )


def test_background_child_survives_mid_turn_interrupt_and_delivers(monkeypatch):
    """New inbound message mid-turn → child keeps running → normal completion."""
    import run_agent

    started = threading.Event()
    release = threading.Event()
    saw_interrupt_flag = {}

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        started.set()
        # Hold the child inside its run across the whole interrupt, then
        # record what the parent's turn state looked like from in here.
        assert release.wait(timeout=10), "child never released"
        saw_interrupt_flag["parent_interrupt_requested"] = getattr(
            parent_agent, "_interrupt_requested", None
        )
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed",
        }

    dt = _patch_delegate(monkeypatch, slow_child)
    _bind_tui_session()
    parent = _interruptible_parent()

    out = json.loads(dt.delegate_task(
        goal="long background job", context="ctx",
        background=True, parent_agent=parent,
    ))
    assert out["status"] == "dispatched", out
    assert out["mode"] == "background"
    assert started.wait(timeout=10), "background child never started"

    # The batch is detached: the parent's interrupt-propagation list must not
    # hold it, or the cascade below would abort it.
    with parent._active_children_lock:
        assert parent._active_children == []

    # THE EVENT UNDER TEST: the real interrupt cascade for an inbound message.
    run_agent.AIAgent.interrupt(parent, "wait, also check the other thing")
    assert parent._interrupt_requested is True  # the parent turn IS interrupted

    release.set()
    evt = _drain_one()
    assert evt is not None, "background completion was never delivered"
    assert evt["type"] == "async_delegation"
    assert evt["status"] == "completed", evt
    results = evt.get("results") or []
    assert results and results[0]["status"] == "completed", evt
    assert results[0]["summary"] == "done: long background job"
    # The child observed the parent's interrupted turn and ran anyway.
    assert saw_interrupt_flag["parent_interrupt_requested"] is True


def test_background_batch_runs_with_parent_interrupt_already_set(monkeypatch):
    """Even a parent interrupted BEFORE the children finish joins the batch.

    Guards ``honor_parent_interrupt=False`` on the background runner: if the
    batch honored the parent's flag it would fabricate ``interrupted`` entries
    for every still-pending child instead of waiting for real results.
    """
    import run_agent

    started = threading.Event()
    release = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        started.set()
        assert release.wait(timeout=10), "child never released"
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"done-{task_index}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed",
        }

    dt = _patch_delegate(monkeypatch, slow_child)
    _bind_tui_session()
    parent = _interruptible_parent()

    out = json.loads(dt.delegate_task(
        tasks=[
            {"goal": "first background task to run in parallel"},
            {"goal": "second background task to run in parallel"},
        ],
        context="ctx",
        background=True, parent_agent=parent,
    ))
    assert out["status"] == "dispatched", out
    assert started.wait(timeout=10)

    run_agent.AIAgent.interrupt(parent, "another message")
    release.set()

    evt = _drain_one()
    assert evt is not None
    results = evt.get("results") or []
    assert len(results) == 2, results
    assert [r["status"] for r in results] == ["completed", "completed"], results
    assert not any(
        (r.get("error") or "").startswith("Parent agent interrupted")
        for r in results
    ), results


def test_explicit_stop_still_cancels_the_background_batch(monkeypatch):
    """The asymmetry is deliberate: /stop IS terminal for background work.

    ``interrupt_for_session`` is what ``/stop`` and ``/new`` call. It must keep
    cancelling, otherwise the operator loses the escape hatch.
    """
    from tools import async_delegation

    started = threading.Event()
    cancelled = threading.Event()

    def stoppable_child(task_index, goal, child=None, parent_agent=None, **kw):
        started.set()
        # The batch interrupt hard-interrupts the child; the double records it.
        assert cancelled.wait(timeout=10), "batch interrupt never fired"
        return {
            "task_index": task_index, "status": "interrupted",
            "summary": None, "api_calls": 1, "duration_seconds": 0.1,
            "model": "m", "exit_reason": "interrupted",
        }

    dt = _patch_delegate(monkeypatch, stoppable_child)
    # Own session key: interrupt_for_session's return is a COUNT over the
    # process-wide registry, so a shared key would let sibling tests' records
    # inflate it and make `== 1` meaningless.
    _bind_tui_session(session_key="stop-sess")
    parent = _interruptible_parent()
    parent.session_id = "stop-sess"

    out = json.loads(dt.delegate_task(
        goal="cancel me: a background job the operator stops",
        context="ctx",
        background=True, parent_agent=parent,
    ))
    assert out["status"] == "dispatched", out
    assert started.wait(timeout=10)

    n = async_delegation.interrupt_for_session(
        session_key="stop-sess",
        parent_session_id="stop-sess",
        reason="stop_command",
    )
    cancelled.set()
    assert n == 1, "explicit /stop must still reach the background batch"
