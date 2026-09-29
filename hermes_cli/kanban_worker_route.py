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
"this worker's card pins its provider", which the CLI uses to refuse an
auth-time substitution instead of running a pinned card on another lane.
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
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
    """The provider this kanban worker is CARD-pinned to at spawn, else None.

    The dispatcher passes ``--provider`` for a card ``set-model`` pin, a
    board-wide lane override, AND a capped-pool fallback rung. Only the first
    is a pin (t_16642ede): the other two are claim-only and never written to
    the card, so treating any ``--provider`` as a pin stripped every
    lane-overridden worker of its auth-time fallback chain. The explicit flag
    is the value; :func:`card_pinned_provider` is the predicate. A run spawned
    on a different provider than the card pins (a dispatch fallback rung) is
    already off its pin and keeps the fallback chain, same as the runtime
    check in :func:`refuse_runtime_failover`.
    """
    provider = (explicit_provider or "").strip().lower()
    if not provider or provider == "auto" or not _is_owning_worker():
        return None
    if card_pinned_provider() != provider:
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
# JSON ``{"model", "provider"}`` the dispatcher sets from the card ROW at claim
# time (t_a30417c3); seeds the pin snapshot so the worker never reads a row a
# later ``set-model`` may already have changed. Absent = older dispatcher.
CLAIMED_CARD_PIN_ENV = "KANBAN_CLAIMED_CARD_PIN"


def _claimed_card_pin_from_env():
    raw = os.environ.get(CLAIMED_CARD_PIN_ENV)
    if not raw:
        return None
    try:
        pin = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(pin, dict):
        return None
    return pin.get("model"), pin.get("provider")

# The two Claude relay POOL faces. A card pinned to one means "this model on
# the pool", not one wire, so failing over to the SIBLING pool for the SAME
# model keeps the pin's meaning (t_6ea895ca). A single-sub pin
# (claude-apx-N / claude-bpx-N) is not a pool face and stays strict.
_SIBLING_POOL = {"claude-bpr": "claude-apr", "claude-apr": "claude-bpr"}


def _is_sibling_pool_same_model(pinned, to_provider, from_model, to_model) -> bool:
    if _SIBLING_POOL.get(pinned) != str(to_provider or "").strip().lower():
        return False
    src = str(from_model or "").strip().lower()
    return bool(src) and src == str(to_model or "").strip().lower()


def card_pinned_route() -> tuple:
    """``(model, provider)`` pinned on THIS worker's card row, else ``(None, None)``.

    Only a card pin (``hermes kanban set-model``, persisted in
    ``tasks.model_override`` / ``provider_override``) counts. A board-wide
    lane override and a capped-pool dispatch fallback rung ALSO reach the
    worker as ``--provider``, but the dispatcher applies them to the in-memory
    claim only and never writes them to the card, so reading the row is what
    tells a card pin apart from a lane default. The dispatcher hands the row
    pin as claimed via ``CLAIMED_CARD_PIN_ENV`` (t_a30417c3); the row is read
    only when that is absent (older dispatcher). Fails open ``(None, None)``
    when the board cannot be read: an unreadable pin must not strand a worker
    without its fallback chain.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id or not _is_owning_worker():
        return None, None
    if task_id in _card_pin_cache:
        return _card_pin_cache[task_id]
    claimed = _claimed_card_pin_from_env()
    if claimed is not None:
        model, provider = claimed
        provider = (str(provider or "")).strip().lower() or None
        model = (str(model or "")).strip() or None
        _card_pin_cache[task_id] = (model, provider)
        return model, provider
    model = provider = None
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_provider_health import model_override

        conn = kb.connect()
        try:
            task = kb.get_task(conn, task_id)
        finally:
            conn.close()
        if task is not None:
            model, provider = model_override(task)
    except Exception:
        logger.debug("kanban card pin lookup failed for %s", task_id, exc_info=True)
        return None, None
    provider = (provider or "").strip().lower() or None
    model = (model or "").strip() or None
    _card_pin_cache[task_id] = (model, provider)
    return model, provider


def card_pinned_provider() -> Optional[str]:
    """The provider pinned on THIS worker's card row, else None."""
    return card_pinned_route()[1]


def _norm(value) -> str:
    return str(value or "").strip().lower()


