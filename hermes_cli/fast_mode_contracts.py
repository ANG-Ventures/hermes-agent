"""Exact, dated wire contracts for provider fast-mode features.

This module is deliberately dependency-free so both the model resolver and
transport adapters can consume the same immutable catalog without creating an
import cycle.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional


# Snapshot of https://openai.com/api-priority-processing/ on 2026-07-12.
# This is the source contract, not the Hermes picker inventory. The shipped
# openai_priority contract below is its exact intersection with the current
# ``openai-api`` provider catalog.
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


def _contract(*, source_url: str, models: tuple[str, ...]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "source_url": source_url,
            "checked_date": "2026-07-12",
            "models": models,
        }
    )


FAST_MODE_CAPABILITY_CATALOG: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "openai_priority": _contract(
            source_url="https://openai.com/api-priority-processing/",
            models=(
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
                "gpt-5.4-mini",
                "gpt-5.4",
                "gpt-5-mini",
                "gpt-4.1",
                "gpt-4o",
                "gpt-4o-mini",
            ),
        ),
        "codex_fast": _contract(
            source_url="https://developers.openai.com/codex/speed",
            models=("gpt-5.5", "gpt-5.4"),
        ),
        # Opus 4.7 is intentionally absent. Anthropic deprecated its Fast
        # contract on 2026-06-25 and will reject speed=fast after 2026-07-24.
        "anthropic_fast": _contract(
            source_url=(
                "https://platform.claude.com/docs/en/build-with-claude/fast-mode"
            ),
            models=("claude-opus-4-8",),
        ),
    }
)


# OpenAI Ultrafast (``service_tier: "ultrafast"``, Responses API only). Published
# per model, not per family: GPT-6 Astra is GA in the API and on the Codex
# backend (Pro 500 / Enterprise) as of 2026-09-29; GPT-6.1 Sol is "coming soon".
# Kept separate from FAST_MODE_CAPABILITY_CATALOG so the Priority/Fast contracts
# (and their dated snapshot tests) stay untouched.
ULTRAFAST_CAPABILITY_CONTRACT: Mapping[str, Any] = MappingProxyType(
    {
        "source_url": "https://developers.openai.com/api/docs/guides/ultrafast-mode",
        "checked_date": "2026-09-29",
        "models": ("gpt-6-astra",),
    }
)

#: ``agent.service_tier`` values sent on every request of a session.
STATIC_SERVICE_TIERS = frozenset({"priority", "ultrafast"})
#: Config/slash words that mean "no tier".
NORMAL_SERVICE_TIER_WORDS = frozenset({"", "normal", "default", "standard", "off", "none"})
#: Config/slash word -> ``agent.service_tier``. The single table every config
#: loader (CLI, gateway, TUI) parses through, so a new tier is one edit.
SERVICE_TIER_WORDS: Mapping[str, str] = MappingProxyType(
    {
        "fast": "priority",
        "priority": "priority",
        "on": "priority",
        "ultrafast": "ultrafast",
    }
)


def parse_service_tier(raw: Any) -> Optional[str]:
    """``agent.service_tier`` for a config/slash word; None for normal AND for unknown words."""
    value = str(raw or "").strip().lower()
    if value in NORMAL_SERVICE_TIER_WORDS:
        return None
    return SERVICE_TIER_WORDS.get(value)


def is_known_service_tier_word(raw: Any) -> bool:
    """True when *raw* is a recognized tier word (including the normal words)."""
    value = str(raw or "").strip().lower()
    return value in NORMAL_SERVICE_TIER_WORDS or value in SERVICE_TIER_WORDS


def service_tier_word(tier: Any) -> str:
    """User-facing word for a stored tier: ``priority`` -> ``fast``, None -> ``normal``."""
    if not tier:
        return "normal"
    return "fast" if tier == "priority" else str(tier)


# Request word -> the tier name a response reports when that tier served it.
# OpenAI answers a ``fast``/``priority`` request with ``service_tier: "priority"``.
_SERVED_TIER_FOR_REQUEST: Mapping[str, str] = MappingProxyType(
    {"fast": "priority", "priority": "priority", "ultrafast": "ultrafast"}
)


# Providers whose response ``service_tier`` echo does NOT reflect the served
# tier. Measured 2026-09-29 on chatgpt.com/backend-api/codex, gpt-6-astra,
# reasoning low: response.completed reports ``default`` for ultrafast, priority
# and no-tier requests alike, while ultrafast ran ~3x faster (4.7 s vs 13.8 s).
# The value is still recorded; it just cannot prove a downgrade there.
SERVED_TIER_ECHO_UNRELIABLE_PROVIDERS = frozenset({"openai-codex"})


def service_tier_downgraded(requested: Any, served: Any) -> bool:
    """True when a response reports a different tier than the static one requested.

    OpenAI echoes the tier that actually served the request; ``default`` for an
    Ultrafast request means the ramp limiter silently served (and billed) it at
    Standard. Unknown/absent values on either side are not judged.
    """
    expected = _SERVED_TIER_FOR_REQUEST.get(str(requested or "").strip().lower())
    served_norm = str(served or "").strip().lower() if isinstance(served, str) else ""
    if not expected or not served_norm:
        return False
    return served_norm != expected


def ultrafast_contract_accepts(model_id: Optional[str]) -> bool:
    """Return whether *model_id* is documented for OpenAI Ultrafast."""
    return normalize_fast_model_id(model_id) in ULTRAFAST_CAPABILITY_CONTRACT["models"]


_FAST_MODEL_ID_PREFIXES: tuple[str, ...] = (
    "anthropic/",
    "openai/",
    "openai-codex/",
    "openai-api/",
)


def normalize_fast_model_id(model_id: Optional[str]) -> str:
    """Normalize only documented spelling aliases; retain all other suffixes."""
    normalized = str(model_id or "").strip().lower()
    # Vendor prefixes (OpenRouter spelling) and Hermes provider-id prefixes
    # (``-m openai-codex/gpt-6-astra``): the provider route is gated
    # separately, so the contract lookup keys on the bare model id.
    if normalized.startswith(_FAST_MODEL_ID_PREFIXES):
        normalized = normalized.split("/", 1)[1]
    if normalized == "claude-opus-4.8":
        return "claude-opus-4-8"
    return normalized


def anthropic_fast_contract_accepts(model_id: Optional[str]) -> bool:
    """Return whether *model_id* exactly matches the native Anthropic contract."""
    return normalize_fast_model_id(model_id) in FAST_MODE_CAPABILITY_CATALOG[
        "anthropic_fast"
    ]["models"]
