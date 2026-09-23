"""Turn admission contracts exercised through the real gateway executor."""

import asyncio
import logging
import sys
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tests.gateway.test_streaming_tts_gateway_regression import (
    _NoopAgent, _make_runner, _setup_monkeypatches,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
async def test_pre_yield_failure_releases_acquired_permits(internal, monkeypatch):
    """Anything raised after acquire but before yield must release the permit.

    ``slot()`` is an async generator: if it raises before its first ``yield``,
    ``__aexit__`` never runs, so the pre-yield region is the ONLY place that
    can hand the permit back. The live case is a CancelledError — the old code
    awaited the notice task inside that region (a wide window: ``_wait_notice``
    sits behind ``ack()`` -> ``adapter.send`` after a 15 s wait), so a cancel
    there permanently burned one of ``cap`` slots. At zero, every turn blocks
    in acquire forever: the exact starvation this gate exists to prevent.
    Injected at the region's last statement so the fault is unambiguous.
    """
    import gateway.turn_admission as turn_admission
    from gateway.turn_admission import TurnAdmission

    admission = TurnAdmission(3)
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise asyncio.CancelledError()

    monkeypatch.setattr(turn_admission.logger, "info", boom)
    entered = False

    async def turn():
        nonlocal entered
        async with admission.slot("victim", internal=internal):
            entered = True

    with pytest.raises(asyncio.CancelledError):
        await turn()

    assert calls, "pre-yield region did not reach the injected fault"
    assert entered is False
    assert admission.total._value == 3
    assert admission.internal._value == 1
    assert admission.in_flight == 0
    assert admission.waiting == 0
    assert not admission._owners

    # The burned-slot symptom: a fresh turn must be admitted immediately.
    monkeypatch.undo()
    ran = asyncio.Event()

    async def next_turn():
        async with admission.slot("next", internal=internal):
            ran.set()

    await asyncio.wait_for(next_turn(), 5)
    assert ran.is_set()
    assert admission.total._value == 3
    assert admission.internal._value == 1


@pytest.mark.asyncio
async def test_notice_cleanup_never_awaits_inside_the_critical_section():
    """The notice task is cancelled and reaped detached, never awaited.

    Awaiting it after the permit was acquired is what made the cancellation
    window wide enough to hit. A notice whose own cleanup blocks forever must
    not delay — let alone deadlock — the admitted turn.
    """
    from gateway.turn_admission import TurnAdmission

    admission = TurnAdmission(2)
    admission.warning_after = 0
    notice_running = asyncio.Event()
    never = asyncio.Event()

    async def _wait_notice(key, internal, ack, started):
        notice_running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await never.wait()  # a cleanup that never completes

    admission._wait_notice = _wait_notice
    entered = asyncio.Event()

    async def turn():
        async with admission.slot("held"):
            entered.set()

    # Contend the semaphore so the notice task is genuinely RUNNING (and
    # therefore has a real cancellation to unwind) when cleanup reaps it.
    await admission.total.acquire()
    await admission.total.acquire()
    task = asyncio.create_task(turn())
    await asyncio.wait_for(notice_running.wait(), 5)
    admission.total.release()
    admission.total.release()

    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(task, 5)

    assert admission.total._value == 2
    assert admission.in_flight == 0


@pytest.mark.asyncio
async def test_admitted_resume_handle_cancel_delegates_to_its_task():
    """cancel() on an admitted handle must own the running resume task.

    A bare Future marks itself done/cancelled instantly while the admitted
    resume body keeps running. The shutdown path reads exactly that state to
    distinguish "never started -> cancel + re-mark the session" from "in
    progress -> leave it alone", so a bare Future let a RUNNING resume be
    re-marked and restored a second time on top of the original turn.
    """
    from gateway.turn_admission import StartupResumePool

    pool = StartupResumePool(1)
    started = asyncio.Event()
    cancelled_inside = False

    async def body():
        nonlocal cancelled_inside
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_inside = True
            raise

    handle = pool.submit(body)
    await asyncio.wait_for(started.wait(), 5)

    assert handle.cancel() is True
    # The task is cancelled, but the handle must NOT be done until the body
    # has actually unwound — that is the whole contract.
    assert not handle.done()

    with pytest.raises(asyncio.CancelledError):
        await handle
    assert cancelled_inside is True
    assert handle.cancelled()
    assert not pool.running


@pytest.mark.asyncio
async def test_queued_resume_handle_keeps_plain_cancel_contract():
    """Queued (unadmitted) entries still cancel immediately and never run."""
    from gateway.turn_admission import StartupResumePool

    pool = StartupResumePool(1)
    release = asyncio.Event()
    ran = []

    async def body(label):
        ran.append(label)
        await release.wait()

    admitted = pool.submit(body, "admitted")
    queued = pool.submit(body, "queued")
    await asyncio.sleep(0)
    assert ran == ["admitted"]

    assert queued.cancel() is True
    assert queued.cancelled()

    release.set()
    await asyncio.wait_for(admitted, 5)
    await asyncio.sleep(0)
    # A cancelled queued entry is dropped by the pump, never dispatched.
    assert ran == ["admitted"]
    assert not pool.pending
    assert not pool.running


@pytest.mark.asyncio
async def test_admitted_resume_handle_completes_with_task_result():
    from gateway.turn_admission import StartupResumePool

    pool = StartupResumePool(2)

    async def ok():
        return "done"

    async def boom():
        raise RuntimeError("resume failed")

    good = pool.submit(ok)
    bad = pool.submit(boom)
    assert await good == "done"
    with pytest.raises(RuntimeError, match="resume failed"):
        await bad
    assert not pool.running


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


def test_runtime_non_integer_turn_cap_is_unbounded_and_warns_once(caplog):
    runner = object.__new__(GatewayRunner)
    runner.__dict__["config"] = SimpleNamespace(max_concurrent_turns=object())

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        first = runner._get_turn_admission()
        second = runner._get_turn_admission()

    assert first is second
    assert first.cap is None
    assert first.total is None
    warnings = [
        record for record in caplog.records
        if "Invalid gateway.max_concurrent_turns" in record.message
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_direct_handler_without_generation_state_keeps_legacy_contract():
    runner = object.__new__(GatewayRunner)
    runner.__dict__["config"] = GatewayConfig()
    runner._handle_message_with_agent_admitted = AsyncMock(return_value="handled")
    event = SimpleNamespace(internal=False)

    result = await runner._handle_message_with_agent(
        event, make_restart_source("direct"), "direct-key", 1,
    )

    assert result == "handled"
    runner._handle_message_with_agent_admitted.assert_awaited_once_with(
        event, make_restart_source("direct"), "direct-key", 1,
    )


@pytest.mark.asyncio
async def test_queued_handler_drops_generation_invalidated_while_waiting():
    runner = object.__new__(GatewayRunner)
    runner.__dict__["config"] = GatewayConfig(max_concurrent_turns=1)
    runner._handle_message_with_agent_admitted = AsyncMock(return_value="handled")
    admission = runner._get_turn_admission()
    release = asyncio.Event()
    holder_entered = asyncio.Event()
    event = SimpleNamespace(internal=False)

    async def hold_slot():
        async with admission.slot("holder"):
            holder_entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_slot())
    await asyncio.wait_for(holder_entered.wait(), 5)
    generation = runner._begin_session_run_generation("queued-key")
    waiter = asyncio.create_task(runner._handle_message_with_agent(
        event, make_restart_source("queued"), "queued-key", generation,
    ))
    try:
        for _ in range(100):
            if admission.waiting == 1:
                break
            await asyncio.sleep(0)
        assert admission.waiting == 1
        runner._invalidate_session_run_generation("queued-key", reason="test")
    finally:
        release.set()
        await holder

    assert await waiter is None
    runner._handle_message_with_agent_admitted.assert_not_awaited()


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
async def test_runtime_non_integer_startup_resume_cap_defaults_to_three(
    monkeypatch, caplog,
):
    runner, _ = make_restart_runner()
    runner.config.__dict__["startup_resume_concurrency"] = object()
    runner._persist_active_agents = MagicMock()
    now = datetime.now()
    entries = [SessionEntry(
        session_key=f"invalid-key-{i}", session_id=f"invalid-sid-{i}", created_at=now,
        updated_at=now, origin=make_restart_source(str(i)),
        platform=Platform.TELEGRAM, chat_type="dm", resume_pending=True,
        resume_reason="restart_interrupted", last_resume_marked_at=now,
    ) for i in range(4)]
    runner.session_store._entries = {entry.session_key: entry for entry in entries}
    release = asyncio.Event()

    async def resume(*_args):
        await release.wait()

    monkeypatch.setattr(runner, "_run_startup_resume_event", resume)
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions() == 4

    tasks = list(runner._background_tasks)
    try:
        assert runner._startup_resume_pool.concurrency == 3
        warnings = [
            record for record in caplog.records
            if "Invalid gateway.startup_resume_concurrency" in record.message
        ]
        assert len(warnings) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)


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


