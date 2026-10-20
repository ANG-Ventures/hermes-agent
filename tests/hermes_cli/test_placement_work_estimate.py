"""Vendored placement + work_estimate (t_82e15796; hermes-home#5688, t_9e395789).

Under ``mode="utilization"`` a kanban job's estimate is keyed per profile (``kanban-worker-llm-wait:<profile>``) and
is work_estimate's p75 of the last runs of that class, not admission's removed all-history p90.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import admission, placement, work_estimate

NOW = 10_000.0
DOC = {
    "schema": 1, "default_first": "ci-box", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 1, "scratch_gb": 0, "gpu_mb": 0}},
    "hosts": {
        "ci-box": {"cores": 128, "threads": 256, "mem_total_gb": 220, "mem_reserve_gb": 26, "cores_reserve": 0,
                   "cpu_saturable": True, "consumers": {"kanban": {"slots": 24}}},
    },
}


@pytest.fixture
def table(tmp_path):
    p = tmp_path / "fleet-hosts.json"
    p.write_text(json.dumps(DOC), encoding="utf-8")
    return placement.load_hosts(p)


def _sample():
    return placement.Sample(mem_available_gb=150.0, load1=10.0, load5=10.0, psi_cpu_some10=0.0, psi_mem_some10=0.0,
                            psi_io_some10=0.0, system_pct=1.0, at=NOW, cpu_busy=0.1, ncpu=256, os="linux")


def _choose(table, tmp_path, history):
    return placement.choose(table, "kanban", {"ci-box": _sample()}, candidates=["ci-box"], now=NOW,
                            exclusions_path=tmp_path / "none.json", mode="utilization",
                            job=placement.Job(profile="x"), history=history, leases=[])


def test_placement_imports_the_vendored_work_estimate():
    assert placement.work_estimate is work_estimate
    assert not hasattr(admission, "estimate")          # the p90 estimator is gone; one estimator only


def test_utilization_need_is_keyed_on_the_profile_class(table, tmp_path):
    hist = [{"class": "kanban-worker-llm-wait:x", "host": "ci-box", "cpus": float(i), "ram_gb": 2.0 * i,
             "gpu_mib": 0.0, "at": NOW - 100 + i} for i in range(1, 9)]
    d = _choose(table, tmp_path, hist)
    need = d.rows[0].need
    assert need is not None
    assert need.source == "p75:kanban-worker-llm-wait:x@ci-box n=8"
    assert need.cpus == 6.0 and need.ram_gb == 12.0      # nearest-rank p75 of 1..8


def test_utilization_need_falls_back_to_the_seed_without_history(table, tmp_path):
    d = _choose(table, tmp_path, [])
    need = d.rows[0].need
    assert need.source == "seed" and (need.cpus, need.ram_gb) == (1.0, 3.0)
