"""A periodic subscription quota wall that arrives as 401/403 is a quota, not auth.

2026-09-29 incident: cpa/kimi-k3 answered
``403 access_terminated_error "You've reached your 5-hour usage limit. Your
quota will reset when the current 5-hour window ends. ..."``. The bare-403
branch classified it ``auth`` and the failover announce said
"Model fallback (auth refresh)", sending the operator to re-auth a valid token.
"""

from types import SimpleNamespace

import pytest

from agent.agent_runtime_helpers import extract_api_error_context
from agent.error_classifier import FailoverReason, classify_api_error

KIMI_5H = (
    "You've reached your 5-hour usage limit. Your quota will reset when the "
    "current 5-hour window ends. To continue now, purchase extra usage or "
    "upgrade your plan: https://www.kimi.com/membership/subscription?tab=quota"
)


class _StatusError(Exception):
    def __init__(self, status, message, err_type):
        body = {"error": {"message": message, "type": err_type}}
        super().__init__(f"Error code: {status} - {body}")
        self.status_code = status
        self.body = body
        self.response = SimpleNamespace(headers={})


@pytest.mark.parametrize("status", [401, 403])
def test_quota_wall_behind_auth_status_is_rate_limit(status):
    e = _StatusError(status, KIMI_5H, "access_terminated_error")
    r = classify_api_error(e, provider="cpa", model="kimi-k3")
    assert r.reason == FailoverReason.rate_limit
    assert r.reason != FailoverReason.auth
    assert r.should_fallback is True


def test_quota_wall_context_names_the_5h_window():
    """The announce renders "<model> usage exhausted · 5h limit" off this stamp."""
    e = _StatusError(403, KIMI_5H, "access_terminated_error")
    assert extract_api_error_context(e).get("quota_window") == "5h"


def test_weekly_quota_wall_context_names_7d_window():
    msg = "You've reached your weekly usage limit. Your quota will reset in 3 days."
    e = _StatusError(403, msg, "access_terminated_error")
    assert classify_api_error(e, provider="cpa", model="m").reason == FailoverReason.rate_limit
    assert extract_api_error_context(e).get("quota_window") == "7d"


@pytest.mark.parametrize(
    "status,message",
    [
        (403, "You do not have access to this model"),
        (401, "invalid x-api-key"),
        # usage-limit phrase but NO reset signal: not provably periodic.
        (403, "You've reached your usage limit."),
    ],
)
def test_negative_controls_keep_auth(status, message):
    e = _StatusError(status, message, "permission_error")
    assert classify_api_error(e, provider="cpa", model="m").reason == FailoverReason.auth


def test_billing_403_keeps_billing_even_with_reset_words():
    e = _StatusError(403, "Key limit exceeded. Please try again later.", "x")
    assert classify_api_error(e, provider="openrouter", model="m").reason == FailoverReason.billing


def test_non_quota_auth_error_gets_no_window():
    e = _StatusError(401, "invalid x-api-key", "authentication_error")
    assert "quota_window" not in extract_api_error_context(e)
