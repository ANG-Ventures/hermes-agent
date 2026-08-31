"""Fork canary: cron refuses provably cross-vendor model/provider pairs (#641).

Surface: the ``cronjob`` tool admission gate (``tools/cronjob_tools.py``),
reached from the CLI (``hermes cron create``), the ``/cron`` slash command, and
the agent-callable ``cronjob`` tool.

Fork feature (fork/main de200ebbf5, PR #641): a job pinned to a model and a
provider from different vendors — e.g. ``model='gpt-5.6-sol'`` with
``provider='claude-apr'`` — used to be accepted, persisted, and then fail EVERY
fire with an HTTP 400 that no retry can fix. Rule #20b promotes the post-hoc
cron-config-lint check into a create/update-time admission gate.

The design property that matters most on a parity merge is the FAIL-OPEN half:
the vendor maps are prefix-based and deliberately incomplete, so an unknown
model or provider name must yield vendor ``None`` and NOT be flagged. A merge
that "improves" the check into a fail-closed catalog would start refusing every
legitimate job on a model the map has never heard of. Both halves are asserted.
"""

import pytest


def _mismatch(model, provider):
    from tools.cronjob_tools import model_provider_vendor_mismatch

    return model_provider_vendor_mismatch(model, provider)


# --------------------------------------------------------------------------- #
# Fail-closed half: provable cross-vendor pairs are refused
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "model,provider,model_vendor,provider_vendor",
    [
        # The exact pair that burned real scheduled jobs on 2026-08-19/08-29.
        ("gpt-5.6-sol", "claude-apr", "openai", "anthropic"),
        ("claude-opus-4-8", "openai-codex", "anthropic", "openai"),
        ("gemini-3-flash", "anthropic", "google", "anthropic"),
        ("grok-4", "deepseek", "xai", "deepseek"),
        ("kimi-k2", "google", "moonshot", "google"),
    ],
)
def test_cross_vendor_pair_is_reported(model, provider, model_vendor, provider_vendor):
    """RED-PROVABLE: in tools/cronjob_tools.py, change the final condition of
    ``model_provider_vendor_mismatch`` (~L664) from
    ``if model_vendor and provider_vendor and model_vendor != provider_vendor:``
    to ``return None`` — every case here fails."""
    result = _mismatch(model, provider)
    assert result == (model_vendor, provider_vendor), (
        f"cron accepted the cross-vendor pair {model!r} + {provider!r}; "
        f"every fire of such a job dies on an unretryable HTTP 400."
    )


def test_vendor_inference_is_case_insensitive():
    """Stored jobs carry whatever casing the user typed.

    RED-PROVABLE: delete ``.lower()`` from ``_vendor_of`` in
    tools/cronjob_tools.py (~L650)."""
    assert _mismatch("GPT-5.6-Sol", "Claude-APR") == ("openai", "anthropic")


# --------------------------------------------------------------------------- #
# Fail-open half: unknown names must NOT be flagged
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "model,provider",
    [
        ("some-unlisted-model", "claude-apr"),   # unknown model side
        ("gpt-5.6-sol", "my-custom-relay"),      # unknown provider side
        ("mystery-a", "mystery-b"),              # both unknown
        ("", "claude-apr"),                      # empty model
        (None, "claude-apr"),                    # missing model
        ("gpt-5.6-sol", None),                   # missing provider
    ],
)
def test_unknown_names_are_not_flagged(model, provider):
    """The maps are conservative on purpose — this rule catches obvious
    mis-pairs, it does not maintain a model catalog. Fail-closing here would
    refuse every job on a model the prefix map has not been taught.

    RED-PROVABLE: in ``_vendor_of`` (tools/cronjob_tools.py ~L649) change the
    fallthrough ``return None`` to ``return "unknown"`` — the unknown-vs-known
    cases immediately start reporting a bogus mismatch."""
    assert _mismatch(model, provider) is None, (
        f"cron wrongly flagged {model!r} + {provider!r}; unknown names must "
        f"fail OPEN or legitimate jobs on uncatalogued models get refused."
    )


def test_same_vendor_pairs_pass():
    """RED-PROVABLE: invert the comparison in
    ``model_provider_vendor_mismatch`` (tools/cronjob_tools.py ~L664) to
    ``model_vendor == provider_vendor``."""
    for model, provider in (
        ("claude-opus-4-8", "claude-apr"),
        ("claude-sonnet-4", "anthropic"),
        ("gpt-5.6-sol", "openai-codex"),
        ("gemini-3-flash", "google"),
    ):
        assert _mismatch(model, provider) is None, (
            f"same-vendor pair {model!r} + {provider!r} was wrongly refused"
        )


# --------------------------------------------------------------------------- #
# The admission gate wiring, not just the predicate
# --------------------------------------------------------------------------- #

def test_admission_gate_renders_a_blocking_error_for_the_object_model_shape():
    """Stored jobs carry model either as a plain name or as the
    ``{"model": ..., "provider": ...}`` object shape; the gate must check the
    inner model against BOTH the inner and the effective top-level provider,
    since either can be the one that actually routes the request.

    RED-PROVABLE: in ``_model_provider_vendor_error`` (tools/cronjob_tools.py
    ~L669) drop ``model.get("provider")`` from ``candidate_providers`` so only
    the top-level provider is considered — the inner-provider case returns
    None and this test fails."""
    from tools.cronjob_tools import _model_provider_vendor_error

    # Mismatch carried on the INNER provider only.
    err = _model_provider_vendor_error(
        {"model": "gpt-5.6-sol", "provider": "claude-apr"}, None
    )
    assert err, "object-shaped model with a cross-vendor inner provider was not blocked"
    assert isinstance(err, str) and err.strip(), "gate returned a non-actionable error"

    # Mismatch carried on the EFFECTIVE top-level provider only.
    err_top = _model_provider_vendor_error({"model": "gpt-5.6-sol"}, "claude-apr")
    assert err_top, "object-shaped model with a cross-vendor top-level provider was not blocked"

    # Consistent pair on both sides must stay allowed (fail-open discipline).
    assert _model_provider_vendor_error(
        {"model": "claude-opus-4-8", "provider": "claude-apr"}, "claude-apr"
    ) is None
