"""Exercise real agent retries, route mutation, and gateway delivery together."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import openai
import pytest

from agent.auxiliary_client import clear_runtime_main
from agent.error_classifier import FailoverReason
from gateway.config import Platform
from gateway.run import GatewayRunner, TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def isolate_auxiliary_route():
    clear_runtime_main()
    try:
        yield
    finally:
        clear_runtime_main()


class RecordingAdapter:
    def __init__(self):
        self.messages = []

    async def send(self, chat_id, content, metadata=None):
        self.messages.append((chat_id, content, metadata))
        return SimpleNamespace(success=True, message_id=str(len(self.messages)))

    async def send_model_picker(self, *, on_model_selected, **kwargs):
        self.picker_callback = on_model_selected
        return SimpleNamespace(success=True)

    async def send_or_update_status(self, *args, **kwargs):
        raise AssertionError(
            "Route changes must not use an overwriteable status bubble"
        )


def make_agent(monkeypatch):
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *a, **kw: {})
    monkeypatch.setattr("run_agent.OpenAI", MagicMock())
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config",
        lambda: {
            "model": {"announce_route_change": True, "announce_recovery": True},
        },
    )
    monkeypatch.setattr(
        "agent.conversation_loop.jittered_backoff", lambda *a, **kw: 0.0
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", lambda *a, **kw: 200000
    )
    monkeypatch.setattr(
        "agent.context_compressor.get_model_context_length", lambda *a, **kw: 200000
    )
    monkeypatch.setattr(
        "hermes_cli.model_normalize.normalize_model_for_provider",
        lambda model, provider: model,
    )

    def resolve(provider, model, **kwargs):
        return SimpleNamespace(
            api_key="test-key",
            base_url="https://fallback.example/v1",
            _custom_headers=None,
            default_headers=None,
        ), model

    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", resolve)
    agent = AIAgent(
        api_key="test-key",
        base_url="https://primary.example/v1",
        provider="openrouter",
        model="primary/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        fallback_model=[
            {"provider": "openrouter", "model": "fallback/one"},
            {"provider": "openrouter", "model": "fallback/two"},
        ],
    )
    agent._api_max_retries = 3
    for name in ("_persist_session", "_save_trajectory", "_cleanup_task_resources"):
        monkeypatch.setattr(agent, name, lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_try_recover_primary_transport", lambda *a, **kw: False)
    return agent


def response():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Recovered", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )


async def bind_delivery(agent, adapter):
    source = SimpleNamespace(platform=Platform.DISCORD, chat_id="test-chat")
    ctx = TurnContext(
        source=source,
        _run_still_current=lambda: True,
        _loop_for_step=asyncio.get_running_loop(),
        _current_status_adapter=lambda: adapter,
        _status_chat_id=source.chat_id,
        _status_thread_metadata={"thread_id": "test-thread"},
    )
    runner = TurnRunner(SimpleNamespace(), ctx)
    agent.status_callback = runner._status_callback_sync


@pytest.mark.parametrize("reason", list(FailoverReason))
def test_every_cause_announces_route_change_in_same_turn(monkeypatch, reason):
    """Mutation: remove _emit_fallback_announce in try_activate_fallback -> RED."""
    agent = make_agent(monkeypatch)
    adapter = RecordingAdapter()

    async def scenario():
        await bind_delivery(agent, adapter)
        agent._current_turn_id = "test-turn"
        assert await asyncio.to_thread(agent._try_activate_fallback, reason=reason)
        # Drain all sends scheduled before activation returned, on this same loop.
        await asyncio.sleep(0)
        assert len(adapter.messages) == 1
        assert agent._last_fallback_event["turn_id"] == "test-turn"

    asyncio.run(scenario())
    chat, text, metadata = adapter.messages[0]
    assert chat == "test-chat" and metadata == {"thread_id": "test-thread"}
    assert "openrouter/primary/model" in text and "openrouter/fallback/one" in text
    assert "Model fallback" in text


@pytest.mark.parametrize("announce_recovery", [True, False])
def test_pool_exhaustion_then_429_delivers_each_hop_before_final(
    monkeypatch, announce_recovery, capsys
):
    agent = make_agent(monkeypatch)
    adapter = RecordingAdapter()
    attempts = []
    request = httpx.Request("POST", "https://primary.example/v1/chat/completions")
    errors = [
        openai.InternalServerError(
            "no eligible sub",
            response=httpx.Response(503, request=request),
            body={"error": "no eligible sub"},
        )
        for _ in range(3)
    ]
    errors.append(
        openai.RateLimitError(
            "rate limited",
            response=httpx.Response(429, request=request),
            body={"error": "rate limited"},
        )
    )

    def call(*args, **kwargs):
        attempts.append(agent.model)
        if errors:
            raise errors.pop(0)
        return response()

    monkeypatch.setattr(agent, "_interruptible_api_call", call)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", call)

    async def scenario():
        await bind_delivery(agent, adapter)
        result = await asyncio.to_thread(agent.run_conversation, "hello")
        await asyncio.sleep(0)
        assert result["final_response"] == "Recovered"
        assert result["completed"] is True
        assert len(adapter.messages) == 2
        assert all(chat == "test-chat" for chat, _, _ in adapter.messages)
        assert "sub pool capped" in adapter.messages[0][1]
        assert "rate limit" in adapter.messages[1][1]
        # Actual restore method, not a direct call to the formatter.
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {
                "model": {"announce_recovery": announce_recovery},
            },
        )
        agent._rate_limited_until = 0
        await asyncio.to_thread(agent._restore_primary_runtime)
        await asyncio.sleep(0)
        assert len(adapter.messages) == 2 + int(announce_recovery)
        if announce_recovery:
            assert "Model recovery" in adapter.messages[-1][1]

    asyncio.run(scenario())
    assert attempts == ["primary/model"] * 3 + ["fallback/one", "fallback/two"]
    console = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("🔄 Model")
    ]
    assert console == [text for _, text, _ in adapter.messages]


def test_effort_only_fallback_and_restore_are_each_delivered(monkeypatch, capsys):
    """Mutation: drop effort from the fallback identity tuples -> silent/RED."""
    agent = make_agent(monkeypatch)
    agent.reasoning_config = {"effort": "high"}
    agent._primary_runtime["reasoning_config"] = dict(agent.reasoning_config)
    agent._fallback_chain = [
        {
            "provider": agent.provider,
            "model": agent.model,
            "base_url": "https://fallback.example/v1",
            "reasoning_effort": "medium",
        }
    ]
    adapter = RecordingAdapter()

    async def scenario():
        await bind_delivery(agent, adapter)
        assert await asyncio.to_thread(
            agent._try_activate_fallback, reason=FailoverReason.overloaded
        )
        await asyncio.sleep(0)
        assert agent.reasoning_config["effort"] == "medium"
        assert len(adapter.messages) == 1
        assert "(high)" in adapter.messages[0][1]
        assert "(medium)" in adapter.messages[0][1]
        assert await asyncio.to_thread(agent._restore_primary_runtime)
        await asyncio.sleep(0)
        assert agent.reasoning_config["effort"] == "high"
        assert len(adapter.messages) == 2
        assert "Model recovery" in adapter.messages[-1][1]
        assert "(medium)" in adapter.messages[-1][1]
        assert "(high)" in adapter.messages[-1][1]

    asyncio.run(scenario())
    console = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("🔄 Model")
    ]
    assert console == [text for _, text, _ in adapter.messages]


@pytest.mark.parametrize(
    "prior",
    [
        {"provider": "other", "model": "old-model", "effort": "high"},
        {"provider": "openrouter", "model": "primary/model", "effort": "high"},
    ],
)
@pytest.mark.parametrize("cached", [False, True])
def test_reinit_route_or_effort_change_is_delivered_by_real_turn_runner(
    monkeypatch, prior, cached
):
    """Removing the pre-run re-init announcement call must turn this RED."""
    agent = make_agent(monkeypatch)
    agent.reasoning_config = {"effort": "high"}
    monkeypatch.setattr(agent, "_interruptible_api_call", lambda *a, **kw: response())
    monkeypatch.setattr(
        agent, "_interruptible_streaming_api_call", lambda *a, **kw: response()
    )
    adapter = RecordingAdapter()
    owner = make_turn_owner(agent, prior, cached=cached)

    async def scenario():
        await run_turn(owner, agent, adapter)
        assert len(adapter.messages) == 1
        text = adapter.messages[0][1]
        if not cached:
            assert f"{prior['provider']}/{prior['model']}" in text
        assert "openrouter/primary/model" in text
        assert "high" in text and "medium" in text

    asyncio.run(scenario())


def make_turn_owner(agent, prior, *, cached=True, effort="medium"):
    owner = MagicMock()
    owner.config = SimpleNamespace(streaming=None)
    owner._provider_routing = {}
    owner._agent_cache_lock = None
    owner._agent_cache = {}
    if cached:
        owner._agent_cache_lock = threading.RLock()
        owner._agent_cache["test-session-key"] = (agent, ("test-signature",))
    owner._session_db = None
    owner._prefill_messages = None
    owner._pending_model_notes = {}
    owner._pending_skills_reload_notes = {}
    owner._override_target_just_changed = {}
    owner.session_store._lock = None
    entry = SimpleNamespace(
        last_served_identity=prior, model_override_identity=None, resume_pending=False
    )
    owner.session_store._entries = {"test-session-key": entry}
    owner._get_system_prompt_for_channel.return_value = None
    owner._resolve_session_agent_runtime.return_value = (agent.model, {})
    owner._resolve_session_reasoning_config.return_value = {"effort": effort}
    owner._resolve_session_service_tier.return_value = None
    owner._resolve_turn_agent_config.return_value = {
        "model": agent.model,
        "runtime": {},
    }
    owner._agent_config_signature.return_value = ("test-signature",)
    owner._extract_cache_busting_config.return_value = {}
    owner._refresh_fallback_model.return_value = None
    owner._consume_pending_native_image_paths.return_value = []
    owner._consume_pending_turn_sidecar_notes.return_value = []
    owner._is_telegram_topic_lane.return_value = False
    owner._is_discord_auto_thread_lane.return_value = False
    owner._is_relay_discord_channel_lane.return_value = False
    owner._announce_reinit_recovery = GatewayRunner._announce_reinit_recovery.__get__(
        owner
    )
    owner._announce_and_persist_served_route = (
        GatewayRunner._announce_and_persist_served_route.__get__(owner)
    )
    owner._switch_announce_enabled = GatewayRunner._switch_announce_enabled
    return owner


async def run_turn(owner, agent, adapter, user_config=None):
    ctx = TurnContext(
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="test-chat", user_id="test-user"
        ),
        message="hello",
        history=[],
        session_id=agent.session_id,
        session_key="test-session-key",
        user_config=user_config or {},
        AIAgent=lambda **kw: agent,
        resolve_display_setting=lambda *a: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
        _current_status_adapter=lambda: adapter,
        _status_chat_id="test-chat",
        _loop_for_step=asyncio.get_running_loop(),
    )
    runner = TurnRunner(owner, ctx)
    ctx._status_callback_sync = runner._status_callback_sync
    result = await asyncio.to_thread(runner.run_sync)
    await asyncio.sleep(0)
    assert result["final_response"] == "Recovered"
    return result


def test_cross_session_pin_mismatch_announces_on_emitted_turn(monkeypatch, tmp_path):
    """Mutation: remove run_sync's chat-pin check -> no durable notice -> RED."""
    from gateway.chat_model_pins import ChatModelPins
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    agent = make_agent(monkeypatch)
    monkeypatch.setattr(agent, "_interruptible_api_call", lambda *a, **kw: response())
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", lambda *a, **kw: response())
    owner = make_turn_owner(agent, None)
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None
    owner.session_store = store
    ChatModelPins(tmp_path).set("main", "discord", "test-chat", {
        "model": "user-pinned/model", "provider": "openrouter",
    })
    adapter = RecordingAdapter()

    async def scenario():
        result = await run_turn(owner, agent, adapter)
        assert result["final_response"] == "Recovered"
        notices = [text for _, text, _ in adapter.messages if "this chat is pinned to" in text]
        assert len(notices) == 1
        assert notices[0].startswith("⚠ replying on openrouter/primary/model")
        assert "openrouter/user-pinned/model" in notices[0]

    asyncio.run(scenario())


