"""t_b0c67f3e: a pool-wide "relay busy" refusal renders the numbers the relay
measured at refusal time instead of the catch-all "all subs at capacity".

Ace, 2026-10-28 23:47: "if the really busy error comes up, and it's because of a
limit in the number of seats, it should say that limit ... And it should actually
measure those numbers." The relay (claude-pool ``pool_pressure_headers``) sends
``x-pool-seats-total/-eligible/-busy``, ``x-pool-inflight(-max)`` and
``x-pool-ineligible-reasons`` on every pool_pressure refusal at the relay hop.
"""

from __future__ import annotations

import datetime as dt
import time

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.fork_ext import relay_quota as rq
from tests.agent.test_fallback_banner_cap_wording import (  # noqa: F401
    FABLE, OPUS, _agent, _busy_headers, _err, _hop, _lines, _settle)
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401

UTC = dt.timezone.utc

FULL = {"x-pool-seats-total": "26", "x-pool-seats-eligible": "11", "x-pool-seats-busy": "11",
        "x-pool-inflight": "142", "x-pool-inflight-max": "304",
        "x-pool-ineligible-reasons": "transport:2,quota:13"}
SPEC_LINE = ("relay busy: 11 of 11 eligible seats full (26 in pool; 15 ineligible: "
             "quota 13, transport 2), 142/304 in flight")
OLD_LINE = "relay busy: all subs at capacity (at the relay)"


@pytest.fixture(autouse=True)
def _pdt():
    """The banner clock is local time; pin PT so '20:02:11' holds on a UTC runner."""
    mp = pytest.MonkeyPatch()
    mp.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        yield
    finally:
        mp.undo()
        time.tzset()


def _measured(**over):
    h = _busy_headers(None)
    h.update(FULL)
    h.update(over)
    return {k: v for k, v in h.items() if v is not None}


def test_all_eligible_full_renders_the_spec_line(_home, monkeypatch):
    a = _agent(monkeypatch, [("claude-btpr", OPUS)])
    _hop(a, _err(_measured()))
    _settle(a)
    [line] = _lines(a)
    print(line)
    assert line.startswith("🔄 Model fallback (relay busy): "), line
    assert line.endswith(f" — {SPEC_LINE}, 20:02:11"), line
    assert "all subs at capacity" not in line
    assert _rows(_home)[0]["notice_text"] == line


def test_headers_absent_keep_todays_line_byte_for_byte(_home, monkeypatch):
    a = _agent(monkeypatch, [("claude-btpr", OPUS)])
    _hop(a, _err(_busy_headers(None)))
    _settle(a)
    [line] = _lines(a)
    assert line.endswith(f" — {OLD_LINE}, 20:02:11"), line


def test_fable_allowance_remainder_wording_unchanged(_home, monkeypatch):
    """The t_634b4ddf cap is the special case: it still wins over the counts."""
    a = _agent(monkeypatch, [("claude-btpr", FABLE)])
    h = _busy_headers("25/26")
    h.update(FULL, **{"x-pool-seats-eligible": "1", "x-pool-seats-busy": "1"})
    _hop(a, _err(h))
    _settle(a)
    [line] = _lines(a)
    assert "1 of 26 seats has Fable allowance left and it is busy" in line, line
    assert "eligible seats full" not in line


def test_same_cause_hops_still_collapse(_home, monkeypatch):
    a = _agent(monkeypatch, [("claude-btpr", OPUS), ("claude-bpr", OPUS), ("claude-alr", OPUS)])
    for _ in range(3):
        _hop(a, _err(_measured()))
    _settle(a)
    [line] = _lines(a)
    assert "fell back through 3 lanes" in line and SPEC_LINE in line, line


def _row(headers):
    base = {"trigger_class": "pool_pressure", "class_source": "relay_header", "hop": "relay",
            "seat": None, "http_status": 503, "err_head": "pool at capacity",
            "from_provider": "claude-dtlr", "from_model": OPUS, "ts": 1793256931.0}
    pp = rq.pool_pressure(headers)
    if pp is not None:
        base["pool_pressure"] = pp
    return base


def test_shapes_unit():
    r = fp.format_cause_rider
    assert r(_row(FULL), tz=UTC).startswith(SPEC_LINE + ", ")
    # Busy unknown (no fresh box reading): state eligibility, never invent "full".
    no_busy = {k: v for k, v in FULL.items() if k != "x-pool-seats-busy"}
    assert r(_row(no_busy), tz=UTC).startswith(
        "relay unavailable: 11 of 26 seats eligible (15 ineligible: quota 13, transport 2), "
        "142/304 in flight, ")
    # Every seat eligible: no ineligible clause.
    all_elig = dict(FULL, **{"x-pool-seats-eligible": "26", "x-pool-seats-busy": "26",
                             "x-pool-ineligible-reasons": "none"})
    assert r(_row(all_elig), tz=UTC).startswith(
        "relay busy: 26 of 26 eligible seats full (26 in pool), 142/304 in flight, ")
    # Missing / junk headers: today's wording.
    for h in ({}, {"x-pool-seats-total": "x", "x-pool-seats-eligible": "3"},
              {"x-pool-seats-total": "3", "x-pool-seats-eligible": "9"}):
        assert r(_row(h), tz=UTC).startswith(OLD_LINE + ", "), h
    # Head label unchanged (collapse key "relay busy").
    assert fp.head_label_override(_row(FULL)) == "relay busy"


