"""Tests for per-model reasoning_effort override during fallback activation.

Tests that try_activate_fallback re-resolves reasoning_config when
swapping to a fallback model, so per-model overrides are honored even
during error recovery.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_reasoning_agent():
    """Minimal AIAgent-like object for driving the REAL try_activate_fallback
    through the reasoning-effort branch (mirrors the harness in
    test_fallback_credential_isolation.py)."""
    agent = MagicMock()
    agent.provider = "claude-apr"
    agent.model = "claude-fable-5"
    agent.base_url = "http://127.0.0.1:18800/v1"
    agent.api_mode = "chat_completions"
    agent.api_key = "primary-key"
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = [
        # No per-entry reasoning_effort — the incident shape.
        {"provider": "claude-apx-1", "model": "claude-opus-4-8"},
    ]
    agent._primary_runtime = {
        "provider": "claude-apr",
        "model": "claude-fable-5",
        "base_url": agent.base_url,
        "api_mode": "chat_completions",
        "api_key": "primary-key",
        "client_kwargs": {"api_key": "primary-key", "base_url": agent.base_url},
        "use_prompt_caching": False,
        "use_native_cache_layout": False,
        "anthropic_api_key": "",
        "anthropic_base_url": "",
    }
    agent._config_context_length = None
    agent._credential_pool = None
    agent._rate_limited_until = 0
    agent._transport_cache = {}
    agent._client_kwargs = {"api_key": "primary-key", "base_url": agent.base_url}
    agent._buffer_status = MagicMock()
    agent._is_azure_openai_url.return_value = False
    agent._is_direct_openai_url.return_value = False
    agent._provider_model_requires_responses_api.return_value = False
    agent._anthropic_prompt_cache_policy.return_value = (False, False)
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    agent._replace_primary_openai_client = MagicMock()
    agent.context_compressor = None
    agent.status_callback = None
    return agent


class TestFallbackReasoningOverride:
    """Test try_activate_fallback re-resolves reasoning_config."""

    def test_fallback_re_resolves_reasoning_config(self):
        """When fallback activates, reasoning_config should be re-resolved.

        We test the resolution logic directly rather than spinning up a
        full try_activate_fallback (which requires extensive agent setup).
        The production code calls resolve_per_model_reasoning_effort with
        the fallback model string — we verify that works correctly.
        """
        from hermes_constants import resolve_per_model_reasoning_effort

        # Simulate: primary was gemini-flash (medium), fallback to claude-opus-4.5 (xhigh)
        overrides = {
            "claude-opus-4.5": "xhigh",
            "gemini-flash": "medium",
        }

        # Fallback model lookup
        fb_result = resolve_per_model_reasoning_effort("claude-opus-4.5", overrides)
        assert fb_result is not None
        assert fb_result["effort"] == "xhigh"

        # Primary model lookup (for comparison)
        primary_result = resolve_per_model_reasoning_effort("gemini-flash", overrides)
        assert primary_result is not None
        assert primary_result["effort"] == "medium"

        # The key point: fallback result differs from primary
        assert fb_result["effort"] != primary_result["effort"]

    def test_fallback_to_model_without_override_uses_global(self):
        """Fallback to a model with no override should resolve to None (→ global)."""
        from hermes_constants import resolve_per_model_reasoning_effort

        overrides = {"claude-opus-4.5": "xhigh"}

        # Fallback to gpt-5 which has no override
        result = resolve_per_model_reasoning_effort("gpt-5", overrides)
        assert result is None  # caller falls back to global


    def test_fallback_recovery_restores_primary_reasoning(self):
        """After fallback + restore_primary_runtime, reasoning_config returns to primary's value.

        This tests the integration of Task 6 (_primary_runtime snapshot) with
        Task 6b (fallback re-resolution). The full cycle:
        1. Primary model = gemini-flash, reasoning = medium
        2. /model switch → _primary_runtime captures reasoning_config
        3. Fallback activates → reasoning re-resolved for fallback model
        4. restore_primary_runtime → reasoning_config restored from snapshot
        """
        from agent.agent_runtime_helpers import restore_primary_runtime

        agent = MagicMock()
        # Simulate: _primary_runtime was captured during /model switch
        agent._primary_runtime = {
            "model": "gemini-flash",
            "provider": "google",
            "base_url": "",
            "api_mode": "openai",
            "api_key": "key",
            "client_kwargs": {},
            "use_prompt_caching": False,
            "use_native_cache_layout": False,
            "reasoning_config": {"enabled": True, "effort": "medium"},
            "compressor_model": "gemini-flash",
            "compressor_base_url": "",
            "compressor_api_key": "",
            "compressor_provider": "",
            "compressor_context_length": 0,
            "compressor_api_mode": "",
            "compressor_threshold_tokens": 0,
        }
        agent._fallback_activated = True
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._transport_cache = {}
        agent._config_context_length = None
        agent._rate_limited_until = 0
        # During fallback, reasoning was changed to xhigh (fallback model's override)
        agent.model = "claude-opus-4.5"
        agent.provider = "anthropic"
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}
        agent.context_compressor = MagicMock()
        agent.base_url = ""
        agent._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        agent._ensure_lmstudio_runtime_loaded = MagicMock()

        result = restore_primary_runtime(agent)
        assert result is True
        # reasoning_config should be restored to primary's value (medium)
        assert agent.reasoning_config == {"enabled": True, "effort": "medium"}

    def test_fallback_global_fallback_with_yaml_false(self):
        """Fallback global fallback must not coerce YAML boolean False.

        Regression: ``or ""`` turned False into "", silently re-enabling
        thinking. The raw value must pass through so
        parse_reasoning_effort(False) returns {'enabled': False}.

        The production code in try_activate_fallback does:
            _fb_global_effort = _fb_agent_cfg.get("reasoning_effort", "")
            agent.reasoning_config = parse_reasoning_effort(_fb_global_effort)
        We verify that passing the raw False (not coerced "") produces
        the disabled config.
        """
        from hermes_constants import parse_reasoning_effort

        # Simulate: no per-model override matches, global is YAML False
        _fb_agent_cfg = {"reasoning_effort": False}

        # This is the exact line from try_activate_fallback's else branch.
        # The bug was: _fb_global_effort = _fb_agent_cfg.get(...) or ""
        # which turned False into "". The fix passes the raw value.
        _fb_global_effort = _fb_agent_cfg.get("reasoning_effort", "")
        result = parse_reasoning_effort(_fb_global_effort)

        assert result is not None
        assert result.get("enabled") is False

    def test_no_per_entry_key_applies_only_per_model_override(self, monkeypatch, tmp_path):
        """Absent per-entry ``reasoning_effort`` → the fallback path applies
        ONLY a per-model override (``agent.reasoning_overrides``) for the
        fallback model. The global ``agent.reasoning_effort`` is deliberately
        NOT re-applied: re-resolving the global clobbered a live session
        override (/reasoning xhigh → medium) on every failover — the
        2026-08-05 demotion bug.
        """
        from hermes_constants import resolve_per_model_reasoning_effort

        overrides = {"claude-opus-4-8": "xhigh"}
        # Per-model override for the fallback model: applies.
        result = resolve_per_model_reasoning_effort("claude-opus-4-8", overrides)
        assert result is not None and result["effort"] == "xhigh"
        # No per-model override → None → the production code keeps the
        # session's current reasoning_config untouched (the global default
        # must NOT be re-imposed here).
        assert resolve_per_model_reasoning_effort("some-other-model", overrides) is None

    def test_session_effort_survives_fallback_without_per_model_override(self):
        """End-to-end through the REAL try_activate_fallback: a session-level
        xhigh must survive failover to a model with no per-entry and no
        per-model override, even when the config file carries a global
        ``agent.reasoning_effort: medium`` (the exact 2026-08-05 incident
        shape: fable@xhigh session → opus fallback → demoted to medium).
        """
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_reasoning_agent()
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        fallback_client = SimpleNamespace(
            api_key="fb-key",
            base_url="https://api.anthropic.com",
            _custom_headers={},
        )
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, "claude-opus-4-8"),
        ), patch(
            "agent.credential_pool.load_pool", return_value=None,
        ), patch(
            "hermes_cli.config.load_config",
            return_value={
                "model": {"default": "claude-fable-5", "provider": "claude-apr"},
                # Global default present — the OLD code re-applied this and
                # clobbered the session's xhigh; the fix must NOT.
                "agent": {"reasoning_effort": "medium"},
            },
        ):
            assert try_activate_fallback(agent) is True

        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}, (
            "session-level reasoning effort was clobbered by the global "
            "config default during fallback activation"
        )

    def test_per_model_override_still_applies_on_fallback(self):
        """A per-model override for the fallback model still wins (the
        deliberate, model-specific directive is honored)."""
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_reasoning_agent()
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        fallback_client = SimpleNamespace(
            api_key="fb-key",
            base_url="https://api.anthropic.com",
            _custom_headers={},
        )
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, "claude-opus-4-8"),
        ), patch(
            "agent.credential_pool.load_pool", return_value=None,
        ), patch(
            "hermes_cli.config.load_config",
            return_value={
                "model": {"default": "claude-fable-5", "provider": "claude-apr"},
                "agent": {
                    "reasoning_effort": "medium",
                    "reasoning_overrides": {"claude-opus-4-8": "high"},
                },
            },
        ):
            assert try_activate_fallback(agent) is True

        assert agent.reasoning_config is not None
        assert agent.reasoning_config.get("effort") == "high"

    def test_per_entry_key_still_wins_over_chokepoint(self):
        """The fork's per-entry key must take precedence, and the no-per-entry
        arm must resolve ONLY the per-model override — never the global
        default (which would clobber a live session effort; 2026-08-05 bug).
        Assert the source structure so a merge can't silently reorder it."""
        import inspect

        import agent.chat_completion_helpers as cch

        src = inspect.getsource(cch.try_activate_fallback)
        entry_idx = src.index('fb.get("reasoning_effort")')
        per_model_idx = src.index("resolve_per_model_reasoning_effort(")
        assert entry_idx < per_model_idx, (
            "per-entry override must be checked BEFORE the per-model resolve"
        )
        # The per-model resolve must live in the OUTER else-arm (no per-entry
        # key) — anchor by searching BACKWARD from the call for the nearest
        # else:, so the inner unknown-level else can't satisfy this.
        outer_else_idx = src.rindex("else:", entry_idx, per_model_idx)
        else_arm = src[outer_else_idx:per_model_idx + 400]
        assert "resolve_per_model_reasoning_effort(" in else_arm
        assert "fb_model" in else_arm
        # And nothing between that else: and the call re-enters the
        # per-entry branch (the else is the one guarding the resolve).
        assert 'fb.get("reasoning_effort")' not in else_arm
        # The GLOBAL-default chokepoint must NOT be used in this arm — it
        # re-imposed agent.reasoning_effort over the session's live config.
        assert "resolve_reasoning_config" not in src, (
            "try_activate_fallback must not re-resolve the global reasoning "
            "default — it clobbers session-level overrides on failover"
        )