def refuse_runtime_failover(agent, to_provider, to_model, reason=None) -> bool:
    """True when a card-pinned worker must NOT fail over to ``to_provider``.

    Refuses only when (a) the card pins a provider and/or a model, (b) this
    run is actually serving on that pin (a dispatch fallback rung may have
    moved it), and (c) the fallback target is a different pinned provider OR,
    when the pin names a model, a different model (t_1d2ba891; a model-only
    pin allows the same model on any provider). Same-provider entries for the
    pinned model (another key on the lane) stay allowed, and so does the
    sibling Claude pool for the identical model when the pin is a pool face
    (claude-bpr <-> claude-apr); that swap is recorded by the normal
    ``worker_route_substituted`` (stage=runtime) event. The first refusal
    per agent writes one ``worker_route_pin_refused`` run event and marks the
    agent so a failed result exits retry-preserving
    (:func:`apply_pin_refusal_to_result`).
    """
    pinned_model, pinned = card_pinned_route()
    if not pinned and not pinned_model:
        return False
    primary = getattr(agent, "_primary_runtime", None)
    primary = primary if isinstance(primary, dict) else {}
    serving = str(primary.get("provider") or getattr(agent, "provider", "") or "").strip().lower()
    if pinned and serving != pinned:
        return False
    serving_model = primary.get("model") or getattr(agent, "model", None)
    # A pin with a model pins the MODEL too (t_1d2ba891): claude-bpr's chain
    # swapped a claude-fable-5-1 pin onto claude-opus-5-5 on the same lane.
    # Only while the run actually serves the pinned model; a run spawned on
    # another model (dispatch rung) is off the model pin, like the provider
    # check above, and keeps the provider-only rule.
    if pinned_model and _norm(serving_model) != _norm(pinned_model):
        if not pinned:
            return False
        pinned_model = None
    model_ok = not pinned_model or _norm(to_model) == _norm(pinned_model)
    if model_ok:
        if not pinned:
            return False  # model-only pin: the same model on any provider
        if _norm(to_provider) == pinned:
            return False
        if _is_sibling_pool_same_model(pinned, to_provider, serving_model, to_model):
            return False
    reason_value = str(getattr(reason, "value", reason) or "") or None
    if not isinstance(getattr(agent, "_kanban_pin_refused_failover", None), dict):
        agent._kanban_pin_refused_failover = {
            "provider": pinned or serving, "to_provider": to_provider, "reason": reason_value,
        }
        record_worker_route_pin_refused(
            provider=pinned or serving, model=getattr(agent, "model", None),
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


# ---------------------------------------------------------------------------
# Live route switch: ``hermes kanban set-model <card> ... --live`` (t_033a3bb1)
# ---------------------------------------------------------------------------
#
# ``set-model --live`` writes the new route onto the card AND, in the same
# transaction, a ``route_changed`` event scoped to the card's live run. The
# worker's conversation loop calls :func:`apply_pending_live_route` at the top
# of every iteration (the turn boundary between two provider calls). When a new
# event exists for this run, the worker re-reads the CARD ROW (the authority),
# re-applies the same gates as at write/spawn time, and swaps the provider
# in-process through ``AIAgent.switch_model`` -- the mid-session ``/model``
# path, which already round-trips history across vendors. The process, the
# message list and the workspace are untouched. An effort-only change only
# replaces ``agent.reasoning_config`` (a per-request parameter).

LIVE_ROUTE_SWITCHED_EVENT = "route_switched"
LIVE_ROUTE_SWITCH_REFUSED_EVENT = "route_switch_refused"
# One indexed board read per provider call (the loop boundary); negligible
# next to the call itself, and it keeps "switches at the NEXT iteration" exact.
_live_state: dict = {"agent": None, "cursor": 0}


def _effort_label(reasoning_config) -> Optional[str]:
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return "none"
    return reasoning_config.get("effort") or None


def _route_snapshot(agent) -> dict:
    return {
        "provider": getattr(agent, "provider", None) or None,
        "model": getattr(agent, "model", None) or None,
        "effort": _effort_label(getattr(agent, "reasoning_config", None)),
    }


def _live_route_gate_error(conn, task, model, provider) -> Optional[str]:
    """The write/spawn gates, re-applied to the route about to go live.

    Flagship: a flagship model needs the card's ``flagship override:`` comment
    (the dispatcher's spawn gate). Pin: a single-sub route needs the card's
    ``--pin-sub`` reason and an admitted sub; pre-rename aliases stay refused
    (the write gate). ``set-model`` already enforced both before writing, so a
    refusal here means the board changed underneath (comment deleted, sub
    disabled) and the worker must not drift onto an unauthorized route.
    """
    if not model:
        return None
    from hermes_cli.model_policy import (
        FLAGSHIP_OVERRIDE_COMMENT_PREFIX,
        flagship_model_match,
        validate_route_provider,
    )

    try:
        from hermes_cli.config import load_config

        policy_config = load_config()
    except Exception:
        policy_config = {}
    if flagship_model_match(model, policy_config):
        authorized = conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? "
            "AND lower(ltrim(body)) LIKE ? LIMIT 1",
            (task.id, f"{FLAGSHIP_OVERRIDE_COMMENT_PREFIX}%"),
        ).fetchone()
        if not authorized:
            return f"flagship model {model!r} has no 'flagship override:' comment on the card"
    try:
        validate_route_provider(model, provider, pin_sub_reason=getattr(task, "pin_sub_reason", None))
    except ValueError as exc:
        return str(exc)
    return None


