"""GPT-6.1 Sol joins the Codex ``-900k`` opt-in (2026-09-30).

Measured 2026-09-30 by request bisection against
chatgpt.com/backend-api/codex/responses: 921,028 input tokens accepted,
921,998 rejected with ``context_length_exceeded`` -> 922,000 hard input
ceiling. The variant grants 900,000 (>=11K margin, same rule as gpt-5.6).

Behaviour contracts:
- ``openai-codex/gpt-6.1-sol-900k`` strips to the wire slug ``gpt-6.1-sol``
  and resolves to a 900,000 window;
- bare ``gpt-6.1-sol`` keeps the advertised 272,000 (opt-in policy);
- no prefix leak: ``gpt-6.1-sol-pro`` / ``-fast`` are not eligible;
- picker surfaces ``gpt-6.1-sol-900k`` right after its base;
- the ``-900k`` alias prices exactly like the base.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.model_metadata import (
    _verified_codex_ctx_for_slug,
    get_model_context_length,
    is_codex_900k_base,
    is_codex_context_variant,
    strip_codex_context_variant_suffix,
)
from agent.usage_pricing import get_pricing_entry


def _codex_ctx(model: str) -> int:
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.json.return_value = {}
    import agent.model_metadata as mm

    mm._codex_oauth_context_cache = {}
    with patch("agent.model_metadata.requests.get", return_value=fake_response), \
         patch("agent.model_metadata.get_cached_context_length", return_value=None), \
         patch("agent.model_metadata.save_context_length"):
        return get_model_context_length(
            model=model,
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="expired-token",
            provider="openai-codex",
        )


def test_gpt61_sol_900k_strips_to_wire_slug():
    assert is_codex_900k_base("gpt-6.1-sol") is True
    assert is_codex_context_variant("openai-codex/gpt-6.1-sol-900k") is True
    assert strip_codex_context_variant_suffix("gpt-6.1-sol-900k") == "gpt-6.1-sol"
    assert (
        strip_codex_context_variant_suffix("openai-codex/gpt-6.1-sol-900k")
        == "openai-codex/gpt-6.1-sol"
    )


def test_gpt61_sol_900k_resolves_to_900k_window():
    assert _verified_codex_ctx_for_slug("openai-codex/gpt-6.1-sol-900k") == 900_000
    assert _codex_ctx("gpt-6.1-sol-900k") == 900_000
    # Measured hard input ceiling is 922,000; the grant stays under it.
    assert _verified_codex_ctx_for_slug("gpt-6.1-sol-900k") < 922_000


def test_bare_gpt61_sol_keeps_advertised_272k():
    assert _verified_codex_ctx_for_slug("gpt-6.1-sol") is None
    assert _codex_ctx("gpt-6.1-sol") == 272_000


@pytest.mark.parametrize("slug", ["gpt-6.1-sol-pro", "gpt-6.1-sol-fast", "gpt-6.1", "gpt-6.1-luna"])
def test_no_prefix_leak(slug):
    assert is_codex_900k_base(slug) is False
    assert is_codex_context_variant(f"{slug}-900k") is False
    assert strip_codex_context_variant_suffix(f"{slug}-900k") == f"{slug}-900k"


def test_picker_surfaces_gpt61_sol_900k():
    from hermes_cli.codex_models import (
        DEFAULT_CODEX_MODELS,
        _finalize_codex_models,
        get_codex_model_ids,
    )

    assert "gpt-6.1-sol" in DEFAULT_CODEX_MODELS
    ids = get_codex_model_ids()  # offline curated path
    assert ids.index("gpt-6.1-sol-900k") == ids.index("gpt-6.1-sol") + 1
    # A live catalog that only lists gpt-6-sol (true on 2026-09-30) still
    # surfaces 6.1 via forward-compat.
    out = _finalize_codex_models(["gpt-6-sol"])
    assert "gpt-6.1-sol" in out and "gpt-6.1-sol-900k" in out


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
def test_gpt61_sol_900k_prices_like_base(provider):
    base = get_pricing_entry("gpt-6.1-sol", provider=provider)
    variant = get_pricing_entry("gpt-6.1-sol-900k", provider=provider)
    assert base is not None and variant is not None
    assert variant == base
