"""Shared worker-model policy for Kanban creation and dispatch.

Main's configurable flagship ban (``kanban.banned_worker_model_substrings``)
is the ONE predicate.  The batch/lane routing helpers further down layer on
top of it (alias canonicalisation, route classification, audit text) and never
define a second ban list.
"""

from __future__ import annotations

import re
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
# Single-sub pin refusal (t_141135aa, Ace 2026-09-25) + deliberate opt-in
# (t_957ca870, Ace 2026-09-27).  Kanban routes ride the POOLS (claude-bpr /
# claude-apr) by default.  WHY #1116 refused every pin: cards/lanes pinned to
# claude-apx-0 -- Ace's PERSONAL Mac sub, also reachable under the alias
# claude-api-proxy -- put workers on his personal sub (95x 429 + 46x 401 in one
# day).  The hole was an UNDECLARED route onto sub 0 (hidden behind an alias),
# not pinning itself.  So any provider + model + effort may be pinned again
# with --pin-sub "<reason>" (supported operator capability) for a sub the usage
# registry admits -- sub 0 included, as a last resort; its protection is that
# the registry reserves it out of every pool.  The aliases stay refused even
# with the flag (name the real provider).  Do not re-ban pins wholesale.  ``claude-apx-N`` / ``claude-bpx-N`` pin one sub, and the
# pre-rename aliases (``claude-api-proxy`` = claude-apx-0 = Ace's personal Mac
# sub, ``claude-bridge`` = claude-bpx-0, ``-fN`` = sub N) are the same pins
# under a name that hides it.  Same patterns as the home fleet-config-lint
# ``provider_naming_floor`` rule.
# ---------------------------------------------------------------------------

PRE_RENAME_PROVIDER_ALIAS_RE = re.compile(
    r"^(?:claude-api-proxy(?:-f\d+|-failover\d+)?|claude-proxy(?:-f\d+)?"
    r"|claude-subscription-proxy|claude-bridge(?:-f\d+|-failover\d+|-fallback\d+)?"
    r"|claude-cli-bridge)$"
)
SINGLE_SUB_PIN_RE = re.compile(r"^claude-(?:apx|bpx)-\d+$")


def _route_provider_names(model: Optional[str], provider: Optional[str]) -> list[str]:
    names = []
    for value in (provider, (model or "").split("/", 1)[0] if "/" in (model or "") else None):
        name = str(value or "").strip().casefold()
        if name:
            names.append(name)
    return names


def pinned_sub_provider_error(
    model: Optional[str],
    provider: Optional[str] = None,
    *,
    pin_sub_reason: Optional[str] = None,
) -> Optional[str]:
    """Refusal text when a route pins one Claude sub (or a pre-rename alias of one).

    Checks the explicit provider and a ``provider/`` prefix on the model.
    Returns ``None`` for pool / non-Claude routes.

    ``pin_sub_reason`` is the deliberate opt-in (``--pin-sub "<reason>"``,
    t_957ca870, Ace 2026-09-27): with a non-empty reason a ``claude-apx-N`` /
    ``claude-bpx-N`` route (any N, sub 0 included) is allowed when
    :func:`sub_pin_admission_error` admits it. Every pre-rename alias stays
    refused even with the flag -- name the real provider.
    """
    reason = (pin_sub_reason or "").strip()
    for name in _route_provider_names(model, provider):
        lane = "bpr" if "bpx" in name or "bridge" in name else "apr"
        if PRE_RENAME_PROVIDER_ALIAS_RE.match(name):
            text = (
                f"provider '{name}' is a pre-rename alias of a single-sub pin "
                "(claude-api-proxy = claude-apx-0 = Ace's personal Mac sub). "
                f"Workers ride the pool (claude-{lane}) by default; to pin one sub "
                "name it as claude-bpx-N / claude-apx-N with --pin-sub \"<reason>\"."
            )
            if reason:
                text += (" --pin-sub never accepts a pre-rename alias; name the "
                         "sub as claude-bpx-N / claude-apx-N.")
            return text
        if SINGLE_SUB_PIN_RE.match(name):
            if not reason:
                return (
                    f"provider '{name}' pins one Claude subscription. Workers ride "
                    f"the pool provider claude-{lane} by default; a deliberate pin "
                    "needs --pin-sub \"<reason>\" (the sub must be enabled in the "
                    "usage registry; sub 0 is a last-resort pin)."
                )
            admission = sub_pin_admission_error(name)
            if admission:
                return admission
    return None


def is_sub_pin_route(model: Optional[str], provider: Optional[str] = None) -> bool:
    """True when the route names one ``claude-apx-N`` / ``claude-bpx-N`` sub."""
    return any(SINGLE_SUB_PIN_RE.match(n) for n in _route_provider_names(model, provider))


def sub_pin_family_pool(provider: Optional[str]) -> Optional[str]:
    """The pool a pinned sub belongs to: bpx-N -> claude-bpr, apx-N -> claude-apr."""
    name = str(provider or "").strip().casefold()
    if not SINGLE_SUB_PIN_RE.match(name):
        return None
    return "claude-bpr" if "-bpx-" in name else "claude-apr"


