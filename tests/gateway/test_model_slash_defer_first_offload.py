"""Discord ``/model <name>`` must ack first and never block the loop on the provider probe.

Card t_515b7fce (2026-09-24): the typed-switch commit path called the persistence door
``_set_session_model_override`` inline; that door re-resolves credentials via
``model_switch.switch_model -> validate_requested_model -> fetch_api_models``, a synchronous
``urllib`` GET of ``<bridge base_url>/v1/models`` (timeout 5 s). It ran on the asyncio loop,
so the watchdog logged PHASE=event_loop_blocked seconds=10 and Discord logged
"interaction expired before defer" 33 times.

Two contracts are pinned here:

1. Ordering — the Discord slash handler acks (``interaction.response.defer``) BEFORE the
   command is dispatched, i.e. before any validation / network I/O can run.
2. Liveness — while the (mocked) blocking probe runs, the event loop keeps servicing other
   coroutines, and the probe runs on a worker thread, not the loop thread.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


# --------------------------------------------------------------------------- #
# 1. Discord adapter: defer before dispatch
# --------------------------------------------------------------------------- #
def _discord_adapter():
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    return adapter


def _interaction(order):
    async def _defer(**kwargs):
        order.append(("defer", kwargs))

    return SimpleNamespace(
        user=SimpleNamespace(id=1, name="ace"),
        channel=SimpleNamespace(id=2),
        channel_id=2,
        guild_id=None,
        response=SimpleNamespace(defer=AsyncMock(side_effect=_defer), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_discord_model_slash_acks_before_dispatch(monkeypatch):
    order: list = []
    adapter = _discord_adapter()
    monkeypatch.setattr(adapter, "_check_slash_authorization", AsyncMock(return_value=True), raising=False)

    def _build(interaction, text):
        order.append(("build", text))
        return SimpleNamespace(source=SimpleNamespace(chat_id="2"), deferred_reply_text="ok")

    async def _handle(event):
        # Stands in for the gateway's /model handler: the provider probe would run here.
        order.append(("dispatch", None))

    monkeypatch.setattr(adapter, "_build_slash_event", _build, raising=False)
    monkeypatch.setattr(adapter, "handle_message", _handle, raising=False)

    await adapter._run_simple_slash(_interaction(order), "/model fable-5-1")

    kinds = [k for k, _ in order]
    assert kinds.index("defer") < kinds.index("dispatch"), order
    assert kinds[0] == "defer", "nothing may run between the auth gate and the ack"
    assert order[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
async def test_discord_slash_auth_gate_does_not_await_io_before_ack(monkeypatch):
    """The only await before defer is the auth gate, and on the allow path it is pure
    in-memory evaluation — prove it resolves without yielding to a slow await."""
    adapter = _discord_adapter()
    monkeypatch.setattr(adapter, "_evaluate_slash_authorization", lambda i: (True, None), raising=False)
    t0 = time.monotonic()
    assert await adapter._check_slash_authorization(_interaction([]), "/model x") is True
    assert time.monotonic() - t0 < 0.1


# --------------------------------------------------------------------------- #
# 2. Gateway /model handler: blocking probe runs off the loop
# --------------------------------------------------------------------------- #
PROBE_SECONDS = 0.6


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._evict_cached_agent = lambda _k: None
    runner.session_store = None
    return runner


def _event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="c1", chat_type="dm", user_id="u1"),
    )


@pytest.fixture
def _home(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: gpt-x\n  provider: openrouter\nproviders: {}\n", encoding="utf-8"
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    return home


@pytest.mark.asyncio
async def test_typed_model_switch_probe_runs_off_the_event_loop(_home, monkeypatch):
    from hermes_cli.model_switch import ModelSwitchResult

    loop_thread = threading.get_ident()
    probe_threads: list[int] = []

    def _switch(**kw):
        return ModelSwitchResult(
            success=True, new_model="fable-5-1", target_provider="claude-bpx-9",
            provider_changed=True, api_key="k", base_url="http://100.69.143.118:3556",
            api_mode="chat_completions", provider_label="bpx-9", is_global=False,
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _switch)

    runner = _make_runner()

    # Mock the persistence-time credential re-resolve (the real one GETs /v1/models).
    def _blocking_reresolve(identity):
        probe_threads.append(threading.get_ident())
        time.sleep(PROBE_SECONDS)  # stands in for socket.connect to a slow/dead bridge
        return None

    monkeypatch.setattr(GatewayRunner, "_reresolve_model_override_credentials",
                        staticmethod(_blocking_reresolve))

    class _Store:
        def __init__(self):
            self.entry = SimpleNamespace(model_override_identity=None)

        def entry_for(self, key):
            return self.entry

        def persist(self):
            pass

    runner.session_store = _Store()

    ticks = 0
    stop = asyncio.Event()

    async def _heartbeat():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    hb = asyncio.create_task(_heartbeat())
    try:
        result = await runner._handle_model_command(_event("/model fable-5-1 --provider claude-bpx-9"))
    finally:
        stop.set()
        await hb

    assert result is not None
    assert probe_threads, "the persistence probe was never exercised"
    assert all(t != loop_thread for t in probe_threads), (
        "credential re-resolve (-> validate_requested_model -> fetch_api_models) ran ON the loop"
    )
    # 0.6 s probe at a 20 ms heartbeat: a blocked loop yields ~1 tick, a live one ~30.
    assert ticks >= 10, f"event loop stalled during the probe (heartbeat ticks={ticks})"
    key = runner._session_key_for_source(_event("x").source)
    assert runner._session_model_overrides[key]["model"] == "fable-5-1"
