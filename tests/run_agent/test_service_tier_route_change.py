"""agent.service_tier overrides are route-gated on EVERY route change.

Prism t_62b562f6 (post-merge review of #1511): the tier override was gated
against the primary route only. Fallback activation and the in-place
``switch_model`` swap re-derived ``extra_body`` but carried the top-level
``service_tier`` / ``speed`` onto the new route unchanged, so a
``gpt-6-astra`` ultrafast session falling back to OpenRouter (or a custom
endpoint) sent ``service_tier: "ultrafast"`` to an endpoint that never
documented it.
"""

from unittest.mock import MagicMock, patch

from agent.agent_runtime_helpers import _apply_switched_provider_request_overrides
from run_agent import AIAgent


def _make_agent(fallback_model=None):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _activate(agent, model):
    client = MagicMock()
    client.base_url = "https://openrouter.ai/api/v1"
    client.api_key = "fb-key"
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(client, model),
    ), patch(
        "agent.model_metadata.get_model_context_length",
        return_value=128_000,
    ):
        assert agent._try_activate_fallback() is True


def _ultrafast_primary(agent):
    agent.provider = "openai-codex"
    agent.api_mode = "codex_responses"
    agent.model = "gpt-6-astra"
    agent.service_tier = "ultrafast"
    agent.request_overrides = {"service_tier": "ultrafast", "extra_body": {"k": 1}}


def test_fallback_to_ungated_route_drops_tier_override():
    agent = _make_agent(
        fallback_model={"provider": "openrouter", "model": "openai/gpt-5.5"}
    )
    _ultrafast_primary(agent)
    _activate(agent, "openai/gpt-5.5")
    assert agent.provider == "openrouter"
    assert "service_tier" not in agent.request_overrides
    assert "speed" not in agent.request_overrides
    # Non-tier caller overrides survive the swap.
    assert agent.request_overrides.get("extra_body") == {"k": 1}
    # The session's requested tier is untouched (primary restore re-applies it).
    assert agent.service_tier == "ultrafast"


def test_fallback_drops_anthropic_speed_on_non_anthropic_route():
    agent = _make_agent(
        fallback_model={"provider": "openrouter", "model": "openai/gpt-5.5"}
    )
    agent.provider = "anthropic"
    agent.api_mode = "anthropic_messages"
    agent.model = "claude-opus-4-6"
    agent.service_tier = "priority"
    agent.request_overrides = {"speed": "fast"}
    _activate(agent, "openai/gpt-5.5")
    assert "speed" not in agent.request_overrides
    assert "service_tier" not in agent.request_overrides


def test_switch_model_regates_tier_both_directions():
    agent = _make_agent()
    _ultrafast_primary(agent)
    # Swap onto a route with no ultrafast contract: the tier must not ride along.
    agent.provider = "openrouter"
    agent.api_mode = "chat_completions"
    agent.model = "openai/gpt-5.5"
    _apply_switched_provider_request_overrides(agent, "openrouter")
    assert "service_tier" not in agent.request_overrides
    # Swap back onto a supporting route: the session tier is re-attached.
    agent.provider = "openai-codex"
    agent.api_mode = "codex_responses"
    agent.model = "gpt-6-astra"
    _apply_switched_provider_request_overrides(agent, "openai-codex")
    assert agent.request_overrides.get("service_tier") == "ultrafast"
