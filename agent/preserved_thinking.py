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

The flag is a LANE property (Ace 2026-10-17): a registered provider profile may carry
``preserve_thinking = True`` (and ``preserve_thinking_window``); the fleet pin factory sets it on
every a-family Anthropic-wire pin (alr/apr/apx-N). Resolution order, first decisive wins:

1. ``config``: an explicit ``providers.<name>.preserve_thinking`` key, either direction. A
   non-bool value is a decision too: off (fail closed), never a fall-through to the lane.
2. ``lane``: the registered profile (``providers.get_provider_profile``) for a route identity has
   ``preserve_thinking is True`` and ``api_mode == "anthropic_messages"``.
3. ``off``.

The first decision per route identity and source is logged once per process at INFO
(``preserve_thinking: route=<id> source=<config|lane|off> ...``), so agent.log names the layer.

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


def _match_identity(identity: str, providers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``providers:`` entry whose key, display name or ``custom:<name>`` slug is ``identity``."""
    from hermes_cli.providers import custom_provider_aliases

    norm = identity.strip().lower()
    entry = providers.get(identity)
    if isinstance(entry, dict):
        return entry
    for key, entry in providers.items():
        if isinstance(entry, dict) and norm in custom_provider_aliases(str(entry.get("name") or key), str(key)):
            return entry
    return None


def _match_base_url(base_url: str, providers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ONE ``providers:`` entry serving ``base_url``; two entries on one endpoint is ambiguous, so off."""
    target = str(base_url or "").strip().rstrip("/").lower()
    if not target:
        return None
    hits = [e for e in providers.values() if isinstance(e, dict)
            and str(e.get("base_url") or e.get("url") or e.get("api") or "").strip().rstrip("/").lower() == target]
    return hits[0] if len(hits) == 1 else None


def _provider_entry(provider: Optional[str], requested_provider: Optional[str] = None,
                    base_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve the agent's route to its ``providers:`` block (``{}`` = no block; None = unreadable).

    A named ``providers:`` relay does not keep its config key in ``agent.provider``: the runtime
    resolver returns ``provider="custom"`` with the key in ``requested_provider``, and other paths
    carry the canonical ``custom:<name>`` slug. Match every identity the route carries, then (bare
    ``custom`` only, e.g. the compressor, which holds no requested identity) the unique entry on
    ``base_url``.
    """
    # The explicit requested identity first: a route resolved as provider="custom" with the key in
    # requested_provider must not pick up a ``providers.custom`` block (Prism 3e5ff74fe83d). A bare
    # ``custom`` is a transport class, never a config key to match.
    identities = [str(i).strip() for i in (requested_provider, provider) if i and str(i).strip()]
    if not identities:
        return {}
    try:
        providers = _load_providers_config()
        entry = next(
            (e for e in (_match_identity(i, providers) for i in identities if i.lower() != "custom") if e is not None),
            None,
        )
        if entry is None and base_url and any(i.lower() == "custom" for i in identities):
            entry = _match_base_url(base_url, providers)
    except Exception:  # noqa: BLE001 — an unreadable config is flag-off, never a dead turn
        logger.debug("preserve_thinking: providers config unreadable; treating as off", exc_info=True)
        return None
    return entry if isinstance(entry, dict) else {}


def _route_identities(provider: Optional[str], requested_provider: Optional[str]) -> list:
    return [str(i).strip() for i in (requested_provider, provider) if i and str(i).strip()]


def _lane_profile(identities: list) -> Any:
    """The registered provider profile that declares ``preserve_thinking`` for this route, or None.

    Only an Anthropic-wire profile counts: a chat_completions face (bpx/bpr) drives a genuine CLI
    that holds its own history, so there is no signed block for the harness to strip or keep.
    """
    try:
        from providers import get_provider_profile
    except Exception:  # noqa: BLE001 — no registry is no lane, never a dead turn
        return None
    for ident in identities:
        name = ident[len("custom:"):] if ident.lower().startswith("custom:") else ident
        if not name or name.lower() == "custom":
            continue
        try:
            profile = get_provider_profile(name)
        except Exception:  # noqa: BLE001
            logger.debug("preserve_thinking: provider lookup failed for %s", name, exc_info=True)
            continue
        if profile is None or getattr(profile, "preserve_thinking", None) is None:
            continue
        return profile
    return None


_logged_sources: set = set()


def _log_source(route: str, source: str, enabled: bool, window: str) -> None:
    key = (route, source)
    if key in _logged_sources:
        return
    _logged_sources.add(key)
    logger.info("preserve_thinking: route=%s source=%s enabled=%s window=%s", route, source, enabled, window)


def resolve_preserve_thinking(provider: Optional[str], requested_provider: Optional[str] = None,
                              base_url: Optional[str] = None) -> tuple:
    """``(enabled, window, source)`` for a route; ``source`` is ``config`` | ``lane`` | ``off``."""
    identities = _route_identities(provider, requested_provider)
    entry = _provider_entry(provider, requested_provider, base_url)
    if entry is None:  # unreadable config: fail closed, never fall through to the lane
        enabled, window, source = False, WINDOW_LATEST, "off"
    elif "preserve_thinking" in entry:
        enabled = entry.get("preserve_thinking") is True
        window = _window(entry.get("preserve_thinking_window")) if enabled else WINDOW_LATEST
        source = "config"
    else:
        profile = _lane_profile(identities)
        enabled = (profile is not None and getattr(profile, "preserve_thinking", None) is True
                   and getattr(profile, "api_mode", None) == "anthropic_messages")
        if enabled:
            window = _window(entry.get("preserve_thinking_window")
                             or getattr(profile, "preserve_thinking_window", None))
            source = "lane"
        else:
            window, source = WINDOW_LATEST, "off"
    _log_source(identities[0] if identities else (base_url or "?"), source, enabled, window)
    return enabled, window, source


def _window(value: Any) -> str:
    return value if isinstance(value, str) and value in _WINDOWS else WINDOW_LATEST


def preserve_thinking_enabled(provider: Optional[str], requested_provider: Optional[str] = None,
                              base_url: Optional[str] = None) -> bool:
    """Config key if present (must be a real ``True``), else the lane default, else off."""
    return resolve_preserve_thinking(provider, requested_provider, base_url)[0]


def preserve_thinking_window(provider: Optional[str], requested_provider: Optional[str] = None,
                             base_url: Optional[str] = None) -> str:
    """``latest`` unless the flag is on AND the config or lane window is a known value."""
    return resolve_preserve_thinking(provider, requested_provider, base_url)[1]


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
