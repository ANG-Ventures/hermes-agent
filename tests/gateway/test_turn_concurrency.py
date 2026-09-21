"""Turn admission contracts exercised through the real gateway executor."""

import asyncio
import logging
import sys
import threading
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import _AGENT_PENDING_SENTINEL
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tests.gateway.test_streaming_tts_gateway_regression import (
    _NoopAgent, _make_runner, _setup_monkeypatches,
)


@pytest.mark.parametrize("cap", [2, None])
@pytest.mark.asyncio
async def test_run_agent_bounds_real_executor(monkeypatch, tmp_path, cap):
    _setup_monkeypatches(monkeypatch, tmp_path)
    runner = _make_runner()
    runner.config.max_concurrent_turns = cap
    loop = asyncio.get_running_loop()
    entered = asyncio.Queue()
    release = threading.Event()
    lock = threading.Lock()
    active = peak = 0

    class Agent(_NoopAgent):
        def run_conversation(self, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            loop.call_soon_threadsafe(entered.put_nowait, True)
            try:
                assert release.wait(20), "test failed to release agent workers"
                return super().run_conversation(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(sys.modules["run_agent"], "AIAgent", Agent)
    tasks = [asyncio.create_task(runner._run_agent(
        "hello", "", [], make_restart_source(str(i)), f"sid-{i}",
        session_key=f"key-{i}",
    )) for i in range(5)]
    try:
        # Every caller must reach admission (waiting or executing) before the
        # executor barrier opens. No negative stopwatch assertion.
        for _ in range(cap or 5):
            await asyncio.wait_for(entered.get(), 20)
        limiter = getattr(runner, "_turn_admission", None)
        if cap:
            for _ in range(100):
                if limiter is None or limiter.waiting == 3:
                    break
                await asyncio.sleep(0)
            assert limiter is not None, "executor has no turn admission gate"
            assert limiter.waiting == 3
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 20)
        assert all(r["completed"] for r in results)
        assert peak == (cap or 5)
        assert active == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_user_turn_can_enter_while_internal_turns_are_saturated():
    from gateway.turn_admission import TurnAdmission

    admission = TurnAdmission(3)
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def run(label, *, internal):
        async with admission.slot(label, internal=internal):
            entered.put_nowait(label)
            await release.wait()

    internal_tasks = [
        asyncio.create_task(run(f"internal-{i}", internal=True))
        for i in range(3)
    ]
    user_task = None
    try:
        assert (await asyncio.wait_for(entered.get(), 5)).startswith("internal-")
        for _ in range(100):
            if admission.waiting == 2:
                break
            await asyncio.sleep(0)
        assert admission.waiting == 2

        user_task = asyncio.create_task(run("user", internal=False))
        assert await asyncio.wait_for(entered.get(), 5) == "user"
        assert admission.in_flight == 2
    finally:
        release.set()
        await asyncio.gather(
            *internal_tasks,
            *([user_task] if user_task is not None else []),
            return_exceptions=True,
        )
    assert admission.in_flight == 0
    assert admission.waiting == 0


@pytest.mark.asyncio
async def test_semaphore_wait_does_not_claim_transcript_lease(
    monkeypatch, tmp_path, caplog,
):
    from gateway.turn_lease import SessionTurnLeaseRegistry
    from tests.gateway.test_42039_duplicate_user_message import (
        _bootstrap, _event, _source,
    )

    runner = _bootstrap(monkeypatch, tmp_path)
    runner.config.max_concurrent_turns = 1
    runner._turn_leases = SessionTurnLeaseRegistry()
    admission = runner._get_turn_admission()
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_slot():
        async with admission.slot("holder", internal=True):
            holder_entered.set()
            await release_holder.wait()

    holder = asyncio.create_task(hold_slot())
    await asyncio.wait_for(holder_entered.wait(), 5)
    with caplog.at_level(logging.WARNING, logger="gateway.turn_lease"):
        waiter = asyncio.create_task(
            runner._handle_message_with_agent(
                _event(), _source(), "agent:main:telegram:group:-1001:12345", 1,
            )
        )
        try:
            for _ in range(100):
                if admission.waiting == 1 or waiter.done():
                    break
                await asyncio.sleep(0)
            assert admission.waiting == 1
            assert not runner._turn_leases._leases
            assert "PHASE=stale_lease_holder" not in caplog.text
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            release_holder.set()
            await holder
    assert admission.in_flight == 0
    assert admission.waiting == 0


@pytest.mark.asyncio
async def test_waiting_user_gets_one_interim_ack(caplog):
    runner, _ = make_restart_runner()
    runner.config.max_concurrent_turns = 1
    adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = make_restart_source("ack")
    admission = runner._get_turn_admission()
    admission.warning_after = 0
    admission.ack_after = 0
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_slot():
        async with admission.slot("holder", internal=True):
            holder_entered.set()
            await release_holder.wait()

    async def wait_for_slot():
        async with admission.slot(
            "user", ack=lambda: runner._ack_turn_slot_wait(source),
        ):
            return None

    with caplog.at_level(logging.INFO, logger="gateway.turn_admission"):
        holder = asyncio.create_task(hold_slot())
        await asyncio.wait_for(holder_entered.wait(), 5)
        waiter = asyncio.create_task(wait_for_slot())
        try:
            for _ in range(100):
                if adapter.send.await_count:
                    break
                await asyncio.sleep(0)
            adapter.send.assert_awaited_once()
            assert adapter.send.await_args.kwargs["metadata"]["_interim_send"] is True
        finally:
            release_holder.set()
            await asyncio.gather(holder, waiter, return_exceptions=True)
    wait_lines = [
        record.message for record in caplog.records
        if "PHASE=turn_slot_wait key=user" in record.message
    ]
    assert len(wait_lines) == 1
    assert any(
        "PHASE=turn_slot_acquire in_flight=1/1" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_slot_stays_reserved_until_retained_worker_finishes():
    from gateway.turn_admission import TurnAdmission

    admission = TurnAdmission(1)
    worker = asyncio.get_running_loop().create_future()
    async with admission.slot("first"):
        admission.retain_worker(worker)

    assert admission.in_flight == 1
    entered = asyncio.Event()

    async def next_turn():
        async with admission.slot("second"):
            entered.set()

    next_task = asyncio.create_task(next_turn())
    for _ in range(100):
        if admission.waiting == 1:
            break
        await asyncio.sleep(0)
    assert admission.waiting == 1

    worker.set_result(None)
    await asyncio.wait_for(entered.wait(), 5)
    await next_task
    assert admission.in_flight == 0
    assert admission.waiting == 0


@pytest.mark.asyncio
async def test_turn_admission_repeated_bursts_release_all_resources():
    from gateway.turn_admission import TurnAdmission

    admission = TurnAdmission(3)

    async def run(index):
        async with admission.slot(str(index), internal=index % 2 == 0):
            await asyncio.sleep(0)

    for burst in range(10):
        await asyncio.gather(*(run(i) for i in range(9)))
        await asyncio.sleep(0)
        assert admission.in_flight == 0
        assert admission.waiting == 0
        assert not admission._owners
        assert admission.total._value == 3
        assert admission.internal._value == 1


@pytest.mark.parametrize("entry_count", [4, 7])
@pytest.mark.asyncio
async def test_boot_resume_claims_all_slots_but_runs_three(
    monkeypatch, caplog, entry_count,
):
    runner, _ = make_restart_runner()
    runner.config.startup_resume_concurrency = 3
    runner._persist_active_agents = MagicMock()
    now = datetime.now()
    entries = [SessionEntry(
        session_key=f"key-{i}", session_id=f"sid-{i}", created_at=now,
        updated_at=now, origin=make_restart_source(str(i)),
        platform=Platform.TELEGRAM, chat_type="dm", resume_pending=True,
        resume_reason="restart_interrupted", last_resume_marked_at=now,
    ) for i in range(entry_count)]
    runner.session_store._entries = {e.session_key: e for e in entries}
    entered = []
    release = asyncio.Event()
    active = peak = 0

    async def resume(adapter, event, key, reason=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.append(key)
        try:
            await release.wait()
        finally:
            active -= 1

    monkeypatch.setattr(runner, "_run_startup_resume_event", resume)
    assert runner._schedule_resume_pending_sessions() == entry_count
    assert all(runner._running_agents[e.session_key] is _AGENT_PENDING_SENTINEL for e in entries)
    assert runner._persist_active_agents.call_count == entry_count
    assert entered == []
    tasks = list(runner._background_tasks)
    try:
        # All scheduled tasks get an event-loop turn; the resume bodies park
        # on the same event, making fan-out observable without wall clocks.
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(entered) == 3
        assert "PHASE=boot_resume_throttled" in caplog.text
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert entered == [e.session_key for e in entries]
    assert peak == 3
    assert active == 0
    assert not runner._startup_resume_pool.pending
    assert not runner._startup_resume_pool.running


@pytest.mark.asyncio
async def test_boot_resume_pool_does_not_extend_restore_gate_timeout(monkeypatch):
    import gateway.run as gateway_run

    runner, _ = make_restart_runner()
    runner.config.startup_resume_concurrency = 1
    runner._startup_restore_in_progress = True
    runner._persist_active_agents = MagicMock()
    runner._schedule_startup_restore_queue_drain = MagicMock()
    now = datetime.now()
    entries = [SessionEntry(
        session_key=f"key-{i}", session_id=f"sid-{i}", created_at=now,
        updated_at=now, origin=make_restart_source(str(i)),
        platform=Platform.TELEGRAM, chat_type="dm", resume_pending=True,
        resume_reason="restart_interrupted", last_resume_marked_at=now,
    ) for i in range(2)]
    runner.session_store._entries = {e.session_key: e for e in entries}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resume(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(runner, "_run_startup_resume_event", resume)
    monkeypatch.setattr(gateway_run, "_startup_restore_drain_timeout_secs", lambda: 0.01)
    assert runner._schedule_resume_pending_sessions() == 2
    tasks = list(runner._startup_restore_tasks)
    finish = asyncio.create_task(runner._finish_startup_restore())
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(finish, 5)

    assert runner._startup_restore_in_progress is False
    assert any(not task.done() for task in tasks)
    assert not any(task.cancelled() for task in tasks)
    runner._schedule_startup_restore_queue_drain.assert_called_once_with()

    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.parametrize("key,value,expected", [
    ("max_concurrent_turns", 2, 2), ("max_concurrent_turns", "4", 4),
    ("max_concurrent_turns", 0, None), ("max_concurrent_turns", "bad", None),
    ("max_concurrent_turns", True, None),
    ("startup_resume_concurrency", 5, 5),
    ("startup_resume_concurrency", "bad", 3),
    ("startup_resume_concurrency", 0, 3),
])
@pytest.mark.parametrize("nested", [False, True])
def test_config_loader_roundtrip(tmp_path, monkeypatch, key, value, expected, nested):
    import yaml
    data = {"gateway": {key: value}} if nested else {key: value}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_gateway_config()
    assert getattr(config, key) == expected
    assert getattr(GatewayConfig.from_dict(config.to_dict()), key) == expected


def test_config_unset_and_top_level_precedence():
    assert GatewayConfig.from_dict({}).max_concurrent_turns is None
    config = GatewayConfig.from_dict({
        "max_concurrent_turns": 0, "startup_resume_concurrency": 6,
        "gateway": {"max_concurrent_turns": 2, "startup_resume_concurrency": 2},
    })
    assert config.max_concurrent_turns is None
    assert config.startup_resume_concurrency == 6
