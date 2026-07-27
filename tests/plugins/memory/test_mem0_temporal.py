"""Tests for W3-TEMPORAL plugin-side temporal parsing (temporal_parse.py).

DST-boundary correctness is the load-bearing property: a query day in the
reference zone (PT) must resolve to the correct half-open [start,end) UTC window,
spanning 23h on spring-forward, 25h on fall-back, with an exact-midnight instant
landing in-day on the start side. Behavior contracts, not value snapshots.
"""

from datetime import date, datetime, timezone

import pytest

from plugins.memory.mem0.temporal_parse import (
    DEFAULT_TZ,
    created_at_in_window,
    parse_temporal_window,
)


def _iso(dt):
    return dt.isoformat()


# ---------------------------------------------------------------------------
# DST-correct day bounds — the digest-proven property (spring 23h / fall 25h).
# ---------------------------------------------------------------------------

def test_spring_forward_day_is_23h_window():
    """2026-03-08 PT (spring-forward): PT-midnight = 08:00Z, next = 07:00Z next day."""
    start, end = parse_temporal_window("notes on 2026-03-08")
    assert start == datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 9, 7, 0, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 23 * 3600


def test_fall_back_day_is_25h_window():
    """2026-11-01 PT (fall-back): PT-midnight = 07:00Z, next = 08:00Z next day."""
    start, end = parse_temporal_window("notes on 2026-11-01")
    assert start == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 25 * 3600


def test_standard_day_is_24h_window():
    start, end = parse_temporal_window("notes on 2026-06-20")
    assert start == datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 24 * 3600


def test_exact_midnight_instant_lands_in_day_on_start_side():
    """Half-open [start,end): the exact PT-midnight UTC instant is IN-day; the next
    day's PT-midnight UTC instant is the first OUT-of-day instant."""
    start, end = parse_temporal_window("2026-06-20")
    # exact start instant (PT-midnight 06-20 → 07:00:00Z) is in-window
    assert created_at_in_window("2026-06-20T07:00:00+00:00", (start, end)) is True
    # one microsecond before start is out
    assert created_at_in_window("2026-06-20T06:59:59.999999+00:00", (start, end)) is False
    # last in-day instant (23:59:59.999999 PT = 06:59:59.999999Z next day) is in
    assert created_at_in_window("2026-06-21T06:59:59.999999+00:00", (start, end)) is True
    # exact end instant (PT-midnight 06-21 → 07:00:00Z) is OUT (half-open)
    assert created_at_in_window("2026-06-21T07:00:00+00:00", (start, end)) is False


# ---------------------------------------------------------------------------
# Expression coverage — the gold temporal stratum's phrasings.
# ---------------------------------------------------------------------------

REF = date(2026, 6, 24)  # fixed reference so relative expressions are deterministic


@pytest.mark.parametrize("query", [
    "What did I plan to capture during the Phase 2 test on June 20th?",
    "per my notes on June 20th",
    "back on the 20th",
    "on the 20th of June",
    "2026-06-20",
])
def test_june_20_variants_resolve_to_same_pt_day(query):
    win = parse_temporal_window(query, reference_date=REF)
    assert win is not None
    start, end = win
    assert start == datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc)


def test_explicit_date_beats_relative_word():
    """'yesterday's tests on the 21st' must resolve to the 21st, not yesterday."""
    win = parse_temporal_window(
        "What proxy did I use during yesterday's tests on the 21st?", reference_date=REF)
    start, end = win
    assert start == datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc)


def test_yesterday_relative_to_reference():
    win = parse_temporal_window("what did we change yesterday", reference_date=REF)
    start, end = win
    assert start == datetime(2026, 6, 23, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 24, 7, 0, tzinfo=timezone.utc)


def test_today_relative_to_reference():
    win = parse_temporal_window("anything from today", reference_date=REF)
    start, end = win
    assert start == datetime(2026, 6, 24, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 25, 7, 0, tzinfo=timezone.utc)


def test_last_week_spans_prior_seven_days():
    win = parse_temporal_window("what did we change last week", reference_date=REF)
    start, end = win
    # prior 7 full days: 06-17 .. 06-23 inclusive
    assert start == datetime(2026, 6, 17, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 24, 7, 0, tzinfo=timezone.utc)


def test_last_month_spans_previous_calendar_month():
    win = parse_temporal_window("what was decided last month", reference_date=REF)
    start, end = win
    # May 2026 (PT): 05-01 .. 05-31
    assert start == datetime(2026, 5, 1, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)


def test_mid_june_is_days_11_to_20():
    win = parse_temporal_window("what was broken mid-June", reference_date=REF)
    start, end = win
    assert start == datetime(2026, 6, 11, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc)


def test_in_june_spans_whole_month():
    win = parse_temporal_window("what happened in June", reference_date=REF)
    start, end = win
    assert start == datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)


def test_past_n_days():
    win = parse_temporal_window("anything from the past 3 days", reference_date=REF)
    start, end = win
    # prior 3 full days: 06-21 .. 06-23 inclusive (excludes today 06-24)
    assert start == datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 6, 24, 7, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Non-temporal queries must NOT produce a window (no false positives).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "what is my postgres password",
    "how do I get into the media box remotely",
    "the host at 192.168.1.77",
    "",
    "what runs on the box Jellyfin is on",
])
def test_non_temporal_queries_return_none(query):
    assert parse_temporal_window(query, reference_date=REF) is None


def test_future_month_day_rolls_to_previous_year():
    """A month/day later than the reference resolves to last year (a past mention)."""
    win = parse_temporal_window("notes from December 25th", reference_date=REF)
    start, end = win
    assert start == datetime(2025, 12, 25, 8, 0, tzinfo=timezone.utc)  # PST = UTC-8


def test_bare_nth_not_yet_this_month_rolls_to_previous_month():
    win = parse_temporal_window("on the 30th", reference_date=date(2026, 6, 24))
    start, end = win
    # the 30th hasn't happened in June yet → May 30th
    assert start == datetime(2026, 5, 30, 7, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# created_at_in_window — suffix tolerance + bad input.
# ---------------------------------------------------------------------------

def test_created_at_in_window_tolerates_z_and_missing_offset():
    win = parse_temporal_window("2026-06-20")
    assert created_at_in_window("2026-06-20T12:00:00Z", win) is True
    # naive (no offset) treated as UTC
    assert created_at_in_window("2026-06-20T12:00:00", win) is True


def test_created_at_in_window_bad_input_is_false():
    win = parse_temporal_window("2026-06-20")
    assert created_at_in_window(None, win) is False
    assert created_at_in_window("", win) is False
    assert created_at_in_window("not-a-date", win) is False


def test_default_tz_is_pacific():
    assert DEFAULT_TZ == "America/Los_Angeles"
