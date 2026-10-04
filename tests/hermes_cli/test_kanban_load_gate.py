"""Projected-load admission gate for the kanban dispatcher (t_b30d4a13).

Behaviour contract (kanban.dispatch_load_gate):

* load1 lags a spawn burst by 60-90 s, so the OLD hysteresis-only gate
  re-admitted a whole backlog on every quiet sample (gateway.log 2026-09-25:
  ``RESUMED load1=20.0`` -> ``spawned=29``; 66 workers / load1 146).
* NEW: admission against load1 + pending_ramp (recent spawns x
  worker_load_cost), a per-tick burst cap, and a load5 floor on resume.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_load_gate as klg
from hermes_cli.kanban_load_gate import LoadGate


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


def _tick(gate, load1, now, *, load5=None, old=False):
    """One dispatcher tick exactly as the gateway/daemon loop runs it."""
    if old:
        # The pre-fix gate: hysteresis only, no allowance.
        reason, allowance = gate.update(load1), None
    else:
        allowance, reason = gate.admit(load1, load5=load5, now=now)
    with kb.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 1, max_spawn=64,
            spawn_paused=reason, spawn_limit=allowance,
        )
    if not old:
        gate.record_spawns(len(res.spawned), now=now)
    return res


# ── the incident: burst at load 20, then load 146 ──────────────────────────

@pytest.mark.parametrize("old", [True, False], ids=["old-gate", "new-gate"])
def test_synthetic_burst_old_admits_backlog_new_caps_then_pauses(
    kanban_home, monkeypatch, old,
):
    _board_with_ready(kanban_home, monkeypatch, 30)
    gate = LoadGate({}, ncpu=32)
    total = 0
    # load1 still reads 20 (lag) the tick after the burst, then the fan-out
    # lands and load1 reads 146.
    for now, load1 in ((0.0, 20.0), (60.0, 20.0), (120.0, 146.0)):
        res = _tick(gate, load1, now, old=old)
        total += len(res.spawned)
        if load1 == 146.0:
            assert res.spawned == [] and res.spawn_paused
    if old:
        assert total == 30          # the defect: whole backlog in one tick
    else:
        assert 1 <= total <= 6      # governed: burst cap + projected ramp
        assert gate.state == "paused"


def test_new_gate_first_tick_is_burst_capped_and_second_sees_ramp(
    kanban_home, monkeypatch,
):
    _board_with_ready(kanban_home, monkeypatch, 30)
    gate = LoadGate({}, ncpu=32)
    assert len(_tick(gate, 20.0, 0.0).spawned) == 4        # max_spawn_per_tick
    # load1 unchanged (lag); 4 x 2.0 x (1 - 30/600) = 7.6 pending:
    # floor((32-20-7.6)/2) = 2
    assert len(_tick(gate, 20.0, 30.0).spawned) == 2
    # 4 x 0.9 + 2 x 0.95 = 5.5 invisible workers x 2.0 = 11.0 pending:
    # floor(1.0/2) = 0 -> saturated, nothing spawned
    res = _tick(gate, 20.0, 60.0)
    assert res.spawned == [] and "pending_ramp=11.0" in (res.spawn_paused or "")
    assert gate.state == "saturated"
    # the ramp (600 s) runs out -> admits again, still capped
    assert len(_tick(gate, 20.0, 700.0).spawned) == 4


# ── pure gate arithmetic ────────────────────────────────────────────────────

def test_allowance_formula_and_caps():
    g = LoadGate({"max_spawn_per_tick": 100}, ncpu=32)
    assert g.admit(20.0, now=0) == (6, None)                 # floor(12/2)
    a, reason = g.admit(31.0, now=0)                         # floor(0.5): stop short
    assert a == 0 and reason.startswith("projected")
    g.record_spawns(3, now=0)
    assert g.pending(now=0) == 6.0
    assert g.pending(now=300) == 3.0                         # half visible by now
    assert g.admit(20.0, now=300) == (4, None)               # floor((12-3)/2)
    assert g.pending(now=601) == 0.0                         # window expired
    g4 = LoadGate({}, ncpu=32)
    assert g4.admit(0.0, now=0) == (4, None)                 # default burst cap
    g5 = LoadGate({"worker_load_cost": 4, "max_spawn_per_tick": 50}, ncpu=32)
    assert g5.admit(0.0, now=0) == (8, None)


def test_hard_pause_hysteresis_and_load5_floor():
    g = LoadGate({}, ncpu=32)
    a, reason = g.admit(146.0, load5=90.0, now=0)
    assert a == 0 and g.state == "paused" and "load1=146.0" in reason
    # one quiet load1 sample while load5 is still saturated: stays paused
    a, reason = g.admit(20.0, load5=60.0, now=60)
    assert a == 0 and g.state == "paused"
    # load5 back under the bar too: resumes
    a, reason = g.admit(20.0, load5=30.0, now=120)
    assert a == 4 and reason is None and g.state == "admitting"
    # floor off: the quiet load1 sample alone resumes
    g2 = LoadGate({"load5_floor": False}, ncpu=32)
    g2.admit(146.0, load5=90.0, now=0)
    assert g2.admit(20.0, load5=60.0, now=60)[0] == 4


def test_legacy_update_contract_unchanged():
    from gateway.kanban_watchers import LoadGate as ReExported

    assert ReExported is LoadGate
    g = LoadGate({}, ncpu=32)
    assert g.update(33) is not None and g.update(23.9) is None


def test_disabled_gate_is_inert():
    g = LoadGate({"enabled": False}, ncpu=1)
    assert g.admit(999.0, now=0) == (None, None)


def test_bad_config_values_fall_back_to_defaults():
    g = LoadGate({"worker_load_cost": "x", "ramp_seconds": -1,
                  "max_spawn_per_tick": 0}, ncpu=8)
    assert (g.worker_load_cost, g.ramp_seconds, g.max_spawn_per_tick) == (2.0, 600.0, 4)


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
        g.admit(20.0, now=0); g.record_spawns(4, now=0); g.log_tick(log, now=0)
        g.admit(20.0, now=10); g.record_spawns(2, now=10); g.log_tick(log, now=10)
        g.admit(40.0, now=20); g.record_spawns(0, now=20); g.log_tick(log, now=20)
        g.admit(40.0, now=30); g.log_tick(log, now=30)          # no change: silent
        g.admit(40.0, now=400); g.log_tick(log, now=400)        # 5-min summary
    msgs = [r.getMessage() for r in caplog.records]
    changes = [m for m in msgs if "->" in m]
    assert len(changes) == 2
    assert "start -> state=admitting" in changes[0]
    assert "admitting -> state=paused" in changes[1]
    summaries = [m for m in msgs if "summary" in m]
    assert len(summaries) == 1 and "admitted_5m=6" in summaries[0]
    assert "pending_ramp=" in summaries[0]


def test_state_file_round_trip_and_diagnostics_line(tmp_path):
    g = LoadGate({}, ncpu=32)
    g.admit(20.0, load5=18.0, now=0)
    g.record_spawns(4, now=0)
    path = tmp_path / "load_gate.json"
    g.write_state(path)
    state = klg.read_state(path)
    assert state["state"] == "admitting" and state["admitted_last_tick"] == 4
    line = klg.format_state_line(state)
    assert line.startswith("Load gate: admitting load1=20.0 load5=18.0")
    assert "allowance=4" in line
    assert "no state published" in klg.format_state_line(None)


def test_run_daemon_applies_gate(kanban_home, monkeypatch):
    import threading

    captured = []
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.append(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (146.0, 90.0))
    gate = LoadGate({}, ncpu=32)
    kbd.run_daemon(interval=0.01, stop_event=stop,
                  on_tick=lambda _res: stop.set(), load_gate=gate)
    assert captured[0]["spawn_limit"] == 0
    assert "load1=146.0" in captured[0]["spawn_paused"]
    assert gate.state == "paused"
    assert klg.read_state()["state"] == "paused"


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


# --- forecast-aware + CPU-corroborated admission (t_bf26e8f1) --------------


def _drive(gate, minutes, *, base=10.0, per_worker=2.0, ramp=600.0):
    """Unlimited backlog on a host where each worker's load arrives linearly
    over ``ramp`` s (measured 5-10 min on the Studio, 2026-09-29). Returns
    the peak load1 and the final worker count."""
    spawns, peak = [], 0.0
    for m in range(minutes):
        now = m * 60.0
        load = base + sum(per_worker * min(1.0, (now - t) / ramp) for t in spawns)
        peak = max(peak, load)
        allowance, _ = gate.admit(load, load5=load, now=now, running=len(spawns))
        spawns += [now] * (allowance or 0)
        gate.record_spawns(allowance or 0, now=now)
    return peak, len(spawns)


def test_forecast_plateaus_under_pause_above_with_lagging_load():
    peak, workers = _drive(LoadGate({"pause_above": 64, "resume_below": 48}, 32), 60)
    assert peak <= 64.0
    assert workers >= 20  # still fills the host, it does not just refuse


def test_short_ramp_mutant_overshoots():
    """The pre-fix ramp (120 s) forgets a spawn before its load lands."""
    peak, _ = _drive(LoadGate({"pause_above": 64, "resume_below": 48,
                               "ramp_seconds": 120}, 32), 60)
    assert peak > 64.0


def test_measured_slope_replaces_prior_and_prior_holds_without_signal():
    g = LoadGate({}, ncpu=32)
    for i in range(11):                       # 10 min, load = 3 + 1.0/worker
        g.admit(3.0 + 10 + i, now=i * 60.0, running=10 + i)
    assert g.cost_source == "measured" and abs(g.cost - 1.0) < 1e-6
    flat = LoadGate({}, ncpu=32)
    for i in range(11):                       # workers never move: no slope
        flat.admit(5.0 + i, now=i * 60.0, running=20)
    assert (flat.cost_source, flat.cost) == ("prior", 2.0)
    noise = LoadGate({}, ncpu=32)
    loads = [30, 5, 28, 6, 31, 4, 29, 7, 30, 5, 28]
    for i in range(11):                       # load unrelated to workers
        noise.admit(float(loads[i]), now=i * 60.0, running=10 + (i % 3))
    assert noise.cost_source == "prior"


def test_measured_slope_is_clamped():
    g = LoadGate({}, ncpu=32)
    for i in range(11):                       # load falls as workers rise
        g.admit(60.0 - 3 * i, now=i * 60.0, running=10 + i)
    assert g.cost == g.worker_load_cost_min
    g2 = LoadGate({}, ncpu=32)
    for i in range(11):
        g2.admit(10.0 + 20 * i, now=i * 60.0, running=10 + i)
    assert g2.cost == g2.worker_load_cost_max


def test_load1_pause_needs_cpu_corroboration():
    """2026-09-29 19:10: load1 250 with the CPU 64% idle paused the gate 2 h."""
    idle = LoadGate({"pause_above": 64, "resume_below": 48}, ncpu=32)
    a, reason = idle.admit(250.0, load5=200.0, now=0, cpu_busy=0.36)
    assert idle.state == "cpu_headroom" and reason is None and a == 4
    busy = LoadGate({"pause_above": 64, "resume_below": 48}, ncpu=32)
    a, reason = busy.admit(250.0, load5=200.0, now=0, cpu_busy=0.95)
    assert (a, busy.state) == (0, "paused") and "load1=250.0" in reason
    blind = LoadGate({"pause_above": 64, "resume_below": 48}, ncpu=32)
    assert blind.admit(250.0, load5=200.0, now=0)[0] == 0      # no CPU sample
    off = LoadGate({"pause_above": 64, "resume_below": 48,
                    "cpu_corroborate": False}, ncpu=32)
    assert off.admit(250.0, load5=200.0, now=0, cpu_busy=0.36)[0] == 0


def test_cpu_headroom_is_consumed_by_pending_workers():
    g = LoadGate({"pause_above": 64, "resume_below": 48,
                  "max_spawn_per_tick": 100}, ncpu=32)
    # (0.70 - 0.50) x 32 = 6.4 cores / 1.0 per worker -> 6
    assert g.admit(250.0, now=0, cpu_busy=0.50)[0] == 6
    g.record_spawns(6, now=0)
    a, reason = g.admit(250.0, now=30, cpu_busy=0.50)   # 5.7 cores pending
    assert a == 0 and "pending cpu" in reason and g.state == "cpu_headroom"


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
    """The finding's scenario end to end: idle 1-core host, prior cost 2.0,
    corrupt current board, healthy idle board -> the empty-host floor admits."""
    _corrupt_current_board_with_healthy_second(kanban_home, 0)
    g = LoadGate({}, ncpu=1)
    allowance, reason = g.admit(0.1, now=0.0, running=klg.count_running_workers())
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


# -- small hosts: floor() must not starve an empty host (Prism P1 c966b642) --
@pytest.mark.parametrize("ncpu,load1", [(1, 0.1), (2, 0.1), (2, 1.5)])
def test_empty_small_host_admits_one_worker(ncpu, load1):
    """1-2 core host, prior cost 2.0, headroom < cost: floor() alone gives 0."""
    g = LoadGate({}, ncpu=ncpu)
    allowance, reason = g.admit(load1, now=1000.0, running=0)
    assert (allowance, reason) == (1, None)
    assert g.state == "admitting"


def test_small_host_floor_still_stops_short_once_a_worker_exists():
    g = LoadGate({}, ncpu=2)
    # A worker is running: no guarantee, floor(1.9 / 2.0) == 0.
    assert g.admit(0.1, now=1000.0, running=1)[0] == 0
    # Nothing running but a spawn is still ramping: no second free worker.
    g2 = LoadGate({}, ncpu=2)
    g2.record_spawns(1, now=1000.0)
    assert g2.admit(0.1, now=1001.0, running=0)[0] == 0
    # Unknown running count: plain floor(), never a guessed guarantee.
    assert LoadGate({}, ncpu=2).admit(0.1, now=1000.0)[0] == 0


def test_empty_small_host_cpu_headroom_arm_admits_one():
    """Hard load1 pause, idle CPU, 1 core: (0.7 - busy) * 1 < 1 core."""
    g = LoadGate({}, ncpu=1)
    allowance, reason = g.admit(3.0, now=1000.0, running=0, cpu_busy=0.1)
    assert (allowance, reason, g.state) == (1, None, "cpu_headroom")
    # Busy CPU still pauses the empty host.
    g2 = LoadGate({}, ncpu=1)
    assert g2.admit(3.0, now=1000.0, running=0, cpu_busy=0.95)[0] == 0
