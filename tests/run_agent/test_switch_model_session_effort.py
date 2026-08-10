"""Card 2 — ``/model`` must not clobber the session's reasoning effort.

Fork PR #467 fixed the global-default clobber on the FAILOVER path. The
ratified precedence (Ace-approved 2026-08-06) is:

    per-entry / caller-supplied effort
      > per-model ``agent.reasoning_overrides`` for the NEW model
      > KEEP the session's current effort.
    The global default is NEVER re-imposed by a route change.

``switch_model`` is the same class of route change and re-resolved from config
only (``resolve_reasoning_config(load_config(), model)``), which is blind to a
session ``/reasoning`` override (that lives on the gateway's SessionState, not
in config.yaml). A session pinned to ``high`` was demoted to the global default
``medium`` for the remainder of the switching turn.

Every assertion here reads the AGENT OBJECT after the switch, never the config.
"""

from unittest.mock import MagicMock, patch

import pytest


CFG = {
    "model": {"default": "claude-sonnet-4-6"},
    "agent": {
        # The global default the clobber used to re-impose.
        "reasoning_effort": "medium",
        # A per-model override that legitimately applies to ONE model.
        "reasoning_overrides": {"gpt-5.5": "low"},
    },
}


def _agent(effort: "str | None" = "high"):
    """Minimal live-session agent pinned to a session ``/reasoning`` effort."""
    a = MagicMock()
    a.model = "claude-sonnet-4-6"
    a.provider = "anthropic"
    a.base_url = "https://api.anthropic.com"
    a.api_mode = "anthropic_messages"
    a.api_key = "k"
    a._client_kwargs = {"api_key": "k", "base_url": "https://api.anthropic.com"}
    a._use_prompt_caching = False
    a._use_native_cache_layout = False
    a.reasoning_config = {"enabled": True, "effort": effort} if effort else None
    a._fallback_activated = False
    a._fallback_index = 0
    a._fallback_chain = []
    a._fallback_model = None
    a._config_context_length = None
    a._transport_cache = {}
    a.context_compressor = None
    a._cached_system_prompt = None
    a._anthropic_api_key = ""
    a._anthropic_base_url = None
    a._is_anthropic_oauth = False
    a._anthropic_prompt_cache_policy = MagicMock(return_value=(False, False))
    a._ensure_lmstudio_runtime_loaded = MagicMock()
    a._create_openai_client = MagicMock(return_value=MagicMock())
    return a


def _switch(agent, new_model, **extra):
    from agent.agent_runtime_helpers import switch_model

    with patch("hermes_cli.config.load_config", return_value=CFG):
        switch_model(
            agent,
            new_model=new_model,
            new_provider="anthropic",
            base_url="https://api.anthropic.com",
            api_mode="anthropic_messages",
            **extra,
        )


class TestSwitchModelKeepsSessionEffort:
    def test_session_effort_survives_switch_to_unoverridden_model(self):
        """THE bug: /model <x> on a session pinned high must stay high.

        The new model has no per-model override, so nothing may touch the
        live effort — least of all the global ``agent.reasoning_effort``.
        """
        agent = _agent(effort="high")
        _switch(agent, "claude-opus-5")
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}, (
            "switch_model re-imposed the global config default over the "
            "session's live effort"
        )

    def test_global_default_is_never_reimposed(self):
        """Any effort above the global default survives, not just 'high'."""
        agent = _agent(effort="xhigh")
        _switch(agent, "claude-opus-5")
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

    def test_explicitly_disabled_thinking_survives(self):
        """``/reasoning none`` must not be silently re-enabled by a switch."""
        agent = _agent(effort=None)
        agent.reasoning_config = {"enabled": False}
        _switch(agent, "claude-opus-5")
        assert agent.reasoning_config == {"enabled": False}

    def test_per_model_override_still_applies(self):
        """NEGATIVE CONTROL: a model that genuinely HAS a per-model override
        still picks it up — the fix must not turn switch_model into a no-op."""
        agent = _agent(effort="high")
        _switch(agent, "gpt-5.5")
        assert agent.reasoning_config == {"enabled": True, "effort": "low"}, (
            "per-model agent.reasoning_overrides must still win for the new model"
        )

    def test_primary_runtime_snapshots_the_kept_effort(self):
        """``_primary_runtime`` must carry the effort actually in force, so a
        later fallback→restore round-trip doesn't resurrect the demotion."""
        agent = _agent(effort="high")
        _switch(agent, "claude-opus-5")
        assert agent._primary_runtime["reasoning_config"] == {
            "enabled": True, "effort": "high",
        }


