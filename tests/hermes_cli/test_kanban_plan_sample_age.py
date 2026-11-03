"""t_1e097d62: unified admission refused every pool host `closed:stale` while var/fleet-pressure.json was fresh.

_class_verdict hands may_start the PLAN's own probe sample (stamped when plan_pool ran). The dispatcher then takes
cards on that plan for seconds to minutes; on the Studio 2026-11-02 the gap from plan to the [default] dispatch line
was p50 50 s, p90 119 s, max 278 s (36% over the 60 s stale_after_s), and 270-400 placement_wait rows an hour read
`closed:stale` on all three hosts at once. A plan sample that aged out is not proof the host is stale: the collector's
fleet-pressure.json (re-written every sampler period) is the fresher evidence, and may_start reads it when no sample
is supplied. The freshness bound is unchanged: a stale collector row still refuses.
"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_headroom as kh

from tests.hermes_cli.test_kanban_unified_admit import Fleet, _plan, home  # noqa: F401  (fixture)


def _collector(home, t, age, hosts=("ace-ai",)):
    """A var/fleet-pressure.json shaped like placement-collector's (field set copied from the Studio's 2026-11-03)."""
    (home / "var").mkdir(exist_ok=True)
    rows = {h: {"at": t - age, "age_s": age, "status": "fresh", "cpu_busy": 0.30, "mem_available_gb": 75.0,
                "mem_total_gb": 121.0, "ram_free_gb": 75.0, "ncpu": 32, "ncores": 16, "load1": 1.0, "load5": 1.0,
                "load_ratio": 0.1, "procs_running": 2, "d_threads": 0, "psi_cpu_some10": 1.0, "psi_cpu_some60": 1.0,
                "psi_mem_some10": 0.0, "psi_io_some10": 0.5, "psi_io_full10": 0.0, "system_pct": 3.0,
                "scratch_free_gb": 500.0, "swap_total_gb": 8.0, "swap_used_gb": 0.0, "slo_breach": False,
                "os": "linux", "host": h}
            for h in hosts}
    (home / "var" / "fleet-pressure.json").write_text(json.dumps({"schema": 1, "at": t, "stale_after_s": 120,
                                                                  "hosts": rows}), encoding="utf-8")


def test_aged_plan_sample_reads_the_fresh_collector_not_closed_stale(home):  # noqa: F811
    t0 = time.time()
    plan = _plan(home, Fleet(t0))              # probe sample 5 s old at plan time
    later = t0 + 150                           # the take runs 150 s after the plan (> stale_after_s 120)
    plan.signal.clock = lambda: later
    _collector(home, later, age=10)            # the collector re-sampled ace-ai 10 s ago
    keep, held = kh.unified_filter(plan, ["ace-ai"], None)
    assert keep == ["ace-ai"] and held == {}, held


def test_aged_plan_sample_with_a_stale_collector_still_refuses(home):  # noqa: F811
    t0 = time.time()
    plan = _plan(home, Fleet(t0))
    later = t0 + 150
    plan.signal.clock = lambda: later
    _collector(home, later, age=200)           # the collector is stale too: the bound still holds
    keep, held = kh.unified_filter(plan, ["ace-ai"], None)
    assert keep == [] and "stale" in held["ace-ai"], held


def test_aged_plan_sample_with_no_collector_file_still_refuses(home):  # noqa: F811
    t0 = time.time()
    plan = _plan(home, Fleet(t0))
    plan.signal.clock = lambda: t0 + 150
    keep, held = kh.unified_filter(plan, ["ace-ai"], None)
    assert keep == [] and held["ace-ai"].startswith("closed:"), held


def test_fresh_plan_sample_is_still_the_one_used(home):  # noqa: F811
    t0 = time.time()
    plan = _plan(home, Fleet(t0))
    _collector(home, t0, age=500)              # a stale collector must not override a fresh plan sample
    keep, held = kh.unified_filter(plan, ["ace-ai"], None)
    assert keep == ["ace-ai"] and held == {}, held