def test_default_user_turn_reserve_preserves_legacy_internal_capacity():
    """Default reserve=2 must reproduce the old hard-coded ``cap - 2``."""
    from gateway.turn_admission import TurnAdmission

    for cap in (1, 2, 3, 8, 100):
        admission = TurnAdmission(cap)
        assert admission.reserve == min(2, max(0, cap - 1))
        assert admission.internal._value == max(1, cap - 2)


@pytest.mark.parametrize("cap,reserve,expected_internal,expected_reserve", [
    (100, 2, 98, 2),       # default at the new cap
    (100, 20, 80, 20),     # a real reserve
    (4, 0, 4, 0),          # 0 disables the reserve entirely
    (4, 9, 1, 3),          # clamped to cap - 1
    (4, -5, 4, 0),         # clamped to 0
])
def test_user_turn_reserve_is_clamped_into_range(
    cap, reserve, expected_internal, expected_reserve,
):
    from gateway.turn_admission import TurnAdmission

    admission = TurnAdmission(cap, reserve=reserve)
    assert admission.reserve == expected_reserve
    assert admission.internal._value == expected_internal


@pytest.mark.asyncio
async def test_internal_waiters_cannot_take_the_reserved_user_slots():
    """With reserve=N, internal turns may never occupy the last N slots."""
    from gateway.turn_admission import TurnAdmission

    cap, reserve = 4, 2
    admission = TurnAdmission(cap, reserve=reserve)
    release = asyncio.Event()
    internal_entered = []

    async def internal_turn(index):
        async with admission.slot(f"internal-{index}", internal=True):
            internal_entered.append(index)
            await release.wait()

    tasks = [asyncio.create_task(internal_turn(i)) for i in range(cap)]
    for _ in range(200):
        if len(internal_entered) >= cap - reserve and admission.waiting == reserve:
            break
        await asyncio.sleep(0)

    # Exactly cap - reserve internal turns admitted; the rest are still waiting.
    assert len(internal_entered) == cap - reserve
    assert admission.in_flight == cap - reserve

    # A user turn is admitted immediately despite saturated internal demand.
    user_entered = asyncio.Event()

    async def user_turn():
        async with admission.slot("user", internal=False):
            user_entered.set()

    await asyncio.wait_for(user_turn(), 5)
    assert user_entered.is_set()

    release.set()
    await asyncio.gather(*tasks)
    assert admission.in_flight == 0
    assert admission.total._value == cap
    assert admission.internal._value == cap - reserve


