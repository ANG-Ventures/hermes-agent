"""The pool-exhaustion fallback label must name the MODEL and the SCOPE.

Card t_555aae48. Ace saw::

    🔄 Model fallback (sub pool capped): claude-apr/claude-opus-5 -> claude-apx-1/claude-opus-5

and asked, reasonably, "if the issue is haiku, why does that matter when I want
opus?" — the label names neither the model that was capped nor the fact that
caps are per-(sub x model), so the only inference a reader can draw is "the
whole pool is out". That is false whenever a single model's budget is what ran
out, and it libels an engine that is behaving correctly.

The pool distinguishes the two scopes at the no-eligible return (it re-runs the
same eligibility predicate with the model filter dropped) and says so in the
response body. These tests pin the harness half: the scope survives into the
announce, narrows the label, and never leaks onto an unrelated failover.
"""

from __future__ import annotations

import pytest

from agent.chat_completion_helpers import (
    _FALLBACK_REASON_LABELS,
    _fallback_reason_label,
    _pool_scope_label,
)
from agent.error_classifier import (
    _POOL_MODEL_SCOPED_PATTERN,
    FailoverReason,
    classify_api_error,
)


# Verbatim from claude_pool_relay.py's model-scoped no-eligible return.
POOL_MODEL_SCOPED_BODY = (
    '{"error":"no eligible sub for the requested model; this model\'s budget '
    'is capped on every subscription while other models are unaffected"}'
)
POOL_FLEET_BODY = '{"error":"no eligible sub"}'


class _Agent:
    """Minimal stand-in — the helpers only read/clear a stamp attribute."""

    _pending_pool_scope: "str | None" = None


class MockAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Classification is UNCHANGED — only the label narrows.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [POOL_MODEL_SCOPED_BODY, POOL_FLEET_BODY])
def test_both_scopes_still_classify_as_pool_exhausted(body):
    """The narrower body must not fall out of the pool_exhausted bucket — that
    would re-open the old 'provider overloaded' / 'connection issue' mislabels
    the generic pattern exists to prevent."""
    result = classify_api_error(MockAPIError(body, status_code=503))
    assert result.reason == FailoverReason.pool_exhausted
    assert result.should_fallback is True
    assert result.should_rotate_credential is False


def test_model_scoped_body_is_matched_without_the_status_code():
    """Same two-surface rule as the generic pattern: the body must classify
    identically whether or not the 503 survives onto the exception."""
    result = classify_api_error(
        MockAPIError(f"HTTP 503: {POOL_MODEL_SCOPED_BODY}", status_code=None))
    assert result.reason == FailoverReason.pool_exhausted


def test_model_scoped_pattern_is_a_substring_of_the_generic_one():
    """Structural guard: if someone edits the pool body so the narrow pattern
    stops implying the generic one, classification silently regresses."""
    assert "no eligible sub" in _POOL_MODEL_SCOPED_PATTERN
    assert _POOL_MODEL_SCOPED_PATTERN in POOL_MODEL_SCOPED_BODY.lower()


# --------------------------------------------------------------------------- #
# The label itself.
# --------------------------------------------------------------------------- #
def test_flat_label_is_still_the_default_for_a_fleet_wide_cap():
    """When the pool really IS out for everything, the old label is correct."""
    agent = _Agent()
    assert _pool_scope_label(agent, "claude-opus-5") is None
    assert _fallback_reason_label(FailoverReason.pool_exhausted) == "sub pool capped"


def test_model_scoped_label_names_the_model_and_absolves_the_rest():
    agent = _Agent()
    agent._pending_pool_scope = "model"

    label = _pool_scope_label(agent, "claude-fable-5")

    assert label == "claude-fable-5 capped pool-wide, other models unaffected"
    # The whole point: it can no longer be read as a fleet outage.
    assert "other models unaffected" in label
    assert label != _FALLBACK_REASON_LABELS["pool_exhausted"]


def test_label_degrades_honestly_when_the_model_is_unknown():
    """Scope known, model not: still must not imply a fleet outage."""
    agent = _Agent()
    agent._pending_pool_scope = "model"

    label = _pool_scope_label(agent, "")

    assert label == "this model capped pool-wide, other models unaffected"


def test_scope_stamp_is_consumed_once():
    """A stale scope must never leak onto a later, unrelated failover — the
    same consume-once discipline as the quota-window stamp."""
    agent = _Agent()
    agent._pending_pool_scope = "model"

    first = _pool_scope_label(agent, "claude-opus-5")
    second = _pool_scope_label(agent, "claude-opus-5")

    assert first is not None
    assert second is None                       # cleared by the first read
    assert getattr(agent, "_pending_pool_scope", None) is None


def test_unknown_scope_value_is_ignored():
    """Only the exact 'model' scope narrows the label; anything else keeps the
    flat one rather than inventing a claim."""
    agent = _Agent()
    agent._pending_pool_scope = "something-else"

    assert _pool_scope_label(agent, "claude-opus-5") is None
