"""Helpers for reading the effective fallback provider chain from config.

A chain is normally a hand-written list of ``{provider, model}`` rungs. That is
fine when the rungs are stable, but it rots badly when the rungs name pooled
credentials whose health changes on its own schedule: the list is a snapshot of
health taken on the day it was written, and nothing re-checks it. A chain whose
every rung has since become unusable fails in the worst possible way — the
primary errors, the chain is walked, every rung errors too, and the run dies
having "used its fallbacks", while usable routes sit outside the list.

So an entry may instead be **derived**: ``{source: <name>}`` names a resolver
registered via :func:`register_chain_source`, which is called at read time and
returns the rungs to splice in at that position. Core ships the mechanism and
no resolvers — deployments register their own from a plugin, so the policy
("prefer the least-utilised seat") lives with the thing that knows about
utilisation, and ``config.yaml`` stays declarative.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# name -> callable(entry) -> list of chain entries. Populated by plugins.
_CHAIN_SOURCES: dict[str, Callable[[dict[str, Any]], Any]] = {}

#: A derived source that expands to nothing leaves no trace in the chain, which
#: is indistinguishable from a healthy expansion in a config dump. Record the
#: most recent expansion so operators (and a lint) can tell the two apart.
_LAST_EXPANSION: dict[str, Any] = {}


def register_chain_source(name: str, resolver: Callable[[dict[str, Any]], Any]) -> None:
    """Register ``resolver`` as the expansion for ``{source: name}`` entries.

    ``resolver`` receives the raw entry dict (so it can read its own options,
    e.g. a model to pin on each generated rung) and returns an iterable of
    ``{provider, model}`` dicts. Re-registering a name replaces it, which keeps
    plugin reloads idempotent.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("chain source name must be a non-empty string")
    if not callable(resolver):
        raise TypeError("chain source resolver must be callable")
    _CHAIN_SOURCES[name] = resolver


def unregister_chain_source(name: str) -> None:
    """Remove a registered source. No-op when it was never registered."""
    _CHAIN_SOURCES.pop(str(name or "").strip(), None)


def registered_chain_sources() -> tuple[str, ...]:
    """Names currently registered, for diagnostics and `hermes fallback` output."""
    return tuple(sorted(_CHAIN_SOURCES))


def last_expansion_report() -> dict[str, Any]:
    """Per-source outcome of the most recent :func:`get_fallback_chain` call.

    Maps source name -> ``{"count": int, "error": str | None}``. An entry with
    ``count == 0`` is the case worth alerting on: the chain silently lost the
    rungs that source was supposed to provide.
    """
    return dict(_LAST_EXPANSION)


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def _expand_source(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand one ``{source: name}`` entry via its registered resolver.

    Never raises: a chain is the failure path, so a broken resolver must not be
    able to take down the request that is already failing over. A resolver that
    errors or returns nothing contributes no rungs, and the outcome is recorded
    in :func:`last_expansion_report` and logged at WARNING so the silence is
    attributable rather than invisible.
    """
    name = str(entry.get("source") or "").strip()
    resolver = _CHAIN_SOURCES.get(name)
    if resolver is None:
        _LAST_EXPANSION[name] = {"count": 0, "error": "no resolver registered"}
        logger.warning(
            "fallback chain source %r is not registered; contributing no rungs "
            "(registered: %s)",
            name,
            ", ".join(registered_chain_sources()) or "none",
        )
        return []
    try:
        produced = _iter_fallback_entries(list(resolver(dict(entry))))
    except Exception as exc:  # noqa: BLE001 - see docstring
        _LAST_EXPANSION[name] = {"count": 0, "error": f"{type(exc).__name__}: {exc}"}
        logger.warning("fallback chain source %r failed to resolve: %s", name, exc)
        return []
    _LAST_EXPANSION[name] = {"count": len(produced), "error": None}
    if not produced:
        logger.warning("fallback chain source %r resolved to zero rungs", name)
    return produced


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.

    An entry of the form ``{source: <name>}`` is expanded in place by its
    registered resolver (see :func:`register_chain_source`), so a derived block
    of rungs keeps the position the author gave it relative to the literal ones
    around it. De-duplication applies across both kinds, which means a literal
    rung written before a derived block wins and the derived copy is dropped
    rather than the route being tried twice.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    _LAST_EXPANSION.clear()

    def _append(entry: dict[str, Any]) -> None:
        identity = _entry_identity(entry)
        if identity in seen:
            return
        seen.add(identity)
        chain.append(entry)

    for key in ("fallback_providers", "fallback_model"):
        raw = config.get(key)
        candidates = [raw] if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        for candidate in candidates:
            if isinstance(candidate, dict) and str(candidate.get("source") or "").strip():
                for derived in _expand_source(candidate):
                    _append(derived)
                continue
            for entry in _iter_fallback_entries(candidate):
                _append(entry)

    return chain
