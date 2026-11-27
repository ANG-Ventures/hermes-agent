"""/stop on the parent ends its TURN, never its registry-owned background delegations.

Regression for t_8c4608e6 (Ace, 2026-11-08: "I don't want /stop to throw away a parent's work").
A background child dispatched through the real ``delegate_task`` is held at a gate; the parent takes a
follow-up user message (the real ``AIAgent.interrupt`` cascade) and then a gateway ``/stop`` (the real
``_interrupt_and_clear_session`` with ``stop_command``). The child must receive no cancellation, finish
after the stop, and deliver its result through the completion outbox. ``delegate_task action='stop'``
and a session reset remain the ways to end it.
"""

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest
import run_agent

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import set_session_vars
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from tools import async_delegation
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


def _interruptible_parent():
    """Carries exactly what ``AIAgent.interrupt`` reads (a bare Mock would make the cascade vacuous)."""
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


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    # object.__new__ skips __init__: pin the session DB to None so the /stop path's user_stopped marker
    # write is a clean no-op instead of opening a store this bare runner has no handles for.
    runner._session_db_pinned = None
    runner._running_agents = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner.adapters = {}
    runner._pending_messages = {}
    runner._invalidate_session_run_generation = lambda key, reason=None: None
    runner._release_running_agent_state = lambda key, **kw: None
    return runner


def test_gateway_stop_leaves_background_child_running_and_delivering(monkeypatch):
    import tools.delegate_tool as dt
    from gateway.run import _INTERRUPT_REASON_STOP

    started, release, cancelled = threading.Event(), threading.Event(), []

    def build(**kw):
        child = MagicMock()
        child._delegate_role = "leaf"
        child._subagent_id = "s1"
        child.model = "m"
        child.prefill_messages = None
        return child

    def run(task_index, goal, child=None, parent_agent=None, **kw):
        started.set()
        assert release.wait(30), "test never released the child"
        status = "interrupted" if cancelled else "completed"
        return {"task_index": task_index, "status": status, "summary": f"done: {goal}", "api_calls": 1,
                "duration_seconds": 0.0, "model": "m", "exit_reason": status}

    def record_cancel(child, *a, **k):
        cancelled.append(child)
        release.set()  # a cancelled child unwinds at once, like the real one
        return True

    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_run_single_child", run)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None})
    monkeypatch.setattr(dt, "_get_detached_background", lambda: False)
    monkeypatch.setattr(dt, "request_hard_interrupt", record_cancel)
    monkeypatch.setattr(process_registry, "kill_started_since", lambda *a, **k: 0)
    set_session_vars(platform="tui", source="tui", chat_id="c", session_key="parent-sess",
                     session_id="parent-sess", async_delivery=True)
    parent = _interruptible_parent()

    out = json.loads(dt.delegate_task(goal="long review", background=True, parent_agent=parent))
    assert out["status"] == "dispatched", out
    assert started.wait(10)

    # 1. The parent handles a follow-up user message while the child is still gated.
    run_agent.AIAgent.interrupt(parent, "also check the other file")
    assert parent._interrupt_requested is True
    assert cancelled == []

    # 2. /stop on the parent (the gateway busy path).
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm")
    asyncio.run(_runner()._interrupt_and_clear_session(
        "parent-sess", source, interrupt_reason=_INTERRUPT_REASON_STOP, invalidation_reason="stop_command"))

    assert cancelled == [], "/stop cancelled a registry-owned background child"
    assert async_delegation.active_count() == 1, "the child's registry record did not survive /stop"

    # 3. Released after the stop, the child completes and its result reaches the outbox.
    release.set()
    # The completion queue is process-global: a background unit from an earlier test can finish late and
    # land first. Take THIS delegation's event; any other id is someone else's late delivery.
    deadline = time.monotonic() + 10
    while True:
        evt = process_registry.completion_queue.get(timeout=max(0.1, deadline - time.monotonic()))
        if evt.get("delegation_id") == out["delegation_id"]:
            break
    assert evt["type"] == "async_delegation"
    assert evt["status"] == "completed", evt
    assert evt["results"][0]["summary"] == "done: long review"


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.TELEGRAM)

    @property
    def name(self):
        return "telegram"

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["stop_idle", "stop_busy", "new"])
async def test_real_runner_stop_spares_and_reset_ends_background_units(monkeypatch, command):
    """Real GatewayRunner: /stop (idle or busy) leaves the session's background unit alone; /new, which
    leaves it no return address, still ends it. Supersedes the #114456 stop-ends-them invariant."""
    from gateway.run import GatewayRunner

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "u1")
    adapter = _Adapter()
    runner = GatewayRunner(config=GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="x")}))
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1", user_name="tester")
    key = adapter._event_session_key(MessageEvent(text="", message_type=MessageType.TEXT, source=source))
    unit_fn = MagicMock()
    with async_delegation._records_lock:
        async_delegation._records["deleg_bg1"] = {
            "delegation_id": "deleg_bg1", "status": "running", "session_key": key,
            "origin_ui_session_id": "", "parent_session_id": "", "interrupt_fn": unit_fn}

    if command == "stop_idle":
        await runner._handle_stop_command(MessageEvent(text="/stop", message_type=MessageType.TEXT, source=source))
    elif command == "stop_busy":
        await runner._interrupt_and_clear_session(key, source, interrupt_reason="stop",
                                                  invalidation_reason="stop_command")
    else:
        await runner._interrupt_and_clear_session(key, source, interrupt_reason="reset",
                                                  invalidation_reason="new_command")

    if command == "new":
        unit_fn.assert_called_once()
    else:
        unit_fn.assert_not_called()
