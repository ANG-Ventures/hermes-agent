"""Opt-in, bounded pre-spawn probes. Only confirmed exhaustion defers work."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path


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
# Box bridge /health bodies measured 8-15 KB (2026-09-24) and grow with their
# diagnostics; a truncated read would parse as garbage and fail open silently.
_MAX_HEALTH_BYTES = 1 << 20
# Box bridge ``usage_limits`` windows whose ``status: rejected`` is a certain
# 429 for every request to that sub until the window resets.
_BOX_LIMIT_WINDOWS = ("five_hour", "seven_day")

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


def configured_box_health() -> bool:
    """``kanban.pool_box_health`` (default True): judge pinned claude-apx/bpx-N
    lanes on their own sub box's bridge ``/health``. Non-bool -> default."""
    from hermes_cli.config import load_config
    try:
        value = load_config().get("kanban", {}).get("pool_box_health", True)
    except Exception:
        return True
    return value if isinstance(value, bool) else True


def _usage_registry_path() -> Path:
    """The usage registry the claude-apx/bpx provider plugins route through
    (``plugins/model-providers/_registry_route.py`` ``_registry_path``)."""
    override = os.environ.get("HERMES_USAGE_REGISTRY", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".hermes" / "config" / "usage-registry.json"


def box_health_url(sub: str) -> str | None:
    """``<bridge_route_base_url>/health`` of the box serving ``sub``, or None.

    apx-N and bpx-N reach the same subscription (one quota), so both lanes are
    judged on the one bridge that reports that sub's ``usage_limits``.
    """
    try:
        data = json.loads(_usage_registry_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    subs = data.get("subs") if isinstance(data, dict) else None
    for row in subs if isinstance(subs, list) else ():
        if isinstance(row, dict) and row.get("key") == sub:
            base = row.get("bridge_route_base_url")
            if isinstance(base, str) and base.strip().startswith(("http://", "https://")):
                return base.strip().rstrip("/") + "/health"
            return None
    return None


def _epoch(value) -> float | None:
    if type(value) in (int, float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _box_rejection(data: dict, now: float):
    """``(window, resets_at)`` for a CURRENT ``rejected`` usage window, else None.

    ``usage_limits`` is captured from the box's last upstream response. A held
    box gets no traffic, so a rejection whose ``resets_at`` has passed is
    stale -- treated as admit, or the gate would hold the box forever.
    """
    limits = data.get("usage_limits")
    if not isinstance(limits, dict):
        return None
    for window in _BOX_LIMIT_WINDOWS:
        w = limits.get(window)
        if not isinstance(w, dict) or w.get("status") != "rejected":
            continue
        resets = _epoch(w.get("resets_at"))
        if resets is not None and resets <= now:
            continue
        return window, w.get("resets_at")
    return None


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


def configured_pool_spawns_per_eligible() -> int:
    """Per-tick spawns per eligible sub; zero restores unlimited admission."""
    from hermes_cli.config import load_config
    try:
        value = load_config().get("kanban", {}).get("pool_spawns_per_eligible", 2)
        if type(value) is int and value >= 0:
            return value
    except (TypeError, AttributeError):
        pass
    return 2


def pool_budget_eligible(provider, probes: dict, cache: dict, pool_urls: dict,
                         *, box_health: bool = True):
    """Known capacity of the serving pool; None means unknown (fail open).

    Pinned routes spend one subscription, not the relay's aggregate count.
    Only a valid relay probe with a numeric eligible_count budgets pooled
    routes. Explicit provider probes take precedence as in capped_provider.
    """
    route = pool_route(provider)
    if route is None:
        return None
    if route[1] is not None:
        relay_url = (pool_urls or {}).get(route[0])
        relay = _fetch(relay_url, cache, provider) if _valid_url(relay_url) else None
        if relay is not None:
            return 1
        if box_health:
            box_url = box_health_url(route[1])
            if box_url is not None and _fetch(box_url, cache, provider) is not None:
                return 1
        return None  # Neither health signal was reachable: fail open.
    url = (probes or {}).get(provider)
    if url is None:
        url = (pool_urls or {}).get(route[0])
    if not _valid_url(url):
        return None
    data = _fetch(url, cache, provider)
    if data is None:
        return None
    eligible = data.get("eligible_count")
    if type(eligible) not in (int, float) or not (0 <= eligible < float("inf")):
        return None
    return int(eligible)


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
                data = json.loads(response.read(_MAX_HEALTH_BYTES))
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


def _valid_url(url) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _pinned_sub_capped(provider, sub: str, relay_url, cache: dict, box_health: bool):
    """Held only on positive evidence that THIS sub is out of quota.

    1. its family relay lists it ``exhausted`` / ``capped_quota``;
    2. else its own box bridge reports a current ``rejected`` usage window.
    The relays track only the subs they pool (live 2026-09-24: 82.8% of
    pinned-lane closes were on subs no relay lists), so (2) is what covers
    most pinned lanes (Argus r2 G2). Every probe failure fails OPEN.
    """
    data = _fetch(relay_url, cache, provider) if _valid_url(relay_url) else None
    if data is not None:
        listed = [f for f in _SUB_EXHAUSTED_FIELDS
                  if isinstance(data.get(f), list) and sub in data[f]]
        if listed:
            _note_probe_state(relay_url, provider, "capped",
                              f"{sub} in {'/'.join(listed)}", scope=sub)
            return {"reason": "provider_capped", "provider": provider, "sub": sub,
                    "reset_at": _reset_at(data)}
        _note_probe_state(relay_url, provider, "ok", scope=sub)
    if not box_health:
        return None
    box_url = box_health_url(sub)
    if box_url is None:
        return None
    box = _fetch(box_url, cache, provider)
    if box is None:
        return None
    rejected = _box_rejection(box, time.time())
    if rejected is not None:
        _note_probe_state(box_url, provider, "capped",
                          f"{sub} usage_limits.{rejected[0]} rejected", scope=sub)
        return {"reason": "provider_capped", "provider": provider, "sub": sub,
                "window": rejected[0], "reset_at": rejected[1]}
    _note_probe_state(box_url, provider, "ok", scope=sub)
    return None


def capped_provider(
    task, probes: dict, cache: dict, *, min_eligible: int = 1,
    pool_urls: dict | None = None, box_health: bool = True,
) -> dict | None:
    if not probes and not pool_urls:
        return None
    provider = effective_provider(task)
    url = (probes or {}).get(provider)
    if url is None and pool_urls:
        # No explicit probe: ask the pool that actually SERVES this provider.
        route = pool_route(provider)
        if route is not None:
            if route[1] is not None:
                return _pinned_sub_capped(
                    provider, route[1], pool_urls.get(route[0]), cache, box_health)
            url = pool_urls.get(route[0])
    if not _valid_url(url):
        return None
    data = _fetch(url, cache, provider)
    if data is None:
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
    pool_urls: dict | None = None, skip_pools=frozenset(), box_health: bool = True,
    budget_available=None,
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
        if budget_available is not None and not budget_available(provider):
            continue
        candidate = SimpleNamespace(
            model_override=model, provider_override=provider, assignee=task.assignee,
        )
        if capped_provider(
            candidate, probes, cache, min_eligible=min_eligible, pool_urls=pool_urls,
            box_health=box_health,
        ) is None:
            return model, provider
    return None
