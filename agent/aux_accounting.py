"""Ambient session-accounting context for auxiliary LLM calls.

Aux calls (vision, compression, title generation, web_extract, session_search, ...) go
through ``agent.auxiliary_client`` which has no session handle, so their usage was
historically discarded. The agent loop publishes ``(session_db, session_id)`` here
(mirroring ``agent.portal_tags``) and the aux client records usage at its single
response-validation chokepoint. ContextVar semantics isolate concurrent agents, propagate
to worker threads via ``tools.thread_context`` and to asyncio tasks automatically.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Optional

logger = logging.getLogger(__name__)

# (session_db, session_id) for the active agent turn, or None outside one.
_accounting: ContextVar[Optional[tuple]] = ContextVar("aux_accounting_context", default=None)

# MoA advisor/aggregator usage is already folded into conversation_loop's
# update_token_counts delta (tokens AND cost); recording it here would double-count.
_EXCLUDED_TASKS = frozenset({"moa_reference", "moa_aggregator"})


def set_accounting_context(session_db: Any, session_id: Optional[str]):
    """Publish the active session's accounting handles; returns the token for ``reset_accounting_context``.

    ``None`` handles (no DB / no session id) clear the context.
    """
    if session_db is None or not session_id:
        return _accounting.set(None)
    return _accounting.set((session_db, session_id))


def reset_accounting_context(token) -> None:
    """Restore the previous accounting context (pair with ``set_...``)."""
    try:
        _accounting.reset(token)
    except Exception:
        _accounting.set(None)


def get_accounting_context() -> Optional[tuple]:
    """Return ``(session_db, session_id)`` for the active turn, or ``None``."""
    return _accounting.get()


# (agent, turn_id) of the active agent turn, for the Blackbox per-call ledger
# (``turn_api_calls``). Separate from ``_accounting`` because the ledger needs
# the agent's per-turn sequence allocator and must work without a session DB.
_blackbox_turn: ContextVar[Optional[tuple]] = ContextVar(
    "aux_blackbox_turn", default=None
)


def set_blackbox_turn(agent: Any, turn_id: Optional[str]):
    """Publish the turn aux calls are ledgered under. Returns the reset token."""
    if agent is None or not turn_id:
        return _blackbox_turn.set(None)
    return _blackbox_turn.set((agent, str(turn_id)))


def reset_blackbox_turn(token) -> None:
    try:
        _blackbox_turn.reset(token)
    except Exception:
        _blackbox_turn.set(None)


def get_blackbox_turn() -> Optional[tuple]:
    """Return ``(agent, turn_id)`` for the active turn, or ``None``."""
    return _blackbox_turn.get()


def _usage_api_mode(raw: Any, route_api_mode: Optional[str]) -> str:
    """Usage dialect from the usage object itself (aux clients return
    OpenAI-shaped usage even on Anthropic-routed providers)."""
    def _has(key: str) -> bool:
        val = raw.get(key) if isinstance(raw, dict) else getattr(raw, key, None)
        return isinstance(val, (int, float)) and not isinstance(val, bool)

    if _has("prompt_tokens"):
        return "chat_completions"
    if _has("input_tokens"):
        return route_api_mode or "anthropic_messages"
    return route_api_mode or "chat_completions"


def record_aux_api_call(
    response: Any,
    task: Optional[str],
    route_info: Optional[dict] = None,
    route_id: Optional[str] = None,
) -> None:
    """Ledger one successful auxiliary call in Blackbox ``turn_api_calls``.

    One row per completed non-streaming aux call made inside an agent turn:
    the route's provider/model, usage and cache fields, ``attribution =
    'aux:<task>'`` and ``lane_family = 'aux'`` (so main-lane cache and
    reconciliation readers can exclude it). Turn cost/token totals are not
    touched. Strictly best-effort; no-ops outside a turn and for MoA slots
    (their usage is folded into the main loop's totals, ``_EXCLUDED_TASKS``).
    """
    try:
        if response is None or task in _EXCLUDED_TASKS:
            return
        binding = _blackbox_turn.get()
        if binding is None:
            return
        agent, turn_id = binding
        raw = (response.get("usage") if isinstance(response, dict)
               else getattr(response, "usage", None))
        route = route_info if isinstance(route_info, dict) else {}
        provider = str(route.get("provider") or "")
        model = str(route.get("model") or "")
        if not model or model == "default":
            model = str(getattr(response, "model", "") or "") or model
        api_mode = _usage_api_mode(raw, route.get("api_mode"))
        usage = None
        if raw is not None:
            from agent.usage_pricing import normalize_usage

            # normalize_usage reads provider "anthropic" as the Anthropic
            # dialect regardless of api_mode; the shape decides here.
            norm_provider = "" if (api_mode == "chat_completions"
                                   and provider.strip().lower() == "anthropic") else provider
            usage = normalize_usage(raw, provider=norm_provider, api_mode=api_mode)
        from agent.chat_completion_helpers import _emit_aux_api_call_record

        _emit_aux_api_call_record(
            agent, turn_id,
            task=str(task or "unspecified"),
            provider=provider, model=model, usage=usage, api_mode=api_mode,
            route_id=route_id,
        )
    except Exception:
        logger.debug("Aux Blackbox ledger recording failed (non-fatal)", exc_info=True)


def record_aux_usage(
    response: Any, task: Optional[str], *, provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> None:
    """Record an auxiliary response's token usage against the ambient session.

    Strictly best-effort (accounting must never break an aux call). No-ops outside an
    agent turn, for main-loop-accounted tasks (``_EXCLUDED_TASKS``), or without usage.
    The model is read from ``response.model`` (accurate after aux provider fallback);
    *provider*/*base_url* reflect the originally-resolved route.
    """
    try:
        if not task or task in _EXCLUDED_TASKS:
            return
        ctx = _accounting.get()
        if ctx is None:
            return
        session_db, session_id = ctx
        raw_usage = getattr(response, "usage", None)
        if raw_usage is None:
            return

        from agent.usage_pricing import (
            USAGE_UNKNOWN_FIELDS, estimate_usage_cost, normalize_usage, with_served_service_tier,
        )

        usage = with_served_service_tier(normalize_usage(raw_usage, provider=provider), response)
        unknown_flags = {
            key: bool(getattr(usage, key, False)) for key in USAGE_UNKNOWN_FIELDS
        }
        if not (
            usage.input_tokens or usage.output_tokens
            or usage.cache_read_tokens or usage.cache_write_tokens
            or usage.reasoning_tokens
            # UNKNOWN != 0: an all-zero canonical usage that carries a
            # discriminator is an UNMEASURED call, not an empty one. Returning
            # early here would drop the only evidence the aux ledger has that
            # its totals are incomplete.
            or any(unknown_flags.values())
        ):
            return
        model = str(getattr(response, "model", "") or "") or "unknown"
        estimated_cost = None
        try:
            cost = estimate_usage_cost(model, usage, provider=provider, base_url=base_url)
            if cost.amount_usd is not None:
                estimated_cost = float(cost.amount_usd)
        except Exception:
            logger.debug("Aux usage cost estimation failed", exc_info=True)
        session_db.record_auxiliary_usage(
            session_id, task, model=model, billing_provider=provider, billing_base_url=base_url,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens, cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens, estimated_cost_usd=estimated_cost,
            **unknown_flags,
        )
    except Exception:
        logger.debug("Aux usage recording failed (non-fatal)", exc_info=True)
