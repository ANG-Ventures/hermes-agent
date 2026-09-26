"""#82888 regression: internal wake rows must reach state.db typed.

Measured on the live fleet (2026-09-24): every kanban wake / internal user row
for 3 days was persisted with ``display_kind`` NULL, although
``_handle_message_with_agent`` computes ``persist_user_display_kind`` and
passes it to ``_run_agent``. The marker was dropped one hop later, by the
session-binding wrapper ``GatewayRunner._run_agent``, whose two hand-copied
``_run_agent_inner`` call lists omitted ``persist_user_display_kind`` (and
``message_type``). The drained-queue path (``_run_queued_followup_if_current``)
never passed it at all, so a wake that arrived while the session was busy lost
it too.

The existing #82888 tests mock ``_run_agent`` and so never crossed the
wrapper. These tests go through it:

1. class detector: every parameter of ``_run_agent`` reaches
   ``_run_agent_inner`` on both the plain and the multiplexed branch;
2. the persisted row: an internal turn driven through the real
   ``_run_agent`` -> ``_run_agent_admitted`` -> ``agent.run_conversation``
   lands in a temp SessionDB with ``display_kind='internal_notification'``;
3. a queued internal event drained in-band keeps the marker; a queued real
   user event does not get it.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Bind the REAL agent + persistence before _install_fakes swaps run_agent.
from agent.turn_context import build_turn_context
from hermes_state import SessionDB
from run_agent import AIAgent as RealAIAgent

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource

SESSION_KEY = "agent:main:telegram:group:-1001"
WAKE = "[kanban] Task t_88ce1aef gave up (retries exhausted), crashed (worker exited)"


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


# ── 1: class detector — the wrapper forwards its whole signature ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplex", [False, True])
async def test_run_agent_wrapper_forwards_every_parameter(monkeypatch, multiplex):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=multiplex)
    runner._set_session_vars_for_source = lambda **_kw: []
    runner._resolve_profile_home_for_source = lambda _source: None
    monkeypatch.setattr(
        gateway_run, "_profile_runtime_scope", lambda _home: contextlib.nullcontext()
    )

    captured = {}

    async def _inner(message, context_prompt, history, source, session_id, **kwargs):
        captured.update(
            message=message,
            context_prompt=context_prompt,
            history=history,
            source=source,
            session_id=session_id,
            **kwargs,
        )
        return {}

    runner._run_agent_inner = _inner

    params = [
        name
        for name in inspect.signature(gateway_run.GatewayRunner._run_agent).parameters
        if name != "self"
    ]
    sentinels = {name: object() for name in params}
    await runner._run_agent(**sentinels)

    missing = [name for name in params if captured.get(name) is not sentinels[name]]
    assert not missing, f"_run_agent dropped {missing} before _run_agent_inner"


# ── 2: the persisted row, through the real wrapper and admitted runner ───


@pytest.fixture()
def real_agent_db(tmp_path):
    db = SessionDB(Path(tmp_path) / "state.db")
    sid = "sess-82888-e2e"
    db.create_session(session_id=sid, source="telegram", model="test-model")
    agent = RealAIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_db=db,
        session_id=sid,
    )
    agent._session_db_created = True
    agent._cached_system_prompt = "SYSTEM"
    agent._skip_mcp_refresh = True
    try:
        yield agent, db, sid
    finally:
        db.close()


def _persisting_agent_cls(real_agent):
    """Gateway-facing agent whose run_conversation writes the user row the way
    the real one does at turn start (build_turn_context's crash persist)."""

    class PersistingAgent:
        def __init__(self, **kwargs):
            self.tools = []

        def run_conversation(self, message, **kwargs):
            with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
                build_turn_context(
                    agent=real_agent,
                    user_message=message,
                    system_message=None,
                    conversation_history=None,
                    task_id=None,
                    stream_callback=None,
                    persist_user_message=kwargs.get("persist_user_message"),
                    persist_user_display_kind=kwargs.get("persist_user_display_kind"),
                    restore_or_build_system_prompt=lambda *a, **k: None,
                    install_safe_stdio=lambda: None,
                    sanitize_surrogates=lambda s: s,
                    summarize_user_message_for_log=lambda s: s,
                    set_session_context=lambda _sid: None,
                    set_current_write_origin=lambda _o: None,
                    ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
                )
            return {"final_response": "ack", "messages": [], "api_calls": 1}

    return PersistingAgent


async def _drive(monkeypatch, tmp_path, real_agent, *, display_kind):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _persisting_agent_cls(real_agent)
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = _make_runner(_Adapter())
    kwargs = {}
    if display_kind is not None:
        kwargs["persist_user_display_kind"] = display_kind
    result = await runner._run_agent(
        message=WAKE,
        context_prompt="",
        history=[],
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="-1001"),
        session_id="sess-82888-e2e",
        session_key=SESSION_KEY,
        **kwargs,
    )
    assert result["final_response"] == "ack"


@pytest.mark.asyncio
async def test_internal_turn_row_is_persisted_typed(monkeypatch, tmp_path, real_agent_db):
    agent, db, sid = real_agent_db

    await _drive(monkeypatch, tmp_path, agent, display_kind="internal_notification")

    row, = [r for r in db.get_messages_as_conversation(sid) if r["role"] == "user"]
    assert row["content"] == WAKE
    assert row["display_kind"] == "internal_notification"


@pytest.mark.asyncio
async def test_real_user_turn_row_stays_untyped(monkeypatch, tmp_path, real_agent_db):
    agent, db, sid = real_agent_db

    await _drive(monkeypatch, tmp_path, agent, display_kind=None)

    row, = [r for r in db.get_messages_as_conversation(sid) if r["role"] == "user"]
    assert row.get("display_kind") is None


# ── 3: queued (drained in-band) internal events keep the marker ──────────


def test_queued_followup_carries_internal_marker():
    """The in-band drained follow-up (``_run_agent`` recursing on the dequeued
    ``pending_event``) must derive ``persist_user_display_kind`` from that
    event's ``internal`` flag, or a wake drained while busy persists untyped."""
    import ast

    gateway_run = importlib.import_module("gateway.run")
    tree = ast.parse(Path(gateway_run.__file__).read_text(encoding="utf-8"))

    calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "_run_agent":
            continue
        kw = {k.arg: k.value for k in node.keywords}
        depth = kw.get("_interrupt_depth")
        if isinstance(depth, ast.BinOp) and isinstance(depth.op, ast.Add):
            calls.append(kw)

    assert calls, "in-band queued follow-up _run_agent call not found"
    for kw in calls:
        kind = kw.get("persist_user_display_kind")
        assert isinstance(kind, ast.IfExp), "follow-up drops persist_user_display_kind"
        src = ast.unparse(kind)
        assert "'internal_notification'" in src
        assert "getattr(pending_event, 'internal'" in src
