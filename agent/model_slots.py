"""Slot names (``sol``, ``opus``, ``grok``, ...) resolved to real model ids for a live agent.

Spec: hermes-home ``plans/2026-11-19_model-slot-aliases-spec.md`` section 3.3 (D1 A). A seat
may be configured with a slot name; the runtime reads ``<root>/config/model-slots.json``
(:func:`hermes_cli.model_registry.slot_view`) and puts the resolved id in ``agent.model``, so
every lookup keyed on ``agent.model`` (context length, pricing, effort tables, compression
thresholds, cache keys) and the wire see the real id. The slot name is kept in
``agent._model_slot``; :func:`refresh_slot_for_turn` re-resolves it at each turn start, so a
re-rendered view moves the next turn to the new id with no restart and no pin migration.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def resolve(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(id, slot)``; ``(model, None)`` for a literal id, an unknown name or no view."""
    try:
        from hermes_cli.model_registry import slot_resolve

        return slot_resolve(model)
    except Exception:
        return model, None


def apply_slot_at_init(agent: Any) -> None:
    """Resolve ``agent.model`` once, before routing/context/compressor setup reads it."""
    resolved, slot = resolve(getattr(agent, "model", None))
    agent._model_slot = slot
    if slot:
        logger.info("model slot %s -> %s", slot, resolved)
        agent.model = resolved


def model_label(agent: Any) -> str:
    """``sol -> gpt-6.2-sol`` for a slot-configured agent, else the model id."""
    model = str(getattr(agent, "model", "") or "")
    slot = getattr(agent, "_model_slot", None)
    return f"{slot} -> {model}" if slot and model and slot != model else model


def refresh_slot_for_turn(agent: Any) -> bool:
    """Turn start: move a slot-configured agent to the slot's current id. True when it moved.

    Runs after the primary-runtime restore, so it only acts on the primary route (a turn still
    held on a fallback keeps it; the next restored turn picks the flip up). A view that went
    missing or dropped the slot keeps the id already in use rather than putting a bare slot name
    on the wire.
    """
    slot = getattr(agent, "_model_slot", None)
    if not slot or getattr(agent, "_fallback_activated", False):
        return False
    new_id, new_slot = resolve(slot)
    if not new_slot or not new_id or new_id == agent.model:
        return False
    old_id = agent.model
    try:
        from agent.agent_runtime_helpers import switch_model

        switch_model(
            agent, slot, agent.provider,
            api_key=getattr(agent, "api_key", "") or "",
            base_url=getattr(agent, "base_url", "") or "",
            api_mode=getattr(agent, "api_mode", "") or "",
            persist_billing_route=False, persist_fallback=False,
        )
    except Exception:
        logger.warning("model slot %s: switch %s -> %s failed; keeping %s",
                       slot, old_id, new_id, old_id, exc_info=True)
        return False
    logger.info("model slot %s moved: %s -> %s", slot, old_id, agent.model)
    return True


__all__ = ["apply_slot_at_init", "model_label", "refresh_slot_for_turn", "resolve"]
