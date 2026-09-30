"""service_tier request overrides: priority vs ultrafast, per route, per surface.

Covers the capability matrix (openai-api / openai-codex / proxy x tier), every
turn-route attach site (CLI, messaging gateway, ``hermes serve``), a real
outbound-request capture on the ``codex_responses`` path, and served-tier
honesty (the tier the response reports vs the one requested).
"""

from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------

CAPABILITY_MATRIX = [
    # (model, provider, api_mode, tier, expected overrides)
    ("gpt-5.5", "openai-api", "codex_responses", "priority", {"service_tier": "priority"}),
    ("gpt-5.5", "openai-codex", "codex_responses", "priority", {"service_tier": "fast"}),
    ("gpt-6-astra", "openai-api", "codex_responses", "ultrafast", {"service_tier": "ultrafast"}),
    ("openai/gpt-6-astra", "openai", "codex_responses", "ultrafast", {"service_tier": "ultrafast"}),
    ("gpt-6-astra", "openai-codex", "codex_responses", "ultrafast", {"service_tier": "ultrafast"}),
    ("gpt-6-astra-900k", "openai-codex", "codex_responses", "ultrafast", {"service_tier": "ultrafast"}),
    # Ultrafast is Responses-only.
    ("gpt-6-astra", "openai-api", "chat_completions", "ultrafast", {}),
    # Undocumented model: no tier at all, never a silent swap to priority.
    ("gpt-5.5", "openai-api", "codex_responses", "ultrafast", {}),
    ("gpt-5.5", "openai-codex", "codex_responses", "ultrafast", {}),
    # Proxies / aggregators fail closed for both tiers.
    ("gpt-6-astra", "openrouter", "chat_completions", "ultrafast", {}),
    ("gpt-6-astra", "custom", "codex_responses", "ultrafast", {}),
    ("gpt-5.5", "openrouter", "chat_completions", "priority", {}),
    ("gpt-5.5", "custom", "codex_responses", "priority", {}),
]


@pytest.mark.parametrize("model, provider, api_mode, tier, expected", CAPABILITY_MATRIX)
def test_capability_matrix(model, provider, api_mode, tier, expected):
    from hermes_cli.models import resolve_fast_mode_capability

    capability = resolve_fast_mode_capability(
        model=model, provider=provider, api_mode=api_mode, tier=tier
    )
    assert capability.request_overrides == expected
    assert capability.supported is bool(expected)
    if not expected:
        assert capability.reason


def test_default_tier_argument_keeps_priority_contract():
    """Omitting ``tier`` is the pre-ultrafast behavior, byte for byte."""
    from hermes_cli.models import resolve_fast_mode_capability

    for provider, expected in (("openai-api", "priority"), ("openai-codex", "fast")):
        assert resolve_fast_mode_capability(
            model="gpt-5.5", provider=provider, api_mode="codex_responses"
        ).request_overrides == {"service_tier": expected}


def test_model_only_overrides_accept_tier():
    from hermes_cli.models import resolve_fast_mode_overrides

    assert resolve_fast_mode_overrides("gpt-6-astra", tier="ultrafast") == {
        "service_tier": "ultrafast"
    }
    assert resolve_fast_mode_overrides("gpt-5.5", tier="ultrafast") is None
    assert resolve_fast_mode_overrides("gpt-5.5") == {"service_tier": "priority"}
    assert resolve_fast_mode_overrides("gpt-5.5", tier="priority") == {
        "service_tier": "priority"
    }


def test_configured_route_with_unpinned_provider_resolves_ultrafast():
    from hermes_cli.models import resolve_fast_mode_capability_for_configured_route

    capability = resolve_fast_mode_capability_for_configured_route(
        model="gpt-6-astra", provider="auto", api_mode=None, tier="ultrafast"
    )
    assert capability.request_overrides == {"service_tier": "ultrafast"}


# ---------------------------------------------------------------------------
# Turn-route attach sites
# ---------------------------------------------------------------------------

def _runtime(provider="openai-codex", api_mode="codex_responses", **extra):
    runtime = {
        "api_key": "test-key",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "provider": provider,
        "requested_provider": provider,
        "api_mode": api_mode,
        "command": None,
        "args": [],
        "credential_pool": None,
    }
    runtime.update(extra)
    return runtime


@pytest.mark.parametrize(
    "tier, model, expected",
    [
        ("ultrafast", "gpt-6-astra", {"service_tier": "ultrafast"}),
        ("priority", "gpt-5.5", {"service_tier": "fast"}),
        ("ultrafast", "gpt-5.5", {}),
        (None, "gpt-6-astra", {}),
    ],
)
def test_gateway_turn_route_carries_tier(tier, model, expected):
    import gateway.run as gateway_run

    runner = SimpleNamespace(_service_tier=tier)
    route = gateway_run.GatewayRunner._resolve_turn_agent_config(
        runner, "hi", model, _runtime()
    )
    assert (route["request_overrides"] or {}) == expected


