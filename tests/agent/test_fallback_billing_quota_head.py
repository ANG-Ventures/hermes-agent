"""t_a3b7bc23: a quota wall the error classifier calls ``billing`` (no reset signal in the
text) but the ledger classes ``quota_seat`` rendered "(credit exhausted)" over the rider
"seat quota exhausted": label and detail named different causes, and
fallback-banner-truth-audit paged label_detail. Live row: turns.db fallback_events 679,
2026-10-21 00:13:45 PDT, cpa/kimi-k3 429 "You've reached your monthly usage limit for this
billing cycle ...". The head now names the quota the rider names.
"""

from __future__ import annotations

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)

from agent import fallback_policy as fp
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401
from tests.agent.test_fallback_relay_quota_banner import _failover

CPA_MONTHLY = ("You've reached your monthly usage limit for this billing cycle. Your quota will be "
               "refreshed in the next cycle. To continue now, purchase extra usage or upgrade your plan.")


def test_monthly_quota_wall_head_agrees_with_rider(_home, monkeypatch):
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_rider_truth import _Err

    _failover(monkeypatch, _Err({"error": {"message": CPA_MONTHLY}}, 429, {}), FailoverReason.billing)
    row = _rows(_home)[0]
    text = row["notice_text"]
    assert row["reason"] == "billing" and row["trigger_class"] == "quota_seat", row
    assert "(credit exhausted)" not in text, text
    assert text.startswith("🔄 Model fallback (seat quota): "), text
    assert " — seat quota exhausted" in text, text


def test_billing_head_unchanged_for_non_quota_class():
    # Only a quota-classed row is relabelled; a billing reason on any other class keeps the
    # caller's "credit exhausted" path (the override returns None).
    row = {"trigger_class": "auth", "class_source": "text", "reason": "billing",
           "from_provider": "cpa", "from_model": "kimi-k3", "err_head": "insufficient credits"}
    assert fp.head_label_override(row) is None
    row = {"trigger_class": "quota_seat", "class_source": "text", "reason": "rate_limit",
           "from_provider": "cpa", "from_model": "kimi-k3", "err_head": CPA_MONTHLY}
    assert fp.head_label_override(row) is None
