"""Kanban dispatcher onto the ONE admission rule (t_3694ab3c; parent t_e5124426, hermes-home#7172).

Inventory D7 (pool placement), D8 (Studio LoadGate) and D9 (6 GB/worker memory cap) now ask
admission.admit_class("kanban") / placement.may_start(host, "kanban"): the fleet line (0.90) and the queued
demand of every class that outranks kanban (fleet/admission-priority.json: main > prism > interactive > kanban).
KANBAN_UNIFIED_ADMIT=0 = today's rule, one release.

Red-first arms (each fails on the pre-t_3694ab3c dispatcher):
  D8 replay   Studio at cpu_busy 0.86 with Prism 64 queued on it: admission.admit() (today) grants 2 workers;
              admit_class holds them back (``held_back:kanban for prism 64 queued``).
  D7 replay   the same Prism queue published for ci-box: a pool take that today lands on ci-box (default_first)
              is held there and goes to the next host; with every host held, the take is refused, never forced.
  D9          admit_on_memory's 6 GB/worker cap no longer cuts a host's slots under the unified rule.
  demand      the tick publishes ready-card demand as var/placement/demand/kanban.json.
Kill switch arms: KANBAN_UNIFIED_ADMIT=0 restores each old answer.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_gate_tick as kgt
from hermes_cli import admission as ad
from hermes_cli import admission_demand as dmd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import placement
from hermes_cli import placement_policy as pp

POLICY = pp.PlacementPolicy()
HOSTS = ("ci-box", "ace-ai", "ace-media")
PRIORITY = {"schema": 1, "order": ["main", "prism", "interactive", "kanban", "ci", "test-gate", "batch"],
            "line": 0.90, "holdback_units": 2, "demand_max_age_s": 120}
PRISM_UNIT = ad.Estimate(cpus=1.0, ram_gb=4.0)


# ------------------------------------------------------------------------------------- D8: the Studio LoadGate

@pytest.fixture
def studio(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "fleet").mkdir(parents=True)
    (root / "fleet" / "admission-priority.json").write_text(json.dumps(PRIORITY), encoding="utf-8")
    monkeypatch.setenv("PLACEMENT_HOME", str(root))
    monkeypatch.delenv("KANBAN_UNIFIED_ADMIT", raising=False)
    c = {"host": "mac-studio", "ceiling": None, "ceiling_reason": "-",
         "need": ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior"),
         "limits": ad.Limits(mem_reserve_gb=128.0), "mem_available_gb": 360.0, "mem_total_gb": 512.0}
    monkeypatch.setattr(klg, "utilization_context", lambda: dict(c))
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0, "psi_io_some10": 0.0})
    return root


def _gate():
    g = klg.LoadGate({"max_spawn_per_tick": 4}, 32)
    g.admission = "utilization"
    return g


def test_d8_studio_spawn_held_back_while_prism_queues_on_it(studio):
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["mac-studio"], root=studio)
    g = _gate()
    a, why = g.admit(5.0, now=1000.0, running=0, cpu_busy=0.86)   # 0.86 + 1/32 = 0.891 < 0.90: room for one alone
    assert a == 0, (a, why)
    assert "held_back:kanban for prism 64 queued" in why
    assert g.util["rule"] == "admit_class" and g.band == "spilling"


def test_d8_kill_switch_restores_admit(studio, monkeypatch):
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["mac-studio"], root=studio)
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")
    g = _gate()
    a, why = g.admit(5.0, now=1000.0, running=0, cpu_busy=0.86)
    assert a == 1 and why is None, (a, why)                         # today's admit(): Prism's queue is not read
    assert g.util["rule"] == "admit"


def test_d8_lower_class_demand_never_holds_kanban(studio):
    dmd.publish("ci", 64, ad.Estimate(cpus=4.0, ram_gb=4.0), root=studio)
    dmd.publish("test-gate", 9, ad.Estimate(cpus=4.0, ram_gb=16.0), root=studio)
    a, why = _gate().admit(5.0, now=1000.0, running=0, cpu_busy=0.50)
    assert a == 4 and why is None, (a, why)                         # max_spawn_per_tick, nothing held back


def test_d8_the_line_is_the_fleet_line_not_cpu_saturable(studio, monkeypatch):
    c = klg.utilization_context()
    c["limits"] = ad.Limits(mem_reserve_gb=128.0, cpu_saturable=True)   # a saturable row may not escape the line
    monkeypatch.setattr(klg, "utilization_context", lambda: dict(c))
    a, why = _gate().admit(5.0, now=1000.0, running=0, cpu_busy=0.95)
    assert a == 0 and "saturated:cpu" in why, (a, why)


# ---------------------------------------------------------------------------- D7: the pool pick (may_start)

TABLE = {
    "schema": 1, "default_first": "ci-box", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 2}},
    "hosts": {
        "ci-box": {"cores": 128, "threads": 256, "mem_total_gb": 220, "mem_reserve_gb": 40,
                   "cores_reserve": 16, "consumers": {"kanban": {"slots": 24}}, "clock_mhz": 3207, "aliases": ["ci"]},
        "ace-media": {"cores": 24, "threads": 32, "mem_total_gb": 94, "mem_reserve_gb": 16,
                      "cores_reserve": 4, "consumers": {"kanban": {"slots": 4}}, "clock_mhz": 6000},
        "ace-ai": {"cores": 16, "threads": 32, "mem_total_gb": 121, "mem_reserve_gb": 24,
                   "cores_reserve": 4, "consumers": {"kanban": {"slots": 4}}, "clock_mhz": 5756},
    },
}


class Fleet:
    def __init__(self, t0):
        self.t0 = t0
        self.busy = {"ci-box": 0.40, "ace-ai": 0.30, "ace-media": 0.30}
        self.ncpu = {"ci-box": 256, "ace-ai": 32, "ace-media": 32}
        self.mem = {"ci-box": 181.0, "ace-ai": 75.0, "ace-media": 77.0}
        self.age = 5.0

    def text(self, h):
        return json.dumps({"host": h, "at": self.t0 - self.age, "load_ratio": 0.1, "band": {"kanban": "ok"},
                           "mem_available_gb": self.mem[h], "psi_cpu_some10": 1.0, "psi_mem_some10": 0.0,
                           "psi_io_some10": 0.5, "os": "linux", "ncpu": self.ncpu[h], "cpu_busy": self.busy[h]})

    def runner(self, argv, **kw):
        h = argv[-2].split("@", 1)[1]
        out = f"10.00 10.00 1.00 1/100 1\n{self.ncpu[h]}\n{int(self.t0)}\n{self.text(h)}\n"
        return SimpleNamespace(returncode=0, stdout=out)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("PLACEMENT_HOME", raising=False)
    monkeypatch.delenv("KANBAN_UNIFIED_ADMIT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (home / "profiles" / "alpha").mkdir(parents=True)
    fleet = home / "fleet"
    fleet.mkdir()
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        h: {"roles": {"kanban-worker": {"slots": 24}}, "state": "active"} for h in HOSTS}}), encoding="utf-8")
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "kanbanw", "priority": ["ace-ai", "ace-media", "ci-box"],
        "profiles": ["alpha"], "hosts": {h: {"enabled": True, "absence": "optional"} for h in HOSTS}}),
        encoding="utf-8")
    (fleet / "admission-priority.json").write_text(json.dumps(PRIORITY), encoding="utf-8")
    (home / "config").mkdir()
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    (home / "config.yaml").write_text("kanban:\n  admission: utilization\n", encoding="utf-8")
    return home


def _plan(home, fleet):
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: None,
                            probe=lambda h: kwp.probe_host(h, runner=fleet.runner, pressure_path=POLICY.pressure_path))
    plan = b.plan_pool([], {})
    assert plan is not None and plan.signal.placement_table is not None
    plan.signal.clock = lambda: fleet.t0
    return plan


def test_d7_prism_queue_on_ci_box_holds_the_kanban_take_there(home):
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ci-box"] = 0.885                                      # alone: 0.885 + 2/256 = 0.893 fits; with 2 prism seats: 0.901
    assert placement.admission_mode() == "utilization"
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["ci-box"], root=home, now=t0)
    plan = _plan(home, f)
    host = plan.take("alpha")
    assert host is not None and host.name != "ci-box", plan.refusal
    keep, held = kh.unified_filter(plan, ["ci-box"], None)
    assert keep == [] and held["ci-box"].startswith("held_back:kanban for prism 64 queued"), held


def test_d7_kill_switch_keeps_todays_pick(home, monkeypatch):
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ci-box"] = 0.885
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["ci-box"], root=home, now=t0)
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")
    plan = _plan(home, f)
    assert kh.unified_filter(plan, list(HOSTS), None) == (list(HOSTS), {})   # inert: Prism's queue not read
    assert plan.take("alpha") is not None
    ci = {r.host: r for r in plan.last_decision.rows}["ci-box"]
    assert ci.eligible, ci.reason                                 # today's scorer admits ci-box at 0.885 + one worker


def test_d7_every_host_held_refuses_the_take(home):
    t0 = time.time()
    f = Fleet(t0)
    f.busy = {"ci-box": 0.885, "ace-ai": 0.86, "ace-media": 0.86}
    dmd.publish("prism", 64, PRISM_UNIT, root=home, now=t0)       # hosts null: Prism may run anywhere
    plan = _plan(home, f)
    assert plan.take("alpha") is None
    assert plan.refusal.startswith("unified_admission (") and "held_back:kanban for prism" in plan.refusal


def test_d7_legacy_admission_mode_is_unchanged(home):
    (home / "config.yaml").write_text("kanban:\n  admission: legacy\n", encoding="utf-8")
    plan = _plan(home, Fleet(time.time()))
    keep, held = kh.unified_filter(plan, list(HOSTS), None)
    assert keep == list(HOSTS) and held == {}


@pytest.mark.parametrize("age,admitted", [(90.0, True), (130.0, False)])
def test_d7_staleness_is_the_kanban_policys_not_a_shared_60s(home, age, admitted):
    """Prism 5113510bdb9a: may_start hardcoded 60 s while the kanban policy (stale_after_s 120) and its scorer call a
    90 s-old push fresh, so unified_filter dropped the host closed:stale. unified_filter now passes the policy's bound:
    a 90 s-old sample is admitted, a 130 s-old one is refused."""
    assert POLICY.stale_after_s == 120.0
    f = Fleet(time.time())
    f.age = age
    plan = _plan(home, f)
    keep, held = kh.unified_filter(plan, ["ace-ai"], None)
    if admitted:
        assert keep == ["ace-ai"] and held == {}, held
    else:
        assert keep == [] and "stale" in held["ace-ai"], held


# ------------------------------------------------------------------------------- D9: the 6 GB/worker cap

def _plan_cfg(home, fleet, cfg):
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: cfg,
                            ledger=lambda boards: {}, connect=lambda board=None: None,
                            probe=lambda h: kwp.probe_host(h, runner=fleet.runner, pressure_path=POLICY.pressure_path))
    return b.plan_pool([], {})


def test_d9_memory_cap_is_reported_not_enforced_under_unified(home):
    f = Fleet(time.time())
    f.mem["ace-ai"] = 7.0                                         # 6 GB/worker reservation: cap 1 < 24 free slots
    cfg = {"placement": {"admit_on_memory": True}}
    plan = _plan_cfg(home, f, cfg)
    assert "ace-ai" in plan.admission                             # still computed and reported
    assert plan.slots["ace-ai"] == 24                             # not enforced: admit_class owns the memory axis


def test_d9_kill_switch_enforces_the_cap(home, monkeypatch):
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")
    f = Fleet(time.time())
    f.mem["ace-ai"] = 7.0
    plan = _plan_cfg(home, f, {"placement": {"admit_on_memory": True}})
    assert plan.slots["ace-ai"] == 1                              # today's floor(7 / 6)


def test_d9_cap_still_binds_where_unified_filter_does_not_run(home):
    """Prism f4d9c1273272: legacy admission mode (unified_filter inert) keeps the 6 GB/worker cap enforced even with
    KANBAN_UNIFIED_ADMIT on, so no path loses its memory check."""
    (home / "config.yaml").write_text("kanban:\n  admission: legacy\n", encoding="utf-8")
    f = Fleet(time.time())
    f.mem["ace-ai"] = 7.0
    plan = _plan_cfg(home, f, {"placement": {"admit_on_memory": True}})
    assert plan.slots["ace-ai"] == 1


def test_d7_pinned_take_passes_the_one_rule_too(home):
    """Prism f4d9c1273272: a pin keeps precedence, not an exemption: held back for Prism, the pinned take refuses."""
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ci-box"] = 0.885
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["ci-box"], root=home, now=t0)
    plan = _plan(home, f)
    assert plan.take("alpha", pin="ci-box") is None
    assert plan.refusal.startswith("pin_host_unified_admission (ci-box: held_back:kanban for prism 64 queued")
    assert plan.take("alpha", pin="ace-ai").name == "ace-ai"     # a host with room: the pin still wins


# ---------------------------------------------------------------------------------- demand publication

def test_tick_publishes_ready_demand(home):
    with kb.connect_closing() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"c{i}", assignee="alpha")
    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: kb.connect())
    assert b.publish_demand([{"slug": "default"}]) == 3
    doc = json.loads((home / "var" / "placement" / "demand" / "kanban.json").read_text(encoding="utf-8"))
    assert doc["workload"] == "kanban" and doc["queued"] == 3 and doc["hosts"] is None
    assert doc["unit"] == {"cpus": 2.0, "ram_gb": 3.0, "gpu_mib": 0.0}


def test_tick_publishes_nothing_under_the_kill_switch(home, monkeypatch):
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")
    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: kb.connect())
    assert b.publish_demand([{"slug": "default"}]) is None
    assert not (home / "var" / "placement" / "demand" / "kanban.json").exists()


# ---------------------------------------------------------------------------------- vendoring contract

def test_vendored_admission_demand_matches_canonical():
    """The vendored admission_demand equals hermes-home's except the VENDORED paragraph, _root() and utf-8-sig
    reads. ADMISSION_DEMAND_CANONICAL names the canonical file (skipped where unset)."""
    env = os.environ.get("ADMISSION_DEMAND_CANONICAL")
    if not env:
        pytest.skip("ADMISSION_DEMAND_CANONICAL not set (no hermes-home checkout to cross-read)")

    def body(text):
        head, sep, rest = text.partition("\nVENDORED from hermes-home")
        if sep:
            text = head + rest[rest.index("\n\n") + 1:]
        s, e = text.index("def _root("), text.index("def _say(")
        return (text[:s] + text[e:]).replace('read_text(encoding="utf-8-sig")', 'read_text(encoding="utf-8")')

    ours = Path(dmd.__file__).read_text(encoding="utf-8")
    assert body(ours) == body(Path(env).read_text(encoding="utf-8"))
