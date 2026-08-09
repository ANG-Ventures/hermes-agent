"""Regression: a corrupt/undecodable response body must fail over, not retry.

THE INCIDENT (2026-08-08). A proxy decoded a gzip response body but relayed the
upstream's now-false ``content-encoding: gzip``. httpx obeyed the header, tried
to gunzip plaintext, and raised

    DecodingError: Error -3 while decompressing data: incorrect header check

*before* the status line or body were parsed. The underlying error was a
perfectly clean Anthropic ``429 rate_limit_error`` — but the classifier never
saw it. ``DecodingError`` is not an ``OSError`` and matched no pattern, so it
fell to the ``unknown`` floor: ``retryable=True``, ``should_fallback=False``.

Consequence, measured from the live log: every rung of the fallback chain burned
its full 3-attempt retry budget (~13s per rung) against a byte-for-byte
identical, deterministic failure — ~90 seconds and 12+ attempts to walk
apx-1 -> apx-7 and land back where it started.

The fix classifies body-decode failures as their own reason:
``retryable=False`` (retrying reproduces it exactly), ``should_fallback=True``
(advance the chain on the FIRST hit), ``should_rotate_credential=False`` (the
key is fine; the bytes are not), and a distinct user-facing label so the
announce reads "corrupt response" rather than the misleading "connection issue".
"""

from __future__ import annotations

import zlib

import httpx
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.chat_completion_helpers import _fallback_reason_label


# The exact exception the fleet produced, kept verbatim.
LIVE_DECODE_MSG = "Error -3 while decompressing data: incorrect header check"


class TestDecodeErrorClassification:
    def test_httpx_decoding_error_is_decode_error(self):
        result = classify_api_error(httpx.DecodingError(LIVE_DECODE_MSG))
        assert result.reason is FailoverReason.decode_error

    def test_decode_error_does_not_retry_in_place(self):
        """The whole point: a deterministic corruption must not burn the budget."""
        result = classify_api_error(httpx.DecodingError(LIVE_DECODE_MSG))
        assert result.retryable is False, (
            "retrying a decode failure reproduces the identical bytes — this is "
            "what burned ~90s across the fallback chain on 2026-08-08"
        )

    def test_decode_error_advances_the_fallback_chain(self):
        result = classify_api_error(httpx.DecodingError(LIVE_DECODE_MSG))
        assert result.should_fallback is True

    def test_decode_error_never_rotates_a_credential(self):
        """The key is healthy — the response bytes are malformed."""
        result = classify_api_error(httpx.DecodingError(LIVE_DECODE_MSG))
        assert result.should_rotate_credential is False

    def test_bare_zlib_error_also_classifies(self):
        """Some SDKs re-raise without preserving the httpx type."""
        result = classify_api_error(zlib.error(LIVE_DECODE_MSG))
        assert result.reason is FailoverReason.decode_error
        assert result.retryable is False

    def test_label_is_honest_and_specific(self):
        """'connection issue' actively misled during the incident."""
        label = _fallback_reason_label(FailoverReason.decode_error)
        assert label == "corrupt response"
        assert label != "connection issue"


class TestNoCollateralDamage:
    """Negative controls — the narrow patterns must not swallow neighbours."""

    @pytest.mark.parametrize(
        "error",
        [
            httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("timed out"),
            httpx.ConnectTimeout("connect timed out"),
        ],
    )
    def test_real_transport_faults_stay_retryable_timeouts(self, error):
        """A genuine transport blip CAN succeed on retry — it must keep retrying."""
        result = classify_api_error(error)
        assert result.reason is FailoverReason.timeout
        assert result.retryable is True

    def test_pool_exhaustion_still_classifies_as_pool_exhausted(self):
        result = classify_api_error(Exception('Error code: 503 - {"error": "no eligible sub"}'))
        assert result.reason is FailoverReason.pool_exhausted

    def test_model_output_decode_failure_is_not_a_body_decode_error(self):
        """A JSON/model-output parse failure is a DIFFERENT bug with a different fix.

        Guards the '🔴 KEEP NARROW' contract on _DECODE_ERROR_PATTERNS: adding a
        generic token like 'decode' here would silently reclassify these.
        """
        result = classify_api_error(Exception("failed to decode json from the model output"))
        assert result.reason is not FailoverReason.decode_error

    def test_unrecognized_error_still_reaches_the_unknown_floor(self):
        result = classify_api_error(Exception("something totally unrecognized"))
        assert result.reason is FailoverReason.unknown


class TestRetryBudgetArithmetic:
    """The behavioural contract the incident actually violated.

    Not a unit-detail assertion: this encodes 'a deterministic failure costs ONE
    attempt per rung, not max_retries per rung', which is the property that turns
    a 90-second chain walk into an immediate honest answer.
    """

    def test_decode_error_costs_one_attempt_per_rung(self):
        max_retries = 3
        result = classify_api_error(httpx.DecodingError(LIVE_DECODE_MSG))
        attempts_per_rung = max_retries if result.retryable else 1
        assert attempts_per_rung == 1

        chain_depth = 7  # apx-1 .. apx-7, the live topology on 2026-08-08
        assert attempts_per_rung * chain_depth == 7, (
            "pre-fix this was 3 * 7 = 21 attempts against an identical "
            "deterministic corruption"
        )

    def test_transport_timeout_keeps_its_full_budget(self):
        """Negative control: we must NOT have made real blips fail fast."""
        result = classify_api_error(httpx.ConnectError("connection refused"))
        assert result.retryable is True