def prepare_warm_fallback(monkeypatch, same_route):
    agent = make_agent(monkeypatch)
    agent.reasoning_config = {"effort": "medium"}
    agent._primary_runtime["reasoning_config"] = dict(agent.reasoning_config)
    agent._fallback_chain = [{
        "provider": agent.provider,
        "model": agent.model if same_route else "fallback/one",
        "base_url": "https://fallback.example/v1",
        "reasoning_effort": "high",
    }]
    monkeypatch.setattr(agent, "_interruptible_api_call", lambda *a, **kw: response())
    monkeypatch.setattr(
        agent, "_interruptible_streaming_api_call", lambda *a, **kw: response()
    )
    return agent


@pytest.mark.parametrize("same_route", [False, True])
@pytest.mark.parametrize("effort", ["medium", "low"])
@pytest.mark.parametrize("announce_switch", [False, True])
@pytest.mark.parametrize("announce_recovery", [False, True])
def test_warm_cache_recovery_preserves_from_effort_and_announces_once(
    monkeypatch, capsys, same_route, effort, announce_switch, announce_recovery
):
    """Mutation: unconditionally overwrite live effort in run_sync -> RED."""
    agent = prepare_warm_fallback(monkeypatch, same_route)
    adapter = RecordingAdapter()
    owner = make_turn_owner(agent, None, effort=effort)
    config = {"model": {
        "announce_switch": announce_switch, "announce_recovery": announce_recovery,
    }}
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: config)

    async def scenario():
        await bind_delivery(agent, adapter)
        assert await asyncio.to_thread(
            agent._try_activate_fallback, reason=FailoverReason.overloaded
        )
        await asyncio.sleep(0)
        assert len(adapter.messages) == 1
        fallback = adapter.messages[0][1].split(": ", 1)[1]
        assert fallback.endswith("(high)")
        capsys.readouterr()
        adapter.messages.clear()

        result = await run_turn(owner, agent, adapter, config)
        assert agent.model == "primary/model"
        assert result["reasoning_config"] == {"effort": effort}
        assert not agent._fallback_activated
        assert len(adapter.messages) == int(announce_recovery)
        if announce_recovery:
            chat, text, _ = adapter.messages[0]
            assert chat == "test-chat"
            assert "Model recovery (restore)" in text
            before, after = fallback.split(" → ")
            assert text.split(": ", 1)[1] == (
                f"{after} → {before.replace('(medium)', f'({effort})')}"
            )
        console = [line for line in capsys.readouterr().out.splitlines()
                   if line.startswith(("🔄 Model", "🔀 Model"))]
        assert console == [text for _, text, _ in adapter.messages]

    asyncio.run(scenario())


