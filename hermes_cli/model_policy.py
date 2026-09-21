"""Shared policy for explicit flagship-model overrides.

Flagship models remain available, but choosing one must be deliberate and
attributable.  The guard is intentionally small and shared by Kanban and
``delegate_task`` so one override surface cannot silently bypass the other.
"""

from __future__ import annotations

from typing import Optional


FIREPOWER_MODEL_SUBSTRINGS = ("gpt-6-astra", "claude-fable")


def canonical_model_pair(
    model: Optional[str], provider: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """Resolve config aliases before any policy, persistence, or audit decision."""

    if not model:
        return model, provider
    try:
        from hermes_cli.model_switch import resolve_model_pair_for_storage

        return resolve_model_pair_for_storage(model, provider)
    except Exception:
        return model, provider


def is_firepower_model(model: Optional[str]) -> bool:
    """Return whether *model* resolves to a flagship/firepower-only family."""

    canonical_model, _ = canonical_model_pair(model)
    normalized = str(canonical_model or "").strip().lower()
    return bool(normalized) and any(
        banned in normalized for banned in FIREPOWER_MODEL_SUBSTRINGS
    )


def route_kind(route: Optional[str]) -> str:
    """Classify a route for dispatcher announcements."""

    return "firepower" if is_firepower_model(route) else "standard"


def firepower_guard_error(
    model: Optional[str],
    reason: Optional[str],
    *,
    reason_field: str = "--firepower",
) -> Optional[str]:
    """Return an actionable refusal message, or ``None`` when allowed."""

    if not is_firepower_model(model):
        return None
    if str(reason or "").strip():
        return None
    return (
        f"model {model!r} is firepower-only; provide {reason_field} "
        '"<reason>" to make this explicit and auditable'
    )


def format_firepower_audit(
    model: str,
    provider: Optional[str],
    reason: str,
) -> str:
    """Stable human-readable audit comment/log payload."""

    canonical_model, canonical_provider = canonical_model_pair(model, provider)
    route = (
        f"{canonical_provider}/{canonical_model}"
        if canonical_provider
        else canonical_model
    )
    return f"firepower override: route={route}; reason={reason.strip()}"
