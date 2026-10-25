"""t_0d987578 D4: ``lcm_db_fds`` / ``cached_agents`` in the runtime-status payload.

The gauge runs on the ``gateway-runtime-status-writer`` thread only, counts this process's
descriptors on lcm.db by ``(st_dev, st_ino)``, and warns at most once per 10 minutes when
``lcm_db_fds > 3 * max(cached_agents, 1)``. Clock injected; no sleeps.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading

import pytest

from gateway import status


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _gauge(clock, fds, cached):
    return status._LcmFdGauge(
        clock=clock, identities=lambda: {(1, 1)},
        cached_agents=lambda: cached[0], count_fds=lambda _ids: fds[0])


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "lcm.db descriptor leak" in r.getMessage()]


def test_count_fds_on_counts_real_descriptors_by_inode(tmp_path):
    db = tmp_path / "lcm.db"
    conns = [sqlite3.connect(db) for _ in range(3)]
    for c in conns:
        c.execute("select 1")
    st = db.stat()
    try:
        assert status.count_fds_on({(st.st_dev, st.st_ino)}) == 3
    finally:
        for c in conns:
            c.close()
    assert status.count_fds_on({(st.st_dev, st.st_ino)}) == 0


def test_payload_carries_lcm_db_fds_and_cached_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("LCM_DATABASE_PATH", raising=False)
    status._reset_identity_caches()
    conns = [sqlite3.connect(tmp_path / "lcm.db") for _ in range(2)]
    for c in conns:
        c.execute("select 1")

    class _Runner:
        def __init__(self):
            self._agent_cache = {"a": 1, "b": 2, "c": 3, "d": 4}

        def count(self):
            return len(self._agent_cache)

    runner = _Runner()
    monkeypatch.setattr(status, "_cached_agents_source", None)
    status.set_cached_agents_source(runner.count)
    monkeypatch.setattr(status, "_LCM_FD_GAUGE", status._LcmFdGauge())
    seen_threads = []
    real_count = status.count_fds_on

    def spy(ids):
        seen_threads.append(threading.current_thread().name)
        return real_count(ids)

    monkeypatch.setattr(status._LCM_FD_GAUGE, "_count_fds", spy)
    try:
        writer = status._RuntimeStatusWriter()
        path = tmp_path / "gateway_state.json"
        assert writer.wait(writer.submit(path, {"gateway_state": "running"}), timeout=5)
        data = json.loads(path.read_text())
    finally:
        for c in conns:
            c.close()
    assert data["lcm_db_fds"] == 2
    assert data["cached_agents"] == 4
    assert seen_threads == ["gateway-runtime-status-writer"], "fd scan must run on the status writer thread only"


def test_warns_exactly_once_per_ten_minutes_when_ratio_exceeded(caplog):
    clock, fds, cached = _Clock(), [130], [20]  # 130 > 3*20 -> leak
    gauge = _gauge(clock, fds, cached)
    caplog.set_level(logging.WARNING, logger="gateway.status")

    for _ in range(10):  # every minute for 10 minutes minus a tick
        payload = {}
        gauge.annotate(payload)
        assert payload == {"lcm_db_fds": 130, "cached_agents": 20}
        clock.t += 59.9
    assert len(_warnings(caplog)) == 1

    clock.t = 1000.0 + 600.0  # 10 minutes after the first warning
    gauge.annotate({})
    assert len(_warnings(caplog)) == 2


def test_no_warning_at_or_below_threshold(caplog):
    clock, fds, cached = _Clock(), [60], [20]  # exactly 3x: not exceeded
    gauge = _gauge(clock, fds, cached)
    caplog.set_level(logging.WARNING, logger="gateway.status")
    gauge.annotate({})
    fds[0], cached[0] = 3, 0  # empty cache: denominator floors at 1, 3 <= 3
    clock.t += 61
    gauge.annotate({})
    assert _warnings(caplog) == []
    fds[0] = 4
    clock.t += 61
    gauge.annotate({})
    assert len(_warnings(caplog)) == 1


def test_scan_is_rate_limited_between_turn_boundary_writes():
    clock, calls = _Clock(), []
    gauge = status._LcmFdGauge(
        clock=clock, identities=lambda: {(1, 1)}, cached_agents=lambda: 1,
        count_fds=lambda ids: calls.append(1) or 2)
    for _ in range(50):
        p = {}
        gauge.annotate(p)
        assert p["lcm_db_fds"] == 2
        clock.t += 1.0
    assert len(calls) == 1
    clock.t += 60
    gauge.annotate({})
    assert len(calls) == 2


def test_gauge_failure_never_breaks_the_status_write(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("fstat exploded")

    monkeypatch.setattr(status, "_LCM_FD_GAUGE", status._LcmFdGauge(identities=boom))
    writer = status._RuntimeStatusWriter()
    path = tmp_path / "gateway_state.json"
    assert writer.wait(writer.submit(path, {"gateway_state": "running"}), timeout=5)
    data = json.loads(path.read_text())
    assert data["gateway_state"] == "running"
    assert "lcm_db_fds" not in data


def test_runner_registers_cached_agent_count(monkeypatch):
    from collections import OrderedDict
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict(a=1, b=2)
    monkeypatch.setattr(status, "_cached_agents_source", None)
    status.set_cached_agents_source(runner._cached_agent_count)
    assert status._cached_agents_now() == 2
    del runner
    import gc
    gc.collect()
    assert status._cached_agents_now() is None, "the source is held weakly; a dead runner reads as unknown"