def test_gateway_turn_route_merges_tier_over_provider_overrides():
    import gateway.run as gateway_run

    runner = SimpleNamespace(_service_tier="ultrafast")
    runtime = _runtime(
        provider="openai-api",
        request_overrides={"extra_body": {"text": {"verbosity": "low"}}},
    )
    route = gateway_run.GatewayRunner._resolve_turn_agent_config(
        runner, "hi", "gpt-6-astra", runtime
    )
    assert route["request_overrides"] == {
        "extra_body": {"text": {"verbosity": "low"}},
        "service_tier": "ultrafast",
    }


@pytest.mark.parametrize(
    "tier, model, expected",
    [
        ("ultrafast", "gpt-6-astra", {"service_tier": "ultrafast"}),
        ("priority", "gpt-5.5", {"service_tier": "fast"}),
        ("ultrafast", "gpt-5.5", {}),
    ],
)
def test_cli_turn_route_carries_tier(tier, model, expected):
    import cli

    stub = SimpleNamespace(
        api_key="k",
        base_url="https://chatgpt.com/backend-api/codex",
        provider="openai-codex",
        requested_provider="openai-codex",
        api_mode="codex_responses",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        model=model,
        service_tier=tier,
    )
    route = cli.HermesCLI._resolve_turn_agent_config(stub, "hi")
    assert route["request_overrides"] == expected


def _patch_serve_build(monkeypatch, tier, model, provider="openai-codex"):
    import run_agent
    import tui_gateway.server as server

    monkeypatch.setattr(
        run_agent,
        "get_tool_definitions",
        lambda **kwargs: [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run shell commands.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    cfg = {
        "model": {"default": model, "provider": provider},
        "agent": {"service_tier": tier},
    }
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(server, "_get_db", lambda: MagicMock())
    monkeypatch.setattr(server, "_load_reasoning_config", lambda *a, **k: None)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *a, **k: None)
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: (model, provider))
    runtime = _runtime(provider=provider)
    monkeypatch.setattr(
        server,
        "_resolve_runtime_with_fallback",
        lambda kwargs: SimpleNamespace(runtime=dict(runtime), used_fallback=False, selected_model=None),
    )
    return server


class _FakeCreateStream:
    def __init__(self, events):
        self._events = list(events)

    def __iter__(self):
        return iter(self._events)

    def close(self):
        pass


def _completed(served_tier):
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(status="completed", service_tier=served_tier),
    )


def test_serve_path_request_carries_ultrafast_to_outbound_request(monkeypatch):
    """`hermes serve` (tui_gateway) builds the agent from config; the tier
    must reach responses.create(), not just sit on agent.service_tier."""
    server = _patch_serve_build(monkeypatch, "ultrafast", "gpt-6-astra")
    agent = server._make_agent("sid-uf", "key-uf")

    assert agent.api_mode == "codex_responses"
    assert agent.service_tier == "ultrafast"
    assert agent.request_overrides.get("service_tier") == "ultrafast"

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeCreateStream([_completed("ultrafast")])

    agent.client = SimpleNamespace(responses=SimpleNamespace(create=fake_create))
    outbound = agent._build_api_kwargs([{"role": "user", "content": "hi"}])
    response = agent._run_codex_stream(outbound)

    assert captured["service_tier"] == "ultrafast"
    assert response.service_tier == "ultrafast"


def test_serve_path_unsupported_model_sends_no_tier(monkeypatch):
    server = _patch_serve_build(monkeypatch, "ultrafast", "gpt-5.5")
    agent = server._make_agent("sid-uf2", "key-uf2")

    assert agent.request_overrides.get("service_tier") is None
    outbound = agent._build_api_kwargs([{"role": "user", "content": "hi"}])
    assert "service_tier" not in outbound


# ---------------------------------------------------------------------------
# Served-tier honesty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "requested, served, downgraded",
    [
        ("ultrafast", "ultrafast", False),
        ("ultrafast", "default", True),
        ("ultrafast", "priority", True),
        ("priority", "priority", False),
        ("fast", "priority", False),
        ("priority", "default", True),
        (None, "default", False),
        ("ultrafast", None, False),
        ("flex", "default", False),
    ],
)
def test_service_tier_downgraded(requested, served, downgraded):
    from hermes_cli.fast_mode_contracts import service_tier_downgraded

    assert service_tier_downgraded(requested, served) is downgraded