@pytest.mark.parametrize("same_route", [False, True])
@pytest.mark.parametrize("blocked_by", ["cooldown", "auto_recovery"])
def test_warm_cache_blocked_recovery_keeps_fallback_effort(
    monkeypatch, same_route, blocked_by
):
    agent = prepare_warm_fallback(monkeypatch, same_route)
    adapter = RecordingAdapter()
    owner = make_turn_owner(agent, None, effort="low")
    config = {"model": {"announce_switch": True, "announce_recovery": True}}
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: config)

    async def scenario():
        await bind_delivery(agent, adapter)
        assert await asyncio.to_thread(
            agent._try_activate_fallback, reason=FailoverReason.overloaded
        )
        await asyncio.sleep(0)
        old_model = agent.model
        adapter.messages.clear()
        if blocked_by == "cooldown":
            agent._rate_limited_until = time.monotonic() + 3600
        else:
            config["model"]["auto_recovery"] = False
        for _ in range(2):
            result = await run_turn(owner, agent, adapter, config)
            assert agent.model == old_model
            assert agent._fallback_activated
            assert result["reasoning_config"]["effort"] == "high"
            assert adapter.messages == []
        agent._rate_limited_until = 0
        config["model"]["auto_recovery"] = True
        result = await run_turn(owner, agent, adapter, config)
        assert result["reasoning_config"] == {"effort": "low"}
        assert len(adapter.messages) == 1
        assert "Model recovery (restore)" in adapter.messages[0][1]
        assert f"openrouter/{old_model} (high) → openrouter/primary/model (low)" in adapter.messages[0][1]

    asyncio.run(scenario())


