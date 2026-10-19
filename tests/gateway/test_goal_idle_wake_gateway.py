"""Gateway idle-wake poller: a timer-parked /goal in an idle session gets a turn started for it."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import goals


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="wire-1")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    source = SessionSource(platform=Platform.DISCORD, chat_id="77", user_id="1", chat_type="channel")
    key = build_session_key(source)
    adapter = _Adapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.DISCORD)
    received = []

    async def handler(event):
        received.append(event)
        return None

    adapter.set_message_handler(handler)
    entry = SimpleNamespace(session_key=key, session_id="goal-sess", origin=source, suspended=False)
    runner = object.__new__(GatewayRunner)
    runner.config = {"goals": {"checkin_minutes": 30, "max_idle_checkins": 3}}
    runner._queued_events = {}
    runner.session_store = SimpleNamespace(list_sessions=lambda: [entry])
    runner._restored_source = lambda e: e.origin
    runner._delivery_adapter_for = lambda s: adapter
    runner._run_in_executor_with_context = asyncio.to_thread

    async def _warm(_label):
        return None

    runner._warm_goals_session_db = _warm
    yield runner, adapter, received, key
    goals._DB_CACHE.clear()


async def _drain(adapter):
    for task in list(getattr(adapter, "_session_tasks", {}).values()):
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_timer_parked_goal_is_woken_when_the_deadline_passes(rig):
    runner, adapter, received, key = rig
    mgr = goals.GoalManager("goal-sess")
    mgr.set("ship the feature")
    mgr.wait_for_seconds(600, reason="CI")
    await runner._goal_idle_wake_scan()
    assert received == []                                   # still parked: nothing fired
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    await runner._goal_idle_wake_scan()
    await _drain(adapter)
    assert len(received) == 1
    assert received[0].internal
    assert received[0].text.startswith("[Continuing toward your standing goal]")
    assert goals.load_goal("goal-sess").waiting_until == 0.0


@pytest.mark.asyncio
async def test_busy_session_is_never_woken(rig):
    runner, adapter, received, key = rig
    mgr = goals.GoalManager("goal-sess")
    mgr.set("g")
    mgr.wait_for_seconds(1, reason="x")
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    runner._running_agents[key] = object()
    await runner._goal_idle_wake_scan()
    assert received == []
    assert goals.load_goal("goal-sess").waiting_until > 0     # barrier untouched for the live turn


@pytest.mark.asyncio
async def test_checkins_off_in_config_disables_the_poller(rig):
    runner, adapter, received, key = rig
    runner.config = {"goals": {"checkin_minutes": 0}}
    mgr = goals.GoalManager("goal-sess")
    mgr.set("g")
    mgr.wait_for_seconds(1, reason="x")
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    await runner._goal_idle_wake_scan()
    assert received == []


def _due(sid="goal-sess"):
    mgr = goals.GoalManager(sid)
    mgr.set("g")
    mgr.wait_for_seconds(1, reason="x")
    mgr.state.waiting_until = time.time() - 1
    mgr._save()
    return mgr


@pytest.mark.asyncio
async def test_user_stop_latch_blocks_idle_wakes(rig):
    """Ace's /stop (or "stop the goal") must hold: a latched session is never woken by the poller."""
    runner, adapter, received, key = rig
    _due()
    runner._stop_latched = lambda k: k == key                # in-memory /stop latch
    await runner._goal_idle_wake_scan()
    assert received == []
    runner._stop_latched = lambda k: False
    entry = runner.session_store.list_sessions()[0]
    entry.user_stopped_at = time.time()                      # durable marker after a restart
    await runner._goal_idle_wake_scan()
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["clear", "pause", "done"])
async def test_ended_goals_are_never_woken(rig, end):
    """Goal reached, /goal pause, or /goal clear: the poller has nothing to resume."""
    runner, adapter, received, key = rig
    mgr = _due()
    if end == "done":
        mgr.state.status = "done"
        mgr._save()
    else:
        getattr(mgr, end)()
    await runner._goal_idle_wake_scan()
    await _drain(adapter)
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["clear", "pause"])
async def test_goal_command_racing_the_idle_wake_is_not_overwritten(rig, end):
    """A /goal pause|clear that lands while the idle wake is hopping to the executor wins: the wake
    must not save its stale active snapshot over it, and must not start a turn."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context

    async def hop_after_user_command(fn, *args):
        getattr(goals.GoalManager("goal-sess"), end)()     # the user's /goal command commits first
        runner._run_in_executor_with_context = real_hop
        return await real_hop(fn, *args)

    runner._run_in_executor_with_context = hop_after_user_command
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 3)
    await _drain(adapter)
    assert received == []
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).status == ("cleared" if end == "clear" else "paused")


@pytest.mark.asyncio
async def test_goal_paused_after_the_wake_is_recorded_does_not_dispatch(rig):
    """Eligibility is re-checked after the executor hop: a pause committed after the wake was
    recorded (but before dispatch) still prevents the automatic turn."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context

    async def hop_then_user_pause(fn, *args):
        result = await real_hop(fn, *args)
        await asyncio.to_thread(lambda: goals.GoalManager("goal-sess").pause())
        return result

    runner._run_in_executor_with_context = hop_then_user_pause
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 3)
    await _drain(adapter)
    assert received == []
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).status == "paused"


@pytest.mark.asyncio
async def test_user_message_arriving_during_the_hop_is_not_raced(rig):
    """A real message that made the session busy during the executor hop owns the next turn."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context

    async def hop_then_busy(fn, *args):
        result = await real_hop(fn, *args)
        runner._running_agents[key] = object()
        return result

    runner._run_in_executor_with_context = hop_then_busy
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 3)
    await _drain(adapter)
    assert received == []


@pytest.mark.asyncio
async def test_goal_command_waits_for_an_in_flight_idle_wake_and_reads_its_save(rig):
    """/goal pause issued while the idle wake holds the session's goal lock runs after it, on the
    wake's saved state (no lost update in either direction)."""
    runner, adapter, received, key = rig
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    real_hop = runner._run_in_executor_with_context
    in_hop, release = asyncio.Event(), asyncio.Event()

    async def slow_hop(fn, *args):
        in_hop.set()
        await release.wait()
        runner._run_in_executor_with_context = real_hop
        return await real_hop(fn, *args)

    async def _manager(event):
        return await asyncio.to_thread(goals.GoalManager, "goal-sess"), SimpleNamespace(session_id="goal-sess")

    runner._run_in_executor_with_context = slow_hop
    runner._get_goal_manager_for_event = _manager
    runner._clear_goal_continuations = lambda *a: None
    entry = runner.session_store.list_sessions()[0]
    wake = asyncio.create_task(runner._goal_idle_wake_one(entry, 30, 3))
    await in_hop.wait()
    cmd_event = SimpleNamespace(source=entry.origin, get_command_args=lambda: "pause")
    command = asyncio.create_task(runner._handle_goal_command(cmd_event))
    await asyncio.sleep(0.05)
    assert not command.done()                                # serialized behind the wake
    release.set()
    await asyncio.wait_for(asyncio.gather(wake, command), 10)
    state = await asyncio.to_thread(goals.load_goal, "goal-sess")
    assert state.status == "paused"
    assert state.last_idle_wake_at > 0                       # the wake's save was not clobbered
