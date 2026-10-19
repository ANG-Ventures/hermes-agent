"""kanban.admission: utilization on the Studio LoadGate + `kanban dispatch` (t_da6f84f7; ruling t_0b33557c).

The readback "the Studio admitting on utilization with the interim 80/90 keys inert": a load1 far over
pause_above with the CPU 45 % busy and memory free admits under utilization and pauses under legacy; the
host table's kanban ceiling (2, `_ceiling_reason`) still caps running workers; a spawn inside the settle window
is a lease the next admit sees; the procs pause still wins.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import admission as ad
from hermes_cli import kanban_load_gate as klg

INTERIM = {"pause_above": 64, "resume_below": 48, "cpu_busy_pause": 0.80, "cpu_spill": 0.90, "cpu_pause": 0.90,
           "cpu_spill_resume": 0.85, "spill_fraction": 1.0, "worker_cpu_cost": 1.0, "max_spawn_per_tick": 4}


@pytest.fixture
def ctx(monkeypatch):
    c = {"host": "mac-studio", "ceiling": 2, "ceiling_reason": "gateway shares the box",
         "need": ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior"),
         "limits": ad.Limits(mem_reserve_gb=128.0), "mem_available_gb": 360.0, "mem_total_gb": 512.0}
    monkeypatch.setattr(klg, "utilization_context", lambda: dict(c))
    # hermetic PSI: the Linux CI host's own /proc/pressure must not decide these tests
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0, "psi_io_some10": 0.0})
    return c


def _gate(mode):
    g = klg.LoadGate(dict(INTERIM), 32)
    g.admission = mode
    return g


def test_utilization_admits_where_legacy_pauses(ctx):
    legacy = _gate("legacy")
    a, why = legacy.admit(190.0, load5=185.0, now=1000.0, running=0, cpu_busy=0.95)
    assert a == 0 and legacy.state == "paused"
    util = _gate("utilization")
    a, why = util.admit(190.0, load5=185.0, now=1000.0, running=0, cpu_busy=0.45)
    assert (a, why) == (2, None), why                      # the ceiling of 2, not pause_above 64
    snap = util.snapshot()
    assert snap["admission"] == "utilization" and snap["util"]["ceiling"] == 2


def test_utilization_refuses_on_cpu_busy_not_load1(ctx):
    g = _gate("utilization")
    a, why = g.admit(2.0, now=1000.0, running=0, cpu_busy=0.97)
    assert a == 0 and "saturated:cpu" in why and g.band == "spilling"


def test_ceiling_counts_running_workers(ctx):
    g = _gate("utilization")
    a, why = g.admit(10.0, now=1000.0, running=2, cpu_busy=0.10)
    assert a == 0 and why.startswith("ceiling: 2 running of kanban ceiling 2 (gateway shares the box)")


def test_spawn_inside_settle_window_is_a_lease(ctx):
    ctx["ceiling"] = None
    ctx["mem_available_gb"] = 128.0 + 3.0 * 3            # room for exactly 3 workers of 3 GB
    g = _gate("utilization")
    a, _ = g.admit(10.0, now=1000.0, running=0, cpu_busy=0.10)
    assert a == 3
    g.record_spawns(3, now=1000.0)
    a, why = g.admit(10.0, now=1030.0, running=0, cpu_busy=0.10)
    assert a == 0 and "mem_gb" in why                     # the 3 leases still count 30 s later
    a, _ = g.admit(10.0, now=1000.0 + ad.DEFAULT_SETTLE_S + 1, running=3, cpu_busy=0.10)
    assert a == 3                                         # settled: the sample is assumed to carry them now


def test_proc_pause_still_wins(ctx):
    g = _gate("utilization")
    a, why = g.admit(1.0, now=1000.0, running=0, cpu_busy=0.05, procs=9000, proc_limit=10000)
    assert a == 0 and g.state == "proc_paused" and not g.remote_allowed


def test_unmeasured_cpu_refuses(ctx):
    g = _gate("utilization")
    a, why = g.admit(1.0, now=1000.0, running=0, cpu_busy=None)
    assert a == 0 and "unmeasured:cpu_busy" in why


def test_ceiling_with_unknown_running_fails_closed(ctx):
    """Prism 710b1c8865a5: a ceiling on running workers with the count unknown admits nothing."""
    g = _gate("utilization")
    a, why = g.admit(1.0, now=1000.0, running=None, cpu_busy=0.05)
    assert a == 0 and why.startswith("ceiling: running count unknown")
    ctx["ceiling"] = None
    a, _ = g.admit(1.0, now=1000.0, running=None, cpu_busy=0.05)
    assert a > 0                                          # no ceiling: nothing to count against


def test_linux_sample_carries_psi_and_refuses_on_it(ctx, monkeypatch):
    """Prism 8952c3f1bbe9 / 616c5f0203bd: on Linux the PSI axes are sampled, so an idle host admits and a
    memory-stalled one refuses (never unmeasured:psi_* on every tick)."""
    monkeypatch.setattr(klg, "_os_name", lambda: "linux")
    g = _gate("utilization")
    a, why = g.admit(1.0, now=1000.0, running=0, cpu_busy=0.05)
    assert a == 2, why
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 25.0, "psi_io_some10": 0.0})
    a, why = g.admit(1.0, now=1000.0, running=0, cpu_busy=0.05)
    assert a == 0 and "saturated:psi_mem_some10" in why


def test_sample_psi_reads_proc_pressure(tmp_path):
    (tmp_path / "cpu").write_text("some avg10=3.10 avg60=2.00 avg300=1.00 total=1\n", encoding="utf-8")
    (tmp_path / "memory").write_text("some avg10=21.50 avg60=0 avg300=0 total=1\nfull avg10=9.0 avg60=0 avg300=0 total=1\n",
                                     encoding="utf-8")
    assert klg.sample_psi(str(tmp_path)) == {"psi_cpu_some10": 3.1, "psi_mem_some10": 21.5}   # io absent = unmeasured


def test_slice_memory_is_a_second_memory_axis(ctx, monkeypatch):
    """Prism 616c5f0203bd: host memory plentiful, kanbanw.slice full -> refused on slice_mem_gb."""
    monkeypatch.setattr(ad, "read_slice_memory",
                        lambda **k: ad.SliceMem(name="kanbanw.slice", limit_gb=24.0, current_gb=23.0,
                                                classes=k.get("classes", ("kanban",))))
    g = _gate("utilization")
    a, why = g.admit(1.0, now=1000.0, running=0, cpu_busy=0.05)
    assert a == 0 and "slice_mem_gb" in why


def test_gate_from_config_reads_the_kill_switch():
    assert klg.gate_from_config({"kanban": {"admission": "utilization"}}).admission == "utilization"
    assert klg.gate_from_config({"kanban": {"admission": "bogus"}}).admission == "legacy"
    assert klg.gate_from_config({"kanban": {}}).admission == "legacy"


def test_vendored_admission_is_the_canonical_bytes():
    import os
    from pathlib import Path

    env = os.environ.get("ADMISSION_CANONICAL")
    if not env:
        pytest.skip("ADMISSION_CANONICAL not set (no hermes-home checkout to cross-read)")
    assert Path(ad.__file__).read_bytes() == Path(env).read_bytes()
