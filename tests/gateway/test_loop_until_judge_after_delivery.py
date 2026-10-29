"""/loop --until: the stop-condition judge (an aux-LLM call) runs AFTER the reply is sent (t_d81971a0).

The tick still settles before the send (marker, caps, next schedule), so a wakeup fired during the
send is never completed by this turn; only the judge waits, and its verdict applies to the tick it
judged. Real ``GatewayRunner._handle_message`` + real ``BasePlatformAdapter`` via the flush-window rig.
"""

import asyncio
import time

import pytest

from tests.gateway.test_turn_lease_flush_window import KEY, SESSION_ID, _msg, _Rig, _wait_for
from hermes_cli import goals, loops


@pytest.fixture
def loop_rig(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm from a sync context (cold bootstrap on the loop drops writes)
    rig = _Rig(monkeypatch, tmp_path)
    del rig.runner._run_post_turn_hooks  # the real hooks
    yield rig
    goals._DB_CACHE.clear()


def _arm_until_loop():
    mgr = loops.LoopManager(session_id=SESSION_ID)
    mgr.set("check the build", interval_seconds=60, until="the build is green")
    assert mgr.fire_tick()  # the wakeup this turn answers: awaiting_response
    return mgr


def _instrument(rig, monkeypatch, verdict, *, judge_s=0.5, during=None):
    log = []
    real_send = rig.adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        if "answer " in str(content):
            log.append(("send", time.monotonic()))
        return await real_send(chat_id, content, reply_to=reply_to, metadata=metadata)

    def judge(goal, reply, *a, **k):
        if goal != "the build is green":  # the /goal judge: no goal is set in these tests
            return ("continue", "", False, None, False)
        log.append(("judge", time.monotonic()))
        if during is not None:
            during()
        time.sleep(judge_s)
        log.append(("judged", time.monotonic()))
        return (verdict, "build is green", False, None, False)

    rig.adapter.send = send
    monkeypatch.setattr(goals, "judge_goal", judge)
    return log


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict, status", [("done", "done"), ("blocked", "paused"), ("continue", "active")])
async def test_until_judge_runs_after_the_send_and_its_verdict_applies(loop_rig, monkeypatch, verdict, status):
    await asyncio.to_thread(_arm_until_loop)
    log = _instrument(loop_rig, monkeypatch, verdict)
    started = time.time()
    await loop_rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: any(e[0] == "judged" for e in log), "judge", timeout=10)
    await loop_rig.settle(1)
    kinds = [e[0] for e in log]
    assert kinds[:2] == ["send", "judge"], f"reply waited on the --until judge: {kinds}"
    state = await asyncio.to_thread(loops.load_loop, SESSION_ID)
    assert (state.status, state.awaiting_response, state.ticks_fired) == (status, False, 1)
    if status == "active":  # continue: the next tick is scheduled from turn end
        assert state.next_due_at >= started + 59
    else:
        assert any("Loop" in s and "build is green" in s for s in loop_rig.adapter.sent), loop_rig.adapter.sent


@pytest.mark.asyncio
async def test_tick_fired_during_the_judge_is_not_stopped_by_its_verdict(loop_rig, monkeypatch):
    """The session slot is released before the post-delivery callback, so the wakeup scanner can fire
    a new tick while the judge runs: the late verdict belongs to the old tick and must not land."""
    await asyncio.to_thread(_arm_until_loop)

    def fire_next_tick():
        # The premise: _run_agent's finally freed the slot, so the scanner's busy check passes now.
        assert KEY not in loop_rig.runner._running_agents
        mgr = loops.LoopManager(session_id=SESSION_ID)
        mgr.state.next_due_at = 0
        assert mgr.fire_tick()

    log = _instrument(loop_rig, monkeypatch, "done", judge_s=0.1, during=fire_next_tick)
    await loop_rig.adapter.handle_message(_msg("first"))
    await _wait_for(lambda: any(e[0] == "judged" for e in log), "judge", timeout=10)
    await loop_rig.settle(1)
    state = await asyncio.to_thread(loops.load_loop, SESSION_ID)
    assert (state.status, state.awaiting_response, state.ticks_fired) == ("active", True, 2)


def test_complete_tick_inline_still_judges(monkeypatch, tmp_path):
    """CLI/TUI callers (no defer_judge) keep the judge inside complete_tick."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("done", "ok", False, None, False))
    mgr = _arm_until_loop()
    assert mgr.complete_tick("did it")["status"] == "done"
    goals._DB_CACHE.clear()
