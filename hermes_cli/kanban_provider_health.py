"""Opt-in, bounded pre-spawn probes. Only confirmed exhaustion defers work."""
from __future__ import annotations

import json
import logging
import re
import urllib.request


_log = logging.getLogger(__name__)

# Providers served by the claude subscription pools. A worker on one of these
# cannot run while ITS pool has no capacity: spawning it is a guaranteed 429
# that still pays for the worker boot and skill/card/repo load (measured
# 2026-09-22: 3,706 rate_limited runs in 7d, 42% lived >2 min).
_RELAY_RE = re.compile(r"^claude-(apr|bpr)$", re.IGNORECASE)
_PINNED_RE = re.compile(r"^claude-(apx|bpx)-(\d+)$", re.IGNORECASE)

# Each family has its OWN relay and its own eligible_count (Argus r1, PR #953:
# gating every family on :18810 asked the apr relay about bpr and pinned-box
# workers, ~80% of the measured storm). apr -> plugins/model-providers/
# claude-apr _DEFAULT_APP_URL; bpr -> claude-bpr _DEFAULT_BPP_URL.
DEFAULT_POOL_HEALTH_URLS: dict[str, str] = {
    "claude-apr": "http://127.0.0.1:18810/health",
    "claude-bpr": "http://127.0.0.1:18811/health",
}
_PINNED_FAMILY_RELAY = {"apx": "claude-apr", "bpx": "claude-bpr"}
# Relay /health lists that name a sub with no quota left. ``capped_transport``
# / ``unreachable`` are deliberately excluded: they are not rate-limit evidence.
_SUB_EXHAUSTED_FIELDS = ("exhausted", "capped_quota")
_PROBE_TIMEOUT_SECONDS = 1  # bounded: probes run inside the dispatch tick

# (url, scope) -> "capped" | "ok" | "unreachable". Logged on TRANSITION only: a
# 60s dispatcher logging every held spawn would emit ~1,440 identical lines/day.
_PROBE_STATE: dict[tuple, str] = {}


def pool_route(provider) -> tuple[str, str | None] | None:
    """Which capacity signal serves ``provider``: ``(relay_family, sub)``.

    ``claude-apr``/``claude-bpr`` -> ``(family, None)``: the relay aggregate.
    ``claude-apx-N``/``claude-bpx-N`` talk DIRECTLY to one sub box, so the
    relay aggregate says nothing about them -> ``(apx->claude-apr |
    bpx->claude-bpr, "local" if N == 0 else "sub-vps-N")``; the sub key mirrors
    ``plugins/model-providers/_registry_route.py`` ``registry_key_for`` (INV-5).
    Anything else -> None (not pool-bound).
    """
    if not isinstance(provider, str):
        return None
    p = provider.strip()
    m = _RELAY_RE.match(p)
    if m:
        return f"claude-{m.group(1).lower()}", None
    m = _PINNED_RE.match(p)
    if m:
        n = int(m.group(2))
        return _PINNED_FAMILY_RELAY[m.group(1).lower()], ("local" if n == 0 else f"sub-vps-{n}")
    return None


def pool_key(provider) -> str | None:
    """Circuit identity: the relay family, or the single sub a pinned lane hits.

    ``claude-apx-7`` and ``claude-bpx-7`` share ``sub-vps-7`` -> one key.
    """
    route = pool_route(provider)
    if route is None:
        return None
    return route[1] or route[0]


def configured_pool_health_urls() -> dict[str, str]:
    """``kanban.pool_health_urls`` merged over the per-family defaults.

    A partial map overrides only the families it names; ``""`` disables that
    family's implicit probe. A non-dict value falls back to the defaults.
    """
    from hermes_cli.config import load_config
    urls = dict(DEFAULT_POOL_HEALTH_URLS)
    try:
        value = load_config().get("kanban", {}).get("pool_health_urls")
    except Exception:
        return urls
    if isinstance(value, dict):
        for family, url in value.items():
            if isinstance(family, str) and isinstance(url, str):
                urls[family.strip().lower()] = url.strip()
    return urls


def _note_probe_state(url: str, provider, state: str, detail: str = "", scope=None) -> None:
    if _PROBE_STATE.get((url, scope)) == state:
        return
    _PROBE_STATE[(url, scope)] = state
    if state == "capped":
        _log.warning("kanban provider health: holding %s spawns — %s reports no capacity (%s)",
                     provider, url, detail)
    elif state == "unreachable":
        _log.warning("kanban provider health: %s unreachable for %s (%s) — failing OPEN",
                     url, provider, detail)
    else:
        _log.info("kanban provider health: %s admitting %s spawns again", url, provider)


def model_override(task) -> tuple[str | None, str | None]:
    """The same model/provider pair used for admission and worker argv."""
    if not task.model_override:
        return None, None
    if task.provider_override:
        return task.model_override, task.provider_override
    provider, separator, model = task.model_override.partition("/")
    if separator and model and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", provider):
        return model, provider
    return task.model_override, None


