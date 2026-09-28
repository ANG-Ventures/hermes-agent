"""t_a8dc8b21: the fallback banner names the real provider error.

An OpenRouter 429 "Rate limited on a cache hit, custom key used" (the
upstream provider's own BYOK key tripped) rendered as "account rate limit
(hop unknown, sub unknown)", which read as "out of funds". The banner must
carry the vendor's words and whose limit tripped, and drop relay vocabulary
for non-relay providers. A relay 429 keeps its hop/sub fields.
"""

from __future__ import annotations

import datetime as dt
import types

import httpx
import openai
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.chat_completion_helpers import _emit_fallback_announce
from agent.error_classifier import FailoverReason

UTC = dt.timezone.utc
BYOK_BODY = {"message": "Rate limited on a cache hit, custom key used", "code": 429,
             "metadata": {"provider_name": None}}


def _or_error(status, body, headers=None):
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    resp = httpx.Response(status, request=req, headers=headers or {})
    cls = openai.RateLimitError if status == 429 else openai.APIStatusError
    # The OpenAI SDK raises with body = data["error"] (the inner object).
    return cls(f"Error code: {status} - {{'error': {body!r}}}", response=resp, body=body)


def _agent(provider="openrouter"):
    emitted = []
    return types.SimpleNamespace(
        provider=provider, session_id="s1", _current_turn_id="s1:t1",
        _pending_fallback_error=None, _emit_status=emitted.append), emitted


def _row_from_error(agent, err, status, provider="openrouter",
                    model="moonshotai/kimi-k3"):
    fbe.stash_api_error(agent, err, status)
    return fbe.build_row(agent, "failover", from_provider=provider, from_model=model,
                         to_provider="claude-bpr", to_model="claude-opus-5-5",
                         reason="rate_limit")


def test_openrouter_byok_429_banner_names_vendor_text_and_byok():
    agent, emitted = _agent()
    row = _row_from_error(agent, _or_error(429, BYOK_BODY), 429)
    assert row["provider_scope"] == "byok"
    assert row["provider_message"] == BYOK_BODY["message"]
    _emit_fallback_announce(agent, "moonshotai/kimi-k3", "claude-opus-5-5", "claude-bpr",
                            old_provider="openrouter", reason=FailoverReason.rate_limit,
                            ledger_row=row)
    assert len(emitted) == 1
    banner = emitted[0]
    assert 'OpenRouter 429 said "Rate limited on a cache hit, custom key used"' in banner
    assert "BYOK" in banner
    assert "account rate limit" not in banner
    assert "hop unknown" not in banner and "sub unknown" not in banner
    assert row["notice_text"] == banner
    assert banner.endswith('said "Rate limited on a cache hit, custom key used"')


def test_openrouter_scopes_upstream_platform_credits():
    up = {"message": "Provider returned error", "code": 429,
          "metadata": {"provider_name": "Alibaba", "raw": "slow down"}}
    assert fbe.provider_error_detail("openrouter", up, None, 429)[1] == "upstream"
    plat = {"message": "Rate limit exceeded: limit_rpm/moonshotai/kimi-k3", "code": 429,
            "metadata": {"error_type": "rate_limit_exceeded"}}
    assert fbe.provider_error_detail("openrouter", plat, None, 429)[1] == "platform"
    hdr = {"message": "Rate limit exceeded", "code": 429}
    assert fbe.provider_error_detail(
        "openrouter", hdr, {"X-RateLimit-Remaining": "0"}, 429)[1] == "platform"
    credits = {"message": "Insufficient credits", "code": 402}
    assert fbe.provider_error_detail("openrouter", credits, None, 402)[1] == "credits"
    # Envelope shape (non-OpenAI SDKs) parses the same.
    assert fbe.provider_error_detail("openrouter", {"error": BYOK_BODY}, None, 429) == (
        BYOK_BODY["message"], "byok")


def test_openrouter_402_says_out_of_credits():
    agent, _ = _agent()
    row = _row_from_error(agent, _or_error(402, {"message": "Insufficient credits",
                                                 "code": 402}), 402)
    rider = fp.format_cause_rider(dict(row, ts=0, first_err_ts=0), tz=UTC)
    assert rider.startswith('out of OpenRouter credits; OpenRouter 402 said "Insufficient credits"')


