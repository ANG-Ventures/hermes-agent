"""Remote relay quota evidence for the main turn's existing exhaustion path."""
import datetime as _dt
from typing import Any, Mapping, Optional

from agent.error_classifier import FailoverReason
from agent.fallback_events import relay_error_class
from agent.fork_ext.relay_headers import _POOL_CAPABILITY_PROVIDERS
from agent.retry_utils import RETRY_AFTER_CAP_RATE_LIMIT_S, parse_retry_after_seconds


def relay_quota_outlasts_retry_window(agent, error, classified) -> bool:
    """A structured quota on a known pool relay cannot recover within our wait policy.

    Local relay credentials are not remote seats. Do not rotate them or shorten
    the server's wait and retry the exhausted route. The caller exhausts this
    attempt block instead, preserving the native fallback walk and terminal error.
    Classification wins over quota-looking text/headers (notably deploy drain).
    """
    if (getattr(agent, "provider", "") or "").strip().lower() not in _POOL_CAPABILITY_PROVIDERS:
        return False
    if classified.reason not in (FailoverReason.rate_limit, FailoverReason.pool_exhausted):
        return False
    headers = getattr(getattr(error, "response", None), "headers", None)
    retry_after = parse_retry_after_seconds(headers)
    if retry_after is None or not retry_after > RETRY_AFTER_CAP_RATE_LIMIT_S:
        return False
    relay_class, _ = relay_error_class(error)
    if relay_class in ("quota_seat", "quota_model"):
        return True
    # Legacy passthrough 429s retain Anthropic's actual quota-window headers.
    # A generic 429, long Retry-After, or "quota" substring alone proves nothing.
    # An explicit non-quota relay class takes precedence over upstream evidence,
    # except rate_upstream: older relays assigned it from Anthropic's "exceed
    # your account's rate limit" wording even over a rejected 5h window
    # (2026-10-18 15:45, t_97c3a1b1), so the window header decides there too.
    if (relay_class not in (None, "upstream_passthrough", "rate_upstream")
            or getattr(error, "status_code", None) != 429):
        return False
    return any(
        str(name).lower() in {
            "anthropic-ratelimit-unified-5h-status",
            "anthropic-ratelimit-unified-7d-status",
            "anthropic-ratelimit-unified-7d_oi-status",
            "anthropic-ratelimit-unified-7d_sonnet-status",
        }
        and str(value).strip().lower() == "rejected"
        for name, value in (headers.items() if headers is not None else ())
    )


# t_634b4ddf: a relay "pool at capacity" refusal for a Fable request while the
# relay's own Fable allowance header says at most this many seats have allowance
# left is the per-model allowance, not concurrency saturation. 2026-10-28 20:02:
# 25/26 seats capped, the one left busy, banner read "relay busy: all subs at
# capacity". With 10 eligible seats all saturated the refusal stays "relay busy".
FABLE_CAP_MAX_LEFT = 2


def fable_cap_busy(row: Mapping[str, Any]) -> Optional[dict]:
    """``{left, of, earliest_reset}`` when a ``pool_pressure`` refusal arrived with
    ``x-pool-fable-allowance`` (parsed onto the row as ``fable_allowance``)
    stating at most :data:`FABLE_CAP_MAX_LEFT` seats with Fable allowance left,
    else None. Reads only what the refusal carried. Never raises."""
    try:
        if row.get("trigger_class") != "pool_pressure":
            return None
        fa = row.get("fable_allowance")
        if not isinstance(fa, Mapping):
            return None
        n, m = fa.get("exhausted"), fa.get("of")
        if (not isinstance(n, int) or not isinstance(m, int) or isinstance(n, bool)
                or n <= 0 or m <= 0 or n > m):
            return None
        left = m - n
        elig = row.get("pool_eligible")
        if left > FABLE_CAP_MAX_LEFT or (isinstance(elig, int) and elig > FABLE_CAP_MAX_LEFT):
            # A wide eligible set that is all busy is concurrency, not the cap.
            return None
        return {"left": left, "of": m, "earliest_reset": fa.get("earliest_reset")}
    except Exception:  # noqa: BLE001
        return None


