"""The turn-body executor must never be narrower than turn admission.

RED-proven 2026-09-23 on Apollo: gateway.max_concurrent_turns=100 admitted
turns that then queued 2151-2356 s behind a 10-thread pool
(PHASE=executor_wait pool=turn inflight=10 queued=2..4). Admission decides a
turn may run; the pool must honour that decision. Known-bad revision: any
tree where _executor_max_workers() ignores the admission cap.
"""
from __future__ import annotations

import concurrent.futures
import types

import pytest

from gateway import run as run_mod
from gateway.run import GatewayRunner
from gateway.turn_admission import TurnAdmission


def test_default_pool_is_not_narrower_than_admission_cap(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", raising=False)
    assert run_mod._executor_max_workers(100) >= 100
    assert run_mod._executor_max_workers(None) == run_mod._EXECUTOR_MAX_WORKERS_DEFAULT
    # a cap below the default keeps the (wider) default
    assert run_mod._executor_max_workers(3) == run_mod._EXECUTOR_MAX_WORKERS_DEFAULT


def test_env_override_is_floored_at_admission_cap(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", "10")
    with caplog.at_level("WARNING", logger="gateway.run"):
        assert run_mod._executor_max_workers(100) == 100
    assert "narrower than gateway.max_concurrent_turns" in caplog.text
    # an override WIDER than the cap is honoured verbatim
    monkeypatch.setenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", "250")
    assert run_mod._executor_max_workers(100) == 250


def test_live_pool_is_created_with_admission_width(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_EXECUTOR_MAX_WORKERS", raising=False)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = types.SimpleNamespace(max_concurrent_turns=100, user_turn_reserve=20)
    pool = runner._get_executor()
    try:
        assert isinstance(pool, concurrent.futures.ThreadPoolExecutor)
        assert pool._max_workers >= 100, pool._max_workers
        assert isinstance(runner._turn_admission, TurnAdmission)
        assert runner._turn_admission.cap == 100
    finally:
        pool.shutdown(wait=False)
