"""Every GPT-6-generation model the curated catalogs carry has an Ultrafast decision (t_1b32cc25).

Clanker moved to gpt-6.2-sol / gpt-6.2-astra on 2026-11-18 with ``agent.service_tier: ultrafast``
and ran untiered for a day because neither slug was in the Ultrafast allowlist. A new slug now has
to land in the contract or in ULTRAFAST_NOT_SUPPORTED (with a reason) before these tests pass.
"""

import pytest

from hermes_cli.codex_models import DEFAULT_CODEX_MODELS
from hermes_cli.fast_mode_contracts import ULTRAFAST_CAPABILITY_CONTRACT, ULTRAFAST_NOT_SUPPORTED
from hermes_cli.models import resolve_fast_mode_overrides
from hermes_cli.models_catalog_static import _OPENAI_ULTRAFAST_MODELS, _PROVIDER_MODELS


@pytest.mark.parametrize("model", ["gpt-6.2-sol", "gpt-6.2-astra", "openai-codex/gpt-6.2-sol", "gpt-6.2-astra-900k"])
@pytest.mark.parametrize("provider", [None, "openai-codex", "openai-api"])
def test_gpt_6_2_resolves_the_ultrafast_override(model, provider):
    assert resolve_fast_mode_overrides(model, provider=provider, tier="ultrafast") == {"service_tier": "ultrafast"}


def test_gpt_6_2_ultrafast_is_never_sent_to_a_proxy():
    assert resolve_fast_mode_overrides("gpt-6.2-sol", provider="openrouter", tier="ultrafast") is None


def test_the_two_ultrafast_lists_agree():
    assert set(ULTRAFAST_CAPABILITY_CONTRACT["models"]) == set(_OPENAI_ULTRAFAST_MODELS)


def _catalog_gpt6_models():
    carried = set(DEFAULT_CODEX_MODELS) | set(_PROVIDER_MODELS["openai-api"]) | set(_PROVIDER_MODELS["openai"])
    # gpt-6-astra / gpt-6.2-astra are live-discovery only on the Codex catalog; name them so the
    # decision is still checked.
    carried |= {"gpt-6-astra", "gpt-6.2-astra"}
    return sorted(m for m in carried if m.startswith("gpt-6"))


@pytest.mark.parametrize("model", _catalog_gpt6_models())
def test_every_gpt6_catalog_model_has_an_ultrafast_decision(model):
    supported = model in ULTRAFAST_CAPABILITY_CONTRACT["models"]
    declined = bool(str(ULTRAFAST_NOT_SUPPORTED.get(model, "")).strip())
    assert supported != declined, (
        f"{model}: add it to ULTRAFAST_CAPABILITY_CONTRACT (+ _OPENAI_ULTRAFAST_MODELS) or to "
        "ULTRAFAST_NOT_SUPPORTED with a reason, never both and never neither"
    )
