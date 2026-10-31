"""t_634b4ddf: a relay busy refusal caused by the per-model allowance names the cap,
and hops of one cause in one turn post ONE fallback line.

Live (Ace, 2026-10-28 20:02-20:03, #1562415294146945114): one Fable turn posted
"Model fallback (relay busy): claude-dtlr -> claude-btpr -- relay busy: all subs at
capacity (at the relay)", then the same line for btpr -> bpr. Relay /health at the
time: ``models_fully_capped: [claude-fable-5-1]``, ``capped_by_model`` 25 of 26 seats
for Fable. The refusal was the busy remainder of Fable's weekly allowance, not load.
The relay states it on the refusal itself: ``x-pool-fable-allowance: 25/26;
earliest_reset=...`` (claude-pool ``fable_allowance_header``, every v2 5xx on a
Fable request).
"""

from __future__ import annotations

import datetime as dt
import time
import types

import pytest

import anthropic  # noqa: F401  (first import outside the home I/O guard, as in the sibling tests)

from agent import fallback_events as fbe
from agent import fallback_policy as fp
from tests.agent.test_fallback_events_ledger import _home, _rows  # noqa: F401

UTC = dt.timezone.utc
TS = dt.datetime(2026, 10, 29, 3, 2, 11, tzinfo=UTC).timestamp()      # 20:02:11 PDT
RESET = "2026-10-30T00:00:00Z"                                          # Thu 17:00 PDT
BUSY_BODY = {"error": "pool at capacity"}
FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5-5"


def _busy_headers(fable="25/26", other_eligible=None):
    h = {"x-relay-error-class": "pool_pressure", "x-relay-error-hop": "relay",
         "x-relay-seat": "none", "retry-after": "5"}
    if fable is not None:
        h["x-pool-fable-allowance"] = f"{fable}; earliest_reset={RESET}"
    if other_eligible is not None:
        h["x-pool-other-eligible"] = str(other_eligible)
    return h


