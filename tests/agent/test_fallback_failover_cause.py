"""Fallback banners that cannot lie: one cause record per failover (t_94a2b0e4).

Spec ``plans/2026-10-18_fallback-announce-truth-SPEC.md`` §F1/§F2. The four
contradiction classes measured over 14 days of ``fallback_events`` (242
failovers), each fixtured here from a live row shape:

* 45 sleep-as-stall: a relay 429 + 600 s Retry-After sleep, then the wall budget
  cut the retry and the banner read "pool sub stalled mid-turn ... no first byte
  in 614 s ... seat unknown — cut before headers" (2026-10-18 15:45:20 /
  15:55:21 PT, route d4c7ed5546ba430dba56f87673e3d1dc, sub-vps-10).
* 64 unknown seat: "sub unknown" while the relay had sent the seat.
* 53 windowless rate limit: "(rate limit)" while the response named the window.
* 21 label/detail mismatch: "relay busy" over "seat drained", "connection issue"
  over "relay box at session capacity", "credit exhausted" over "OAuth revoked".
"""

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import pytest

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from agent.fallback_wall_budget import FallbackWallBudgetExceeded
from tests.agent.test_relay_error_class import RelayError

UTC = dt.timezone.utc
FABLE = "claude-fable-5-1"
ROUTE = "d4c7ed5546ba430dba56f87673e3d1dc"
T_429 = dt.datetime(2026, 10, 18, 22, 45, 20, tzinfo=UTC).timestamp()   # 15:45:20 PDT
ANTHROPIC_RATE_TEXT = ("This request would exceed your account's rate limit. "
                       "Please try again later.")


def _live_429(**extra_headers):
    """The 15:45:20 relay answer: 429, classed rate_upstream by wording (§F3 is
    the relay fix), Retry-After 600, seat + route + Anthropic's 5h rejected."""
    headers = {"x-relay-error-class": "rate_upstream", "x-relay-error-hop": "bridge->upstream",
               "x-relay-seat": "sub-vps-10", "x-pool-route-id": ROUTE, "retry-after": "600",
               "anthropic-ratelimit-unified-5h-status": "rejected",
               "anthropic-ratelimit-unified-representative-claim": "five_hour"}
    headers.update(extra_headers)
    return RelayError(429, {"type": "error", "error": {"type": "rate_limit_error",
                                                       "message": ANTHROPIC_RATE_TEXT}},
                      headers)


def _live_cut(elapsed=614.0):
    return FallbackWallBudgetExceeded("cut", lane="claude-alr", elapsed_s=elapsed, pool_headers={})


def _agent(provider="claude-alr"):
    return SimpleNamespace(_pending_fallback_error=None, session_id="20261007_215000_ac99b02a",
                           _current_turn_id="20261007_215000_ac99b02a:t1", provider=provider,
                           model=FABLE)


def _row(a, reason="pool_stalled", to=("claude-dtlr", FABLE)):
    row = fbe.build_row(a, "failover", from_provider=a.provider, from_model=FABLE,
                        to_provider=to[0], to_model=to[1], reason=reason)
    row["first_err_ts"] = T_429
    return row


def _fbc():
    # Imported per test so the module collects on main (RED, not a collection error).
    from agent import fallback_cause
    return fallback_cause


def _sleep_then_cut(a, slept=600.0):
    fbe.stash_api_error(a, _live_429(), 429, None, elapsed_s=2.0)
    _fbc().note_wait(a, slept)
    fbe.stash_api_error(a, _live_cut(), None, None, elapsed_s=614.0)


# ── 45: sleep-as-stall ─────────────────────────────────────────────────────


def test_timer_cut_after_a_429_sleep_renders_the_recorded_cause():
    a = _agent()
    _sleep_then_cut(a)
    row = _row(a)
    rider = fp.format_cause_rider(row, tz=UTC)
    assert row["trigger_class"] == "rate_upstream" and row["class_source"] == "relay_header", row
    assert row["reason"] == "rate_limit", row
    assert row["http_status"] == 429 and row["seat"] == "sub-vps-10" and row["route_id"] == ROUTE
    assert rider == ("Fable 5h limit (Anthropic 429) on sub-vps-10; retried after a 600 s wait, "
                     "moved on at 614 s, 22:45:20"), rider
    for banned in ("no first byte", "stalled", "unknown", "cut before headers"):
        assert banned not in rider, rider


