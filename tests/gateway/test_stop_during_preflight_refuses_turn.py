"""A /stop that lands during turn PRE-FLIGHT must stop the turn from running.

Incident 2026-09-24 12:37-13:09 PT (Discord #claude-bridge, Apollo gateway
pid 45339): Ace sent ``continue`` at 12:37:49; the turn claimed its slot with
the PENDING sentinel and spent ~49 s in pre-flight (transcript load, model
rehydrate, agent construction). ``/stop`` arrived at 12:38:07 —
``_interrupt_and_clear_session`` found only the sentinel, so there was no
agent to interrupt; it bumped the run generation (1 → 2) and released the
slot. At 12:38:38 pre-flight finished and the turn entered
``run_conversation`` anyway: it acquired the durable session turn lease and
ran **123 API calls over ~68 minutes**, every result later discarded as
stale. Ace's replacement turn (generation 2) queued behind that lease for
the full 1800 s budget and died with "Another Hermes process kept this
session busy too long" — the "other process" was the same pid.

The only generation check on that path was ``track_agent``'s "Skipping
stale agent promotion", which merely keeps the stale agent out of the slot.
The fix adds a gate right before ``run_conversation``: a stale generation
refuses to start (no lease, no API calls). These tests drive the REAL
``GatewayRunner._run_agent`` boundary with a fake ``AIAgent`` whose
constructor is the point at which ``/stop`` lands.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource

SESSION_KEY = "agent:main:telegram:group:-2001"


class _NullAdapter(BasePlatformAdapter):
    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id="m-1")

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {}


class _RunLedger:
    """Shared record of what the fake agent did."""

    def __init__(self):
        self.run_conversation_calls = 0
        self.constructed = 0


def _make_agent_cls(ledger: _RunLedger, on_construct=None):
    class FakeAgent:
        def __init__(self, **kwargs):
            ledger.constructed += 1
            self.tools = []
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            if on_construct is not None:
                on_construct()

        def run_conversation(self, message, conversation_history=None, task_id=None, **kw):
            ledger.run_conversation_calls += 1
            return {
                "final_response": "zombie output",
                "messages": [],
                "api_calls": 1,
            }

    return FakeAgent


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
    runner.session_store = SimpleNamespace(_entries={}, _save=lambda: None)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
        streaming=StreamingConfig.from_dict({"enabled": False}),
    )
    return runner


async def _drive_turn(monkeypatch, tmp_path, *, stop_during_preflight: bool):
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.dump({"display": {"tool_progress": "off"}, "streaming": {"enabled": False}}),
        encoding="utf-8",
    )
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    adapter = _NullAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    # The turn claims its generation exactly as _handle_message does.
    generation = runner._begin_session_run_generation(SESSION_KEY)
    ledger = _RunLedger()

    def _stop_lands_now():
        # /stop during pre-flight: the slot holds the PENDING sentinel so
        # there is no agent to interrupt — the only effect is the bump.
        runner._invalidate_session_run_generation(SESSION_KEY, reason="stop_command")

    agent_cls = _make_agent_cls(
        ledger, on_construct=_stop_lands_now if stop_during_preflight else None
    )
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-2001", chat_type="group")
    result = await runner._run_agent(
        message="continue",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-stop-preflight",
        session_key=SESSION_KEY,
        run_generation=generation,
    )
    return ledger, result, runner


@pytest.mark.asyncio
async def test_stop_during_preflight_never_enters_run_conversation(monkeypatch, tmp_path):
    ledger, result, _ = await _drive_turn(monkeypatch, tmp_path, stop_during_preflight=True)

    assert ledger.constructed == 1, "pre-flight still builds the agent"
    assert ledger.run_conversation_calls == 0, (
        "a turn whose generation was invalidated during pre-flight ran anyway — "
        "this is the 2026-09-24 zombie turn (123 API calls, lease held ~68 min)"
    )
    assert isinstance(result, dict)
    assert result.get("api_calls", 0) == 0
    assert result.get("interrupted") is True
    # The gateway may substitute its canned "interrupted before processing
    # started" notice for an empty interrupted result (and the stale-result
    # check in _handle_message_with_agent discards it anyway). What must
    # never appear is model output from the refused turn.
    assert "zombie output" not in (result.get("final_response") or "")


@pytest.mark.asyncio
async def test_current_generation_still_runs(monkeypatch, tmp_path):
    """Control: no /stop → the turn runs exactly once."""
    ledger, result, _ = await _drive_turn(monkeypatch, tmp_path, stop_during_preflight=False)

    assert ledger.run_conversation_calls == 1
    assert result["final_response"] == "zombie output"
