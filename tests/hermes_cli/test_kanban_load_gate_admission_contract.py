"""kanban_load_gate against the REAL vendored admission + work_estimate modules (t_865feb4e).

af0437b0a63 re-vendored admission.py without ``estimate()`` (hermes-home#5688 moved the estimator to
work_estimate.py). test_kanban_load_gate_utilization.py stubs ``utilization_context`` wholesale, so the
stale ``_ad.estimate`` call in it went unseen until ``kanban dispatch --dry-run`` crashed on the Studio.
These tests drive utilization_context() and the utilization admit path with nothing in admission mocked.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.fleet_placement import admission, work_estimate
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
    monkeypatch.setenv("ALLOCATION_HOME", str(tmp_path))
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
    for i in range(1, 11):    # work_estimate MIN_ROWS 10, p90 (hermes-home t_d0c8e8f0)
        admission.record_actual(led, job_class=klg.UTIL_CLASS, host="any-host", cpus=float(i), ram_gb=2.0 * i,
                                at=now - 100 + i)
    need = klg.utilization_context()["need"]
    assert need.source.startswith(f"p90:{klg.UTIL_CLASS}@")
    assert need.ram_gb == 18.0


def test_local_utilization_context_enforces_fleet_cap(placement_home):
    path = placement_home / "config" / "fleet-hosts.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    table["hosts"]["studio"]["fleet_cap"] = {"cpus": 8}
    path.write_text(json.dumps(table), encoding="utf-8")

    ctx = klg.utilization_context()
    assert ctx["host"] == "studio"  # resolved through the local alias
    sample = {"host": ctx["host"], "os": "darwin", "at": 1000.0,
              "ncpu": 32, "cpu_busy": 0.05, "mem_available_gb": 400,
              "mem_total_gb": 512, "fleet_cpus": 8.0}
    at_cap = admission.admit(sample, ctx["need"], [], host=ctx["host"], limits=ctx["limits"])
    assert not at_cap.ok
    assert "fleet_cap:cpus" in at_cap.reason

    sample["fleet_cpus"] = 0.0
    below_cap = admission.admit(sample, ctx["need"], [], host=ctx["host"], limits=ctx["limits"])
    assert below_cap.ok, below_cap.reason


@pytest.mark.parametrize("os_name", ["darwin", "linux"])
@pytest.mark.parametrize("cap,fleet,mem_total,age,allowed,reason", [
    ({"cpus": 8}, {"cpus": 2, "ram_gb": 3}, 512, 10, 1, None),
    ({"cpus": 8}, {"cpus": 8, "ram_gb": 3}, 512, 10, 0, "fleet_cap:cpus"),
    ({"cpus": 8}, None, 512, 10, 0, "unmeasured:fleet_cap_cpus"),
    ({"cpus": 8}, {"cpus": "2"}, 512, 10, 0, "unmeasured:fleet_cap_cpus"),
    ({"cpus": 8}, {"cpus": 2}, 512, 121, 0, "unmeasured:fleet_cap_cpus"),
    ({"ram_gb": "10%"}, {"ram_gb": 23}, 256, 10, 0, "fleet_cap:ram_gb"),
    ({"ram_gb": "10%"}, {"ram_gb": 23}, None, 10, 0, "unmeasured:fleet_cap_ram_gb"),
], ids=["below", "at", "absent", "malformed", "stale", "percent-ram", "missing-ram-total"])
def test_local_gate_reads_fleet_cap_measurements(
    placement_home, monkeypatch, os_name, cap, fleet, mem_total, age, allowed, reason,
):
    from types import SimpleNamespace
    import psutil
    from hermes_cli import kanban_db, kanban_headroom

    path = placement_home / "config" / "fleet-hosts.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    table["hosts"]["studio"]["fleet_cap"] = cap
    path.write_text(json.dumps(table), encoding="utf-8")
    policy = placement_home / "fleet" / "allocation-policy.json"
    policy.parent.mkdir()
    pressure = placement_home / "host-pressure.json"
    policy.write_text(json.dumps({"schema": 1, "pressure_path": {os_name: str(pressure)}}), encoding="utf-8")
    row = {"host": "studio", "at": 2000 - age, "mem_total_gb": mem_total}
    if fleet is not None:
        row["fleet"] = fleet
    pressure.write_text(json.dumps(row), encoding="utf-8")

    monkeypatch.setattr(klg.time, "time", lambda: 2000.0)
    monkeypatch.setattr(klg, "_os_name", lambda: os_name)
    monkeypatch.setattr(kanban_db, "kanban_home", lambda: placement_home)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=400 << 30, total=512 << 30))
    monkeypatch.setattr(klg, "sample_psi", lambda: {
        "psi_cpu_some10": 0.0, "psi_mem_some10": 0.0, "psi_io_some10": 0.0,
    } if os_name == "linux" else {})
    monkeypatch.setattr(kanban_headroom, "local_kind_pause", lambda: None)
    monkeypatch.setattr(admission, "read_slice_memory", lambda **_: None)
    monkeypatch.setattr(klg, "unified_inputs", lambda *_: ([], admission.Priority()))
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "1")

    gate = klg.LoadGate({"max_spawn_per_tick": 1}, 32)
    allowance, why = gate.admit(1.0, now=1000.0, running=0, cpu_busy=0.05)
    assert allowance == allowed, why
    if reason:
        assert reason in why
    else:
        assert why is None


def test_local_gate_ages_shared_lease_against_each_sample(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import allocation_policy, kanban_headroom

    pressure = tmp_path / "host-pressure.json"
    pressure.write_text(json.dumps({"at": 1890, "fleet": {"cpus": 2}}), encoding="utf-8")
    monkeypatch.setattr(allocation_policy, "load", lambda _: SimpleNamespace(
        pressure_path=str(pressure), doc={"pressure_path": {"darwin": str(pressure)}}, stale_after_s=120,
    ))
    monkeypatch.setattr(klg.time, "time", lambda: 2000.0)
    monkeypatch.setattr(klg, "_os_name", lambda: "darwin")
    monkeypatch.setattr(klg, "sample_psi", lambda: {})
    monkeypatch.setattr(kanban_headroom, "local_kind_pause", lambda: None)
    monkeypatch.setattr(admission, "read_slice_memory", lambda **_: None)
    monkeypatch.setattr(klg, "unified_inputs", lambda _: ([], admission.Priority(line=0.8)))
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "1")
    # The lease started after the fleet reading but is already settled in the live reading.
    lease = admission.Lease("studio", "ci", admission.Estimate(cpus=2), at=1900.0)
    monkeypatch.setattr(klg, "shared_leases", lambda _: [lease])
    ctx = {"host": "studio", "need": admission.Estimate(cpus=1), "ceiling": None,
           "mem_available_gb": 32, "mem_total_gb": 64}
    monkeypatch.setattr(klg, "utilization_context", lambda: ctx)
    gate = klg.LoadGate({"max_spawn_per_tick": 1}, 32)

    ctx["limits"] = admission.Limits(fleet_cap=(("cpus", 5),))
    allowance, why = gate.admit(1.0, now=1000.0, running=0, cpu_busy=0.75)
    assert allowance == 1, why  # exactly 2 measured + 2 held + 1 requested

    ctx["limits"] = admission.Limits(fleet_cap=(("cpus", 4),))
    allowance, why = gate.admit(1.0, now=1000.0, running=0, cpu_busy=0.75)
    assert allowance == 0
    assert "fleet_cap:cpus used 2.00 + young 2.00 + need 1.00" in why
    assert gate.util["free"]["cpu"] > 0


def test_utilization_admit_path_runs_on_the_real_modules(placement_home, monkeypatch):
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0,
                                                   "psi_io_some10": 0.0})
    g = klg.LoadGate({"max_spawn_per_tick": 4}, 32)
    allowance, why = g.admit(1.0, now=1000.0, running=0, cpu_busy=0.05)
    assert isinstance(allowance, int) and allowance >= 0
    assert g.util and g.util["need"]["source"]
