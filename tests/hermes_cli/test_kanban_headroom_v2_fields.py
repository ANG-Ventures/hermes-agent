"""kanban_headroom probe adapters forward scorer v2 inputs (t_f8a3b6c3; Prism fd116a11e14a on hermes-agent#2104).

``sample_fields()`` + ``sample_from_detail()`` build the Sample the dispatcher's in-process path scores. They must
carry d_threads, runnable_ewma, procs_running and psi_cpu_some60, or a v2 scorer gates on uncorrected load and
never applies the PSI avg60 closure.
"""

from __future__ import annotations

import json

from hermes_cli import kanban_headroom as kh
from hermes_cli.fleet_placement import placement

T0 = 1_792_400_000.0


def _detail(**extra):
    doc = {"at": T0 - 5, "mem_available_gb": 80.0, "psi_cpu_some10": 1.0, "psi_mem_some10": 0.0, "system_pct": 2.0}
    doc.update(extra)
    d = kh.sample_fields(30.0, T0, json.dumps(doc), stale_after_s=60.0, local_now=T0)
    d["load1"] = 30.0
    return d


def test_probe_carries_the_v2_fields_into_the_sample():
    s = kh.sample_from_detail(_detail(d_threads=20, runnable_ewma=6.5, procs_running=9, psi_cpu_some60=30.0))
    assert (s.d_threads, s.runnable_ewma, s.procs_running, s.psi_cpu_some60) == (20, 6.5, 9, 30.0)
    assert s.occupancy == (6.5, "runnable_ewma")
    assert s.load == 10.0          # load1 30 net of 20 D-state threads


def test_probe_without_the_fields_is_unmeasured_not_zero():
    s = kh.sample_from_detail(_detail())
    assert (s.d_threads, s.runnable_ewma, s.procs_running, s.psi_cpu_some60) == (None, None, None, None)
    assert s.occupancy == (30.0, "load")


def test_v2_scorer_closes_on_psi_avg60_from_the_probe():
    spec = placement.HostSpec(name="h", cores=16, threads=32, mem_total_gb=121, mem_reserve_gb=0, cores_reserve=0,
                              ceilings={"kanban": 4}, aliases=(), scorer="v2", mt_yield=1.3)
    fp = placement.Footprint(ram_gb=3, threads=2)
    hot = kh.sample_from_detail(_detail(procs_running=5, psi_cpu_some60=30.0))
    h = placement.headroom(spec, hot, fp, consumer="kanban", closed_when={"psi_cpu_some10": 40.0})
    assert not h.eligible and h.reason == "closed:psi_cpu_some60 30 >= 25"
    ok = kh.sample_from_detail(_detail(procs_running=5, d_threads=25))
    h = placement.headroom(spec, ok, fp, consumer="kanban", closed_when={"psi_cpu_some10": 40.0})
    assert h.eligible and h.occupancy == 4.0      # procs_running - 1, not the raw load 30


def test_probe_carries_the_power_block_to_an_ac_only_host():
    """Prism 24ab4994fb91: sample_fields()/sample_from_detail() dropped the pressure document's ``power`` block, so a
    ``power_policy: ac_only`` host read ``closed:power unmeasured`` on every pick, AC-powered and awake or not."""
    spec = placement.HostSpec(name="mbp", cores=12, threads=18, mem_total_gb=48, mem_reserve_gb=0, cores_reserve=0,
                              ceilings={"kanban": 4}, aliases=(), power_policy="ac_only")
    awake = kh.sample_from_detail(_detail(power={"source": "ac", "sleep_disabled": True}))
    assert (awake.power_source, awake.sleep_disabled) == ("ac", True)
    assert placement.power_refusal(spec, awake) is None
    battery = kh.sample_from_detail(_detail(power={"source": "battery", "sleep_disabled": True}))
    assert placement.power_refusal(spec, battery) == "closed:power battery (power_policy ac_only)"
    for bad in (None, "ac", {"source": "mains", "sleep_disabled": 1}):     # absent / malformed = unmeasured, closed
        s = kh.sample_from_detail(_detail(**({} if bad is None else {"power": bad})))
        assert (s.power_source, s.sleep_disabled) == (None, None)
        assert placement.power_refusal(spec, s) == "closed:power unmeasured (power_policy ac_only)"
