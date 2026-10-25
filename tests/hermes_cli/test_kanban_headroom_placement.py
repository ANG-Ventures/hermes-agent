"""Kanban pool picks through the ONE placement scorer (card t_d6ce856e).

``SpilloverPlan.take()`` asks the vendored ``placement.choose()`` when
``<root>/config/fleet-hosts.json`` is present: headroom gate -> Job overrides
-> ci-box first under its physical cores -> most headroom. The probe seam is a
fake ssh runner answering the real probe command; nothing opens a connection
and the real spawn raises.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_gate_tick as kgt
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import placement
from hermes_cli import placement_policy as pp

POLICY = pp.PlacementPolicy()
T0 = 1_791_850_000.0
HOSTS = ("ci-box", "ace-ai", "ace-media")


def _dead_pid() -> int:
    """A pid no live process owns. A fixed fake pid (4242) is a real process on some hosts
    (dnsmasq on ace-ai), so ``complete_task`` saw a live worker claim and raised LiveClaimError."""
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


FAKE_PID = _dead_pid()

# The #3945 table, trimmed to what the scorer reads.
TABLE = {
    "schema": 1, "default_first": "ci-box", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 2}},
    "hosts": {
        "ci-box": {"cores": 128, "threads": 256, "mem_total_gb": 220, "mem_reserve_gb": 40,
                   "cores_reserve": 16, "consumers": {"kanban": {"slots": 24}}, "clock_mhz": 3207,
                   "aliases": ["ci"]},
        "ace-media": {"cores": 24, "threads": 32, "mem_total_gb": 94, "mem_reserve_gb": 16,
                      "cores_reserve": 4, "consumers": {"kanban": {"slots": 4}}, "clock_mhz": 6000},
        "ace-ai": {"cores": 16, "threads": 32, "mem_total_gb": 121, "mem_reserve_gb": 24,
                   "cores_reserve": 4, "consumers": {"kanban": {"slots": 4}}, "clock_mhz": 5756},
    },
}


class Fleet:
    """Per-host probe answers: load1/load5, remote clock, host-pressure.json."""

    def __init__(self):
        self.now = {h: T0 for h in HOSTS}
        self.load = {"ci-box": 40.0, "ace-ai": 3.8, "ace-media": 6.6}
        self.mem = {"ci-box": 181.0, "ace-ai": 75.0, "ace-media": 77.0}
        self.age = {h: 5.0 for h in HOSTS}

    def text(self, h):
        return json.dumps({"host": h, "at": self.now[h] - self.age[h], "load_ratio": 0.1, "band": {"kanban": "ok"},
                           "mem_available_gb": self.mem[h], "psi_cpu_some10": 1.0, "psi_mem_some10": 0.0})

    def runner(self, argv, **kw):
        h = argv[-2].split("@", 1)[1]
        out = (f"{self.load[h]:.2f} {self.load[h]:.2f} 1.00 1/100 1\n32\n{int(self.now[h])}\n"
               f"{self.text(h)}\n")
        return SimpleNamespace(returncode=0, stdout=out)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("PLACEMENT_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("real spawn")))
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (home / "profiles" / "alpha").mkdir(parents=True)
    fleet = home / "fleet"
    fleet.mkdir()
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        h: {"roles": {"kanban-worker": {"slots": 24}}, "state": "active"} for h in HOSTS}}), encoding="utf-8")
    # The sidecar order deliberately puts ci-box LAST: the scorer must not read it as an order.
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "kanbanw", "priority": ["ace-ai", "ace-media", "ci-box"],
        "profiles": ["alpha"], "hosts": {h: {"enabled": True, "absence": "optional"} for h in HOSTS}}),
        encoding="utf-8")
    (home / "config").mkdir()
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    return home


def _plan(home, fleet, *, now=T0):
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: None,
                            probe=lambda h: kwp.probe_host(h, runner=fleet.runner,
                                                           pressure_path=POLICY.pressure_path))
    plan = b.plan_pool([], {})
    assert plan is not None and plan.signal.placement_table is not None
    plan.signal.clock = lambda: now
    return plan


def _row(plan, name):
    return {r.host: r for r in plan.last_decision.rows}[name]


# -- the five verify cases ---------------------------------------------------

def test_three_hosts_ok_ci_box_under_cores_takes_ci_box(home):
    plan = _plan(home, Fleet())
    assert plan.take("alpha").name == "ci-box"
    assert plan.last_decision.rule == "default_first"
    assert all(r.eligible for r in plan.last_decision.rows)


def test_ci_box_at_cores_falls_to_most_headroom(home):
    f = Fleet()
    f.load["ci-box"] = 127.0      # 127 + 2 threads > 128 physical cores: past its comfortable ceiling
    f.mem["ci-box"] = 100.0       # still eligible (100 - 40 - 3 = 57 GB) but no longer the best fraction
    plan = _plan(home, f)
    host = plan.take("alpha").name
    d = plan.last_decision
    assert d.rule == "headroom"
    best = max((r for r in d.rows if r.eligible), key=lambda r: r.score)
    assert host == best.host and host != "ci-box"


def test_continuity_beats_the_default(home):
    plan = _plan(home, Fleet())
    assert plan.take("alpha", job=placement.Job(continuity="ace-media")).name == "ace-media"
    assert plan.last_decision.rule == "continuity"


def test_stale_ace_ai_is_closed(home):
    f = Fleet()
    f.age["ace-ai"] = 600.0
    plan = _plan(home, f)
    assert plan.slots["ace-ai"] == 0
    # Even asked for by affinity, a stale host is never picked: the gate wins.
    host = plan.take("alpha", job=placement.Job(affinity="ace-ai")).name
    assert host != "ace-ai"
    r = _row(plan, "ace-ai")
    assert not r.eligible and r.reason.startswith("closed:stale")


def test_twenty_takes_in_one_tick_see_twenty_headrooms(home):
    plan = _plan(home, Fleet())
    seen = []
    for _ in range(20):
        assert plan.take("alpha").name == "ci-box"
        seen.append(_row(plan, "ci-box").mem_gb)
    assert len(set(seen)) == 20 and seen == sorted(seen, reverse=True)
    assert seen[0] - seen[-1] == pytest.approx(19 * TABLE["footprints"]["kanban"]["ram_gb"])


# -- wiring: dispatch, reservation ledger, release, the old ladder ----------

def test_sidecar_priority_order_is_not_the_pick(home):
    """ci-box is LAST in the sidecar list and still wins by default_first."""
    plan = _plan(home, Fleet())
    assert list(plan.hosts)[0] != "ci-box"
    assert plan.take("alpha").name == "ci-box"


def test_no_table_keeps_the_band_pick(home):
    (home / "config" / "fleet-hosts.json").unlink()
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    f = Fleet()
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: None,
                            probe=lambda h: kwp.probe_host(h, runner=f.runner, pressure_path=POLICY.pressure_path))
    plan = b.plan_pool([], {})
    assert plan.signal.placement_table is None
    assert plan.take("alpha") is not None and plan.last_decision is None


def test_dispatch_reserves_each_spawn_and_run_end_releases_it(home):
    plan = _plan(home, Fleet(), now=__import__("time").time())
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return FAKE_PID

    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"c{i}", assignee="alpha") for i in range(3)]
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
    assert sorted(spawned) == sorted((i, "ci-box") for i in ids)
    ledger = home / placement.RESERVATIONS_REL
    rows = json.loads(ledger.read_text(encoding="utf-8"))["rows"]
    assert sorted(r["job_id"] for r in rows) == sorted(ids) and {r["host"] for r in rows} == {"ci-box"}
    assert placement.live_reservations(ledger)["ci-box"].ram_gb == 9
    with kb.connect_closing() as conn:
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'worker_placed'",
                          (ids[0],)).fetchone()
        payload = json.loads(ev[0])
        assert payload[kh.RESERVATION_KEY] in {r["id"] for r in rows} and payload["rule"] == "default_first"
        assert kb.complete_task(conn, ids[0], summary="done")
    left = json.loads(ledger.read_text(encoding="utf-8"))["rows"]
    assert sorted(r["job_id"] for r in left) == sorted(ids[1:])
    # The next tick's pick subtracts the two live promises.
    plan2 = _plan(home, Fleet(), now=__import__("time").time())
    plan2.take("alpha")
    assert _row(plan2, "ci-box").reserved_gb == 6


def test_continuity_from_previous_placed_run(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="alpha", priority=0)
        kb._append_event(conn, tid, kwp.PLACED_EVENT, {"host": "ace-media"})
        row = conn.execute("SELECT id, priority FROM tasks WHERE id = ?", (tid,)).fetchone()
        job = kh.job_for_row(conn, row)
    assert job.continuity == "ace-media" and job.priority is None
    assert kh.job_priority(100) == 0 and kh.job_priority(95) == 1 and kh.job_priority(80) is None


class UtilFleet(Fleet):
    """Fleet whose host-pressure.json carries the utilization row (scripts/host-pressure.py fields)."""

    def text(self, h):
        doc = json.loads(super().text(h))
        doc.update({"os": "linux", "ncpu": 32, "cpu_busy": 0.30, "psi_io_some10": 0.5})
        return json.dumps(doc)


def test_utilization_take_places_on_a_measured_pool_host(home):
    """Prism a864560114d1 / e65f40aa5a69 / 14062df79154: under ``kanban.admission: utilization`` the probe's
    pressure row reaches the Sample (ncpu, cpu_busy, psi_io_some10, os), so a real take() places remotely
    instead of closing every host ``unmeasured:ncpu``."""
    (home / "config.yaml").write_text("kanban:\n  admission: utilization\n", encoding="utf-8")
    assert placement.admission_mode() == "utilization"
    plan = _plan(home, UtilFleet())
    host = plan.take("alpha")
    rows = {r.host: r for r in plan.last_decision.rows}
    assert host is not None, {h: r.reason for h, r in rows.items()}
    assert all("unmeasured" not in r.reason for r in rows.values()), {h: r.reason for h, r in rows.items()}
    picked = rows[host.name]
    assert picked.eligible and picked.cpu_busy == 0.30 and picked.need is not None
    s = kh.sample_from_detail(plan.detail[host.name])
    assert (s.ncpu, s.cpu_busy, s.psi_io_some10, s.os) == (32, 0.30, 0.5, "linux")


# -- vendored copy stays the canonical scorer -------------------------------

def test_vendored_placement_matches_canonical():
    """Cross-repo contract (like scripts/tests/test_ci_placement_ledger_adapter.py): the
    vendored module equals hermes-home's scripts/lib/placement.py except the VENDORED header
    paragraph, fleet_root() and the fork's encodings (utf-8-sig reads, encoding= on writes, the
    footgun marker). PLACEMENT_CANONICAL names the canonical file; CI's tests.yml sets it from the
    runners' hermes-home mirror. Skipped where neither exists (hosted runners, a bare dev box)."""
    # The suite's home-io guard forbids reading the real ~/.hermes, so the canonical
    # file is named explicitly: PLACEMENT_CANONICAL=<hermes-home>/scripts/lib/placement.py.
    env = os.environ.get("PLACEMENT_CANONICAL")
    if not env:
        pytest.skip("PLACEMENT_CANONICAL not set (no hermes-home checkout to cross-read)")
    src = Path(env)
    ours = Path(placement.__file__).read_text(encoding="utf-8")
    theirs = src.read_text(encoding="utf-8")
    assert "\nVENDORED from hermes-home" in ours and "\nVENDORED from" not in theirs

    def body(text: str) -> str:
        head, sep, rest = text.partition("\nVENDORED from hermes-home")
        if sep:                                   # drop the header paragraph (up to its blank line)
            text = head + rest[rest.index("\n\n") + 1:]
        start, end = text.index("def fleet_root()"), text.index("def _num(")
        text = text[:start] + text[end:]
        text = re.sub(r"  # windows-footgun: ok\b.*", "", text)
        return text.replace('encoding="utf-8-sig"', 'encoding="utf-8"').replace(', encoding="utf-8")', ")")

    assert body(ours) == body(theirs)


