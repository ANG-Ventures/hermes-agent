"""t_f00f05bd: Anthropic's 400 "You're out of extra usage" (err_hash
6937830dd9, 11 rows 09-29..09-30) was ledger class ``unclassified`` and the
fallback announce said "unclassified error". It is a seat's billing wall:
ledger class ``quota_seat``, cause "extra usage exhausted". The sibling
"Third-party apps now draw from your extra usage" 400 (c06e75c5e2) stays the
route refusal #1581 made it (auth, "plan billing refused (extra usage only)").

Both samples run end to end: wire error -> stash -> ledger row -> the shipping
``_emit_fallback_announce``.
"""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.agent_runtime_helpers import extract_api_error_context
from agent.error_classifier import FailoverReason, classify_api_error

APR = "http://127.0.0.1:18810/anthropic"
OUT_OF_EXTRA = "You're out of extra usage. Add more at claude.ai/settings/usage and keep going."
THIRD_PARTY = ("Third-party apps now draw from your extra usage, not your plan limits. "
               "Add more at claude.ai/settings/usage and keep going.")

# (message, err_hash from t_a716610d, ledger class, classifier reason, cause)
SAMPLES = [
    (OUT_OF_EXTRA, "6937830dd9", "quota_seat", FailoverReason.billing, fp.EXTRA_USAGE_EXHAUSTED_CAUSE),
    (THIRD_PARTY, "c06e75c5e2", "auth", FailoverReason.extra_usage_only, fp.THIRD_PARTY_CAUSE),
]


def _bad_request(message: str) -> anthropic.BadRequestError:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message},
            "request_id": "req_011CfaKfE3y42mXv4DPeRm7v"}
    req = httpx.Request("POST", f"{APR}/v1/messages")
    resp = httpx.Response(400, json=body, request=req, headers={"x-pool-served-by": "sub-vps-15"})
    return anthropic.BadRequestError(message=f"Error code: 400 - {body}", response=resp, body=body)


def test_out_of_extra_usage_is_a_seat_quota_with_an_honest_cause():
    assert fbe.classify_text(OUT_OF_EXTRA, http_status=400) == "quota_seat"
    assert fp._cause_phrase({"trigger_class": "quota_seat", "err_head": OUT_OF_EXTRA}) \
        == "extra usage exhausted"
    # the generic seat-quota phrase is unchanged for other quota_seat text
    assert fp._cause_phrase({"trigger_class": "quota_seat", "err_head": "usage limit"}) \
        == "seat quota exhausted"
    # still the auth row (#1581), not captured by the new needle
    assert fbe.classify_text(THIRD_PARTY, http_status=400) == "auth"


@pytest.mark.parametrize("message,ehash,cls,reason,cause", SAMPLES)
def test_sample_end_to_end_through_the_shipping_emitter(message, ehash, cls, reason, cause):
    from agent.chat_completion_helpers import _emit_fallback_announce

    err = _bad_request(message)
    c = classify_api_error(err, provider="claude-apr", model="claude-fable-5-1")
    assert c.reason is reason
    assert c.reason is not FailoverReason.format_error

    emitted: list = []
    agent = SimpleNamespace(
        provider="claude-apr", model="claude-fable-5-1", session_id="s_t_f00f05bd",
        _current_turn_id="s_t_f00f05bd:1", _primary_runtime={
            "provider": "claude-apr", "model": "claude-fable-5-1", "base_url": APR},
        _fallback_activated=False, _pending_quota_window=None, _pending_pool_scope=None,
        _pending_stream_error_reason=None, _emit_status=emitted.append,
        _last_fallback_announced=None,
    )
    fbe.stash_api_error(agent, err, 400, extract_api_error_context(err))
    row = fbe.build_row(agent, "failover", from_provider="claude-apr", from_model="claude-fable-5-1",
                        to_provider="claude-bpr", to_model="claude-opus-5-5", reason=c.reason)
    assert row["err_hash"] == ehash
    assert row["trigger_class"] == cls

    _emit_fallback_announce(agent, "claude-fable-5-1", "claude-opus-5-5", "claude-bpr",
                            old_provider="claude-apr", record_event=False,
                            reason=c.reason, ledger_row=row)
    assert emitted, "announce emitted nothing"
    line = emitted[0]
    print("\nANNOUNCE:", line)
    assert cause in line
    assert "unclassified error" not in line
    assert "bad request" not in line
