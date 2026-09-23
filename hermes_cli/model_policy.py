"""Shared worker-model policy for Kanban creation and dispatch.

Main's configurable flagship ban (``kanban.banned_worker_model_substrings``)
is the ONE predicate.  The batch/lane routing helpers further down layer on
top of it (alias canonicalisation, route classification, audit text) and never
define a second ban list.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional


FLAGSHIP_MODEL_SUBSTRINGS = ("fable", "astra")
FLAGSHIP_OVERRIDE_COMMENT_PREFIX = "flagship override:"
FLAGSHIP_REFUSAL_COMMENT_PREFIX = "flagship dispatch refused:"


def banned_worker_model_substrings(
    config: Optional[Mapping[str, Any]] = None,
) -> tuple[str, ...]:
    """Return the configured case-insensitive worker-model ban list."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            loaded = load_config()
            config = loaded if isinstance(loaded, Mapping) else {}
        except Exception:
            config = {}
    kanban = config.get("kanban", {}) if isinstance(config, Mapping) else {}
    configured = (
        kanban.get("banned_worker_model_substrings")
        if isinstance(kanban, Mapping)
        else None
    )
    if configured is None:
        return FLAGSHIP_MODEL_SUBSTRINGS
    if not isinstance(configured, (list, tuple)):
        return FLAGSHIP_MODEL_SUBSTRINGS
    normalized = tuple(
        str(item).strip().casefold()
        for item in configured
        if str(item).strip()
    )
    return normalized or FLAGSHIP_MODEL_SUBSTRINGS


def flagship_model_match(
    model: Optional[str],
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return the banned substring matched by ``model``, if any."""
    folded = (model or "").casefold()
    if not folded:
        return None
    return next(
        (part for part in banned_worker_model_substrings(config) if part in folded),
        None,
    )


def flagship_model_error(model: str) -> str:
    return (
        f"flagship model '{model}' is orchestrator-only (Ace 2026-09-17). "
        "Workers: claude-opus-5/claude-apr, or openai-codex/gpt-5.6-sol "
        "when Claude is capped. Override with --allow-flagship \"<reason>\" "
        "(logged)."
    )


def validate_worker_model(
    model: Optional[str],
    *,
    allow_flagship_reason: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Validate a worker model and return a normalized override reason."""
    if not flagship_model_match(model, config):
        return None
    reason = (allow_flagship_reason or "").strip()
    if not reason:
        raise ValueError(flagship_model_error(str(model)))
    return reason


def override_comment(reason: str) -> str:
    return f"{FLAGSHIP_OVERRIDE_COMMENT_PREFIX} {reason.strip()}"


# ---------------------------------------------------------------------------
# Batch / lane routing helpers (set-model --where, lane-model).  These consume
# ``flagship_model_match`` above; they add alias resolution so a config alias
# that expands to a flagship id cannot slip past the substring check.
# ---------------------------------------------------------------------------


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


def is_firepower_model(
    model: Optional[str],
    config: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return whether *model* (raw or alias-resolved) hits the flagship ban."""

    if not str(model or "").strip():
        return False
    if flagship_model_match(model, config):
        return True
    canonical_model, _ = canonical_model_pair(model)
    return bool(flagship_model_match(canonical_model, config))


def route_kind(route: Optional[str]) -> str:
    """Classify a route for dispatcher announcements."""

    return "firepower" if is_firepower_model(route) else "standard"


def firepower_guard_error(
    model: Optional[str],
    reason: Optional[str],
    *,
    reason_field: str = "--firepower",
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return an actionable refusal message, or ``None`` when allowed."""

    if not is_firepower_model(model, config):
        return None
    if str(reason or "").strip():
        return None
    message = flagship_model_error(str(model))
    if reason_field != "--allow-flagship":
        message += f" ({reason_field} is accepted as an alias.)"
    return message


def format_firepower_audit(
    model: str,
    provider: Optional[str],
    reason: str,
) -> str:
    """Human-readable audit LOG line naming the route (delegate_task, lane-model).

    Kanban card comments use :func:`override_comment` instead, byte-identical
    to main's create/set-model writers, which is what the dispatcher's
    ``flagship override:`` gate authorizes on.
    """

    canonical_model, canonical_provider = canonical_model_pair(model, provider)
    route = (
        f"{canonical_provider}/{canonical_model}"
        if canonical_provider
        else canonical_model
    )
    return override_comment(f"route={route}; reason={reason.strip()}")
