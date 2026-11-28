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
def test_relay_hop_seat_and_attribution_keep_the_seat_verbatim(seat):
    """The bound is render-time only (Prism #606 1d16abfcfca0): the extracted seat that
    the ledger stores and the carry compares is the relay's own identifier."""
    for _hop, got in [fallback_events.relay_hop_seat({}, {"relay_seat": seat}),
                      fallback_events.relay_hop_seat({"x-relay-seat": seat}, None)]:
        assert got == seat.strip()
    from agent.fallback_cause import attribution
    assert attribution({}, {"relay_seat": seat})["seat"] == seat.strip()


def test_distinct_long_seats_stay_distinct_in_ledger_and_carry():
    """Prism #606 repro: two seats sharing a 30-char prefix persist as two seats, and the
    carry does not treat them as one (an older seat's route id must not carry over)."""
    from agent import fallback_cause as fc

    east, west = "sub-vps-production-east-pool-east", "sub-vps-production-east-pool-west"
    seats = [fallback_events.relay_hop_seat({}, {"relay_seat": s})[1] for s in (east, west)]
    assert seats == [east, west]
    agent = SimpleNamespace(provider="claude-dtlr", _failover_carry=None)
    fc._carry(agent, fc.attribution({"x-relay-seat": east, "x-relay-route-id": "r-east"}))
    fc._carry(agent, fc.attribution({"x-relay-seat": west}))
    assert agent._failover_carry["seat"] == west
    assert agent._failover_carry.get("route_id") != "r-east"


def test_failover_rider_bounds_the_seat_but_not_the_row():
    from agent import fallback_policy as fp

    seat = "evil\nseat\x1b[31m" + "s" * 300
    row = {"trigger_class": "quota_seat", "http_status": 429, "from_provider": "claude-dtlr",
           "from_model": FABLE, "seat": seat, "same_err_prev_seat": seat,
           "hop": "bridge→anthropic", "return_branch": "warm_seat"}
    for text in (fp.format_cause_rider(row), fp.format_recovery_rider(row)):
        print(repr(text))
        assert "\n" not in text and "\x1b" not in text and "s" * 40 not in text
        assert "evil seat" in text
    assert row["seat"] == seat
    ok = dict(row, seat="sub-vps-12", same_err_prev_seat="sub-vps-12")
    assert "sub-vps-12" in fp.format_cause_rider(ok)
    assert "on sub-vps-12" in fp.format_recovery_rider(ok)



# Prism #606 0aa4ea16a348 (t_48bb0de3): the wait line's quota gate reads the same ordered
# classifier as the cause label, so a capacity 429 that quotes a seat's cap sentence is
# capacity, not the allowance.
POOL_CAP = "pool at capacity; last seat reported: You've reached your Opus 4.5 limit."
RETRYING = {"stage": "upstream_ttfb", "seat": "sub-vps-3", "since_ms": 4000,
            "attempt_ms": 4000, "request_ms": 62000}


def _fable_used_19_of_26(monkeypatch):
    def fetch(url, timeout):
        if url.endswith("/health"):
            return {"fable_allowance": {"exhausted": 19, "of": 26,
                                        "earliest_reset": "2026-11-15T18:59:59Z"}}
        if "/eligibility?" in url:
            return {"model": FABLE, "eligible_count": 7, "model_eligible": True}
        return None

    monkeypatch.setattr(rc, "fetch_json", fetch)
    monkeypatch.setattr(rh, "fetch_relay_stage", lambda base, key, **kw: RETRYING)
    monkeypatch.setattr(wb, "_knobs", lambda: (600.0, 120.0))
    monkeypatch.setattr(wb, "_dedupe_on", lambda: True)
    monkeypatch.setattr(wb, "post_logs", lambda text: None)


def _wait_line(message, relay_class=None):
    a = _agent()
    wb.arm(a, now=0.0)
    wb.note_upstream_failure(a, status=429, relay_class=relay_class, text=message)
    return wb.notice_due(a, now=120.0)


def test_unclassed_429_capacity_quoting_a_cap_sentence_is_not_the_allowance(monkeypatch):
    _fable_used_19_of_26(monkeypatch)
    assert fallback_events.classify_text(POOL_CAP, http_status=429) == "pool_pressure"
    line = _wait_line(POOL_CAP)
    print(line)
    assert "allowance" not in line, line
    cause = _line(429, POOL_CAP)
    print(cause)
    assert "capacity" in cause and "quota" not in cause and "allowance" not in cause, cause


@pytest.mark.parametrize("message", ["You've hit your weekly limit",
                                     "You've reached your Fable limit"])
def test_unclassed_429_quota_still_names_the_allowance(monkeypatch, message):
    _fable_used_19_of_26(monkeypatch)
    line = _wait_line(message)
    print(line)
    assert "Fable allowance used up on 19/26 seats" in line, line


def test_unclassed_429_rate_limit_does_not_name_the_allowance(monkeypatch):
    _fable_used_19_of_26(monkeypatch)
    line = _wait_line("You've reached your rate limit")
    print(line)
    assert "allowance" not in line, line


@pytest.mark.parametrize("relay_class", ["quota_model", "quota_seat"])
def test_relay_quota_class_decides_on_its_own(monkeypatch, relay_class):
    _fable_used_19_of_26(monkeypatch)
    line = _wait_line("pool at capacity", relay_class=relay_class)
    print(line)
    assert "Fable allowance used up on 19/26 seats" in line, line
    assert not wb._is_quota_refusal(429, "pool_pressure", "You've reached your Fable limit")
