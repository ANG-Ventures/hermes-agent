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
    r.judge_calls = []

    def slow_judge(*_a, **_kw):
        r.judge_calls.append(1)
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


def test_stale_evaluation_aborts_before_touching_the_replacement_goal(monkeypatch, tmp_path):
    """Prism b56ccc13f778 / 3ee618dd3a93: the first lost save ends the evaluation; the old goal's
    verdict (here: blocked -> pause) never lands on the goal the user set meanwhile."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
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
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_goal_clear_mid_judge_still_settles_the_loop_tick(goal_rig):
    """Prism 60b462e95f0c: the goal fence must not skip the /loop bookkeeping in the same task."""
    r, started, ended = goal_rig
    settled = []

    async def loop_completion(**_kw):
        settled.append(1)

    r.runner._post_turn_loop_completion = loop_completion
    await _first_turn_then(r, started, ended, lambda: _goal_verb(r, "clear"))
    assert settled == [1]


@pytest.mark.asyncio
async def test_stop_fences_every_queued_post_turn_task_of_the_session(goal_rig):
    """Prism 1fff0d50aed2: turn 2's hook task queued behind turn 1's judge never judges after /stop."""
    r, started, ended = goal_rig

    async def stop_after_second_turn():
        await r.adapter.handle_message(_msg("second"))
        await _wait_for(lambda: len(r.runner._post_turn_tasks) >= 2, "second post-turn task", timeout=5)
        r.runner._invalidate_session_run_generation(KEY, reason="stop_command_handler")

    await _first_turn_then(r, started, ended, stop_after_second_turn)
    await asyncio.sleep(0.5)
    assert len(r.judge_calls) == 1
    assert [c.split("\n\n")[0] for c in r.calls] == ["first", "second"]
    assert KEY not in r.adapter._pending_messages


@pytest.mark.asyncio
async def test_shutdown_cancels_overdue_judge_and_spools_the_owed_continuation(goal_rig, caplog):
    """Prism 7e832d73ac10 / 5e1b82dbf4fb: a judge outliving the drain bound is cancelled (its own late
    enqueue never happens) and the continuation it owed is queued BEFORE the adapter spool, once:
    with goals.checkin_minutes 0 the idle check-in would never pick the goal up after boot."""
    r, _started, _ended = goal_rig
    started = asyncio.Event()

    async def stuck(**kw):
        started.set()
        await asyncio.sleep(60)
        r.runner._enqueue_fifo(KEY, r.runner._synthetic_prompt_event(kw["source"], "late"), r.adapter)

    r.runner._run_post_turn_hooks = stuck
    await r.adapter.handle_message(_msg("first"))
    await asyncio.wait_for(started.wait(), 5)
    tasks = list(r.runner._post_turn_tasks)
    r.runner._draining = True
    await asyncio.wait_for(r.runner._drain_post_turn_tasks(0.2), 5)
    await asyncio.sleep(0.05)
    assert all(t.cancelled() for t in tasks)
    assert f"post-turn:{KEY}" in caplog.text
    queued = [r.adapter._pending_messages.get(KEY)] + list(r.runner._overflow_queue(KEY) or [])
    texts = [e.text for e in queued if e is not None]
    assert len(texts) == 1 and texts[0].startswith("[Continuing toward your standing goal]")