def test_timer_never_overwrites_the_first_error():
    """Mutation guard: let the timer exception replace the cause and this goes red."""
    a = _agent()
    _sleep_then_cut(a)
    cause = _fbc().peek(a)
    assert cause is not None and cause.http_status == 429 and cause.seat == "sub-vps-10"
    assert cause.evidence["exc"] == "RelayError" and cause.slept_s == 600.0
    assert a._pending_fallback_error["exc"] == "FallbackWallBudgetExceeded"
    assert _fbc().effective(a._pending_fallback_error, cause) is cause.evidence


def test_a_served_call_ends_the_block():
    """A recovered 429 is not the cause of a later, unrelated cut."""
    a = _agent()
    fbe.stash_api_error(a, _live_429(), 429, None)
    fbe.clear_pending(a)            # what a served call / a new turn does
    fbe.stash_api_error(a, _live_cut(), None, None)
    row = _row(a)
    assert row["trigger_class"] == "no_first_byte_budget", row
    assert "cause_wait" not in row


def test_cut_with_no_prior_error_stays_a_budget_cut_worded_for_a_buffering_lane():
    a = _agent()
    fbe.stash_api_error(a, _live_cut(600.0), None, None)
    row = _row(a)
    rider = fp.format_cause_rider(row, tz=UTC)
    assert row["trigger_class"] == "no_first_byte_budget"
    assert rider == "no reply in 600 s on claude-alr (relay sent no headers before the cut), 22:45:20"
    assert fp.head_label_override(row) == "no reply within the turn budget"
    assert "stalled" not in rider and "unknown" not in rider
    # A non-buffering lane keeps the first-byte wording.
    b = _agent("cpa")
    fbe.stash_api_error(b, FallbackWallBudgetExceeded("cut", lane="cpa", elapsed_s=600.0), None, None)
    assert fp.format_cause_rider(_row(b), tz=UTC).startswith("no first byte in 600 s on cpa")


def test_cause_on_another_lane_is_not_this_lanes_cause():
    a = _agent()
    fbe.stash_api_error(a, _live_429(), 429, None)
    a.provider = "claude-dtlr"      # walked on without a failover row consuming it
    fbe.stash_api_error(a, RelayError(503, {"error": "upstream connect timed out"},
                                      {"x-relay-error-class": "conn", "x-relay-seat": "sub-vps-3"}),
                        503, None)
    cause = _fbc().peek(a)
    assert cause.lane == "claude-dtlr" and cause.seat == "sub-vps-3"


# ── 64: unknown seat ───────────────────────────────────────────────────────


def test_timer_cut_carries_the_last_relay_headers():
    a = _agent()
    fbe.stash_api_error(a, _live_429(), 429, None)
    fbe.stash_api_error(a, _live_cut(), None, None)
    row = _row(a)
    assert row["seat"] == "sub-vps-10" and row["route_id"] == ROUTE and row["quota_window"] == "5h"


def test_a_different_seat_drops_the_old_seats_window_and_route():
    """Prism e20f57909f40: seat A said 5h; seat B (no window, no route) must not
    inherit A's window or route."""
    a = _agent()
    fbe.stash_api_error(a, _live_429(), 429, None)
    fbe.stash_api_error(a, RelayError(429, {"type": "error", "error": {
        "type": "rate_limit_error", "message": ANTHROPIC_RATE_TEXT}},
        {"x-relay-error-class": "rate_upstream", "x-relay-seat": "sub-vps-11"}), 429, None)
    carry = a._failover_carry
    assert carry["seat"] == "sub-vps-11"
    assert "quota_window" not in carry and "route_id" not in carry, carry
    # Prism 5152328892a7: B's own (non-timer) failover row does not get A's window.
    row = _row(a, reason="rate_limit")
    assert row["seat"] == "sub-vps-11" and not row.get("quota_window"), row
    assert fp.head_label_override(row) is None


