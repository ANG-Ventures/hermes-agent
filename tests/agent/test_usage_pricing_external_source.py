"""External pricing fallback and display-path cache safety.

The catalog fixtures are in-memory only: no test depends on live models.dev or
OpenRouter data.
"""

from decimal import Decimal

import pytest
import yaml

import agent.model_metadata as model_metadata
import agent.models_dev as models_dev
import agent.usage_pricing as usage_pricing
from agent.usage_pricing import get_pricing_entry


@pytest.fixture
def seeded_models_dev(monkeypatch):
    """Install a deterministic models.dev registry and return a seeder."""

    def seed(registry):
        monkeypatch.setattr(models_dev, "_models_dev_cache", registry)
        monkeypatch.setattr(models_dev, "_models_dev_cache_time", float("inf"))

    seed({})
    return seed


@pytest.fixture
def default_source_order(monkeypatch):
    monkeypatch.setattr(
        usage_pricing,
        "_pricing_source_order",
        lambda: ("models_dev", "openrouter"),
        raising=False,
    )


@pytest.fixture
def openrouter_catalog_knows_glm5(monkeypatch):
    catalog = {
        "glm-5": {
            "pricing": {
                "prompt": "0.00000095",
                "completion": "0.0000038",
            }
        }
    }
    monkeypatch.setattr(
        usage_pricing,
        "fetch_model_metadata",
        lambda *args, **kwargs: catalog,
    )
    return catalog


def test_models_dev_cost_reader_preserves_all_rate_classes():
    assert models_dev._extract_cost(
        {
            "cost": {
                "input": 5,
                "output": 25,
                "cache_read": 0.5,
                "cache_write": 6.25,
            }
        }
    ) == {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    }


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {},
        {"cost": {}},
        {"cost": {"cache_read": 1}},
        {"cost": {"input": True, "output": False}},
        {"cost": {"input": "5", "output": "25"}},
        {"cost": {"input": -1, "output": -2}},
    ],
)
def test_models_dev_cost_reader_rejects_unpriced_or_malformed_entries(entry):
    assert models_dev._extract_cost(entry) is None


def test_models_dev_pricing_is_provider_scoped(seeded_models_dev):
    seeded_models_dev(
        {
            "xiaomi": {
                "models": {
                    "mimo-new": {"cost": {"input": 1, "output": 3}},
                }
            }
        }
    )

    assert models_dev.lookup_models_dev_pricing("xiaomi", "mimo-new") == {
        "input": 1.0,
        "output": 3.0,
    }
    assert models_dev.lookup_models_dev_pricing("custom", "mimo-new") is None
    assert models_dev.lookup_models_dev_pricing("unknown", "mimo-new") is None


def test_snapshot_miss_prices_from_models_dev(seeded_models_dev, default_source_order):
    model = "claude-not-in-snapshot-1"
    assert ("anthropic", model) not in usage_pricing._OFFICIAL_DOCS_PRICING
    seeded_models_dev(
        {
            "anthropic": {
                "models": {
                    model: {
                        "cost": {
                            "input": 5,
                            "output": 25,
                            "cache_read": 0.5,
                            "cache_write": 6.25,
                        }
                    }
                }
            }
        }
    )

    entry = get_pricing_entry(model, provider="anthropic")

    assert entry is not None
    assert entry.input_cost_per_million == Decimal("5")
    assert entry.output_cost_per_million == Decimal("25")
    assert entry.cache_read_cost_per_million == Decimal("0.5")
    assert entry.cache_write_cost_per_million == Decimal("6.25")
    assert entry.pricing_version == "models-dev-api"


def test_curated_snapshot_still_precedes_external_catalog(
    seeded_models_dev, default_source_order
):
    seeded_models_dev(
        {
            "openai": {
                "models": {
                    "gpt-4o": {"cost": {"input": 999, "output": 999}},
                }
            }
        }
    )

    entry = get_pricing_entry("gpt-4o", provider="openai")

    assert entry is not None
    assert entry.pricing_version != "models-dev-api"
    assert entry.input_cost_per_million != Decimal("999")


