"""Regression: a Claude Code CLI usage-cap sentence relayed through claude-bpx
must classify as a QUOTA (rate_limit + window), never server_error, and the
fallback announce must say "usage exhausted" — not "connection issue".

Live incident 2026-09-19 (#connection-issue): sub-vps-12's bridge egressed
``type=internal_error code=500`` with message "Claude Code returned an error
result: You've hit your session limit · resets 2:20pm (UTC)". Hermes read the
500 → server_error → "🔄 Model fallback (connection issue)" for a spent 5h bucket.

🔗 SIBLING HALF — claude-bpx ``bridge/test/unit-sdk-thrown-error-result-classification.test.js``
The fix spans TWO repos and the halves are NOT redundant. Measured 2026-09-19 by
neutralizing each in turn against the live announce emitter:

    both present ...... "claude-fable-5-1 usage exhausted · 5h limit, resets in 11h"
    harness half gone .. "rate limit"                  <- relay tests still pass
    relay half gone .... "usage exhausted · 5h limit"  <- harness recovers it

So a GREEN relay suite does not prove the user-visible label survived, and vice
versa. ``test_relay_429_alone_does_not_carry_the_window`` below pins that
asymmetry so the claim can't rot into an assumption again.
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


# ── The announce WORDING contract ────────────────────────────────────────────
# The three tests below assert the user-visible rider, which is the whole point
# of the fix: "connection issue" and "rate limit" both describe a spent
# subscription bucket wrongly. Without these, a parity merge can revert
# agent/chat_completion_helpers.py's `_reason_label = f"{_who} usage exhausted"`
# line (~2960) to the flat label and every other test in this file stays green.
# RED-proof: replace that line with `_reason_label = _reason_label`.

def _announce(reason, *, window_ctx, old_model="claude-fable-5-1"):
    """Render one real fallback announce through the shipping emitter."""
    from agent.chat_completion_helpers import _emit_fallback_announce

    emitted = []
    agent = SimpleNamespace(
        _pending_quota_window=window_ctx,
        _pending_pool_scope=None,
        _pending_stream_error_reason=None,
        _emit_status=emitted.append,
        provider="claude-bpx-12",
        model=old_model,
    )
    _emit_fallback_announce(
        agent, old_model, "claude-opus-5", "claude-apx-1",
        old_provider="claude-bpx-12", record_event=False, reason=reason,
    )
    return emitted[0] if emitted else ""


def test_announce_names_usage_exhaustion_not_a_transport_fault():
    """The reported symptom: a spent bucket must not read as a connection fault."""
    ctx = extract_api_error_context(_status_error(SESSION, 500, "internal_error"))
    line = _announce(FailoverReason.rate_limit, window_ctx={
        k: ctx.get(k) for k in ("quota_window", "quota_window_reset", "quota_window_reset_text")
    })
    assert "usage exhausted" in line
    assert "claude-fable-5-1 usage exhausted" in line  # names WHOSE budget ran out
    assert "5h limit" in line                          # and WHICH window bound
    assert "connection issue" not in line
    assert "rate limit" not in line


def test_announce_distinguishes_the_weekly_window():
    ctx = extract_api_error_context(_status_error(WEEKLY, 500, "internal_error"))
    line = _announce(FailoverReason.rate_limit, window_ctx={
        k: ctx.get(k) for k in ("quota_window", "quota_window_reset", "quota_window_reset_text")
    })
    assert "usage exhausted" in line and "7d limit" in line


def test_announce_without_a_known_window_keeps_the_plain_rate_limit_label():
    """Scoping guard: only a BOUND quota window earns the exhaustion wording.

    A real request-rate throttle (no window resolved) must keep saying
    "rate limit" — widening the new wording to every 429 would be its own lie.
    """
    line = _announce(FailoverReason.rate_limit, window_ctx=None)
    assert "rate limit" in line
    assert "usage exhausted" not in line


# ── Cross-repo non-redundancy (the claim that must not rot) ──────────────────

def test_relay_429_alone_does_not_carry_the_window():
    """The relay half and this harness half are NOT redundant.

    A tempting-but-false simplification during a parity merge: "claude-bpx
    already maps the cap to 429, so the harness-side sentence parsing is
    belt-and-braces and can go." It cannot. The 429 body carries the cap
    sentence but NO ``anthropic-ratelimit-unified-*`` headers, so WITHOUT the
    sentence-derived window the announce degrades to a bare "rate limit" —
    losing the model name, the 5h/7d window, and the reset time, which are the
    entire point of the fix.

    Measured 2026-09-19 by neutralizing each half in turn:
        both present ...... "claude-fable-5-1 usage exhausted · 5h limit, resets in 11h"
        harness half gone .. "rate limit"
        relay half gone .... "usage exhausted · 5h limit"

    This test pins the SECOND row. If it ever fails because a bare relay 429
    now carries the window on its own, the premise changed — re-measure before
    deleting anything.
    """
    # A relay that already did its job: 429 rate_limit_error, cap sentence in
    # the body, no unified-quota headers (what claude-bpx actually sends).
    err = _status_error(SESSION, 429, "rate_limit_error")
    assert not getattr(err.response, "headers", {}).get("anthropic-ratelimit-unified-5h-status")

    ctx = extract_api_error_context(err)
    # The window is recoverable ONLY because this harness parses the sentence.
    assert ctx.get("quota_window") == "5h", (
        "the harness sentence-parser is what recovers the window; "
        "the relay's 429 does not supply it"
    )

    # Strip what the harness contributed → exactly what a relay-only world has.
    relay_only = {k: v for k, v in ctx.items()
                  if k not in ("quota_window", "quota_window_reset", "quota_window_reset_text")}
    degraded = _announce(FailoverReason.rate_limit,
                         window_ctx=relay_only.get("quota_window"))
    assert "rate limit" in degraded
    assert "usage exhausted" not in degraded
    assert "5h limit" not in degraded
