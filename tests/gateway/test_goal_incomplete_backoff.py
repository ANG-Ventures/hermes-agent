"""Incomplete gateway turns must not spin the standing-goal loop."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.run_goals import GatewayGoalsMixin
from hermes_cli import goals


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["dict", "text", "streamed"])
async def test_incomplete_turns_park_backoff_reset_and_notify_once(tmp_path, monkeypatch, delivery):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayGoalsMixin()
    runner.config = {"goals": {"max_incomplete_turns": 3}}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="incomplete")))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._delivery_adapter_for = lambda source: None
    runner._session_key_for_source = lambda source: "test"
    event = SimpleNamespace(text="[Continuing toward your standing goal]\nGoal: ship it", internal=True)
    source = SimpleNamespace(chat_id="origin")
    mgr = await asyncio.to_thread(goals.GoalManager, "incomplete")
    await asyncio.to_thread(mgr.set, "ship it")
    clock = mgr.state.created_at

    async def turn(completed=False, text="This turn did not complete"):
        event._goal_turn_completed = completed
        event._streamed_final_response = text if delivery == "streamed" else ""
        result = {"completed": completed, "final_response": text} if delivery == "dict" else (
            text if delivery == "text" else None)
        await runner._run_post_turn_hooks(
            agent_result=result,
            source=source, is_internal=event.internal, event=event, parts=("goal continuation",))
        return await asyncio.to_thread(goals.GoalManager, "incomplete")

    with patch("hermes_cli.goals.time.time", side_effect=lambda: clock), patch(
        "hermes_cli.goals.judge_goal", return_value=("continue", "next", False, None, False)
    ) as judge:
        for _ in range(2):
            mgr = await turn()
            assert not mgr.state.waiting_until
        for delay in (60, 120, 240, 480, 960, 1800, 1800):
            mgr = await turn(text="")  # blank failures also settle the goal
            assert mgr.state.waiting_until == clock + delay
            assert mgr.idle_wake_prompt(clock + delay - 1) is None
            assert mgr.idle_wake_prompt(clock + delay)
            clock += delay
        assert judge.call_count == 0
        assert mgr.state.turns_used == 9  # every executed failure spends budget
        assert runner._defer_goal_status_notice_after_delivery.await_count == 1
        assert runner._defer_goal_status_notice_after_delivery.call_args.args[0] is source
        mgr = await turn(completed=True, text="made progress")
        assert mgr.state.consecutive_incomplete_turns == 0
        assert judge.call_count == 1
        runner._defer_goal_status_notice_after_delivery.reset_mock()  # exclude normal judge notice
        for _ in range(3):
            mgr = await turn()
        assert runner._defer_goal_status_notice_after_delivery.await_count == 1
        # A human message resets the prior failure streak even if its own turn fails.
        event.text, event.internal = "try again", False
        mgr = await turn()
        assert mgr.state.consecutive_incomplete_turns == 1
        assert not mgr.state.waiting_until
        assert not mgr.idle_wake_prompt(clock)  # no immediate recovery-loop wake
        mgr = await turn(completed=True, text="")
        assert mgr.state.consecutive_incomplete_turns == 0
        assert judge.call_count == 1  # blank success only resets, never judges


@pytest.mark.parametrize("claimed", [False, True])
def test_blank_success_preserves_retry_recovery(tmp_path, monkeypatch, claimed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("blank-retry")
    mgr.set("ship it")
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    deadline = mgr.state.waiting_until
    if claimed:
        clock = deadline
        assert mgr.idle_wake_prompt(checkin_minutes=0)
    with patch.object(goals, "judge_goal") as judge:
        decision = mgr.evaluate_after_turn("", turn_completed=True, max_incomplete_turns=1)
    judge.assert_not_called()
    assert not decision["should_continue"]
    mgr = goals.GoalManager("blank-retry")
    assert mgr.state.incomplete_retry_wait
    assert mgr.state.waiting_until == (deadline if not claimed else clock + 120)
    for _ in range(2):
        clock, prior = mgr.state.waiting_until, mgr.state.waiting_until - clock
        assert mgr.idle_wake_prompt(checkin_minutes=0)
        mgr.evaluate_after_turn("", turn_completed=True, max_incomplete_turns=1)
        mgr = goals.GoalManager("blank-retry")
        assert mgr.state.incomplete_retry_wait and mgr.state.waiting_until - clock > prior
    assert mgr.idle_wake_prompt(mgr.state.waiting_until - 1, checkin_minutes=0) is None
    assert mgr.idle_wake_prompt(mgr.state.waiting_until, checkin_minutes=0)


@pytest.mark.asyncio
async def test_delivery_preserves_completion_before_returning_text():
    from gateway.run_turn import GatewayTurnMixin

    event = SimpleNamespace()
    with patch("gateway.run_turn.diagnostic_wake_muted", return_value=True):
        for completed in (False, True):
            await GatewayTurnMixin()._hmwa_deliver_turn_response(
                event, None, None, "test", 1, {"completed": completed}, [], "", "", False)
            assert event._goal_turn_completed is completed


@pytest.mark.asyncio
async def test_incomplete_limit_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = await asyncio.to_thread(goals.GoalManager, "configured")
    await asyncio.to_thread(mgr.set, "ship it")
    decision = await asyncio.to_thread(
        mgr.evaluate_after_turn, "", turn_completed=False, max_incomplete_turns=1)
    assert not decision["should_continue"]
    assert mgr.state.waiting_until > 0
    assert decision["message"]


def test_failed_real_event_rearms_idle_checkins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("rearm")
    mgr.set("ship it")
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    mgr.state.idle_wakes = 3
    mgr._save()
    mgr.evaluate_after_turn("failed user turn", real_event=True, turn_completed=False)
    mgr = goals.GoalManager("rearm")
    assert mgr.state.idle_wakes == 0
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)):
        mgr.evaluate_after_turn("recovered", real_event=False)
    clock += 1801
    assert mgr.idle_wake_prompt(clock, checkin_minutes=30, max_checkins=3)


def test_loop_observation_does_not_consume_due_retry(tmp_path, monkeypatch):
    from hermes_cli.loops import goal_blocks_loop_tick

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("loop-retry")
    mgr.set("ship it")
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    clock = mgr.state.waiting_until
    assert goal_blocks_loop_tick("loop-retry")
    mgr = goals.GoalManager("loop-retry")
    assert mgr.idle_wake_prompt(clock, checkin_minutes=0)
    assert not mgr.idle_wake_prompt(clock + 1, checkin_minutes=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("global_minutes,goal_minutes", [(0, -1), (30, 0)])
async def test_retry_wakes_with_checkins_disabled(tmp_path, monkeypatch, global_minutes, goal_minutes):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayGoalsMixin()
    runner.config = {"goals": {"checkin_minutes": global_minutes}}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._warm_goals_session_db = AsyncMock()
    entry = SimpleNamespace(session_id="retry-off", origin=object(), suspended=False)
    runner.session_store = SimpleNamespace(list_sessions=lambda: [entry])
    prompts = []

    async def wake(entry, minutes, max_checkins):
        mgr = await asyncio.to_thread(goals.GoalManager, entry.session_id)
        prompts.append(await asyncio.to_thread(
            mgr.idle_wake_prompt, deadline, checkin_minutes=minutes, max_checkins=max_checkins))

    runner._goal_idle_wake_one = wake
    mgr = await asyncio.to_thread(goals.GoalManager, entry.session_id)
    await asyncio.to_thread(mgr.set, "ship it")
    await asyncio.to_thread(mgr.set_checkin_minutes, goal_minutes)
    await asyncio.to_thread(mgr.evaluate_after_turn, "", turn_completed=False, max_incomplete_turns=1)
    deadline = mgr.state.waiting_until
    assert mgr.idle_wake_prompt(deadline - 1, checkin_minutes=global_minutes) is None
    await runner._goal_idle_wake_scan()
    assert prompts and prompts[0]
    mgr = await asyncio.to_thread(goals.GoalManager, entry.session_id)
    assert mgr.idle_wake_prompt(deadline + 3600, checkin_minutes=global_minutes) is None
    await asyncio.to_thread(mgr.wait_for_seconds, 1, "deployment")
    assert mgr.idle_wake_prompt(mgr.state.waiting_until, checkin_minutes=global_minutes) is None


@pytest.mark.parametrize("reset", ["completed", "human"])
@pytest.mark.parametrize("barrier", ["timer", "session", "pid"])
def test_reset_preserves_unrelated_wait(tmp_path, monkeypatch, reset, barrier):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("wait-owner")
    mgr.set("ship it")
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    if barrier == "timer":
        mgr.wait_for_seconds(3600, "deployment")
    elif barrier == "session":
        monkeypatch.setattr(goals, "_session_waiting", lambda sid: True)
        mgr.wait_on_session("deployment", "deployment")
    else:
        monkeypatch.setattr(goals, "_pid_alive", lambda pid: True)
        mgr.wait_on(12345, "deployment")
    before = (mgr.state.waiting_until, mgr.state.waiting_on_pid, mgr.state.waiting_on_session,
              mgr.state.waiting_reason, mgr.state.waiting_since)
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)) as judge:
        decision = mgr.evaluate_after_turn(
            "made progress", turn_completed=reset == "completed", human_message=reset == "human")
    assert (mgr.state.waiting_until, mgr.state.waiting_on_pid, mgr.state.waiting_on_session,
            mgr.state.waiting_reason, mgr.state.waiting_since) == before
    assert not decision["should_continue"]
    judge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["dict", "text", "streamed"])
@pytest.mark.parametrize("failed,interrupted", [(False, False), (True, False), (False, True)])
async def test_iteration_handoff_is_counted_not_retried(tmp_path, monkeypatch, delivery, failed, interrupted):
    from gateway.run_turn import GatewayTurnMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayGoalsMixin()
    runner.config = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="handoff")))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._delivery_adapter_for = lambda source: None
    runner._session_key_for_source = lambda source: "test"
    event = SimpleNamespace(text="[Continuing toward your standing goal]\nGoal: ship it", internal=True)
    result = {"completed": False, "failed": failed, "interrupted": interrupted,
              "turn_exit_reason": "max_iterations_reached(1/1)", "final_response": "saved implementation"}
    with patch("gateway.run_turn.diagnostic_wake_muted", return_value=True):
        await GatewayTurnMixin()._hmwa_deliver_turn_response(
            event, None, None, "test", 1, result, [], "", "", False)
    event._streamed_final_response = result["final_response"] if delivery == "streamed" else ""
    mgr = await asyncio.to_thread(goals.GoalManager, "handoff")
    await asyncio.to_thread(mgr.set, "ship it", max_turns=1)
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)) as judge:
        await runner._run_post_turn_hooks(
            agent_result=result if delivery == "dict" else (result["final_response"] if delivery == "text" else None),
            source=SimpleNamespace(chat_id="origin"), is_internal=True, event=event, parts=("goal continuation",))
    mgr = await asyncio.to_thread(goals.GoalManager, "handoff")
    productive = not failed and not interrupted
    assert mgr.state.turns_used == 1  # a failed turn is not retried for free either
    assert judge.call_count == int(productive)
    assert mgr.state.status == "paused"
    assert mgr.state.consecutive_incomplete_turns == int(not productive)


def _run_sync_result(monkeypatch, raw):
    """The real TurnRunner.run_sync reconstruction of an agent result (not the raw dict)."""
    import gateway.run as gateway_run
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext

    monkeypatch.setattr(gateway_run, "_current_max_iterations", lambda: 1)
    runner = SimpleNamespace(
        _resolve_session_agent_runtime=lambda **_k: ("m", {"provider": "p"}),
        _pre_agent_fallback_notice=None, _provider_routing=None,
        _resolve_session_reasoning_config=lambda **_k: None,
        _resolve_session_service_tier=lambda **_k: None,
        _resolve_turn_agent_config=lambda *a: {},
        _announce_and_persist_served_route=lambda **_k: None,
        _get_system_prompt_for_channel=lambda *a, **k: "", _ephemeral_system_prompt="")
    ctx = TurnContext(source=SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="dm"),
                      session_key="slack:C1", user_config={}, message="continue")
    turn = TurnRunner(runner, ctx)
    agent = SimpleNamespace(model="m", provider="p")
    ctx.agent_holder[0] = agent
    stubs = {
        "_setup_stream_consumer": lambda *_a: (None, None, None, False),
        "_resolve_turn_agent": lambda *_a: (agent, True),
        "_wire_turn_agent_callbacks": lambda *_a, **_k: None,
        "_load_turn_history": lambda *_a: ([], None, []),
        "_prepare_turn_message": lambda *_a, **_k: (None, None),
        "_run_conversation_with_approval": lambda *_a: dict(raw),
        "_finish_stream_consumer": lambda *_a: None,
        "_sync_session_after_run": lambda *_a: (False, None, 0),
        "_announce_chat_pin_mismatch": lambda *_a: None,
        "_append_auto_media_tags": lambda text, *_a: text,
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(turn, name, fn)
    return turn.run_sync()


@pytest.mark.asyncio
async def test_run_sync_iteration_handoff_reaches_goal_judge(tmp_path, monkeypatch):
    """Production boundary: the handoff exit reason survives TurnRunner.run_sync into the goal hook."""
    from gateway.run_turn import GatewayTurnMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw = {"completed": False, "failed": False, "interrupted": False, "api_calls": 1,
           "turn_exit_reason": "max_iterations_reached(1/1)", "final_response": "saved implementation"}
    result = _run_sync_result(monkeypatch, raw)
    assert result["turn_exit_reason"] == raw["turn_exit_reason"]
    runner = GatewayGoalsMixin()
    runner.config = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="runsync")))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._delivery_adapter_for = lambda source: None
    runner._session_key_for_source = lambda source: "test"
    event = SimpleNamespace(text="[Continuing toward your standing goal]\nGoal: ship it", internal=True)
    with patch("gateway.run_turn.diagnostic_wake_muted", return_value=True):
        await GatewayTurnMixin()._hmwa_deliver_turn_response(
            event, None, None, "test", 1, result, [], "", "", False)
    mgr = await asyncio.to_thread(goals.GoalManager, "runsync")
    await asyncio.to_thread(mgr.set, "ship it", max_turns=1)
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)) as judge:
        await runner._run_post_turn_hooks(
            agent_result=result, source=SimpleNamespace(chat_id="origin"), is_internal=True,
            event=event, parts=("goal continuation",))
    mgr = await asyncio.to_thread(goals.GoalManager, "runsync")
    assert judge.call_count == 1
    assert (mgr.state.turns_used, mgr.state.status, mgr.state.consecutive_incomplete_turns) == (1, "paused", 0)


def test_blank_retry_successes_are_bounded(tmp_path, monkeypatch):
    """Blank/silent retry turns back off and charge the turn budget; they never loop every 60s."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("blank-bound")
    mgr.set("ship it", max_turns=4)
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    delays, wakes = [], 0
    with patch.object(goals, "judge_goal") as judge:
        while mgr.state.status == "active" and wakes < 20:
            clock = mgr.state.waiting_until
            assert mgr.idle_wake_prompt(checkin_minutes=0)
            wakes += 1
            mgr.evaluate_after_turn("", turn_completed=True, max_incomplete_turns=1)
            mgr = goals.GoalManager("blank-bound")
            if mgr.state.status == "active":
                delays.append(mgr.state.waiting_until - clock)
    judge.assert_not_called()
    assert mgr.state.status == "paused" and mgr.state.turns_used == 4
    assert wakes == 3  # the initial executed failure already spent one turn
    assert delays == sorted(delays) and delays[-1] > delays[0]
    assert mgr.idle_wake_prompt(clock + 7200, checkin_minutes=0) is None


