"""/model verifier probe budget and URL wording (t_1737b2b7).

Regression for 2026-11-01 15:53:22 PT: "could not verify claude-opus-5-5
against Claude ALR ... probe of http://127.0.0.1:18810/anthropic/models failed
with a timeout (retried once)". The ALR relay forwards GET /v1/models upstream
(15 probes at 17:40: 12 under 0.1 s, 3 in 2.5-7.1 s), so a 5 s budget failed a
healthy relay; and the URL named was the 404 half, not the
``/anthropic/v1/models`` path the relay serves.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

import hermes_cli.models as models
from hermes_cli.models import validate_requested_model


@pytest.fixture(autouse=True)
def _no_neg_cache():
    models._probe_neg_cache.clear()
    yield
    models._probe_neg_cache.clear()


def _validate(base_url, provider="claude-alr"):
    seen = []

    def _fake_urlopen(req, timeout, **_kw):
        seen.append((req.full_url, timeout))
        raise socket.timeout("timed out")

    with patch("hermes_cli.models._urlopen_model_catalog_request", side_effect=_fake_urlopen):
        result = validate_requested_model("claude-opus-5-5", provider, api_key="k",
                                          base_url=base_url, api_mode="anthropic_messages")
    return result, seen


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:18810/anthropic",
                                      "http://192.168.1.216:18813/anthropic",
                                      "http://studio:18810/anthropic"])
def test_loopback_and_lan_relays_get_15s(base_url):
    _result, seen = _validate(base_url)
    assert seen and {t for _u, t in seen} == {15.0}


def test_public_endpoint_keeps_5s():
    _result, seen = _validate("https://api.anthropic.com", provider="anthropic")
    assert seen and {t for _u, t in seen} == {5.0}


def test_timeout_warning_names_the_url_the_relay_serves():
    result, seen = _validate("http://127.0.0.1:18810/anthropic")
    assert [u for u, _t in seen][-1] == "http://127.0.0.1:18810/anthropic/v1/models"
    msg = result["message"]
    assert "http://127.0.0.1:18810/anthropic/v1/models" in msg
    assert "timeout" in msg and "does not appear to expose" not in msg
