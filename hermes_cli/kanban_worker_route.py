"""Make a kanban worker's in-process provider substitution visible on the board.

The dispatcher records the route it CHOSE (``dispatch_lane_route`` /
``dispatch_provider_fallback``). After spawn, the worker process can still
swap provider on its own: an auth-time fallback when the pinned provider's
credentials fail to resolve (``Primary auth failed — switching to fallback``)
and a runtime fallback when the provider errors mid-turn. Neither reached the
board, so a card pinned to ``openai-codex`` to escape a bridge fault quietly
ran on ``claude-bpr`` (t_7b7cb5aa run 9822, card t_4fe0700a).

This module is the worker-side counterpart: one run-scoped
``worker_route_substituted`` event per substitution, and a predicate for
"this worker's provider was pinned at spawn", which the CLI uses to refuse an
auth-time substitution instead of running a pinned card on another lane.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

WORKER_ROUTE_SUBSTITUTED_EVENT = "worker_route_substituted"
WORKER_ROUTE_PIN_REFUSED_EVENT = "worker_route_pin_refused"


def _is_owning_worker() -> bool:
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return False
    try:
        from agent.delegation_context import owns_kanban_worker_authority

        return bool(owns_kanban_worker_authority())
    except Exception:
        return False


def pinned_worker_provider(explicit_provider: Optional[str]) -> Optional[str]:
    """The provider this kanban worker was pinned to at spawn, else None.

    The dispatcher only passes ``--provider`` when it resolved a route for the
    card (card ``set-model``, lane override, or capped-pool fallback rung), so
    an explicit ``--provider`` inside an owning worker is a pin, not a default.
    """
    provider = (explicit_provider or "").strip().lower()
    if not provider or provider == "auto" or not _is_owning_worker():
        return None
    return provider


def _append_run_event(kind: str, payload: dict) -> bool:
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id or not _is_owning_worker():
        return False
    run_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    try:
        run_id = int(run_raw) if run_raw else None
    except (TypeError, ValueError):
        run_id = None
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, kind, payload, run_id=run_id)
        finally:
            conn.close()
        return True
    except Exception:
        # Telemetry must never break the worker; the warning below still lands
        # in the worker log.
        logger.debug("kanban worker route event %s failed", kind, exc_info=True)
        return False


def record_worker_route_substitution(
    *,
    stage: str,
    from_provider: Optional[str],
    from_model: Optional[str],
    to_provider: Optional[str],
    to_model: Optional[str],
    reason: Optional[str] = None,
) -> bool:
    """Record that this worker swapped provider after the dispatcher spawned it.

    ``stage`` is ``auth`` (credential resolution at startup) or ``runtime``
    (failover mid-turn). ``to_provider`` is the pool that actually serves the
    run, so ``rate_limit_circuits`` charges a rate-limited close to it.
    """
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return False
    payload = {
        "stage": stage,
        "from_provider": from_provider or None,
        "from_model": from_model or None,
        "to_provider": to_provider or None,
        "to_model": to_model or None,
    }
    if reason:
        payload["reason"] = str(reason)[:300]
    logger.warning(
        "PHASE=kanban_worker_route_substituted task=%s stage=%s %s/%s -> %s/%s reason=%s",
        os.environ.get("HERMES_KANBAN_TASK"), stage, from_provider, from_model,
        to_provider, to_model, payload.get("reason"),
    )
    return _append_run_event(WORKER_ROUTE_SUBSTITUTED_EVENT, payload)


def record_worker_route_pin_refused(
    *, provider: str, model: Optional[str], reason: Optional[str], rate_limited: bool,
) -> bool:
    """Record that a pinned worker refused to substitute and is exiting."""
    payload = {
        "provider": provider,
        "model": model or None,
        "rate_limited": bool(rate_limited),
    }
    if reason:
        payload["reason"] = str(reason)[:300]
    logger.warning(
        "PHASE=kanban_worker_route_pin_refused task=%s provider=%s model=%s rate_limited=%s reason=%s",
        os.environ.get("HERMES_KANBAN_TASK"), provider, model, rate_limited, payload.get("reason"),
    )
    return _append_run_event(WORKER_ROUTE_PIN_REFUSED_EVENT, payload)
