"""The kanban dispatcher's spawn gate (kanban.dispatch_load_gate).

Since t_b20850d2 (FWA-H, Ace 2026-11-04 07:36: ONE layer decides how much work runs) the gate has one
predicate, ``admission.admit_class("kanban")`` over the host's measured sample; load1 is recorded for display
and never read. These tests pin the gate's plumbing (disabled, log/state lines, the daemon, the per-board
split, the running count). The predicate itself is pinned in test_kanban_load_gate_utilization.py.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.fleet_placement import admission as ad
from hermes_cli import kanban_load_gate as klg
from hermes_cli.kanban_load_gate import LoadGate


@pytest.fixture(autouse=True)
def ctx(monkeypatch):
    """A hermetic utilization context: 32 GB free over the reserve (room for 10 x 3 GB), no ceiling, no PSI."""
    c = {"host": "mac-studio", "ceiling": None, "ceiling_reason": "",
         "need": ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior"),
         "limits": ad.Limits(mem_reserve_gb=128.0), "mem_available_gb": 160.0, "mem_total_gb": 512.0}
    monkeypatch.setattr(klg, "utilization_context", lambda: dict(c))
    monkeypatch.setattr(klg.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(ad, "read_slice_memory", lambda **kw: None)
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0,
                                                    "psi_io_some10": 0.0})
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")   # no fleet demand file in a temp home
    # the shared reservation ledger is the RUNNER HOST's real file: other consumers' live leases there are not this
    # test's input (CI slice 5/5 read (3, None)); the ledger read has its own tests in test_kanban_local_start_ledger
    monkeypatch.setattr(klg, "shared_leases", lambda host, *a, **k: [])
    return c


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _board_with_ready(kanban_home, monkeypatch, n):
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (kanban_home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(n):
            kb.create_task(conn, title=f"t{i}", assignee="alpha")


def test_disabled_gate_is_inert():
    g = LoadGate({"enabled": False}, ncpu=1)
    assert g.admit(999.0, now=0) == (None, None)


def test_bad_config_values_fall_back_to_defaults():
    g = LoadGate({"ramp_seconds": -1, "max_spawn_per_tick": 0}, ncpu=8)
    assert (g.ramp_seconds, g.max_spawn_per_tick) == (600.0, 4)


def test_load1_is_never_read_only_the_measured_sample(ctx):
    """t_b20850d2 (red on base: the legacy gate paused at load1 > pause_above with an idle CPU and free memory).
    A load1 of 250 on 32 threads with the CPU 10% busy admits; the same load with the CPU saturated refuses on cpu."""
    leftover = {"pause_above": 64, "resume_below": 48, "cpu_busy_pause": 0.5, "worker_cpu_cost": 8.0,
                "spill_fraction": 0.5, "cpu_spill": 0.5, "cpu_spill_resume": 0.4, "cpu_pause": 0.5}
    g = LoadGate(leftover, ncpu=32)                      # leftover keys in config.yaml are ignored, not refused
    assert g.admit(250.0, load5=240.0, now=0, running=0, cpu_busy=0.10) == (4, None)   # max_spawn_per_tick
    assert g.state == "admitting" and g.band == "admitting"
    busy = LoadGate({}, ncpu=32)
    a, why = busy.admit(1.0, load5=1.0, now=0, running=0, cpu_busy=0.99)
    assert a == 0 and "saturated:cpu" in why and busy.band == "spilling"
    assert "pause_above" not in busy.snapshot() and busy.snapshot()["admission"] == "utilization"


def test_gate_from_config_runs_utilization_whatever_kanban_admission_says(ctx):
    """kanban.admission: legacy no longer selects a load1 gate for the spawn path (t_b20850d2)."""
    g = klg.gate_from_config({"kanban": {"admission": "legacy", "dispatch_load_gate": {"pause_above": 1}}})
    assert g.admit(500.0, now=0, running=0, cpu_busy=0.05) == (4, None)


def test_dispatch_once_spawn_limit_intersects_budget(kanban_home, monkeypatch):
    _board_with_ready(kanban_home, monkeypatch, 5)
    with kb.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, spawn_limit=2)
    assert len(res.spawned) == 2
    with kb.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, spawn_limit=0)
    assert res.spawned == []


# ── observability ───────────────────────────────────────────────────────────

def test_log_line_on_state_change_and_five_minute_summary(caplog):
    log = logging.getLogger("test.load_gate")
    g = LoadGate({}, ncpu=32)
    with caplog.at_level(logging.INFO, logger="test.load_gate"):
        g.admit(20.0, now=0, running=0, cpu_busy=0.1); g.record_spawns(4, now=0); g.log_tick(log, now=0)
        g.admit(20.0, now=10, running=4, cpu_busy=0.1); g.record_spawns(2, now=10); g.log_tick(log, now=10)
        g.admit(40.0, now=20, running=6, cpu_busy=0.99); g.record_spawns(0, now=20); g.log_tick(log, now=20)
        g.admit(40.0, now=30, running=6, cpu_busy=0.99); g.log_tick(log, now=30)      # no change: silent
        g.admit(40.0, now=400, running=6, cpu_busy=0.99); g.log_tick(log, now=400)    # 5-min summary
    msgs = [r.getMessage() for r in caplog.records]
    changes = [m for m in msgs if "->" in m]
    assert len(changes) == 2
    assert "start -> state=admitting" in changes[0]
    assert "admitting -> state=saturated" in changes[1]
    summaries = [m for m in msgs if "summary" in m]
    assert len(summaries) == 1 and "admitted_5m=6" in summaries[0]
    assert "admission=utilization" in summaries[0] and "pause_above" not in summaries[0]
    # Ace ruling 2026-10-12 (t_998e643d): the load figure carries its denominator, threads = the gate's ncpu
    assert re.search(r"\(load 40\.0 tasks / (?:\d+c\u00b7)?32t, not read\)", summaries[0]), summaries[0]


def test_state_file_round_trip_and_diagnostics_line(tmp_path):
    g = LoadGate({}, ncpu=32)
    g.admit(20.0, load5=18.0, now=0, running=0, cpu_busy=0.1)
    g.record_spawns(4, now=0)
    path = tmp_path / "load_gate.json"
    g.write_state(path)
    state = klg.read_state(path)
    assert state["state"] == "admitting" and state["admitted_last_tick"] == 4
    line = klg.format_state_line(state)
    assert line.startswith("Load gate: admitting admission=utilization cpu_busy=0.1 load1=20.0 load5=18.0")
    assert "allowance=4" in line and "pause_above" not in line
    assert re.search(r"\(load 20\.0 tasks / (?:\d+c\u00b7)?32t, not read\)", line), line
    assert "no state published" in klg.format_state_line(None)


def test_run_daemon_applies_gate(kanban_home, monkeypatch):
    import threading

    captured = []
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.append(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (1.0, 1.0))           # load1 low: never the reason
    monkeypatch.setattr(klg, "sample_cpu_busy", lambda prev=None, block=0.0: (0.99, object()))
    gate = LoadGate({}, ncpu=32)
    kbd.run_daemon(interval=0.01, stop_event=stop,
                  on_tick=lambda _res: stop.set(), load_gate=gate)
    assert captured[0]["spawn_limit"] == 0
    assert "saturated:cpu" in captured[0]["spawn_paused"]
    assert gate.state == "saturated"
    assert klg.read_state()["state"] == "saturated"


# --- per-board split of one tick's allowance (t_f78d1938) -------------------


def _simulate_ticks(split, ticks, allowance, ready):
    """Drive ``split`` for N ticks; each tick every board spawns its quota."""
    got = []
    for t in range(ticks):
        demand = [(slug, n) for slug, n in ready.items()]
        quotas = split(allowance, demand, start=t)
        got.append(dict(quotas))
        for slug, q in quotas.items():
            ready[slug] -= q or 0
    return got


def test_split_allowance_never_starves_the_second_board():
    """2 boards, allowance 4, A has 10 ready, B has 3 -> B gets >= 1 every
    tick while it has ready work. The fixed-order consumer this replaces
    gave A 4/4 on every tick and B nothing (subs-ace 0 spawns for 93 min)."""
    got = _simulate_ticks(klg.split_allowance, 3, 4, {"a": 10, "b": 3})
    assert [g["b"] for g in got] == [2, 1, 0]
    assert all(sum(g.values()) == 4 for g in got)
    assert [g["a"] for g in got] == [2, 3, 4]


def test_fixed_order_mutant_goes_red():
    def fixed_order(allowance, demand, start=0):
        left, out = allowance, {}
        for slug, n in demand:
            out[slug] = min(n, left)
            left -= out[slug]
        return out

    got = _simulate_ticks(fixed_order, 2, 4, {"a": 10, "b": 3})
    assert any(g["b"] == 0 for g in got)  # the starvation the split removes


def test_split_allowance_rotates_first_pick_when_allowance_is_one():
    firsts = [
        [s for s, q in klg.split_allowance(1, [("a", 5), ("b", 5), ("c", 5)], start=t).items() if q][0]
        for t in range(6)
    ]
    assert firsts == ["a", "b", "c", "a", "b", "c"]


def test_split_allowance_edges():
    assert klg.split_allowance(None, [("a", 3)]) == {"a": None}
    assert klg.split_allowance(0, [("a", 3), ("b", 1)]) == {"a": 0, "b": 0}
    # demand below allowance: every board gets its whole demand, no more
    assert klg.split_allowance(4, [("a", 1), ("b", 0), ("c", 2)]) == {"a": 1, "b": 0, "c": 2}


def test_board_starvation_lines_threshold():
    state = {"boards": {
        "subs-ace": {"ready": 7, "quota": 1, "spawned": 0, "starved_since": 1000.0},
        "default": {"ready": 3, "quota": 2, "spawned": 2, "starved_since": None},
        "young": {"ready": 1, "quota": 1, "spawned": 0, "starved_since": 1500.0},
    }}
    lines = klg.format_board_starvation_lines(state, now=1000.0 + 700)
    assert len(lines) == 1 and "[subs-ace]" in lines[0] and "ready=7" in lines[0]
    assert klg.format_board_starvation_lines(None) == []


def test_dispatch_once_names_the_cap_that_refused(kanban_home, monkeypatch):
    """A manual dispatch that spawns 0 must say which cap refused."""
    _board_with_ready(kanban_home, monkeypatch, 4)
    with kb.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, spawn_limit=0)
    assert res.spawned == [] and "load gate" in (res.spawn_capped or "")
    with kb.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, max_spawn=2)
    assert len(res.spawned) == 2 and res.spawn_capped is None
    with kb.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, max_spawn=2)
    assert res.spawned == [] and "max_spawn=2" in (res.spawn_capped or "")


def test_count_spawnable_demand(kanban_home, monkeypatch):
    _board_with_ready(kanban_home, monkeypatch, 3)
    with kb.connect_closing() as conn:
        assert kb.count_spawnable_demand(conn) == 3
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: False, raising=False)
    with kb.connect_closing() as conn:
        assert kb.count_spawnable_demand(conn) == 0


def test_sample_cpu_busy_delta(monkeypatch):
    import collections
    import sys
    import types

    T = collections.namedtuple("T", "user system idle")
    seq = iter([T(10, 10, 80), T(40, 20, 90)])
    fake = types.SimpleNamespace(cpu_times=lambda: next(seq))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    busy, snap = klg.sample_cpu_busy(None)
    assert busy is None and snap == T(10, 10, 80)
    busy, _ = klg.sample_cpu_busy(snap)
    assert abs(busy - 0.8) < 1e-9          # 40 of 50 ticks not idle


def test_count_running_workers(kanban_home, monkeypatch):
    _board_with_ready(kanban_home, monkeypatch, 2)
    assert klg.count_running_workers() == 0
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='running'")
        conn.commit()
    assert klg.count_running_workers() == 2
    monkeypatch.setattr(kb, "connect", lambda *a, **k: 1 / 0)
    assert klg.count_running_workers() is None


# -- one unreadable board must not hide the healthy ones (Prism P1 72ab3045) --
def _corrupt_current_board_with_healthy_second(kanban_home, running_on_second):
    kb.create_board("second")
    with kb.connect(board="second") as conn:
        for i in range(running_on_second):
            tid = kb.create_task(conn, title=f"busy{i}", assignee="alpha")
            assert kb.claim_task(conn, tid) is not None
    # A real corrupt file, not a mocked connect(): the default board's DB.
    kb.kanban_db_path(board=None).write_bytes(b"not a sqlite database" * 64)
    with pytest.raises(Exception):
        kb.connect().close()


@pytest.mark.parametrize("running_on_second", [0, 2])
def test_count_running_workers_isolates_unreadable_current_board(
    kanban_home, running_on_second,
):
    """Red on eb4963e8: the current board's connect() raised and the whole
    host count came back None, hiding the healthy board."""
    _corrupt_current_board_with_healthy_second(kanban_home, running_on_second)
    assert klg.count_running_workers() == running_on_second


def test_unreadable_current_board_keeps_small_host_dispatching(kanban_home):
    """The finding's scenario end to end: idle small host, corrupt current board, healthy idle board ->
    the running count is still known (0), so the gate admits."""
    _corrupt_current_board_with_healthy_second(kanban_home, 0)
    g = LoadGate({"max_spawn_per_tick": 1}, ncpu=4)
    allowance, reason = g.admit(0.1, now=0.0, running=klg.count_running_workers(), cpu_busy=0.0)
    assert (allowance, reason) == (1, None)


def test_count_running_workers_none_only_when_no_board_readable(
    kanban_home, monkeypatch,
):
    _corrupt_current_board_with_healthy_second(kanban_home, 1)
    monkeypatch.setattr(kb, "list_boards", lambda **k: 1 / 0)
    assert klg.count_running_workers() is None


def test_host_count_counts_each_db_file_once(kanban_home):
    """Current board also appears in list_boards(): count it once."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="busy", assignee="alpha")
        assert kb.claim_task(conn, tid) is not None
    assert kb.count_running_tasks_host() == 1