def _pdt(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()


def _agent(monkeypatch, chain):
    from tests.agent.test_fallback_dead_letter_cause import _alr_agent
    from tests.agent.test_fallback_events_ledger import _patch_resolver

    _patch_resolver(monkeypatch)
    monkeypatch.setattr(fbe, "time", types.SimpleNamespace(
        time=lambda: TS, monotonic=time.monotonic, strftime=time.strftime))
    a = _alr_agent()
    a.provider = "claude-dtlr"
    a._fallback_chain = [{"provider": p, "model": m} for p, m in chain]
    a.fallback_model = list(a._fallback_chain)
    return a


def _hop(a, err):
    from agent.chat_completion_helpers import try_activate_fallback
    from agent.error_classifier import FailoverReason

    fbe.stash_api_error(a, err, err.status_code)
    assert try_activate_fallback(a, reason=FailoverReason.overloaded) is True


def _settle(a):
    """The route settled (first byte / served call / turn end): post any held line.
    A no-op on a tree without the hold, so the fixtures fail on wording, not import."""
    from agent import fallback_cause

    flush = getattr(fallback_cause, "flush_banner", None)
    return flush(a) if flush else None


def _lines(a):
    return [m for _k, m in a._announced if m.startswith("🔄 Model fallback")]


def _err(headers):
    from tests.agent.test_fallback_rider_truth import _Err

    return _Err(dict(BUSY_BODY), 503, headers)


# ── D1: the cap wording ─────────────────────────────────────────────────────

def test_busy_refusal_with_25_of_26_capped_names_the_fable_cap(_home, monkeypatch):
    _pdt(monkeypatch)
    try:
        a = _agent(monkeypatch, [("claude-btpr", FABLE)])
        _hop(a, _err(_busy_headers("25/26")))
        _settle(a)
        [line] = _lines(a)
        print(line)
        assert line == ("🔄 Model fallback (Fable weekly cap): claude-dtlr/claude-fable-5-1 → "
                        "claude-btpr/claude-fable-5-1 — 1 of 26 seats has Fable allowance left "
                        "and it is busy; earliest reset Thu 17:00, 20:02:11"), line
        assert "relay busy" not in line and "all subs at capacity" not in line
        # The ledger notice_text is the same line (AC7: measurable with announce off).
        assert _rows(_home)[0]["notice_text"] == line
    finally:
        monkeypatch.undo()
        time.tzset()


def test_busy_refusal_with_wide_eligible_pool_keeps_relay_busy(_home, monkeypatch):
    """10 eligible seats, all saturated: concurrency, not the cap."""
    a = _agent(monkeypatch, [("claude-btpr", FABLE)])
    _hop(a, _err(_busy_headers("16/26", other_eligible=10)))
    _settle(a)
    [line] = _lines(a)
    assert line.startswith("🔄 Model fallback (relay busy): "), line
    assert "relay busy: all subs at capacity (at the relay)" in line, line
    assert "Fable" not in line, line


def test_rider_rules_unit():
    base = {"trigger_class": "pool_pressure", "class_source": "relay_header", "hop": "relay",
            "seat": None, "http_status": 503, "err_head": "pool at capacity",
            "from_provider": "claude-dtlr", "from_model": FABLE, "ts": TS}
    reset = dt.datetime.fromisoformat(RESET.replace("Z", "+00:00")).timestamp()

    def row(n, m=26, **kw):
        return dict(base, fable_allowance={"exhausted": n, "of": m, "earliest_reset": reset}, **kw)

    assert fp.head_label_override(row(25)) == "Fable weekly cap"
    assert fp.format_cause_rider(row(25), tz=UTC).startswith(
        "1 of 26 seats has Fable allowance left and it is busy; earliest reset Fri 00:00")
    assert fp.format_cause_rider(row(24), tz=UTC).startswith(
        "2 of 26 seats have Fable allowance left and all are busy")
    # 3 seats left, or a wide eligible set, or no allowance header: relay busy.
    for r in (row(23), row(25, pool_eligible=10), base):
        assert fp.head_label_override(r) == "relay busy", r
        assert fp.format_cause_rider(r, tz=UTC).startswith(
            "relay busy: all subs at capacity (at the relay)"), r
    # A seat-attributed pool_pressure is not pool-wide; untouched.
    assert "Fable" not in fp.format_cause_rider(row(25, seat="sub-vps-3", hop="relay->bridge"),
                                                tz=UTC)


# ── D2: same-cause hops collapse ────────────────────────────────────────────

def test_three_same_cause_hops_in_one_turn_post_one_banner(_home, monkeypatch):
    _pdt(monkeypatch)
    try:
        a = _agent(monkeypatch, [("claude-btpr", FABLE), ("claude-bpr", FABLE),
                                 ("claude-alr", OPUS)])
        for _ in range(3):
            _hop(a, _err(_busy_headers("25/26")))
        assert len(_lines(a)) <= 1, "one line, never one per hop"
        _settle(a)               # first byte / served call / turn end
        lines = _lines(a)
        print(lines)
        assert lines == [
            "🔄 Model fallback (Fable weekly cap): fell back through 3 lanes to "
            "claude-alr/claude-opus-5-5 (from claude-dtlr/claude-fable-5-1) — 1 of 26 seats "
            "has Fable allowance left and it is busy; earliest reset Thu 17:00, 20:02:11"], lines
        # Every hop still has its own ledger row.
        assert [r["to_provider"] for r in _rows(_home)] == ["claude-btpr", "claude-bpr",
                                                            "claude-alr"]
        assert _settle(a) is None    # posted once
    finally:
        monkeypatch.undo()
        time.tzset()


def test_cap_then_exhausted_allowance_is_one_cause(_home, monkeypatch):
    """dtlr busy-cap -> btpr busy-cap -> bpr refuses with 26/26 (Fable weekly
    limit): the same allowance, one line."""
    from tests.agent.test_fallback_relay_quota_banner import CAPPED_BODY
    from tests.agent.test_fallback_rider_truth import _Err

    a = _agent(monkeypatch, [("claude-btpr", FABLE), ("claude-bpr", FABLE),
                             ("claude-alr", OPUS)])
    _hop(a, _err(_busy_headers("25/26")))
    _hop(a, _err(_busy_headers("25/26")))
    _hop(a, _Err(dict(CAPPED_BODY), 503, {
        "x-relay-error-class": "quota_model", "x-relay-error-hop": "relay",
        "x-relay-seat": "none", "x-relay-eligible": "0",
        "x-pool-fable-allowance": f"26/26; earliest_reset={RESET}"}))
    _settle(a)
    [line] = _lines(a)
    assert line.startswith("🔄 Model fallback (Fable weekly cap): fell back through 3 lanes to "
                           "claude-alr/claude-opus-5-5"), line


def test_different_cause_flushes_the_held_banner_first(_home, monkeypatch):
    from tests.agent.test_fallback_rider_truth import _Err

    a = _agent(monkeypatch, [("claude-btpr", FABLE), ("claude-alr", OPUS)])
    _hop(a, _err(_busy_headers("25/26")))
    _hop(a, _Err({"error": "upstream unreachable"}, 502, {
        "x-relay-error-class": "conn", "x-relay-error-hop": "relay->bridge",
        "x-relay-seat": "sub-vps-7"}))
    lines = _lines(a)
    assert len(lines) == 2, lines
    assert lines[0].startswith("🔄 Model fallback (Fable weekly cap): claude-dtlr/"), lines
    assert "claude-btpr/claude-fable-5-1 → claude-alr/claude-opus-5-5" in lines[1], lines


def test_a_new_turn_does_not_merge_with_the_last(_home, monkeypatch):
    a = _agent(monkeypatch, [("claude-btpr", FABLE), ("claude-bpr", FABLE)])
    _hop(a, _err(_busy_headers("25/26")))
    a._current_turn_id = "t2"
    _hop(a, _err(_busy_headers("25/26")))
    _settle(a)
    assert len(_lines(a)) == 2, _lines(a)


@pytest.mark.parametrize("chain", [
    [("claude-alr", OPUS)],                                             # one hop
    [("claude-btpr", FABLE), ("claude-bpr", FABLE), ("claude-alr", OPUS)],  # live chain
], ids=["one-hop", "three-hops"])
def test_pinned_model_capped_on_all_lanes_announces_every_turn(_home, monkeypatch, chain):
    """t_f1f73872: a session whose pinned Fable is capped on every lane walks the
    SAME chain to alr/opus on consecutive turns, so the route transitions repeat
    byte-for-byte. A dedupe that outlived the turn would swallow every banner
    after the first (it can only bite the one-hop walk: on a longer chain the
    next turn's first hop differs from the last one announced). Each turn posts
    its own line (live 2026-10-29 02:20 / 02:25 / 02:54, session
    20261023_104818_32a4431a, all three posted to Discord)."""
    from agent.turn_context import _bind_turn_identity

    a = _agent(monkeypatch, chain)
    per_turn = []
    for turn in range(3):
        if turn:
            # Next turn starts back on the pin (sticky_resume -> primary).
            a.provider, a.model = "claude-dtlr", FABLE
            a._fallback_index, a._fallback_activated = 0, False
        _bind_turn_identity(a, None, None, None, None, None)
        before = len(_lines(a))
        for _ in chain:
            _hop(a, _err(_busy_headers("25/26")))
        _settle(a)
        per_turn.append(_lines(a)[before:])
    assert [len(p) for p in per_turn] == [1, 1, 1], per_turn
    for [line] in per_turn:
        assert line.startswith("🔄 Model fallback (Fable weekly cap): "), line
        assert "claude-dtlr/claude-fable-5-1" in line and "claude-alr/claude-opus-5-5" in line, line


class _SettleAgent:
    """Records every chat line (fallback banner and diagnostic notices) in post order."""

    def __init__(self):
        self.posted = []
        self._current_turn_id = "turn-1"

    def _emit_status(self, msg):
        self.posted.append(msg)

    def _emit_diagnostic_status(self, msg):
        self.posted.append(msg)

    def _touch_activity(self, *_a, **_k):
        pass


def _held(agent, msg="Model fallback (relay busy): a -> b"):
    from agent import fallback_cause as fbc
    fbc.hold_banner(agent, msg=msg, key="busy", first_from="a", to_label="b", hops=1,
                    label="relay busy", rider=None)
    assert agent.posted == []
    return msg


def test_first_real_stream_chunk_posts_the_held_banner_and_a_ping_does_not():
    from agent import chat_completion_helpers as cch

    agent = _SettleAgent()
    msg = _held(agent)
    call = object.__new__(cch._StreamingCall)
    call.agent, call.last_chunk_time = agent, {"t": 0.0}
    call._count_chunk({}, None, heartbeat=True)
    assert agent.posted == []
    call._count_chunk({}, None)
    call._count_chunk({}, None)
    assert agent.posted == [msg]


def test_served_call_posts_the_held_banner(monkeypatch):
    from agent import chat_completion_helpers as cch

    monkeypatch.setattr(cch, "_note_billed_response", lambda *a, **k: None)
    monkeypatch.setattr(cch, "_note_api_call_recording_failure", lambda *a, **k: None)
    pooled = sorted(cch._POOLED_PROVIDERS)[0]   # no pool_headers: returns after the settle
    monkeypatch.setattr(cch, "_serving_route", lambda a: {"provider": pooled, "api_mode": "x"})
    agent = _SettleAgent()
    msg = _held(agent)
    cch._record_successful_api_call(agent, types.SimpleNamespace())
    assert agent.posted == [msg]


def test_turn_end_posts_the_held_banner_even_when_the_turn_raises(monkeypatch):
    from agent import conversation_loop as cl

    agent = _SettleAgent()
    msg = _held(agent)

    def _boom(*_a, **_k):
        raise RuntimeError("terminal")

    monkeypatch.setattr(cl, "_run_conversation_turn", _boom)
    with pytest.raises(RuntimeError):
        cl.run_conversation(agent, "hi")
    assert agent.posted == [msg]


def test_stream_wait_notice_posts_after_the_held_banner(monkeypatch):
    from agent import chat_completion_stream_monitor as csm

    monkeypatch.setattr(csm.wb, "notice_due", lambda a: "still waiting, 2 lanes tried")
    monkeypatch.setattr(csm.wb, "should_cut_attempt", lambda a: False)
    agent = _SettleAgent()
    msg = _held(agent)
    mon = object.__new__(csm.StreamingWaitMonitor)
    mon.agent = agent
    assert mon._poll_wall_budget() is False
    assert agent.posted == [msg, "still waiting, 2 lanes tried"]


def test_retry_backoff_wait_notice_posts_after_the_held_banner(monkeypatch):
    from agent import fallback_wall_budget as wb
    from agent import turn_api_error as tae
    from agent.error_classifier import FailoverReason

    monkeypatch.setattr(tae, "compute_error_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(tae, "interruptible_backoff_sleep", lambda *a, **k: None)
    monkeypatch.setattr(wb, "notice_due", lambda a: "still waiting, retrying")
    monkeypatch.setattr(wb, "note_quiet", lambda *a, **k: None)
    agent = _SettleAgent()
    msg = _held(agent)
    classified = types.SimpleNamespace(reason=FailoverReason.timeout, retryable=True,
                                       should_compress=False, should_fallback=False,
                                       display_reason=None)
    retry = types.SimpleNamespace(restart_with_redirected_messages=False)
    verdict = tae.settle_unrecovered_error(
        agent, api_error=RuntimeError("upstream timeout"), classified=classified, _retry=retry,
        status_code=None, error_msg="upstream timeout", is_context_length_error=False,
        is_rate_limited=False, _is_zai_coding_overload=False, _provider="p", _base="", _model="m",
        messages=[], api_messages=[], api_kwargs={}, active_system_prompt="", conversation_history=[],
        approx_tokens=0, retry_count=0, max_retries=3, compression_attempts=0, api_call_count=1,
    )
    assert verdict.action == "fallthrough"
    assert agent.posted == [msg, "still waiting, retrying"]
