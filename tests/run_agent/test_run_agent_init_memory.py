"""Agent initialization and memory integration tests for run_agent.AIAgent.

Split verbatim from the former monolithic ``test_run_agent.py`` so the
per-file test runner can schedule each theme independently. Shared fixtures
live in ``conftest.py`` and shared mock builders in ``_run_agent_helpers.py``.
"""

from unittest.mock import patch

import pytest

from run_agent import AIAgent
from tests.run_agent._run_agent_helpers import _make_tool_defs


def test_malformed_memory_config_still_builds_default_store():
    """A non-mapping memory section must not leave an advertised dead tool."""
    malformed = {"memory": "not-a-mapping"}
    with (
        patch(
            "hermes_cli.config.load_config_readonly",
            return_value=malformed,
        ),
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("memory"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-k...7890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=["memory"],
        )

    assert agent._memory_enabled is True
    assert agent._user_profile_enabled is True
    assert agent._memory_store is not None
    assert agent._memory_store.memory_enabled is True
    assert agent._memory_store.user_profile_enabled is True


class TestInit:
    def test_anthropic_base_url_accepted(self):
        """Anthropic base URLs should route to native Anthropic client."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.anthropic_adapter._anthropic_sdk") as mock_anthropic,
        ):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://api.anthropic.com/v1/",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert agent.api_mode == "anthropic_messages"
            mock_anthropic.Anthropic.assert_called_once()

    def test_tool_delay_kwarg_is_deprecated_noop(self):
        """tool_delay stays accepted for compatibility but warns and is ignored."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            with pytest.warns(DeprecationWarning, match="tool_delay"):
                a = AIAgent(
                    api_key="test-key-1234567890",
                    base_url="https://openrouter.ai/api/v1",
                    tool_delay=0,
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                )
            # The value is discarded — nothing downstream reads it anymore.
            assert not hasattr(a, "tool_delay")

    def test_prompt_caching_claude_openrouter(self):
        """Claude model via OpenRouter should enable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-k...7890",
                model="anthropic/claude-sonnet-4-20250514",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._use_prompt_caching is True

    def test_prompt_caching_non_claude(self):
        """Non-Claude model should disable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                model="openai/gpt-4o",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._use_prompt_caching is False


    def test_prompt_caching_native_anthropic(self):
        """Native Anthropic provider should enable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.anthropic_adapter._anthropic_sdk"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://api.anthropic.com/v1/",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a.api_mode == "anthropic_messages"
            assert a._use_prompt_caching is True

    def test_prompt_caching_cache_ttl_defaults_without_config(self):
        """cache_ttl stays 5m when prompt_caching is absent from config."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("hermes_cli.config.load_config", return_value={}), patch("hermes_cli.config.load_config_readonly", return_value={}),
        ):
            a = AIAgent(
                api_key="test-k...7890",
                model="anthropic/claude-sonnet-4-20250514",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._cache_ttl == "5m"

    @pytest.mark.parametrize(
        "falsy_value", [False, None, "off", "false", "disabled", "no", "none"],
    )
    def test_prompt_caching_disabled_by_falsy_cache_ttl(self, falsy_value):
        """Falsy cache_ttl values should fully disable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "hermes_cli.config.load_config",
                return_value={"prompt_caching": {"cache_ttl": falsy_value}},
            ),
            patch(
                "hermes_cli.config.load_config_readonly",
                return_value={"prompt_caching": {"cache_ttl": falsy_value}},
            ),
        ):
            a = AIAgent(
                api_key="test-k...7890",
                model="anthropic/claude-sonnet-4-20250514",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._use_prompt_caching is False
            assert a._use_native_cache_layout is False
            assert a._cache_ttl is None

    def test_prompt_caching_disable_survives_policy_rederivation(self):
        """The disable must survive anthropic_prompt_cache_policy() re-derivation
        (called during /model switch and fallback activation)."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "hermes_cli.config.load_config",
                return_value={"prompt_caching": {"cache_ttl": False}},
            ),
            patch(
                "hermes_cli.config.load_config_readonly",
                return_value={"prompt_caching": {"cache_ttl": False}},
            ),
        ):
            a = AIAgent(
                api_key="test-k...7890",
                model="anthropic/claude-sonnet-4-20250514",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._cache_ttl is None
            # Re-run the policy (simulates /model switch or fallback)
            should_cache, use_native = a._anthropic_prompt_cache_policy()
            assert should_cache is False
            assert use_native is False
            assert a._use_prompt_caching is False


    def test_constructor_max_tokens_wins_over_config(self):
        """Explicit constructor max_tokens keeps programmatic callers stable."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "hermes_cli.config.load_config",
                return_value={"model": {"max_tokens": 4096}},
            ), patch(
                "hermes_cli.config.load_config_readonly",
                return_value={"model": {"max_tokens": 4096}},
            ),
        ):
            a = AIAgent(
                api_key="test-k...7890",
                provider="custom",
                model="claude-opus-4-6-thinking",
                base_url="http://proxy.example/v1",
                max_tokens=8192,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        assert a.max_tokens == 8192


class TestMemoryNudgeCounterPersistence:
    """_turns_since_memory must persist across run_conversation calls."""

    def test_counters_initialized_in_init(self):
        """Counters must exist on the agent after __init__."""
        with patch("run_agent.get_tool_definitions", return_value=[]):
            a = AIAgent(
                model="test", api_key="test-key", base_url="http://localhost:1234/v1",
                provider="openrouter", skip_context_files=True, skip_memory=True,
            )
        assert hasattr(a, "_turns_since_memory")
        assert hasattr(a, "_iters_since_skill")
        assert a._turns_since_memory == 0
        assert a._iters_since_skill == 0


class TestMemoryContextSanitization:
    """sanitize_context() helper correctness — used at provider boundaries."""


    def test_sanitize_context_strips_full_block(self):
        """Helper-level: a string with an embedded memory-context block is
        cleaned to just the surrounding text.  Used by build_memory_context_block
        (input-validation) and by plugins on their own backend boundary."""
        from agent.memory_manager import sanitize_context
        user_text = "how is the honcho working"
        injected = (
            user_text + "\n\n"
            "<memory-context>\n"
            "[System note: The following is recalled memory context, "
            "NOT new user input. Treat as informational background data.]\n\n"
            "## User Representation\n"
            "[2026-01-13 02:13:00] stale observation about AstroMap\n"
            "</memory-context>"
        )
        result = sanitize_context(injected)
        assert "memory-context" not in result.lower()
        assert "stale observation" not in result
        assert "how is the honcho working" in result
