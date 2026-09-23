"""Opt-in, bounded pre-spawn probes. Only confirmed exhaustion defers work."""
from __future__ import annotations

import json
import logging
import re
import urllib.request


_log = logging.getLogger(__name__)


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


def capped_provider(task, probes: dict, cache: dict, *, min_eligible: int = 1) -> dict | None:
    if not probes:
        return None
    provider = effective_provider(task)
    url = probes.get(provider)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if url not in cache:
        cache[url] = None
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                data = json.loads(response.read(65536))
            if isinstance(data, dict) and (
                (type(data.get("eligible_count")) in (int, float)
                 and 0 <= data["eligible_count"] < min_eligible)
                or data.get("status") == "all_capped"
            ):
                reset_at = data.get("reset_at")
                cache[url] = {"reset_at": reset_at if type(reset_at) in (int, float, str) else None}
        except Exception:
            # A failed/unknown probe is not evidence of a capped account.
            _log.debug("kanban provider health unavailable for %s; admitting worker", provider)
    if cache[url] is not None:
        return {"reason": "provider_capped", "provider": provider, **cache[url]}
    return None


def available_profile_fallback(task, probes: dict, cache: dict, *, min_eligible: int = 1) -> tuple[str, str] | None:
    """Pick a healthy configured profile rung without changing the task row."""
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
        candidate = SimpleNamespace(
            model_override=model, provider_override=provider, assignee=task.assignee,
        )
        if capped_provider(candidate, probes, cache, min_eligible=min_eligible) is None:
            return model, provider
    return None