def test_account_rate_limit_only_when_vendor_says_account():
    base = {"from_provider": "xai", "trigger_class": "rate_upstream", "http_status": 429,
            "first_err_ts": 0}
    said = dict(base, provider_message="You exceeded your account's rate limit")
    assert fp.format_cause_rider(said, tz=UTC).startswith("account rate limit; xAI 429 said")
    plain = dict(base, provider_message="Too many requests")
    r = fp.format_cause_rider(plain, tz=UTC)
    assert r.startswith('rate limit; xAI 429 said "Too many requests"')
    assert "account" not in r and "hop" not in r and "sub" not in r


def test_long_vendor_message_truncated():
    msg, _ = fbe.provider_error_detail("openai-codex", {"message": "x " * 200}, None, 429)
    assert len(msg) <= fbe.PROVIDER_MESSAGE_MAX and msg.endswith("…")


def test_relay_429_keeps_hop_and_sub():
    agent, emitted = _agent("claude-bpr")
    req = httpx.Request("POST", "http://relay/v1/messages")
    resp = httpx.Response(429, request=req, headers={
        "x-relay-error-class": "rate_upstream", "x-relay-error-hop": "bridge->upstream",
        "x-relay-seat": "sub-vps-7"})
    err = openai.RateLimitError("This request would exceed your account's rate limit",
                                response=resp, body={"message": "rate limited"})
    row = _row_from_error(agent, err, 429, provider="claude-bpr", model="claude-fable-5-1")
    rider = fp.format_cause_rider(row, tz=UTC)
    assert rider.startswith("account rate limit (Anthropic 429) on sub-vps-7"), rider
    unknown = fp.format_cause_rider({"from_provider": "claude-bpr", "trigger_class": "rate_upstream",
                                     "http_status": 429, "first_err_ts": 0}, tz=UTC)
    assert "(hop unknown, sub unknown)" in unknown


@pytest.mark.parametrize("provider", ["openrouter", "openai-codex", "xai-oauth",
                                      "gemini-bridge", "kimi-code", "xai"])
@pytest.mark.parametrize("message", ["Rate limit exceeded", None])
def test_non_relay_banner_has_no_hop_or_sub(provider, message):
    """Ace 2026-09-27 18:48: hop/sub are relay-pool words (which relay leg,
    which Claude seat); a non-relay fallback banner carries neither."""
    agent, emitted = _agent(provider)
    row = {"from_provider": provider, "trigger_class": "rate_upstream", "http_status": 429,
           "first_err_ts": 0, "provider_message": message}
    _emit_fallback_announce(agent, "some-model", "claude-opus-5-5", "claude-bpr",
                            old_provider=provider, reason=FailoverReason.rate_limit,
                            ledger_row=row)
    rider = emitted[0].split(" — ", 1)[1]
    assert "hop" not in rider and "sub" not in rider, rider
    if message:
        assert rider.endswith(f'said "{message}"'), rider


def test_custom_prefixed_relay_keeps_relay_rider():
    from agent.fallback_policy import _plain_provider
    assert _plain_provider({"from_provider": "custom:claude-apr"}) is False
    assert _plain_provider({"from_provider": "custom:claude-apx-7"}) is False
    assert _plain_provider({"from_provider": "openrouter"}) is True


# ── t_21bba7dc: conn drop to the local relay before it answered ──────────

# The real 2026-09-28 08:15:40 ledger row (turns.db fallback_events id 781):
# APIConnectionError to 127.0.0.1:18811 while relay-autodeploy restarted it.
REAL_0815_ROW = {"from_provider": "claude-bpr", "from_model": "claude-fable-5-1",
                 "to_provider": "claude-bpr", "to_model": "claude-opus-5-5",
                 "kind": "failover", "reason": "timeout", "trigger_class": "conn",
                 "class_source": "text", "http_status": None, "relay_synthetic": 0,
                 "hop": None, "seat": None, "attempts": 1,
                 "ts": 1790608540.24259, "first_err_ts": 1790608540.24259}


