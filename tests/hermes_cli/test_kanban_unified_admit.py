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
from hermes_cli.fleet_placement import admission as ad
from hermes_cli.fleet_placement import admission_demand as dmd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli.fleet_placement import placement
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
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "1")
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


def test_d8_prism_queue_restricted_by_alias_holds_kanban_back(studio, monkeypatch):
    """Prism f6d8d4445d4c: unified_inputs read demand with no resolver, so a Prism queue published for the alias
    "studio" never matched "mac-studio" and kanban took the room Prism is queued for. The table's resolver folds it."""
    (studio / "config").mkdir()
    (studio / "config" / "fleet-hosts.json").write_text(json.dumps({
        "schema": 1, "default_first": "mac-studio", "saturate_to": 1.0, "footprints": {"kanban": {"ram_gb": 3, "threads": 2}},
        "hosts": {"mac-studio": {"cores": 24, "threads": 32, "mem_total_gb": 512, "mem_reserve_gb": 128,
                                 "cores_reserve": 4, "consumers": {}, "aliases": ["studio", "local"]}}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(studio))
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["studio"], root=studio)
    a, why = _gate().admit(5.0, now=1000.0, running=0, cpu_busy=0.86)
    assert a == 0 and "held_back:kanban for prism 64 queued" in (why or ""), (a, why)


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
        self.gpu = {}
        self.band = {}         # host -> published band.kanban (default ok)
        self.extra = {}        # host -> extra pressure-row fields (procs, maxproc, slo_breach)

    def text(self, h):
        return json.dumps({"host": h, "at": self.t0 - self.age, "load_ratio": 0.1,
                           "band": {"kanban": self.band.get(h, "ok")},
                           "mem_available_gb": self.mem[h], "psi_cpu_some10": 1.0, "psi_mem_some10": 0.0,
                           "psi_io_some10": 0.5, "os": "linux", "ncpu": self.ncpu[h], "cpu_busy": self.busy[h],
                           **({"gpu_free_mib": self.gpu[h]} if h in self.gpu else {}), **self.extra.get(h, {})})

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
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "1")
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


def test_d7_pinned_gpu_take_reads_the_targets_free_gpu(home):
    """Prism 85f2492e610e: sample_fields / sample_from_detail dropped gpu_free_mib, so a GPU card's pinned take read
    unmeasured:gpu_free_mib on a host reporting free VRAM and was refused in utilization mode."""
    f = Fleet(time.time())
    f.gpu["ace-ai"] = 8192.0
    plan = _plan(home, f)
    got = plan.take("alpha", pin="ace-ai", job=placement.Job(footprint={"gpu_mb": 1024}))
    assert got is not None and got.name == "ace-ai", plan.refusal
    f2 = Fleet(time.time())
    f2.gpu["ace-ai"] = 512.0                                      # measured and too small: refused on the gpu axis
    plan = _plan(home, f2)
    assert plan.take("alpha", pin="ace-ai", job=placement.Job(footprint={"gpu_mb": 1024})) is None
    assert "gpu" in plan.refusal and "unmeasured" not in plan.refusal, plan.refusal


# ------------------------------------------- round 7: the class verdict is the locked ledger's; booked = admitted

def _kanban_rows(home):
    p = home / placement.RESERVATIONS_REL
    rows = json.loads(p.read_text(encoding="utf-8"))["rows"] if p.exists() else []
    return [r for r in rows if r.get("consumer") == "kanban"]


def _plan_mode(home, fleet, allocd, monkeypatch):
    if allocd == "on":       # alloc_client's in-process fallback (router-off): the same ledger, allocd's own path
        from hermes_cli import alloc_client
        (home / "var" / "alloc").mkdir(parents=True, exist_ok=True)
        (home / "var" / "alloc" / "router-off").write_text("", encoding="utf-8")
        monkeypatch.setattr(alloc_client, "ledger_is_local", lambda table=None: True)
        # the fallback scores on the collector's document, not the plan's probe: the same fleet, written there
        (home / "var" / "fleet-pressure.json").write_text(json.dumps({"at": fleet.t0, "hosts": {
            h: {**json.loads(fleet.text(h)), "age_s": fleet.age, "load1": 10.0, "load5": 10.0} for h in HOSTS}}),
            encoding="utf-8")
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet",
                            kanban_cfg=lambda: {"placement": {"allocd": allocd}},
                            ledger=lambda boards: {}, connect=lambda board=None: None,
                            probe=lambda h: kwp.probe_host(h, runner=fleet.runner, pressure_path=POLICY.pressure_path))
    plan = b.plan_pool([], {})
    plan.signal.clock = lambda: fleet.t0
    return plan


