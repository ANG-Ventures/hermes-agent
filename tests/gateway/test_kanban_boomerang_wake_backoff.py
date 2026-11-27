"""t_827d5940: pending boomerang handbacks back off, dead-letter, and terminal claims are pruned.

Measured 2026-11-12: 4 undeliverable rows had 24,406 attempts between them (one per notifier tick,
each a full rewrite of an 18.7 MB state file), and 5,237 terminal claims were never pruned.
The board, the session store and the adapter are the real fixtures of the dispatch suite; the
clock is ``kanban_boomerang_wake._time`` (simulated, so an hour of ticks runs in seconds).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as ah
from gateway.platforms.base import SendResult
from tests.gateway.test_kanban_boomerang_wake_dispatch import CHAT, _card, env  # noqa: F401 (fixture)
from hermes_cli import kanban_db as kb

T0 = 1_794_500_000.0
TICK_S = 5.0   # notifier tick (the live interval is shorter; a shorter tick only widens the base gap)


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(env, monkeypatch):  # noqa: F811
    c = Clock()
    monkeypatch.setattr(ah, "_time", SimpleNamespace(time=c.time, monotonic=time.monotonic, sleep=time.sleep))
    monkeypatch.setattr(ah, "_LAST_PRUNE", {}, raising=False)
    monkeypatch.setattr(ah, "BACKOFF_BASE_S", 10.0, raising=False)   # the dispatch fixture zeroes it
    return c


class Sends:
    """Wraps the fixture adapter's send: counts every attempt; ``result`` decides each one."""

    def __init__(self, adapter, result) -> None:
        self.attempts = 0
        self._send = adapter.send
        self._result = result
        adapter.send = self

    async def __call__(self, chat_id, text, metadata=None):
        self.attempts += 1
        res = self._result(self.attempts)
        return res if res is not None else await self._send(chat_id, text, metadata=metadata)


def _row(tid: str, *, parts, created_at: float, home_sid: str = "", turn=None) -> dict:
    return {"task_id": tid, "board": None, "route": {"platform": "discord", "chat_id": CHAT, "thread_id": "",
                                                       "profile": None},
            "home_sid": home_sid, "title": "card", "kind": "completed", "event_ts": None, "event_id": None,
            "assignee": "worker", "pr": None, "claim_key": None, "parts": list(parts), "action": "complete",
            "line": f"boomerang handled {tid} (completed): verified", "turn": turn, "attempts": 0,
            "created_at": created_at}


def _ticks(env, clock, seconds: float) -> None:  # noqa: F811
    end = clock.now + seconds
    while clock.now < end:
        asyncio.run(ah.reconcile(env["runner"], now=clock.now))
        clock.now += TICK_S


def _comments(tid: str) -> list:
    with kb.connect_closing() as conn:
        return [c.body for c in kb.list_comments(conn, tid) if c.author == ah.AUTHOR]


def _count_writes(monkeypatch) -> list:
    writes: list = []
    real = ah.BoomerangWakeState._write

    def counting(self, data):
        writes.append(1)
        return real(self, data)

    monkeypatch.setattr(ah.BoomerangWakeState, "_write", counting)
    return writes


# (a) ------------------------------------------------------------------------------------------

def test_failing_row_is_attempted_a_bounded_number_of_times_per_hour(env, clock, monkeypatch):  # noqa: F811
    tid = _card(env)
    state = ah.BoomerangWakeState()
    state.put_handback("del-a", _row(tid, parts=[ah.PART_LINE], created_at=T0))
    sends = Sends(env["adapter"], lambda n: SendResult(success=False, error="injected 503"))
    writes = _count_writes(monkeypatch)
    _ticks(env, clock, 3600)
    # 10 s doubling to 15 min: t+0, 10, 30, 70, 150, 310, 630, 1270, 2170, 3070 -> 10 attempts.
    assert 5 <= sends.attempts <= 12, sends.attempts      # base: 720 (one per tick)
    assert len(writes) <= sends.attempts + 1, (len(writes), sends.attempts)   # no write while waiting
    row = state.handbacks()["del-a"]
    assert row["attempts"] == sends.attempts and row["parts"] == [ah.PART_LINE]
    assert state.dead_letters() == {}                     # transient and < 24 h: still pending


def test_no_live_home_turn_dead_letters_after_the_bound(env, clock):  # noqa: F811
    """A turn whose home session is not live here, 85 h old. (t_31c2626f was operator-homed; an
    operator home now resolves through its route chat, t_1637dd3e, so a gone session stands in.)"""
    tid = _card(env)
    logs: list = []
    env["monkeypatch"].setattr(ah, "post_logs", logs.append)
    state = ah.BoomerangWakeState()
    state.put_handback("del-home", _row(tid, parts=[ah.PART_TURN], created_at=T0 - 85 * 3600,
                                        home_sid="20261019_999999_gone", turn="wake text"))
    _ticks(env, clock, 30)
    assert state.handbacks() == {}
    dead = state.dead_letters()["del-home"]
    assert dead["parts"] == [ah.PART_TURN] and "no live home session" in dead["reasons"][ah.PART_TURN]
    assert len(logs) == 1 and "del-home" in logs[0]
    assert len([c for c in _comments(tid) if "dead-letter" in c]) == 1


