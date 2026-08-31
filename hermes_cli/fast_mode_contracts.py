"""Exact, dated wire contracts for provider fast-mode features.

``/fast`` maps onto several *different* vendor features that share one name in
the UI:

- OpenAI API **Priority Processing** — ``service_tier="priority"`` on the
  direct OpenAI API.
- **Codex Fast** — ``service_tier="fast"`` on the Codex Responses backend.
- Anthropic **Fast Mode** — ``speed="fast"`` plus the ``fast-mode-*`` beta
  header on the native Messages API.
- xAI **Priority Processing** — ``service_tier="priority"`` on Grok 4.6.

Each key is only valid on its own wire: ``speed`` is an Anthropic Messages
parameter, ``service_tier`` an OpenAI/xAI one. The *model id alone* therefore
cannot decide what a fast toggle may put on the request — the same
``claude-opus-4-6`` reached through an OpenAI-compatible proxy speaks a wire
that has no ``speed`` parameter at all.

This module holds the parts of that answer which are **exact, dated
allowlists**: the vendor contracts where sending the parameter to a
neighbouring model in the same family is a hard 4xx rather than a no-op. The
route table that maps ``(provider, api_mode)`` onto a family lives in
``hermes_cli.models`` next to the existing model gates.

It is deliberately dependency-free (no imports from ``hermes_cli`` or
``agent``) so both the model resolver and the transport adapters can consume
the same immutable catalog without creating an import cycle.

Refreshing a contract is a one-file edit: update ``models`` and bump
``checked_date``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional


# Snapshot of https://openai.com/api-priority-processing/ on 2026-07-12.
#
# Informational: the OpenAI Priority gate in ``hermes_cli.models`` is
# deliberately *pattern* based, because ``service_tier`` is silently ignored
# rather than rejected by endpoints that do not offer it, and a too-narrow
# list would hide the toggle from a newly released flagship. This snapshot is
# kept so that choice can be audited against what the vendor actually
# published on a known date.
OPENAI_PRIORITY_SOURCE_MODELS: tuple[str, ...] = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5.1-codex",
    "gpt-5-codex",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-2024-11-20",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-05-13",
    "gpt-4o-mini",
    "o3",
    "o4-mini",
)


def _contract(*, source_url: str, checked_date: str, models: tuple[str, ...]):
    return MappingProxyType(
        {
            "source_url": source_url,
            "checked_date": checked_date,
            "models": models,
        }
    )


#: Families gated by an exact, dated allowlist. ``models`` holds normalized ids
#: (see :func:`normalize_fast_model_id`) and is matched exactly — never by
#: prefix — because both vendors ship neighbouring ids in the same family that
#: reject the parameter outright.
FAST_MODE_CAPABILITY_CATALOG: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "codex_fast": _contract(
            source_url="https://developers.openai.com/codex/speed",
            checked_date="2026-07-12",
            models=("gpt-5.5", "gpt-5.4"),
        ),
        # Anthropic's Fast Mode contract is version-pinned: ``speed="fast"`` is
        # a hard 400 on every model not listed here (Opus 4.7 included), so
        # this must stay an allowlist rather than a ``claude-*`` prefix match.
        "anthropic_fast": _contract(
            source_url=(
                "https://platform.claude.com/docs/en/build-with-claude/fast-mode"
            ),
            checked_date="2026-07-12",
            models=("claude-opus-4-6",),
        ),
    }
)

#: Families billed above the standard tier. Every shipped family is a paid
#: upgrade today; the constant exists so surfaces can say so without
#: re-deriving the set, and so a future free tier is a one-line edit.
BILLED_AT_PREMIUM: frozenset[str] = frozenset(
    {"openai_priority", "codex_fast", "anthropic_fast", "xai_priority"}
)


def normalize_fast_model_id(model_id: Optional[str]) -> str:
    """Normalize documented spelling aliases only; retain all other suffixes.

    Vendor prefixes (``anthropic/``, ``openai/``) and the two documented
    dot/dash spellings of the Opus id are folded. Everything else — dated
    snapshots, ``:free`` suffixes, proxy-mangled ids — is left intact so it
    fails the exact-match gate rather than being coerced into a contract that
    never covered it.
    """
    normalized = str(model_id or "").strip().lower()
    if normalized.startswith(("anthropic/", "openai/")):
        normalized = normalized.split("/", 1)[1]
    if normalized == "claude-opus-4.6":
        return "claude-opus-4-6"
    return normalized


def anthropic_fast_contract_accepts(model_id: Optional[str]) -> bool:
    """Return whether *model_id* exactly matches the native Anthropic contract."""
    return (
        normalize_fast_model_id(model_id)
        in FAST_MODE_CAPABILITY_CATALOG["anthropic_fast"]["models"]
    )


def codex_fast_contract_accepts(model_id: Optional[str]) -> bool:
    """Return whether *model_id* exactly matches the Codex Fast contract."""
    return (
        normalize_fast_model_id(model_id)
        in FAST_MODE_CAPABILITY_CATALOG["codex_fast"]["models"]
    )
