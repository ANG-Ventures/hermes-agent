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
    await asyncio.to_thread(_due)                         # off-loop: no SessionDB bootstrap window
    runner._running_agents[key] = object()
    await runner._goal_idle_wake_scan()
    assert received == []
    assert (await asyncio.to_thread(goals.load_goal, "goal-sess")).waiting_until > 0  # barrier untouched


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


@pytest.mark.asyncio
async def test_wake_abandoned_for_a_busy_session_does_not_consume_the_checkin(rig):
    """The session turned busy during the hop: no dispatch, and the recorded wake is given back so
    the last allowed check-in is still available once the session is idle again."""
    runner, adapter, received, key = rig

    def _quiet_due():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()

    await asyncio.to_thread(_quiet_due)
    real_hop = runner._run_in_executor_with_context

    async def hop_then_busy(fn, *args):
        runner._run_in_executor_with_context = real_hop
        result = await real_hop(fn, *args)
        runner._running_agents[key] = object()
        return result

    runner._run_in_executor_with_context = hop_then_busy
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 1)
    await _drain(adapter)
    assert received == []
    state = await asyncio.to_thread(goals.load_goal, "goal-sess")
    assert (state.idle_wakes, state.last_idle_wake_at) == (0, 0.0)
    runner._running_agents.pop(key)
    await runner._goal_idle_wake_one(entry, 30, 1)          # idle again: the sole check-in fires
    await _drain(adapter)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_idle_wake_undo_never_reactivates_a_goal_paused_from_another_surface(rig):
    """The give-back is one transaction against the stored row: a pause from a surface outside the
    gateway lock that landed after the wake was recorded is kept (no stale active state saved)."""
    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=1)
        after = mgr.idle_wake_snapshot()
        goals.GoalManager("goal-sess").pause()                # CLI / other surface
        undone = goals.undo_idle_wake("goal-sess", before, after)
        return undone, goals.load_goal("goal-sess")

    undone, state = await asyncio.to_thread(_scenario)
    assert undone is False
    assert state.status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["guard", "stop"])
@pytest.mark.parametrize("busy_after_hop", [1, 2])
async def test_checkin_is_never_handed_off_while_the_session_is_active(rig, busy_after_hop, claim):
    """Prism d8319143ee50 / 3f5d9f5e1df7: the final busy/stop check runs after the LAST executor
    await. A /goal command that claims the session's active guard during any pre-dispatch hop (here:
    after hop 1 or hop 2) must stop the wake; it must never be queued behind the command, where the
    pause/clear continuation sweep does not recognise a check-in. The abandoned wake is refunded."""
    runner, adapter, received, key = rig

    def _quiet_due():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()

    await asyncio.to_thread(_quiet_due)
    real_hop = runner._run_in_executor_with_context
    hops = []

    async def hop_then_command_claims_session(fn, *args):
        result = await real_hop(fn, *args)
        hops.append(fn)
        if len(hops) == busy_after_hop:
            if claim == "guard":
                adapter._active_sessions[key] = asyncio.Event()   # /goal pause took the guard
            else:
                runner._stop_latched = lambda k: k == key         # Prism 5bf6d1a6c65b: a late /stop
        return result

    handed_off = []
    real_handle = adapter.handle_message

    async def spy_handle(event):
        handed_off.append(key in adapter._active_sessions or runner._stop_latched(key))
        return await real_handle(event)

    adapter.handle_message = spy_handle
    runner._run_in_executor_with_context = hop_then_command_claims_session
    entry = runner.session_store.list_sessions()[0]
    await runner._goal_idle_wake_one(entry, 30, 1)
    assert True not in handed_off                            # never queued behind the command
    if not handed_off:
        state = await asyncio.to_thread(goals.load_goal, "goal-sess")
        assert (state.idle_wakes, state.last_idle_wake_at) == (0, 0.0)   # refunded


@pytest.mark.asyncio
async def test_idle_wake_undo_never_writes_into_a_replacement_goal(rig):
    """Prism 3897e56402cb: the give-back is bound to the exact row the wake saved. A replacement goal
    whose wait fields equal the post-wake values must not inherit the old goal's deadline/reason."""
    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("old goal")
        mgr.wait_for_seconds(1, reason="CI")
        mgr.state.waiting_until = time.time() - 1          # expired timer park
        mgr._save()
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=3)
        after = mgr.idle_wake_snapshot()
        assert after["waiting_until"] == 0.0 and before["waiting_until"] > 0
        goals.GoalManager("goal-sess").set("replacement")  # another surface replaces the goal
        undone = goals.undo_idle_wake("goal-sess", before, after)
        return undone, goals.load_goal("goal-sess")

    undone, state = await asyncio.to_thread(_scenario)
    assert undone is False
    assert state.goal == "replacement"
    assert (state.waiting_until, state.waiting_reason) == (0.0, None)


@pytest.mark.asyncio
async def test_idle_wake_undo_still_refunds_an_untouched_row(rig):
    """The identity guard does not disable the give-back: the row the wake saved is restored."""
    def _scenario():
        mgr = goals.GoalManager("goal-sess")
        mgr.set("g")
        mgr.state.created_at = mgr.state.last_turn_at = time.time() - 31 * 60
        mgr._save()
        before = mgr.idle_wake_snapshot()
        assert mgr.idle_wake_prompt(checkin_minutes=30, max_checkins=1)
        after = mgr.idle_wake_snapshot()
        return goals.undo_idle_wake("goal-sess", before, after), goals.load_goal("goal-sess")

    undone, state = await asyncio.to_thread(_scenario)
    assert undone is True
    assert (state.idle_wakes, state.last_idle_wake_at, state.status) == (0, 0.0, "active")
