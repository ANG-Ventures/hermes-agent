"""Tests for agent.retry_utils jittered backoff."""

import threading

import agent.retry_utils as retry_utils
from types import SimpleNamespace

from agent.retry_utils import adaptive_rate_limit_backoff, is_zai_coding_overload_error, jittered_backoff


def test_backoff_is_exponential():
    """Base delay should double each attempt (before jitter)."""
    for attempt in (1, 2, 3, 4):
        delays = [jittered_backoff(attempt, base_delay=5.0, max_delay=120.0, jitter_ratio=0.0) for _ in range(100)]
        expected = min(5.0 * (2 ** (attempt - 1)), 120.0)
        mean = sum(delays) / len(delays)
        assert abs(mean - expected) < 0.01, f"attempt {attempt}: expected {expected}, got {mean}"


def test_backoff_respects_max_delay():
    """Even with high attempt numbers, delay should not exceed max_delay."""
    for attempt in (10, 20, 100):
        delay = jittered_backoff(attempt, base_delay=5.0, max_delay=60.0, jitter_ratio=0.0)
        assert delay <= 60.0, f"attempt {attempt}: delay {delay} exceeds max 60s"




def test_backoff_attempt_1_is_base():
    """First attempt delay should equal base_delay (with no jitter)."""
    delay = jittered_backoff(1, base_delay=3.0, max_delay=120.0, jitter_ratio=0.0)
    assert delay == 3.0








def test_backoff_thread_safety():
    """Concurrent calls should generally produce different delays."""
    results = []
    barrier = threading.Barrier(8)

    def _call_backoff():
        barrier.wait()
        results.append(jittered_backoff(1, base_delay=10.0, max_delay=120.0, jitter_ratio=0.5))

    threads = [threading.Thread(target=_call_backoff) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    unique = len(set(results))
    assert unique >= 6, f"Expected mostly unique delays, got {unique}/8 unique"


def test_backoff_uses_locked_tick_for_seed(monkeypatch):
    """Seed derivation should use per-call tick captured under lock."""
    import time

    monkeypatch.setattr(retry_utils, "_jitter_counter", 0)

    recorded_seeds = []

    class _RecordingRandom:
        def __init__(self, seed):
            recorded_seeds.append(seed)

        def uniform(self, a, b):
            return 0.0

    monkeypatch.setattr(retry_utils.random, "Random", _RecordingRandom)

    fixed_time_ns = 123456789

    def _time_ns_wait_for_two_ticks():
        deadline = time.time() + 2.0
        while retry_utils._jitter_counter < 2 and time.time() < deadline:
            time.sleep(0.001)
        return fixed_time_ns

    monkeypatch.setattr(retry_utils.time, "time_ns", _time_ns_wait_for_two_ticks)

    barrier = threading.Barrier(2)

    def _call():
        barrier.wait()
        jittered_backoff(1, base_delay=10.0, max_delay=120.0, jitter_ratio=0.5)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(recorded_seeds) == 2
    assert len(set(recorded_seeds)) == 2, f"Expected unique seeds, got {recorded_seeds}"


def _zai_overload_error():
    return SimpleNamespace(
        status_code=429,
        body={
            "error": {
                "code": "1305",
                "message": "The service may be temporarily overloaded, please try again later",
            }
        },
    )


def test_zai_overload_retry_ceiling_exceeds_short_attempts():
    """Invariant: the ceiling must sit above the short-retry threshold, or the
    long-backoff tier is unreachable and the whole schedule is dead code
    (the original bug: default api_max_retries == short_attempts == 3)."""
    from agent.retry_utils import (
        zai_coding_overload_retry_ceiling,
        _ZAI_CODING_OVERLOAD_LONG_BACKOFF,
    )

    short_attempts = 3
    ceiling = zai_coding_overload_retry_ceiling(short_attempts)
    assert ceiling > short_attempts
    # Invariant (not a formula mirror): the loop's give-up check
    # (retry_count >= ceiling) runs *before* the attempt's backoff, so the
    # ceiling must leave headroom for every long-backoff entry to execute —
    # i.e. the largest attempt the loop still computes backoff for
    # (ceiling - 1) must reach the final long-tier index.
    last_attempt_with_backoff = ceiling - 1
    assert last_attempt_with_backoff - short_attempts >= len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF)


def test_zai_overload_ceiling_makes_long_tier_reachable(monkeypatch):
    """End-to-end over the attempt range the retry loop actually walks: with the
    extended ceiling, at least one attempt reaches the long-backoff tier and the
    full 30/60/90/120s schedule is exercised."""
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *a, **kw: kw["base_delay"])
    from agent.retry_utils import zai_coding_overload_retry_ceiling

    err = _zai_overload_error()
    ceiling = zai_coding_overload_retry_ceiling()

    long_waits = []
    # The loop computes backoff for attempts 1..ceiling-1 (it gives up at ceiling).
    for attempt in range(1, ceiling):
        _wait, policy = adaptive_rate_limit_backoff(
            attempt,
            base_url="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.2",
            error=err,
            default_wait=1.0,
        )
        if policy == "zai_coding_overload_long":
            long_waits.append(_wait)

    assert long_waits, "long-backoff tier never reached within the retry ceiling"
    assert long_waits == [30.0, 60.0, 90.0, 120.0]


