"""Slot aliases P7 (t_efde9d1a): context window and price per id come from the registry view
(``<root>/config/model-meta.json``, rendered by hermes-home ``model-registry.py render``) first,
with the runtime's built-in tables as the fallback when the view is missing or has no row.

Before this change a fresh id the built-in tables did not list got the 256,000 unknown-model
fallback and no price, so a rollout needed a runtime release before the id was usable.
"""
import json
import os
from decimal import Decimal

import pytest

from agent.model_metadata import get_model_context_length
from agent.usage_pricing import get_pricing_entry
from hermes_cli.model_registry import registry_context_length, registry_pricing

FRESH = "gpt-9-sol"  # in no built-in table
KNOWN = "gpt-6.2-sol"  # in the built-in tables


def _write(root, name, body):
    path = root / "config" / name
    path.write_text(json.dumps(body), encoding="utf-8")
    st = path.stat()  # same-size rewrites inside one mtime tick must still be seen
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "vhome"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _meta(root, rows):
    _write(root, "model-meta.json", {"_generated": "test", "rendered_from": "x", "models": rows})


FRESH_ROW = {"context": 777_777, "pricing": {"input": 3, "output": "12.5", "cache_read": 0.3}}


def test_fresh_id_gets_registry_context_and_price(home):
    builtin_ctx = get_model_context_length(FRESH, provider="cpa")
    assert builtin_ctx != 777_777
    _meta(home, {FRESH: FRESH_ROW})
    assert get_model_context_length(FRESH, provider="cpa") == 777_777
    entry = get_pricing_entry(FRESH, provider="cpa")
    assert entry is not None and entry.pricing_version == "model-registry"
    assert (entry.input_cost_per_million, entry.output_cost_per_million) == (Decimal("3"), Decimal("12.5"))
    assert entry.cache_read_cost_per_million == Decimal("0.3")
    assert entry.cache_write_cost_per_million is None


def test_slot_name_reads_its_ids_row(home):
    _meta(home, {FRESH: FRESH_ROW})
    _write(home, "model-slots.json", {"slots": {"sol": FRESH}})
    assert get_model_context_length("sol", provider="cpa") == 777_777
    assert get_pricing_entry("sol", provider="cpa").input_cost_per_million == Decimal("3")


def test_registry_row_wins_over_builtin_table(home):
    _meta(home, {KNOWN: {"context": 123_456}, "claude-sonnet-5-5": {"pricing": {"input": 9, "output": 99}}})
    assert get_model_context_length(KNOWN, provider="cpa") == 123_456
    assert get_model_context_length(f"cpa/{KNOWN}", provider="cpa") == 123_456
    # a direct route: the registry row replaces the built-in snapshot price
    assert get_pricing_entry("claude-sonnet-5-5", provider="anthropic").output_cost_per_million == Decimal("99")


# Prism df9e5fd55d0d (Apollo 2026-11-20): on a metered relay route (provider openrouter: cpa,
# openai-codex, openrouter) the relay's configured tariff wins over the registry row; the row
# stands in for the built-in snapshot and prices the id when the relay lookup has nothing.
def _relay_tariff(monkeypatch, entry):
    import agent.usage_pricing as up
    monkeypatch.setattr(up, "_external_pricing_entry", lambda route: entry)


def test_relay_tariff_wins_over_registry_row(home, monkeypatch):
    from agent.usage_pricing import PricingEntry
    _relay_tariff(monkeypatch, PricingEntry(
        input_cost_per_million=Decimal("5"), output_cost_per_million=Decimal("15"),
        source="provider_models_api", pricing_version="openrouter-models-api"))
    _meta(home, {FRESH: {"pricing": {"input": 3, "output": "12.50"}}})
    entry = get_pricing_entry(FRESH, provider="openrouter")
    assert (entry.input_cost_per_million, entry.output_cost_per_million) == (Decimal("5"), Decimal("15"))