@pytest.mark.parametrize("allocd", ["off", "on"])
@pytest.mark.parametrize("unified", ["1", "0"])
def test_two_concurrent_takes_where_only_one_fits_the_class_rule_admit_exactly_one(home, monkeypatch, allocd,
                                                                                   unified):
    """Prism c8d0343d7fb6: unified_filter ran BEFORE the ledger lock, so dispatcher A passed the class rule, B booked,
    and A's locked admit() (today's rule, no holdback) booked too: two kanban leases where the holdback leaves room
    for one. ci-box at 0.880 with Prism queued: 1 kanban = 0.880 + 2/256 + 2 held = 0.8956 fits, 2 = 0.9034 does not;
    today's admit() (no holdback) fits both (0.8956). Unified: exactly one admitted. Kill switch: both (today)."""
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", unified)
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ci-box"] = 0.880
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["ci-box"], root=home, now=t0)
    a, b = _plan_mode(home, f, allocd, monkeypatch), _plan_mode(home, f, allocd, monkeypatch)
    real, raced = kh.unified_filter, []

    def racing(plan, names, job):
        out = real(plan, names, job)
        if plan is a and not raced:          # A has passed the class rule; B takes before A books
            raced.append(b.take("alpha", only=["ci-box"], job_id="t_b"))
        return out

    monkeypatch.setattr(kh, "unified_filter", racing)
    got_a = a.take("alpha", only=["ci-box"], job_id="t_a")
    assert raced, "the race never ran"
    admitted = [h for h in (got_a, raced[0]) if h is not None]
    rows = _kanban_rows(home)
    if unified == "1":
        assert len(admitted) == 1, (a.refusal, b.refusal)
        assert len(rows) == 1, rows                         # the refused take left no lease behind
        assert "held_back:kanban for prism" in (a.refusal or b.refusal), (a.refusal, b.refusal)
    else:
        assert len(admitted) == 2 and len(rows) == 2, (a.refusal, b.refusal)


@pytest.mark.parametrize("unified,booked", [("1", (6.0, 12.0)), ("0", (2.0, 3.0))])
def test_pinned_take_books_the_learned_estimate_it_was_admitted_on(home, monkeypatch, unified, booked):
    """Prism 8e0e895ec5d4: a pin passed may_start on the LEARNED estimate (6 CPUs / 12 GB from class usage) but
    book_pin reserved the table's declared footprint (2 threads / 3 GB), so the next take over-admitted. Under the
    one rule the pin books what it was admitted on; the kill switch keeps today's declared booking."""
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", unified)
    t0 = time.time()
    hist = home / placement.CLASS_USAGE_REL
    hist.parent.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        ad.record_actual(hist, job_class="kanban-worker-llm-wait", host="ace-ai", cpus=6.0, ram_gb=12.0,
                         at=t0 - 600 + i, job_id=f"t_h{i}")
    plan = _plan(home, Fleet(t0))
    assert plan.take("alpha", pin="ace-ai", job_id="t_pin") is not None, plan.refusal
    (row,) = _kanban_rows(home)
    assert (row["threads"], row["ram_gb"]) == booked, row
    if unified == "1":
        need = placement.admit_context("kanban", plan.signal.placement_table.footprint("kanban"), now=t0,
                                       table=plan.signal.placement_table, root=home).need("ace-ai")
        assert (need.cpus, need.ram_gb) == booked              # the number may_start admitted on
        assert row.get("job_class") == "kanban-worker-llm-wait"


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