def test_no_rendered_banner_says_unknown_when_the_seat_header_was_present():
    """Every pooled shape: x-relay-seat present -> the banner names it."""
    shapes = [
        RelayError(503, {"error": "seat drained"}, {"x-relay-error-class": "pool_pressure",
                                                    "x-relay-error-hop": "relay"}),
        RelayError(503, {"error": "upstream connect timed out"},
                   {"x-relay-error-class": "conn", "x-relay-error-hop": "relay->bridge"}),
        _live_429(),
    ]
    for err in shapes:
        a = _agent()
        err.response.headers["x-relay-seat"] = "sub-vps-12"
        fbe.stash_api_error(a, err, err.status_code, None)
        rider = fp.format_cause_rider(_row(a, reason="overloaded"), tz=UTC)
        assert "sub-vps-12" in rider and "unknown" not in rider, rider


def test_absent_seat_says_why_in_words():
    a = _agent()
    fbe.stash_api_error(a, RelayError(503, {"error": "seat drained"},
                                      {"x-relay-error-class": "pool_pressure",
                                       "x-relay-error-hop": "relay", "x-relay-seat": "none",
                                       "x-pool-route-id": "cbd714f7413c433aabde725aadc00df3"}),
                        503, None)
    row = _row(a, reason="overloaded")
    rider = fp.format_cause_rider(row, tz=UTC)
    assert rider == "seat drained at the relay (relay sent no seat), 22:45:20", rider
    assert row["route_id"] == "cbd714f7413c433aabde725aadc00df3"


# ── 53: windowless rate limit ──────────────────────────────────────────────


def test_rate_limit_label_names_the_window_the_response_named():
    a = _agent()
    fbe.stash_api_error(a, _live_429(), 429, None)
    row = _row(a, reason="rate_limit")
    assert fp.head_label_override(row) == "Fable 5h limit"
    b = _agent()
    fbe.stash_api_error(b, RelayError(503, {"error": "no eligible sub for the requested model"},
                                      {"x-relay-error-class": "quota_seat",
                                       "x-relay-quota-window": "seven_day_overage_included",
                                       "x-relay-seat": "sub-vps-4"}), 503, None)
    rb = _row(b, reason="rate_limit")
    assert rb["quota_window"] == "7d_oi"
    assert fp.head_label_override(rb) == "Fable weekly (7d_oi) limit"


@pytest.mark.parametrize("window,word", [("five_hour", "5h limit"), ("seven_day", "weekly limit")])
def test_no_eligible_rider_keeps_the_response_quota_window(window, word):
    a = _agent()
    fbe.stash_api_error(a, RelayError(503, {"error": "no eligible sub"},
                                      {"x-relay-error-class": "quota_seat",
                                       "x-relay-quota-window": window,
                                       "x-relay-eligible": "0"}), 503, None)
    row = _row(a, reason="rate_limit")
    assert word in fp.format_cause_rider(row, tz=UTC)


def test_genuine_rate_upstream_with_no_window_keeps_rate_limit():
    a = _agent()
    fbe.stash_api_error(a, RelayError(429, {"type": "error", "error": {
        "type": "rate_limit_error", "message": ANTHROPIC_RATE_TEXT}},
        {"x-relay-error-class": "rate_upstream", "x-relay-seat": "sub-vps-9"}), 429, None)
    row = _row(a, reason="rate_limit")
    assert fp.head_label_override(row) is None
    assert fp.format_cause_rider(row, tz=UTC).startswith("account rate limit")


# ── 21: label/detail mismatch, one table row each ──────────────────────────


