"""Operator override of the model a kanban boomerang-wake child runs on (t_54bbaca0).

Default (no override anywhere): the child runs exactly as before, on the home session's route
(a fork when eligible, else a summary fold that inherits the parent's model and lane). Wakes are
orchestration, so the parent's model is the deliberate default (Ace msg 1573512367600308385).

An override is a model-slot name (``opus``) or ``provider/model`` (``claude-alr/claude-opus-5-5``).
Precedence, first set wins:

1. card: a body line ``wake-model: <value>`` on the woken card;
2. session: ``delegation.boomerang.wake_child.sessions.<home session id>: <value>``;
3. global: ``delegation.boomerang.wake_child.model`` (lane: ``wake_child.provider``);
4. the parent's route.

A slot or bare id keeps the parent's lane unless ``wake_child.provider`` names one. ``inherit``
at any level means the parent. A value naming no known slot or model falls back to the parent
with one log line; the wake is never dropped. An overridden child cannot share the parent's
prompt cache, so it always runs as a summary fold, with no fallback chain of its own: when it
fails, the wake runs again on the parent's model and chain and its surfaced line says so.
"""
from __future__ import annotations

import contextvars
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CARD_LINE = re.compile(r"^[ \t]*wake-model:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_PARENT = ("inherit", "parent")

#: The resolved override of the wake whose child runs in this context (set by the dispatcher;
#: ``asyncio.to_thread`` copies it into the worker thread). None = the parent's route.
CURRENT_ROUTE: contextvars.ContextVar = contextvars.ContextVar("wake_child_route", default=None)


def card_wake_model(body: Any) -> Optional[str]:
    """The ``wake-model:`` value on a card body (the last such line), else None."""
    found = _CARD_LINE.findall(body) if isinstance(body, str) else []
    return found[-1] if found else None


def _delegation_cfg() -> dict:
    from tools.delegate_tool_config import _load_config

    cfg = _load_config()
    return cfg if isinstance(cfg, dict) else {}


def wake_child_cfg(delegation_cfg: Optional[dict] = None) -> dict:
    cfg = _delegation_cfg() if delegation_cfg is None else delegation_cfg
    boom = cfg.get("boomerang") if isinstance(cfg, dict) else None
    wc = boom.get("wake_child") if isinstance(boom, dict) else None
    return wc if isinstance(wc, dict) else {}


def requested(card_value: Optional[str], home_sid: Any,
              delegation_cfg: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    """``(value, source)`` of the first override set (card, session, global), or ``(None, None)``."""
    wc = wake_child_cfg(delegation_cfg)
    sessions = wc.get("sessions") if isinstance(wc.get("sessions"), dict) else {}
    for value, source in ((card_value, "card"), (sessions.get(str(home_sid or "")), "session"),
                          (wc.get("model"), "global")):
        if isinstance(value, str) and value.strip():
            return value.strip(), source
    return None, None


def resolve_route(value: Optional[str], parent_provider: Optional[str], *, source: str = "",
                  task_id: str = "", delegation_cfg: Optional[dict] = None) -> Optional[dict]:
    """``{"model", "provider", "label", "source"}`` for an override value, or None for the parent's
    route (no value, ``inherit``, or a value whose lane does not list the model: one log line)."""
    if not value or value.strip().lower() in _PARENT:
        return None
    from hermes_cli.model_registry import resolve, slot_resolve

    spec = value.strip()
    model_id = slot_resolve(spec)[0] or spec
    lane = str(wake_child_cfg(delegation_cfg).get("provider") or parent_provider or "").strip()
    if "/" in model_id:
        lane, model_id = model_id.split("/", 1)
    try:
        verdict = resolve(lane or None, model_id, resolve_aliases=False)
        why = f"{verdict.reason}: {verdict.model} on {verdict.provider or 'no lane'}"
    except Exception as exc:
        verdict, why = None, f"not resolvable ({exc})"
    if verdict is None or verdict.reason != "known":   # only a model its lane lists
        logger.warning("kanban boomerang-wake: %s wake-model %r from %s is %s; the child runs on the parent's model",
                       task_id or "?", spec, source or "config", why)
        return None
    return {"model": verdict.model, "provider": verdict.provider, "label": spec, "source": source}


def delegate_kwargs(route: Optional[dict], mode: str) -> dict:
    """``delegate_task`` kwargs: the parent's route and chain as before, or the override pinned as a
    summary fold with NO fallback chain of its own (its failure falls back to the parent's route)."""
    if not route:
        return {"inherit_mode": mode}
    creds = {"provider": route["provider"], "model": route["model"], "base_url": "", "api_key": "",
             "api_mode": "", "fallback_providers": []}
    return {"inherit_mode": "summary", "model": route["model"], "provider": route["provider"],
            "credentials_cfg": creds}