# (b) ------------------------------------------------------------------------------------------

def test_permanent_send_error_dead_letters_after_one_attempt_with_one_comment(env, clock):  # noqa: F811
    """The t_e534ef4a shape: every send answers Discord 404 10003 (deleted channel)."""
    tid = _card(env)
    logs: list = []
    env["monkeypatch"].setattr(ah, "post_logs", logs.append)
    state = ah.BoomerangWakeState()
    state.put_handback("del-b", _row(tid, parts=[ah.PART_LINE], created_at=T0))
    sends = Sends(env["adapter"], lambda n: SendResult(
        success=False, error=f"Unknown Channel {CHAT} (deleted; cached 404 10003)", error_kind="not_found"))
    _ticks(env, clock, 3600)
    asyncio.run(ah.deliver_handbacks(env["runner"], state))   # an unpaced drain finds nothing either
    assert sends.attempts == 1
    assert state.handbacks() == {}
    dead = state.dead_letters()["del-b"]
    assert dead["parts"] == [ah.PART_LINE] and "10003" in dead["reasons"][ah.PART_LINE]
    assert len(logs) == 1 and "del-b" in logs[0]
    notes = [c for c in _comments(tid) if "dead-letter" in c]
    assert len(notes) == 1 and "(delegation del-b)" in notes[0]


def test_permanent_send_error_matches_the_discord_code_not_a_digit_substring():
    """Prism b36991a3b989: a channel id that contains ``10003`` is not a Discord 10003."""
    p = ah._permanent_send_error
    assert not p("HTTP 503 while posting to channel 110003123456789012", "server_error")
    assert not p("HTTP 503 while posting to channel 110003123456789012", None)
    assert not p("timeout after 100030 ms", None)
    assert p("Unknown Channel 1535189663533506600 (deleted; cached 404 10003)", None)
    assert p("Unknown Channel 1535189663533506600 (deleted; cached 404 10003)", "not_found")
    assert p("anything", "not_found")


def test_dead_letter_merges_reasons_across_parts(env, clock):  # noqa: F811
    """Prism 4f3cc0c30033: a line dead first and a turn dead later keep BOTH reasons."""
    state = ah.BoomerangWakeState()
    state.put_handback("del-m", {"parts": [ah.PART_LINE, ah.PART_TURN]})
    state.dead_letter("del-m", {"parts": [ah.PART_LINE], "reasons": {ah.PART_LINE: "404 10003"},
                                "dead_at": T0}, left=[ah.PART_TURN])
    state.dead_letter("del-m", {"parts": [ah.PART_TURN], "reasons": {ah.PART_TURN: "no live home session"},
                                "dead_at": T0 + 1}, left=[])
    dead = state.dead_letters()["del-m"]
    assert dead["parts"] == sorted([ah.PART_LINE, ah.PART_TURN])
    assert dead["reasons"] == {ah.PART_LINE: "404 10003", ah.PART_TURN: "no live home session"}
    assert state.handbacks() == {}


# (c) ------------------------------------------------------------------------------------------

def test_row_that_becomes_deliverable_is_delivered_exactly_once(env, clock):  # noqa: F811
    tid = _card(env)
    state = ah.BoomerangWakeState()
    state.put_handback("del-c", _row(tid, parts=[ah.PART_LINE], created_at=T0))
    sends = Sends(env["adapter"], lambda n: SendResult(success=False, error="injected 503") if n <= 3 else None)
    _ticks(env, clock, 3600)
    line = f"boomerang handled {tid} (completed): verified"
    assert env["adapter"].sent.count(line) == 1
    assert sends.attempts == 4
    assert state.handbacks() == {} and state.dead_letters() == {}
    with kb.connect_closing() as conn:
        marks = [e for e in kb.list_events(conn, tid) if e.kind == ah.DELIVERED_KIND]
    assert len(marks) == 1


# kill switch + claims -------------------------------------------------------------------------

def test_kill_switch_restores_retry_every_tick(env, clock):  # noqa: F811
    tid = _card(env)
    stop = ah.stop_file_path().parent / ah.BACKOFF_STOP_FILE
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("")
    state = ah.BoomerangWakeState()
    state.put_handback("del-k", _row(tid, parts=[ah.PART_LINE], created_at=T0 - 48 * 3600))
    sends = Sends(env["adapter"], lambda n: SendResult(success=False, error="x 10003", error_kind="not_found"))
    _ticks(env, clock, 60)
    assert sends.attempts == 12
    assert state.dead_letters() == {} and "del-k" in state.handbacks()
    assert ah.enabled()   # the boomerang-wake stop file is a separate switch