# -- Prism round 1 on #1941 --------------------------------------------------

def test_pool_alias_maps_back_to_the_pool_host(home):
    """Pool id ``ci`` vs table id ``ci-box`` (alias ``ci``): the pick lands on the
    pool host ``ci`` and decrements ITS slots (Prism 5c776514232a)."""
    roles = json.loads((home / "fleet" / kwp.ROLES_FILE).read_text(encoding="utf-8"))
    roles["hosts"]["ci"] = roles["hosts"].pop("ci-box")
    (home / "fleet" / kwp.ROLES_FILE).write_text(json.dumps(roles), encoding="utf-8")
    side = json.loads((home / "fleet" / kwp.SIDECAR_FILE).read_text(encoding="utf-8"))
    side["priority"] = ["ace-ai", "ace-media", "ci"]
    side["hosts"]["ci"] = side["hosts"].pop("ci-box")
    (home / "fleet" / kwp.SIDECAR_FILE).write_text(json.dumps(side), encoding="utf-8")
    f = Fleet()
    f.runner_orig = f.runner

    def runner(argv, **kw):
        if argv[-2].endswith("@ci"):
            argv = [*argv[:-2], argv[-2] + "-box", argv[-1]]
        return f.runner_orig(argv, **kw)
    f.runner = runner
    plan = _plan(home, f)
    before = plan.slots["ci"]
    assert plan.take("alpha").name == "ci"
    assert plan.last_decision.host == "ci-box" and plan.slots["ci"] == before - 1


