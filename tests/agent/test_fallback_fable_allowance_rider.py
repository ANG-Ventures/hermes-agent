"""claude-pool t_c61937cd: the rate-limit failover rider for a relay lane names
the shared-snapshot Fable allowance (``x-pool-fable-allowance``) and NOTHING
about lanes or seats (Ace 2026-10-17 11:58). Before: ``model budget capped at
the relay (sub unknown) · pool had no other seat``.
"""

from __future__ import annotations

import datetime as dt

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401

UTC = dt.timezone.utc
TS = dt.datetime(2026, 10, 17, 18, 40, 0, tzinfo=UTC).timestamp()
RESET = "2026-10-19T03:00:00+00:00"
CAPPED = ('HTTP 503: {"error":"no eligible sub for the requested model; this '
          "model's budget is capped on every subscription while other models "
          'are unaffected"}')


def _row(**kw):
    row = {"trigger_class": "quota_model", "from_provider": "claude-alr",
           "hop": "relay", "seat": None, "http_status": 503, "err_head": CAPPED,
           "pool_eligible": 0, "ts": TS,
           "fable_allowance": {"exhausted": 24, "of": 25,
                               "earliest_reset": dt.datetime.fromisoformat(RESET).timestamp()}}
    row.update(kw)
    return row


def test_header_parses():
    assert fbe.fable_allowance(
        {"X-Pool-Fable-Allowance": "24/25; earliest_reset=2026-10-19T03:00:00Z"}) == {
        "exhausted": 24, "of": 25,
        "earliest_reset": dt.datetime.fromisoformat(RESET).timestamp()}
    assert fbe.fable_allowance({"x-pool-fable-allowance": "0/25; earliest_reset=none"}) == {
        "exhausted": 0, "of": 25, "earliest_reset": None}
    for junk in ("", "x/25", "3/0; earliest_reset=none", "garbage"):
        assert fbe.fable_allowance({"x-pool-fable-allowance": junk}) is None
    assert fbe.fable_allowance({}) is None


def test_fable_tail_exact_string():
    assert fp.format_cause_rider(_row(), tz=UTC) == (
        "Fable weekly allowance (7d_oi) exhausted on 24/25 seats; "
        "earliest reset Mon 03:00, 18:40:00")


def test_fable_tail_names_no_lane_or_seat():
    text = fp.format_cause_rider(_row(seat="sub-vps-22"), tz=UTC)
    for banned in ("sub unknown", "pool had no other seat", "sub-vps", "claude-",
                   "relay", "all subs"):
        assert banned not in text, (banned, text)


def test_fable_tail_without_reset():
    row = _row(fable_allowance={"exhausted": 3, "of": 25, "earliest_reset": None})
    assert fp.format_cause_rider(row, tz=UTC) == (
        "Fable weekly allowance (7d_oi) exhausted on 3/25 seats, 18:40:00")


def test_non_fable_rate_limit_tail_unchanged():
    base = _row()
    del base["fable_allowance"]
    before = fp.format_cause_rider(base, tz=UTC)
    assert "Fable weekly allowance" not in before
    # t_d6227140: "all subs" already says it; no " · pool had no other seat".
    assert before == "model budget capped at the relay on all subs, 18:40:00", before
    # zero exhausted: not a Fable allowance event, the old tail stays
    zero = _row(fable_allowance={"exhausted": 0, "of": 25, "earliest_reset": None})
    assert fp.format_cause_rider(zero, tz=UTC) == before
    # a non-quota class never takes the Fable tail
    conn = _row(trigger_class="conn", err_head='{"error":"upstream attempt timed out"}')
    assert "Fable weekly allowance" not in fp.format_cause_rider(conn, tz=UTC)


def test_live_failover_banner_carries_the_tail(_home, monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver
    from tests.agent.test_fallback_rider_truth import _Err

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    err = _Err({"error": "no eligible sub for the requested model; this model's budget "
                         "is capped on every subscription while other models are unaffected"},
               503, {"x-relay-error-class": "quota_model", "x-relay-error-hop": "relay",
                     "x-pool-other-eligible": "0",
                     "x-pool-fable-allowance": "24/25; earliest_reset=2026-10-19T03:00:00Z"})
    fbe.stash_api_error(a, err, 503)
    assert try_activate_fallback(a, reason=FailoverReason.rate_limit) is True
    text = _rows(_home)[0]["notice_text"]
    assert " — Fable weekly allowance (7d_oi) exhausted on 24/25 seats; earliest reset " in text, text
    for banned in ("sub unknown", "pool had no other seat"):
        assert banned not in text, (banned, text)
