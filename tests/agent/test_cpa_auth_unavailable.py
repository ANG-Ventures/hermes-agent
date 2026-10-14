"""t_ac76e76f: CLIProxyAPI (cpa) 503 "auth_unavailable: no auth available" is
an auth failure, not provider overload.

Fixture is the wire body from agent.log 2026-09-30 10:58:09 (provider=cpa,
base_url=http://127.0.0.1:18812/v1, model=kimi-k3, err_hash 025cdfe644,
34 rows 09-30 10:58..13:16). Before the fix: fallback_events class
``unclassified`` and FailoverReason ``overloaded`` ("provider overloaded").
"""

from __future__ import annotations

import httpx
import openai

from agent import fallback_events as fbe
from agent.error_classifier import FailoverReason, classify_api_error

CPA = "http://127.0.0.1:18812/v1"
MSG = "auth_unavailable: no auth available (providers=kimi, model=kimi-k3)"
BODY = {"message": MSG, "type": "server_error", "code": "internal_server_error"}


def _cpa_503() -> openai.InternalServerError:
    req = httpx.Request("POST", f"{CPA}/chat/completions")
    resp = httpx.Response(503, json={"error": BODY}, request=req)
    return openai.InternalServerError(
        f"Error code: 503 - {{'error': {BODY}}}", response=resp, body=BODY)


def test_text_table_classifies_sample_as_auth():
    assert fbe.classify_text(MSG, http_status=503) == "auth"
    err = _cpa_503()
    assert fbe.classify_trigger(text=str(err), http_status=503,
                                headers=err.response.headers) == ("auth", "text")


def test_error_classifier_does_not_call_it_overloaded():
    c = classify_api_error(_cpa_503(), provider="cpa", model="kimi-k3")
    assert c.reason is not FailoverReason.overloaded
    assert c.reason is FailoverReason.auth_permanent
    assert c.is_auth
    assert c.retryable is False
    assert c.should_rotate_credential is False
    assert c.should_fallback is True


def test_plain_503_overload_still_overloaded():
    req = httpx.Request("POST", f"{CPA}/chat/completions")
    body = {"message": "server is overloaded", "type": "server_error"}
    resp = httpx.Response(503, json={"error": body}, request=req)
    err = openai.InternalServerError("Error code: 503", response=resp, body=body)
    assert classify_api_error(err, provider="cpa", model="kimi-k3").reason is FailoverReason.overloaded


# t_03fb601f: cpa sends the same token when credentials EXIST but are all
# overloaded / cooling down. Wire text from agent.log 2026-10-13 23:53 (codex,
# gpt-6-astra): zero 401s at cpa, 2 creds upstream 502 server_is_overloaded,
# the 3rd in quota cooldown. It rendered "OAuth revoked (401)".
OVERLOADED_MSG = (
    "auth_unavailable: no auth available (providers=codex, model=gpt-6-astra; "
    "last upstream error: server_is_overloaded: Our servers are currently "
    "overloaded. Please try again later.)")


def _cpa_503_with(msg: str) -> openai.InternalServerError:
    body = {"message": msg, "type": "server_error", "code": "internal_server_error"}
    req = httpx.Request("POST", f"{CPA}/chat/completions")
    resp = httpx.Response(503, json={"error": body}, request=req)
    return openai.InternalServerError(
        f"Error code: 503 - {{'error': {body}}}", response=resp, body=body)


def test_overloaded_upstream_is_capacity_not_auth():
    assert fbe.classify_text(OVERLOADED_MSG, http_status=503) == "pool_pressure"
    err = _cpa_503_with(OVERLOADED_MSG)
    assert fbe.classify_trigger(text=str(err), http_status=503,
                                headers=err.response.headers) == ("pool_pressure", "text")
    c = classify_api_error(err, provider="cpa", model="gpt-6-astra")
    assert not c.is_auth
    assert c.reason is FailoverReason.overloaded
    assert c.retryable is True
    assert c.should_rotate_credential is False


def test_overloaded_banner_cause_is_not_oauth_revoked():
    from agent.fallback_policy import _cause_phrase

    cause = _cause_phrase({"trigger_class": fbe.classify_text(OVERLOADED_MSG, http_status=503),
                           "err_head": OVERLOADED_MSG, "http_status": 503})
    assert "OAuth revoked" not in cause
    assert cause == "upstream overloaded"
    # The bare no-credential body still renders as auth.
    assert _cause_phrase({"trigger_class": fbe.classify_text(MSG, http_status=503),
                          "err_head": MSG}) == "OAuth revoked (401)"


def test_capacity_variants_map_to_their_class():
    base = "auth_unavailable: no auth available (providers=codex, model=m; "
    cases = {
        base + "last upstream error: 429 Too Many Requests)": "rate_upstream",
        base + "last upstream error: rate_limit_exceeded)": "rate_upstream",
        base + "last upstream error: usage quota exceeded)": "quota_seat",
        base + "cooldown: credential_quota until 2026-10-19)": "quota_seat",
    }
    for msg, cls in cases.items():
        assert fbe.classify_text(msg, http_status=503) == cls, msg
        c = classify_api_error(_cpa_503_with(msg), provider="cpa", model="m")
        assert not c.is_auth, msg
        assert c.reason is FailoverReason.rate_limit, msg
        assert c.should_fallback is True, msg


def test_non_capacity_upstream_error_stays_auth():
    # "access_terminated_error" is the account being cut off, not capacity.
    msg = ("auth_unavailable: no auth available (providers=kimi, model=kimi-k3; "
           "last upstream error: access_terminated_error: You have been terminated)")
    assert fbe.classify_text(msg, http_status=503) == "auth"
    assert classify_api_error(_cpa_503_with(msg), provider="cpa",
                              model="kimi-k3").reason is FailoverReason.auth_permanent