def test_relay_without_tariff_uses_registry_row(home, monkeypatch):
    _relay_tariff(monkeypatch, None)
    _meta(home, {FRESH: {"pricing": {"input": 3, "output": "12.50"}}})
    entry = get_pricing_entry(FRESH, provider="openrouter")
    assert entry.pricing_version == "model-registry"
    assert (entry.input_cost_per_million, entry.output_cost_per_million) == (Decimal("3"), Decimal("12.50"))


@pytest.mark.parametrize("view", ["missing", "no_row", "bad_rows"])
def test_builtin_tables_are_the_fallback(home, view):
    want_ctx = get_model_context_length(KNOWN, provider="cpa")
    want_price = get_pricing_entry(KNOWN, provider="cpa")
    assert want_price is not None and want_price.pricing_version != "model-registry"
    if view == "no_row":
        _meta(home, {FRESH: FRESH_ROW})
    elif view == "bad_rows":
        _meta(home, {KNOWN: {"context": "big", "pricing": {"input": 1}}})  # no output rate
    assert get_model_context_length(KNOWN, provider="cpa") == want_ctx
    assert get_pricing_entry(KNOWN, provider="cpa") == want_price


def test_explicit_config_context_still_wins(home):
    _meta(home, {FRESH: FRESH_ROW})
    assert get_model_context_length(FRESH, provider="cpa", config_context_length=50_000) == 50_000


@pytest.mark.parametrize("pricing", [
    {"input": -1, "output": 2}, {"input": True, "output": 2}, {"input": "x", "output": 2},
    {"input": 1, "output": 2, "cache_read": [1]}, "flat",
])
def test_malformed_pricing_is_no_row(home, pricing):
    _meta(home, {FRESH: {"pricing": pricing}})
    assert registry_pricing(FRESH) is None


@pytest.mark.parametrize("raw", [
    '{"input": Infinity, "output": 2}', '{"input": NaN, "output": 2}',
    '{"input": 1, "output": 1' + "0" * 400 + '}',  # a JSON integer float() cannot hold (OverflowError)
])
def test_non_finite_pricing_is_no_row_and_never_raises(home, raw):
    """Prism c-61c5c01d036c6b97: a pricing lookup must never raise or return 'inf'."""
    path = home / "config" / "model-meta.json"
    path.write_text('{"models": {"%s": {"pricing": %s}}}' % (FRESH, raw), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert registry_pricing(FRESH) is None


@pytest.mark.parametrize("ctx", [0, -5, True, 1.5, "100"])
def test_malformed_context_is_no_row(home, ctx):
    _meta(home, {FRESH: {"context": ctx}})
    assert registry_context_length(FRESH) is None


# Prism dbbdd3313f7f: a registry row is one per id (the API window). A lane that enforces its own,
# smaller window keeps it; the row only fills in when that lane's lookup has nothing.
BIG_ROW = {"context": 1_100_000}


def test_codex_oauth_lane_window_beats_registry_row(home):
    want = get_model_context_length(KNOWN, provider="openai-codex")
    assert want != 1_100_000
    _meta(home, {KNOWN: BIG_ROW})
    assert get_model_context_length(KNOWN, provider="openai-codex") == want


def test_codex_subscription_proxy_lane_window_beats_registry_row(home, monkeypatch):
    import agent.model_metadata as mm
    monkeypatch.setattr(mm, "provider_serves_codex_subscription", lambda p: p == "cpa")
    want = get_model_context_length(KNOWN, provider="cpa")
    assert want != 1_100_000
    _meta(home, {KNOWN: BIG_ROW})
    assert get_model_context_length(KNOWN, provider="cpa") == want


def test_copilot_lane_window_beats_registry_row(home, monkeypatch):
    import hermes_cli.models as models
    monkeypatch.setattr(models, "get_copilot_model_context", lambda model, api_key=None: 128_000)
    _meta(home, {KNOWN: BIG_ROW})
    assert get_model_context_length(KNOWN, provider="copilot") == 128_000


def test_lane_window_provider_uses_registry_row_when_lane_has_none(home):
    _meta(home, {FRESH: FRESH_ROW})
    assert get_model_context_length(FRESH, provider="openai-codex") == 777_777