def fable_cap_busy_text(cap: Mapping[str, Any], tz: Optional[_dt.tzinfo] = None) -> str:
    """``1 of 26 seats has Fable allowance left and it is busy; earliest reset Thu 17:00``."""
    left, of = int(cap["left"]), int(cap["of"])
    if left == 0:
        text = f"0 of {of} seats have Fable allowance left"
    elif left == 1:
        text = f"1 of {of} seats has Fable allowance left and it is busy"
    else:
        text = f"{left} of {of} seats have Fable allowance left and all are busy"
    reset = cap.get("earliest_reset")
    if isinstance(reset, (int, float)) and not isinstance(reset, bool):
        text += f"; earliest reset {_dt.datetime.fromtimestamp(float(reset), tz).strftime('%a %H:%M')}"
    return text


# t_b0c67f3e: a pool-wide refusal states the numbers it was decided on
# (claude-pool ``pool_pressure_headers``). The gateway renders them; it never
# asks /health. Headers absent (an older relay) -> None -> today's wording.
POOL_PRESSURE_HEADERS = ("x-pool-seats-total", "x-pool-seats-eligible", "x-pool-seats-busy",
                         "x-pool-inflight", "x-pool-inflight-max",
                         "x-pool-ineligible-reasons")


def _count(v: Any) -> Optional[int]:
    s = str(v if v is not None else "").strip()
    return int(s) if s.isdigit() else None


def pool_pressure(headers: Any) -> Optional[dict]:
    """``{total, eligible, busy, inflight, inflight_max, ineligible}`` from the
    refusal's ``x-pool-*`` headers, else None. ``total`` and ``eligible`` are
    required; ``busy`` is None when the relay had no fresh box reading;
    ``ineligible`` is ``[(reason, n), ...]`` largest first. Junk -> None.
    Never raises."""
    try:
        h = {str(k).lower(): v for k, v in (headers.items() if headers is not None else ())}
        total, elig = _count(h.get("x-pool-seats-total")), _count(h.get("x-pool-seats-eligible"))
        if total is None or elig is None or total <= 0 or elig > total:
            return None
        busy = _count(h.get("x-pool-seats-busy"))
        if busy is not None and busy > elig:
            busy = None
        reasons = []
        for i, part in enumerate(str(h.get("x-pool-ineligible-reasons") or "").split(",")):
            name, _, n = part.strip().partition(":")
            if name.strip() and _count(n):
                reasons.append((i, name.strip(), int(n)))
        reasons.sort(key=lambda r: (-r[2], r[0]))
        return {"total": total, "eligible": elig, "busy": busy,
                "inflight": _count(h.get("x-pool-inflight")),
                "inflight_max": _count(h.get("x-pool-inflight-max")),
                "ineligible": [(name, n) for _i, name, n in reasons]}
    except Exception:  # noqa: BLE001
        return None


def pool_pressure_text(pp: Mapping[str, Any]) -> str:
    """``relay busy: 11 of 11 eligible seats full (26 in pool; 15 ineligible:
    quota 13, transport 2), 142/304 in flight``."""
    total, elig, busy = int(pp["total"]), int(pp["eligible"]), pp.get("busy")
    if isinstance(busy, int):
        head = f"relay busy: {busy} of {elig} eligible seats full"
    else:
        head = f"relay busy: {elig} of {total} seats eligible"
    inel = total - elig
    paren = f"{total} in pool" if isinstance(busy, int) else ""
    if inel > 0:
        why = ", ".join(f"{name} {n}" for name, n in pp.get("ineligible") or ())
        tail = f"{inel} ineligible" + (f": {why}" if why else "")
        paren = f"{paren}; {tail}" if paren else tail
    text = f"{head} ({paren})" if paren else head
    n, m = pp.get("inflight"), pp.get("inflight_max")
    if isinstance(n, int) and isinstance(m, int) and m > 0:
        text += f", {n}/{m} in flight"
    return text