@pytest.mark.parametrize("cls,body,headers,label,cause", [
    ("pool_pressure", {"error": "seat drained"},
     {"x-relay-error-class": "pool_pressure", "x-relay-error-hop": "relay"},
     "seat drained", "seat drained"),
    ("conn", {"type": "error", "error": {"code": "tui_capacity", "message": "no slot"}},
     {"x-relay-error-class": "conn", "x-relay-error-hop": "bridge->upstream",
      "x-relay-seat": "sub-vps-8"},
     "relay box at session capacity", "relay box at session capacity"),
])
def test_label_and_detail_come_from_the_same_row(cls, body, headers, label, cause):
    a = _agent()
    fbe.stash_api_error(a, RelayError(503, body, headers), 503, None)
    row = _row(a, reason="overloaded")
    assert fp.head_label_override(row) == label
    assert cause in fp.format_cause_rider(row, tz=UTC)


def test_credit_exhaustion_is_not_oauth_revoked():
    a = _agent("xai-oauth")
    err = RelayError(403, {"code": "personal-team-blocked:spending-limit",
                           "error": "You have run out of credits or need a Grok subscription."},
                     {}, text="Error code: 403 - {'code': 'personal-team-blocked:spending-limit', "
                              "'error': 'You have run out of credits or need a Grok subscription.'}")
    fbe.stash_api_error(a, err, 403, None)
    row = _row(a, reason="billing", to=("openai-codex", "gpt-6.1-sol"))
    rider = fp.format_cause_rider(row, tz=UTC)
    assert "OAuth revoked" not in rider and "out of credits" in rider, rider


# ── the real turn loop: 429 -> 600 s sleep -> cut -> next lane ─────────────


def test_turn_loop_15_45_sequence_renders_the_true_banner(monkeypatch, tmp_path):
    from unittest.mock import patch

    import agent.auxiliary_client as ac
    from tests.agent.test_fallback_events_ledger import _rows
    from tests.agent.test_relay_quota_retry_handoff import make_agent
    from tests.run_agent.test_pool_capacity_503_retry import _FakeClock, _fallback_client, _response

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("blackbox:\n  enabled: true\n")
    ac.clear_runtime_main()
    statuses: list = []
    agent = make_agent(statuses)
    agent.model = FABLE
    # make_agent's two local relay keys are what deferred the eager fallback
    # live: the harness retried the same lane after the Retry-After sleep.
    sleeps: list = []
    clock = _FakeClock(sleeps)
    script = [_live_429(), _live_cut()]
    attempts: list = []

    def wire(*_a, **_k):
        attempts.append(agent.provider)
        if script:
            raise script.pop(0)
        return _response("served", agent.model)

    agent._disable_streaming = True
    try:
        with (
            patch("agent.chat_completion_helpers.should_use_direct_api_call", return_value=True),
            patch.object(agent, "_interruptible_api_call", side_effect=wire),
            patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.auxiliary_client.resolve_provider_client",
                  side_effect=lambda *args, **kw: (_fallback_client(), kw.get("model", agent.model))),
            patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
            patch("hermes_cli.config.read_raw_config",
                  return_value={"model": {"announce_route_change": True}}),
            patch("agent.turn_recovery.time", clock),
        ):
            result = agent.run_conversation("hello")
    finally:
        ac.clear_runtime_main()
    assert result["completed"] is True, result
    assert attempts == ["claude-alr", "claude-alr", "claude-dtlr"], attempts
    rows = [r for r in _rows(tmp_path) if r["kind"] == "failover"]
    assert len(rows) == 1, rows
    text = rows[0]["notice_text"]
    print(text)
    assert rows[0]["trigger_class"] == "rate_upstream" and rows[0]["seat"] == "sub-vps-10"
    assert rows[0]["route_id"] == ROUTE
    assert text.startswith("🔄 Model fallback (Fable 5h limit): claude-alr/claude-fable-5-1 → "
                           "claude-dtlr/"), text
    assert "Fable 5h limit (Anthropic 429) on sub-vps-10" in text, text
    assert "retried after a 600 s wait, moved on at 614 s" in text, text
    for banned in ("no first byte", "stalled", "unknown", "cut before headers"):
        assert banned not in text, text


# ── notice side: a retry sleep is never "turn in progress" (Ace 2026-10-18 21:16) ──