def _bound_live_agent(agent) -> bool:
    """Only the worker's own top-level agent follows the card route.

    Delegation children, review forks and compaction helpers run their own
    ``run_conversation`` in the same process; the first top-level agent to
    poll is the worker's and is the only one ever switched.
    """
    if getattr(agent, "_delegate_depth", 0):
        return False
    import weakref

    ref = _live_state.get("agent")
    bound = ref() if ref is not None else None
    if bound is None:
        _live_state.update(agent=weakref.ref(agent), cursor=0)
        return True
    return bound is agent


def _refuse_live(task_id, run_id, event_id, reason, target) -> None:
    logger.warning(
        "PHASE=kanban_worker_route_switch_refused task=%s run=%s event=%s reason=%s",
        task_id, run_id, event_id, reason,
    )
    _append_run_event(LIVE_ROUTE_SWITCH_REFUSED_EVENT, {
        "event_id": event_id, "to": target, "reason": str(reason)[:300],
    })


def _coalesce_live_events(task, rows):
    """Fold every pending ``route_changed`` event into one requested change.

    Returns ``(touch_model, touch_effort, model, provider, effort)``. Each
    event only asks for the fields it wrote (``touch_model`` /
    ``touch_effort``), and the value is the one THAT live write set (its
    payload snapshot), the latest pending event per field winning. A card
    field written without ``--live`` therefore never goes live on the back of
    an unrelated live event. Events without the touch flags (hand-written /
    pre-flag rows) fall back to the card row for both fields.
    """
    from hermes_cli.kanban_provider_health import model_override

    touch_model = touch_effort = False
    model_pair = (task.model_override, task.provider_override)
    effort = task.reasoning_effort
    for row in rows:
        try:
            payload = json.loads(row[1] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if "touch_model" not in payload and "touch_effort" not in payload:
            touch_model = touch_effort = True
            model_pair = (task.model_override, task.provider_override)
            effort = task.reasoning_effort
            continue
        if payload.get("touch_model"):
            touch_model = True
            model_pair = (payload.get("model"), payload.get("provider"))
        if payload.get("touch_effort"):
            touch_effort = True
            effort = payload.get("reasoning_effort")
    model, provider = (None, None)
    if touch_model and model_pair[0]:
        model, provider = model_override(SimpleNamespace(
            model_override=model_pair[0], provider_override=model_pair[1],
        ))
    return touch_model, touch_effort, model, provider, (effort if touch_effort else None)


def mark_live_route_unsupported(agent, *, reason: str) -> None:
    """Record that this worker's runtime cannot switch routes in place.

    The ``codex_app_server`` runtime hands the whole turn to a subprocess and
    never reaches the loop-boundary poll, so a ``set-model --live`` would be
    accepted and never applied. The run-scoped marker makes the write side
    stop promising a live switch for this run (the write lands as
    next-dispatch), and any live event already pending is refused on the
    board rather than silently dropped. Never raises.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id or not _is_owning_worker() or not _bound_live_agent(agent):
        return
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or 0) or None
    except (TypeError, ValueError):
        run_id = None
    if run_id is None:
        return
    if not _write_live_unsupported_marker(task_id, run_id, reason):
        _start_marker_retry(task_id, run_id, reason)


# Backoff (seconds) for re-writing a marker whose first write failed; the
# last delay repeats until the write lands or the process exits.
_MARKER_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)


def _start_marker_retry(task_id: str, run_id: int, reason: str) -> None:
    """Keep retrying a failed ``route_live_unsupported`` write in the background.

    The app-server turn runs for the rest of the run with no loop boundary, so
    this is the marker's only other chance. Until it lands the write side may
    still accept a ``--live`` write; the marker's pending sweep refuses any
    such event on the board once the write succeeds. One thread per process.
    """
    import threading

    if _live_state.get("marker_retry") is not None:
        return

    def _retry() -> None:
        attempt = 0
        while True:
            delay = _MARKER_RETRY_DELAYS[min(attempt, len(_MARKER_RETRY_DELAYS) - 1)]
            attempt += 1
            threading.Event().wait(delay)
            if _write_live_unsupported_marker(task_id, run_id, reason):
                logger.warning(
                    "PHASE=kanban_live_route_unsupported_marked task=%s run=%s attempt=%s",
                    task_id, run_id, attempt,
                )
                return

    thread = threading.Thread(target=_retry, name="kanban-live-route-marker", daemon=True)
    _live_state["marker_retry"] = thread
    thread.start()


def _write_live_unsupported_marker(task_id: str, run_id: int, reason: str) -> bool:
    """Write the run's ``route_live_unsupported`` marker; True once it is on the board."""
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                marked = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? "
                    "AND kind = ? LIMIT 1",
                    (task_id, run_id, kb.ROUTE_LIVE_UNSUPPORTED_EVENT),
                ).fetchone()
                if not marked:
                    kb._append_event(conn, task_id, kb.ROUTE_LIVE_UNSUPPORTED_EVENT,
                                     {"reason": reason}, run_id=run_id)
                pending = conn.execute(
                    "SELECT id FROM task_events WHERE task_id = ? AND run_id = ? "
                    "AND kind = ? AND id > ? ORDER BY id",
                    (task_id, run_id, kb.ROUTE_CHANGED_EVENT, int(_live_state["cursor"])),
                ).fetchall()
                for row in pending:
                    kb._append_event(conn, task_id, LIVE_ROUTE_SWITCH_REFUSED_EVENT, {
                        "event_id": int(row[0]), "reason": reason,
                    }, run_id=run_id)
                if pending:
                    _live_state["cursor"] = int(pending[-1][0])
        finally:
            conn.close()
    except Exception:
        logger.warning("kanban live route unsupported mark failed for %s; retrying",
                       task_id, exc_info=True)
        return False
    return True