def test_alias_and_canonical_reservations_are_summed(home):
    """Ledger rows under ``ci`` and ``ci-box`` both count (Prism e182ebcf98ea)."""
    import time as _t
    now = _t.time()
    ledger = home / placement.RESERVATIONS_REL
    placement.reserve("ci", "kanban", placement.Footprint(10.0, 1.0), ttl_s=600, path=ledger, now=now,
                      admissions_path=ledger.parent / "admissions.jsonl")
    placement.reserve("ci-box", "kanban", placement.Footprint(5.0, 1.0), ttl_s=600, path=ledger, now=now,
                      admissions_path=ledger.parent / "admissions.jsonl")
    plan = _plan(home, Fleet(), now=now)
    plan.take("alpha", job_id="t_x")
    assert _row(plan, "ci-box").reserved_gb == 15


def test_unreadable_ledger_refuses_and_is_never_overwritten(home):
    """A corrupt ledger is not 'no promises' (Prism 5c1d070683d7): take() refuses
    and reserve() leaves the bytes alone."""
    ledger = home / placement.RESERVATIONS_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{not json", encoding="utf-8")
    plan = _plan(home, Fleet())
    assert plan.take("alpha", job_id="t_x") is None and "LedgerError" in plan.refusal
    assert ledger.read_text(encoding="utf-8") == "{not json"


def test_exclusions_file_closes_a_host_and_unreadable_closes_all(home):
    """var/placement/exclusions.json is honoured; an unreadable one refuses the pick
    instead of re-opening the hosts (Prism dc299a3694c6)."""
    x = home / placement.EXCLUSIONS_REL
    x.parent.mkdir(parents=True, exist_ok=True)
    x.write_text(json.dumps({"hosts": {"ci": {"until": T0 + 600, "reason": "drain"}}}), encoding="utf-8")
    plan = _plan(home, Fleet())
    assert plan.take("alpha").name != "ci-box"
    assert _row(plan, "ci-box").reason == "excluded:drain"
    x.write_text("{oops", encoding="utf-8")
    plan = _plan(home, Fleet())
    assert plan.take("alpha") is None and "ExclusionsError" in plan.refusal


def test_take_reserves_before_launch_and_a_failed_spawn_refunds(home):
    """take() writes the reservation under the ledger lock BEFORE launch, so a
    second planner (another board) sees it; release() refunds it (Prism
    e35e13b216d4 / 62f48e3043e0)."""
    import time as _t
    now = _t.time()
    a = _plan(home, Fleet(), now=now)
    b = _plan(home, Fleet(), now=now)
    ledger = home / placement.RESERVATIONS_REL
    host = a.take("alpha", job=placement.Job(), job_id="t_a")
    assert host.name == "ci-box" and a.last_reservation
    assert [r["job_id"] for r in json.loads(ledger.read_text(encoding="utf-8"))["rows"]] == ["t_a"]
    b.take("alpha", job=placement.Job(), job_id="t_b")
    assert _row(b, "ci-box").reserved_gb == 3   # b saw a's promise without sharing a's plan
    a.release(host)                              # a's spawn failed: refund
    assert [r["job_id"] for r in json.loads(ledger.read_text(encoding="utf-8"))["rows"]] == ["t_b"]


def test_dry_run_take_never_writes_the_ledger(home):
    plan = _plan(home, Fleet())
    plan.dry_run = True
    for _ in range(3):
        assert plan.take("alpha", job=placement.Job(), job_id="t_x").name == "ci-box"
    assert not (home / placement.RESERVATIONS_REL).exists()
    assert _row(plan, "ci-box").reserved_gb == 6  # in-memory takes still count


@pytest.mark.parametrize("row", [{"host": "ci-box", "until": "9999999999"}, {"until": 9999999999}, "x"])
def test_malformed_ledger_row_refuses_and_survives(home, row):
    """A row placement.live_reservations would silently drop (non-numeric until,
    no host) refuses the pick and is never pruned (Prism r2 17b487c4d74d)."""
    ledger = home / placement.RESERVATIONS_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"schema": 1, "rows": [row]})
    ledger.write_text(body, encoding="utf-8")
    plan = _plan(home, Fleet())
    assert plan.take("alpha", job_id="t_x") is None and "LedgerError" in plan.refusal
    assert ledger.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("doc", [{"hosts": {"ci-box": {"until": "1791900000"}}}, {"ci-box": "drain"},
                                 {"hosts": ["ci-box"]}])