def test_stash_keeps_the_pool_headers():
    """The headers must survive the pending-error whitelist to reach the row."""
    class _R:
        status_code = 503
        headers = dict(FULL)

        def json(self):
            return {"error": "pool at capacity"}

    class _E(Exception):
        response = _R()
        status_code = 503
        body = {"error": "pool at capacity"}

    class _A:
        pass

    a = _A()
    fbe.stash_api_error(a, _E("pool at capacity"), 503)
    kept = a._pending_fallback_error["headers"]
    assert {k: kept.get(k) for k in FULL} == FULL


@pytest.mark.parametrize("body,headers,label,detail", [
    # Historical fallback_events row 28270 (2026-11-03 17:29:14 PT):
    # refusal-time counts, NOT a later /health snapshot. The body and class
    # survived in the ledger; the counts survived in its rendered notice.
    ({"error": "no eligible sub: no subscription that serves the requested bridge mode is available; retry shortly or drop the mode"},
     {"x-pool-seats-total": "26", "x-pool-seats-eligible": "0",
      "x-pool-ineligible-reasons": "quota:26", "x-pool-inflight": "0",
      "x-pool-inflight-max": "60"}, "relay quota exhausted", "quota 26"),
    ({"error": "no eligible sub"}, {}, "relay unavailable", "no eligible subscription"),
    ({"error": "no eligible sub"}, {"x-relay-error-class": None},
     "relay unavailable", "no eligible subscription"),
    ({"error": "no eligible sub"},
     {"x-pool-seats-total": "26", "x-pool-seats-eligible": "0",
      "x-pool-ineligible-reasons": "quota:20,transport:6"},
     "relay unavailable", "quota 20, transport 6"),
    ({"error": "no eligible sub"},
     {"x-pool-seats-total": "26", "x-pool-seats-eligible": "0",
      "x-pool-seats-busy": "0", "x-pool-ineligible-reasons": "transport:26"},
     "relay transport unavailable", "transport 26"),
    ({"error": "draining-for-deploy"}, {}, "relay deploying", "deploy"),
    ({"error": "pool at capacity"}, FULL, "relay busy", "11 of 11 eligible seats full"),
    ({"error": "no eligible sub"}, {"x-relay-error-class": "quota_seat"},
     "seat quota", "no eligible sub for the model"),
    ({"error": "upstream connect timed out"}, {"x-relay-error-class": "conn"},
     "connection issue", "connect timeout"),
])
def test_refusal_evidence_names_the_same_cause_on_both_banners(
        _home, monkeypatch, body, headers, label, detail):
    from agent.chat_completion_helpers import chat_pin_own_fallback, format_chat_pin_notice
    from tests.agent.test_fallback_rider_truth import _Err

    a = _agent(monkeypatch, [("claude-btpr", OPUS)])
    a.model = OPUS
    pin = {"provider": a.provider, "model": a.model}
    a._primary_runtime = dict(pin)
    h = _busy_headers(None)
    h.update(headers)
    h = {k: v for k, v in h.items() if v is not None}
    _hop(a, _Err(body, 503, h))
    _settle(a)
    [line] = _lines(a)
    own, compact_label = chat_pin_own_fallback(a, pin)
    compact = format_chat_pin_notice(a.model, a.provider, pin,
                                     own_fallback=own, fallback_label=compact_label)
    assert compact is not None
    assert f"Model fallback ({label}):" in line, line
    assert detail in line, line
    assert f"(fallback: {label})" in compact, compact
    if label != "relay busy":
        assert "relay busy" not in line + compact
        assert "all subs at capacity" not in line
    # Display attribution does not reclassify the refusal used by routing.
    expected_class = h.get("x-relay-error-class", "quota_model")
    assert _rows(_home)[0]["trigger_class"] == expected_class


@pytest.mark.parametrize("busy", [None, "0", "3"])
def test_eligibility_alone_does_not_prove_busy(_home, monkeypatch, busy):
    from tests.agent.test_fallback_rider_truth import _Err

    a = _agent(monkeypatch, [("claude-btpr", OPUS)])
    h = _measured(**{"x-pool-seats-busy": busy})
    _hop(a, _Err({"error": "no eligible sub"}, 503, h))
    _settle(a)
    [line] = _lines(a)
    assert "Model fallback (relay unavailable):" in line, line
    assert "relay busy" not in line and "all subs at capacity" not in line


@pytest.mark.parametrize("reason,label", [
    ("quota", "relay quota exhausted"),
    ("transport", "relay transport unavailable"),
])
def test_text_classified_refusal_uses_counts_for_head_and_rider(reason, label):
    row = _row({"x-pool-seats-total": "26", "x-pool-seats-eligible": "0",
                "x-pool-ineligible-reasons": f"{reason}:26"})
    row.update(trigger_class="quota_model", class_source="text", err_head="no eligible sub")
    assert fp.head_label_override(row) == label
    assert fp.format_cause_rider(row, tz=UTC).startswith(label + ":")


@pytest.mark.parametrize("seat,hop,location", [
    ("seat-a", "relay→bridge", "to seat-a bridge"),
    (None, "bridge→anthropic", "from the bridge"),
])
def test_neutral_refusal_preserves_recorded_location(seat, hop, location):
    row = _row({})
    row.update(err_head="request refused", seat=seat, hop=hop)
    rider = fp.format_cause_rider(row, tz=UTC)
    assert rider.startswith("relay unavailable ")
    assert location in rider
