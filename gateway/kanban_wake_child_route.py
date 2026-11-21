"""Operator override of the model a kanban boomerang-wake child runs on (t_54bbaca0).

Default (no override anywhere): the child runs exactly as before, on the home session's route
(a fork when eligible, else a summary fold that inherits the parent's model and lane). Wakes are
orchestration, so the parent's model is the deliberate default (Ace msg 1573512367600308385).

Precedence, first set wins:

1. card: a body line ``wake-model: <slot>`` on the woken card. A model-routing SLOT name only:
   a worker can edit a card body, so the card cannot pick a provider the operator never chose;
2. session: ``delegation.boomerang.wake_child.sessions.<home session id>: <value>``;
3. global: ``delegation.boomerang.wake_child.model`` (lane: ``wake_child.provider``);
4. the parent's route.

Session and global values (operator-written config) take a slot, a bare model id or an explicit
``provider/model``. A slot keeps its target id whole (``anthropic/claude-x`` stays one model id) on
the parent's lane unless ``wake_child.provider`` names one. ``inherit`` at any level means the parent.

An override that cannot be used (unknown slot or model, a lane that does not list it, an unreadable
value, a same-lane override, on the RESOLVED provider, while the gateway's ``provider_routing``
sets a filter or cannot be read) is NOT dropped:
the wake runs on the parent and its surfaced line says ``wake-model <x> not usable (<why>)``. An
overridden child runs as a summary fold with no fallback chain of its own; any non-success outcome
reruns the wake on the parent's model and chain, and its surfaced line says so.

Effort (item 5): ``wake-effort: <level>`` on the card, ``wake_child.effort_sessions.<home sid>``
and ``wake_child.effort``, same precedence. A level is ``none`` or one of
``hermes_constants.VALID_REASONING_EFFORTS`` (a fixed word list, so the card line is safe). It
works alone (the parent's model and lane; a level other than the parent's runs as a summary fold,
since it changes the fork's request fingerprint) or with a model override, and
reaches the child as ``delegate_task(reasoning_effort=...)``. No setting = the home's effort.
"""
from __future__ import annotations

