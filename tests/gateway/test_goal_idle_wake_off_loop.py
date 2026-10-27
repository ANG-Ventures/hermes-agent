"""The goal idle-wake scan never reads state.db on the event-loop thread (t_6b37ae49).

Live: ``GoalManager()`` was built on the loop, its ``SessionDB.get_meta`` waited on the db lock while
another thread wrote, and the whole gateway loop stalled 60 s (Discord ack_stale, Matrix sync late).
"""
import asyncio
import threading
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
    yield runner, adapter, received
    goals._DB_CACHE.clear()


def _due_goal():
    mgr = goals.GoalManager("goal-sess")
    mgr.set("ship it")
    mgr.wait_for_seconds(1, reason="x")
    mgr.state.waiting_until = time.time() - 1
    mgr._save()


@pytest.mark.asyncio
async def test_idle_wake_scan_keeps_the_loop_responsive_while_another_thread_holds_the_db_lock(rig):
    runner, adapter, received = rig
    await asyncio.to_thread(_due_goal)
    db = await asyncio.to_thread(goals._get_session_db)
    held, release = threading.Event(), threading.Event()

    def _writer():  # another thread mid-write on the SessionDB lock
        with db._lock:
            held.set()
            release.wait(5)

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    assert held.wait(2)
    scan = asyncio.ensure_future(runner._goal_idle_wake_scan())
    ticks = 0
    try:
        for _ in range(10):  # 0.5 s with the lock held: a loop-thread get_meta gets zero ticks
            await asyncio.wait_for(asyncio.sleep(0.05), 1)
            ticks += 1
        assert not scan.done()  # the scan really is waiting on the held lock, off the loop
    finally:
        release.set()
        writer.join(2)
    await asyncio.wait_for(scan, 5)
    for task in list(getattr(adapter, "_session_tasks", {}).values()):
        await asyncio.wait_for(task, 2)
    assert ticks == 10
    assert len(received) == 1 and received[0].internal  # the wake still fires once the lock frees


@pytest.mark.asyncio
async def test_goal_load_does_not_queue_behind_a_writer_holding_the_db_lock(rig):
    """t_04b5d027: the pre-send goal pin / loop load read state_meta on SessionDB._lock and waited out
    every in-process writer's BEGIN IMMEDIATE retry while another process held the write lock (4.05 s
    before a Discord send). Under WAL the committed read goes through the reader pool instead."""
    await asyncio.to_thread(_due_goal)
    db = await asyncio.to_thread(goals._get_session_db)
    if not db._wal_active:
        pytest.skip("reader pool needs WAL on this filesystem")
    held, release = threading.Event(), threading.Event()

    def _writer():
        with db._lock:
            held.set()
            release.wait(5)

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    assert held.wait(2)
    try:
        state, raw = await asyncio.wait_for(asyncio.to_thread(goals._load_goal_raw, "goal-sess", strict=True), 1)
    finally:
        release.set()
        writer.join(2)
    assert state is not None and raw


@pytest.mark.asyncio
async def test_idle_wake_scan_never_calls_get_meta_on_the_loop_thread(rig, monkeypatch):
    runner, adapter, received = rig
    await asyncio.to_thread(_due_goal)
    db = await asyncio.to_thread(goals._get_session_db)
    loop_thread = threading.get_ident()
    threads = []
    real = type(db).get_meta_committed

    def _spy(self, key):
        threads.append(threading.get_ident())
        return real(self, key)

    monkeypatch.setattr(type(db), "get_meta_committed", _spy)
    await runner._goal_idle_wake_scan()
    for task in list(getattr(adapter, "_session_tasks", {}).values()):
        await asyncio.wait_for(task, 2)
    assert threads, "the scan should load the goal"
    assert loop_thread not in threads
    assert len(received) == 1


@pytest.mark.asyncio
async def test_durable_claim_settle_runs_off_the_loop_thread(monkeypatch):
    import tools.async_delegation as ad

    loop_thread = threading.get_ident()
    calls = []
    monkeypatch.setattr(ad, "release_completion_delivery", lambda d, c: calls.append((d, c, threading.get_ident())))
    runner = object.__new__(GatewayRunner)
    await runner._settle_durable_claim("release", "deleg-1", "claim-1")
    await runner._settle_durable_claims("release", [("deleg-2", "claim-2"), ("deleg-3", "claim-3")])
    assert [(d, c) for d, c, _ in calls] == [("deleg-1", "claim-1"), ("deleg-2", "claim-2"), ("deleg-3", "claim-3")]
    assert all(tid != loop_thread for _, _, tid in calls)
