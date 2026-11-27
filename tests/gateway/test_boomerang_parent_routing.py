"""Cold boomerang construction must use the foreground session route resolver."""
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as wake
import run_agent  # Load bootstrap before the per-test home-I/O guard.


def test_cold_parent_uses_session_identity_and_effective_policy(monkeypatch):
    seen = {}
    calls = []
    home = SimpleNamespace(session_id="pinned-parent", session_key="agent:main:discord:group:7", origin=object())
    chain = [{"provider": "synthetic-fallback", "model": "fallback-model"}]

    def resolve(**kw):
        calls.append(kw)
        return "pinned-model", {"provider": "synthetic-parent", "api_key": "synthetic-key", "base_url": "http://localhost:1"}

    runner = SimpleNamespace(
        session_store=SimpleNamespace(load_transcript=lambda sid: []),
        _resolve_session_agent_runtime=resolve, _refresh_fallback_model=lambda: chain,
        _pre_agent_fallback_notice="foreground-notice",
    )
    monkeypatch.setattr("run_agent.AIAgent", lambda **kw: (seen.update(kw) or SimpleNamespace()))
    parent = wake._db_parent(runner, home)
    assert parent._boomerang_route_source == "session-rehydrated"
    assert seen["model"] == "pinned-model"
    assert seen["fallback_model"] == chain
    assert calls[0]["session_key"] == home.session_key
    assert calls[0]["source"] is home.origin
    assert runner._pre_agent_fallback_notice == "foreground-notice"


@pytest.mark.parametrize("broken", [False, True])
def test_cold_session_pin_channel_precedence_and_fail_closed(monkeypatch, tmp_path, broken):
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore, SessionSource
    from gateway.config import GatewayConfig, Platform, PlatformConfig, ChannelOverride

    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(
        enabled=True, channel_overrides={"7": ChannelOverride(model="channel-model", provider="channel-provider")})})
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = SessionSource(platform=Platform.DISCORD, chat_id="7", chat_type="group")
    entry = store.get_or_create_session(source)
    store.set_model_override(entry.session_key, {"provider": "pinned-provider", "model": "pinned-model"})
    store2 = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config, runner.session_store = config, store2
    runner._session_model_overrides = {}
    runner._pre_agent_fallback_notice = "foreground"
    runner._refresh_fallback_model = lambda: [{"provider": "backup", "model": "backup-model"}]
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *a, **kw: "global-model")
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {"provider": "global-provider", "api_key": "global-key"})
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda provider, **kw: {"provider": provider, "api_key": "channel-key"})
    monkeypatch.setattr(runner, "_reresolve_model_override_credentials", lambda value: None if broken else {
        "provider": "pinned-provider", "model": "pinned-model", "api_key": "pin-key", "base_url": "http://pin", "api_mode": "chat_completions"})
    captured = []
    monkeypatch.setattr("run_agent.AIAgent", lambda **kw: (captured.append(kw) or SimpleNamespace()))
    if broken:
        with pytest.raises(Exception, match="credentials could not be resolved"):
            wake._db_parent(runner, entry)
        assert not captured
    else:
        wake._db_parent(runner, entry)
        assert (captured[-1]["model"], captured[-1]["provider"]) == ("pinned-model", "pinned-provider")
        store2.set_model_override(entry.session_key, None)
        runner._session_model_overrides.clear()
        wake._db_parent(runner, entry)
        assert (captured[-1]["model"], captured[-1]["provider"]) == ("channel-model", "channel-provider")
    assert runner._pre_agent_fallback_notice == "foreground"


@pytest.mark.parametrize("failure", ["owner", "scope"])
def test_owner_or_scope_failure_preserves_pending_wake(monkeypatch, tmp_path, failure):
    import asyncio
    from contextlib import asynccontextmanager
    from gateway import kanban_owner_wake as owner

    entry = {"key": "claim", "delegation_id": "failed-routing", "task_id": "fixture-task",
             "home_sid": "fixture-home", "wake_text": "original obligation"}
    state = wake.BoomerangWakeState(tmp_path / "claims.json")
    state.claim(entry["key"], entry)
    def resolve(*a):
        if failure == "owner":
            raise RuntimeError("owner lookup failed")
        return SimpleNamespace(origin=object())
    @asynccontextmanager
    async def scope(*a):
        raise RuntimeError("scope entry failed")
        yield
    runner = SimpleNamespace(session_store=None, _async_profile_scope_for_source=scope)
    monkeypatch.setattr(owner, "resolve_owner", resolve)
    async def refuse(*a, **kw):
        return False
    monkeypatch.setattr(wake, "post_line", refuse)
    monkeypatch.setattr(wake, "resurface", refuse)
    monkeypatch.setattr(wake, "_board_delivered", lambda *a: set())
    asyncio.run(wake.run_entry(runner, entry, state))
    claim = state.get_claim(entry["key"])
    assert claim["outcome"] == "failed"
    assert failure in claim["reason"]
    assert not claim["resurface_delivered"]
    row = state.handbacks()[entry["delegation_id"]]
    assert row["turn"] == "original obligation" and wake.PART_TURN in row["parts"]