def test_turn_admission_logs_resolved_cap_and_reserve_once(caplog):
    import gateway.turn_admission as turn_admission

    with caplog.at_level(logging.INFO, logger=turn_admission.logger.name):
        turn_admission.TurnAdmission(100, reserve=20)

    lines = [
        record.getMessage() for record in caplog.records
        if "PHASE=turn_admission_init" in record.getMessage()
    ]
    assert lines == ["PHASE=turn_admission_init cap=100 reserve=20"]


@pytest.mark.parametrize("value,expected", [
    (5, 5), ("7", 7), (0, 0), (None, 2), ("bad", 2), (True, 2), (-1, 2),
])
@pytest.mark.parametrize("nested", [False, True])
def test_user_turn_reserve_config_roundtrip(
    tmp_path, monkeypatch, value, expected, nested,
):
    import yaml
    key = "user_turn_reserve"
    data = {} if value is None else (
        {"gateway": {key: value}} if nested else {key: value}
    )
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_gateway_config()
    assert config.user_turn_reserve == expected
    assert GatewayConfig.from_dict(config.to_dict()).user_turn_reserve == expected


def test_runtime_user_turn_reserve_reaches_admission():
    runner = object.__new__(GatewayRunner)
    runner.__dict__["config"] = GatewayConfig(
        max_concurrent_turns=100, user_turn_reserve=20,
    )

    admission = runner._get_turn_admission()

    assert admission.cap == 100
    assert admission.reserve == 20
    assert admission.internal._value == 80


def test_runtime_non_integer_user_turn_reserve_falls_back_and_warns(caplog):
    runner = object.__new__(GatewayRunner)
    runner.__dict__["config"] = SimpleNamespace(
        max_concurrent_turns=8, user_turn_reserve=object(),
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        admission = runner._get_turn_admission()

    assert admission.reserve == 2
    assert admission.internal._value == 6
    warnings = [
        record for record in caplog.records
        if "Invalid gateway.user_turn_reserve" in record.message
    ]
    assert len(warnings) == 1