def _conn_error(url="http://127.0.0.1:18811/v1/messages"):
    return openai.APIConnectionError(message="Connection error.",
                                     request=httpx.Request("POST", url))


def test_relay_conn_drop_names_local_relay_not_hop_sub(monkeypatch):
    monkeypatch.setattr(fbe, "probe_listener", lambda addr, timeout=0.3: False)
    agent, emitted = _agent("claude-bpr")
    fbe.stash_api_error(agent, _conn_error(), None)
    row = fbe.build_row(agent, "failover", from_provider="claude-bpr",
                        from_model="claude-fable-5-1", to_provider="claude-bpr",
                        to_model="claude-opus-5-5", reason="timeout")
    assert row["trigger_class"] == "conn" and row["relay_addr"] == "127.0.0.1:18811"
    _emit_fallback_announce(agent, "claude-fable-5-1", "claude-opus-5-5", "claude-bpr",
                            old_provider="claude-bpr", reason=FailoverReason.timeout,
                            ledger_row=row)
    banner = emitted[0]
    assert "hop unknown" not in banner and "sub unknown" not in banner, banner
    assert ("local relay 127.0.0.1:18811 dropped the connection before answering; "
            "relay not reachable") in banner, banner


def test_relay_conn_drop_says_relay_back_up_when_listener_answers(monkeypatch):
    monkeypatch.setattr(fbe, "probe_listener", lambda addr, timeout=0.3: True)
    agent, _ = _agent("claude-bpr")
    fbe.stash_api_error(agent, _conn_error(), None)
    row = fbe.build_row(agent, "failover", from_provider="claude-bpr",
                        from_model="claude-fable-5-1", to_provider="claude-bpr",
                        to_model="claude-opus-5-5", reason="timeout")
    row["relay_up_ts"] = 1790608541.0  # 08:15:41 PDT
    rider = fp.format_cause_rider(dict(row, first_err_ts=REAL_0815_ROW["ts"]),
                                  tz=dt.timezone(dt.timedelta(hours=-7)))
    assert rider == ("local relay 127.0.0.1:18811 dropped the connection before answering; "
                     "relay back up 08:15:41, 08:15:40"), rider


def test_real_0815_row_without_address_drops_rider():
    rider = fp.format_cause_rider(REAL_0815_ROW, tz=UTC)
    assert "hop unknown" not in rider and "sub unknown" not in rider, rider
    assert rider.startswith("the relay dropped the connection before answering"), rider


def test_relay_conn_with_seat_keeps_rider():
    row = dict(REAL_0815_ROW, seat="sub-vps-7", hop="relay→bridge", relay_addr="127.0.0.1:18811")
    rider = fp.format_cause_rider(row, tz=UTC)
    assert rider.startswith("connection error to sub-vps-7 bridge"), rider
    assert "local relay" not in rider
    # A mid-stream drop (incomplete read) with no evidence keeps the relay rider too.
    mid = dict(REAL_0815_ROW, err_head="peer closed connection: incomplete read")
    assert "(hop unknown, sub unknown)" in fp.format_cause_rider(mid, tz=UTC)


def test_openrouter_conn_row_unchanged(monkeypatch):
    called = []
    monkeypatch.setattr(fbe, "probe_listener", lambda *a, **k: called.append(a))
    agent, _ = _agent("openrouter")
    fbe.stash_api_error(agent, _conn_error("https://openrouter.ai/api/v1/chat/completions"), None)
    row = fbe.build_row(agent, "failover", from_provider="openrouter",
                        from_model="moonshotai/kimi-k3", to_provider="claude-bpr",
                        to_model="claude-opus-5-5", reason="timeout")
    assert not called and "relay_addr" not in row
    rider = fp.format_cause_rider(dict(row, ts=3600, first_err_ts=3600), tz=UTC)
    assert rider == "connection error, 01:00:00", rider


def test_probe_listener_real_socket():
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert fbe.probe_listener(f"127.0.0.1:{port}") is True
    finally:
        srv.close()
    assert fbe.probe_listener(f"127.0.0.1:{port}") is False
    assert fbe.probe_listener(None) is None
