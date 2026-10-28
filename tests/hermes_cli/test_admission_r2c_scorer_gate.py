"""R2c admission hunks apply under scorer v2 ONLY (Prism 78da7ab3e3d9 on hermes-agent#2339, t_80e4c88c).

#2339 made admission.admit() read psi_cpu_some60 and close PSI cpu AT its line for every caller, so the scorer v1
kill switch no longer rolled back: a v1 non-saturable host was refused at avg60 >= 25 and at avg10 == 40, and the
avg60 axis moved v1 scores. ``Limits.r2c`` (set by placement._limits only for scorer v2) gates both.
"""
from __future__ import annotations

import pytest

from hermes_cli.fleet_placement import admission, placement

NOW = 10_000.0
FP = placement.Footprint(ram_gb=3, threads=1)


def _spec(scorer):
    return placement.HostSpec(name="h", cores=16, threads=32, mem_total_gb=121, mem_reserve_gb=0, cores_reserve=0,
                              ceilings={"kanban": 4}, aliases=(), scorer=scorer)


def _head(scorer, p10, p60):
    s = placement.Sample(mem_available_gb=80.0, load1=4, load5=4, psi_cpu_some10=p10, psi_cpu_some60=p60,
                         psi_mem_some10=0.0, psi_io_some10=0.0, system_pct=1.0, at=NOW, cpu_busy=0.1, ncpu=32,
                         os="linux")
    return placement.headroom(_spec(scorer), s, FP, consumer="kanban", mode="utilization", now=NOW, leases=[],
                              closed_when={"psi_cpu_some10": 40.0})


def test_v1_ignores_avg60():
    h = _head("v1", 1.0, 30.0)
    assert h.eligible, h.reason
    assert h.binding == "mem_gb" and h.score == pytest.approx(_head("v1", 1.0, None).score)


def test_v1_avg60_does_not_move_the_score():
    assert _head("v1", 1.0, 20.0).score == _head("v1", 1.0, None).score
    assert _head("v1", 1.0, 20.0).binding != "psi_cpu_some60"


def test_v1_admits_at_exactly_avg10_40():
    assert _head("v1", 40.0, 1.0).eligible           # pre-R2c: free == 0 is room


def test_v2_keeps_the_r2c_closures():
    assert not _head("v2", 1.0, 30.0).eligible
    assert "psi_cpu_some60" in _head("v2", 1.0, 30.0).reason
    assert not _head("v2", 40.0, 1.0).eligible
    assert _head("v2", 1.0, 20.0).binding == "psi_cpu_some60"


def test_limits_default_is_the_pre_r2c_predicate():
    row = {"host": "h", "at": NOW, "os": "linux", "ncpu": 32, "cpu_busy": 0.1, "mem_available_gb": 80.0,
           "mem_total_gb": 121.0, "psi_cpu_some10": 40.0, "psi_cpu_some60": 30.0, "psi_mem_some10": 0.0,
           "psi_io_some10": 0.0}
    need = admission.Estimate(cpus=1.0, ram_gb=3.0)
    assert admission.admit(row, need).ok                                   # kanban_load_gate / runner allocator
    assert "psi_cpu_some60" not in admission.admit(row, need).free
    v = admission.admit(row, need, limits=admission.Limits(r2c=True))
    assert not v.ok and v.binding == "psi_cpu_some10"
