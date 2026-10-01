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