def apply_pending_live_route(agent, *, iteration: int, active_system_prompt=None):
    """Apply a pending ``set-model --live`` to this worker; loop-boundary hook.

    Returns the system prompt the loop should use from here on: unchanged
    unless the model/provider switched, in which case only its ``Model:`` /
    ``Provider:`` identity lines are rewritten (the same rewrite a provider
    failover does). Never raises: a failed poll or switch leaves the worker on
    its current route and is recorded as ``route_switch_refused``.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id or not _is_owning_worker() or not _bound_live_agent(agent):
        return active_system_prompt
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or 0) or None
    except (TypeError, ValueError):
        run_id = None
    if run_id is None:
        return active_system_prompt
    # Snapshot this run's card pin before its first provider call, so a later
    # next-dispatch ``set-model`` (no ``--live``) never changes the failover
    # policy of the run already in flight; only a live switch below moves it.
    card_pinned_route()

    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            rows = conn.execute(
                "SELECT id, payload FROM task_events WHERE task_id = ? AND run_id = ? "
                "AND kind = ? AND id > ? ORDER BY id",
                (task_id, run_id, kb.ROUTE_CHANGED_EVENT, int(_live_state["cursor"])),
            ).fetchall()
            if not rows:
                return active_system_prompt
            event_id = int(rows[-1][0])
            task = kb.get_task(conn, task_id)
            if task is None or task.current_run_id != run_id:
                _live_state["cursor"] = event_id  # handled: not this run's card
                return active_system_prompt
            touch_model, touch_effort, model, provider, effort = _coalesce_live_events(task, rows)
            target = {"provider": provider or None, "model": model or None, "effort": effort or None}
            gate_error = _live_route_gate_error(conn, task, model, provider) if touch_model else None
        finally:
            conn.close()
    except Exception:
        # The cursor did NOT move: the pending event(s) are retried at the
        # next iteration instead of being lost for the rest of this run.
        logger.debug("kanban live route poll failed for %s", task_id, exc_info=True)
        return active_system_prompt
    # Every exit below records its outcome (switched / refused / already on
    # it), so the events are handled from here on.
    _live_state["cursor"] = event_id

    before = _route_snapshot(agent)
    from hermes_constants import parse_reasoning_effort  # noqa: E402

    new_reasoning = parse_reasoning_effort(effort) if effort else None
    # Only the fields a pending live event asked for: a next-dispatch model
    # write on the card must not ride along with an effort-only live event.
    route_changes = touch_model and bool(model) and (
        model != (agent.model or "")
        or bool(provider) and provider.strip().lower() != (agent.provider or "").strip().lower()
    )
    effort_changes = (
        touch_effort and bool(effort)
        and new_reasoning != getattr(agent, "reasoning_config", None)
    )
    # The model and effort halves are separate live writes: a refused model
    # change is recorded on its own and a valid effort change still applies.
    model_target = dict(target, effort=None) if effort_changes else target
    if gate_error:
        _refuse_live(task_id, run_id, event_id, gate_error, model_target)
        route_changes = False
    elif touch_model and model and not route_changes:
        # A live pin onto the route already serving switches nothing but
        # still pins this run from here on.
        _card_pin_cache[task_id] = (
            model.strip() or None, (provider or "").strip().lower() or None,
        )
    if not route_changes and not effort_changes:
        return active_system_prompt  # already on it (e.g. written before spawn)

    prompt_before = refusal = None
    if route_changes:
        try:
            from hermes_cli.model_switch import switch_model as resolve_switch

            api_key = agent.api_key if isinstance(getattr(agent, "api_key", ""), str) else ""
            result = resolve_switch(
                raw_input=model,
                current_provider=agent.provider or "",
                current_model=agent.model or "",
                current_base_url=agent.base_url or "",
                current_api_key=api_key,
                explicit_provider=provider or "",
                probe_catalog=False,
            )
            if not result.success:
                refusal = result.error_message or "route did not resolve"
            else:
                prompt_before = getattr(agent, "_cached_system_prompt", None)
                extra = {"session_reasoning_config": new_reasoning} if touch_effort and effort else {}
                agent.switch_model(
                    new_model=result.new_model,
                    new_provider=result.target_provider,
                    api_key=result.api_key,
                    base_url=result.base_url,
                    api_mode=result.api_mode,
                    **extra,
                )
        except Exception as exc:  # switch_model rolls the agent back itself
            refusal = f"{exc.__class__.__name__}: {exc}"
        if refusal is not None:
            _refuse_live(task_id, run_id, event_id, refusal, model_target)
            if not effort_changes:
                return active_system_prompt
            route_changes = False
    if route_changes:
        # switch_model drops the cached prompt so the NEXT session turn
        # rebuilds it; mid-loop, keep it byte-stable apart from the identity
        # lines (the failover rewrite), which is what the loop is sending.
        from agent.chat_completion_helpers import _rewrite_identity_lines

        if isinstance(prompt_before, str) and prompt_before:
            agent._cached_system_prompt = _rewrite_identity_lines(
                prompt_before, agent.model, agent.provider,
            )
        if isinstance(active_system_prompt, str) and active_system_prompt:
            active_system_prompt = _rewrite_identity_lines(
                active_system_prompt, agent.model, agent.provider,
            )
    else:
        agent.reasoning_config = new_reasoning
        primary = getattr(agent, "_primary_runtime", None)
        if isinstance(primary, dict):
            primary["reasoning_config"] = dict(new_reasoning) if new_reasoning else None

    if route_changes:
        # The pin moves with a live route switch and ONLY with it: re-snapshot
        # from the route now serving, not from a later (next-dispatch) row.
        _card_pin_cache[task_id] = (
            (agent.model or model or "").strip() or None,
            (agent.provider or "").strip().lower() or None if provider else None,
        )
    after = _route_snapshot(agent)
    logger.warning(
        "PHASE=kanban_worker_route_switched task=%s run=%s iteration=%s %s -> %s",
        task_id, run_id, iteration, before, after,
    )
    _append_run_event(LIVE_ROUTE_SWITCHED_EVENT, {
        "event_id": event_id,
        "from": before,
        "to": after,
        "iteration": int(iteration),
        "kind": "route" if route_changes else "effort",
    })
    return active_system_prompt
