"""Retry utilities — jittered backoff for decorrelated retries.

Replaces fixed exponential backoff with jittered delays to prevent
thundering-herd retry spikes when multiple sessions hit the same
rate-limited provider concurrently.
"""

import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# Monotonic counter for jitter seed uniqueness within the same process.
# Protected by a lock to avoid race conditions in concurrent retry paths
# (e.g. multiple gateway sessions retrying simultaneously).
_jitter_counter = 0
_jitter_lock = threading.Lock()

# Z.AI Coding Plan's GLM-5.2 endpoint often returns HTTP 429 code 1305
# ("The service may be temporarily overloaded...") for otherwise valid
# Hermes requests. Short retries tend to hammer the same overloaded window;
# after a few normal retries, progressively widen the wait window. Keep the
# cap interactive-friendly: a simple TUI message should fail visibly in minutes,
# not sit silent for 20+ minutes.
_ZAI_CODING_OVERLOAD_LONG_BACKOFF = (30.0, 60.0, 90.0, 120.0)

# Number of initial short retries before the adaptive long-backoff tier kicks
# in. Shared by ``adaptive_rate_limit_backoff`` (which walks the long table
# starting at attempt ``short_attempts + 1``) and
# ``zai_coding_overload_retry_ceiling`` (which sizes the retry loop so every
# long-tier entry is reachable). Keeping it a single module constant prevents
# the two from silently desyncing if the short-retry count is ever tuned.
_ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS = 3


def parse_retry_after_seconds(value_or_headers: Any) -> Optional[float]:
    """Parse a ``Retry-After`` value into non-negative seconds.

    Accepts either a raw header value (numeric string / HTTP-date / number)
    or a headers mapping, in which case the ``Retry-After`` key is looked up
    case-insensitively (``.get`` on dict-like objects tries both common
    casings; real HTTP header containers like httpx/requests are already
    case-insensitive).

    Returns:
        Seconds as a ``float`` (negative deltas clamped to ``0.0``), or
        ``None`` when the header is absent or unparseable.
    """
    raw = value_or_headers
    if raw is not None and not isinstance(raw, (str, int, float)):
        # Looks like a headers mapping — pull the header out of it.
        getter = getattr(raw, "get", None)
        if callable(getter):
            try:
                value = getter("Retry-After")
                if value is None:
                    value = getter("retry-after")
            except Exception:
                return None
            raw = value
        else:
            return None
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    # HTTP-date form (RFC 7231): seconds until that instant, clamped at 0.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Base delay in seconds for attempt 1.
        max_delay: Maximum delay cap in seconds.
        jitter_ratio: Fraction of computed delay to use as random jitter
            range.  0.5 means jitter is uniform in [0, 0.5 * delay].

    Returns:
        Delay in seconds: min(base * 2^(attempt-1), max_delay) + jitter.

    The jitter decorrelates concurrent retries so multiple sessions
    hitting the same provider don't all retry at the same instant.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    # Seed from time + counter for decorrelation even with coarse clocks.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def _error_text(error: Any) -> str:
    """Best-effort flattened provider error text for retry classification."""
    parts = [
        error,
        getattr(error, "message", None),
        getattr(error, "body", None),
        getattr(error, "response", None),
    ]
    return " ".join(str(part) for part in parts if part is not None).lower()