@pytest.mark.asyncio
async def test_executed_failures_consume_turn_budget(tmp_path, monkeypatch):
    """Executed failed retries charge max_turns; an unexecuted (route-refused) wake never does."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayGoalsMixin()
    runner.config = {"goals": {"max_incomplete_turns": 1}}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="fail-budget")))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._delivery_adapter_for = lambda source: None
    runner._session_key_for_source = lambda source: "test"
    mgr = await asyncio.to_thread(goals.GoalManager, "fail-budget")
    await asyncio.to_thread(mgr.set, "ship it", max_turns=3)
    clock = mgr.state.created_at

    async def wake_turn(executed):
        event = SimpleNamespace(text="[Continuing toward your standing goal]\nGoal: ship it", internal=True,
                                _goal_idle_wake=True, _goal_execution_started=executed,
                                _goal_turn_completed=False)
        await runner._run_post_turn_hooks(
            agent_result={"completed": False, "final_response": "This turn did not complete"},
            source=SimpleNamespace(chat_id="origin"), is_internal=True, event=event, parts=("goal continuation",))
        return await asyncio.to_thread(goals.GoalManager, "fail-budget")

    wakes = 0
    with patch("hermes_cli.goals.time.time", side_effect=lambda: clock), patch.object(goals, "judge_goal") as judge:
        mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
        mgr = await asyncio.to_thread(goals.GoalManager, "fail-budget")
        assert mgr.state.turns_used == 1
        while mgr.state.status == "active" and wakes < 20:
            clock = mgr.state.waiting_until
            assert mgr.idle_wake_prompt(checkin_minutes=0)
            mgr = await wake_turn(executed=False)  # route refusal: settled by refund, not the judge
            assert mgr.state.turns_used == 1 + wakes
            mgr = await wake_turn(executed=True)
            wakes += 1
    judge.assert_not_called()
    assert (mgr.state.status, mgr.state.turns_used, wakes) == ("paused", 3, 2)
    assert mgr.idle_wake_prompt(clock + 7200, checkin_minutes=0) is None


_CHILD = r"""
import sys, time
from hermes_cli import goals
mgr = goals.GoalManager("orphan")
if sys.argv[1] == "fail":
    mgr.set("ship it")
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    print(mgr.state.waiting_until, flush=True)
else:
    print(bool(mgr.idle_wake_prompt(float(sys.argv[2]), checkin_minutes=0)), flush=True)
    if sys.argv[1] == "claim-and-hold":
        sys.stdin.read()  # alive until the parent closes stdin: dispatch still owned