import contextvars
import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_CARD_LINE = re.compile(r"^[ \t]*wake-model:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_EFFORT_LINE = re.compile(r"^[ \t]*wake-effort:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_PARENT = ("inherit", "parent")

def _efforts() -> tuple:
    """Every level the runtime accepts: ``none`` plus ``VALID_REASONING_EFFORTS`` (not restated)."""
    from hermes_constants import VALID_REASONING_EFFORTS

    return ("none", *VALID_REASONING_EFFORTS)
# Parent attributes a pinned provider resets (tools/delegate_tool_config._ROUTING_FILTER_DEFAULTS).
_FILTERS = ("providers_allowed", "providers_ignored", "providers_order", "provider_sort",
            "provider_require_parameters", "provider_data_collection")
# The gateway's ``provider_routing`` keys a live parent is built from (gateway/run_turn_runner.py).
_ROUTING_KEYS = ("only", "ignore", "order", "sort", "require_parameters", "data_collection")

#: The resolved override of the wake whose child runs in this context (set by the dispatcher;
#: ``asyncio.to_thread`` copies it into the worker thread). None = the parent's route.
CURRENT_ROUTE: contextvars.ContextVar = contextvars.ContextVar("wake_child_route", default=None)


class Unusable(ValueError):
    """An override value that cannot be used; ``str(exc)`` is the reason for the wake line."""


def card_wake_model(body: Any) -> Optional[str]:
    """The ``wake-model:`` value on a card body (the last such line), else None."""
    found = _CARD_LINE.findall(body) if isinstance(body, str) else []
    return found[-1] if found else None


def card_wake_effort(body: Any) -> Optional[str]:
    """The ``wake-effort:`` value on a card body (the last such line), else None."""
    found = _EFFORT_LINE.findall(body) if isinstance(body, str) else []
    return found[-1] if found else None


def requested_effort(card_value: Optional[str], home_sids: Any,
                     delegation_cfg: Optional[dict] = None) -> tuple[Any, Optional[str]]:
    """``(value, source)`` of the first effort override (card, session, global), else ``(None, None)``."""
    return requested(card_value, home_sids, delegation_cfg, key="effort", sessions_key="effort_sessions")


def resolve_effort(value: Any, *, source: str = "") -> Optional[str]:
    """The effort word for an override value (``none`` or a ``VALID_REASONING_EFFORTS`` level), None
    for the parent's effort (no value or ``inherit``). Raises :class:`Unusable` for anything else: a
    fixed word list, so a card line cannot send anything but a known effort."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise Unusable(f"unreadable {source or 'config'} value {type(value).__name__}")
    word = value.strip().lower()
    if not word or word in _PARENT:
        return None
    if word not in _efforts():
        raise Unusable(f"effort must be one of {'|'.join(_efforts())}")
    return word


def _delegation_cfg() -> dict:
    from tools.delegate_tool_config import _load_config

    cfg = _load_config()
    return cfg if isinstance(cfg, dict) else {}


def wake_child_cfg(delegation_cfg: Optional[dict] = None) -> dict:
    """The ``delegation.boomerang.wake_child`` mapping; ``{}`` when absent. A section that is set but
    is not a mapping raises :class:`Unusable` (surfaced like a bad value inside it)."""
    cfg = _delegation_cfg() if delegation_cfg is None else delegation_cfg
    boom = cfg.get("boomerang") if isinstance(cfg, dict) else None
    wc = boom.get("wake_child") if isinstance(boom, dict) else None
    if wc is not None and not isinstance(wc, dict):
        raise Unusable(f"unreadable wake_child section {type(wc).__name__}")
    return wc or {}


def requested(card_value: Optional[str], home_sids: Any, delegation_cfg: Optional[dict] = None, *,
              key: str = "model", sessions_key: str = "sessions") -> tuple[Any, Optional[str]]:
    """``(value, source)`` of the first override set (card, session, global), or ``(None, None)``.

    ``home_sids``: the home session ids to look the session override up by, resolved id first
    (one id or an iterable). A set value that is not a non-empty string is returned as is, so
    :func:`resolve_route` reports it unusable instead of skipping it."""
    wc = wake_child_cfg(delegation_cfg)
    sessions = wc.get(sessions_key)
    ids: Iterable = (home_sids,) if isinstance(home_sids, str) or home_sids is None else home_sids
    session_value = None
    if isinstance(sessions, dict):
        session_value = next((sessions[s] for s in (str(i) for i in ids if i) if s in sessions), None)
    for value, source in ((card_value, "card"), (session_value, "session"), (wc.get(key), "global")):
        if source == "session" and sessions is not None and not isinstance(sessions, dict):
            raise Unusable(f"unreadable {sessions_key} map {type(sessions).__name__}")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return (value.strip() if isinstance(value, str) else value), source
    return None, None


def _filters_set(parent: Any, routing: Any) -> list:
    """The routing/privacy filters the parent runs with: the gateway's ``provider_routing`` (the
    source a live parent is built from; a cold rebuild after a restart carries none) plus any on the
    parent object. ``routing`` None or not a mapping = cannot be read: raises :class:`Unusable`."""
    if not isinstance(routing, dict):
        raise Unusable("the parent's routing filters cannot be read")
    empty = (None, "", False, [], ())
    return ([k for k in _ROUTING_KEYS if routing.get(k) not in empty]
            + [a for a in _FILTERS if getattr(parent, a, None) not in empty])


def _canon(provider: Any) -> str:
    from hermes_cli.providers import normalize_provider

    return normalize_provider(str(provider or ""))


def resolve_route(value: Any, parent_provider: Optional[str], *, source: str = "", task_id: str = "",
                  delegation_cfg: Optional[dict] = None, parent: Any = None,
                  routing: Any = None) -> Optional[dict]:
    """``{"model", "provider", "label", "source"}`` for an override value, or None for the parent's
    route (no value or ``inherit``). Raises :class:`Unusable` for a value that cannot be used.

    ``routing``: the gateway's ``provider_routing`` mapping. A same-lane override (decided on the
    RESOLVED provider) would reset the parent's filters, so it is unusable when any is set or when
    ``routing`` cannot be read (None)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise Unusable(f"unreadable {source or 'config'} value {type(value).__name__}")
    spec = value.strip()
    if not spec or spec.lower() in _PARENT:
        return None
    from hermes_cli.model_registry import resolve, slot_view

    slots = slot_view()
    wc_lane = str(wake_child_cfg(delegation_cfg).get("provider") or "").strip()
    lane = wc_lane or str(parent_provider or "").strip()
    if spec in slots:
        model_id = slots[spec]              # a slot's target is one model id, never split (Prism bd8cd36ed8a8)
    elif source == "card":
        raise Unusable("the card line takes a model-routing slot name only")
    elif "/" in spec:
        lane, model_id = spec.split("/", 1)   # an explicit provider/model (operator config)
        model_id = slots.get(model_id, model_id)
    else:
        model_id = spec
    try:
        verdict = resolve(_canon(lane) or None, model_id, resolve_aliases=False)
    except Exception as exc:
        raise Unusable(f"not resolvable: {exc}") from exc
    if verdict.reason != "known":   # only a model its lane lists
        raise Unusable(f"{verdict.reason}: {verdict.model} on {verdict.provider or 'no lane'}")
    same_lane = _canon(verdict.provider) == _canon(parent_provider)
    kept = _filters_set(parent, routing) if same_lane else []
    if kept:   # Prism 3d5c62bd0009: a provider pin resets these; never drop the parent's filters
        raise Unusable(f"would drop the parent's routing filters ({', '.join(kept)})")
    return {"model": verdict.model, "provider": verdict.provider, "label": spec, "source": source}


def delegate_kwargs(route: Optional[dict], mode: str) -> dict:
    """``delegate_task`` kwargs: the parent's route and chain as before, or the override pinned as a
    summary fold with NO fallback chain of its own (its failure falls back to the parent's route).
    An effort override (``route["effort"]``) rides along either way, fork included."""
    effort = {"reasoning_effort": route["effort"]} if route and route.get("effort") else {}
    if not route or not route.get("model"):
        return {"inherit_mode": mode, **effort}
    creds = {"provider": route["provider"], "model": route["model"], "base_url": "", "api_key": "",
             "api_mode": "", "fallback_providers": []}
    return {"inherit_mode": "summary", "model": route["model"], "provider": route["provider"],
            "credentials_cfg": creds, **effort}
