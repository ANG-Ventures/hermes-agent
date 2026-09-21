"""A saturated pool must not delay a boot-resume turn body (2026-09-20).

Incident: boot resumes were SCHEDULED at 10:02:45 (runner slot claimed with
``_AGENT_PENDING_SENTINEL``, internal event dispatched ~0.5s later) but their
turn BODIES did not start until 10:12:36 — ~590s — because ``run_sync``
executes off-loop on the runner's shared ``ThreadPoolExecutor`` and that pool
was saturated.

The load-bearing measurement behind these tests: the pool was NOT saturated by
turns. Only 4-5 sessions were live against 10 slots. It was saturated by
best-effort HOUSEKEEPING that nobody was waiting on any more —
``_finalize_session_off_loop`` and ``_cleanup_agent_resources_off_loop`` wrap
their submission in ``asyncio.wait_for`` and, on timeout, log "the worker
thread is left to finish on its own" and proceed.

``asyncio.wait_for`` bounds the AWAIT but not the OCCUPANCY: a
``concurrent.futures`` work item that has already begun executing is not
cancellable, so an abandoned worker holds its slot until its blocking call
returns. N abandonments retire N slots for ANY N, which is why raising
``max_workers`` does not fix this and why housekeeping needs its own pool.

These tests drive the REAL runner methods bound onto a minimal object holding
only the attributes they read, so they cannot pass by mirroring the
implementation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
import types

import pytest

from gateway.run import GatewayRunner


def _runner(cleanup=None, *, cleanup_timeout=1.0):
    """Minimal object exposing exactly the attributes the pool helpers read."""
    obj = types.SimpleNamespace()
    obj._executor_lock = threading.Lock()
    obj._executor = None
    obj._housekeeping_executor = None
    obj._executor_closing = False
    obj._CLEANUP_TIMEOUT_S = cleanup_timeout
    obj._FINALIZE_TIMEOUT_S = cleanup_timeout
    if cleanup is not None:
        obj._cleanup_agent_resources = cleanup
    for name in (
        "_get_pool",
        "_get_executor",
        "_get_housekeeping_executor",
        "_submit_with_context",
        "_run_in_executor_with_context",
        "_run_housekeeping_in_executor",
        "_cleanup_agent_resources_off_loop",
        "_shutdown_executor",
    ):
        setattr(obj, name, types.MethodType(getattr(GatewayRunner, name), obj))
    return obj


def _shutdown(runner):
    for attr in ("_executor", "_housekeeping_executor"):
        pool = getattr(runner, attr, None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def test_housekeeping_and_turns_use_separate_pools():
    """The two pools must be distinct objects, or isolation is impossible."""
    runner = _runner()
    try:
        assert runner._get_executor() is not runner._get_housekeeping_executor()
    finally:
        _shutdown(runner)


def test_abandoned_housekeeping_cannot_delay_a_boot_resume():
    """The incident, as a regression pin.

    Abandon more cleanups than the housekeeping pool can hold — so it is
    saturated AND backlogged — then submit a boot-resume turn body and assert
    it starts within a bounded time. Before the split this starved: every
    abandoned worker held a turn slot.
    """
    wedge = threading.Event()
    entered = threading.Semaphore(0)

    def wedged_cleanup(agent):
        entered.release()
        assert wedge.wait(60), "wedge never released"

    runner = _runner(wedged_cleanup)
    turn_pool = runner._get_executor()
    hk_pool = runner._get_housekeeping_executor()
    hk_workers = hk_pool._max_workers
    abandoned = turn_pool._max_workers + hk_workers

    async def exercise():
        # Every one of these gives up after _CLEANUP_TIMEOUT_S and leaves its
        # worker running, exactly as the production log line describes.
        await asyncio.gather(
            *(
                runner._cleanup_agent_resources_off_loop(
                    object(), context="session expiry"
                )
                for _ in range(abandoned)
            )
        )
        for _ in range(hk_workers):
            assert await asyncio.to_thread(entered.acquire, True, 30)

        # Housekeeping is saturated and backlogged...
        assert len(hk_pool._threads or ()) == hk_workers
        assert hk_pool._work_queue.qsize() > 0
        # ...but it took nothing from the turn pool.
        assert len(turn_pool._threads or ()) == 0

        started: dict[str, float] = {}

        def boot_resume_body():
            started["at"] = time.monotonic()

        submitted = time.monotonic()
        await asyncio.wait_for(
            runner._run_in_executor_with_context(boot_resume_body), timeout=30
        )
        return started["at"] - submitted

    try:
        latency = asyncio.run(exercise())
    finally:
        wedge.set()
        _shutdown(runner)

    # Bounded start: the card's acceptance criterion. Generous vs the ~590s
    # incident and vs the cleanup budget the callers already abandoned.
    assert latency < 5.0, f"boot resume waited {latency:.2f}s behind housekeeping"


def test_turn_pool_saturation_still_queues_fairly():
    """Turn-vs-turn contention is NOT what this change alters.

    A pool of N with N in-flight turns still queues the N+1th; the fix is
    about housekeeping never consuming those slots, not about unbounded
    turn concurrency. Pinned so a future "just raise max_workers" change
    cannot silently claim to have fixed the incident.
    """
    release = threading.Event()
    occupied = threading.Semaphore(0)
    runner = _runner()
    max_workers = runner._get_executor()._max_workers

    def long_turn():
        occupied.release()
        assert release.wait(60)

    async def exercise():
        turns = [
            asyncio.ensure_future(runner._run_in_executor_with_context(long_turn))
            for _ in range(max_workers)
        ]
        for _ in range(max_workers):
            assert await asyncio.to_thread(occupied.acquire, True, 30)

        started = threading.Event()
        queued = asyncio.ensure_future(
            runner._run_in_executor_with_context(started.set)
        )
        await asyncio.sleep(0.5)
        was_queued = not started.is_set()
        release.set()
        await asyncio.gather(queued, *turns)
        return was_queued

    try:
        assert asyncio.run(exercise()), "a full turn pool must queue the next turn"
    finally:
        release.set()
        _shutdown(runner)


def test_executor_wait_logs_pool_depth_when_a_submit_waits(caplog, monkeypatch):
    """PHASE=executor_wait must name the pool, the wait and the saturation.

    The incident logs showed the inbound message and then the turn body ~590s
    later with nothing in between attributing the gap. This is that missing
    line.
    """
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_WAIT_WARN", "0.2")
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", "1")
    release = threading.Event()
    occupied = threading.Semaphore(0)
    runner = _runner()

    def long_turn():
        occupied.release()
        assert release.wait(60)

    def queued_turn():
        return "ran"

    async def exercise():
        first = asyncio.ensure_future(
            runner._run_in_executor_with_context(long_turn)
        )
        assert await asyncio.to_thread(occupied.acquire, True, 30)
        second = asyncio.ensure_future(
            runner._run_in_executor_with_context(queued_turn)
        )
        await asyncio.sleep(0.5)
        release.set()
        await asyncio.gather(first, second)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        try:
            asyncio.run(exercise())
        finally:
            release.set()
            _shutdown(runner)

    waits = [r.getMessage() for r in caplog.records if "PHASE=executor_wait" in r.getMessage()]
    assert waits, f"no executor_wait line emitted; saw {[r.getMessage() for r in caplog.records]}"
    line = waits[0]
    assert "pool=turn" in line
    assert "key=queued_turn" in line
    assert "max_workers=1" in line
    assert "inflight=1" in line


def test_fast_submit_does_not_log_executor_wait(caplog, monkeypatch):
    """The warning must be a saturation signal, not per-submit noise."""
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_WAIT_WARN", "5")
    runner = _runner()

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        try:
            asyncio.run(runner._run_in_executor_with_context(lambda: "quick"))
        finally:
            _shutdown(runner)

    assert not [
        r for r in caplog.records if "PHASE=executor_wait" in r.getMessage()
    ]


def test_housekeeping_waits_are_attributed_to_their_own_pool(caplog, monkeypatch):
    """A housekeeping backlog must be distinguishable from a turn backlog."""
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_WAIT_WARN", "0.2")
    monkeypatch.setenv("HERMES_GATEWAY_HOUSEKEEPING_MAX_WORKERS", "1")
    wedge = threading.Event()
    entered = threading.Semaphore(0)

    def wedged_cleanup(agent):
        entered.release()
        assert wedge.wait(60)

    runner = _runner(wedged_cleanup, cleanup_timeout=0.3)

    async def exercise():
        await runner._cleanup_agent_resources_off_loop(object(), context="expiry")
        assert await asyncio.to_thread(entered.acquire, True, 30)
        # Second cleanup queues behind the wedged one on the housekeeping pool.
        second = asyncio.ensure_future(
            runner._run_housekeeping_in_executor("cleanup", lambda: "hk")
        )
        await asyncio.sleep(0.5)
        wedge.set()
        await second

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        try:
            asyncio.run(exercise())
        finally:
            wedge.set()
            _shutdown(runner)

    waits = [
        r.getMessage() for r in caplog.records if "PHASE=executor_wait" in r.getMessage()
    ]
    assert waits, "housekeeping backlog emitted no executor_wait line"
    assert any("pool=cleanup" in line for line in waits), waits


def test_shutdown_stops_the_housekeeping_pool():
    """Non-daemon housekeeping workers must not survive shutdown.

    concurrent.futures registers an atexit hook that joins non-daemon workers;
    a pool left running would strand the process "down but not exited" — the
    same failure _shutdown_executor's bounded drain exists to prevent.
    """
    runner = _runner()
    hk = runner._get_housekeeping_executor()
    assert not hk._shutdown

    runner._shutdown_executor()

    assert hk._shutdown, "housekeeping pool was left running at shutdown"
    assert runner._housekeeping_executor is None
    # And the closing latch must stop it being resurrected mid-shutdown.
    with pytest.raises(RuntimeError):
        runner._get_housekeeping_executor()


def test_pool_sizes_are_configurable(monkeypatch):
    """Sizing must be an ops knob, not a recompile."""
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", "17")
    monkeypatch.setenv("HERMES_GATEWAY_HOUSEKEEPING_MAX_WORKERS", "3")
    runner = _runner()
    try:
        assert runner._get_executor()._max_workers == 17
        assert runner._get_housekeeping_executor()._max_workers == 3
    finally:
        _shutdown(runner)


@pytest.mark.parametrize("bad", ["0", "-4", "not-a-number", ""])
def test_invalid_pool_size_falls_back_to_default(monkeypatch, bad):
    """A typo in an ops knob must not create a zero-width or crashing pool."""
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", bad)
    runner = _runner()
    try:
        pool = runner._get_executor()
        assert isinstance(pool, concurrent.futures.ThreadPoolExecutor)
        assert pool._max_workers == 10
    finally:
        _shutdown(runner)


def test_contextvars_survive_both_pools():
    """The context hop is why these helpers exist; it must not regress.

    _run_in_executor_with_context was introduced because a bare
    run_in_executor drops the profile secret scope under multiplexing.
    """
    from contextvars import ContextVar

    probe: ContextVar[str] = ContextVar("probe", default="unset")
    runner = _runner()

    async def exercise():
        probe.set("scoped")
        turn = await runner._run_in_executor_with_context(probe.get)
        hk = await runner._run_housekeeping_in_executor("cleanup", probe.get)
        return turn, hk

    try:
        assert asyncio.run(exercise()) == ("scoped", "scoped")
    finally:
        _shutdown(runner)


def test_submitted_arguments_and_results_round_trip():
    """The timing wrapper must be transparent to args, results and errors."""
    runner = _runner()

    async def exercise():
        assert await runner._run_in_executor_with_context(
            lambda a, b: a + b, 2, 3
        ) == 5
        with pytest.raises(ValueError, match="boom"):
            await runner._run_housekeeping_in_executor(
                "cleanup", _raise_boom
            )

    try:
        asyncio.run(exercise())
    finally:
        _shutdown(runner)


def _raise_boom():
    raise ValueError("boom")
