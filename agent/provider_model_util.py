"""Neutral provider/model string utility shared across surfaces.

Lives in a low-level module that BOTH ``gateway/runtime_footer.py`` and
``agent/conversation_compression.py`` can import without creating a
gateway↔agent circular import (gateway already imports agent).
"""

from __future__ import annotations

from typing import Optional, Tuple


def split_provider_model(
    provider: Optional[str], model: Optional[str]
) -> Tuple[str, str]:
    """Resolve a clean ``(provider, model)`` pair for display.

    The served ``provider`` is authoritative whenever it is supplied — it is
    the endpoint that actually handled (and billed) the turn. A ``/`` inside
    the model id is NOT assumed to be a provider prefix, because aggregator
    providers (OpenRouter, Nous, Yunwu, ...) namespace their model ids as
    ``vendor/model`` — ``openrouter`` + ``moonshotai/kimi-k3`` must render as
    ``openrouter/moonshotai/kimi-k3``, not silently drop the provider and
    show ``moonshotai/kimi-k3`` as if Moonshot were serving it.

    Three cases:

    * provider set, model prefix == provider (``claude-app`` +
      ``claude-app/claude-opus-4-8``; the live config once had
      ``model.default: claude-app/...`` AND ``model.provider: claude-app``) →
      de-duped ``("claude-app", "claude-opus-4-8")``, never a
      ``claude-app/claude-app/...`` triple.
    * provider set, model prefix differs → the slash is part of the model id;
      kept whole: ``("openrouter", "moonshotai/kimi-k3")``.
    * provider unset, model carries ``a/b`` → legacy split so the footer reads
      ``a/b`` instead of an ``unset/a/b`` placeholder (mirrors the
      blackbox-inspect ``/context`` fallback).
    """
    prov = (provider or "").strip()
    mdl = (model or "").strip()
    if "/" not in mdl:
        return prov, mdl
    prefix, _, rest = mdl.partition("/")
    if not prov:
        # No served provider known — the embedded prefix is the best we have.
        return prefix, rest
    if rest and prefix.strip().lower() == prov.lower():
        # Redundant self-prefix — de-dupe rather than emit provider/provider/model.
        return prov, rest
    # Aggregator vendor namespace (openrouter → moonshotai/kimi-k3): the slash
    # belongs to the model id; the served provider stays in front of it.
    return prov, mdl


def format_provider_model(provider: Optional[str], model: Optional[str]) -> str:
    """Render ``provider/model`` (or bare ``model`` when no provider), de-duped."""
    prov, mdl = split_provider_model(provider, model)
    if prov and mdl:
        return f"{prov}/{mdl}"
    return mdl