def test_malformed_exclusion_row_refuses(home, doc):
    """An exclusion row the scorer would skip refuses instead of re-opening the
    host (Prism r2 a5504de2ecfc)."""
    x = home / placement.EXCLUSIONS_REL
    x.parent.mkdir(parents=True, exist_ok=True)
    x.write_text(json.dumps(doc), encoding="utf-8")
    plan = _plan(home, Fleet())
    assert plan.take("alpha", job_id="t_x") is None and "ExclusionsError" in plan.refusal


# -- Apollo changes-requested on #1941: the kanban-glue P1s ------------------

def _ledger_rows(home):
    p = home / placement.RESERVATIONS_REL
    return json.loads(p.read_text(encoding="utf-8"))["rows"] if p.exists() else []


def test_pinned_take_books_the_ledger_and_the_next_admit_sees_it(home):
    """A pin keeps precedence but its footprint is a ledger row, so the next
    unpinned admit (and other boards) subtract it (Prism 4609a040ad20 /
    b8c13f6c2e7a / 9b1349450aff)."""
    import time as _t
    now = _t.time()
    plan = _plan(home, Fleet(), now=now)
    assert plan.take("alpha", pin="ace-media", job_id="t_pin").name == "ace-media"
    rows = _ledger_rows(home)
    assert [(r["host"], r["job_id"]) for r in rows] == [("ace-media", "t_pin")] and plan.last_reservation == rows[0]["id"]
    plan.take("alpha", job=placement.Job(continuity="ace-media"), job_id="t_next")
    assert _row(plan, "ace-media").reserved_gb == 3   # the pin's 3 GB, read from the ledger
    other = _plan(home, Fleet(), now=now)            # another board's plan
    other.take("alpha", job=placement.Job(continuity="ace-media"), job_id="t_other")
    assert _row(other, "ace-media").reserved_gb == 6