class TestCallerSuppliedSessionEffort:
    """The gateway owns session state; ``agent/`` cannot see it. The explicit
    parameter is how the session-resolved value reaches switch_model."""

    def test_caller_supplied_config_wins(self):
        agent = _agent(effort="high")
        _switch(
            agent,
            "claude-opus-5",
            session_reasoning_config={"enabled": True, "effort": "xhigh"},
        )
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

    def test_caller_supplied_config_wins_over_per_model_override(self):
        """The caller already resolved the FULL ladder (session > per-model >
        global) for the new model, so its answer is final."""
        agent = _agent(effort="high")
        _switch(
            agent,
            "gpt-5.5",  # has per-model override 'low'
            session_reasoning_config={"enabled": True, "effort": "xhigh"},
        )
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

    def test_caller_supplied_none_means_provider_default(self):
        """An explicit ``None`` is a real answer (nothing configured), distinct
        from the parameter being omitted."""
        agent = _agent(effort="high")
        _switch(agent, "claude-opus-5", session_reasoning_config=None)
        assert agent.reasoning_config is None

    def test_caller_supplied_dict_is_copied_not_aliased(self):
        """Mutating the agent's config must not write back into session state."""
        session_cfg = {"enabled": True, "effort": "xhigh"}
        agent = _agent(effort="high")
        _switch(agent, "claude-opus-5", session_reasoning_config=session_cfg)
        agent.reasoning_config["effort"] = "mutated"
        assert session_cfg == {"enabled": True, "effort": "xhigh"}


class TestResolutionFailureKeepsCurrent:
    def test_config_read_failure_keeps_session_effort(self):
        """A broken config read must never break the swap NOR demote the
        session — the failure path keeps the current effort."""
        agent = _agent(effort="high")
        with patch(
            "hermes_cli.config.load_config", side_effect=RuntimeError("boom")
        ):
            from agent.agent_runtime_helpers import switch_model

            switch_model(
                agent,
                new_model="claude-opus-5",
                new_provider="anthropic",
                base_url="https://api.anthropic.com",
                api_mode="anthropic_messages",
            )
        assert agent.reasoning_config == {"enabled": True, "effort": "high"}
        assert agent.model == "claude-opus-5", "the swap itself must still land"


class TestForwarderThreadsTheParameter:
    def test_aiagent_forwarder_passes_session_reasoning_config(self):
        """``AIAgent.switch_model`` is the only entry point gateway callers use;
        it must forward the kwarg rather than swallowing it."""
        import run_agent

        seen = {}

        def _fake(agent, new_model, new_provider, api_key='', base_url='',
                  api_mode='', **kwargs):
            seen.update(kwargs)
            return True

        obj = object.__new__(run_agent.AIAgent)
        with patch("agent.agent_runtime_helpers.switch_model", _fake):
            obj.switch_model(
                new_model="m",
                new_provider="p",
                session_reasoning_config={"enabled": True, "effort": "high"},
            )
        assert seen == {
            "session_reasoning_config": {"enabled": True, "effort": "high"}
        }

    def test_aiagent_forwarder_omits_kwarg_when_not_given(self):
        """Callers that don't own session state must not accidentally pin
        ``None`` — the sentinel default has to survive."""
        import run_agent

        seen = {}

        def _fake(agent, new_model, new_provider, api_key='', base_url='',
                  api_mode='', **kwargs):
            seen["kwargs"] = kwargs
            return True

        obj = object.__new__(run_agent.AIAgent)
        with patch("agent.agent_runtime_helpers.switch_model", _fake):
            obj.switch_model(new_model="m", new_provider="p")
        assert seen["kwargs"] == {}
