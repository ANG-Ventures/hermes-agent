"""Config and failure semantics of command-scoped model announcements.

Both real command paths are exercised in test_route_change_turn_contract.py.
"""

import asyncio
import types


def _mixin_instance():
    """Bare object carrying just the mixin's _announce_model_switch (bound)."""
    from gateway.slash_commands import GatewaySlashCommandsMixin

    obj = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    obj._sent = []

    async def send(chat_id, text, metadata=None):
        obj._sent.append(text)

    obj._adapter_for_source = lambda source: types.SimpleNamespace(send=send)
    obj._thread_metadata_for_source = lambda *args: None
    return obj


def _capture_agent():
    a = types.SimpleNamespace()
    a._announced = []
    a._emit_status = lambda m: a._announced.append(m)
    a._last_switch_announced = None
    return a


def test_announce_helper_emits_when_gate_default_on(monkeypatch):
    import gateway.run as run

    # No announce_switch key -> default ON.
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"model": {}}, raising=True)
    obj = _mixin_instance()
    agent = _capture_agent()
    asyncio.run(obj._announce_model_switch(
        agent,
        source=types.SimpleNamespace(chat_id="c1"),
        old_model="claude-opus-4-8", new_model="gpt-5.5",
        old_provider="claude-app", new_provider="openai-codex",
    ))
    msgs = [m for m in obj._sent if m.startswith("🔀")]
    assert len(msgs) == 1, agent._announced
    assert agent._announced == []
    assert "claude-app/claude-opus-4-8" in msgs[0]
    assert "openai-codex/gpt-5.5" in msgs[0]


def test_announce_helper_silent_when_gate_off(monkeypatch):
    import gateway.run as run

    monkeypatch.setattr(
        run, "_load_gateway_config",
        lambda: {"model": {"announce_switch": False}}, raising=True,
    )
    obj = _mixin_instance()
    agent = _capture_agent()
    asyncio.run(obj._announce_model_switch(
        agent,
        source=types.SimpleNamespace(chat_id="c1"),
        old_model="a", new_model="b", old_provider="p1", new_provider="p2",
    ))
    assert obj._sent == [], f"gate off must be silent, got {obj._sent!r}"


def test_announce_helper_never_raises_on_bad_agent(monkeypatch):
    import gateway.run as run

    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"model": {}}, raising=True)
    obj = _mixin_instance()
    # A failed direct delivery must not propagate (the switch stands).
    bad = types.SimpleNamespace()
    bad._last_switch_announced = None

    async def _boom(*args, **kwargs):
        raise RuntimeError("emit broke")

    obj._adapter_for_source = lambda source: types.SimpleNamespace(send=_boom)
    asyncio.run(obj._announce_model_switch(
        bad, old_model="a", new_model="b", old_provider="p", new_provider="q",
        source=types.SimpleNamespace(chat_id="c1"),
    ))  # must not raise