def test_failed_pin_refunds_only_its_own_reservation(home):
    """Unpinned take succeeds on ci-box, then a pinned take on ci-box fails to
    spawn: the refund releases the PIN's row, never the live worker's (Prism
    b711acd04da4)."""
    import time as _t
    plan = _plan(home, Fleet(), now=_t.time())
    host = plan.take("alpha", job=placement.Job(), job_id="t_live")
    assert host.name == "ci-box"
    pinned = plan.take("alpha", pin="ci-box", job_id="t_pin")
    pin_rid = plan.last_reservation
    plan.release(pinned, pin_rid)
    assert [r["job_id"] for r in _ledger_rows(home)] == ["t_live"]
    # Without the id, the newest take on the host is the one refunded.
    plan.take("alpha", pin="ci-box", job_id="t_pin2")
    plan.release(pinned)
    assert [r["job_id"] for r in _ledger_rows(home)] == ["t_live"]


def test_dry_run_copy_does_not_double_count_real_takes(home):
    """A dry-run dispatch over a plan that already admitted for real must not add
    those takes on top of the ledger rows they wrote (Prism 08bbc332020f /
    32dbcfc15ac3)."""
    import dataclasses as _dc
    import time as _t
    plan = _plan(home, Fleet(), now=_t.time())
    plan.take("alpha", job=placement.Job(), job_id="t_a")
    plan.take("alpha", pin="ci-box", job_id="t_b")
    dry = _dc.replace(plan, slots=dict(plan.slots), taken=dict(plan.taken), dry_run=True, _rids={},
                      _simulated=dict(plan._simulated))
    dry.take("alpha", job=placement.Job(), job_id="t_dry")
    assert _row(dry, "ci-box").reserved_gb == 6        # the two real rows, once each
    dry.take("alpha", job=placement.Job(), job_id="t_dry2")
    assert _row(dry, "ci-box").reserved_gb == 9        # + the first simulated take
    assert len(_ledger_rows(home)) == 2


def test_run_end_releases_only_after_commit(home):
    """_end_run inside a transaction that rolls back keeps the reservation; the
    committed end releases it (Prism 441fe743030f)."""
    import time as _t
    from hermes_cli import kanban_db_connect as kdc
    plan = _plan(home, Fleet(), now=_t.time())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: FAKE_PID, max_spawn=8, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
        assert [r["job_id"] for r in _ledger_rows(home)] == [tid]
        with pytest.raises(RuntimeError, match="boom"):
            with kdc.write_txn(conn):
                kb._end_run(conn, tid, outcome="completed")
                raise RuntimeError("boom")
        assert [r["job_id"] for r in _ledger_rows(home)] == [tid]   # rolled back: promise kept
        assert conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is not None
        with kdc.write_txn(conn):
            kb._end_run(conn, tid, outcome="completed")
            assert [r["job_id"] for r in _ledger_rows(home)] == [tid]  # not before COMMIT
        assert _ledger_rows(home) == []


