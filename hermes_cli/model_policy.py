"""Shared worker-model policy for Kanban creation and dispatch."""

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
