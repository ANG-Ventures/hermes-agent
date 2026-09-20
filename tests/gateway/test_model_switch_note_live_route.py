"""The /model switch note must name the route the session was ACTUALLY running.

Measured 2026-09-19 on the fleet: a Telegram session whose cached agent was live
on ``claude-opus-5`` via ``claude-apx-1`` (every API-call log line said so) got
the note ``[Note: model was just switched from claude-fable-5-1 to
claude-fable-5-1 via Claude BPX-8]`` — because the handler read ``current_model``
from config.yaml + the session override (a stale earlier pin), never from the
live agent. The in-place switch log, which reads the agent, correctly said
``claude-opus-5 (claude-apx-1) -> claude-fable-5-1 (claude-bpx-8)``.

Two consequences, both pinned here:
  * the FROM side must come from the live agent when one is cached;
  * the provider route must be spelled out, because a sub-to-sub move
    (bpx-8 -> bpx-11) keeps the model slug and otherwise reads as a no-op.

The note's PREFIX ``[Note: model was just switched from X to Y `` is a lockstep
anchor for the claude-bpx scaffold detector (its fixture pins the f-string
source), so the fix corrects the values, not the literal. Asserted too.
"""

import threading
import types

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._agent_cache = {}
    # A REAL lock: the handler skips the cached-agent path when the lock is falsy.
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = None
    runner._evict_cached_agent = lambda _session_key: None
    runner.session_store = None
    return runner


def _make_event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="571820863",
            chat_type="dm",
            user_id="user-1",
        ),
    )


def _live_agent(model, provider):
    """A cached agent that is genuinely running `model` on `provider`."""
    calls = []

    def switch_model(**kw):
        calls.append(kw)

    return types.SimpleNamespace(
        model=model,
        provider=provider,
        reasoning_config=None,
        context_compressor=None,
        switch_model=switch_model,
        _switch_calls=calls,
    )


@pytest.fixture
def switched_runner(tmp_path, monkeypatch):
    """Config + stale override both say fable-5-1/bpx-8; the LIVE agent is on
    opus-5/apx-1 (what a failover leaves behind). /model moves it to bpx-11."""
    import gateway.run as gateway_run
    from hermes_cli.model_switch import ModelSwitchResult

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: claude-fable-5-1\n  provider: claude-bpx-8\nproviders: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kw: ModelSwitchResult(
            success=True,
            new_model="claude-fable-5-1",
            target_provider="claude-bpx-11",
            provider_changed=True,
            api_key="k",
            base_url="http://100.105.31.87:3556/v1",
            api_mode="openai_chat",
            request_overrides={},
            provider_label="Claude BPX-11",
            is_global=False,
        ),
    )

    runner = _make_runner()
    event = _make_event("/model claude-bpx-11/claude-fable-5-1")
    session_key = runner._session_key_for_source(event.source)
    # Stale pin from an earlier /model — the value the old code reported as "from".
    runner._session_model_overrides[session_key] = {
        "model": "claude-fable-5-1", "provider": "claude-bpx-8",
        "api_key": "k", "base_url": "http://100.105.240.51:3556/v1", "api_mode": "openai_chat",
    }
    agent = _live_agent("claude-opus-5", "claude-apx-1")
    runner._agent_cache[session_key] = (agent, 0.0)

    async def _noop_announce(*_a, **_k):
        return None

    runner._announce_model_switch = _noop_announce
    runner._announce_switch = _noop_announce
    runner._switch_reasoning_kwargs = lambda **_k: {}
    return runner, event, session_key, agent


@pytest.mark.asyncio
async def test_switch_note_names_the_live_route_not_the_stale_override(switched_runner):
    runner, event, session_key, agent = switched_runner

    result = await runner._handle_model_command(event)

    assert result is not None
    assert agent._switch_calls and agent._switch_calls[0]["new_provider"] == "claude-bpx-11"
    note = runner._pending_model_notes[session_key]
    # FROM = what the session was actually running, not config/override.
    assert "switched from claude-opus-5 to claude-fable-5-1" in note, note
    assert "from claude-fable-5-1 to claude-fable-5-1" not in note, note
    # The provider route is explicit, so a same-slug sub move is legible.
    assert "(route claude-apx-1 -> claude-bpx-11)" in note, note


@pytest.mark.asyncio
async def test_switch_note_keeps_the_bridge_lockstep_prefix(switched_runner):
    runner, event, session_key, _agent = switched_runner
    await runner._handle_model_command(event)
    note = runner._pending_model_notes[session_key]
    # claude-bpx grammar #10 anchor + LOCK-1 fixture: prefix shape is load-bearing.
    assert note.startswith("[Note: model was just switched from "), note
    assert " via Claude BPX-11 " in note, note
    assert note.endswith("Adjust your self-identification accordingly.]"), note


@pytest.mark.asyncio
async def test_switch_note_falls_back_to_override_when_no_agent_is_cached(switched_runner):
    runner, event, session_key, _agent = switched_runner
    runner._agent_cache.clear()  # nothing live yet (first message after restart)

    await runner._handle_model_command(event)

    note = runner._pending_model_notes[session_key]
    # With no live agent the override IS the best available truth.
    assert "switched from claude-fable-5-1 to claude-fable-5-1" in note, note
    assert "(route claude-bpx-8 -> claude-bpx-11)" in note, note
