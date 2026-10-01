"""CLIProxyAPI (cpa) turns: lane ``cpa``, real vendor/provider, priced estimated (t_d59c7936).

The ``cpa`` provider plugin (aliases kimi-code, kimi-cpa, cliproxyapi) fronts one
local proxy serving Kimi, Codex and Grok subscriptions. The blackbox lane is
``cpa``; the vendor and upstream provider come from the SERVED model id through
the one model->vendor map (agent.usage_pricing._infer_vendor_from_model). No
provider maps to ``"other"``.
"""
import os
import sqlite3

import pytest

from agent.usage_pricing import (
    CanonicalUsage,
    NOTIONAL_PROXY_PROVIDERS,
    UNKNOWN_VENDOR,
    _PROXY_VENDOR_PRICING_LANE,
    _infer_vendor_from_model,
    attribute_route,
    estimate_usage_cost,
    get_pricing_entry,
    is_known_model,
)
from plugins import blackbox
from plugins.blackbox import store
from plugins.blackbox.last_turn import render_last_turn_record
from plugins.blackbox.record import TurnRecord

_HOME_ENV = "HERMES_HOME"
_PROXIES = ("cpa", "kimi-code", "kimi-cpa", "cliproxyapi", "custom:cpa", "CPA")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv(_HOME_ENV, str(tmp_path))
    monkeypatch.setattr(blackbox, "_config", lambda: {"enabled": True})
    with store._connect():
        pass
    return store._db_path()


def test_home_env_name_is_the_real_one():
    # Guard against a display-layer rewrite of the literal above.
    assert _HOME_ENV == "HERMES" + "_HOME"


@pytest.mark.parametrize("provider", _PROXIES)
@pytest.mark.parametrize("model,vendor,served", [
    ("kimi-k3", "moonshotai", "kimi"),
    ("k3", "moonshotai", "kimi"),
    ("k3-256k", "moonshotai", "kimi"),
    ("gpt-5.5", "openai", "openai-codex"),
    ("codex-mini", "openai", "openai-codex"),
    ("grok-4.6", "xai", "xai"),
    ("gemini-3.8-flash-high", "google", "google"),
])
def test_proxy_lane_attribution(provider, model, vendor, served):
    assert store.lane_family(provider) == "cpa"
    route = attribute_route(provider, model)
    assert route["vendor"] == vendor
    assert route["served_provider"] == served


def test_unknown_proxy_model_is_named_vendor_unknown_not_other():
    route = attribute_route("cpa", "frobnicate-9")
    assert route == {"vendor": UNKNOWN_VENDOR, "vendor_label": UNKNOWN_VENDOR,
                     "served_provider": UNKNOWN_VENDOR}
    assert "other" not in route.values()
    assert store.lane_family("cpa") == "cpa"


def test_kimi_vendor_label():
    assert attribute_route("cpa", "kimi-k3")["vendor_label"] == "Kimi (Moonshot)"


def test_direct_lane_keeps_its_recorded_provider():
    route = attribute_route("openai-codex", "gpt-5.5")
    assert route["vendor"] == "openai" and route["served_provider"] == "openai-codex"


@pytest.mark.parametrize("provider", _PROXIES)
def test_proxy_kimi_turn_prices_estimated_at_k3_snapshot(provider):
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100, cache_read_tokens=500)
    result = estimate_usage_cost("kimi-k3", usage, provider=provider)
    assert result.status == "estimated"
    # $3/M in, $15/M out, $0.30/M cache read (openrouter-kimi-k3-2026-09-27).
    assert result.amount_usd is not None
    assert float(result.amount_usd) == pytest.approx(0.00465)
    native = estimate_usage_cost("k3", usage, provider="kimi-oauth")
    assert result.amount_usd == native.amount_usd


def test_proxy_grok_turn_prices_like_xai_oauth():
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100)
    via_proxy = estimate_usage_cost("grok-4.6", usage, provider="cpa")
    native = estimate_usage_cost("grok-4.6", usage, provider="xai-oauth")
    assert via_proxy.status == "estimated" and via_proxy.amount_usd is not None
    assert via_proxy.amount_usd == native.amount_usd


def test_proxy_unknown_model_stays_unpriced():
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100)
    assert estimate_usage_cost("frobnicate-9", usage, provider="cpa").amount_usd is None


@pytest.mark.parametrize("provider", _PROXIES)
@pytest.mark.parametrize("model", ["claude-opus-4-5", "claude-sonnet-4-6"])
def test_proxy_vendor_without_pricing_lane_stays_unpriced(provider, model):
    # The served vendor is recognised and has a direct-lane rate, but no proxy
    # pricing lane: the vendor fallback must not borrow that rate.
    assert _infer_vendor_from_model(model) not in _PROXY_VENDOR_PRICING_LANE
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100)
    result = estimate_usage_cost(model, usage, provider=provider)
    assert result.amount_usd is None and result.status == "unknown"
    assert get_pricing_entry(model, provider=provider) is None
    assert is_known_model(model, provider=provider) is False
    # The same model is priced on its own vendor's route.
    assert get_pricing_entry(model, provider=_infer_vendor_from_model(model)) is not None


def test_every_registered_provider_maps_to_a_named_family():
    from providers import list_providers

    names = set(NOTIONAL_PROXY_PROVIDERS)
    for profile in list_providers():
        names.add(profile.name)
        names.update(getattr(profile, "aliases", ()) or ())
    assert len(names) > len(NOTIONAL_PROXY_PROVIDERS)
    bad = {n: store.lane_family(n) for n in names
           if store.lane_family(n) in ("other", "")}
    assert bad == {}
    assert store.lane_family("") == "unknown"