# --- resolve_retry_after: honor Retry-After on rate-limit AND overload -------
# SPEC 2026-07-07 (pool-at-capacity transient-503, 4b). The relay emits a bounded
# Retry-After on a pool-at-capacity 503; the harness honors it on the overloaded
# path (not just rate-limit), with a final-pre-fallback-retry carve-out.
from agent.retry_utils import (  # noqa: E402
    resolve_retry_after,
    RETRY_AFTER_CAP_OVERLOAD_S,
    RETRY_AFTER_CAP_RATE_LIMIT_S,
)


def _honor(**kw):
    base = dict(raw_value="8", is_rate_limit=False, is_overload=True,
                retry_count=0, max_retries=3)
    base.update(kw)
    return resolve_retry_after(**base)


def test_retry_after_honored_on_overload():
    # AC-3: an overload with a numeric Retry-After is honored (not None/jitter).
    assert _honor(raw_value="8") == 8.0


def test_retry_after_honored_on_rate_limit():
    assert _honor(is_rate_limit=True, is_overload=False, raw_value="12") == 12.0


def test_retry_after_ignored_for_other_reasons():
    # AC-4-adjacent: neither rate-limit nor overload -> never honored (jitter).
    assert _honor(is_rate_limit=False, is_overload=False, raw_value="8") is None


def test_retry_after_overload_capped_at_60():
    # INV-4: overload honors a tighter 60s cap.
    assert _honor(raw_value="999") == RETRY_AFTER_CAP_OVERLOAD_S == 60.0


def test_retry_after_rate_limit_capped_at_600():
    assert _honor(is_rate_limit=True, is_overload=False,
                  raw_value="99999") == RETRY_AFTER_CAP_RATE_LIMIT_S == 600.0


def test_retry_after_not_honored_on_final_pre_fallback_retry():
    # AC-8 / RC-1: reserve the LAST reachable retry for a fast jitter→fallback,
    # but ONLY when there are ≥2 reachable retries (max_retries ≥ 3). The caller
    # is 1-based (retry_count incremented before this runs) and activates
    # fallback at retry_count >= max_retries, so reachable retries are 1..N-1.
    # max_retries=3 -> reachable {1,2}: honor 1, jitter 2 (the last reachable).
    assert _honor(retry_count=1, max_retries=3) == 8.0   # 1 < 3-1=2 -> honor
    assert _honor(retry_count=2, max_retries=3) is None  # last reachable -> jitter
    # boundary: honor iff retry_count < max_retries-1 (when max_retries >= 3)
    assert _honor(retry_count=3, max_retries=5) == 8.0   # 3 < 5-1=4 -> honor
    assert _honor(retry_count=4, max_retries=5) is None  # last reachable -> jitter


def test_retry_after_honored_when_only_one_reachable_retry():
    # Greptile #223 P1: with max_retries=2 there is exactly ONE reachable retry
    # (retry_count=1); reserving it for jitter would make the whole overload
    # feature a no-op. So it MUST be honored.
    assert _honor(retry_count=1, max_retries=2) == 8.0


def test_retry_after_max_retries_1_never_honors():
    # max_retries=1: zero reachable retries (retry_count 1 >= max_retries 1) ->
    # never honored (straight to fallback).
    assert _honor(retry_count=1, max_retries=1) is None


def test_retry_after_http_date_falls_through_to_jitter():
    # A RFC-valid HTTP-date Retry-After is not float()-parseable -> None (jitter),
    # not a crash. Covers the pass-2 security-lens HTTP-date path.
    assert _honor(raw_value="Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_retry_after_garbage_and_missing_fall_through():
    assert _honor(raw_value="abc") is None
    assert _honor(raw_value=None) is None
    assert _honor(raw_value="") is None


def test_retry_after_nonpositive_falls_through():
    assert _honor(raw_value="0") is None
    assert _honor(raw_value="-5") is None

# ---------------------------------------------------------------------------
# parse_retry_after_seconds — shared Retry-After parser
# ---------------------------------------------------------------------------


class TestParseRetryAfterSeconds:
    def test_numeric_string(self):
        from agent.retry_utils import parse_retry_after_seconds
        assert parse_retry_after_seconds("120") == 120.0
        assert parse_retry_after_seconds(" 4.5 ") == 4.5

    def test_numeric_value(self):
        from agent.retry_utils import parse_retry_after_seconds
        assert parse_retry_after_seconds(45) == 45.0
        assert parse_retry_after_seconds(3.25) == 3.25


    def test_http_date(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        from agent.retry_utils import parse_retry_after_seconds

        future = datetime.now(timezone.utc) + timedelta(seconds=90)
        seconds = parse_retry_after_seconds(format_datetime(future, usegmt=True))
        assert seconds is not None and 80 <= seconds <= 91

        past = datetime.now(timezone.utc) - timedelta(seconds=90)
        assert parse_retry_after_seconds(format_datetime(past, usegmt=True)) == 0.0



    def test_headers_get_raises(self):
        from agent.retry_utils import parse_retry_after_seconds

        class Explosive:
            def get(self, _key):
                raise RuntimeError("boom")

        assert parse_retry_after_seconds(Explosive()) is None
