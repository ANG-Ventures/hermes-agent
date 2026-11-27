"""Non-secret per-task boomerang route policy for existing delegation records."""
from __future__ import annotations

import copy

from tools.async_delegation_store import _carries_credential, persistable_fallback_chain

_ROUTING_FILTERS = frozenset({"providers_allowed", "providers_ignored", "providers_order", "provider_sort",
                             "provider_require_parameters", "provider_data_collection"})


def _virtual_endpoint(provider, endpoint):
    if provider == "moa":
        return endpoint == "moa://local"
    from providers import get_provider_profile
    profile = get_provider_profile(provider)
    if profile is None or profile.auth_type != "external_process":
        return False
    from urllib.parse import urlsplit
    own = urlsplit(profile.base_url)
    selected = urlsplit(endpoint)
    return (own.scheme in ("acp", "acp+tcp") and selected.scheme == own.scheme
            and bool(selected.hostname) and not selected.query and not selected.fragment)


def _validate_policy(route):
    """Validate before credential lookup; empty is resolved-disabled, None is not."""
    chain = route.get("fallback_chain")
    if (_carries_credential(route) or not isinstance(chain, list)
            or not isinstance(route.get("request_overrides", {}), dict)
            or not isinstance(route.get("routing_filters", {}), dict)
            or set(route.get("routing_filters", {})) - _ROUTING_FILTERS):
        raise ValueError("boomerang routing-unavailable: invalid persisted policy")
    for rung in chain:
        if (not isinstance(rung, dict)
                or not isinstance(rung.get("provider"), str) or not rung["provider"].strip()
                or not isinstance(rung.get("model"), str) or not rung["model"].strip()):
            raise ValueError("boomerang routing-unavailable: invalid persisted policy")
    for item in [route, *chain]:
        endpoint = item.get("base_url")
        if endpoint is not None:
            from urllib.parse import urlsplit
            try:
                parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
                valid = parsed and parsed.hostname and not parsed.fragment and (
                    parsed.scheme in ("http", "https") or _virtual_endpoint(item.get("provider"), endpoint))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("boomerang routing-unavailable: invalid persisted policy endpoint")



def persist_route(agent):
    captured = getattr(agent, "_boomerang_parent_route", None)
    route = copy.deepcopy(captured) if isinstance(captured, dict) else {"source": "parent", "model": agent.model,
             "provider": getattr(agent, "requested_provider", None) or agent.provider,
             "api_mode": agent.api_mode,
             "base_url": agent.base_url,
             "fallback_chain": copy.deepcopy(agent._fallback_chain),
             "request_overrides": copy.deepcopy(getattr(agent, "request_overrides", {}) or {})}
    # Constructor normalization resolves symbolic/legacy policy. Never persist its input
    # (for example "auto") as though it were an already selected list of lanes.
    effective_chain = getattr(agent, "_fallback_chain", None)
    original_chain = effective_chain if isinstance(effective_chain, list) else route["fallback_chain"] or []
    route["fallback_chain"] = persistable_fallback_chain(original_chain) or []
    if len(route["fallback_chain"]) != len(original_chain):
        raise ValueError("boomerang routing-unavailable: fallback credentials need symbolic references")
    # Headers may contain opaque secrets whose spelling no generic scrubber can recognize.
    overrides = route["request_overrides"]
    if overrides.get("extra_headers"):
        raise ValueError("boomerang routing-unavailable: request headers require live credential resolution")
    overrides.pop("extra_headers", None)
    if _carries_credential(route):
        raise ValueError("boomerang routing-unavailable: route policy contains credentials")
    _validate_policy(route)
    return route


def recover_route_parent(parent, route, *, credential_ref=None, delegation_cfg=None):
    from agent.route_snapshot import snapshot_parent
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider

    if (not isinstance(route, dict)
            or not isinstance(route.get("provider"), str) or not route["provider"].strip()
            or not isinstance(route.get("model"), str) or not route["model"].strip()):
        raise ValueError("boomerang routing-unavailable: incomplete persisted identity")
    _validate_policy(route)
    if (credential_ref or {}).get("source") == "delegation_config":
        from tools.delegate_tool_config import _resolve_delegation_credentials
        cfg = delegation_cfg or {}
        endpoint = cfg.get("base_url")
        if (not isinstance(endpoint, str) or not route.get("base_url")
                or endpoint.rstrip("/") != route["base_url"].rstrip("/")):
            raise ValueError("boomerang routing-unavailable: selected endpoint credentials could not be resolved")
        runtime = _resolve_delegation_credentials({**cfg, "model": route["model"]}, parent)
        # The current config owns this key only for its configured endpoint. Do not
        # borrow an arbitrary provider key, even when that provider has the same URL.
    else:
        runtime = _resolve_runtime_agent_kwargs_for_provider(route["provider"], target_model=route["model"])
    if not runtime.get("api_key") or not runtime.get("base_url"):
        raise ValueError("boomerang routing-unavailable: credentials could not be resolved")
    if route.get("base_url") and route["base_url"].rstrip("/") != runtime["base_url"].rstrip("/"):
        raise ValueError("boomerang routing-unavailable: selected endpoint credentials could not be resolved")
    view = snapshot_parent(parent)
    view.model = route["model"]
    view.provider = runtime["provider"]
    view.requested_provider = route["provider"]
    view.base_url = route.get("base_url") or runtime["base_url"]
    view.api_mode = route.get("api_mode") or runtime.get("api_mode")
    view.api_key = runtime["api_key"]
    view._client_kwargs = {"api_key": view.api_key, "base_url": view.base_url}
    view._fallback_chain = copy.deepcopy(route.get("fallback_chain") or [])
    view.request_overrides = copy.deepcopy(route.get("request_overrides") or {})
    for name, value in (route.get("routing_filters") or {}).items():
        setattr(view, name, copy.deepcopy(value))
    return view