def test_cpa_turn_rows_carry_lane_vendor_provider(db):
    usage = CanonicalUsage(input_tokens=1_000, output_tokens=100)
    store.insert_api_call("t_cpa", 0, ts=1, provider="cpa", model="kimi-k3",
                          usage=usage, sub_key=None, attribution="wire",
                          http_status=200)
    store.insert_turn(TurnRecord(turn_id="t_cpa", chat_id="c", ts_start=1, ts_end=2,
                                 provider="cpa", model="kimi-k3"))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT lane_family, vendor, served_provider FROM turns WHERE turn_id='t_cpa'"
        ).fetchone() == ("cpa", "moonshotai", "kimi")
        assert conn.execute(
            "SELECT lane_family, vendor, served_provider FROM turn_api_calls "
            "WHERE turn_id='t_cpa'"
        ).fetchone() == ("cpa", "moonshotai", "kimi")


def test_backfill_retires_stored_other(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO turn_api_calls (turn_id, seq, provider, lane_family) "
                     "VALUES ('old', 0, 'kimi-code', 'other')")
    store.backfill_cache_monitoring()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT lane_family FROM turn_api_calls "
                            "WHERE turn_id='old'").fetchone()[0] == "cpa"


def test_last_turn_card_names_vendor_provider_lane():
    lines = render_last_turn_record({"provider": "cpa", "model": "kimi-k3", "profile": "p"})
    assert "• Route: vendor Kimi (Moonshot) · provider kimi · lane cpa" in lines
    plain = render_last_turn_record({"provider": "openai-codex", "model": "gpt-5.5",
                                     "profile": "p"})
    assert not any(line.startswith("• Route:") for line in plain)


# ── Gemini via the Antigravity lane (t_673e956a) ─────────────────────────────
# A cpa Gemini turn prices exactly as the gemini-bridge lane prices the same id
# (both resolve to the Google snapshot rows). Ids are the live cpa Antigravity
# catalog (Studio :18812, 2026-10-01) plus the registry aliases of
# ANG-Ventures/CLIProxyAPI@684e390c and the agy tier spellings.
_CPA_GEMINI_IDS = [
    "gemini-3.8-flash",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-3.7-flash-high",
    "gemini-3.6-flash-high",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash-low",
    "gemini-3.5-flash-extra-low",
    "gemini-3.1-pro-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-flash-lite",
    "gemini-pro-agent",
    "gemini-3-flash",
    "gemini-3-flash-agent",
    "gemini-4-argon",
    "gemini-4-argon-medium",
]
_USAGE = CanonicalUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=200_000)


@pytest.mark.parametrize("provider", _PROXIES)
@pytest.mark.parametrize("model", _CPA_GEMINI_IDS)
def test_proxy_gemini_prices_at_gemini_bridge_parity(provider, model):
    via_proxy = estimate_usage_cost(model, _USAGE, provider=provider)
    bridge = estimate_usage_cost(model, _USAGE, provider="gemini-bridge")
    assert via_proxy.status == "estimated", (provider, model)
    assert via_proxy.amount_usd is not None and via_proxy.amount_usd > 0
    assert via_proxy.amount_usd == bridge.amount_usd
    assert is_known_model(model, provider=provider) is True


@pytest.mark.parametrize("alias,vendor_id", [
    ("gemini-pro-agent", "gemini-3.1-pro"),
    ("gemini-3-flash", "gemini-3-flash-preview"),
    ("gemini-3-flash-agent", "gemini-3.5-flash"),
    ("gemini-3.5-flash-extra-low", "gemini-3.5-flash"),
    ("gemini-3.8-flash-medium", "gemini-3.8-flash"),
])
def test_proxy_gemini_alias_resolves_to_the_vendor_row(alias, vendor_id):
    vendor_row = get_pricing_entry(vendor_id, provider="google")
    assert vendor_row is not None
    assert get_pricing_entry(alias, provider="cpa") == vendor_row


def test_proxy_gemini_flash_medium_matches_bridge_estimate():
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    got = estimate_usage_cost("gemini-3.8-flash-medium", usage, provider="cpa")
    want = estimate_usage_cost("gemini-3.8-flash-medium", usage, provider="gemini-bridge")
    assert got.status == want.status == "estimated"
    assert got.amount_usd == want.amount_usd


@pytest.mark.parametrize("model", ["gemini-3.1-flash-image", "gemini-9-nonexistent", "frobnicate-9"])
def test_proxy_unpriced_gemini_and_unknown_vendor_ids_stay_unpriced(model):
    result = estimate_usage_cost(model, _USAGE, provider="cpa")
    assert result.amount_usd is None and result.status == "unknown"


@pytest.mark.parametrize("model", ["gemini-3.8-flash-high", "gemini-pro-agent", "gemini-3-flash"])
def test_proxy_gemini_context_is_googles_window(model, tmp_path, monkeypatch):
    from agent.model_metadata import get_model_context_length

    monkeypatch.setenv(_HOME_ENV, str(tmp_path))
    assert get_model_context_length(model, provider="cpa") == 1_048_576
    assert get_model_context_length("gemini-4-argon-high", provider="cpa") == 1_000_000