@pytest.mark.parametrize("announce_switch", [False, True])
def test_cached_config_effort_change_survives_later_fallback(
    monkeypatch, announce_switch
):
    """Mutation: omit primary reasoning snapshot refresh in run_sync -> RED."""
    agent = prepare_warm_fallback(monkeypatch, same_route=False)
    adapter = RecordingAdapter()
    owner = make_turn_owner(agent, None, effort="low")
    config = {"model": {"announce_switch": announce_switch, "announce_recovery": False}}
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: config)

    async def scenario():
        result = await run_turn(owner, agent, adapter, config)
        assert result["reasoning_config"] == {"effort": "low"}
        assert len(adapter.messages) == int(announce_switch)
        if announce_switch:
            assert "Model switched" in adapter.messages[0][1]
            assert "effort medium→low" in adapter.messages[0][1]
        await run_turn(owner, agent, adapter, config)
        assert len(adapter.messages) == int(announce_switch)  # no-op is silent
        assert await asyncio.to_thread(
            agent._try_activate_fallback, reason=FailoverReason.overloaded
        )
        await asyncio.sleep(0)
        adapter.messages.clear()
        # Exercise core restoration too: gateway config refresh must update the
        # snapshot, not leave the next non-gateway restore pointing at old effort.
        assert await asyncio.to_thread(agent._restore_primary_runtime)
        await asyncio.sleep(0)
        assert agent.reasoning_config == {"effort": "low"}
        assert adapter.messages == []

    asyncio.run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("picker", [False, True])
