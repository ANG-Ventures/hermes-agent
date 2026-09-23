"""The turn-body executor must never re-queue an ADMITTED turn.

RED-proven 2026-09-23 on Apollo: gateway.max_concurrent_turns=100 admitted
turns that then queued 334-2356 s behind a 10-thread ThreadPoolExecutor
(PHASE=executor_wait pool=turn inflight=10 queued=2..4). Ace ruling 08:47 PT:
no pool cap at all — admission is the only concurrency control. Known-bad
revision: any tree whose default turn executor has a finite max_workers.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
import types

from gateway import run as run_mod
from gateway.run import GatewayRunner
from gateway.turn_admission import TurnAdmission


def test_default_is_unbounded(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", raising=False)
    assert run_mod._executor_max_workers(100) is None
    assert run_mod._executor_max_workers(None) is None


def test_env_cap_is_floored_at_admission_cap(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", "10")
    with caplog.at_level("WARNING", logger="gateway.run"):
        assert run_mod._executor_max_workers(100) == 100
    assert "narrower than gateway.max_concurrent_turns" in caplog.text
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", "250")
    assert run_mod._executor_max_workers(100) == 250


def test_live_pool_runs_more_than_36_blocking_turns_concurrently(monkeypatch):
    """ThreadPoolExecutor(max_workers=None) is min(32, ncpu+4) — NOT unbounded.
    Prove the live pool actually runs 60 blocking bodies at once."""
    monkeypatch.delenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", raising=False)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = types.SimpleNamespace(max_concurrent_turns=100, user_turn_reserve=20)
    pool = runner._get_executor()
    try:
        assert isinstance(pool, concurrent.futures.Executor)
        assert isinstance(runner._turn_admission, TurnAdmission)
        n = 60
        gate = threading.Barrier(n + 1, timeout=10)
        def body():
            gate.wait()   # every body must be RUNNING at the same instant
            return threading.current_thread().name
        futs = [pool.submit(body) for _ in range(n)]
        gate.wait()       # raises BrokenBarrierError if fewer than n are running
        names = {f.result(timeout=10) for f in futs}
        assert len(names) == n
        assert all(nm.startswith("hermes-gateway") for nm in names), names
        assert pool._work_queue.qsize() == 0
    finally:
        pool.shutdown(wait=True)


def test_unbounded_executor_propagates_exceptions_and_shuts_down():
    ex = run_mod._UnboundedThreadExecutor("hermes-gateway-t")
    def boom():
        raise ValueError("x")
    f = ex.submit(boom)
    try:
        f.result(timeout=5)
    except ValueError:
        pass
    else:
        raise AssertionError("exception not propagated")
    ex.shutdown(wait=True)
    try:
        ex.submit(lambda: 1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("submit after shutdown must raise")
