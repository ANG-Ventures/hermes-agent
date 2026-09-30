"""Hot-path glue for the Phase 2 fallback policy (spec rev12 §4.1-4.3, §4.8).

``agent/fallback_policy.py`` holds the policy; this module adapts it to a live
agent so the shared hot-path files (``chat_completion_helpers``,
``agent_runtime_helpers``, ``gateway/run.py``) only carry one-line calls:

* :func:`pre_failover` / :func:`post_failover` — class-gated quota gate and
  legacy arm, the sticky writer and the mid-turn ``fallback_failed`` return;
* :func:`restore_allowed` — THE one reader of ``_rate_limited_until`` and the
  sticky store for the restore / chain-refresh gates (``probe`` form per site);
* :func:`on_primary_return` — ``record_return`` + the ``recovery`` row fields;
* :func:`note_success` — ``last_primary_call_epoch``/seat and the fallback-side
  write-through;
* :func:`resume_sticky_fallback` / :func:`decide_rebuild_for_agent` — the
  gateway pre-run construction decision;
* config: ``fallback.sticky_policy`` (default true; false purges the table),
  ``model.announce_seat_names`` (default true).

Everything here is best-effort (I3): a store / DB / config failure never
raises into a turn; it degrades to today's behaviour.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, NamedTuple, Optional, Tuple

from agent import fallback_policy as fp
from agent import fallback_sticky_store as fss
from agent.fallback_sticky_store import StickyKey, StickyState

logger = logging.getLogger(__name__)

RELAY_PROVIDERS = fp.RELAY_PROVIDERS
PRIMARY_NOTE_MIN_INTERVAL_S = 30.0

_purge_lock = threading.Lock()
_purged = False
_note_lock = threading.Lock()
_last_primary_note: Dict[StickyKey, Tuple[float, Optional[str]]] = {}


# ── config ────────────────────────────────────────────────────────────────

def _raw_config() -> Dict[str, Any]:
    try:
        # Same reader as the announce gates (model.announce_*), so one config
        # source (and one test seam) governs the whole route-change path.
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config() or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def sticky_policy_enabled() -> bool:
    """``fallback.sticky_policy`` (default true, §7). The first ``false`` seen by
    this process purges the profile's ``fallback_sticky`` table (rollback,
    §4.2): flipping back must not revive days-old quota rows."""
    global _purged
    fb = _raw_config().get("fallback")
    enabled = True
    if isinstance(fb, dict) and "sticky_policy" in fb:
        enabled = bool(fb.get("sticky_policy"))
    if enabled:
        with _purge_lock:
            _purged = False
        return True
    with _purge_lock:
        if not _purged:
            _purged = True
            try:
                store().purge()
            except Exception:  # noqa: BLE001
                logger.warning("fallback_sticky purge on sticky_policy=false failed",
                               exc_info=True)
    return False


def warm_refusal_arm(key: StickyKey) -> bool:
    """Warm-seat spec §5 P3 A/B: ``fallback.warm_refusal_ab_pct`` (default 50)
    of sessions, hashed on the lineage root (stable across compaction)."""
    fb = _raw_config().get("fallback")
    pct: Any = fp.WARM_REFUSAL_AB_PCT_DEFAULT
    if isinstance(fb, dict) and "warm_refusal_ab_pct" in fb:
        pct = fb.get("warm_refusal_ab_pct")
    return fp.warm_refusal_arm(key.lineage_root, pct)


def local_relay_restart_wait_s() -> float:
    """``fallback.local_relay_restart_wait_s`` (default 20): how long a
    connection error on a loopback relay waits for the port to come back
    before it counts toward the conn-fallback policy. ``0`` disables."""
    from agent.retry_utils import LOCAL_RELAY_RESTART_WAIT_DEFAULT_S

    fb = _raw_config().get("fallback")
    val: Any = LOCAL_RELAY_RESTART_WAIT_DEFAULT_S
    if isinstance(fb, dict) and "local_relay_restart_wait_s" in fb:
        val = fb.get("local_relay_restart_wait_s")
    try:
        return max(float(val), 0.0)
    except (TypeError, ValueError):
        return LOCAL_RELAY_RESTART_WAIT_DEFAULT_S


def relay_drain_wait_s() -> float:
    """``fallback.relay_drain_wait_s`` (default 150, see
    ``retry_utils.RELAY_DRAIN_WAIT_DEFAULT_S``): how long a relay deploy-drain
    503 is waited out on the SAME model before the fallback chain. ``0``
    disables."""
    from agent.retry_utils import RELAY_DRAIN_WAIT_DEFAULT_S

    fb = _raw_config().get("fallback")
    val: Any = RELAY_DRAIN_WAIT_DEFAULT_S
    if isinstance(fb, dict) and "relay_drain_wait_s" in fb:
        val = fb.get("relay_drain_wait_s")
    try:
        return max(float(val), 0.0)
    except (TypeError, ValueError):
        return RELAY_DRAIN_WAIT_DEFAULT_S


def announce_seat_names() -> bool:
    model = _raw_config().get("model")
    if isinstance(model, dict) and "announce_seat_names" in model:
        return bool(model.get("announce_seat_names"))
    return True


def store() -> fss.StickyStore:
    return fss.default_store()


# ── identity ──────────────────────────────────────────────────────────────

def primary_route(agent: Any) -> Tuple[str, str]:
    rt = getattr(agent, "_primary_runtime", None)
    if isinstance(rt, dict) and rt.get("provider"):
        return str(rt.get("provider") or ""), str(rt.get("model") or "")
    return str(getattr(agent, "provider", "") or ""), str(getattr(agent, "model", "") or "")


def key_for(agent: Any) -> StickyKey:
    p, m = primary_route(agent)
    return StickyKey.build(fss.lineage_root_for_agent(agent), p, m)


def on_fallback(agent: Any) -> bool:
    """True when the live route is a fallback, not the primary."""
    if getattr(agent, "_fallback_activated", False) is not True:
        return False
    cur = fp._norm_pm(getattr(agent, "provider", ""), getattr(agent, "model", ""))
    return cur != fp._norm_pm(*primary_route(agent))


def _state(key: StickyKey) -> Optional[StickyState]:
    try:
        return fss.get(key, store())
    except Exception:  # noqa: BLE001 — StoreUnreadable or anything else (I3)
        return None


# ── eligibility (§4.3 Discovery) ─────────────────────────────────────────

def _eligibility_route(agent: Any):
    """Live ``(provider, model, base_url, sid)`` for the primary's ``/eligibility``
    query, or None when the primary is not a relay lane / has no base_url."""
    provider, model = primary_route(agent)
    if provider.strip().lower() not in RELAY_PROVIDERS:
        return None
    rt = getattr(agent, "_primary_runtime", None) or {}
    base_url = str(rt.get("base_url") or "")
    if not base_url:
        return None
    return provider, model, base_url, str(getattr(agent, "session_id", "") or "")


def _eligibility_fn(agent: Any):
    """At most one ``/eligibility`` GET per turn, relay lanes only. Non-relay
    primaries get None (warm_seat / fallback_failed fail closed).

    The per-agent cache outlives this call and compression rotates
    ``agent.session_id`` on the same agent, so the route (session / model /
    base_url) is resolved at fetch time and is part of the cache key, never
    captured once when the cache is first built."""
    if _eligibility_route(agent) is None:
        return None

    def _fetch():
        route = _eligibility_route(agent)
        if route is None:
            return None
        provider, model, base_url, sid = route
        if provider.strip().lower() == "claude-apr":
            return fp.fetch_eligibility(base_url, model=model, session=sid or None)
        return fp.fetch_eligibility(base_url, model=model,
                                    user=f"hermes-sess:{sid}" if sid else None)

    def _cached():
        turn = getattr(agent, "_current_turn_id", None)
        cache = getattr(agent, "_fallback_turn_eligibility", None)
        if not isinstance(cache, fp.TurnEligibility):
            cache = fp.TurnEligibility(_fetch)
            try:
                agent._fallback_turn_eligibility = cache
            except Exception:  # noqa: BLE001
                pass
        # A rotated session/route within the same turn (mid-turn compression)
        # must not reuse the old session's answer either.
        return cache.get((turn, _eligibility_route(agent)))

    return _cached


def _bench_fn(agent: Any, key: StickyKey, now: float):
    if not fp.is_direct_pin(key.primary_provider):
        return None
    rt = getattr(agent, "_primary_runtime", None) or {}
    fp_token = fp.token_fingerprint(rt.get("api_key") if isinstance(rt.get("api_key"), str) else None)
    return lambda: fp.direct_pin_benched(store(), key, now, token_fp=fp_token or None)


# ── the one restore reader ────────────────────────────────────────────────

def restore_allowed(agent: Any, *, probe: bool, now: Optional[float] = None) -> fp.Decision:
    """§4.2 "one restore predicate": ``max(sticky.until, _rate_limited_until)``
    plus the §4.3 branches. ``probe=False`` does no network I/O (chain
    refresh); ``probe=True`` adds ``warm_seat`` (turn boundary). The only
    reader of ``_rate_limited_until`` outside its writer."""
    rl_until = getattr(agent, "_rate_limited_until", 0) or 0
    if rl_until > time.monotonic():
        return fp.Decision(False, None, "cooldown")
    if not sticky_policy_enabled():
        return fp.Decision(True, None, "sticky_policy off")
    now = time.time() if now is None else now
    key = key_for(agent)
    state = _state(key)
    try:
        return fp.restore_allowed(
            state, now, probe=probe,
            live_session_id=str(getattr(agent, "session_id", "") or "") or None,
            primary_provider=key.primary_provider,
            eligibility=_eligibility_fn(agent) if probe else None,
            direct_pin_benched=_bench_fn(agent, key, now) if probe else None,
            refusal_arm=warm_refusal_arm(key) if probe else False,
        )
    except Exception:  # noqa: BLE001
        logger.debug("restore_allowed failed open", exc_info=True)
        return fp.Decision(True, None, "policy error (fail open)")


def on_primary_return(agent: Any, decision: Optional[fp.Decision],
                      now: Optional[float] = None) -> Dict[str, Any]:
    """After a successful restore: ``record_return`` and the recovery row's
    policy fields (empty dict when no sticky episode was active)."""
    if decision is None or not decision.branch:
        return {}
    try:
        now = time.time() if now is None else now
        key = key_for(agent)
        state = _state(key)
        if state is None or not state.active:
            return {}
        row = fp.recovery_row(state, decision, now,
                              live_session_id=str(getattr(agent, "session_id", "") or "") or None)
        fp.record_return(store(), key, now, decision.branch)
        return row
    except Exception:  # noqa: BLE001
        logger.debug("sticky return bookkeeping failed", exc_info=True)
        return {}


# ── failover ──────────────────────────────────────────────────────────────

class FailoverPlan(NamedTuple):
    cls: str
    window: Optional[str]
    skip_quota_gate: bool
    skip_legacy_arm: bool
    returned: bool = False
    retry_after_s: Optional[float] = None
    stated_reset_s: Optional[float] = None


LEGACY_PLAN = FailoverPlan("unclassified", None, False, False)


def _pending(agent: Any) -> Dict[str, Any]:
    p = getattr(agent, "_pending_fallback_error", None)
    return p if isinstance(p, dict) else {}


def failure_class(agent: Any, reason: Any) -> Tuple[str, Optional[str]]:
    """§4.1 class of the failing call (peeked, not consumed) after the
    direct-pin rule (``fp.lane_class``)."""
    from agent import fallback_events as fbe

    cls = fbe.pending_trigger_class(agent)
    if cls is None:
        cls = "refusal" if getattr(reason, "value", reason) == "content_policy_blocked" else "unclassified"
    return fp.lane_class(cls, getattr(agent, "provider", ""), _pending(agent).get("text"))


def pre_failover(agent: Any, reason: Any,
                 error_context: Optional[Dict[str, Any]] = None) -> FailoverPlan:
    """Top of ``try_activate_fallback``. Never raises (legacy plan on error)."""
    try:
        if not sticky_policy_enabled():
            return LEGACY_PLAN
        cls, window = failure_class(agent, reason)
        now = time.time()
        pend = _pending(agent)
        ra = None
        try:
            ra = float((pend.get("headers") or {}).get("retry-after"))
        except (TypeError, ValueError):
            ra = None
        reset = fp.stated_reset_from_context(error_context, now)
        if reset is None:
            reset = fp.parse_stated_reset_s(pend.get("text"), now)
        # §4.1 plumbing: apply_quota_gate AND the legacy arm block are gated
        # on class; only quota_model / rate_upstream / refusal / unclassified
        # reach their old behaviour.
        skip = fp.skips_quota_gate(cls)
        plan = FailoverPlan(cls, window, skip, skip, False, ra, reset)
        if on_fallback(agent):
            key = key_for(agent)
            state = _state(key)
            if state is not None and state.active:
                d = fp.fallback_failed_allowed(
                    state, cls, now, primary_provider=key.primary_provider,
                    eligibility=_eligibility_fn(agent),
                    direct_pin_benched=_bench_fn(agent, key, now))
                if d.allowed:
                    from agent.agent_runtime_helpers import restore_primary_runtime

                    if restore_primary_runtime(agent, _policy_decision=d,
                                               _failed_class=cls):
                        # The fallback's error is spent on this return; it
                        # must not be attributed to a later failover.
                        from agent import fallback_events as fbe

                        fbe.clear_pending(agent)
                        return plan._replace(returned=True)
        return plan
    except Exception:  # noqa: BLE001
        logger.debug("fallback policy pre_failover failed open", exc_info=True)
        return LEGACY_PLAN


def post_failover(agent: Any, plan: FailoverPlan, *, failing: Tuple[Any, Any],
                  fallback: Tuple[Any, Any], fallback_index: Optional[int]) -> Dict[str, Any]:
    """After an entry activated: arm the sticky writer (B1 guard inside
    ``fp.arm_sticky``). Returns the extra failover-row fields."""
    extra: Dict[str, Any] = {}
    if plan is LEGACY_PLAN or plan.cls not in fp.STICKY_CLASSES:
        return extra
    try:
        key = key_for(agent)
        if not key.lineage_root:
            return extra  # no session identity: nothing to key the episode on
        state = fp.arm_sticky(
            store(), key, cls=plan.cls, now=time.time(), failing=failing,
            fallback=fallback, fallback_index=fallback_index,
            stated_reset_s=plan.stated_reset_s, retry_after_s=plan.retry_after_s,
            window=plan.window)
        if state is not None:
            extra["sticky_until_epoch"] = state.until_epoch
            extra["cooldown_s"] = max(0.0, state.until_epoch - time.time())
    except Exception:  # noqa: BLE001
        logger.debug("sticky arm failed (best-effort)", exc_info=True)
    return extra


# ── success bookkeeping (§4.3 State) ─────────────────────────────────────

def note_success(agent: Any, headers: Optional[Dict[str, str]]) -> None:
    try:
        if not sticky_policy_enabled():
            return
        now = time.time()
        key = key_for(agent)
        if not key.lineage_root or not key.primary_provider:
            return
        if on_fallback(agent):
            fp.note_fallback_success(store(), key, now,
                                     str(getattr(agent, "session_id", "") or "") or None)
            return
        seat = fp.seat_from_response(getattr(agent, "provider", ""), headers)
        with _note_lock:
            last = _last_primary_note.get(key)
            if last and now - last[0] < PRIMARY_NOTE_MIN_INTERVAL_S and last[1] == seat:
                return
            _last_primary_note[key] = (now, seat)
            if len(_last_primary_note) > fss.MAX_ENTRIES:
                _last_primary_note.pop(next(iter(_last_primary_note)))
        fp.note_primary_success(store(), key, now, provider=getattr(agent, "provider", ""),
                                headers=headers)
    except Exception:  # noqa: BLE001
        logger.debug("sticky success note failed (best-effort)", exc_info=True)


def note_compaction(agent: Any) -> None:
    """§4.3: record a completed compaction (in place or rotating) on the
    active sticky episode. Best-effort, never raises."""
    try:
        if not sticky_policy_enabled():
            return
        key = key_for(agent)
        if not key.lineage_root or not key.primary_provider:
            return
        fp.note_compaction(store(), key, time.time())
    except Exception:  # noqa: BLE001
        logger.debug("sticky compaction note failed (best-effort)", exc_info=True)


# ── §4.2 construction-time decision ──────────────────────────────────────

_RESUME_UNTOUCHED = (
    "_rate_limited_until", "_rate_limit_backoff_count", "_last_fallback_event",
    "_last_fallback_announced", "_quota_gate_applied", "_pending_fallback_error",
    "_pending_stream_error_reason", "_quota_gate_skipped_providers",
)


def resume_sticky_fallback(agent: Any, state: StickyState) -> bool:
    """§4.2 state contract: put a rebuilt agent back on the stored fallback.

    Sets the fallback runtime, ``_fallback_index`` and ``_fallback_activated``
    (keeping the construction-time ``_primary_runtime`` snapshot for the later
    restore). Touches no cooldown, backoff counter, quota gate, pending reason,
    ``_last_fallback_event`` / ``_last_fallback_announced`` or ``until``; writes
    one ``sticky_resume`` row and no route-change line or announce.
    """
    rt = getattr(agent, "_primary_runtime", None)
    if not isinstance(rt, dict) or not rt.get("provider"):
        return False
    chain = list(getattr(agent, "_fallback_chain", None) or [])
    if not chain:
        return False
    idx, walked = fp.resume_target_index(chain, state)
    saved = {a: getattr(agent, a, None) for a in _RESUME_UNTOUCHED if hasattr(agent, a)}
    from_p, from_m = getattr(agent, "provider", None), getattr(agent, "model", None)
    agent._fallback_index = idx
    agent._sticky_resume_in_progress = True
    try:
        from agent.chat_completion_helpers import try_activate_fallback

        ok = bool(try_activate_fallback(agent, None))
    except Exception:  # noqa: BLE001
        logger.warning("resume_sticky_fallback activation failed; staying on primary",
                       exc_info=True)
        ok = False
    finally:
        agent._sticky_resume_in_progress = False
        for a in _RESUME_UNTOUCHED:
            if a in saved:
                setattr(agent, a, saved[a])
            elif hasattr(agent, a) and a in ("_last_fallback_event", "_last_fallback_announced"):
                setattr(agent, a, None)
    if not ok:
        agent._fallback_index = 0
        return False
    from agent import fallback_events as fbe

    fbe.record(agent, "sticky_resume", from_provider=from_p, from_model=from_m,
               to_provider=getattr(agent, "provider", None), to_model=getattr(agent, "model", None),
               reason="chain_head" if walked else None, consume=False,
               extra={"sticky_until_epoch": state.until_epoch, "trigger_class": state.cls})
    return True


def close_episodes_for_user_route(lineage_root: str,
                                  now: Optional[float] = None) -> int:
    """An explicit user route (``/model``) wins over an automatic one: close
    EVERY active sticky episode on this lineage, whatever primary it is keyed
    on (a later ``/model`` back to that primary must not resurrect it).
    Returns the number closed. Best-effort, never raises."""
    if not lineage_root:
        return 0
    closed = 0
    try:
        now = time.time() if now is None else now
        st = store()
        for key in st.keys_for_lineage(lineage_root):
            state = _state(key)
            if state is not None and state.active:
                fp.record_return(st, key, now, fp.USER_ROUTE_BRANCH)
                closed += 1
    except Exception:  # noqa: BLE001
        logger.debug("sticky user-route close failed (best-effort)", exc_info=True)
    return closed


def live_fallback_active(agent: Any) -> bool:
    """Whether a live agent is currently on an automatic fallback route."""
    if agent is None:
        return False
    return bool(getattr(agent, "_provider_fallback_active", False) is True
                or getattr(agent, "_fallback_activated", False) is True)


def decide_rebuild_for_agent(agent: Any, *, user_route: bool = False) -> str:
    """Gateway pre-run site for a FRESH agent, before ``_announce_reinit_recovery``.
    Returns ``primary`` | ``resume`` | ``return`` | ``store_unreadable``.

    ``return`` stashes the recovery-row fields on ``agent._sticky_recovery_row``;
    the re-init announce that runs next folds them into its one ledger row and
    its notice rider (one decision, one notice: G2).

    ``user_route`` (the user just ran ``/model``): close every active episode on
    the lineage and start on the requested primary, never resume."""
    try:
        if not sticky_policy_enabled():
            return "primary"
        now = time.time()
        key = key_for(agent)
        if not key.lineage_root:
            return "primary"
        if user_route:
            close_episodes_for_user_route(key.lineage_root, now)
            return "primary"
        sid = str(getattr(agent, "session_id", "") or "") or None
        rd = fp.decide_rebuild(store(), key, now, live_session_id=sid,
                               eligibility=_eligibility_fn(agent),
                               direct_pin_benched=_bench_fn(agent, key, now),
                               refusal_arm=warm_refusal_arm(key))
        if rd.action == "resume" and rd.state is not None:
            if rd.decision is not None:
                from agent import fallback_events as fbe

                fbe.record_restore_refused(agent, rd.decision.reason, extra={
                    **(rd.decision.warm or {}),
                    "gate_bound_expires_in_s": rd.decision.gate_bound_expires_in_s,
                    "sticky_until_epoch": rd.state.until_epoch,
                    "from_provider": rd.state.fallback_provider,
                    "from_model": rd.state.fallback_model})
            resumed = resume_sticky_fallback(agent, rd.state)
            if resumed and rd.decision is not None:
                # Turn-start restore would repeat this construction-time decision.
                agent._sticky_rebuild_refusal_pending = True
            return "resume" if resumed else "primary"
        if rd.action == "return" and rd.state is not None and rd.decision is not None:
            # rd.state is post-record_return (active=false); the row wants
            # the episode fields, which record_return keeps.
            agent._sticky_recovery_row = fp.recovery_row(rd.state, rd.decision, now,
                                                         live_session_id=sid)
        return rd.action
    except Exception:  # noqa: BLE001
        logger.warning("sticky rebuild decision failed; starting on primary", exc_info=True)
        return "primary"


def flush_unconsumed_recovery_row(agent: Any) -> None:
    """If the re-init announce did not consume the stashed row (no persisted
    identity to compare), still write the one ``recovery`` row."""
    row = getattr(agent, "_sticky_recovery_row", None)
    if not isinstance(row, dict):
        return
    agent._sticky_recovery_row = None
    from agent import fallback_events as fbe

    fbe.record(agent, "recovery", from_provider=row.get("from_provider"),
               from_model=row.get("from_model"), to_provider=row.get("to_provider"),
               to_model=row.get("to_model"), consume=False, extra=row)
