"""Regression: a quota 429 must name WHICH window is exhausted (5h vs 7d).

Ace, 2026-08-08: "yeah it should say if its 7d-exhausted or 5hr-exhausted right?
do we do both now?" — we did neither. Every quota 429 rendered a flat, unqualified
"rate limit".

Anthropic tells us exactly which window bound, on the response headers. Captured
live from a 7d-exhausted sub on 2026-08-08::

    anthropic-ratelimit-unified-5h-status: allowed      <- 5h is FINE
    anthropic-ratelimit-unified-5h-utilization: 0.0
    anthropic-ratelimit-unified-7d-status: rejected     <- 7d is DEAD
    anthropic-ratelimit-unified-7d-utilization: 1.0
    anthropic-ratelimit-unified-representative-claim: seven_day
    retry-after: 178772                                 <- ~2 DAYS

These are OPPOSITE operational situations: 5h clears in under an hour, 7d can be
two days out. Collapsing both to "rate limit" hides the only detail that changes
what you do next.

The headers were always reachable — ``APIStatusError`` carries the full
``httpx.Response``, and ``extract_api_error_context`` already read
``retry-after``. It simply walked past the ``anthropic-ratelimit-unified-*``
family sitting beside it.
"""

from __future__ import annotations

import time

import pytest

from agent.agent_runtime_helpers import extract_api_error_context
from agent.chat_completion_helpers import _quota_window_suffix


# Verbatim capture from sub-vps-2, 2026-08-08 — a genuinely 7d-exhausted box.
LIVE_7D_HEADERS = {
    "anthropic-ratelimit-unified-status": "rejected",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-reset": "1786245600",
    "anthropic-ratelimit-unified-5h-utilization": "0.0",
    "anthropic-ratelimit-unified-7d-status": "rejected",
    "anthropic-ratelimit-unified-7d-utilization": "1.0",
    "anthropic-ratelimit-unified-representative-claim": "seven_day",
    "anthropic-ratelimit-unified-fallback-percentage": "0.5",
}


class _FakeResponse:
    def __init__(self, headers):
        self.headers = headers


class _FakeError(Exception):
    """Shaped like anthropic.RateLimitError: carries .response with headers."""

    def __init__(self, headers, message="rate limited"):
        super().__init__(message)
        self.response = _FakeResponse(headers)


class _FakeAgent:
    """Stand-in for the live agent; only needs the consume-once stamp slot."""

    _pending_quota_window: "dict | None" = None


def _headers_with_reset(window: str, seconds_out: float, claim: str):
    reset = str(int(time.time() + seconds_out))
    return {
        "anthropic-ratelimit-unified-representative-claim": claim,
        f"anthropic-ratelimit-unified-{window}-status": "rejected",
        f"anthropic-ratelimit-unified-{window}-reset": reset,
    }


class TestQuotaWindowExtraction:
    def test_seven_day_claim_is_recognized(self):
        ctx = extract_api_error_context(_FakeError(LIVE_7D_HEADERS))
        assert ctx["quota_window"] == "7d"

    def test_five_hour_claim_is_recognized(self):
        headers = dict(LIVE_7D_HEADERS)
        headers["anthropic-ratelimit-unified-representative-claim"] = "five_hour"
        ctx = extract_api_error_context(_FakeError(headers))
        assert ctx["quota_window"] == "5h"

    def test_falls_back_to_status_scan_without_a_claim_header(self):
        headers = {k: v for k, v in LIVE_7D_HEADERS.items()
                   if "representative-claim" not in k}
        ctx = extract_api_error_context(_FakeError(headers))
        assert ctx["quota_window"] == "7d", (
            "the 7d window reports rejected; 5h reports allowed"
        )

    def test_reset_epoch_is_captured(self):
        headers = _headers_with_reset("7d", 172800, "seven_day")
        ctx = extract_api_error_context(_FakeError(headers))
        assert "quota_window_reset" in ctx

    def test_retry_after_still_wins_for_reset_at(self):
        """Don't regress the existing cooldown source of truth."""
        headers = dict(LIVE_7D_HEADERS)
        headers["retry-after"] = "60"
        ctx = extract_api_error_context(_FakeError(headers))
        assert ctx["reset_at"] <= time.time() + 61


class TestNoHeadersNoChange:
    """Every non-Anthropic provider must render exactly what it rendered before."""

    def test_no_headers_yields_no_window(self):
        ctx = extract_api_error_context(Exception("plain rate limit"))
        assert "quota_window" not in ctx

    def test_unrelated_headers_yield_no_window(self):
        ctx = extract_api_error_context(_FakeError({"x-ratelimit-reset": "123"}))
        assert "quota_window" not in ctx

    def test_suffix_is_empty_when_nothing_stamped(self):
        assert _quota_window_suffix(_FakeAgent()) == ""


class TestSuffixRendering:
    def test_names_the_window(self):
        agent = _FakeAgent()
        agent._pending_quota_window = {"quota_window": "7d", "quota_window_reset": None}
        assert "7d limit" in _quota_window_suffix(agent)

    @pytest.mark.parametrize(
        "seconds_out,expected_unit",
        [(1800, "m"), (7200, "h"), (172800, "d")],
    )
    def test_reset_renders_in_a_sensible_unit(self, seconds_out, expected_unit):
        agent = _FakeAgent()
        agent._pending_quota_window = {
            "quota_window": "7d",
            "quota_window_reset": time.time() + seconds_out,
        }
        out = _quota_window_suffix(agent)
        assert "resets in" in out
        assert out.rstrip().endswith(expected_unit)

    def test_the_two_windows_render_differently(self):
        """The whole point — these must not be confusable."""
        a5, a7 = _FakeAgent(), _FakeAgent()
        a5._pending_quota_window = {"quota_window": "5h", "quota_window_reset": None}
        a7._pending_quota_window = {"quota_window": "7d", "quota_window_reset": None}
        assert _quota_window_suffix(a5) != _quota_window_suffix(a7)

    def test_stamp_is_consumed_once(self):
        """A stale window must never leak onto a later, unrelated failover."""
        agent = _FakeAgent()
        agent._pending_quota_window = {"quota_window": "7d", "quota_window_reset": None}
        assert _quota_window_suffix(agent) != ""
        assert _quota_window_suffix(agent) == "", "second read must be empty"

    def test_expired_reset_is_not_rendered(self):
        agent = _FakeAgent()
        agent._pending_quota_window = {
            "quota_window": "5h",
            "quota_window_reset": time.time() - 500,
        }
        out = _quota_window_suffix(agent)
        assert "5h limit" in out
        assert "resets in" not in out

    def test_garbage_window_is_ignored(self):
        agent = _FakeAgent()
        agent._pending_quota_window = {"quota_window": "banana"}
        assert _quota_window_suffix(agent) == ""


class TestLiveIncidentEndToEnd:
    def test_tonights_429_says_7d_not_just_rate_limit(self):
        """The exact headers from the 2026-08-08 incident, end to end."""
        ctx = extract_api_error_context(_FakeError(LIVE_7D_HEADERS))
        agent = _FakeAgent()
        agent._pending_quota_window = {
            "quota_window": ctx.get("quota_window"),
            "quota_window_reset": ctx.get("quota_window_reset"),
        }
        suffix = _quota_window_suffix(agent)
        assert "7d limit" in suffix
        assert "5h" not in suffix, "the 5h window was ALLOWED — naming it would mislead"
