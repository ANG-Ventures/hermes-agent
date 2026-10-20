"""The gateway tick series ``var/gate-ticks.jsonl`` (Fleet Work Allocation
spec v0.3 §3.6, AC-12; card t_90887924).

ONE line per gateway tick, written from the tick loop in
``gateway.kanban_watchers`` after the board loop; ZERO lines from
``kanban dispatch --dry-run`` or a standalone-daemon tick, which share
``GateTickBuilder.build()``. Drives the real watcher loop (the fair-dispatch
harness), the real one-shot CLI and the real daemon.
"""

import argparse
import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import kanban_gate_series as series
from gateway import kanban_watchers as watchers
from hermes_cli import config, kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_load_gate as klg

from tests.gateway.test_kanban_dispatcher_standby import Clock, cancel, runner


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_DISPATCH_IN_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = {"kanban": {"dispatch_interval_seconds": 2, "auto_decompose": False}}
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(kbd, "resolve_max_in_progress", lambda value: value)
    kb.init_db()
    return home


def _lines(home: Path) -> list:
    p = series.series_path(home)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.fixture
def two_boards(home, monkeypatch):
    """Two boards, allowance 4/tick, a fake dispatch that leaves 1 portable
    row waiting on 'subs-ace' and routes every ready row."""
    ready = {"default": 10, "subs-ace": 3}
    monkeypatch.setattr(kb, "list_boards", lambda **kw: [{"slug": "default"}, {"slug": "subs-ace"}])
    monkeypatch.setattr(kb, "count_spawnable_demand", lambda conn, **kw: ready[conn.slug])
    real_connect = kb.connect

    def connect(board=None, **kw):
        if board in ready:
            return SimpleNamespace(slug=board, close=lambda: None)
        return real_connect(board=board, **kw)

    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)
    monkeypatch.setattr(kbd, "reap_worker_zombies", Mock(return_value=[]))

    def fake_dispatch(conn, *, board=None, spawn_paused=None, spawn_limit=None, **kw):
        # A real DB statement inside the tick so `queries` is observable.
        with kb.connect_closing() as real:
            real.execute("SELECT count(*) FROM tasks").fetchone()
        res = kb.DispatchResult()
        n = 0 if spawn_paused else min(ready[board], spawn_limit if spawn_limit is not None else 99)
        res.spawned = [(f"{board}-{ready[board] - i}", "alpha", None) for i in range(n)]
        res.rows_routed = ready[board]
        if board == "subs-ace":
            res.portable_waiting = 1
        ready[board] -= n
        return res

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch)
    monkeypatch.setattr(klg.LoadGate, "admit_now", lambda self, **kw: (4, None))
    clock = Clock()
    monkeypatch.setattr(watchers, "asyncio", SimpleNamespace(
        sleep=clock.sleep, to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError,
        create_task=asyncio.create_task, shield=asyncio.shield,
    ))
    return SimpleNamespace(clock=clock, home=home)


async def _run_ticks(clock, n_resumes: int):
    b = runner()
    task = asyncio.create_task(b._kanban_dispatcher_watcher())
    try:
        for _ in range(n_resumes):
            _delay, resume = await clock.paused(task)
            resume.set_result(None)
    finally:
        await cancel(task, b)


@pytest.mark.asyncio
async def test_one_line_per_gateway_tick_with_every_field(two_boards):
    """AC-12: each gateway tick appends exactly one complete line.
    Mutation: remove the append from the tick loop -> 0 lines (RED)."""
    await _run_ticks(two_boards.clock, 4)
    lines = _lines(two_boards.home)
    assert len(lines) >= 2, lines
    for rec in lines:
        assert set(series.FIELDS) <= set(rec), set(series.FIELDS) - set(rec)
        assert rec["path"] == "gateway"
        assert rec["boards"] == 2
        assert rec["allowance"] == 4
        # 4 granted, 4 spawned across both boards after the _spare pass.
        assert rec["allowance_unused"] == 0
        assert rec["portable_waiting"] == 1
        assert rec["tick_ms_cpu"] >= 0 and rec["tick_ms_wall"] >= 0
    # The first tick routed 13 ready rows across both boards: rows_routed is
    # read from the board loop, so a tick_ms measured around build() alone
    # (mutation) would leave it at 0.
    assert lines[0]["rows_routed"] == 13
    assert lines[0]["queries"] >= 2


