"""Session-hygiene compression deadline + repeated-failure escalation.

Two pure helpers, extracted so the policy is testable without standing up a
gateway:

``resolve_hygiene_timeout_seconds``
    The gateway wraps its pre-agent hygiene compression in an
    ``asyncio.wait_for`` wall-clock guard.  That guard used to default to a
    hardcoded **30 s** while the summariser call it wraps is handed
    ``auxiliary.compression.timeout`` (default 300 s, and the auxiliary client
    additionally applies a 300 s floor for the ``compression`` task).  An outer
    guard tighter than the inner deadline is not a safety net — it is a
    *guaranteed* kill for any summary that legitimately needs longer than the
    outer value, no matter how healthy the backend is.  Long-lived sessions
    (thousands of messages / hundreds of thousands of tokens) reliably land in
    that window, so hygiene compression can never succeed and the transcript
    grows without bound.

    Invariant enforced here: **the outer wall-clock guard is never tighter than
    the inner LLM deadline it wraps.**  An explicitly configured
    ``compression.hygiene_timeout_seconds`` still wins verbatim (operators may
    deliberately want a short guard, and the hermetic tests rely on it).

``should_alert_loudly`` / ``format_repeated_failure_alert``
    A hygiene failure is benign per-occurrence (nothing is dropped) but is
    *not* benign in aggregate: a session that can never compress grows until it
    overflows the model window.  After N consecutive failures the gateway stops
    whispering and says so plainly, with the knob to turn.
"""

from typing import Any, Mapping, Optional, Tuple

__all__ = [
    "DEFAULT_HYGIENE_TIMEOUT_SECONDS",
    "MAX_DERIVED_HYGIENE_TIMEOUT_SECONDS",
    "DEFAULT_HYGIENE_FAILURE_ALERT_AFTER",
    "resolve_hygiene_timeout_seconds",
    "should_alert_loudly",
    "format_repeated_failure_alert",
]

# Floor for the derived deadline. Never go *below* the historical default —
# a tiny auxiliary.compression.timeout must not make hygiene twitchier than it
# was before this change.
DEFAULT_HYGIENE_TIMEOUT_SECONDS = 30.0

# Ceiling for the DERIVED value only. An explicit hygiene_timeout_seconds is
# honoured verbatim above this; this cap only bounds what we infer on the
# operator's behalf, so a wild auxiliary.compression.timeout (say 86400) can
# never wedge every incoming gateway message behind a day-long await.
MAX_DERIVED_HYGIENE_TIMEOUT_SECONDS = 900.0

DEFAULT_HYGIENE_FAILURE_ALERT_AFTER = 3


def _positive_float(raw: Any) -> Optional[float]:
    """``raw`` as a float when it is a usable positive number, else ``None``."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if value <= 0:
        return None
    return value


def _as_mapping(raw: Any) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def resolve_hygiene_timeout_seconds(
    compression_cfg: Any,
    auxiliary_cfg: Any,
    default: float = DEFAULT_HYGIENE_TIMEOUT_SECONDS,
) -> Tuple[float, bool]:
    """Resolve the gateway hygiene wall-clock deadline.

    Args:
        compression_cfg: the ``compression`` config block (mapping or junk).
        auxiliary_cfg: the ``auxiliary`` config block (mapping or junk); the
            ``compression.timeout`` inside it is the inner LLM deadline.
        default: floor for the derived value (the historical 30 s).

    Returns:
        ``(seconds, explicit)`` where *explicit* is True when the operator set
        ``compression.hygiene_timeout_seconds`` themselves (in which case
        *seconds* is exactly their value, unclamped).
    """
    comp = _as_mapping(compression_cfg)

    explicit = _positive_float(comp.get("hygiene_timeout_seconds"))
    if explicit is not None:
        return explicit, True

    aux_compression = _as_mapping(_as_mapping(auxiliary_cfg).get("compression"))
    inner = _positive_float(aux_compression.get("timeout"))

    derived = float(default)
    if inner is not None:
        derived = max(derived, inner)
    return min(derived, MAX_DERIVED_HYGIENE_TIMEOUT_SECONDS), False


def resolve_failure_alert_after(compression_cfg: Any) -> int:
    """``compression.hygiene_failure_alert_after`` (default 3; 0 disables)."""
    raw = _as_mapping(compression_cfg).get("hygiene_failure_alert_after")
    if raw is None or isinstance(raw, bool):
        return DEFAULT_HYGIENE_FAILURE_ALERT_AFTER
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HYGIENE_FAILURE_ALERT_AFTER
    if value < 0:
        return DEFAULT_HYGIENE_FAILURE_ALERT_AFTER
    return value


def should_alert_loudly(consecutive_failures: int, alert_after: int) -> bool:
    """True on the Nth failure and every failure after it (0 = never alert)."""
    if alert_after <= 0:
        return False
    try:
        count = int(consecutive_failures)
    except (TypeError, ValueError):
        return False
    return count >= alert_after


def format_repeated_failure_alert(
    consecutive_failures: int,
    timeout_seconds: float,
    message_count: Optional[int] = None,
    approx_tokens: Optional[int] = None,
) -> str:
    """The loud, user-facing repeated-failure message.

    Deliberately states the growth consequence and names the exact config keys,
    because the quiet version of this failure is how a session grows until it
    overflows the window.
    """
    size_bits = []
    if message_count:
        size_bits.append(f"{message_count:,} messages")
    if approx_tokens:
        size_bits.append(f"~{approx_tokens:,} tokens")
    size = f" (now {', '.join(size_bits)})" if size_bits else ""

    return (
        f"🚨 Context compression has now failed {int(consecutive_failures)} times "
        f"in a row on this session{size}. No messages have been dropped, but the "
        "transcript is NOT shrinking and will keep growing until it overflows the "
        "model's context window.\n"
        f"The hygiene deadline is currently {float(timeout_seconds):.1f}s. Fix one of:\n"
        "• raise `compression.hygiene_timeout_seconds` in config.yaml (it now "
        "defaults to `auxiliary.compression.timeout`), then restart the gateway\n"
        "• run /compress manually — it has no wall-clock deadline\n"
        "• run /reset for a clean session\n"
        "• check `auxiliary.compression` (provider/model) is healthy and not "
        "rate-limited"
    )
