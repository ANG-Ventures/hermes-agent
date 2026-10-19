"""Fork canary: one cause record per failover; a timer never becomes the cause (t_94a2b0e4).

Surfaces: ``agent/fallback_cause.py`` (fork-only), its call sites in
``agent/fallback_events.py`` (``stash_api_error`` -> ``note_error``,
``build_row`` -> ``take``/``effective``/``_stamp_cause_and_carry``,
``clear_pending``), ``agent/turn_api_error.py`` (``note_wait`` after the retry
sleep), ``agent/chat_completion_helpers.py`` (``clear`` on a served call, the
announce label from ``ledger_row["reason"]``) and ``agent/fallback_policy.py``
(``_cause_wait_clause``, ``_seat_absent_words``, ``_window_cause``).

``fallback_events.py``, ``fallback_policy.py`` and ``fallback_cause.py`` do not
exist upstream; ``turn_api_error.py`` and ``chat_completion_helpers.py`` do. A
parity merge that resolves those two toward upstream drops ``note_wait`` /
``clear`` with no import error, and the 2026-10-18 15:45 banner comes back with
every upstream test green. Each test names the merge-shaped edit that turns it red.
"""

from __future__ import annotations

import datetime as dt

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from tests.agent.test_fallback_failover_cause import (
    ROUTE, _agent, _live_429, _live_cut, _row, _sleep_then_cut,
    test_turn_loop_15_45_sequence_renders_the_true_banner as _turn_loop,
)

UTC = dt.timezone.utc


def test_a_timer_cut_renders_the_recorded_429_and_the_wait():
    """RED-PROVABLE: in ``fallback_events.build_row`` replace
    ``pending = fallback_cause.effective(pending, cause)`` with nothing (the
    timer's evidence wins again): the class reads ``no_first_byte_budget``."""
    a = _agent()
    _sleep_then_cut(a)
    row = _row(a)
    rider = fp.format_cause_rider(row, tz=UTC)
    assert row["trigger_class"] == "rate_upstream" and row["seat"] == "sub-vps-10"
    assert "retried after a 600 s wait" in rider and "no first byte" not in rider, rider


def test_the_cause_is_captured_once_per_block():
    """RED-PROVABLE: drop the ``isinstance(..., FailoverCause)`` early return in
    ``fallback_cause.note_error`` (a later error overwrites the first)."""
    a = _agent()
    fbe.stash_api_error(a, _live_429(), 429, None)
    fbe.stash_api_error(a, _live_429(**{"x-relay-seat": "sub-vps-99"}), 429, None)
    from agent import fallback_cause as fbc

    assert fbc.peek(a).seat == "sub-vps-10"


def test_the_relay_seat_rides_a_timer_ended_row():
    """RED-PROVABLE: drop the ``_stamp_cause_and_carry`` call in ``build_row``."""
    a = _agent()
    fbe.stash_api_error(a, _live_429(), 429, None)
    fbe.stash_api_error(a, _live_cut(), None, None)
    row = _row(a)
    assert (row["seat"], row["route_id"], row["quota_window"]) == ("sub-vps-10", ROUTE, "5h")
    assert "unknown" not in fp.format_cause_rider(row, tz=UTC)


def test_turn_loop_sleep_is_credited_to_the_cause(monkeypatch, tmp_path):
    """RED-PROVABLE: resolve ``agent/turn_api_error.py`` toward upstream, i.e. drop
    the ``note_wait(agent, wait_time)`` after ``interruptible_backoff_sleep``: the
    banner loses "retried after a 600 s wait". Or drop the ``ledger_row["reason"]``
    block in ``_emit_fallback_announce``: the label reads "pool sub stalled"."""
    _turn_loop(monkeypatch, tmp_path)