def _armed_alr(waited_s):
    from agent import fallback_wall_budget as wb
    a = _agent()
    a._fallback_chain = [{"provider": "claude-dtlr", "model": FABLE}]
    a._fallback_index = 0
    a._wall_budget = None
    wb.arm(a, now=time.monotonic() - waited_s)
    return a, wb


def _live_2112_429():
    """agent.log 21:12:15 session 20260927_135034_52aefa: 429, "Retrying API call in 150.0s"."""
    return _live_429(**{"retry-after": "150"})


def test_notice_during_a_retry_sleep_names_the_wait_and_the_cause():
    a, wb = _armed_alr(256)
    fbe.stash_api_error(a, _live_2112_429(), 429, None)
    _fbc().note_sleep(a, 150.0)
    notice = wb.notice_due(a)
    assert notice is not None
    assert notice.startswith("⏳ waiting 150 s: Fable 5h limit on sub-vps-10 (Anthropic 429), "
                             "retrying then falling back"), notice
    for banned in ("turn in progress", "buffering lane", "first byte", "first reply"):
        assert banned not in notice, notice


def test_notice_on_the_retry_after_a_sleep_says_it_is_a_retry():
    a, wb = _armed_alr(400)
    fbe.stash_api_error(a, _live_2112_429(), 429, None)
    _fbc().note_sleep(a, 150.0)
    _fbc().note_wait(a, 150.0)
    notice = wb.notice_due(a)
    assert notice.startswith("⏳ retrying claude-alr/claude-fable-5-1 after a 150 s wait: "
                             "Fable 5h limit on sub-vps-10 (Anthropic 429)"), notice
    assert "turn in progress" not in notice


def test_notice_with_no_recorded_error_keeps_the_in_flight_wording():
    a, wb = _armed_alr(256)
    notice = wb.notice_due(a)
    assert notice.startswith("⏳ turn in progress on claude-alr/claude-fable-5-1"), notice


def test_turn_loop_2112_sleep_posts_the_waiting_notice(monkeypatch, tmp_path):
    """The real turn loop: 429 + Retry-After 150 with the clock already 256 s old.
    Mutation guard: drop the note_sleep / notice read in turn_api_error and the
    status reads "turn in progress" (or nothing)."""
    from unittest.mock import patch

    import agent.auxiliary_client as ac
    from tests.agent.test_relay_quota_retry_handoff import make_agent
    from tests.run_agent.test_pool_capacity_503_retry import _FakeClock, _fallback_client, _response

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ac.clear_runtime_main()
    statuses: list = []
    agent = make_agent(statuses)
    agent.model = FABLE
    sleeps: list = []
    clock = _FakeClock(sleeps)
    script = [_live_2112_429()]
    emitted: list = []

    def wire(*_a, **_k):
        from agent import fallback_wall_budget as wb
        if script:
            # The no-first-byte clock has run 256 s when the 429 lands.
            st = getattr(agent, "_wall_budget", None)
            if isinstance(st, dict):
                st["started"] = time.monotonic() - 256
            raise script.pop(0)
        return _response("served", agent.model)

    agent._disable_streaming = True
    try:
        with (
            patch("agent.chat_completion_helpers.should_use_direct_api_call", return_value=True),
            patch.object(agent, "_interruptible_api_call", side_effect=wire),
            patch.object(agent, "_emit_diagnostic_status", side_effect=emitted.append),
            patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.auxiliary_client.resolve_provider_client",
                  side_effect=lambda *args, **kw: (_fallback_client(), kw.get("model", agent.model))),
            patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
            patch("hermes_cli.config.read_raw_config",
                  return_value={"model": {"announce_route_change": True}}),
            patch("agent.turn_recovery.time", clock),
        ):
            result = agent.run_conversation("hello")
    finally:
        ac.clear_runtime_main()
    assert result["completed"] is True, result
    waits = [m for m in emitted if isinstance(m, str) and m.startswith("⏳")]
    assert waits, emitted
    assert waits[0].startswith("⏳ waiting 150 s: Fable 5h limit on sub-vps-10 (Anthropic 429)"), waits
    assert not any("turn in progress" in m or "buffering lane" in m for m in waits), waits
