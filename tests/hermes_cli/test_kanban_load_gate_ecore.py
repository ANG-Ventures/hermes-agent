"""E-core band leg of the kanban load gate (t_a7978f86).

2026-10-18 12:3x on the Studio: load1 325, CPU 54% idle, P-cores 33% busy,
the 8 E-cores 100% busy. The cpu_headroom row read the 32-core mean (0.41-0.50
< cpu_busy_pause) and kept admitting local spawns while every Background-QoS
service starved (dpx /health 8 s -> false autodeploy HALT).
"""

from __future__ import annotations

import collections
import logging
import sys
import types

from hermes_cli import kanban_load_gate as klg
from hermes_cli.kanban_load_gate import LoadGate

BASE = {"pause_above": 64, "resume_below": 48}


def _gate(**kw):
    return LoadGate(dict(BASE, **kw), ncpu=32)


def test_report_is_the_default_and_changes_nothing():
    g = _gate()
    assert g.ecore_gate == "report"
    a, reason = g.admit(325.0, load5=300.0, now=0, cpu_busy=0.46, ecore_busy=1.0)
    plain = _gate(ecore_gate="off")
    b, _ = plain.admit(325.0, load5=300.0, now=0, cpu_busy=0.46, ecore_busy=1.0)
    assert (a, reason, g.state) == (b, None, "cpu_headroom") and a > 0
    assert g.ecore_would_refuse == a and plain.ecore_would_refuse == 0


def test_on_refuses_local_spawns_when_the_ecore_band_is_saturated():
    g = _gate(ecore_gate="on")
    a, reason = g.admit(325.0, load5=300.0, now=0, cpu_busy=0.46, ecore_busy=1.0)
    assert a == 0 and g.state == "cpu_headroom"
    assert "ecore_busy=1.00 >= ecore_busy_pause=0.95" in reason
    assert g.snapshot()["ecore_would_refuse"] > 0


def test_on_admits_below_the_bar_or_without_a_sample():
    below = _gate(ecore_gate="on")
    a, reason = below.admit(325.0, load5=300.0, now=0, cpu_busy=0.46, ecore_busy=0.80)
    blind = _gate(ecore_gate="on")
    b, _ = blind.admit(325.0, load5=300.0, now=0, cpu_busy=0.46)   # Linux / no perflevels
    assert a > 0 and reason is None and b == a


def test_on_also_caps_the_spill_band_cpu_allowance():
    keys = {"spill_fraction": 0.5, "cpu_spill": 0.75, "cpu_spill_resume": 0.6, "cpu_pause": 0.9}
    ref = _gate(ecore_gate="off", **keys)
    # cpu 0.80 >= cpu_spill opens the band; load leaves 4, cpu (0.9-0.8)*32 leaves 3
    assert ref.admit(20.0, load5=20.0, now=0, cpu_busy=0.80, ecore_busy=0.99)[0] == 3
    g = _gate(ecore_gate="on", **keys)
    a, reason = g.admit(20.0, load5=20.0, now=0, cpu_busy=0.80, ecore_busy=0.99)
    assert g.band == "spilling" and a == 0 and "ecore_busy=0.99" in reason


def test_dry_line_is_logged_rate_limited(caplog):
    g = _gate()
    with caplog.at_level(logging.WARNING):
        for t in (0, 60, 400):
            g.admit(325.0, load5=300.0, now=t, cpu_busy=0.46, ecore_busy=1.0)
            g.log_tick(logging.getLogger("t"), now=t)
    lines = [r.getMessage() for r in caplog.records if "ecore_busy" in r.getMessage()]
    assert len(lines) == 2 and all("(dry)" in m and "would-refuse" in m for m in lines)


def test_invalid_mode_and_bar_fall_back():
    g = _gate(ecore_gate="bogus", ecore_busy_pause=7)
    assert (g.ecore_gate, g.ecore_busy_pause) == ("report", 0.95)