class TestFallbackEffortAnnounceHonesty:
    """The route-change announce + audit sink must report the agent's ACTUAL
    post-swap effort, not just the per-entry field. 2026-08-05 incident: the
    sink logged ``@xhigh -> @xhigh`` while the agent actually ran medium,
    because only the (absent) per-entry ``reasoning_effort`` was consulted.
    """

    def test_announce_reports_actual_post_swap_effort(self):
        from agent import chat_completion_helpers as cch

        agent = _make_reasoning_agent()
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        fallback_client = SimpleNamespace(
            api_key="fb-key",
            base_url="https://api.anthropic.com",
            _custom_headers={},
        )
        captured = {}

        def _capture_announce(agent_, old_model, new_model, new_provider, **kw):
            captured["new_effort"] = kw.get("new_effort")
            captured["old_effort"] = kw.get("old_effort")

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, "claude-opus-4-8"),
        ), patch(
            "agent.credential_pool.load_pool", return_value=None,
        ), patch(
            "hermes_cli.config.load_config",
            return_value={
                "model": {"default": "claude-fable-5", "provider": "claude-apr"},
                "agent": {
                    "reasoning_effort": "medium",
                    # Per-model override CHANGES the effective effort on swap.
                    "reasoning_overrides": {"claude-opus-4-8": "high"},
                },
            },
        ), patch.object(
            cch, "_emit_fallback_announce", side_effect=_capture_announce,
        ), patch.object(
            cch, "_append_route_change",
        ):
            assert cch.try_activate_fallback(agent) is True

        # The agent actually runs "high" now (per-model override); the
        # announce must say so — NOT echo the stale primary "xhigh".
        assert agent.reasoning_config.get("effort") == "high"
        assert captured.get("old_effort") == "xhigh"
        assert captured.get("new_effort") == "high", (
            f"announce reported {captured.get('new_effort')!r} but the agent "
            "actually runs 'high' — the effort rider is lying"
        )