def test_tick_demand_skips_operator_only_cards(home):
    """Prism 8d48747ae751: no_worker=1 cards are dropped before spawn; publishing them reserved kanban capacity on
    every host for work that never runs."""
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"c{i}", assignee="alpha") for i in range(3)]
        for tid in ids[:2]:
            conn.execute("UPDATE tasks SET no_worker = 1 WHERE id = ?", (tid,))
        conn.commit()
    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: kb.connect())
    assert b.publish_demand([{"slug": "default"}]) == 1


def test_tick_demand_skips_human_and_unassigned_cards(home):
    """t_365e5753 (2026-10-30): 135 ready cards assigned human:apollo (review/operator lanes the dispatcher never
    spawns) were published as kanban demand 163 and held back ~110 CI runners on ci-box under the ONE rule while 146
    workflow runs queued. Only cards a worker would actually spawn for are demand: the same predicate the
    dispatcher uses (kanban_db.is_worker_assignee)."""
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="w1", assignee="alpha")
        kb.create_task(conn, title="w2", assignee="beta")
        for who in ("human:apollo", "human", "Human:Ace", " human:aegis "):
            kb.create_task(conn, title=f"h-{who}", assignee=who)
        tid = kb.create_task(conn, title="unassigned", assignee="alpha")
        conn.execute("UPDATE tasks SET assignee = NULL WHERE id = ?", (tid,))
        conn.commit()
    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: kb.connect())
    assert b.publish_demand([{"slug": "default"}]) == 2
    doc = json.loads((home / "var" / "placement" / "demand" / "kanban.json").read_text(encoding="utf-8"))
    assert doc["queued"] == 2


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


def test_kanban_unified_admit_defaults_on_kill_switch_zero(monkeypatch):
    """t_c466b7ee: absent KANBAN_UNIFIED_ADMIT = 1 (the ONE rule) on every reader, the vendored rule included;
    "0" is the kill switch (today's rule); other classes keep the vendored default."""
    from hermes_cli.fleet_placement import admission_demand as dmd
    from hermes_cli.kanban_load_gate import unified_admit

    monkeypatch.delenv("KANBAN_UNIFIED_ADMIT", raising=False)
    monkeypatch.delenv("PRISM_UNIFIED_ADMIT", raising=False)
    assert unified_admit() is True
    assert dmd.unified("kanban") is True
    assert dmd.unified(dmd.workload_of("athena")) is True
    assert dmd.unified("kanban", {}) is True
    assert dmd.unified("prism") is True
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")
    assert unified_admit() is False and dmd.unified("kanban") is False


# ------------------------------------------------- t_c466b7ee: round-8/9 Prism residue of #2397, red-on-base each

def test_confirm_leaves_out_the_takes_own_row_by_reservation_id(home):
    """Prism d1bd26bd688c: confirm_class dropped the FIRST lease matching (host, job_id, at). A second live row with the
    same three (a re-admit of the same card in the same second) but a big footprint came first in the ledger, so it
    was left out instead of the take's own row: the verdict read room that a live 150-thread row holds. By id, the
    big row counts and the take is refused (and its own row dropped)."""
    t0 = time.time()
    plan = _plan(home, Fleet(t0))
    rpath = Path(plan.signal.placement_reservations)
    big = placement.reserve("ci-box", "kanban", placement.Footprint(ram_gb=3.0, threads=150.0), ttl_s=600,
                            job_id="t_dup", path=rpath, now=t0)
    own = placement.reserve("ci-box", "kanban", placement.Footprint(ram_gb=3.0, threads=2.0), ttl_s=600,
                            job_id="t_dup", path=rpath, now=t0)
    why = kh.confirm_class(plan, "ci-box", None, own)
    assert why is not None and why.startswith("saturated:cpu"), why   # 0.40 + 150/256 + 2/256 > 0.90
    ids = {r["id"] for r in _kanban_rows(home)}
    assert own not in ids and big in ids, ids


@pytest.mark.parametrize("local,detail", [(False, {"unified": True}), (False, {"rule": "utilization"}),
                                          (False, {"unified": "yes"}), (False, None), (True, {"unified": True})])