@pytest.mark.parametrize("provider", [None, "unknown", "custom", "local", "xiaomi"])
def test_openrouter_bare_id_lookup_is_reserved_for_openrouter_routes(
    provider, openrouter_catalog_knows_glm5
):
    route = usage_pricing.resolve_billing_route("glm-5", provider=provider)
    assert route.provider != "openrouter"

    assert usage_pricing._openrouter_pricing_entry(route) is None


def test_openrouter_guard_is_not_vacuous(openrouter_catalog_knows_glm5):
    entry = usage_pricing._pricing_entry_from_metadata(
        openrouter_catalog_knows_glm5,
        "glm-5",
        source_url="test",
        pricing_version="test",
    )

    assert entry is not None
    assert entry.input_cost_per_million == Decimal("0.95000000")


@pytest.mark.parametrize("provider", [None, "unknown", "custom", "local"])
def test_unknown_or_self_hosted_route_never_falls_through_to_openrouter(
    provider,
    seeded_models_dev,
    default_source_order,
    openrouter_catalog_knows_glm5,
):
    seeded_models_dev({"xiaomi": {"models": {}}})

    assert get_pricing_entry("glm-5", provider=provider) is None
    assert usage_pricing.has_known_pricing("glm-5", provider=provider) is False


def test_mapped_named_provider_reaches_models_dev_despite_unknown_billing_mode(
    seeded_models_dev, default_source_order
):
    seeded_models_dev(
        {
            "xiaomi": {
                "models": {
                    "mimo-new": {"cost": {"input": 1, "output": 3}},
                }
            }
        }
    )
    route = usage_pricing.resolve_billing_route("mimo-new", provider="xiaomi")
    assert route.billing_mode == "unknown"

    entry = get_pricing_entry("mimo-new", provider="xiaomi")

    assert entry is not None
    assert entry.input_cost_per_million == Decimal("1")
    assert entry.pricing_version == "models-dev-api"


def test_openrouter_route_keeps_its_bare_id_rate_card(openrouter_catalog_knows_glm5):
    entry = get_pricing_entry("glm-5", provider="openrouter")

    assert entry is not None
    assert entry.input_cost_per_million == Decimal("0.95000000")
    assert entry.pricing_version == "openrouter-models-api"


def test_external_source_order_is_configurable(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"pricing": {"external_source": "openrouter"}},
    )

    assert usage_pricing._pricing_source_order() == ("openrouter", "models_dev")
    assert set(usage_pricing._pricing_source_order()) == set(
        usage_pricing._VALID_PRICING_SOURCES
    )


def test_documented_pricing_config_path_is_registered_and_writable(
    tmp_path, monkeypatch
):
    from hermes_cli import config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert config._validate_config_key("pricing.external_source") == (True, None)

    config.set_config_value("pricing.external_source", "openrouter")

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["pricing"]["external_source"] == "openrouter"


def test_has_known_pricing_makes_no_outbound_connection(monkeypatch):
    import socket

    monkeypatch.setattr(model_metadata, "_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_model_metadata_cache_time", 0)
    monkeypatch.setattr(model_metadata, "_load_model_metadata_disk_cache", dict)
    monkeypatch.setattr(
        model_metadata,
        "_model_metadata_disk_cache_age_seconds",
        lambda: None,
    )
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_load_disk_cache", dict)

    attempts = []

    def refuse(self, address):
        attempts.append(address)
        raise AssertionError(f"has_known_pricing opened a connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)

    for model, provider in [
        ("glm-5", None),
        ("gpt-4o", "openai"),
        ("some-model", "openrouter"),
        ("another", "anthropic"),
    ]:
        usage_pricing.has_known_pricing(model, provider)

    assert attempts == []


def test_has_known_pricing_uses_warm_models_dev_cache(
    seeded_models_dev, default_source_order
):
    seeded_models_dev(
        {
            "anthropic": {
                "models": {
                    "claude-cached-1": {"cost": {"input": 3, "output": 15}},
                }
            }
        }
    )

    assert usage_pricing.has_known_pricing("claude-cached-1", "anthropic") is True
