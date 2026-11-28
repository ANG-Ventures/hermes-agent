"""Banner follow-ups from hermes-agent#603 (t_67969ec8).

1. Prism 65513351dee1: an unclassed 429 saying "You've reached your rate limit" rendered
   ``seat quota exhausted (Anthropic 429)`` on the wait/retry line, because the text table's
   bare "reached your" needle read it as quota. The wall budget's quota gate already ignored
   that needle; the cause label now uses the same rule.
2. Prism 686796853b7c: a ``relay_seat`` recovered from the response body was printed in the
   banner verbatim; every relay-sent seat name now goes through the one-line bound.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import fallback_events
from agent import fallback_wall_budget as wb
from agent.fork_ext import relay_capacity as rc
from agent.fork_ext import relay_headers as rh

FABLE = "claude-fable-5-2"
DTLR = "http://127.0.0.1:18816/v1"


class _Response:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self._body = body

    def json(self):
        return self._body


class APIStatusError(Exception):
    """The OpenAI SDK shape: ``body`` is ``data.get("error", data)``."""

    def __init__(self, status, headers, body):
        msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body
        super().__init__(f"Error code: {status} - {msg}")
        self.status_code = status
        self.response = _Response(status, headers, body)
        self.body = body.get("error", body) if isinstance(body, dict) else body


@pytest.fixture
def quiet_relay(monkeypatch):
    monkeypatch.setattr(rc, "fetch_json", lambda url, timeout: None)
    monkeypatch.setattr(rh, "fetch_relay_stage", lambda base, key, **kw: None)
    monkeypatch.setattr(wb, "_knobs", lambda: (600.0, 120.0))
    monkeypatch.setattr(wb, "_dedupe_on", lambda: True)
    monkeypatch.setattr(wb, "post_logs", lambda text: None)


def _agent():
    return SimpleNamespace(provider="claude-dtlr", model=FABLE, base_url=DTLR, session_id="s1",
                           _fallback_chain=[], _fallback_index=0, _wall_budget=None,
                           _failover_cause=None, _failover_carry=None)


def _line(status, message, headers=None, **body_extra):
    a = _agent()
    wb.arm(a, now=0.0)
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": message},
            **body_extra}
    fallback_events.stash_api_error(
        a, APIStatusError(status, headers or {"content-type": "application/json"}, body), status)
    return wb.notice_due(a, now=120.0)


def test_unclassed_429_reached_your_rate_limit_is_not_quota(quiet_relay):
    line = _line(429, "You've reached your rate limit")
    print(line)
    assert "quota" not in line
    assert "rate limit" in line


@pytest.mark.parametrize("message", ["You've hit your weekly limit",
                                     "You've reached your Fable limit",
                                     "You've reached your Opus 4.5 limit."])
def test_unclassed_429_with_a_quota_phrase_still_reads_quota(quiet_relay, message):
    line = _line(429, message)
    print(line)
    assert "quota" in line or "limit on" in line or "limit (" in line, line
    assert "rate limit" not in line


def test_relay_classed_quota_seat_429_still_reads_quota(quiet_relay):
    line = _line(429, "You've reached your rate limit", {"x-relay-error-class": "quota_seat"})
    print(line)
    assert "seat quota exhausted" in line


def test_classify_text_one_rule_for_label_and_gate():
    assert fallback_events.classify_text("You've reached your rate limit", http_status=429) \
        == "rate_upstream"
    assert fallback_events.classify_text("You've reached your Fable limit") == "quota_seat"
    assert fallback_events.classify_text("You've reached your Opus 4.5 limit.") == "quota_seat"
    assert not wb._is_quota_refusal(429, None, "You've reached your rate limit")
    assert wb._is_quota_refusal(429, None, "You've reached your Opus 4.5 limit.")


def test_recovered_relay_seat_is_bounded(quiet_relay):
    seat = "evil\nseat\x1b[31m" + "s" * 300
    line = _line(429, "You've hit your weekly limit", relay_seat=seat)
    print(repr(line))
    assert "\n" not in line and "\x1b" not in line
    assert "s" * 40 not in line
    assert "evil seat" in line


@pytest.mark.parametrize("seat", ["sub-vps-12", "local"])
def test_real_seat_names_render_unchanged(quiet_relay, seat):
    line = _line(429, "You've hit your weekly limit", relay_seat=seat)
    print(line)
    assert f"on {seat}" in line


@pytest.mark.parametrize("seat", ["sub-vps-12", "evil\n" + "s" * 300])
def test_relay_hop_seat_and_attribution_bound_the_seat(seat):
    for _hop, got in [fallback_events.relay_hop_seat({}, {"relay_seat": seat}),
                      fallback_events.relay_hop_seat({"x-relay-seat": seat}, None)]:
        assert "\n" not in got and len(got) <= 30
    from agent.fallback_cause import attribution
    got = attribution({}, {"relay_seat": seat})["seat"]
    assert "\n" not in got and len(got) <= 30
    if seat == "sub-vps-12":
        assert got == seat