def test_terminal_claims_older_than_ttl_are_pruned(env, clock):  # noqa: F811
    state = ah.BoomerangWakeState()
    old = T0 - ah.CLAIM_TTL_S - 60
    state.claim("b|t1|1", {"task_id": "t1", "state": ah.STATE_TERMINAL, "claimed_at": old, "finished_at": old})
    state.claim("b|t2|2", {"task_id": "t2", "state": ah.STATE_TERMINAL, "claimed_at": old})
    state.claim("b|t3|3", {"task_id": "t3", "state": ah.STATE_TERMINAL, "claimed_at": old, "finished_at": T0 - 60})
    state.claim("b|t4|4", {"task_id": "t4", "state": ah.STATE_TERMINAL, "claimed_at": old, "finished_at": old})
    state.put_handback("del-p", dict(_row("t4", parts=[ah.PART_LINE], created_at=T0), claim_key="b|t4|4",
                                     attempts=1, last_attempt_at=T0))
    assert ah.BoomerangWakeState.prune_claims(state, T0, ah.CLAIM_TTL_S) == 2
    assert set(state.claims()) == {"b|t3|3", "b|t4|4"}
    # an open claim is never pruned, however old
    state.claim("b|t5|5", {"task_id": "t5", "state": ah.STATE_CLAIMED, "claimed_at": old})
    assert state.prune_claims(T0 + 10 * ah.CLAIM_TTL_S, ah.CLAIM_TTL_S) == 1   # t3 goes, t4 (pending) stays
    assert set(state.claims()) == {"b|t4|4", "b|t5|5"}
    # a terminal restart successor of an open claim stays (reconcile closes the original against it)
    state.claim("b|t5|5#restart1", {"task_id": "t5", "state": ah.STATE_TERMINAL, "claimed_at": old,
                                    "finished_at": old})
    assert state.prune_claims(T0, ah.CLAIM_TTL_S) == 0
    assert "b|t5|5#restart1" in state.claims()


# t_e861b07c -----------------------------------------------------------------------------------

def test_stuck_wake_turn_pages_alerts_once_and_keeps_retrying(env, clock):  # noqa: F811
    """A turn whose home is not live here (the aegis session the default gateway claimed) used to
    retry silently until the 24 h dead-letter. After NOTICE_AFTER_ATTEMPTS it pages #alerts once."""
    t1, t2 = _card(env), _card(env)
    alerts: list = []
    env["monkeypatch"].setattr(ah, "post_alerts", alerts.append)
    state = ah.BoomerangWakeState()
    state.put_handback("del-1", _row(t1, parts=[ah.PART_TURN], created_at=T0,
                                     home_sid="20261120_035316_gone", turn="wake text"))
    _ticks(env, clock, 3 * 3600)
    row = state.handbacks()["del-1"]
    assert row["attempts"] > ah.NOTICE_AFTER_ATTEMPTS and row.get("noticed_at")
    assert state.dead_letters() == {}                     # still retrying toward the 24 h bound
    assert len(alerts) == 1 and t1 in alerts[0] and "20261120_035316_gone" in alerts[0]
    # a second row crossing later is its own single line; the first is not repeated
    state.put_handback("del-2", _row(t2, parts=[ah.PART_TURN], created_at=clock.now,
                                     home_sid="20261120_035316_gone", turn="wake text"))
    _ticks(env, clock, 3 * 3600)
    assert len(alerts) == 2 and t2 in alerts[1] and t1 not in alerts[1]


def test_rows_already_past_the_bound_page_once_together(env, clock):  # noqa: F811
    """The live shape when this loads: rows at ~70 tries with no notice yet -> ONE line naming all."""
    tids = [_card(env) for _ in range(3)]
    alerts: list = []
    env["monkeypatch"].setattr(ah, "post_alerts", alerts.append)
    state = ah.BoomerangWakeState()
    for i, tid in enumerate(tids):
        r = _row(tid, parts=[ah.PART_TURN], created_at=T0 - 16 * 3600, home_sid="20261120_035316_gone",
                 turn="wake text")
        r.update(attempts=70 + i, last_attempt_at=T0 - 60 * i)
        state.put_handback(f"del-{i}", r)
    _ticks(env, clock, 1800)
    assert len(alerts) == 1 and all(t in alerts[0] for t in tids) and alerts[0].count("3 wake turn") == 1
    assert set(state.handbacks()) == {"del-0", "del-1", "del-2"}   # nothing dropped or rewritten away


def test_notice_stop_file_silences_the_page(env, clock):  # noqa: F811
    from hermes_constants import get_hermes_home

    tid = _card(env)
    alerts: list = []
    env["monkeypatch"].setattr(ah, "post_alerts", alerts.append)
    (get_hermes_home() / "state").mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / "state" / ah.NOTICE_STOP_FILE).touch()
    state = ah.BoomerangWakeState()
    r = _row(tid, parts=[ah.PART_TURN], created_at=T0, home_sid="20261120_035316_gone", turn="wake text")
    r.update(attempts=50, last_attempt_at=T0)
    state.put_handback("del-s", r)
    _ticks(env, clock, 1800)
    assert alerts == [] and not state.handbacks()["del-s"].get("noticed_at")
