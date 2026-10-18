"""t_d6227140: a relay quota refusal's failover banner is labelled by the quota,
not "rate limit", and a seatless zero-eligible quota_seat says "all subs", not
"sub unknown". Live row: Aegis 2026-10-17 15:49:56 PDT, alr refused 503
``no eligible sub for the requested model`` with x-relay-error-class quota_seat
(a seat recovers at a known time), x-relay-seat none, x-relay-eligible 0, and
Fable's weekly allowance exhausted on 25/25 seats. Ace approved the target
string 2026-10-17 16:04.
"""

from __future__ import annotations

import datetime as dt
import time
import types

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401

UTC = dt.timezone.utc
TS = dt.datetime(2026, 10, 17, 22, 49, 56, tzinfo=UTC).timestamp()   # 15:49:56 PDT
CAPPED_BODY = {"error": "no eligible sub for the requested model; this model's budget "
                        "is capped on every subscription while other models are unaffected"}
LIVE_HEADERS = {"x-relay-error-class": "quota_seat", "x-relay-error-hop": "relay",
                "x-relay-seat": "none", "x-relay-eligible": "0",
                "x-pool-fable-allowance": "25/25; earliest_reset=2026-10-18T07:00:00Z"}
EXPECTED = ("🔄 Model fallback (Fable weekly limit): claude-alr/claude-fable-5-1 → "
            "claude-dtlr/claude-opus-5-5 — Fable weekly allowance (7d_oi) exhausted on "
            "25/25 seats; earliest reset Sun 00:00, 15:49:56")
CAPPED = 'HTTP 503: ' + '{"error":"' + CAPPED_BODY["error"] + '"}'


def _pdt(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()


def _failover(monkeypatch, err, reason):
    from agent.chat_completion_helpers import try_activate_fallback
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    monkeypatch.setattr(fbe, "time", types.SimpleNamespace(
        time=lambda: TS, monotonic=time.monotonic, strftime=time.strftime))
    a = _alr_agent()
    a._fallback_chain = [{"provider": "claude-dtlr", "model": "claude-opus-5-5"}]
    a.fallback_model = list(a._fallback_chain)
    fbe.stash_api_error(a, err, err.status_code)
    assert try_activate_fallback(a, reason=reason) is True
    return a


def test_live_154956_row_renders_the_approved_banner(_home, monkeypatch):
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_rider_truth import _Err

    _pdt(monkeypatch)
    try:
        _failover(monkeypatch, _Err(CAPPED_BODY, 503, dict(LIVE_HEADERS)),
                  FailoverReason.rate_limit)
        text = _rows(_home)[0]["notice_text"]
        print(text)
        assert text == EXPECTED, text
    finally:
        monkeypatch.undo()   # restores TZ before tzset re-reads it
        time.tzset()


def test_genuine_upstream_429_keeps_rate_limit_head(_home, monkeypatch):
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_rider_truth import _Err

    err = _Err({"type": "error", "error": {"type": "rate_limit_error", "message":
                "This request would exceed your account's rate limit. Please try again later."}},
               429, {"x-relay-error-class": "rate_upstream", "x-relay-error-hop": "bridge->upstream",
                     "x-relay-seat": "sub-vps-9"})
    _failover(monkeypatch, err, FailoverReason.rate_limit)
    text = _rows(_home)[0]["notice_text"]
    assert text.startswith("🔄 Model fallback (rate limit): "), text


def _row(**kw):
    row = {"trigger_class": "quota_seat", "class_source": "relay_header",
           "from_provider": "claude-alr", "from_model": "claude-fable-5-1", "hop": "relay",
           "seat": None, "http_status": 503, "err_head": CAPPED, "pool_eligible": 0, "ts": TS}
    row.update(kw)
    return row


def test_head_label_names_the_quota():
    fa = {"exhausted": 25, "of": 25, "earliest_reset": None}
    assert fp.head_label_override(_row(fable_allowance=fa)) == "Fable weekly limit"
    assert fp.head_label_override(_row(trigger_class="quota_model", fable_allowance=fa)) == \
        "Fable weekly limit"
    weekly = "Claude Fable weekly limit reached"
    assert fp.head_label_override(_row(err_head=weekly)) == "Fable weekly limit"
    assert fp.head_label_override(_row(from_model="claude-opus-5-5")) == "seat quota"
    assert fp.head_label_override(_row(trigger_class="quota_model",
                                       from_model="claude-opus-5-5")) == "model quota"
    # A Fable allowance with no exhausted seat is not evidence of the Fable limit.
    assert fp.head_label_override(_row(
        from_model="claude-opus-5-5",
        fable_allowance={"exhausted": 0, "of": 25, "earliest_reset": None})) == "seat quota"
    # Not relay-classed (text / direct pin): the caller's label path is unchanged.
    assert fp.head_label_override(_row(class_source="text")) is None
    assert fp.head_label_override(_row(trigger_class="rate_upstream")) is None


def test_seatless_zero_eligible_quota_seat_is_all_subs():
    text = fp.format_cause_rider(_row(), tz=UTC)
    assert text == "model budget capped at the relay on all subs, 22:49:56", text
    # The quota_model twin already said all subs; the redundant pool rider goes.
    qm = fp.format_cause_rider(_row(trigger_class="quota_model"), tz=UTC)
    assert qm == "model budget capped at the relay on all subs, 22:49:56", qm


def test_sub_unknown_stays_for_missing_evidence():
    # No eligible count from the relay: genuinely unknown, not pool-wide.
    text = fp.format_cause_rider(_row(pool_eligible=None), tz=UTC)
    assert "(sub unknown)" in text and "all subs" not in text, text
    # A named seat still names it.
    named = fp.format_cause_rider(_row(seat="sub-vps-9", pool_eligible=None), tz=UTC)
    assert "on sub-vps-9" in named, named
