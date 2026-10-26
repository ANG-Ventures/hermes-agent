"""The post-delivery /goal judge must not override the user's reaction to the reply (t_3acac80f, Prism
2a73e090fd20 / 7e832d73ac10).

The judge runs after the reply is sent, so its 2-12 s window is when the user types /goal pause,
/goal clear, /new or /stop. The real judge path runs here (real GatewayRunner, real goals SessionDB);
only the judge's LLM call is a sleep.
"""

import asyncio
import threading
import time

import pytest

from tests.gateway.test_goal_idle_wake_gateway import _drain, rig  # noqa: F401  (fixture)
from tests.gateway.test_turn_lease_flush_window import KEY, SESSION_ID, _msg, _Rig, _wait_for
from hermes_cli import goals


@pytest.fixture
def goal_rig(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    goals._get_session_db()
    r = _Rig(monkeypatch, tmp_path)
    del r.runner._run_post_turn_hooks  # the real hooks, not the rig's AsyncMock
    goals.GoalManager(SESSION_ID).set("ship the feature")
    started, ended = threading.Event(), threading.Event()

    def slow_judge(*_a, **_kw):
        started.set()
        time.sleep(2.0)
        ended.set()
        return "continue", "more to do", False, None, False

    monkeypatch.setattr(goals, "judge_goal", slow_judge)
    yield r, started, ended
    goals._DB_CACHE.clear()


async def _first_turn_then(r, started, ended, react):
    await r.adapter.handle_message(_msg("first"))
    await asyncio.to_thread(started.wait, 5)
    await asyncio.sleep(0.5)
    await react()
    await asyncio.to_thread(ended.wait, 5)
    await asyncio.sleep(0.5)  # the judge thread's save + any continuation it would queue


async def _goal_verb(r, verb):
    await r.runner._handle_goal_command(_msg(f"/goal {verb}"))


@pytest.mark.asyncio
@pytest.mark.parametrize("verb,status", [("pause", "paused"), ("clear", "cleared")])
async def test_goal_verb_during_judge_wins_and_no_continuation(goal_rig, verb, status):
    r, started, ended = goal_rig
    await _first_turn_then(r, started, ended, lambda: _goal_verb(r, verb))
    assert goals.load_goal(SESSION_ID).status == status, "stale judge save overwrote the user's verb"
    assert [c.split("\n\n")[0] for c in r.calls] == ["first"]
    assert KEY not in r.adapter._pending_messages


@pytest.mark.asyncio
async def test_new_during_judge_queues_nothing_for_the_new_session(goal_rig):
    r, started, ended = goal_rig

    async def new():
        r.runner._invalidate_session_run_generation(KEY, reason="session_reset")  # what /new and /stop do

    await _first_turn_then(r, started, ended, new)
    assert [c.split("\n\n")[0] for c in r.calls] == ["first"]
    assert KEY not in r.adapter._pending_messages


def test_stale_manager_save_is_dropped_and_never_continues(monkeypatch, tmp_path):
    """The backstop alone, every caller: a manager loaded before a pause cannot resurrect the goal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    goals.GoalManager("cas").set("finish")
    stale = goals.GoalManager("cas")
    goals.GoalManager("cas").pause()
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    decision = stale.evaluate_after_turn("did a thing", made_tool_calls=True)
    assert decision["should_continue"] is False
    assert goals.load_goal("cas").status == "paused"
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_shutdown_cancels_overdue_judge_and_the_checkin_fires_after_boot(rig, monkeypatch, tmp_path, caplog):  # noqa: F811
    """A judge outliving the drain bound is cancelled (nothing enqueued after the spool); the goal is
    still 'active' in SessionDB, so the next boot's idle check-in picks it up, once."""
    runner2, adapter2, received, _key = rig
    flush = _Rig(monkeypatch, tmp_path / "flush")
    started = asyncio.Event()

    async def stuck(**_kw):
        started.set()
        await asyncio.sleep(60)
        flush.runner._enqueue_fifo(KEY, flush.runner._synthetic_prompt_event(_kw["source"], "late"), flush.adapter)

    flush.runner._run_post_turn_hooks = stuck
    await flush.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(started.wait(), 5)
    tasks = list(flush.runner._post_turn_tasks)
    await asyncio.wait_for(flush.runner._drain_post_turn_tasks(0.2), 5)
    await asyncio.sleep(0.05)
    assert all(t.cancelled() for t in tasks)
    assert KEY not in flush.adapter._pending_messages
    assert f"post-turn:{KEY}" in caplog.text

    mgr = goals.GoalManager("goal-sess")
    mgr.set("ship the feature")
    mgr.state.created_at = mgr.state.last_turn_at = time.time() - 3600
    mgr._save()
    await runner2._goal_idle_wake_scan()   # simulated boot: a fresh runner over the same SessionDB
    await _drain(adapter2)
    await runner2._goal_idle_wake_scan()
    await _drain(adapter2)
    assert len(received) == 1