def test_record_served_tier_warns_on_downgrade(caplog):
    from agent.conversation_loop import _record_served_service_tier

    agent = SimpleNamespace(
        request_overrides={"service_tier": "ultrafast"},
        model="gpt-6-astra",
        provider="openai-api",
        _served_service_tier=None,
    )
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        _record_served_service_tier(agent, SimpleNamespace(service_tier="default"))
    assert agent._served_service_tier == "default"
    assert any(
        "requested=ultrafast but served=default" in r.getMessage() for r in caplog.records
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        _record_served_service_tier(agent, SimpleNamespace(service_tier="ultrafast"))
    assert agent._served_service_tier == "ultrafast"
    assert not caplog.records


def test_record_served_tier_codex_echo_is_recorded_but_not_judged(caplog):
    """The Codex backend echoes ``default`` for every tier (measured
    2026-09-29), so it must not raise a downgrade warning on each turn."""
    from agent.conversation_loop import _record_served_service_tier

    agent = SimpleNamespace(
        request_overrides={"service_tier": "ultrafast"},
        model="gpt-6-astra",
        provider="openai-codex",
        _served_service_tier=None,
    )
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        _record_served_service_tier(agent, SimpleNamespace(service_tier="default"))
    assert agent._served_service_tier == "default"
    assert not caplog.records


def test_record_served_tier_ignores_responses_without_tier():
    from agent.conversation_loop import _record_served_service_tier

    agent = SimpleNamespace(request_overrides={}, _served_service_tier=None)
    _record_served_service_tier(agent, SimpleNamespace())
    assert agent._served_service_tier is None


def test_codex_stream_keeps_served_tier(monkeypatch):
    """The Codex stream assembler must keep the terminal frame's service_tier
    (a downgraded ultrafast request reports ``default``)."""
    import run_agent

    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    agent = run_agent.AIAgent(
        model="gpt-6-astra",
        provider="openai-codex",
        api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="codex-token",
        request_overrides={"service_tier": "ultrafast"},
        quiet_mode=True,
        max_iterations=1,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kw: _FakeCreateStream([_completed("default")]))
    )
    response = agent._run_codex_stream(
        agent._build_api_kwargs([{"role": "user", "content": "hi"}])
    )
    assert response.service_tier == "default"


# ---------------------------------------------------------------------------
# Review follow-ups (Prism on #1511)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["flex", "default", "auto", "turbo"])
def test_non_fast_tier_is_never_upgraded_to_priority(tier):
    from hermes_cli.models import resolve_fast_mode_capability, service_tier_request_overrides

    capability = resolve_fast_mode_capability(
        model="gpt-5.5", provider="openai-api", api_mode="codex_responses", tier=tier
    )
    assert capability.supported is False
    assert capability.request_overrides == {}
    assert service_tier_request_overrides(
        model="gpt-5.5", provider="openai-api", api_mode="codex_responses", tier=tier
    ) == {}


def test_served_tier_is_per_call_not_sticky():
    from agent.conversation_loop import _record_served_service_tier

    agent = SimpleNamespace(request_overrides={}, provider="openai-api", _served_service_tier=None)
    _record_served_service_tier(agent, SimpleNamespace(service_tier="ultrafast"))
    assert agent._served_service_tier == "ultrafast"
    _record_served_service_tier(agent, SimpleNamespace())
    assert agent._served_service_tier is None


def test_cli_fast_fast_refused_on_ultrafast_only_route():
    import cli

    stub = SimpleNamespace(
        service_tier=None,
        provider="openai-codex",
        requested_provider="openai-codex",
        api_mode="codex_responses",
        model="gpt-6-astra",
        agent=None,
    )
    stub._fast_command_available = lambda: cli.HermesCLI._fast_command_available(stub)
    with patch.object(cli, "_cprint") as cprint:
        cli.HermesCLI._handle_fast_command(stub, "/fast fast")
    assert stub.service_tier is None
    assert any("ultrafast" in str(c) for c in cprint.call_args_list)

    with patch.object(cli, "_cprint"), patch.object(cli, "save_config_value"):
        cli.HermesCLI._handle_fast_command(stub, "/fast ultrafast")
    assert stub.service_tier == "ultrafast"


def test_slash_mirror_routes_tier_into_request_overrides():
    import tui_gateway.server as server

    agent = SimpleNamespace(
        model="gpt-6-astra",
        provider="openai-codex",
        api_mode="codex_responses",
        service_tier="priority",
        request_overrides={"service_tier": "priority", "extra_body": {"x": 1}},
    )
    session = {"agent": agent}
    with patch.object(server, "_emit"), patch.object(server, "_session_info", return_value={}):
        server._mirror_slash_side_effects("sid", session, "/fast ultrafast")
    assert agent.service_tier == "ultrafast"
    assert agent.request_overrides == {"service_tier": "ultrafast", "extra_body": {"x": 1}}

    # Unsupported route: mirror leaves the session as it was.
    agent.model = "gpt-5.5"
    with patch.object(server, "_emit"), patch.object(server, "_session_info", return_value={}):
        server._mirror_slash_side_effects("sid", session, "/fast ultrafast")
    assert agent.service_tier == "ultrafast"
    assert agent.request_overrides["service_tier"] == "ultrafast"

    with patch.object(server, "_emit"), patch.object(server, "_session_info", return_value={}):
        server._mirror_slash_side_effects("sid", session, "/fast normal")
    assert agent.service_tier is None
    assert agent.request_overrides == {"extra_body": {"x": 1}}
