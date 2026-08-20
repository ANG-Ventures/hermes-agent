"""Pool read-phase 504 → fail over on the FIRST hit, never burn retries.

Option A of the 2026-08-19 rotation ruling (pool-side half = claude-pool
77a755a). The pool splits its timeout by phase: connect-phase failures now
rotate internally and surface as synthetic 429s, so a 504 reaching the
harness means specifically "a box ACCEPTED the request and then stalled" —
and the pool has already spent its entire internal retry budget (whole-turn
deadline, 300s default) terminally failing this turn without rotating
(rotating mid-generation would re-bill the same expensive turn on a second
sub). Retrying the SAME provider from the harness both re-buys the stall and
re-bills a fresh generation attempt; measured 2026-08-19, three kanban runs
died to exactly this (3/3 identical 504 retries, 251-311s each; 123/6,724
routed attempts = 1.83% fleet rate).

Chosen classification: ``pool_stalled`` — retryable=False +
should_fallback=True (the decode_error / stale-circuit-breaker shape), scoped
by the pool's EXACT body strings so a non-pool 504 is untouched. Both pool
bodies ("upstream attempt timed out" = per-attempt stall, "pool deadline
exceeded" = whole-turn budget blown) get the same treatment: they differ only
in where the pool's budget went, and in both cases the pool has terminally
failed the turn.

Double-bill analysis (why should_fallback does NOT reproduce the cost the
pool's own no-rotate policy avoids): the pool declines to rotate while the
stalled generation might still complete — its output could still be served.
By the time the harness holds a 504 the pool has CLOSED the request; the
stalled generation's output is unreachable and its cost sunk regardless of
what the harness does next. Every recovery path (retry same provider,
fallback, give up and re-run the task) pays for exactly one new generation;
fallback just buys it from a provider that is answering.
"""

from agent.error_classifier import FailoverReason, classify_api_error


class MockAPIError(Exception):
    """Simulates an OpenAI SDK APIStatusError."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


class TestPoolReadPhase504:
    """The two pool 504 bodies, on both classifier surfaces."""

    def test_attempt_timeout_504_with_status(self):
        """Per-attempt stall (box accepted, then idled past per-attempt
        timeout). Was: server_error via the 5xx floor → retryable=True,
        should_fallback=False → 3 retries against the stalled provider."""
        e = MockAPIError(
            '{"error":"upstream attempt timed out"}', status_code=504
        )
        result = classify_api_error(e, provider="claude-apr", model="claude-fable-5")
        assert result.reason == FailoverReason.pool_stalled
        assert result.retryable is False
        assert result.should_fallback is True
        assert result.should_rotate_credential is False

    def test_pool_deadline_504_with_status(self):
        """Whole-turn budget blown (request_deadline_s). Same terminal state:
        the pool exhausted its internal retry budget on this turn."""
        e = MockAPIError(
            '{"error":"pool deadline exceeded"}', status_code=504
        )
        result = classify_api_error(e, provider="claude-bpr", model="claude-opus-4-8")
        assert result.reason == FailoverReason.pool_stalled
        assert result.retryable is False
        assert result.should_fallback is True
        assert result.should_rotate_credential is False

    def test_attempt_timeout_status_only_in_message(self):
        """Same two-surface split as pool_exhausted: the classification must
        be identical whether or not the 504 survives onto the exception
        object. Was: timeout via _TIMEOUT_MESSAGE_PATTERNS → retryable=True,
        should_fallback=False."""
        e = MockAPIError(
            'HTTP 504: {"error":"upstream attempt timed out"}', status_code=None
        )
        result = classify_api_error(e, provider="claude-apr", model="claude-fable-5")
        assert result.reason == FailoverReason.pool_stalled
        assert result.retryable is False
        assert result.should_fallback is True
        assert result.should_rotate_credential is False

    def test_pool_deadline_status_only_in_message(self):
        e = MockAPIError(
            'HTTP 504: {"error":"pool deadline exceeded"}', status_code=None
        )
        result = classify_api_error(e, provider="claude-bpr", model="claude-fable-5")
        assert result.reason == FailoverReason.pool_stalled
        assert result.retryable is False
        assert result.should_fallback is True
        assert result.should_rotate_credential is False


class TestNegativeControls:
    """Load-bearing classifications that must NOT move."""

    def test_non_pool_504_stays_retryable_server_error(self):
        """A real gateway timeout from some other upstream (nginx/Cloudflare
        HTML page, no pool body string) keeps the historical 5xx-floor
        classification: retryable server_error, no forced failover. Pinned
        deliberately — the pool-stall scoping is the exact body strings, not
        the status code, so unrelated providers' 504s are untouched."""
        e = MockAPIError("<html>504 Gateway Time-out</html>", status_code=504)
        result = classify_api_error(e, provider="openrouter", model="deepseek/deepseek-v4")
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True
        assert result.should_fallback is False

    def test_non_pool_504_with_generic_timeout_wording_untouched(self):
        """nginx's actual wording is 'upstream timed out' — which does NOT
        contain 'upstream attempt timed out'. Must stay on the 5xx floor."""
        e = MockAPIError(
            "upstream timed out (110: Connection timed out) while reading "
            "response header from upstream",
            status_code=504,
        )
        result = classify_api_error(e, provider="custom", model="local-model")
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True
        assert result.should_fallback is False

    def test_503_no_eligible_sub_keeps_fallback(self):
        """Pool exhaustion (every sub quota-capped) keeps its classification:
        pool_exhausted, retryable, should_fallback=True, never rotate."""
        e = MockAPIError('{"error":"no eligible sub"}', status_code=503)
        result = classify_api_error(e, provider="claude-apr", model="claude-fable-5")
        assert result.reason == FailoverReason.pool_exhausted
        assert result.retryable is True
        assert result.should_fallback is True
        assert result.should_rotate_credential is False

    def test_429_rate_limit_keeps_fallback(self):
        """A plain 429 keeps rate_limit + should_fallback=True + rotation."""
        e = MockAPIError("Too Many Requests", status_code=429)
        result = classify_api_error(e)
        assert result.reason == FailoverReason.rate_limit
        assert result.retryable is True
        assert result.should_fallback is True
        assert result.should_rotate_credential is True


class TestFallbackAnnounceLabel:
    def test_pool_stalled_maps_to_honest_label(self):
        """The failover announce must say WHY: a stalled pool sub is neither a
        'connection issue' (the unmapped floor) nor 'sub pool capped'."""
        from agent.chat_completion_helpers import _fallback_reason_label

        assert (
            _fallback_reason_label(FailoverReason.pool_stalled)
            == "pool sub stalled mid-turn"
        )
