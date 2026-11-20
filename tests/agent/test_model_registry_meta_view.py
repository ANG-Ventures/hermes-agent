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
    _meta(home, {KNOWN: {"context": 123_456, "pricing": {"input": 9, "output": 99}}})
    assert get_model_context_length(KNOWN, provider="cpa") == 123_456
    assert get_pricing_entry(KNOWN, provider="cpa").output_cost_per_million == Decimal("99")
    assert get_model_context_length(f"cpa/{KNOWN}", provider="cpa") == 123_456


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


@pytest.mark.parametrize("ctx", [0, -5, True, 1.5, "100"])
def test_malformed_context_is_no_row(home, ctx):
    _meta(home, {FRESH: {"context": ctx}})
    assert registry_context_length(FRESH) is None