def test_allocd_take_is_confirmed_against_the_ledger_allocd_owns(home, monkeypatch, local, detail):
    """Prism 72e85daf44a6 / d618d0a51436: off the router host allocd books in ITS ledger, never this dispatcher's file;
    confirm_class looked the row up locally, found nothing and refused every remote allocd take (closed: not live).
    Prism 11efd32500fc: off the router host the take stands ONLY on a router-side class verdict (the reply's
    ``unified: true``, set by an allocd that ran admit_class); an ordinary grant (placement.admit's older rule) is
    refused and released, since
    unified_filter's local pre-check cannot see allocd's ledger. Router host (one ledger): the local check still runs,
    and a row allocd says it booked but the shared file lacks is refused as before, whatever the reply says."""
    from hermes_cli import alloc_client

    t0 = time.time()
    f = Fleet(t0)
    plan = _plan_mode(home, f, "on", monkeypatch)
    monkeypatch.setattr(alloc_client, "ledger_is_local", lambda table=None: local)
    released = []
    monkeypatch.setattr(kh, "allocd_admit", lambda plan, cands, job, job_id: SimpleNamespace(
        host="ci-box", reservation_id="r_allocd_1", reason="", source="allocd", detail=detail))
    monkeypatch.setattr(kh, "release_alloc", lambda rid: released.append(rid))
    host = plan.take("alpha", only=["ci-box"], job_id="t_remote")
    if local:
        assert host is None and "r_allocd_1 not live" in plan.refusal, plan.refusal
        assert released == ["r_allocd_1"]
    elif detail and detail.get("unified") is True:
        assert host is not None and host.name == "ci-box", plan.refusal
        assert plan.last_reservation == "r_allocd_1" and released == []
    else:
        assert host is None and plan.refusal.startswith("unified_admission"), plan.refusal
        assert "no router-side class verdict" in plan.refusal and released == ["r_allocd_1"]


def test_remote_plain_allocd_grants_cannot_overrun_the_class_rule(home, monkeypatch):
    """Prism 11efd32500fc repro: off the router host, a router that runs only placement.admit() grants every take; the
    local pre-check reads an empty dispatcher ledger and passes both. Under the unified rule neither plain grant may
    stand (the second would project 0.880 + (4+2)/256 > 0.90 with Prism's demand held); both are released."""
    from hermes_cli import alloc_client

    t0 = time.time()
    f = Fleet(t0)
    f.busy = {"ci-box": 0.880, "ace-ai": 0.5, "ace-media": 0.5}
    plan = _plan_mode(home, f, "on", monkeypatch)
    monkeypatch.setattr(alloc_client, "ledger_is_local", lambda table=None: False)
    n = iter(range(10))
    released = []
    monkeypatch.setattr(kh, "allocd_admit", lambda plan, cands, job, job_id: SimpleNamespace(
        host="ci-box", reservation_id=f"r_plain_{next(n)}", reason="", source="allocd", rule="utilization",
        detail={"host": "ci-box"}))
    monkeypatch.setattr(kh, "release_alloc", lambda rid: released.append(rid))
    assert plan.take("alpha", only=["ci-box"], job_id="t_a") is None
    assert plan.take("alpha", only=["ci-box"], job_id="t_b") is None
    assert released == ["r_plain_0", "r_plain_1"], released


@pytest.fixture
def remote_allocd(home, monkeypatch):
    """A kanban dispatcher OFF the router host, taking through a real HTTP allocd (FakeAllocd mirrors hermes-home
    scripts/allocd.py, including its unified confirm). The local pre-check is a pass-through: off the router it reads
    the dispatcher's own file, which never holds allocd's rows (Prism 11efd32500fc), so only allocd can refuse."""
    from hermes_cli import alloc_client
    from tests.hermes_cli.test_kanban_allocd_route import FakeAllocd

    def make(fleet, *, legacy=False):
        plan = _plan_mode(home, fleet, "on", monkeypatch)
        (home / "var" / "alloc" / "router-off").unlink()
        monkeypatch.setattr(alloc_client, "ledger_is_local", lambda table=None: False)
        monkeypatch.setattr(kh, "unified_filter", lambda plan, names, job: (list(names), {}))
        fake = FakeAllocd()
        if legacy:                      # an allocd that predates the ask: runs placement.admit() only
            real = fake.admit
            fake.admit = lambda req: real({k: v for k, v in req.items() if k not in ("unified", "stale_after_s")})
        monkeypatch.setenv("ALLOCD_URL", fake.url)
        fakes.append(fake)
        return plan, fake

    fakes = []
    yield make
    for f in fakes:
        f.close()


