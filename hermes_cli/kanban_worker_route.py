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
    stage: str = "auth", to_provider: Optional[str] = None, to_model: Optional[str] = None,
) -> bool:
    """Record that a pinned worker refused to substitute provider.

    ``stage`` is ``auth`` (startup; the worker exits) or ``runtime`` (a
    mid-turn failover to ``to_provider`` was refused; the worker keeps
    retrying its pinned provider).
    """
    payload = {
        "stage": stage,
        "provider": provider,
        "model": model or None,
        "rate_limited": bool(rate_limited),
    }
    if to_provider:
        payload["to_provider"] = to_provider
        payload["to_model"] = to_model or None
    if reason:
        payload["reason"] = str(reason)[:300]
    logger.warning(
        "PHASE=kanban_worker_route_pin_refused task=%s stage=%s provider=%s model=%s rate_limited=%s reason=%s",
        os.environ.get("HERMES_KANBAN_TASK"), stage, provider, model, rate_limited, payload.get("reason"),
    )
    return _append_run_event(WORKER_ROUTE_PIN_REFUSED_EVENT, payload)


# ---------------------------------------------------------------------------
# Runtime (mid-turn) failover for a CARD-pinned worker (t_ed0289e3)
# ---------------------------------------------------------------------------

# ``failure_reason`` a pinned worker's failed result carries when it refused a
# runtime failover and then ran out of retries on its pinned provider. Mapped
# to a retry-preserving exit class in ``kanban_worker_exit``.
PINNED_PROVIDER_FAILURE_REASON = "pinned_provider_unavailable"

_RATE_LIMIT_REASONS = frozenset({"rate_limit", "billing", "upstream_rate_limit"})
_card_pin_cache: dict = {}


def card_pinned_provider() -> Optional[str]:
    """The provider pinned on THIS worker's card row, else None.

    Only a card pin (``hermes kanban set-model``, persisted in
    ``tasks.model_override`` / ``provider_override``) counts. A board-wide
    lane override and a capped-pool dispatch fallback rung ALSO reach the
    worker as ``--provider``, but the dispatcher applies them to the in-memory
    claim only and never writes them to the card, so reading the row is what
    tells a card pin apart from a lane default. Fails open (None) when the
    board cannot be read: an unreadable pin must not strand a worker without
    its fallback chain.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id or not _is_owning_worker():
        return None
    if task_id in _card_pin_cache:
        return _card_pin_cache[task_id]
    provider = None
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_provider_health import model_override

        conn = kb.connect()
        try:
            task = kb.get_task(conn, task_id)
        finally:
            conn.close()
        if task is not None:
            _model, provider = model_override(task)
    except Exception:
        logger.debug("kanban card pin lookup failed for %s", task_id, exc_info=True)
        return None
    provider = (provider or "").strip().lower() or None
    _card_pin_cache[task_id] = provider
    return provider


def refuse_runtime_failover(agent, to_provider, to_model, reason=None) -> bool:
    """True when a card-pinned worker must NOT fail over to ``to_provider``.

    Refuses only when (a) the card pins a provider, (b) this run is actually
    serving on that pin (a dispatch fallback rung may have moved it), and
    (c) the fallback target is a different provider. Same-provider entries
    (another model / key on the pinned lane) stay allowed. The first refusal
    per agent writes one ``worker_route_pin_refused`` run event and marks the
    agent so a failed result exits retry-preserving
    (:func:`apply_pin_refusal_to_result`).
    """
    pinned = card_pinned_provider()
    if not pinned:
        return False
    primary = getattr(agent, "_primary_runtime", None)
    primary = primary if isinstance(primary, dict) else {}
    serving = str(primary.get("provider") or getattr(agent, "provider", "") or "").strip().lower()
    if serving != pinned:
        return False
    if str(to_provider or "").strip().lower() == pinned:
        return False
    reason_value = str(getattr(reason, "value", reason) or "") or None
    if not isinstance(getattr(agent, "_kanban_pin_refused_failover", None), dict):
        agent._kanban_pin_refused_failover = {
            "provider": pinned, "to_provider": to_provider, "reason": reason_value,
        }
        record_worker_route_pin_refused(
            provider=pinned, model=getattr(agent, "model", None),
            reason=reason_value, rate_limited=reason_value in _RATE_LIMIT_REASONS,
            stage="runtime", to_provider=to_provider, to_model=to_model,
        )
    return True


def apply_pin_refusal_to_result(agent, result):
    """Make a pinned worker's failed run requeue instead of counting a failure.

    Applies only when this agent refused a runtime failover, the run failed,
    the classifier called the failure retryable, and it is not already a
    retry-preserving class (a quota wall keeps its own reason). A
    deterministic failure (bad request, context overflow, auth) still exits 1
    so a broken card cannot requeue forever.
    """
    if not isinstance(result, dict) or not result.get("failed"):
        return result
    if not isinstance(getattr(agent, "_kanban_pin_refused_failover", None), dict):
        return result
    if not result.get("failure_retryable"):
        return result
    from hermes_cli.kanban_worker_exit import worker_exit_class

    if worker_exit_class(result.get("failure_reason"), result.get("error", "")) is not None:
        return result
    logger.warning(
        "PHASE=kanban_worker_pin_refused_exit task=%s original_failure_reason=%s",
        os.environ.get("HERMES_KANBAN_TASK"), result.get("failure_reason"),
    )
    result["failure_reason"] = PINNED_PROVIDER_FAILURE_REASON
    return result