def configured_probes() -> dict:
    from hermes_cli.config import load_config

    try:
        probes = load_config().get("kanban", {}).get("provider_health_probes", {})
        return probes if isinstance(probes, dict) else {}
    except Exception:
        return {}


def configured_min_eligible() -> int:
    from hermes_cli.config import load_config
    try:
        value = int(load_config().get("kanban", {}).get("provider_health_min_eligible", 1))
        return max(1, value)
    except (TypeError, ValueError, AttributeError):
        return 1


def effective_provider(task) -> str | None:
    _, provider = model_override(task)
    if provider:
        return provider
    from hermes_cli.config import load_config
    from hermes_cli.profiles import resolve_profile_env
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    try:
        home = resolve_profile_env(task.assignee)
        token = set_hermes_home_override(home)
        try:
            model = load_config().get("model", {})
            return model.get("provider") if isinstance(model, dict) else None
        finally:
            reset_hermes_home_override(token)
    except Exception:
        return None


def _fetch(url: str, cache: dict, provider):
    """Parsed ``/health`` JSON for ``url`` (once per tick), or None on failure."""
    if url not in cache:
        cache[url] = None
        try:
            with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read(65536))
            cache[url] = data if isinstance(data, dict) else None
            if cache[url] is None:
                _note_probe_state(url, provider, "unreachable", "non-object body")
        except Exception as exc:
            # A failed/unknown probe is not evidence of a capped account.
            _note_probe_state(url, provider, "unreachable", f"{type(exc).__name__}: {exc}")
    return cache[url]


def _reset_at(data: dict):
    reset_at = data.get("reset_at", data.get("soonest_exhausted_reset"))
    return reset_at if type(reset_at) in (int, float, str) else None


def capped_provider(
    task, probes: dict, cache: dict, *, min_eligible: int = 1,
    pool_urls: dict | None = None,
) -> dict | None:
    if not probes and not pool_urls:
        return None
    provider = effective_provider(task)
    url = (probes or {}).get(provider)
    sub = None
    if url is None and pool_urls:
        # No explicit probe: ask the pool that actually SERVES this provider.
        route = pool_route(provider)
        if route is not None:
            url = pool_urls.get(route[0])
            sub = route[1]
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    data = _fetch(url, cache, provider)
    if data is None:
        return None
    if sub is not None:
        # Pinned single-box lane: only positive evidence that THIS sub is out
        # of quota holds it. Unlisted (unknown to the relay) admits.
        listed = [f for f in _SUB_EXHAUSTED_FIELDS
                  if isinstance(data.get(f), list) and sub in data[f]]
        if listed:
            _note_probe_state(url, provider, "capped", f"{sub} in {'/'.join(listed)}", scope=sub)
            return {"reason": "provider_capped", "provider": provider, "sub": sub,
                    "reset_at": _reset_at(data)}
        _note_probe_state(url, provider, "ok", scope=sub)
        return None
    if (
        (type(data.get("eligible_count")) in (int, float)
         and 0 <= data["eligible_count"] < min_eligible)
        or data.get("status") == "all_capped"
    ):
        _note_probe_state(url, provider, "capped",
                          f"eligible={data.get('eligible_count')} < {min_eligible}")
        return {"reason": "provider_capped", "provider": provider, "reset_at": data.get("reset_at")
                if type(data.get("reset_at")) in (int, float, str) else None}
    _note_probe_state(url, provider, "ok")
    return None


def available_profile_fallback(
    task, probes: dict, cache: dict, *, min_eligible: int = 1,
    pool_urls: dict | None = None, skip_pools=frozenset(),
) -> tuple[str, str] | None:
    """Pick a healthy configured profile rung without changing the task row.

    Each rung is judged on ITS OWN serving pool (``capped_provider``), and a
    rung whose ``pool_key`` is in ``skip_pools`` (an open rate-limit circuit)
    is never chosen.
    """
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.profiles import resolve_profile_env
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    try:
        token = set_hermes_home_override(resolve_profile_env(task.assignee))
        try:
            chain = get_fallback_chain(load_config())
        finally:
            reset_hermes_home_override(token)
    except Exception:
        return None
    from types import SimpleNamespace
    for rung in chain:
        provider, model = rung.get("provider"), rung.get("model")
        if not isinstance(provider, str) or not isinstance(model, str) or not provider or not model:
            continue
        if provider == effective_provider(task):
            continue
        if skip_pools and pool_key(provider) in skip_pools:
            continue
        candidate = SimpleNamespace(
            model_override=model, provider_override=provider, assignee=task.assignee,
        )
        if capped_provider(
            candidate, probes, cache, min_eligible=min_eligible, pool_urls=pool_urls,
        ) is None:
            return model, provider
    return None