@pytest.mark.parametrize("bad", [{"ram_gb": -8}, {"threads": "x"}])
def test_negative_or_non_numeric_reservation_quantity_refuses(home, bad):
    """A live -8 GB row would cancel a real 10 GB promise in the sum; a
    non-numeric one must be a PlacementError the take handles, not a crash
    (Prism 67cb39a5da3b)."""
    import time as _t
    now = _t.time()
    good = {"id": "a", "host": "ci-box", "consumer": "kanban", "ram_gb": 10, "threads": 1, "until": now + 600}
    ledger = home / placement.RESERVATIONS_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"schema": 1, "rows": [good, {**good, "id": "b", **bad}]})
    ledger.write_text(body, encoding="utf-8")
    plan = _plan(home, Fleet(), now=now)
    assert plan.take("alpha", job_id="t_x") is None and "LedgerError" in plan.refusal
    assert ledger.read_text(encoding="utf-8") == body



def test_refund_with_unknown_reservation_is_a_noop(home):
    """A refund naming a reservation this plan no longer holds (a double
    refund) frees nothing: neither the newest live row nor a slot (Prism
    8b0ce035f2c1)."""
    import time as _t
    plan = _plan(home, Fleet(), now=_t.time())
    first = plan.take("alpha", job=placement.Job(), job_id="t_first")
    first_rid = plan.last_reservation
    plan.take("alpha", job=placement.Job(), job_id="t_live")
    plan.release(first, first_rid)
    assert [r["job_id"] for r in _ledger_rows(home)] == ["t_live"]
    slots = dict(plan.slots)
    plan.release(first, first_rid)                       # the same refund again
    assert [r["job_id"] for r in _ledger_rows(home)] == ["t_live"]
    assert plan.slots == slots
    plan.release(first, "not-a-rid")
    assert [r["job_id"] for r in _ledger_rows(home)] == ["t_live"] and plan.slots == slots


def test_live_run_reservation_is_renewed_past_the_admission_ttl(home):
    """A pool run longer than RESERVE_TTL_S stays counted: the tick renews its
    row while the run is live (Prism 6c6c0c765a1a); an ended run's row is not
    renewed, and a row that lapsed under a live run is reported as a leak."""
    import time as _t
    now = _t.time()
    plan = _plan(home, Fleet(), now=now)
    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"c{i}", assignee="alpha") for i in range(2)]
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: FAKE_PID, max_spawn=8, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
    ledger = home / placement.RESERVATIONS_REL
    assert sorted(r["job_id"] for r in _ledger_rows(home)) == sorted(ids)
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: kb.connect(board=board))
    boards = [{"slug": "default"}]
    b._boards = boards
    later = now + kh.RESERVE_TTL_S - 60                  # one minute before the admission TTL
    assert b.renew_reservations(now=later) == {"renewed": 2, "lapsed": 0, "reaped": 0}
    past = now + kh.RESERVE_TTL_S + 600                  # well past the original TTL
    assert set(placement.live_reservations(ledger, now=past)) == {"ci-box"}
    assert placement.live_admissions("kanban", ledger, now=past)["ci-box"] == 2
    assert b.renew_reservations(now=later + 1) is None   # not due yet
    # Without renewal both rows would lapse; a lapsed row of a live run is a leak.
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, ids[0], summary="done")
    far = later + 2 * kh.RESERVE_TTL_S
    # ... and that lapsed row is pruned on the same pass, ledgered once as ``expired`` (allocation 2.3)
    assert b.renew_reservations(now=far) == {"renewed": 0, "lapsed": 1, "reaped": 0}
    adm = [json.loads(x) for x in (ledger.parent / placement.ADMISSIONS_REL.name).read_text(
        encoding="utf-8").splitlines()]
    assert [r["job_id"] for r in adm if r["decision"] == "expired"] == [ids[1]]
    assert not _ledger_rows(home)


