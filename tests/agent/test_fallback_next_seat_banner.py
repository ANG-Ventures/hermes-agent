"""t_18148399: a relay quota_seat states when the NEXT seat frees (Retry-After =
next_seat_in_s, claude-pool spec D2). The banner says "no seat for ~6 min", not
the upstream weekly reset. Live row: session 20261111_165414_664cbf 11-11
16:54:15, banner "...seat quota exhausted at the relay on all subs; upstream
reset 2026-11-14 23:00" while claude-apr logged next_seat_in_s 374.
"""

from __future__ import annotations

import datetime as dt
import types

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)
import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401
from tests.agent.test_fallback_rider_truth import _Err

BODY = {"type": "error", "error": {"type": "rate_limit_error",
                                   "message": "seat quota exhausted",
                                   "reset_at": "2026-11-15T07:00:00Z"}}


def _headers(**extra):
    return {"x-relay-error-class": "quota_seat", "x-relay-error-hop": "relay",
            "x-relay-seat": "none", "x-relay-eligible": "0", **extra}


def _row(headers):
    a = types.SimpleNamespace(session_id="next-seat")
    fbe.stash_api_error(a, _Err(BODY, 429, headers), 429)
    return fbe.build_row(a, "failover", from_provider="claude-alr",
                         from_model="claude-fable-5-2", to_provider="claude-dtlr",
                         to_model="claude-fable-5-2")


def test_next_seat_in_s_374_renders_six_minutes_and_no_reset_date():
    text = fp.format_cause_rider(_row(_headers(**{"retry-after": "374"})),
                                 tz=dt.timezone.utc)
    assert "no seat for ~6 min" in text, text
    assert "reset" not in text and "2026-11-15" not in text, text
    assert text.startswith("seat quota exhausted at the relay on all subs; no seat for ~6 min, "), text


def test_no_next_seat_keeps_the_upstream_reset():
    text = fp.format_cause_rider(_row(_headers()), tz=dt.timezone.utc)
    assert "upstream reset 2026-11-15 07:00 UTC" in text, text
    assert "no seat for" not in text, text


@pytest.mark.parametrize("cls", ["quota_model", "rate_upstream"])
def test_retry_after_on_other_classes_is_not_a_next_seat(cls):
    row = _row(_headers(**{"x-relay-error-class": cls, "retry-after": "374"}))
    assert "next_seat_in_s" not in row
    assert "no seat for" not in fp.format_cause_rider(row, tz=dt.timezone.utc)


@pytest.mark.parametrize("ra,want", [("30", "~1 min"), ("5400", "~2 h"), ("0", None), ("junk", None)])
def test_next_seat_span_wording(ra, want):
    text = fp.format_cause_rider(_row(_headers(**{"retry-after": ra})), tz=dt.timezone.utc)
    if want is None:
        assert "no seat for" not in text and "upstream reset" in text, text
    else:
        assert f"no seat for {want}" in text and "reset" not in text, text


def test_persisted_banner_names_the_next_seat(_home, monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from agent.error_classifier import FailoverReason
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    a = _alr_agent()
    a._fallback_chain = [{"provider": "claude-dtlr", "model": "claude-fable-5-2"}]
    a.fallback_model = list(a._fallback_chain)
    fbe.stash_api_error(a, _Err(BODY, 429, _headers(**{"retry-after": "374"})), 429)
    assert try_activate_fallback(a, reason=FailoverReason.rate_limit) is True
    text = _rows(_home)[0]["notice_text"]
    assert "no seat for ~6 min" in text, text
    assert "upstream reset" not in text, text
