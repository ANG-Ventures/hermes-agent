"""One cause record per failover; a timer never becomes the cause (t_94a2b0e4).

Spec: ``plans/2026-10-18_fallback-announce-truth-SPEC.md`` §F1/§F2. On 2026-10-18
15:45 sub-vps-10 answered claude-alr with a 429 (Fable five_hour rejected). The
harness slept the 600 s Retry-After on the same lane, the no-first-byte wall
budget then cut the retry, and the banner was rendered from the CUT:
"pool sub stalled mid-turn ... no first byte in 614 s ... seat unknown — cut
before headers". Every claim was false. 45 of the 64 ``no_first_byte_budget``
rows of the 14 days before had the same shape.

The mechanism: ``fallback_events.stash_api_error`` keeps only the LATEST failing
call, so the timer's exception overwrote the 429, its headers (seat, route id)
and its class. This module keeps two things for the attempt block (the errors
on one lane between a served call, a failover and a turn start):

* :class:`FailoverCause`: captured at the FIRST provider error of the block and
  never overwritten by a later one. When the block ends on a harness timer
  (:data:`TIMER_EXCS`), the failover row is classified and rendered from it, and
  the wait is stated as a wait.
* the carry: the last relay attribution seen in the block (seat, route id, quota
  window), stamped on EVERY failover row, timer-ended ones included.

State lives on the agent (``_failover_cause``, ``_failover_carry``). Every
function here never raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Harness timers: they end a wait, they are never its cause.
TIMER_EXCS = frozenset({"FallbackWallBudgetExceeded"})

_SEAT_UNSTATED = frozenset(("", "unknown", "none"))
_ROUTE_HEADERS = ("x-pool-route-id", "x-relay-route-id")
# x-relay-quota-window (claude-pool, spec §F3) and the Anthropic unified claim.
_WINDOW_NAMES = {
    "five_hour": "5h", "5h": "5h",
    "seven_day": "7d", "7d": "7d",
    "seven_day_overage_included": "7d_oi", "7d_oi": "7d_oi",
    "seven_day_sonnet": "7d_sonnet", "7d_sonnet": "7d_sonnet",
}
_UNIFIED_STATUS = (("5h", "anthropic-ratelimit-unified-5h-status"),
                   ("7d", "anthropic-ratelimit-unified-7d-status"),
                   ("7d_oi", "anthropic-ratelimit-unified-7d_oi-status"),
                   ("7d_sonnet", "anthropic-ratelimit-unified-7d_sonnet-status"))


@dataclass
class FailoverCause:
    """The first provider error of an attempt block.

    ``evidence`` is that error's ``_pending_fallback_error`` dict, so the ledger
    classifies and renders it exactly as it would have had nothing overwritten it.
    ``slept_s`` counts the retry sleeps taken on the lane after it."""

    evidence: Dict[str, Any]
    reason: Optional[str]
    trigger_class: Optional[str]
    relay_class: Optional[str]
    http_status: Optional[int]
    seat: Optional[str]
    route_id: Optional[str]
    quota_window: Optional[str]
    at: float
    at_mono: float
    lane: Optional[str]
    slept_s: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def waited_s(self, now_mono: Optional[float] = None) -> float:
        return max(0.0, (time.monotonic() if now_mono is None else now_mono) - self.at_mono)


def _lower(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for k, v in (headers.items() if headers is not None else ()):
            out[str(k).lower()] = str(v)
    except Exception:  # noqa: BLE001
        pass
    return out


def quota_window(headers: Any, body: Any = None) -> Optional[str]:
    """``5h`` | ``7d`` | ``7d_oi`` | ``7d_sonnet`` the response named, else None.

    ``x-relay-quota-window`` first (the relay's own quota facts), then Anthropic's
    ``anthropic-ratelimit-unified-representative-claim``, then the first
    ``anthropic-ratelimit-unified-<w>-status: rejected``."""
    h = _lower(headers)
    for key in ("x-relay-quota-window", "anthropic-ratelimit-unified-representative-claim"):
        w = _WINDOW_NAMES.get((h.get(key) or "").strip().lower())
        if w:
            return w
    if isinstance(body, dict):
        w = _WINDOW_NAMES.get(str(body.get("relay_quota_window") or "").strip().lower())
        if w:
            return w
    for w, key in _UNIFIED_STATUS:
        if (h.get(key) or "").strip().lower() == "rejected":
            return w
    return None


def attribution(headers: Any, body: Any = None) -> Dict[str, Optional[str]]:
    """``{seat, route_id, quota_window}`` the relay stated on one response."""
    h = _lower(headers)
    seat = None
    for key in ("x-relay-seat", "x-pool-served-by"):
        v = (h.get(key) or "").strip()
        if v.lower() not in _SEAT_UNSTATED:
            seat = v
            break
    if seat is None and isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else {}
        v = str(body.get("relay_seat") or inner.get("relay_seat") or "").strip()
        if v.lower() not in _SEAT_UNSTATED:
            seat = v
    route = next((h[k].strip() for k in _ROUTE_HEADERS if (h.get(k) or "").strip()), None)
    answered = any(k.startswith(("x-relay-", "x-pool-")) for k in h)
    return {"seat": seat, "route_id": route, "quota_window": quota_window(headers, body),
            "relay_answered": "1" if answered else None}


def _carry(agent: Any, attr: Dict[str, Optional[str]]) -> None:
    lane = str(getattr(agent, "provider", "") or "") or None
    carry = getattr(agent, "_failover_carry", None)
    if not isinstance(carry, dict) or carry.get("lane") != lane:
        carry = {"lane": lane}   # another lane's seat is not this lane's
    for key in ("seat", "route_id"):
        if attr.get(key) and carry.get(key) and attr[key] != carry[key]:
            # A different seat/route answered: the old one's window/route is not
            # this answer's (Prism e20f57909f40).
            carry = {"lane": lane}
            break
    for k, v in attr.items():
        if v:
            carry[k] = v
    agent._failover_carry = carry


def note_error(agent: Any, pending: Dict[str, Any], api_error: Any, raw_headers: Any) -> None:
    """Called by ``stash_api_error`` for every failing call of the block.

    Updates the carry from this call's relay headers (a timer's ``pool_headers``
    included) and captures the cause on the first non-timer error."""
    try:
        body = pending.get("body")
        _carry(agent, attribution(raw_headers, body))
        exc = pending.get("exc")
        if exc in TIMER_EXCS:
            _carry(agent, attribution(getattr(api_error, "pool_headers", None)))
            return
        # t_b0a117bd: every failing attempt of the armed no-first-byte wait is
        # remembered on the wall clock, so its notice names the cause.
        from agent.fallback_wall_budget import note_upstream_failure

        note_upstream_failure(agent, status=pending.get("status"),
                              socket=pending.get("socket_cause"), exc_name=exc,
                              box=attribution(raw_headers, body)["seat"])
        if isinstance(getattr(agent, "_failover_cause", None), FailoverCause) \
                and agent._failover_cause.lane == (str(getattr(agent, "provider", "") or "") or None):
            return
        from agent import fallback_events as fbe

        cls, _src = fbe.classify_trigger(
            text=pending.get("text"), http_status=pending.get("status"),
            headers=pending.get("headers"), body=body, exc_name=exc,
            floor=pending.get("floor"))
        reason = None
        try:
            from agent.error_classifier import classify_api_error

            reason = classify_api_error(
                api_error, provider=str(getattr(agent, "provider", "") or ""),
                model=str(getattr(agent, "model", "") or "")).reason.value
        except Exception:  # noqa: BLE001
            logger.debug("failover cause: classify failed", exc_info=True)
        attr = attribution(raw_headers, body)
        agent._failover_cause = FailoverCause(
            evidence=dict(pending), reason=reason, trigger_class=cls,
            relay_class=(pending.get("headers") or {}).get("x-relay-error-class"),
            http_status=pending.get("status"), seat=attr["seat"],
            route_id=attr["route_id"], quota_window=attr["quota_window"],
            at=time.time(), at_mono=float(pending.get("at") or time.monotonic()),
            lane=str(getattr(agent, "provider", "") or "") or None)
    except Exception:  # noqa: BLE001
        logger.debug("failover cause: note failed", exc_info=True)


def note_sleep(agent: Any, seconds: Any, now_mono: Optional[float] = None) -> None:
    """A retry sleep on the lane is about to start (Retry-After / backoff): the
    wall-budget notice reads it instead of claiming a request is in flight."""
    try:
        cause = getattr(agent, "_failover_cause", None)
        if isinstance(cause, FailoverCause) and float(seconds) > 0:
            now = time.monotonic() if now_mono is None else now_mono
            cause.extra["sleep_until"] = now + float(seconds)
            cause.extra["sleep_s"] = float(seconds)
    except Exception:  # noqa: BLE001
        pass


def note_wait(agent: Any, seconds: Any) -> None:
    """A retry sleep was taken on the lane after the cause (Retry-After / backoff)."""
    try:
        cause = getattr(agent, "_failover_cause", None)
        if isinstance(cause, FailoverCause):
            cause.extra.pop("sleep_until", None)
            if float(seconds) > 0:
                cause.slept_s += float(seconds)
    except Exception:  # noqa: BLE001
        pass


def cause_text(agent: Any, cause: "FailoverCause") -> str:
    """``Fable 5h limit on sub-vps-10 (Anthropic 429)``: the cause phrase from the
    same table the failover rider uses (``fallback_policy._cause_phrase``)."""
    from agent import fallback_policy as fp

    ev = cause.evidence or {}
    row = {"trigger_class": cause.trigger_class, "http_status": cause.http_status,
           "err_head": str(ev.get("text") or "")[:400], "quota_window": cause.quota_window,
           "from_provider": cause.lane, "from_model": getattr(agent, "model", None),
           "seat": cause.seat}
    text = fp._cause_phrase(row)
    if cause.seat:
        text += f" on {cause.seat}"
    if cause.http_status:
        text += f" (Anthropic {cause.http_status})" if str(cause.lane or "").startswith("claude-") \
            else f" (HTTP {cause.http_status})"
    return text


def wait_notice(agent: Any, now_mono: Optional[float] = None) -> Optional[str]:
    """The wall-budget notice while the lane's attempt block holds a recorded
    error (t_94a2b0e4, Ace 2026-10-18 21:16): in a retry sleep it says it is
    waiting and why; on the retry after one it says so. None when the lane has
    no recorded cause (the notice keeps its in-flight wording)."""
    cause = peek(agent)
    lane = str(getattr(agent, "provider", "") or "") or None
    if cause is None or cause.lane != lane:
        return None
    now = time.monotonic() if now_mono is None else now_mono
    why = cause_text(agent, cause)
    until = cause.extra.get("sleep_until")
    if isinstance(until, (int, float)) and until > now:
        return (f"⏳ waiting {int(round(cause.extra.get('sleep_s') or (until - now)))} s: {why}, "
                f"retrying then falling back")
    route = f"{lane or '?'}/{getattr(agent, 'model', '') or '?'}"
    if cause.slept_s > 0:
        return f"⏳ retrying {route} after a {int(round(cause.slept_s))} s wait: {why}"
    return f"⏳ retrying {route}: {why}"


def peek(agent: Any) -> Optional[FailoverCause]:
    cause = getattr(agent, "_failover_cause", None)
    return cause if isinstance(cause, FailoverCause) else None


def effective(pending: Optional[Dict[str, Any]], cause: Optional[FailoverCause]) -> Optional[Dict[str, Any]]:
    """The evidence a failover is classified and rendered from: the cause when the
    block ended on a harness timer, else the last failing call."""
    if isinstance(pending, dict) and pending.get("exc") in TIMER_EXCS and cause is not None:
        return cause.evidence
    return pending


def take(agent: Any) -> tuple:
    """``(cause, carry)`` of the block, cleared (consumed by a failover row)."""
    cause, carry = peek(agent), getattr(agent, "_failover_carry", None)
    clear(agent)
    return cause, (carry if isinstance(carry, dict) else {})


def clear(agent: Any) -> None:
    try:
        agent._failover_cause = None
        agent._failover_carry = None
    except Exception:  # noqa: BLE001
        pass