def test_diagnostics_slot_count_reads_the_ledger_with_a_fixed_live_snapshot(home):
    """With placement_live set (pick_rung's snapshot) the slot cap still counts
    the ledger's live rows, so --placement agrees with admit() (Prism
    404998a66d0e)."""
    import time as _t
    now = _t.time()
    table = json.loads(json.dumps(TABLE))
    table["hosts"]["ci-box"]["consumers"]["kanban"]["slots"] = 1
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(table), encoding="utf-8")
    placement.reserve("ci-box", "kanban", placement.Footprint(3, 2), ttl_s=600,
                      path=home / placement.RESERVATIONS_REL, now=now)
    plan = _plan(home, Fleet(), now=now)
    plan.signal.placement_live = {}                      # a snapshot taken before that row
    plan.dry_run = True
    plan.take("alpha", job=placement.Job())
    assert not _row(plan, "ci-box").eligible


def test_on_commit_never_outlives_an_unmanaged_transaction(home):
    """A callback queued inside a transaction write_txn did not open (ended by
    conn.rollback()) never fires on a later, unrelated commit (Prism
    907d56fc45e5)."""
    from hermes_cli import kanban_db_connect as kdc
    fired = []
    with kb.connect_closing() as conn:
        conn.execute("BEGIN")
        with kdc.write_txn(conn, allow_nested=True):
            kdc.on_commit(conn, lambda: fired.append("stale"))
        conn.rollback()
        with kdc.write_txn(conn):
            kdc.on_commit(conn, lambda: fired.append("ok"))
        assert fired == ["ok"]
        assert id(conn) not in kdc._POST_COMMIT


# -- Prism 83b0c929b421: a non-finite clock never reaches the ledger ---------

_BAD_CLOCKS = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("bad", _BAD_CLOCKS)
@pytest.mark.parametrize("call", ["release", "renew", "reserve", "live_reservations", "live_admissions"])
def test_non_finite_now_is_refused_and_the_ledger_survives(tmp_path, call, bad):
    """``release(now=nan)`` read every row as expired and rewrote the ledger
    EMPTY, even for an unknown id; the same clock made live rows vanish from
    every reader. Any non-finite ``now`` is refused before the ledger is read,
    and the file is byte-identical afterwards."""
    led = tmp_path / "r.json"
    rid = placement.reserve("ci-box", "kanban", placement.Footprint(3, 2), ttl_s=600, path=led, now=T0,
                            admissions_path=tmp_path / "a.jsonl")
    before = led.read_bytes()
    calls = {
        "release": lambda: placement.release("unknown", path=led, now=bad),
        "renew": lambda: placement.renew(rid, ttl_s=600, path=led, now=bad),
        "reserve": lambda: placement.reserve("ci-box", "kanban", placement.Footprint(3, 2), ttl_s=600,
                                             path=led, now=bad, admissions_path=tmp_path / "a.jsonl"),
        "live_reservations": lambda: placement.live_reservations(led, now=bad),
        "live_admissions": lambda: placement.live_admissions("kanban", led, now=bad),
    }
    with pytest.raises(placement.PlacementError, match="now must be a finite"):
        calls[call]()
    assert led.read_bytes() == before
    assert placement.live_admissions("kanban", led, now=T0 + 1) == {"ci-box": 1}


