"""Post-turn hooks (the /goal judge) run AFTER the reply is sent, as the adapter's post-delivery
callback inside the session task (t_3acac80f). The session stays busy while the judge runs.

Real ``GatewayRunner._handle_message`` + real ``BasePlatformAdapter`` via the flush-window rig.
"""

import asyncio
import time

import pytest

import gateway.platforms.base as base
from gateway.turn_lease import TurnLeaseTimeoutError
from tests.gateway.test_turn_lease_flush_window import KEY, _msg, _Rig, _wait_for
from hermes_cli import goals


def _timed_sends(rig):
    stamps = []
    real_send = rig.adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        if "answer " in str(content):  # the turn's reply, not a notice
            stamps.append(time.monotonic())
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    rig.adapter.send = send
    return stamps


def _slow_hooks(rig, seconds, log):
    async def hooks(*, event=None, **_kw):
        log.append(("start", event.text, time.monotonic()))
        await asyncio.sleep(seconds)
        log.append(("end", event.text, time.monotonic()))

    rig.runner._run_post_turn_hooks = hooks


@pytest.mark.asyncio
async def test_reply_is_sent_before_the_goal_judge_starts(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    sends, log = _timed_sends(rig), []
    _slow_hooks(rig, 3.0, log)  # a judge round trip under load
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: len(log) == 2 and sends, "judge and reply", timeout=10)
    assert sends[0] < log[0][2], "reply waited on the /goal judge"


@pytest.mark.asyncio
async def test_message_arriving_mid_judge_is_queued_and_runs_after_it(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    log = []
    _slow_hooks(rig, 0.5, log)
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: log, "judge start")
    assert KEY in rig.adapter._active_sessions, "session guard released before the judge ran"
    await rig.adapter.handle_message(_msg("second"))
    assert [c.split("\n\n")[0] for c in rig.calls] == ["first"], "second turn started during the judge"
    await _wait_for(lambda: len(rig.calls) >= 2, "second turn", timeout=10)
    assert [entry[:2] for entry in log[:2]] == [("start", "first"), ("end", "first")]


@pytest.mark.asyncio
async def test_stop_mid_judge_cancels_it(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    log = []
    _slow_hooks(rig, 5.0, log)
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: log, "judge start")
    await rig.adapter.cancel_session_processing(KEY)  # what /stop does to the session task
    await asyncio.sleep(0.1)
    assert [e[0] for e in log] == ["start"], "judge kept running after /stop"


@pytest.mark.asyncio
async def test_loop_tick_settles_inside_the_callback(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    sends, order = _timed_sends(rig), []
    del rig.runner._run_post_turn_hooks  # the real hooks, both of them

    async def judge(**_kw):
        order.append("judge")
        await asyncio.sleep(0.3)

    async def loop_completion(**_kw):
        order.append("loop" if sends else "loop-before-send")

    rig.runner._post_turn_goal_continuation = judge
    rig.runner._post_turn_loop_completion = loop_completion
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: "loop" in order or "loop-before-send" in order, "loop completion")
    assert order == ["judge", "loop"]


@pytest.mark.asyncio
async def test_hooks_outlive_the_default_callback_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(base, "_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS", 0.1)
    rig = _Rig(monkeypatch, tmp_path)
    log = []
    _slow_hooks(rig, 0.4, log)
    await rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: len(log) == 2, "judge end", timeout=5)


@pytest.mark.asyncio
async def test_lease_rejection_returns_before_any_hook(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path)
    calls = []

    async def hooks(**_kw):
        calls.append(1)

    async def reject(*_a, **_kw):
        raise TurnLeaseTimeoutError("sid", owner_key=KEY, generation=1, wait_seconds=0.1)

    rig.runner._run_post_turn_hooks = hooks
    rig.runner._handle_message_with_agent = reject
    rig.runner._hm_lease_rejection_reply = lambda *_a: "resend"
    assert await rig.runner._handle_message(_msg("first")) == "resend"
    await asyncio.sleep(0.05)
    assert calls == []
    assert not rig.adapter._post_delivery_callbacks


@pytest.fixture
def goal_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def test_stale_evaluator_save_is_dropped_and_never_continues(goal_db, monkeypatch):
    """Backstop: an evaluator loaded before a pause cannot resurrect the goal."""
    goals.GoalManager("cas").set("finish")
    stale = goals.GoalManager("cas")
    goals.GoalManager("cas").pause()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    decision = stale.evaluate_after_turn("did a thing", made_tool_calls=True)
    assert decision["should_continue"] is False
    assert goals.load_goal("cas").status == "paused"


def test_stale_evaluation_aborts_before_touching_the_replacement_goal(goal_db, monkeypatch):
    """The first lost save ends the evaluation; the old goal's verdict never lands on the new goal."""
    old = goals.GoalManager("abort")
    old.set("old")
    old.state.idle_wakes = 1
    old._save()
    stale = goals.GoalManager("abort")
    goals.GoalManager("abort").set("new")
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("blocked", "impossible", False, None, False))
    decision = stale.evaluate_after_turn("old reply", real_event=True)
    stored = goals.load_goal("abort")
    assert decision["should_continue"] is False
    assert (stored.goal, stored.status, stored.last_verdict) == ("new", "active", None)


def test_user_verb_after_a_concurrent_save_is_not_lost(goal_db):
    """Prism 14f3a188fdff: only the evaluator may drop a write; an acknowledged gate persists."""
    goals.GoalManager("s").set("ship")
    a, b = goals.GoalManager("s"), goals.GoalManager("s")
    b.set_checkin_minutes(5)
    a.add_gate("false")
    assert [g.command for g in goals.load_goal("s").gates] == ["false"]
