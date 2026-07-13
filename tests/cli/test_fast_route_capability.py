"""Route-aware Fast/Priority capability contracts."""

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("provider", "api_mode", "model", "supported", "family", "overrides"),
    [
        (
            "openai-api",
            "codex_responses",
            "gpt-5.6-sol",
            True,
            "openai_priority",
            {"service_tier": "priority"},
        ),
        (
            "openai-codex",
            "codex_responses",
            "gpt-5.6-sol",
            False,
            "codex_fast",
            {},
        ),
        (
            "openai-codex",
            "codex_responses",
            "gpt-5.5",
            True,
            "codex_fast",
            {"service_tier": "fast"},
        ),
        (
            "openai-codex",
            "codex_responses",
            "gpt-5.4-mini",
            True,
            "codex_fast",
            {"service_tier": "fast"},
        ),
        (
            "anthropic",
            "anthropic_messages",
            "claude-opus-4-6",
            True,
            "anthropic_fast",
            {"speed": "fast"},
        ),
        (
            "anthropic",
            "anthropic_messages",
            "claude-opus-4-8",
            False,
            "anthropic_fast",
            {},
        ),
        ("openrouter", "chat_completions", "gpt-5.6-sol", False, "unsupported", {}),
        ("custom:proxy", "codex_responses", "gpt-5.5", False, "unsupported", {}),
    ],
)
def test_fast_capability_is_route_specific(
    provider, api_mode, model, supported, family, overrides
):
    from hermes_cli.models import resolve_fast_mode_capability

    capability = resolve_fast_mode_capability(
        model=model,
        provider=provider,
        api_mode=api_mode,
    )

    assert capability.supported is supported
    assert capability.family == family
    assert capability.request_overrides == overrides


def test_opus_48_guidance_names_separate_fast_model():
    from hermes_cli.models import resolve_fast_mode_capability

    capability = resolve_fast_mode_capability(
        model="claude-opus-4-8",
        provider="anthropic",
        api_mode="anthropic_messages",
    )

    assert capability.supported is False
    assert "claude-opus-4-8-fast" in capability.reason
    assert "speed" in capability.reason


def test_opus_48_proxy_route_stays_unsupported_but_keeps_separate_model_guidance():
    from hermes_cli.models import resolve_fast_mode_capability

    capability = resolve_fast_mode_capability(
        model="claude-opus-4-8",
        provider="claude-apr",
        api_mode="anthropic_messages",
    )

    assert capability.supported is False
    assert capability.family == "unsupported"
    assert capability.request_overrides == {}
    assert "claude-opus-4-8-fast" in capability.reason
    assert "speed=fast" in capability.reason


def test_fast_capability_catalog_entries_exist_in_provider_catalogs():
    from hermes_cli.models import FAST_MODE_CAPABILITY_CATALOG, provider_model_ids

    for contract in FAST_MODE_CAPABILITY_CATALOG.values():
        assert contract["source_url"].startswith("https://")
        assert contract["checked_date"] == "2026-07-12"
        for provider in contract["providers"]:
            catalog = set(provider_model_ids(provider))
            assert set(contract["models"]) <= catalog


@pytest.mark.parametrize(
    ("family", "provider", "api_mode", "added_model"),
    [
        ("codex_fast", "openai-codex", "codex_responses", "gpt-catalog-only"),
        (
            "anthropic_fast",
            "anthropic",
            "anthropic_messages",
            "claude-catalog-only",
        ),
    ],
)
def test_route_capability_support_is_driven_by_catalog(
    monkeypatch, family, provider, api_mode, added_model
):
    from hermes_cli.models import (
        FAST_MODE_CAPABILITY_CATALOG,
        resolve_fast_mode_capability,
    )

    contract = FAST_MODE_CAPABILITY_CATALOG[family]
    original_models = contract["models"]
    removed_model = original_models[0]
    monkeypatch.setitem(contract, "models", (added_model,))

    assert resolve_fast_mode_capability(
        model=added_model, provider=provider, api_mode=api_mode
    ).supported
    assert not resolve_fast_mode_capability(
        model=removed_model, provider=provider, api_mode=api_mode
    ).supported


def test_request_enforcement_call_sites_do_not_use_model_only_wrapper():
    root = Path(__file__).resolve().parents[2]
    compatibility_definition = root / "hermes_cli" / "models.py"
    production_files = (
        path
        for path in root.rglob("*.py")
        if compatibility_definition != path
        and "tests" not in path.relative_to(root).parts
        and not any(
            part.startswith(".") or part in {"venv", "node_modules"}
            for part in path.relative_to(root).parts
        )
    )

    for path in production_files:
        source = path.read_text(encoding="utf-8")
        assert "resolve_fast_mode_overrides(" not in source, path