def test_ecore_indices_m3_ultra_and_plain_hosts():
    def sysctl(table):
        return lambda name: table[name]

    def mac(brand, e_n):
        return sysctl({"hw.perflevel1.logicalcpu": e_n, "hw.packages": 1,
                       "machdep.cpu.brand_string": brand})

    assert klg.ecore_cpu_indices(mac("Apple M3 Ultra", 8), ncpu=32) == (0, 1, 2, 3, 16, 17, 18, 19)
    # Prism 400ac2841de6: an Ultra under 32 cores is still two dies
    assert klg.ecore_cpu_indices(mac("Apple M2 Ultra", 8), ncpu=24) == (0, 1, 2, 3, 12, 13, 14, 15)
    assert klg.ecore_cpu_indices(mac("Apple M1 Ultra", 4), ncpu=20) == (0, 1, 10, 11)
    # a 32-thread non-Ultra stays one die
    assert klg.ecore_cpu_indices(mac("Apple M4 Max", 4), ncpu=16) == (0, 1, 2, 3)
    assert klg.ecore_cpu_indices(mac("Apple M2 Pro", 4), ncpu=12) == (0, 1, 2, 3)
    assert klg.ecore_cpu_indices(mac("Apple M2 Pro", 0), ncpu=16) == ()


def test_sample_ecore_busy_reads_only_the_ecore_deltas(monkeypatch):
    T = collections.namedtuple("T", "user system idle")
    snaps = iter([
        [T(0, 0, 0), T(0, 0, 0), T(0, 0, 0)],
        [T(10, 0, 0), T(0, 0, 10), T(5, 0, 5)],
    ])
    fake = types.SimpleNamespace(cpu_times=lambda percpu=False: next(snaps))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    busy, prev = klg.sample_ecore_busy((0,), None)
    assert busy is None and prev is not None
    busy, _ = klg.sample_ecore_busy((0, 2), prev)
    assert abs(busy - 0.75) < 1e-9          # cpu0 100% + cpu2 50%; idle cpu1 ignored
    assert klg.sample_ecore_busy((), None) == (None, None)


def test_yaml_bare_on_off_bools_map_to_modes():
    """Prism 3d10a4bc87be: YAML 1.1 parses a bare `on`/`off` as True/False; they must not fall back to report."""
    from hermes_cli import kanban_load_gate as klg
    for raw, want in ((True, "on"), (False, "off"), ("on", "on"), ("OFF", "off"), ("bogus", "report")):
        g = klg.LoadGate({"enabled": True, "ecore_gate": raw}, 32)
        assert g.ecore_gate == want, (raw, g.ecore_gate)


def test_one_shot_dispatch_admission_samples_the_ecores(monkeypatch):
    """Prism 3b7cbf596c2e: `hermes kanban dispatch` (one-shot admission) feeds the E-core sample to admit(), so
    ecore_gate=on refuses there too instead of seeing no sample."""
    import sqlite3, types
    from hermes_cli import kanban_load_gate as klg
    from hermes_cli import kanban_ops
    seen = {}
    real_admit = klg.LoadGate.admit

    def spy(self, *a, **kw):
        seen["ecore_busy"] = kw.get("ecore_busy")
        return real_admit(self, *a, **kw)

    monkeypatch.setattr(klg.LoadGate, "admit", spy)
    monkeypatch.setattr(klg, "ecore_cpu_indices", lambda: (0, 1))
    monkeypatch.setattr(klg, "sample_ecore_busy", lambda cpus, prev=None, block=0.0: (0.99, None))
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (1.0, 1.0))
    monkeypatch.setattr(klg, "sample_cpu_busy", lambda prev=None, block=0.0: (0.10, None))
    monkeypatch.setattr(klg, "sample_user_procs", lambda: (None, None))
    monkeypatch.setattr(klg, "count_running_workers", lambda: 0)
    conn = sqlite3.connect(":memory:")
    conn.execute("create table task_runs (started_at integer)")
    cfg = {"kanban": {"dispatch_load_gate": {"enabled": True, "ecore_gate": "on"}}}
    out = kanban_ops._one_shot_load_gate(conn, types.SimpleNamespace(json=True), cfg, None)
    assert out.get("gate") is not None and out["gate"].enabled, out
    assert seen.get("ecore_busy") == 0.99, seen