@pytest.mark.asyncio
async def test_allowance_unused_is_read_after_spare_redistribution(two_boards, monkeypatch):
    """Board 'default' spawns nothing; its quota passes to 'subs-ace' through
    _spare. Allowance 1, one local spawn -> allowance_unused 0, never 1."""
    monkeypatch.setattr(klg.LoadGate, "admit_now", lambda self, **kw: (1, None))
    inner = kbd.dispatch_once

    def capped(conn, *, board=None, **kw):
        if board == "default":
            return kb.DispatchResult()
        return inner(conn, board=board, **kw)

    monkeypatch.setattr(kbd, "dispatch_once", capped)
    await _run_ticks(two_boards.clock, 2)
    first = _lines(two_boards.home)[0]
    assert first["allowance"] == 1
    assert first["allowance_unused"] == 0, first


def test_dry_run_and_standalone_daemon_add_no_line(home, monkeypatch):
    """AC-12 negative: neither 5 one-shot dry-runs nor one standalone-daemon
    tick may append. The daemon tick goes through GateTickBuilder.build()
    (asserted), so moving the append into build() turns this RED. The one-shot
    CLI calls plan_pool() directly, not build(), on the current tree."""
    from hermes_cli import kanban_ops as kops
    from gateway import kanban_gate_tick as kgt

    built = []
    real_build = kgt.GateTickBuilder.build

    def spy_build(self, boards):
        built.append(1)
        return real_build(self, boards)

    monkeypatch.setattr(kgt.GateTickBuilder, "build", spy_build)
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (1.0, 1.0))
    monkeypatch.setattr(klg, "sample_cpu_busy", lambda prev=None, block=0.0: (0.1, None))
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"dispatch_load_gate": {}}})
    for _ in range(5):
        assert kops._cmd_dispatch(argparse.Namespace(dry_run=True, json=False, max=None,
                                                     max_running=None, failure_limit=2)) == 0
    stop = threading.Event()
    ticks = []

    def on_tick(_res):
        ticks.append(1)
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick,
                   load_gate=klg.LoadGate({}, os.cpu_count() or 1))
    assert ticks == [1]
    assert built, "the daemon tick must go through GateTickBuilder.build()"
    assert _lines(home) == []


def test_rotation_renames_once_per_utc_day_and_keeps_14(tmp_path):
    p = tmp_path / "var" / series.SERIES_FILE
    day = 86400
    t0 = 1_791_000_000.0
    for i in range(17):
        now = t0 + i * day
        series.append_record(p, {"ts": now}, now=now)
        os.utime(p, (now, now))
    rotated = sorted(p.parent.glob("gate-ticks.????-??-??.jsonl"))
    assert len(rotated) == 14
    # The live file holds only today's line; no line was lost or duplicated
    # across the kept window.
    assert len(p.read_text().splitlines()) == 1
    assert all(len(r.read_text().splitlines()) == 1 for r in rotated)
    # Same UTC day: no rename.
    assert series.rotate(p, t0 + 16 * day + 60) is None


def test_rotation_never_overwrites_an_existing_archive(tmp_path):
    """Prism 9376e97343ca: a clock step back after a rotation must not let the
    next rotation replace the day's archive."""
    p = tmp_path / "var" / series.SERIES_FILE
    t0 = 1_791_000_000.0
    series.append_record(p, {"n": 1}, now=t0)
    os.utime(p, (t0, t0))
    series.append_record(p, {"n": 2}, now=t0 + 86400)       # rotates day0 -> archive
    os.utime(p, (t0, t0))                             # clock stepped back: live file dated day0 again
    series.rotate(p, t0 + 86400)
    (arch,) = p.parent.glob("gate-ticks.????-??-??.jsonl")
    assert [json.loads(l)["n"] for l in arch.read_text().splitlines()] == [1, 2]