@pytest.mark.parametrize("bad", _BAD_CLOCKS)
def test_non_finite_now_never_lifts_an_operator_exclusion(tmp_path, bad):
    """``until > nan`` is False: a live exclusion row read as absent (same class)."""
    ex = tmp_path / "exclusions.json"
    ex.write_text(json.dumps({"ci-box": {"until": T0 + 3600, "reason": "voice session"}}), encoding="utf-8")
    assert placement.read_exclusions(ex, now=T0) == {"ci-box": "voice session"}
    with pytest.raises(placement.PlacementError, match="now must be a finite"):
        placement.read_exclusions(ex, now=bad)
    (tmp_path / "hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    table = placement.load_hosts(tmp_path / "hosts.json")
    with pytest.raises(placement.PlacementError, match="now must be a finite"):
        placement.choose(table, "kanban", {}, now=bad, exclusions_path=ex)
    with pytest.raises(placement.PlacementError, match="stale_after_s"):
        placement.choose(table, "kanban", {}, now=T0, exclusions_path=ex, stale_after_s=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_clock_never_reads_a_stale_sample_as_fresh(bad):
    """``age > stale`` is False for a NaN age or a NaN threshold: a 10-minute-old
    pressure row came back ``fresh`` (same class: a non-finite clock fails open)."""
    doc = {"at": T0 - 600, "hosts": {"ci-box": {"at": T0 - 600, "age_s": 0, "load1": 1.0, "load5": 1.0,
                                                "mem_available_gb": 100.0, "psi_cpu_some10": 0.0,
                                                "psi_mem_some10": 0.0, "system_pct": 1.0}}}
    assert placement.samples_from_pressure(doc, now=T0)["ci-box"].status == "stale"
    with pytest.raises(placement.PlacementError, match="now must be a finite"):
        placement.samples_from_pressure(doc, now=bad)
    with pytest.raises(placement.PlacementError, match="stale_after_s"):
        placement.samples_from_pressure(doc, now=T0, stale_after_s=bad)
    with pytest.raises(placement.PlacementError, match="now must be a finite"):
        placement.sample_from_probe(1.0, 100.0, now=bad)


# -- per-profile kanban classes (t_edc220cc) ---------------------------------

def test_dispatch_books_the_assignees_profile_class(home):
    """``job_for_row`` carries the assignee, so a profile with a measured row in
    ``footprints.kanban.profiles`` is admitted and booked at ITS class; a profile
    without a row keeps the pooled kanban class."""
    table = json.loads((home / "config" / "fleet-hosts.json").read_text(encoding="utf-8"))
    table["footprints"]["kanban"]["profiles"] = {
        "alpha": {"ram_gb": 7.5, "threads": 1, "scratch_gb": 1, "gpu_mb": 0}}
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(table), encoding="utf-8")
    plan = _plan(home, Fleet(), now=__import__("time").time())
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="alpha")
        b = kb.create_task(conn, title="b", assignee="beta")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (a,)).fetchone()
        assert kh.job_for_row(conn, row).profile == "alpha"
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: FAKE_PID, max_spawn=64, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
    rows = json.loads((home / placement.RESERVATIONS_REL).read_text(encoding="utf-8"))["rows"]
    booked = {r["job_id"]: r["ram_gb"] for r in rows}
    assert booked.get(a) == 7.5
    assert b not in booked or booked[b] == TABLE["footprints"]["kanban"]["ram_gb"]


# -- Prism post-merge on #2101 (t_bc197d33) ---------------------------------

@pytest.mark.parametrize("text", [
    "kanban:\n  admission: utilization\n",
    "kanban:\n    admission: utilization\n",                       # 4-space indent (Prism 7ca95688be5a)
    "kanban: {admission: utilization}\n",                        # flow style
    "kanban: {dispatch_load_gate: {enabled: true}, admission: 'utilization'}  # c\n",
])
def test_admission_mode_reads_any_yaml_layout(tmp_path, text):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text, encoding="utf-8")
    assert placement.admission_mode(cfg) == "utilization"


def test_admission_mode_unknown_value_is_legacy_quietly(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("kanban:\n  admission: turbo\n", encoding="utf-8")
    assert placement.admission_mode(cfg) == "legacy"
    assert capsys.readouterr().err == ""


def test_admission_mode_parse_failure_is_legacy_and_loud(tmp_path, capsys):
    """Prism 7ca95688be5a: a config.yaml that does not parse reads legacy, and says so on stderr."""
    pytest.importorskip("yaml")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("kanban:\n  admission: utilization\n bad: [\n", encoding="utf-8")
    assert placement.admission_mode(cfg) == "legacy"
    err = capsys.readouterr().err
    assert "kanban.admission" in err and "does not parse" in err and str(cfg) in err


_USAGE_ROW = {"id": "r1", "host": "ace-ai", "consumer": "kanban", "threads": 2, "ram_gb": 4, "gpu_mb": 0,
              "job_id": "t_1"}


@pytest.mark.parametrize("row, actual", [
    (_USAGE_ROW, {"ram_gb": 12}),                                  # Prism dd0cbea04139: cpus unmeasured
    (_USAGE_ROW, {"cpus": 1.5}),                                   # ram unmeasured
    (dict(_USAGE_ROW, gpu_mb=8000), {"cpus": 1.5, "ram_gb": 12}),  # the lease estimates gpu; gpu unmeasured
])
def test_partial_actual_leaves_class_history_unchanged(tmp_path, capsys, row, actual):
    usage = tmp_path / "class-usage.jsonl"
    assert placement.record_usage(row, actual, now=T0, path=usage) is None
    assert not usage.exists()
    assert "class usage not recorded for r1" in capsys.readouterr().err


def test_full_actual_is_recorded(tmp_path):
    usage = tmp_path / "class-usage.jsonl"
    row = dict(_USAGE_ROW, gpu_mb=8000)
    got = placement.record_usage(row, {"cpus": 1.5, "ram_gb": 12, "gpu_mib": 6000}, now=T0, path=usage)
    assert got is not None and (got["cpus"], got["ram_gb"], got["gpu_mib"]) == (1.5, 12.0, 6000.0)
    assert len(usage.read_text(encoding="utf-8").splitlines()) == 1
