"""t_cf4c6032 (2026-11-20 04:58 PT): ``hermes kanban create --model sol --provider cpa`` was
refused with "not on the lane's model list [cpa/kimi-k3, cpa/kimi-k3-256k, cpa/kimi-k2.7-code,
cpa/kimi-k2.7-code-highspeed]".

That list is the cpa plugin's ``fallback_models`` offline snapshot. When the proxy's
``/v1/models`` probe fails, :func:`hermes_cli.models.provider_model_ids` serves it as a
``CuratedFallbackModels`` placeholder and the disk cache stores it as a ``fallback`` row (60 s
TTL). ``cached_provider_model_ids`` handed it back as a plain list, so the model registry took a
placeholder for the lane's real catalog and refused every id the snapshot does not name. A
placeholder is "the list could not be read": the registry must call that unverifiable (admit,
dispatcher re-checks every tick), and still refuse a made-up id once the live list is back.
"""
from __future__ import annotations

import time

import pytest

import hermes_cli.models as models
from hermes_cli import model_registry

SNAPSHOT = ["cpa/kimi-k3", "cpa/kimi-k3-256k", "cpa/kimi-k2.7-code", "cpa/kimi-k2.7-code-highspeed"]
LIVE = ["cpa/gpt-6.2-sol", "cpa/sol", "cpa/terra", "cpa/kimi-k3.1-pro", "cpa/grok"]


@pytest.fixture
def cpa_lane(monkeypatch):
    """The real catalog seam (no ``_fetch_catalog`` stub), with the disk cache in memory and
    ``sol`` a slot that resolves to ``gpt-6.2-sol``."""
    monkeypatch.setenv("HERMES_MODEL_REGISTRY_LIVE", "1")
    model_registry.clear_catalog_memo()
    cache: dict = {}
    monkeypatch.setattr(models, "_load_provider_models_cache", lambda: cache)
    monkeypatch.setattr(models, "_save_provider_models_cache", lambda *a, **k: None)
    monkeypatch.setattr(models, "_credential_fingerprint", lambda provider: "fp")
    monkeypatch.setattr(models, "_spawn_swr_refresh", lambda *a, **k: None)
    monkeypatch.setattr(model_registry, "slot_view", lambda: {"sol": "gpt-6.2-sol"})
    monkeypatch.setattr("hermes_cli.model_switch.resolve_model_pair_for_storage", lambda m, p: (m, p))
    yield cache
    model_registry.clear_catalog_memo()


def test_slot_on_proxy_lane_is_admitted_while_the_disk_row_is_a_placeholder(cpa_lane, monkeypatch):
    # The 04:58 state: a fresh fallback row (the probe failed seconds ago) on disk.
    cpa_lane["cpa"] = {"fp": "fp", "at": time.time() - 5, "models": list(SNAPSHOT), "fallback": True}
    monkeypatch.setattr(models, "provider_model_ids", lambda *a, **k: pytest.fail("fresh row must be served"))

    verdict = model_registry.require("cpa", "sol", flag="--force-model")

    assert verdict.ok and verdict.reason == "unverifiable"
    assert verdict.model == "gpt-6.2-sol"
    assert model_registry.invalid_pin_reason("cpa", "sol") is None


def test_live_probe_that_degrades_to_the_snapshot_is_unverifiable_and_not_memoised(cpa_lane, monkeypatch):
    served = [models.CuratedFallbackModels(SNAPSHOT)]
    monkeypatch.setattr(models, "provider_model_ids", lambda *a, **k: served[0])

    assert model_registry.resolve("cpa", "gpt-6.2-sol").reason == "unverifiable"
    assert cpa_lane["cpa"]["fallback"] is True

    # The proxy answers again: the placeholder was not pinned in the 10-minute memo, so the very
    # next check sees the live list and decides on it.
    served[0] = list(LIVE)
    cpa_lane.clear()
    assert model_registry.resolve("cpa", "gpt-6.2-sol").reason == "known"


def test_live_catalog_accepts_the_slot_and_still_refuses_a_made_up_id(cpa_lane, monkeypatch):
    monkeypatch.setattr(models, "provider_model_ids", lambda *a, **k: list(LIVE))

    assert model_registry.require("cpa", "sol").reason == "known"
    with pytest.raises(model_registry.UnknownModel) as exc:
        model_registry.require("cpa", "gpt-9-made-up")
    assert "gpt-9-made-up" in str(exc.value) and "cpa/gpt-6.2-sol" in str(exc.value)
    assert model_registry.invalid_pin_reason("cpa", "gpt-9-made-up") == (
        "model_override_invalid: gpt-9-made-up not on cpa")


def test_cached_provider_model_ids_keeps_the_placeholder_marker(cpa_lane, monkeypatch):
    """The picker still gets the snapshot rows; only the marker that says what they are survives."""
    cpa_lane["cpa"] = {"fp": "fp", "at": time.time() - 5, "models": list(SNAPSHOT), "fallback": True}
    out = models.cached_provider_model_ids("cpa")
    assert out == SNAPSHOT and isinstance(out, models.CuratedFallbackModels)

    cpa_lane.clear()
    monkeypatch.setattr(models, "provider_model_ids", lambda *a, **k: models.CuratedFallbackModels(SNAPSHOT))
    out = models.cached_provider_model_ids("cpa", force_refresh=True)
    assert out == SNAPSHOT and isinstance(out, models.CuratedFallbackModels)

    cpa_lane["cpa"] = {"fp": "fp", "at": time.time() - 5, "models": list(LIVE)}
    out = models.cached_provider_model_ids("cpa")
    assert out == LIVE and not isinstance(out, models.CuratedFallbackModels)


def test_parallel_picker_warmup_keeps_the_placeholder_marker(cpa_lane, monkeypatch):
    """Prism 8e3d509f0460: the picker warmup re-saves every non-empty result through the locked
    writer. That writer must keep the ``fallback`` flag, or the next pin check reads the snapshot as
    the lane's list again and refuses the slot."""
    from hermes_cli.model_switch_providers import _prefetch_provider_models_parallel

    monkeypatch.setattr(models, "provider_model_ids", lambda *a, **k: models.CuratedFallbackModels(SNAPSHOT))

    _prefetch_provider_models_parallel(["cpa"])

    assert cpa_lane["cpa"]["models"] == SNAPSHOT and cpa_lane["cpa"]["fallback"] is True
    assert model_registry.require("cpa", "gpt-6.2-sol").reason == "unverifiable"


def test_locked_writer_never_replaces_a_live_row_with_a_placeholder(cpa_lane):
    cpa_lane["cpa"] = {"fp": "fp", "at": time.time() - 5, "models": list(LIVE)}
    models.update_provider_cache_entry("cpa", models.CuratedFallbackModels(SNAPSHOT))
    assert cpa_lane["cpa"]["models"] == LIVE and not cpa_lane["cpa"].get("fallback")

    models.update_provider_cache_entry("cpa", list(LIVE[:2]))
    assert cpa_lane["cpa"]["models"] == LIVE[:2] and not cpa_lane["cpa"].get("fallback")