def _usage_registry_subs() -> Optional[list]:
    """``subs[]`` of the usage registry the relay pool and the claude-apx/bpx
    provider plugins read (``kanban_provider_health._usage_registry_path``).
    ``None`` when it cannot be read."""
    import json

    from . import kanban_provider_health

    try:
        data = json.loads(
            kanban_provider_health._usage_registry_path().read_text(encoding="utf-8-sig")
        )
    except Exception:
        return None
    subs = data.get("subs") if isinstance(data, dict) else None
    return subs if isinstance(subs, list) else None


def sub_pin_admission_error(provider: str) -> Optional[str]:
    """Why ``provider`` (``claude-apx-N`` / ``claude-bpx-N``) may NOT be pinned.

    The sub must be a row in the usage registry (the same file the relay pool
    loads, never a hand list; N=0 is key ``local``, else ``sub-vps-N``) with
    ``enabled: true``, and must be serving the lane being pinned:

    * ``claude-bpx-N`` -- the bridge serves every enabled sub, including one in
      burn-in or not (yet) admitted to claude-bpr: an enabled row is admitted.
    * ``claude-apx-N`` -- apx is OFF during burn-in (the sub-vps-21 ban), so a
      row whose ``burn_in_until`` is still in the future is refused.

    Sub 0 (Ace's own Max 20x, ``local``) IS pinnable with the same --pin-sub
    (Ace 2026-09-27 08:45): its protection lives on the POOL side (the registry
    reserves it out of every pool: pool_enabled false, pool_lb_exclude true),
    so no worker reaches it without an explicit pin. Standing rule: sub 0 is a
    LAST-RESORT pin -- only when Ace asks or every other sub is capped.

    The registry is read fresh; an unreadable registry refuses (fail closed:
    this is a write-time gate an operator can retry, not the dispatch loop).
    """
    name = str(provider or "").strip().casefold()
    match = re.match(r"^claude-(apx|bpx)-(\d+)$", name)
    if not match:
        return f"--pin-sub needs a claude-bpx-N / claude-apx-N provider, not '{provider}'"
    lane, n = match.group(1), int(match.group(2))
    subs = _usage_registry_subs()
    if subs is None:
        return (
            f"--pin-sub {name}: cannot read the usage registry to check the sub is "
            "admitted for worker traffic; refusing (fail closed)."
        )
    key = "local" if n == 0 else f"sub-vps-{n}"
    row = next((r for r in subs if isinstance(r, dict) and r.get("key") == key), None)
    if row is None:
        return (
            f"--pin-sub {name}: {key} is not in the usage registry; only registered, "
            "enabled subs are pinnable."
        )
    if row.get("enabled") is not True:
        return f"--pin-sub {name}: {key} is disabled in the usage registry (enabled != true)."
    if lane == "apx":
        from datetime import datetime, timezone

        until = row.get("burn_in_until")
        try:
            ends = datetime.fromisoformat(str(until).replace("Z", "+00:00")) if until else None
        except ValueError:
            ends = None
        if ends is not None and ends > datetime.now(timezone.utc):
            return (
                f"--pin-sub {name}: {key} is in burn-in until {until}; apx is off "
                f"during burn-in. Pin claude-bpx-{n} instead."
            )
    return None


def pin_sub_arg_error(
    model: Optional[str], provider: Optional[str], pin_sub_reason: Optional[str],
    *, pin_sub_fallback: bool = False,
) -> Optional[str]:
    """``--pin-sub`` / ``--pin-sub-fallback`` given on a route that pins nothing."""
    if (pin_sub_reason is not None and not str(pin_sub_reason).strip()):
        return "--pin-sub requires a non-empty reason"
    if pin_sub_fallback and not (pin_sub_reason or "").strip():
        return "--pin-sub-fallback requires --pin-sub <reason>"
    names = _route_provider_names(model, provider)
    if any(PRE_RENAME_PROVIDER_ALIAS_RE.match(n) for n in names):
        return None  # pinned_sub_provider_error names the alias and the fix
    if (pin_sub_reason or "").strip() and not is_sub_pin_route(model, provider):
        return (
            "--pin-sub needs a single-sub route (--provider claude-bpx-N / "
            "claude-apx-N); pool and non-Claude routes are not pins"
        )
    return None


def validate_route_provider(
    model: Optional[str],
    provider: Optional[str] = None,
    *,
    pin_sub_reason: Optional[str] = None,
) -> None:
    """Raise ``ValueError`` for a single-sub pin route (see above)."""
    error = pinned_sub_provider_error(model, provider, pin_sub_reason=pin_sub_reason)
    if error:
        raise ValueError(error)


SUB_PIN_COMMENT_PREFIX = "sub pin:"


def sub_pin_comment(provider: Optional[str], reason: str, *, fallback: bool = False) -> str:
    """Audit comment written with every deliberate pin (card or lane)."""
    tail = f"; fallback={sub_pin_family_pool(provider)}" if fallback else "; fallback=wait"
    return f"{SUB_PIN_COMMENT_PREFIX} {provider}{tail}; reason={reason.strip()}"


def format_pin_badge(provider: Optional[str], reason: Optional[str]) -> str:
    """``[PIN claude-bpx-N: <reason>]`` for show/list/pins, or ``""``."""
    reason = (reason or "").strip()
    if not reason or not provider:
        return ""
    return f"[PIN {provider}: {reason}]"


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
