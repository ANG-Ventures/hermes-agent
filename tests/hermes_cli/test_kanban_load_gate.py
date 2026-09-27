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
    monkeypatch.setattr(kb, "_system_memory_sample", lambda: {}, raising=False)
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
        res = kb.dispatch_once(
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
    # load1 unchanged (lag) but 4 x 2.0 pending: ceil((32-20-8)/2) = 2
    assert len(_tick(gate, 20.0, 30.0).spawned) == 2
    # 6 x 2.0 = 12 pending: headroom 0 -> saturated, nothing spawned
    res = _tick(gate, 20.0, 60.0)
    assert res.spawned == [] and "pending_ramp=12.0" in (res.spawn_paused or "")
    assert gate.state == "saturated"
    # ramp window (120 s) expires -> admits again, still capped
    assert len(_tick(gate, 20.0, 200.0).spawned) == 4


# ── pure gate arithmetic ────────────────────────────────────────────────────

def test_allowance_formula_and_caps():
    g = LoadGate({"max_spawn_per_tick": 100}, ncpu=32)
    assert g.admit(20.0, now=0) == (6, None)                 # ceil(12/2)
    assert g.admit(31.0, now=0) == (1, None)                 # ceil(0.5)
    a, reason = g.admit(32.0, now=0)
    assert a == 0 and reason.startswith("projected")
    g.record_spawns(3, now=0)
    assert g.pending(now=10) == 6.0
    assert g.admit(20.0, now=10) == (3, None)                # ceil((12-6)/2)
    assert g.pending(now=121) == 0.0                         # window expired
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
    assert (g.worker_load_cost, g.ramp_seconds, g.max_spawn_per_tick) == (2.0, 120.0, 4)


def test_dispatch_once_spawn_limit_intersects_budget(kanban_home, monkeypatch):
    _board_with_ready(kanban_home, monkeypatch, 5)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, spawn_limit=2)
    assert len(res.spawned) == 2
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, spawn_limit=0)
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

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (146.0, 90.0))
    gate = LoadGate({}, ncpu=32)
    kb.run_daemon(interval=0.01, stop_event=stop,
                  on_tick=lambda _res: stop.set(), load_gate=gate)
    assert captured[0]["spawn_limit"] == 0
    assert "load1=146.0" in captured[0]["spawn_paused"]
    assert gate.state == "paused"
    assert klg.read_state()["state"] == "paused"