def is_zai_coding_overload_error(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """Return True for Z.AI Coding Plan transient overload 429s.

    The coding-plan endpoint reports overload as HTTP 429 with body code 1305
    and message "The service may be temporarily overloaded...". Treat only
    that narrow shape specially so ordinary quota/billing 429s still fail fast
    through the existing classifier.
    """
    base = (base_url or "").lower()
    model_name = (model or "").lower()
    status = getattr(error, "status_code", None)
    text = _error_text(error)
    return (
        status == 429
        and "api.z.ai/api/coding/paas/v4" in base
        and "glm-5.2" in model_name
        and ("1305" in text or "temporarily overloaded" in text)
    )


def adaptive_rate_limit_backoff(
    attempt: int,
    *,
    base_url: str | None,
    model: str | None,
    error: Any,
    default_wait: float,
    short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS,
) -> tuple[float, str | None]:
    """Provider-aware rate-limit backoff.

    For most providers this returns ``default_wait`` unchanged. For Z.AI
    Coding Plan GLM-5.2 overloads, keep the first ``short_attempts`` retries on
    the normal short exponential schedule, then switch to progressively longer
    waits (30s → 60s → 90s → 120s, capped) plus light jitter.

    ``attempt`` is 1-based, matching the retry loop's logged attempt number.
    Returns ``(wait_seconds, reason_label)`` where ``reason_label`` is suitable
    for status/log decoration when a provider-specific policy fired.
    """
    if not is_zai_coding_overload_error(base_url=base_url, model=model, error=error):
        return default_wait, None
    if attempt <= short_attempts:
        return default_wait, "zai_coding_overload_short"

    idx = min(attempt - short_attempts - 1, len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) - 1)
    base_delay = _ZAI_CODING_OVERLOAD_LONG_BACKOFF[idx]
    # A smaller jitter ratio keeps long waits readable while still avoiding
    # synchronized retry storms across concurrent Hermes sessions.
    return jittered_backoff(1, base_delay=base_delay, max_delay=base_delay, jitter_ratio=0.2), "zai_coding_overload_long"


# Cap for a honored Retry-After, by class. A rate-limit reset window can be
# minutes (Anthropic Tier-1 input buckets reset ~171s, some providers longer),
# so 600s. A provider OVERLOAD / local-relay backpressure transient clears in
# seconds, so a much tighter 60s cap is sufficient and safer.
RETRY_AFTER_CAP_RATE_LIMIT_S = 600.0
RETRY_AFTER_CAP_OVERLOAD_S = 60.0


def _seat_recovers_within(seat_recovery_seconds: Any, wait: float) -> bool:
    """True when a pool-reported seat recovery lands inside ``wait`` seconds.

    ``None`` (the credential pool's documented "no wait information" value on
    ``next_available_at``) and any unparseable/negative-infinite value are
    treated as NOT recovering in time: on an exhausted seat the safe default is
    to release the caller toward its fallback chain, not to sleep blind.
    A non-positive recovery means the seat is already due back → in time.
    """
    if seat_recovery_seconds is None or isinstance(seat_recovery_seconds, bool):
        return False
    try:
        recovery = float(seat_recovery_seconds)
    except (TypeError, ValueError):
        return False
    if recovery != recovery:  # NaN
        return False
    return recovery <= wait