def _live_kanban(fleet_t0):
    return [r for r in placement._read_ledger(placement.reservations_path())["rows"]
            if r["consumer"] == "kanban" and placement._row_live(r, time.time())]


def test_remote_take_from_an_allocd_without_the_class_verdict_is_refused(home, remote_allocd):
    """Prism 11efd32500fc: an allocd whose /admit is placement.admit() alone grants; the reply has no unified field,
    so the remote take is refused and the row allocd booked is released."""
    f = Fleet(time.time())
    plan, fake = remote_allocd(f, legacy=True)
    assert plan.take("alpha", only=["ci-box"], job_id="t_r1") is None
    assert plan.refusal.startswith("unified_admission") and "unified: true" in plan.refusal, plan.refusal
    assert [p for p, _ in fake.requests] == ["/admit", "/release"]
    assert _live_kanban(f.t0) == []


def test_remote_take_from_an_admit_class_allocd_is_admitted(home, remote_allocd):
    f = Fleet(time.time())
    plan, fake = remote_allocd(f)
    host = plan.take("alpha", only=["ci-box"], job_id="t_r1")
    assert host is not None and host.name == "ci-box", plan.refusal
    assert plan.last_decision.detail["unified"] is True
    path, req = fake.requests[0]
    assert path == "/admit" and req["unified"] is True and req["stale_after_s"] == POLICY.stale_after_s


def test_two_remote_takes_against_a_full_class_second_refused(home, remote_allocd):
    """Prism 11efd32500fc repro: ci-box at cpu_busy 0.880 / 256 CPUs, kanban need 2, Prism's demand holding 2. The
    first remote take fits (0.880 + 4/256); the second (0.880 + 6/256 = 0.9034) is refused by allocd's admit_class
    on ITS ledger, though the dispatcher's local pre-check passed both."""
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ci-box"] = 0.880
    dmd.publish("prism", 64, PRISM_UNIT, hosts=["ci-box"], root=home, now=t0)
    plan, fake = remote_allocd(f)
    assert plan.take("alpha", only=["ci-box"], job_id="t_r1") is not None, plan.refusal
    assert plan.take("alpha", only=["ci-box"], job_id="t_r2") is None
    assert "unified_admission" in plan.refusal, plan.refusal
    assert len(_live_kanban(t0)) == 1


@pytest.mark.parametrize("extra,why", [({"slo_breach": True}, "saturated:slo"),
                                       ({"procs": 9900, "maxproc": 10000}, "saturated:procs")])
def test_the_full_utilization_sample_reaches_admit_class(home, extra, why):
    """Prism ce581503f8d8: sample_fields / sample_from_detail dropped procs, maxproc and slo_breach, so admit_class
    never saw the process-table and latency-SLO guards the band cap it replaces carried (ACE-AI voice hub)."""
    f = Fleet(time.time())
    f.extra["ace-ai"] = extra
    plan = _plan(home, f)
    keep, held = kh.unified_filter(plan, ["ace-ai"], None)
    assert keep == [] and held["ace-ai"].startswith(why), held


@pytest.mark.parametrize("admit_on_memory", [True, False])
def test_hot_band_with_slo_breach_is_refused_on_both_paths_under_unified(home, admit_on_memory):
    """Prism ce581503f8d8 (card acceptance): a published hot band.kanban + slo_breach on ace-ai, KANBAN_UNIFIED_ADMIT=1.
    The pinned and the unpinned take both refuse. With admit_on_memory the band axis still zeroes the slots (only the
    memory axis is retired); without it (the live config) admit_class refuses on the SLO guard."""
    t0 = time.time()
    f = Fleet(t0)
    f.band["ace-ai"] = "hot"
    f.extra["ace-ai"] = {"slo_breach": True}
    cfg = {"placement": {"admit_on_memory": admit_on_memory}}
    plan = _plan_cfg(home, f, cfg)
    plan.signal.clock = lambda: t0
    if admit_on_memory:
        assert plan.slots["ace-ai"] == 0 and "band.kanban hot" in plan.detail["ace-ai"]["pressure"]
    assert plan.take("alpha", pin="ace-ai") is None
    assert plan.refusal.startswith("pin_host_"), plan.refusal
    assert plan.take("alpha", only=["ace-ai"]) is None
    assert plan.refusal.startswith(("unified_admission", "pool_unavailable")), plan.refusal


