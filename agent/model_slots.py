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
from typing import Any

from hermes_cli.model_registry import _split_prefixed, slot_resolve

logger = logging.getLogger(__name__)


def apply_slot_at_init(agent: Any) -> None:
    """Resolve ``agent.model`` once, before routing/context/compressor setup reads it."""
    resolved, slot = slot_resolve(getattr(agent, "model", None))
    agent._model_slot = slot
    if slot:
        logger.info("model slot %s -> %s", slot, resolved)
        agent.model = resolved


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
    new_id, new_slot = slot_resolve(slot)
    # Bare-id compare: init may have normalized the lane prefix off agent.model.
    if not new_slot or not new_id or _split_prefixed(new_id)[1] == _split_prefixed(agent.model or "")[1]:
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


__all__ = ["apply_slot_at_init", "refresh_slot_for_turn"]
