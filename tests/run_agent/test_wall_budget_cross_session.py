"""Cross-session dedupe of the wall-budget "still waiting" notice (t_78b5a5bb).

2026-10-28 17:00-18:00 PT: 47 identical "still waiting for a first reply" lines across
5 Discord sessions on one gateway, all from one slow window on claude-alr/claude-fable-5-1.
Each session's clock knew nothing about its siblings. The registry makes the shared
case one line per wait in the thread, and one open/close pair per window in #logs.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import fallback_wall_budget as wb

FABLE = "claude-fable-5-1"
ALR = "http://127.0.0.1:18810/v1"
CHAIN = [{"provider": "claude-dtlr", "model": FABLE}, {"provider": "cpa", "model": "gpt-6.1-sol"}]
CFG = {"fallback": {"still_waiting_notice_seconds": 120, "no_first_byte_budget_seconds": 600}}
CFG_OFF = {**CFG, "fallback_wall_budget": {"cross_session_dedupe": False}}


def _agent(session: str, provider: str = "claude-alr", model: str = FABLE, base_url: str = ALR):
    return SimpleNamespace(provider=provider, model=model, base_url=base_url, session_id=session,
                           _fallback_chain=CHAIN, _fallback_index=0, _wall_budget=None)


@pytest.fixture
def emits(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(wb, "post_logs", sent.append, raising=False)
    return sent


@pytest.fixture
def cfg():
    with patch("hermes_cli.config.load_config_readonly", return_value=CFG):
        yield


def _poll(agents, now):
    """One monitor poll per agent at ``now`` (the stream monitor polls every 0.3 s)."""
    return [wb.notice_due(a, now=now) for a in agents]


def _run(agents, t0, t1, step=10.0):
    """Poll every armed agent every ``step`` s over [t0, t1]; the lines each one posted."""
    posted = {id(a): [] for a in agents}
    t = t0
    while t <= t1:
        for a in agents:
            text = wb.notice_due(a, now=t)
            if text:
                posted[id(a)].append((t, text))
        t += step
    return [posted[id(a)] for a in agents]


def test_two_sessions_one_lane_get_the_shared_line_once_then_silence(cfg, emits):
    a1, a2 = _agent("s1"), _agent("s2")
    wb.arm(a1, now=0.0)
    wb.arm(a2, now=0.0)
    p1, p2 = _run([a1, a2], 10.0, 590.0)
    shared = ("⏳ provider slow: 2 sessions waiting on claude-alr/claude-fable-5-1 "
              "(oldest 120s); this turn 120s, cut at 600s")
    # agent 1 is first past its interval while agent 2 is too: both get the shared line ONCE.
    assert p1 == [(120.0, shared)]
    assert p2 == [(120.0, shared)]
    assert emits == [], "two sessions never open a #logs window"


def test_first_past_the_interval_alone_keeps_the_solo_line_then_goes_shared_once(cfg, emits):
    a1, a2 = _agent("s1"), _agent("s2")
    wb.arm(a1, now=0.0)
    wb.arm(a2, now=50.0)
    p1, p2 = _run([a1, a2], 10.0, 590.0)
    assert len(p1) == 2 and p1[0][0] == 120.0 and p1[0][1].startswith("⏳ turn in progress on ")
    assert p1[1] == (240.0, "⏳ provider slow: 2 sessions waiting on claude-alr/claude-fable-5-1 "
                            "(oldest 240s); this turn 240s, cut at 600s")
    assert p2 == [(170.0, "⏳ provider slow: 2 sessions waiting on claude-alr/claude-fable-5-1 "
                          "(oldest 170s); this turn 120s, cut at 600s")]


def test_shared_line_returns_to_solo_when_the_sibling_recovers(cfg, emits):
    a1, a2 = _agent("s1"), _agent("s2")
    wb.arm(a1, now=0.0)
    wb.arm(a2, now=0.0)
    p1, _ = _run([a1, a2], 10.0, 250.0)
    assert len(p1) == 1 and "provider slow" in p1[0][1]
    wb.disarm(a2, now=255.0)  # a2's lane recovered
    p1, = _run([a1], 260.0, 370.0)
    assert len(p1) == 1 and p1[0][1].startswith("⏳ turn in progress"), p1


def test_cut_fired_is_a_changed_condition(cfg, emits):
    a1, a2 = _agent("s1"), _agent("s2")
    wb.arm(a1, now=0.0)
    wb.arm(a2, now=0.0)
    _run([a1, a2], 10.0, 590.0)
    assert wb.should_cut_attempt(a2, now=600.0)
    _, p2 = _run([a1, a2], 600.0, 720.0)
    assert len(p2) == 1 and p2[0][1].startswith("⏳ provider slow: 2 sessions"), p2


def test_other_lane_is_not_a_sibling(cfg, emits):
    a1, a2 = _agent("s1"), _agent("s2", provider="claude-dtlr")
    wb.arm(a1, now=0.0)
    wb.arm(a2, now=0.0)
    wb.notice_due(a1, now=121.0)
    text = wb.notice_due(a2, now=121.0)
    assert text and "provider slow" not in text


def test_three_sessions_open_one_logs_window_fourth_adds_none_disarm_closes_it(cfg, emits):
    agents = [_agent(f"s{i}") for i in range(4)]
    for a in agents[:3]:
        wb.arm(a, now=0.0)
    wb.notice_due(agents[0], now=121.0)
    wb.notice_due(agents[1], now=121.0)
    assert emits == []
    wb.notice_due(agents[2], now=121.0)
    assert emits == ["⏳ provider slow window open: lane claude-alr/claude-fable-5-1, "
                     "3 sessions waiting (oldest 121s)"]
    wb.arm(agents[3], now=130.0)
    wb.note_status(agents[3], 529)
    wb.note_status(agents[0], 502)
    wb.note_status(agents[0], 429)  # not a slow-window status
    _run(agents, 130.0, 390.0)
    assert len(emits) == 1, emits
    for a in agents[:3]:
        wb.disarm(a, now=400.0)
    assert len(emits) == 1, "the 4th session is still slow on the lane"
    wb.disarm(agents[3], now=410.0)
    assert emits[1:] == ["provider slow window over: lane claude-alr/claude-fable-5-1, "
                         "4 sessions, peak wait 400s, 502/529 ×2"]


def test_window_rate_limit_one_pair_per_lane_per_30_min(cfg, emits):
    def _episode(t0):
        agents = [_agent(f"e{t0}-{i}") for i in range(3)]
        for a in agents:
            wb.arm(a, now=t0)
        _poll(agents, t0 + 121.0)
        for a in agents:
            wb.disarm(a, now=t0 + 130.0)

    _episode(0.0)
    assert len(emits) == 2
    _episode(600.0)  # 10 min later: within the 30-min limit, no new pair
    assert len(emits) == 2
    _episode(1900.0)  # >30 min after the first open
    assert len(emits) == 4


def test_turn_reset_without_disarm_leaves_no_phantom_waiter(cfg, emits):
    a1, a2 = _agent("s1"), _agent("s2")
    wb.arm(a1, now=0.0)
    wb.arm(a2, now=0.0)
    wb.reset(a1)  # turn start bypasses disarm (agent/turn_context.py resets _wall_budget)
    text = wb.notice_due(a2, now=121.0)
    assert text and "provider slow" not in text


def test_unpolled_waiter_goes_stale_but_a_declared_sleep_keeps_it(cfg, emits):
    a1, a2 = _agent("s1"), _agent("s2")
    wb.arm(a1, now=0.0)
    wb.arm(a2, now=0.0)
    # a1 was last seen at 0 and declares nothing: at 121 it is a dead turn, not a waiter.
    assert "provider slow" not in wb.notice_due(a2, now=121.0)
    b1, b2 = _agent("s3"), _agent("s4")
    wb.arm(b1, now=1000.0)
    wb.arm(b2, now=1000.0)
    wb.note_quiet(b1, 200.0, now=1000.0)  # a retry backoff: nothing polls it
    assert wb.notice_due(b2, now=1121.0).startswith("⏳ provider slow: 2 sessions")


def test_registry_is_thread_safe_under_concurrent_arm_poll_disarm(cfg, emits):
    errors: list[BaseException] = []

    def _worker(i):
        try:
            for j in range(200):
                a = _agent(f"t{i}-{j}")
                wb.arm(a, now=0.0)
                wb.notice_due(a, now=121.0)
                wb.note_status(a, 529)
                wb.disarm(a, now=130.0)
        except BaseException as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert wb._REGISTRY == {}
    # A window opened under contention is closed by the last disarm.
    assert len(emits) % 2 == 0 and len(emits) <= 2


def test_kill_switch_restores_per_session_behaviour(emits):
    with patch("hermes_cli.config.load_config_readonly", return_value=CFG_OFF):
        agents = [_agent(f"s{i}") for i in range(4)]
        for a in agents:
            wb.arm(a, now=0.0)
        texts = _poll(agents, 121.0)
        later = _poll(agents, 241.0)
        for a in agents:
            wb.disarm(a, now=300.0)
    assert all(t and t.startswith("⏳ turn in progress") for t in texts + later), texts + later
    assert emits == []
    assert wb._REGISTRY == {}
