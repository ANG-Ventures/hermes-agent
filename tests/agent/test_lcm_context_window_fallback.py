"""A rejected leaf must reach the configured larger-window compression seat."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent import auxiliary_client as aux
from plugins.context_engine.lcm import escalation


class PromptTooLong(Exception):
    status_code = 500


def test_leaf_prompt_too_long_uses_configured_chain_at_level_one(monkeypatch):
    # Captured bpr error text; no live provider traffic or credentials.
    source = "conversation detail and decisions. " * 40000
    rejected = PromptTooLong("500 Claude Code returned an error result: Prompt is too long")
    primary = MagicMock()
    primary.base_url = "https://bpr.invalid/v1"
    primary.chat.completions.create.side_effect = rejected
    fallback = MagicMock()
    fallback.base_url = "https://luna.invalid/v1"
    served = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Decisions retained."), finish_reason="stop")]
    )
    seen = []

    def primary_route(provider, model=None, **kwargs):
        return primary, "claude-sonnet-5-5"

    def fallback_route(entry):
        assert entry["model"] == "gpt-6-luna-900k"
        return fallback, entry["model"]

    def capture_primary(**kwargs):
        seen.append(("primary", kwargs["messages"][0]["content"]))
        raise rejected

    def capture_fallback(**kwargs):
        seen.append(("fallback", kwargs["messages"][0]["content"]))
        return served

    primary.chat.completions.create.side_effect = capture_primary
    fallback.chat.completions.create.side_effect = capture_fallback
    monkeypatch.setattr(aux, "_resolve_task_provider_model", lambda *a, **k: ("claude-bpr", "claude-sonnet-5-5", None, None, None))
    monkeypatch.setattr(aux, "_get_cached_client", primary_route)
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {
        "provider": "claude-bpr", "model": "claude-sonnet-5-5",
        "fallback_chain": [{"provider": "openai-codex", "model": "gpt-6-luna-900k"}],
    })
    monkeypatch.setattr(aux, "_resolve_fallback_entry", fallback_route)
    monkeypatch.setattr(aux, "_transient_retry_count", lambda: 2)
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0)
    monkeypatch.setattr(aux, "_task_minimum_context_length", lambda task: None)
    monkeypatch.setattr(aux, "_record_aux_call_cost", lambda *a, **k: None)
    monkeypatch.setattr("agent.aux_accounting.record_aux_api_call", lambda *a, **k: None)
    result, level = escalation.summarize_with_escalation(
        source, source_tokens=300000, token_budget=2000,
    )
    assert (result, level) == ("Decisions retained.", 1)
    assert [name for name, _ in seen] == ["primary", "fallback"]
    assert seen[0][1] == seen[1][1]
    assert source in seen[1][1]