def test_failed_append_rolls_back_the_partial_line(tmp_path, monkeypatch):
    """Prism 12af5c33fcb9: a short write is completed, a failed one truncated."""
    p = tmp_path / "var" / series.SERIES_FILE
    series.append_record(p, {"n": 1})
    real = os.write
    calls = {"n": 0}

    def short_then_fail(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(fd, data[:5])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(series.os, "write", short_then_fail)
    with pytest.raises(OSError):
        series.append_record(p, {"n": 2})
    monkeypatch.setattr(series.os, "write", real)
    series.append_record(p, {"n": 3})
    assert [json.loads(l)["n"] for l in p.read_text().splitlines()] == [1, 3]


def test_failed_rotation_merge_leaves_the_archive_untouched(tmp_path, monkeypatch):
    """Prism 2468447f9fca: a disk-full merge into an existing archive is rolled
    back, so a retried rotation neither truncates nor duplicates lines."""
    p = tmp_path / "var" / series.SERIES_FILE
    t0 = 1_791_000_000.0
    series.append_record(p, {"n": 1}, now=t0)
    os.utime(p, (t0, t0))
    series.append_record(p, {"n": 2}, now=t0 + 86400)
    os.utime(p, (t0, t0))
    (arch,) = p.parent.glob("gate-ticks.????-??-??.jsonl")
    before = arch.read_bytes()
    real = os.write
    monkeypatch.setattr(series.os, "write", lambda fd, data: real(fd, data[:3]) if len(data) > 3 else (_ for _ in ()).throw(OSError(28, "full")))
    with pytest.raises(OSError):
        series.rotate(p, t0 + 86400)
    monkeypatch.setattr(series.os, "write", real)
    assert arch.read_bytes() == before and p.exists()
    series.rotate(p, t0 + 86400)
    assert [json.loads(l)["n"] for l in arch.read_text().splitlines()] == [1, 2]


def test_crash_between_merge_and_unlink_never_duplicates_records(tmp_path, monkeypatch):
    """Apollo review r1 (t_90887924), Prism 2468447f9fca: a crash after the
    archive merge but before the live file is unlinked must not re-append the
    same ticks on the next rotation (the I-0 query sums this series)."""
    p = tmp_path / "var" / series.SERIES_FILE
    t0 = 1_791_000_000.0
    series.append_record(p, {"n": 1}, now=t0)
    os.utime(p, (t0, t0))
    series.append_record(p, {"n": 2}, now=t0 + 86400)       # day0 archive holds n=1
    os.utime(p, (t0, t0))                             # clock step back: live dated day0
    (arch,) = p.parent.glob("gate-ticks.????-??-??.jsonl")
    real_unlink = Path.unlink

    def crash_on_live(self, *a, **kw):
        if self == p:
            raise OSError("simulated crash before unlink")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", crash_on_live)
    with pytest.raises(OSError):
        series.rotate(p, t0 + 86400)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert p.exists()
    series.rotate(p, t0 + 86400)                      # the retry after the crash
    assert not p.exists()
    assert [json.loads(l)["n"] for l in arch.read_text().splitlines()] == [1, 2]
    assert not list(p.parent.glob(".*.tmp"))


def test_pool_budget_is_the_plan_budget_before_any_take():
    """Prism 24de3acc1c0b: an unpinned take with the signal off decrements
    slots without counting `taken`; the series must still record the grant."""
    from hermes_cli import kanban_worker_pool as kwp
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=4,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state="active", enabled=True, priority=0)
    plan = kwp.SpilloverPlan(hosts={"ace-ai": host}, slots={"ace-ai": 4})
    budget = plan.budget
    for _ in range(4):
        assert plan.take("alpha") is not None
    rec = series.tick_record(ts=0.0, gate=SimpleNamespace(), tick=SimpleNamespace(remote_plan=plan, band="spilling", spill_reason="load"),
                             builder=SimpleNamespace(), allowance=0, pool_budget=budget, spawn_paused=None,
                             local_spawned=0, results=[], tick_ms_cpu=1.0, tick_ms_wall=1.0, boards=1, queries=0)
    assert rec["pool_budget"] == 4 and rec["pool_free_by_host"] == {"ace-ai": 0}