async def test_manual_model_switch_delivers_once_without_old_turn_callback(
    tmp_path, monkeypatch, picker
):
    from tests.gateway.test_model_command_context_offload import (
        _event,
        _runner_with_store,
    )

    owner = _runner_with_store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *a, **kw: 200000,
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_picker_providers",
        lambda **kw: [
            {"slug": "openrouter", "name": "OpenRouter", "models": ["gpt-5.5"]},
        ],
    )
    adapter = RecordingAdapter()
    event = _event("/model" if picker else "/model gpt-5.5")
    owner.adapters = {event.source.platform: adapter}
    key = owner._session_key_for_source(event.source)
    prior_turn = []
    agent = SimpleNamespace(
        model="fallback/model",
        provider="fallback-provider",
        reasoning_config={"effort": "medium"},
        context_compressor=SimpleNamespace(context_length=200000),
        _emit_status=prior_turn.append,
    )

    def switch(**kwargs):
        agent.model = kwargs["new_model"]
        agent.provider = kwargs["new_provider"]
        agent.reasoning_config = {"effort": "high"}

    agent.switch_model = switch
    owner._agent_cache = {key: (agent, ("signature",))}
    owner._agent_cache_lock = threading.RLock()
    state_at_send = []
    original_send = adapter.send

    async def send_after_commit(*args, **kwargs):
        override = owner._session_model_overrides.get(key) or {}
        state_at_send.append(override.get("model"))
        return await original_send(*args, **kwargs)

    adapter.send = send_after_commit
    result = await owner._handle_model_command(event)
    if picker:
        assert result is None
        result = await adapter.picker_callback("c1", "gpt-5.5", "openrouter")
    assert result is not None
    assert prior_turn == []
    assert state_at_send == ["gpt-5.5"]
    assert len(adapter.messages) == 1
    assert "fallback-provider/fallback/model" in adapter.messages[0][1]
    assert "openrouter/gpt-5.5" in adapter.messages[0][1]
    assert "medium" in adapter.messages[0][1] and "high" in adapter.messages[0][1]