"""


def _child(tmp_path, *args, hold=False):
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {**os.environ, "HERMES_HOME": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    proc = subprocess.Popen([sys.executable, "-c", _CHILD, *args], env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    line = proc.stdout.readline().strip()
    if not hold:
        proc.communicate("", timeout=60)
    return line, proc


def test_retry_claim_survives_claimant_death(tmp_path, monkeypatch):
    """A retry claimed by a process that died before dispatch/settlement is re-offered with
    check-ins off; a live claimant fences it, and only one of two racing reclaimers wins."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    deadline = float(_child(tmp_path, "fail")[0])
    out, holder = _child(tmp_path, "claim-and-hold", str(deadline), hold=True)
    assert out == "True"
    try:
        mgr = goals.GoalManager("orphan")
        assert not mgr.state.incomplete_retry_wait and not mgr.state.waiting_until
        assert mgr.idle_wake_prompt(deadline + 1, checkin_minutes=0) is None  # live owner: fenced
    finally:
        holder.communicate("", timeout=60)  # the claimant exits without dispatching
    racer = goals.GoalManager("orphan")
    mgr = goals.GoalManager("orphan")
    before = mgr.state.turns_used
    assert mgr.idle_wake_prompt(deadline + 2, checkin_minutes=0) is None
    assert racer.idle_wake_prompt(deadline + 2, checkin_minutes=0) is None  # lost the reclaim
    recovered = goals.GoalManager("orphan")
    assert recovered.state.turns_used == before + 1
    assert recovered.state.idle_wake_attempt is None
    assert recovered.state.incomplete_retry_wait
    retry_at = recovered.state.waiting_until
    assert retry_at > deadline + 2
    assert recovered.idle_wake_prompt(retry_at - 1, checkin_minutes=0) is None
    timer_racer = goals.GoalManager("orphan")
    assert recovered.idle_wake_prompt(retry_at, checkin_minutes=0)
    assert timer_racer.idle_wake_prompt(retry_at, checkin_minutes=0) is None
    owner = goals.GoalManager("orphan").state.idle_wake_attempt
    assert owner == recovered.state.idle_wake_attempt and owner.startswith("retry:")