def test_hot_band_kill_switch_keeps_todays_cap(home, monkeypatch):
    monkeypatch.setenv("KANBAN_UNIFIED_ADMIT", "0")
    f = Fleet(time.time())
    f.band["ace-ai"] = "hot"
    plan = _plan_cfg(home, f, {"placement": {"admit_on_memory": True}})
    assert plan.slots["ace-ai"] == 0                              # today's cap: band.kanban hot = 0


def test_unified_refusal_code_is_in_the_shared_take_vocabulary(home):
    """Prism 43117075a644: take() refused with ``unified_admission (...)``, a code outside TAKE_REFUSALS, so the I-1
    closeout query read every such refusal as a violation."""
    from hermes_cli import kanban_placement_reasons as pr

    t0 = time.time()
    f = Fleet(t0)
    f.busy = {"ci-box": 0.885, "ace-ai": 0.86, "ace-media": 0.86}
    dmd.publish("prism", 64, PRISM_UNIT, root=home, now=t0)
    plan = _plan(home, f)
    assert plan.take("alpha") is None
    assert pr.take_refusal_code(plan.refusal) == pr.UNIFIED_ADMISSION
    assert pr.UNIFIED_ADMISSION in pr.TAKE_REFUSALS


def test_dry_run_counts_its_simulated_takes_against_the_class_rule(home):
    """Prism 9eecf7c2e1ee: a dry run books nothing, and _class_verdict read only the ledger, so every simulated take
    on a host saw the same empty headroom: the dry run granted pins the live run refuses. ace-ai at 0.80 busy on 32
    CPUs, 2 CPUs a take: one = 0.8625 fits, two = 0.925 does not."""
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ace-ai"] = 0.80
    plan = _plan(home, f)
    plan.dry_run = True
    assert plan.take("alpha", pin="ace-ai") is not None, plan.refusal
    assert plan.take("alpha", pin="ace-ai") is None
    assert plan.refusal.startswith("pin_host_unified_admission (ace-ai: saturated:cpu"), plan.refusal
    keep, held = kh.unified_filter(plan, ["ace-ai"], None)
    assert keep == [] and held["ace-ai"].startswith("saturated:cpu"), held
    assert _kanban_rows(home) == []                               # still a dry run: nothing booked


def test_dry_run_replays_each_simulated_takes_own_footprint(home):
    """Prism fd6bd8070eee: _with_simulated rebuilt every earlier simulated take from the CURRENT job's footprint, so a
    3-thread take followed by a 1-thread one counted the first as 1 thread (0.80 + 2/32 = 0.8625, granted). Each
    simulated take now carries its own lease: 0.80 + (3 + 1)/32 = 0.925 > 0.90, refused."""
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ace-ai"] = 0.80
    plan = _plan(home, f)
    plan.dry_run = True
    big = placement.Job(footprint={"threads": 3, "ram_gb": 3})
    small = placement.Job(footprint={"threads": 1, "ram_gb": 3})
    assert plan.take("alpha", pin="ace-ai", job=big) is not None, plan.refusal      # 0.80 + 3/32 = 0.894
    assert plan.take("alpha", pin="ace-ai", job=small) is None
    assert plan.refusal.startswith("pin_host_unified_admission (ace-ai: saturated:cpu"), plan.refusal
    plan.release(plan.hosts["ace-ai"])                                             # refund the 3-thread take
    assert plan._sim_leases["ace-ai"] == []
    assert plan.take("alpha", pin="ace-ai", job=small) is not None, plan.refusal    # 0.80 + 1/32 fits again
