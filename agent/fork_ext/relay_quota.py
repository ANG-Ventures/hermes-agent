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
