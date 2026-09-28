"""Background admission lane contract for bridge providers (opt-in)."""
from types import SimpleNamespace

import pytest

from agent.fork_ext import relay_headers as rh
from agent import chat_completion_helpers as cch


def agent(provider, platform, depth=0):
    return SimpleNamespace(provider=provider, platform=platform, _delegate_depth=depth)


@pytest.mark.parametrize("provider", ["claude-bpr", "claude-bpx-21"])
@pytest.mark.parametrize("platform,depth", [("kanban", 0), ("cron", 0), ("subagent", 1)])
def test_worker_launchers_stamp_background(provider, platform, depth, monkeypatch):
    monkeypatch.setattr(rh, "_bridge_lane_enabled", lambda: True)
    kwargs = {}
    rh.stamp_bridge_lane(agent(provider, platform, depth), kwargs)
    assert kwargs["extra_headers"]["x-hermes-lane"] == "background"


@pytest.mark.parametrize("provider", ["claude-bpr", "claude-bpx-21"])
@pytest.mark.parametrize("platform", ["discord", "telegram"])
def test_interactive_gateway_never_stamps(provider, platform, monkeypatch):
    monkeypatch.setattr(rh, "_bridge_lane_enabled", lambda: True)
    kwargs = {"extra_headers": {"x-hermes-lane": "background"}}
    rh.stamp_bridge_lane(agent(provider, platform), kwargs)
    assert "x-hermes-lane" not in kwargs["extra_headers"]


def test_off_and_other_providers_are_unchanged(monkeypatch):
    monkeypatch.setattr(rh, "_bridge_lane_enabled", lambda: False)
    kwargs = {}
    rh.stamp_bridge_lane(agent("claude-bpr", "cron"), kwargs)
    assert kwargs == {}
    monkeypatch.setattr(rh, "_bridge_lane_enabled", lambda: True)
    for provider in ("anthropic", "claude-apr", "openrouter"):
        kwargs = {}
        rh.stamp_bridge_lane(agent(provider, "cron"), kwargs)
        assert kwargs == {}


def test_config_is_off_by_default_and_true_only_when_enabled(monkeypatch):
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"agent": {}})
    assert rh._bridge_lane_enabled() is False
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"agent": {"bridge_background_lane": "true"}})
    assert rh._bridge_lane_enabled() is False
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"agent": {"bridge_background_lane": True}})
    assert rh._bridge_lane_enabled() is True


def test_real_call_path_stamps_at_each_attempt(monkeypatch):
    monkeypatch.setattr(rh, "_bridge_lane_enabled", lambda: True)
    seen = []
    monkeypatch.setattr(cch, "should_use_direct_api_call", lambda _agent: True)
    monkeypatch.setattr(cch, "direct_api_call", lambda _agent, kwargs: (
        seen.append(dict(kwargs.get("extra_headers") or {})) or SimpleNamespace(usage=None)))
    a = agent("claude-bpr", "cron")
    a._current_turn_id = "turn-test"
    cch.interruptible_api_call(a, {"model": "m"})
    assert seen[0]["x-hermes-lane"] == "background"
