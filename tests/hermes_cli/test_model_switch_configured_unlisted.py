"""/model on a configured model the live listing omits must not read as a typo.

2026-09-29: ``/model k3`` (alias k3 -> cpa/kimi-k3) during a kimi 5-hour cap
printed "Model `kimi-k3` was not found in this provider's model listing.
Similar models: kimi-k2, ...". CPA hides a model from /v1/models while every
account behind it is quota-exhausted, so the model was real.
"""

from unittest.mock import patch

import hermes_cli.model_switch as ms
from hermes_cli.model_switch import DirectAlias
from hermes_cli.models import validate_requested_model

LISTING = ["kimi-k2", "kimi-k2.6", "kimi-k2.8"]


def _validate(model):
    probe = {
        "models": LISTING,
        "probed_url": "http://127.0.0.1:18812/v1/models",
        "resolved_base_url": "http://127.0.0.1:18812/v1",
        "suggested_base_url": None,
        "used_fallback": False,
    }
    with patch("hermes_cli.models.fetch_api_models", return_value=LISTING), \
         patch("hermes_cli.models.probe_api_models", return_value=probe):
        return validate_requested_model(
            model, "cpa", base_url="http://127.0.0.1:18812/v1", api_key="k"
        )


def test_listing_rejection_is_flagged_not_listed():
    v = _validate("kimi-k3")
    assert v["accepted"] is False
    assert v.get("not_listed") is True
    # The exact 2026-09-29 text: the generic path still offers suggestions.
    assert "Similar models" in v["message"]


def test_configured_alias_gets_unavailable_message(monkeypatch):
    monkeypatch.setattr(
        ms, "_load_direct_aliases",
        lambda: ({"k3": DirectAlias("kimi-k3", "cpa", "")}, True),
    )
    msg = ms._configured_but_unlisted_message("kimi-k3", "cpa", "cpa")
    assert msg is not None
    assert "alias `k3`" in msg
    assert "quota" in msg
    assert "not found" not in msg
    assert "Similar models" not in msg


def test_unconfigured_model_keeps_generic_message(monkeypatch):
    monkeypatch.setattr(
        ms, "_load_direct_aliases",
        lambda: ({"k3": DirectAlias("kimi-k3", "cpa", "")}, True),
    )
    # Typo'd id, or the right id on a different provider: no evidence it exists.
    assert ms._configured_but_unlisted_message("kimi-k33", "cpa") is None
    assert ms._configured_but_unlisted_message("kimi-k3", "openrouter") is None


def test_switch_model_surfaces_unavailable_message(monkeypatch):
    """End to end through switch_model's rejection branch."""
    monkeypatch.setattr(
        ms, "_load_direct_aliases",
        lambda: ({"k3": DirectAlias("kimi-k3", "cpa", "")}, True),
    )
    rejected = {
        "accepted": False, "persist": False, "recognized": False,
        "not_listed": True,
        "message": "Model `kimi-k3` was not found in this provider's model listing."
                   "\n  Similar models: `kimi-k2`",
    }
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model", lambda *a, **k: rejected
    )
    res = ms.switch_model("k3", current_provider="cpa", current_model="claude-opus-5-5")
    assert res.success is False, res
    assert "Similar models" not in (res.error_message or ""), res.error_message
    assert "quota" in (res.error_message or ""), res.error_message
