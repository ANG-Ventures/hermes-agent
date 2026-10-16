"""Preserved thinking on relays: the per-provider ``preserve_thinking`` flag.

``agent/anthropic_message_convert._manage_thinking_signatures`` strips every signed thinking block
for any endpoint that is not anthropic.com, because signatures are proprietary and a compatible
endpoint 400s on them. A relay in FRONT of Anthropic (the fleet's ``127.0.0.1:18810`` apr/alr
pool) validates the same signatures, so there the strip only throws the reasoning away: measured
2026-10-16 (plans/preserved-thinking-prefix-stability), 328 signed blocks across 10 sessions,
every one stripped before replay.

``providers.<name>.preserve_thinking: true`` (config.yaml, default false) treats that provider as
first-party for the strip decision ONLY. Everything else about a third-party endpoint (auth
style, beta set, fast mode) is untouched. With the flag on, the request also carries
``thinking.block_binding.prefix_mismatch_behavior: drop_block`` and the
``thinking-binding-controls-2026-08-01`` beta, so a history edit the relay or harness makes costs
one dropped block (reported in the response's ``input_transformations``) instead of a 400. The
mode is never ``error``: the harness still has known prefix edits (steer re-render, compaction,
resume notices) and ``agent/turn_recovery.py``'s thinking_signature 400 recovery stays the backstop.

``providers.<name>.preserve_thinking_window`` (``latest`` default | ``contiguous``) widens the
replay from the latest assistant turn to every assistant turn; off by default, measured before it
is widened.

The provider name is resolved where the agent knows it (``chat_completion_helpers``,
``ContextCompressor``) and threaded down as booleans, so the pure converter stays
provider-agnostic and the config read happens once per request, not per message.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PREFIX_MISMATCH_BEHAVIOR = "drop_block"
BINDING_CONTROLS_BETA = "thinking-binding-controls-2026-08-01"
WINDOW_LATEST = "latest"
WINDOW_CONTIGUOUS = "contiguous"
_WINDOWS = frozenset({WINDOW_LATEST, WINDOW_CONTIGUOUS})
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _load_providers_config() -> Dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    providers = config.get("providers") if isinstance(config, dict) else None
    return providers if isinstance(providers, dict) else {}


def _provider_entry(provider: Optional[str]) -> Dict[str, Any]:
    if not provider:
        return {}
    try:
        entry = _load_providers_config().get(provider)
    except Exception:  # noqa: BLE001 — an unreadable config is flag-off, never a dead turn
        logger.debug("preserve_thinking: providers config unreadable; treating as off", exc_info=True)
        return {}
    return entry if isinstance(entry, dict) else {}


def preserve_thinking_enabled(provider: Optional[str]) -> bool:
    """``providers.<provider>.preserve_thinking is True``; anything else (missing, truthy string) is off."""
    return _provider_entry(provider).get("preserve_thinking") is True


def preserve_thinking_window(provider: Optional[str]) -> str:
    """``latest`` unless the flag is on AND ``preserve_thinking_window`` is a known value."""
    entry = _provider_entry(provider)
    if entry.get("preserve_thinking") is not True:
        return WINDOW_LATEST
    window = entry.get("preserve_thinking_window")
    return window if isinstance(window, str) and window in _WINDOWS else WINDOW_LATEST


def strip_thinking_at_boundary(message: Dict[str, Any]) -> Dict[str, Any]:
    """Drop thinking blocks from a compaction-kept assistant row's replay channels.

    A compaction rewrites ``messages[0]`` and drops the middle, so every kept turn's thinking is
    signed against a prefix that no longer exists; the API would ``drop_block`` each one anyway.
    Stripping here is deterministic (a contiguous front drop) and keeps the drop out of the
    per-turn ``input_transformations`` log. ``message`` is the compaction COPY; the lists are
    replaced, never mutated, so the live transcript row keeps its blocks.
    """
    for key in ("reasoning_details", "anthropic_content_blocks"):
        blocks = message.get(key)
        if not isinstance(blocks, list):
            continue
        kept = [b for b in blocks if not (isinstance(b, dict) and str(b.get("type", "")).lower() in _THINKING_TYPES)]
        if len(kept) != len(blocks):
            message[key] = kept
    return message


def _count_replayed_thinking(api_kwargs: Dict[str, Any]) -> int:
    count = 0
    for m in api_kwargs.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if m.get("role") == "assistant" and isinstance(content, list):
            count += sum(1 for b in content if isinstance(b, dict) and b.get("type") in _THINKING_TYPES)
    return count


def log_input_transformations(message: Any, api_kwargs: Dict[str, Any], *, log_prefix: str = "") -> Optional[Dict[str, Any]]:
    """INFO line per response naming what the API did to the replayed thinking.

    ``input_transformations`` is present only when the binding-controls beta reached the API, so
    its absence is silent (DEBUG): on an unflagged lane there is nothing to report. Returns the
    summary dict for tests/telemetry, None when the key is absent.
    """
    transformations = getattr(message, "input_transformations", None)
    if transformations is None and isinstance(message, dict):
        transformations = message.get("input_transformations")
    if transformations is None:
        logger.debug("%sinput_transformations absent on response (binding-controls beta not on this wire)", log_prefix)
        return None
    entries = [t for t in (transformations or []) if isinstance(t, dict)]
    reasons: Dict[str, int] = {}
    for t in entries:
        reason = str(t.get("reason") or t.get("type") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    summary = {"replayed_thinking_blocks": _count_replayed_thinking(api_kwargs), "dropped": len(entries), "reasons": reasons}
    logger.info(
        "%sinput_transformations: replayed=%d dropped=%d reasons=%s paths=%s",
        log_prefix, summary["replayed_thinking_blocks"], summary["dropped"],
        reasons or "{}", [t.get("path") for t in entries if t.get("path")] or "[]",
    )
    return summary
