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

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)

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
        "relay busy: 11 of 26 seats eligible (15 ineligible: quota 13, transport 2), "
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
