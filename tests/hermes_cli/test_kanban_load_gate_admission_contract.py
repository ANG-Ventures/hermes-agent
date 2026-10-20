"""kanban_load_gate against the REAL vendored admission + work_estimate modules (t_865feb4e).

af0437b0a63 re-vendored admission.py without ``estimate()`` (hermes-home#5688 moved the estimator to
work_estimate.py). test_kanban_load_gate_utilization.py stubs ``utilization_context`` wholesale, so the
stale ``_ad.estimate`` call in it went unseen until ``kanban dispatch --dry-run`` crashed on the Studio.
These tests drive utilization_context() and the utilization admit path with nothing in admission mocked.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import admission, work_estimate
from hermes_cli import kanban_load_gate as klg

TABLE = {
    "schema": 1, "default_first": "studio", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 1, "scratch_gb": 0, "gpu_mb": 0}},
    "hosts": {
        "studio": {"aliases": ["local"], "cores": 24, "threads": 32, "mem_total_gb": 512, "mem_reserve_gb": 128, "cores_reserve": 0,
                   "consumers": {"kanban": {"slots": 2, "_ceiling_reason": "gateway shares the box"}}},
    },
}


@pytest.fixture
def placement_home(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    monkeypatch.setenv("PLACEMENT_HOME", str(tmp_path))
    return tmp_path


def test_every_module_attribute_the_gate_uses_exists():
    assert klg.missing_contract() == []


def test_contract_names_a_dropped_symbol(monkeypatch):
    monkeypatch.delattr(admission, "read_history")
    monkeypatch.delattr(work_estimate, "estimate")
    assert klg.missing_contract() == ["admission.read_history", "work_estimate.estimate"]


def test_utilization_context_runs_on_the_real_modules(placement_home):
    ctx = klg.utilization_context()
    assert isinstance(ctx["need"], admission.Estimate)
    assert isinstance(ctx["limits"], admission.Limits)
    assert ctx["need"].ram_gb > 0 and ctx["need"].source


def test_utilization_context_learns_from_the_class_usage_ledger(placement_home):
    import time

    now = time.time()
    led = placement_home / work_estimate.CLASS_USAGE_REL
    led.parent.mkdir(parents=True)
    for i in range(1, 9):
        admission.record_actual(led, job_class=klg.UTIL_CLASS, host="any-host", cpus=float(i), ram_gb=2.0 * i,
                                at=now - 100 + i)
    need = klg.utilization_context()["need"]
    assert need.source.startswith(f"p75:{klg.UTIL_CLASS}@")
    assert need.ram_gb == 12.0


def test_utilization_admit_path_runs_on_the_real_modules(placement_home, monkeypatch):
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0,
                                                   "psi_io_some10": 0.0})
    g = klg.LoadGate({"max_spawn_per_tick": 4}, 32)
    g.admission = "utilization"
    allowance, why = g.admit(1.0, now=1000.0, running=0, cpu_busy=0.05)
    assert isinstance(allowance, int) and allowance >= 0
    assert g.util and g.util["need"]["source"]
