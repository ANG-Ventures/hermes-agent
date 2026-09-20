"""Regression: a Claude Code CLI usage-cap sentence relayed through claude-bpx
must classify as a QUOTA (rate_limit + window), never server_error, and the
fallback announce must say "usage exhausted" — not "connection issue".

Live incident 2026-09-19 (#connection-issue): sub-vps-12's bridge egressed
``type=internal_error code=500`` with message "Claude Code returned an error
result: You've hit your session limit · resets 2:20pm (UTC)". Hermes read the
500 → server_error → "🔄 Model fallback (connection issue)" for a spent 5h bucket.
"""
from types import SimpleNamespace

import httpx
import openai
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.agent_runtime_helpers import extract_api_error_context
from agent.chat_completion_helpers import _quota_window_suffix


SESSION = "Claude Code returned an error result: You've hit your session limit · resets 2:20pm (UTC)"
WEEKLY = "Claude Code returned an error result: You've hit your weekly limit · resets Sep 23, 2pm (UTC)"
COMPACTION = ("Claude Code returned an error result: Prompt is too long · automatic compaction failed: "
              "You've hit your session limit · resets 2:20pm (UTC)")


def _status_error(msg, status, err_type):
    req = httpx.Request("POST", "http://bpx.test/v1/chat/completions")
    body = {"error": {"message": msg, "type": err_type, "code": status}}
    resp = httpx.Response(status, request=req, json=body)
    return openai.APIStatusError(msg, response=resp, body=body)


@pytest.mark.parametrize("status,err_type", [(500, "internal_error"), (429, "rate_limit_error")])
@pytest.mark.parametrize("msg", [SESSION, WEEKLY, COMPACTION])
def test_cli_usage_cap_is_rate_limit_regardless_of_relay_status(status, err_type, msg):
    r = classify_api_error(_status_error(msg, status, err_type), provider="claude-bpx-12", model="claude-fable-5-1")
    assert r.reason is FailoverReason.rate_limit, r
    assert r.should_fallback and r.should_rotate_credential


def test_cli_usage_cap_statusless_is_rate_limit():
    req = httpx.Request("POST", "http://bpx.test/v1/chat/completions")
    r = classify_api_error(openai.APIError(SESSION, request=req, body=None), provider="claude-bpx-12", model="x")
    assert r.reason is FailoverReason.rate_limit


def test_genuine_500_still_server_error():
    r = classify_api_error(_status_error("Claude Code process exited with code 1. stderr: ENOSPC", 500, "internal_error"),
                           provider="claude-bpx-12", model="x")
    assert r.reason is FailoverReason.server_error


def test_context_uses_session_window_and_literal_reset_text():
    ctx = extract_api_error_context(_status_error(SESSION, 500, "internal_error"))
    assert ctx["quota_window"] == "5h"
    assert ctx["quota_window_reset_text"] == "2:20pm (UTC)"
    assert isinstance(ctx.get("quota_window_reset"), float)


def test_context_uses_weekly_window():
    ctx = extract_api_error_context(_status_error(WEEKLY, 500, "internal_error"))
    assert ctx["quota_window"] == "7d"
    assert ctx["quota_window_reset_text"] == "Sep 23, 2pm (UTC)"


def _reset_clock():
    from agent.agent_runtime_helpers import _parse_cli_reset_clock
    return _parse_cli_reset_clock


def test_reset_clock_resolves_utc_to_future_epoch():
    # 2026-09-19 11:54 UTC → "2:20pm (UTC)" is 2h26m later.
    now = 1789818840.0
    epoch = _reset_clock()("2:20pm (UTC)", now=now)
    assert epoch is not None and 0 < epoch - now <= 3 * 3600


def test_reset_clock_without_zone_is_not_guessed():
    assert _reset_clock()("2:20pm") is None


def test_suffix_falls_back_to_literal_reset_text_when_no_epoch():
    agent = SimpleNamespace(_pending_quota_window={
        "quota_window": "5h", "quota_window_reset": None, "quota_window_reset_text": "2:20pm (UTC)",
    })
    assert _quota_window_suffix(agent) == "5h limit, resets 2:20pm (UTC)"
    assert agent._pending_quota_window is None  # consume-once preserved