def resolve_retry_after(
    *,
    raw_value: Any,
    is_rate_limit: bool,
    is_overload: bool,
    retry_count: int,
    max_retries: int,
    seat_exhausted: bool = False,
    seat_recovery_seconds: float | None = None,
) -> float | None:
    """Decide whether to honor a server ``Retry-After`` and for how long.

    Pure function (no I/O) so the honor-policy is unit-testable in isolation
    from the conversation loop. Returns the number of seconds to wait, or
    ``None`` to fall through to the caller's jittered backoff.

    Policy:
      * Honor only for a rate-limit OR a provider overload (503/529). A
        pool-at-capacity 503 from the local relay is a self-describing
        transient that emits a bounded ``Retry-After``; honoring it beats
        blind jitter. Any other reason → ``None`` (jitter).
      * The caller activates its fallback chain at ``retry_count >=
        max_retries``, so the honor-reachable retries are ``1 .. max_retries-1``.
        Reserve the LAST reachable retry for jitter (so the fast direct-box
        fallback isn't delayed by a relay-informed wait) — but ONLY when there
        are at least TWO reachable retries to spare one. When ``max_retries==2``
        there is a single reachable retry; skipping it would make the whole
        overload feature a no-op, so it IS honored (Greptile #223 P1).
      * A non-numeric value (e.g. an HTTP-date ``Retry-After``, RFC-valid but
        not a bare number) is NOT parsed here → ``None`` (jitter). This is
        deliberate: overload/backpressure sources emit numeric seconds; date
        parsing would be scope creep.
      * ``seat_exhausted`` / ``seat_recovery_seconds`` — the caller's credential
        pool has ALREADY declared the seat this request rode exhausted and has
        nothing to rotate to, and (optionally) says how many seconds until that
        seat re-enters rotation. On a ``rate_limit`` this makes the server's
        ``Retry-After`` meaningless: the window it advertises is a per-request
        throttle hint, while the seat's real reset can be a day-plus out
        (measured 2026-09-16: a 7-day-capped sub-vps seat sent Retry-After=600
        with ~31h until reset). Sleeping the full cap guarantees the retry
        re-429s on the same dead seat and burns the caller's whole budget
        before the fallback chain is reached — 3 cron sessions, 0 output.
        So a rate-limit on an exhausted seat → ``None`` (jitter, then
        fallback), EXCEPT when the pool reports a recovery that lands within
        the honored wait: there the seat genuinely does come back inside the
        window, so honoring it still beats leaving the primary.
        ``seat_recovery_seconds=None`` means "no recovery information" (the
        pool's own contract for ``next_available_at``) and is treated as
        beyond the window — fail toward the chain, not toward a blind sleep.
        Scoped to ``rate_limit`` deliberately. An OVERLOAD Retry-After stays
        honored even on an exhausted seat: overload is a transient about the
        SERVER's capacity, not the seat's quota, its cap is already a tight
        60s, and the relay's bounded hint still beats blind jitter.
      * The honored value is clamped to a class-specific cap
        (rate-limit 600s, overload 60s) and floored at 0.

    ``retry_count`` is the caller's 1-based attempt number (incremented before
    this runs); ``max_retries`` is the retry ceiling.
    """
    if not (is_rate_limit or is_overload):
        return None
    # Past/at the fallback threshold → don't honor (defensive; the caller
    # normally activates fallback before reaching here).
    if retry_count >= max_retries:
        return None
    # Reserve the final reachable retry for a fast jitter→fallback, but only
    # when there are ≥2 reachable retries (max_retries ≥ 3) so we don't spend
    # our only reachable retry and disable the feature (Greptile #223 P1).
    if max_retries >= 3 and retry_count >= max_retries - 1:
        return None
    if raw_value in (None, ""):
        return None
    try:
        secs = float(raw_value)
    except (TypeError, ValueError):
        return None
    if secs <= 0:
        return None
    cap = RETRY_AFTER_CAP_RATE_LIMIT_S if is_rate_limit else RETRY_AFTER_CAP_OVERLOAD_S
    wait = min(secs, cap)
    # A rate-limit Retry-After on a seat the pool has already benched is a hint
    # about a window we are no longer waiting on — honoring it sleeps the full
    # cap and wakes to the same dead seat. Fall through to jitter so the caller
    # reaches its fallback chain on THIS 429 instead of ~10 minutes later. The
    # carve-out: a pool-reported recovery landing inside the wait we were about
    # to serve anyway. Overload is excluded entirely (see docstring).
    if is_rate_limit and seat_exhausted and not _seat_recovers_within(
        seat_recovery_seconds, wait
    ):
        return None
    return wait


# ── Pool-capacity (``pool_exhausted``) retry policy ───────────────────────
#
# Our claude-apr/-bpr relays answer 503 ``{"error":"no eligible sub ..."}``
# when every pooled sub is quota-reserved / capped for the requested model.
# That is a CAPACITY signal about the pool, not an auth or transport fault —
# and, crucially, leaving the provider on the first hit is not free: a
# ``fallback_providers`` switch is a HOST MOVE for a bridge-backed
# conversation, and the receiving box has no CLI session for it, so the bridge
# replays the entire history (measured 2026-09-24: 168 replays of 64k-830k
# chars on ONE sub in 10h, 52 of them inside 70 minutes, every one a session
# that left the pool the moment it saw this 503). Before this policy the loop
# gave a pool 503 the generic 2s/4s jitter and walked the chain after ~6s.
#
# Policy: stay on the SAME provider and wait for a seat, bounded by an
# attempt count (``agent.capacity_retry_attempts``) and a wall-clock budget
# (``agent.capacity_retry_max_wait_s``). Honour the relay's ``Retry-After``
# when it fits the remaining budget; a hint LONGER than the remaining budget
# means the pool has told us it will not free in time, so leave now instead
# of sleeping a wait we cannot afford. Without a header, use a slower jitter
# than the generic path (seats free on the order of tens of seconds, not
# two) clamped to the remaining budget. ``attempts == 0`` disables the policy
# (the loop's pre-existing behaviour is untouched).
CAPACITY_RETRY_DEFAULT_ATTEMPTS = 3
CAPACITY_RETRY_DEFAULT_MAX_WAIT_S = 90.0
CAPACITY_RETRY_BASE_DELAY_S = 5.0
CAPACITY_RETRY_MAX_DELAY_S = 30.0


def capacity_retry_wait(
    *,
    retry_count: int,
    max_retries: int,
    raw_retry_after: Any,
    waited_s: float,
    max_wait_s: float,
    base_delay: float = CAPACITY_RETRY_BASE_DELAY_S,
    max_delay: float = CAPACITY_RETRY_MAX_DELAY_S,
) -> Optional[float]:
    """Seconds to wait before the next SAME-provider attempt on a pool 503.

    Pure function (no I/O). Returns ``None`` when the capacity budget is spent
    and the caller should fall back NOW:

      * ``retry_count >= max_retries`` — attempt budget exhausted.
      * ``waited_s >= max_wait_s`` — wall-clock budget exhausted.
      * a numeric ``Retry-After`` larger than the remaining budget — the relay
        says the seat frees later than we are willing to wait.

    Otherwise the wait is the relay's numeric ``Retry-After`` (when present and
    positive) or a jittered backoff, and in both cases never exceeds the
    remaining budget. ``retry_count`` is the caller's 1-based attempt number
    (incremented before this runs), matching :func:`resolve_retry_after`.
    """
    if retry_count >= max_retries:
        return None
    try:
        remaining = float(max_wait_s) - float(waited_s)
    except (TypeError, ValueError):
        return None
    if remaining <= 0:
        return None
    hinted: Optional[float] = None
    if raw_retry_after not in (None, ""):
        try:
            secs = float(raw_retry_after)
        except (TypeError, ValueError):
            secs = None
        if secs is not None and secs > 0:
            hinted = secs
    if hinted is not None:
        if hinted > remaining:
            return None
        return hinted
    wait = jittered_backoff(retry_count, base_delay=base_delay, max_delay=max_delay)
    return min(wait, remaining)


def zai_coding_overload_retry_ceiling(short_attempts: int = _ZAI_CODING_OVERLOAD_SHORT_ATTEMPTS) -> int:
    """Retry-loop ceiling needed for the full Z.AI overload backoff schedule.

    The adaptive policy runs ``short_attempts`` short retries, then walks the
    long-backoff table one entry per subsequent attempt. The retry loop gives
    up as soon as ``retry_count >= ceiling`` — and that check runs *before* the
    attempt's backoff is computed — so the ceiling must sit one past the final
    long-backoff entry for every long tier to actually execute.

    With the default ``api_max_retries`` (3) equal to ``short_attempts`` (3),
    the loop always gave up before reaching the long tier, leaving the whole
    long-backoff schedule as dead code. Callers extend the ceiling to this
    value for Z.AI Coding overload 429s so the 30/60/90/120s waits run.
    """
    return short_attempts + len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) + 1
