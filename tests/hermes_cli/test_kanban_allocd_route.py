"""Router Core Card B1 (t_4187c8f1): the kanban dispatcher -> alloc_client, behind
``kanban.placement.allocd`` (off | shadow | on, default off).

* off: byte-identical to the in-process scorer. The replay fixture (91 real
  decisions off the live board, ``tests/fixtures/kanban_placement/allocd_replay.json``)
  reproduces every logged {host, rule} through ``SpilloverPlan.take``, and a fake
  allocd that fails loudly sees ZERO requests (the mutation arm: forcing the gate
  open makes it fire, so the detector is live).
* on: the same fixture through ``alloc_client.admit`` against a fake allocd that
  runs allocd's /admit logic (hermes-home@58a2aeb6d3 ``do_admit``) over the same
  vendored scorer; every disagreement is PRINTED, never asserted zero (that is the
  live shadow gate's job, n >= 50 / 0 disagreements).
* shadow: both paths, the old one acts, one line per decision in
  ``<root>/var/alloc/shadow/kanban.jsonl``.

Nothing opens a non-loopback connection; the fake allocd binds 127.0.0.1:0.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_kanban_headroom_placement import (  # noqa: F401  (fixture)
    FAKE_PID, TABLE, Fleet, _plan, home,
)
from hermes_cli.fleet_placement import alloc_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli.fleet_placement import placement
from hermes_cli import allocation_policy as pp

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kanban_placement" / "allocd_replay.json"
JOB_FIELDS = tuple(f.name for f in dataclasses.fields(placement.Job))


# -- a fake allocd: allocd's /admit, /release, /renew over the vendored scorer ---------------

class FakeAllocd:
    """``scripts/allocd.py`` do_admit / do_release / do_renew (hermes-home@58a2aeb6d3)
    over THIS checkout's placement + alloc_client, with an injected clock (the replay
    decides at the logged ``at``). ``loud=True`` answers every POST 500 and counts it:
    the off path must never reach it."""

    def __init__(self, *, clock=time.time, loud: bool = False):
        self.clock = clock
        self.loud = loud
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append((self.path, req))
                code, body = (500, {"error": "loud fake allocd: the off path must not ask"}) if fake.loud \
                    else fake.route(self.path, req)
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def route(self, path, req):
        if path == "/admit":
            return self.admit(req)
        if path == "/release":
            refused = None
            if req.get("actual") is not None:   # do_release: a refused actual still ends the lease, unrecorded
                try:
                    ok = placement.release(req["reservation_id"], owner=req["consumer"], actual=req["actual"],
                                           actual_source=req.get("actual_source"))
                except placement.ActualRefused as exc:
                    refused = str(exc)
                    ok = placement.release(req["reservation_id"], owner=req["consumer"])
            else:
                ok = placement.release(req["reservation_id"])
            if not ok:
                return 410, {"gone": True}
            return 200, {"released": True, **({"actual_refused": refused} if refused else {})}
        if path == "/demand/publish":       # the pair door's enqueue / withdraw / queue verbs (t_dd7a1b7d)
            return 200, {"schema": 1, "workload": req.get("workload"), "host": req.get("host"), "ok": True,
                         "door": {"at": self.clock()}}
        if path == "/demand/authorize":
            return self.authorize(req)
        if path == "/renew":
            ok = placement.renew(req["reservation_id"], ttl_s=req.get("ttl_s", 1800.0), now=self.clock())
            return (200, {"renewed": True}) if ok else (410, {"gone": True})
        return 404, {"error": "not found"}

    def admit(self, req):
        now = self.clock()
        table = placement.load_hosts()
        samples = alloc_client.read_samples(now=now)
        cfg = json.loads((placement.fleet_root() / placement.TABLE_REL).read_text(encoding="utf-8")).get("router") or {}
        extra = {} if req.get("class") == alloc_client.P0 else {
            h: placement.Footprint.of(fp) for h, fp in (cfg.get("p0_reserve") or {}).items()
            if not h.startswith("_") and isinstance(fp, dict)}
        job_kw = {k: v for k, v in (req.get("job") or {}).items() if k in JOB_FIELDS}
        job = placement.Job(**job_kw) if job_kw else None
        cands = req.get("candidates")
        if req.get("shadow") is True:
            resv = placement.live_reservations(now=now, table=table)
            for k, v in extra.items():
                hid = table.resolve(k) or k
                resv[hid] = resv[hid].plus(v) if hid in resv else v
            d = placement.choose(table, req["consumer"], samples, reservations=resv, now=now, candidates=cands,
                                 admitted=placement.live_admissions(req["consumer"], now=now, table=table),
                                 footprint=req.get("footprint"), job=job, previous=req.get("previous"))
            return 200, {"shadow": True, "host": d.host, "reason": d.reason, "rule": d.rule}
        d, rid = placement.admit(table, req["consumer"], samples, ttl_s=req.get("ttl_s", 1800.0), job_id=req["job_id"],
                                 footprint=req.get("footprint"), job=job, previous=req.get("previous"),
                                 reuse_job=True, extra_reserved=extra, candidates=cands, now=now)
        if d.host and req.get("unified") is True:
            ok, why = self.confirm_class(table, req["consumer"], job, rid, samples, now=now,
                                         stale_after_s=float(req.get("stale_after_s") or placement.DEFAULT_STALE_AFTER_S))
            if not ok:
                return 409, {"refused": True, "reason": f"unified_admission: {why}"}
        if d.host:
            out = {"host": d.host, "reservation_id": rid, "rule": d.rule, "band": None}
            if req.get("unified") is True:
                out["unified"] = True
            return 200, out
        return 409, {"refused": True, "reason": d.reason}

    def authorize(self, req):
        """allocd's door authorize with an existing capacity lease: the SAME row is retagged to the pair identity
        ``<workload>@<host>`` (its release then names that owner) and its id comes back."""
        rid, owner = req.get("existing_reservation_id"), f"{req['workload']}@{req['host']}"
        rpath = placement.reservations_path()
        with placement._locked(rpath):
            doc = placement._read_ledger(rpath)
            row = next((r for r in doc["rows"] if r.get("id") == rid), None)
            if row is None:
                return 409, {"reason": "capacity", "detail": f"no live row {rid}"}
            row["consumer"] = owner
            placement._write_ledger(rpath, doc)
        return 200, {"schema": 1, "workload": req["workload"], "host": req["host"], "id": req["id"],
                     "reservation_id": rid, "until": self.clock() + 90, "reused": False}

    @staticmethod
    def confirm_class(table, consumer, job, rid, samples, *, now, stale_after_s):
        """``scripts/allocd.py`` _confirm_class (hermes-home, t_c466b7ee): placement.may_start on THIS ledger under
        its lock with the booked row left out by id; refused = the row is dropped in the same lock section."""
        rpath = placement.reservations_path()
        with placement._locked(rpath):
            doc = placement._read_ledger(rpath)
            own = next((r for r in doc["rows"] if r.get("id") == rid), None)
            if own is None or not placement._row_live(own, now):
                return False, f"closed:reservation {rid} not live at the class check"
            hid = table.resolve(own["host"]) or own["host"]
            live = [r for r in doc["rows"] if placement._row_live(r, now)]
            leases = placement.ledger_leases(rpath, now=now, table=table)
            del leases[next(i for i, r in enumerate(live) if r.get("id") == rid)]
            admitted = placement.live_admissions(consumer, rpath, now=now, table=table).get(hid, 0) - 1
            ok, why = placement.may_start(hid, consumer, job=job, table=table, now=now, samples=samples,
                                          leases=leases, admitted=admitted, stale_after_s=stale_after_s)
            if not ok:
                placement._write_ledger(rpath, {"schema": doc.get("schema", placement.SCHEMA),
                                                "rows": [r for r in doc["rows"] if r.get("id") != rid]})
            return ok, why


@pytest.fixture
def allocd(home, monkeypatch):
    fake = FakeAllocd()
    monkeypatch.setenv("ALLOCD_URL", fake.url)
    yield fake
    fake.close()


def _pressure(home, hosts, *, at):
    """var/fleet-pressure.json, the vector allocd reads (fresh rows only)."""
    doc = {"schema": 1, "at": at, "stale_after_s": 60.0, "hosts": {
        h: {"status": "fresh", "age_s": 0.0, "at": at, "load1": v["load"], "load5": v["load"],
            "mem_available_gb": v["mem"], "psi_cpu_some10": 0.0, "psi_mem_some10": 0.0, "system_pct": 0.0}
        for h, v in hosts.items()}}
    p = home / alloc_client.PRESSURE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc), encoding="utf-8")


# -- replay ------------------------------------------------------------------------------------

def _replay_plan(home, dec, table_doc, mode):
    """One pool plan rebuilt from a logged decision: eligible / no_headroom hosts carry the
    logged sample (load, MemAvailable) and their kanban slots; every other host is closed for
    the reason the scorer logged (slots 0, no sample: the probe or the band refused it)."""
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(table_doc), encoding="utf-8")
    table = placement.load_hosts(home / "config" / "fleet-hosts.json")
    shutil.rmtree(home / "var", ignore_errors=True)
    at = dec["at"]
    rpath = home / placement.RESERVATIONS_REL
    for h, v in dec["hosts"].items():
        if v["reserved_gb"] or v["reserved_threads"]:
            placement.reserve(h, "replay", placement.Footprint(v["reserved_gb"], v["reserved_threads"]),
                              ttl_s=86400, job_id=f"seed-{h}", path=rpath, now=at - 1)
    hosts, slots, detail, measured = [], {}, {}, {}
    for i, (h, v) in enumerate(sorted(dec["hosts"].items())):
        n = table.hosts[table.resolve(h)].ceiling_for("kanban") or 1
        hosts.append(kwp.PoolHost(name=h, ssh_host=h, ssh_user="kanbanw", slots=n, capacity_pct=100.0,
                                  absence="optional", profiles=(dec["assignee"],), state="active",
                                  enabled=True, priority=i))
        live = v["eligible"] or (v["reason"] or "").startswith("no_headroom")
        if live:
            measured[h] = {"load": v["load"], "mem": v["memavail_gb"]}
            slots[h] = n
            detail[h] = {"slots": n, "running": 0, "load1": v["load"], "load5": v["load"],
                         "mem_available_gb": v["memavail_gb"], "psi_cpu_some10": 0.0, "psi_mem_some10": 0.0,
                         "system_pct": None, "sample_at": at, "sample_status": "fresh"}
        else:
            slots[h] = 0
            detail[h] = {"slots": n, "running": 0, "load1": None,
                         "pressure": (v["reason"] or "closed").split(":", 1)[-1]}
    _pressure(home, measured, at=at)
    sig = kwp.TargetSignal(policy=pp.PlacementPolicy(), ledger_dir=home / "var" / "placement", clock=lambda: at, placement_table=table,
                           placement_reservations=rpath, allocd=mode)
    return kwp._finish_plan(hosts, slots, detail, (), at, None, sig)


def _job(dec):
    return placement.Job(continuity=dec["continuity"], priority=kh.job_priority(dec["priority"]),
                         profile=dec["assignee"])


def _replay(home, mode, *, on_router=None, fake=None):
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    out = []
    for dec in fx["decisions"]:
        doc = json.loads(json.dumps(fx["tables"][dec["table"]]))
        if on_router is not None:
            doc["router"] = on_router
        plan = _replay_plan(home, dec, doc, mode)
        if fake is not None:
            fake.clock = (lambda at: lambda: at)(dec["at"])   # allocd decides at the logged instant
        host = plan.take(dec["assignee"], job=_job(dec), job_id=dec["job_id"])
        d = plan.last_decision
        out.append((dec, host.name if host else None, getattr(d, "rule", None) if host else plan.refusal))
    return fx, out


def test_replay_fixture_is_real_and_big_enough():
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(fx["decisions"]) >= 50
    assert len({d["job_id"] for d in fx["decisions"]}) >= 50
    assert all(d["table"] in fx["tables"] for d in fx["decisions"])


def test_flag_off_replay_reproduces_every_logged_decision(home, monkeypatch):
    """off: every one of the >= 50 real decisions comes out of take() as logged, AND a
    loud fake allocd sees zero requests (alloc_client is never consulted)."""
    loud = FakeAllocd(loud=True)
    monkeypatch.setenv("ALLOCD_URL", loud.url)
    try:
        fx, got = _replay(home, kh.ALLOCD_OFF)
    finally:
        loud.close()
    wrong = [(d["job_id"], d["old"], h, r) for d, h, r in got if (h, r) != (d["old"]["host"], d["old"]["rule"])]
    assert not wrong, wrong
    assert len(got) == len(fx["decisions"]) >= 50
    assert loud.requests == []
    assert not (home / kh.SHADOW_REL).exists()


def test_mutation_gate_open_reaches_the_loud_allocd(home, monkeypatch):
    """Mutation arm: with the mode gate forced open (shadow) the SAME replay hits the loud
    fake on the first decision, so the zero-request assertion above can go red."""
    loud = FakeAllocd(loud=True)
    monkeypatch.setenv("ALLOCD_URL", loud.url)
    monkeypatch.setattr(kh, "plan_allocd_mode", lambda plan: kh.ALLOCD_SHADOW)
    try:
        fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
        dec = fx["decisions"][0]
        plan = _replay_plan(home, dec, fx["tables"][dec["table"]], kh.ALLOCD_OFF)
        plan.take(dec["assignee"], job=_job(dec), job_id=dec["job_id"])
    finally:
        loud.close()
    assert loud.requests and loud.requests[0][0] == "/admit"


def test_flag_on_replay_routes_through_alloc_client_and_lists_disagreements(home, allocd, capsys):
    """on: the same >= 50 decisions through alloc_client.admit -> the fake allocd (the live
    router block, p0 reserve included). Every take is booked BY allocd; disagreements are
    listed, not asserted zero."""
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    router = fx["tables"][max(fx["tables"], key=lambda k: "router" in fx["tables"][k])].get("router")
    _, got = _replay(home, kh.ALLOCD_ON, on_router=router, fake=allocd)
    admits = [r for p, r in allocd.requests if p == "/admit"]
    assert len(admits) == len(got) >= 50
    assert all(r["consumer"] == "kanban" and r["job_id"] for r in admits)
    dis = [(d["job_id"], f"{d['old']['host']}/{d['old']['rule']}", f"{h}/{r}") for d, h, r in got
           if (h, r) != (d["old"]["host"], d["old"]["rule"])]
    print(f"\nallocd replay: {len(got)} decisions, {len(got) - len(dis)} agree, {len(dis)} disagree")
    for jid, old, new in dis:
        print(f"  DISAGREE {jid}: old {old} -> allocd {new}")
    out = capsys.readouterr().out
    with capsys.disabled():
        print(out)


# -- unit: the knob ----------------------------------------------------------------------------

@pytest.mark.parametrize("cfg,want", [
    ({}, "off"), ({"placement": {}}, "off"), ({"placement": {"allocd": "off"}}, "off"),
    ({"placement": {"allocd": False}}, "off"), ({"placement": {"allocd": None}}, "off"),
    ({"placement": {"allocd": "shadow"}}, "shadow"), ({"placement": {"allocd": " Shadow "}}, "shadow"),
    ({"placement": {"allocd": "on"}}, "on"), ({"placement": {"allocd": True}}, "on"),
    ({"placement": {"allocd": "yes-please"}}, "off"), ({"placement": {"allocd": 1}}, "off"),
])
def test_allocd_mode(cfg, want):
    assert kh.allocd_mode(cfg) == want


def test_default_config_ships_off():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert kh.allocd_mode(DEFAULT_CONFIG["kanban"]) == kh.ALLOCD_OFF


def test_gate_tick_reads_the_knob_per_tick(home):
    from gateway import kanban_gate_tick as kgt
    gate = type("G", (), {"cost": 2.0})()
    for raw, want in (("shadow", "shadow"), ("on", "on"), (None, "off")):
        cfg = {"placement": {"allocd": raw}} if raw else {}
        b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: cfg,
                                ledger=lambda boards: {}, connect=lambda board=None: None)
        assert b.target_signal(cfg).allocd == want


# -- unit: shadow / on through the real probe plan ---------------------------------------------

def _pressure_from_fleet(home, f):
    _pressure(home, {h: {"load": f.load[h], "mem": f.mem[h]} for h in f.load}, at=time.time())


def test_shadow_old_path_acts_and_logs_one_line(home, allocd):
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_SHADOW
    host = plan.take("alpha", job_id="t_shadow1")
    assert host.name == "ci-box"
    # the in-process ledger booked it, allocd only scored it
    assert plan.reservation_via(plan.last_reservation) is None
    assert [r["shadow"] for p, r in allocd.requests if p == "/admit"] == [True]
    rows = placement._read_ledger(home / placement.RESERVATIONS_REL)["rows"]
    assert [r["id"] for r in rows] == [plan.last_reservation]
    lines = [json.loads(x) for x in (home / kh.SHADOW_REL).read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == {"at", "consumer", "job_id", "old_host", "new_host", "agree", "reason", "old_rule",
                         "new_rule", "candidates"}
    assert line["job_id"] == "t_shadow1" and line["consumer"] == "kanban"
    assert line["old_host"] == line["new_host"] == "ci-box" and line["agree"] is True
    assert line["old_rule"] == line["new_rule"] == "default_first"
    assert sorted(line["candidates"]) == ["ace-ai", "ace-media", "ci-box"]


def test_shadow_disagreement_is_named(home, allocd):
    """allocd closes ci-box (its vector says ci-box is out of memory); the probe says it is fine."""
    f = Fleet()
    _pressure_from_fleet(home, f)
    doc = json.loads((home / alloc_client.PRESSURE_REL).read_text(encoding="utf-8"))
    doc["hosts"]["ci-box"]["mem_available_gb"] = 10.0
    (home / alloc_client.PRESSURE_REL).write_text(json.dumps(doc), encoding="utf-8")
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_SHADOW
    assert plan.take("alpha", job_id="t_shadow2").name == "ci-box"
    line = json.loads((home / kh.SHADOW_REL).read_text(encoding="utf-8").splitlines()[-1])
    assert line["agree"] is False and line["old_host"] == "ci-box" and line["new_host"] != "ci-box"
    assert line["reason"] == f"old ci-box vs new {line['new_host']}"


def test_shadow_with_allocd_down_records_no_comparison(home, monkeypatch):
    monkeypatch.setenv("ALLOCD_URL", "http://127.0.0.1:9")
    f = Fleet()
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_SHADOW
    assert plan.take("alpha", job_id="t_shadow3").name == "ci-box"
    line = json.loads((home / kh.SHADOW_REL).read_text(encoding="utf-8").splitlines()[-1])
    assert line["agree"] is None and line["new_host"] is None
    assert line["reason"].startswith("allocd_unreachable")
    # shadow never books through the fallback: only the old path's row is in the ledger
    assert len(placement._read_ledger(home / placement.RESERVATIONS_REL)["rows"]) == 1


def test_on_honours_allocd_host_and_reservation(home, allocd):
    f = Fleet()
    _pressure_from_fleet(home, f)
    doc = json.loads((home / alloc_client.PRESSURE_REL).read_text(encoding="utf-8"))
    doc["hosts"]["ci-box"]["mem_available_gb"] = 10.0      # allocd's vector closes ci-box
    (home / alloc_client.PRESSURE_REL).write_text(json.dumps(doc), encoding="utf-8")
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    host = plan.take("alpha", job_id="t_on1")
    admits = [r for p, r in allocd.requests if p == "/admit"]
    assert len(admits) == 1 and "shadow" not in admits[0]
    assert host.name == plan.last_decision.host != "ci-box"
    rid = plan.last_reservation
    assert rid == plan.last_decision.reservation_id and plan.reservation_via(rid) == kh.VIA_ALLOC_CLIENT
    rows = placement._read_ledger(home / placement.RESERVATIONS_REL)["rows"]
    assert [(r["id"], r["host"]) for r in rows] == [(rid, host.name)]
    assert rows[0]["job_id"].startswith("t_on1#") and admits[0]["job_id"] == rows[0]["job_id"]
    # refund goes back through alloc_client -> allocd /release
    plan.release(host, rid)
    assert ("/release", {"consumer": "kanban", "reservation_id": rid}) in allocd.requests
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)


def test_on_refusal_is_final_and_named(home, allocd):
    f = Fleet()
    _pressure_from_fleet(home, f)
    doc = json.loads((home / alloc_client.PRESSURE_REL).read_text(encoding="utf-8"))
    for h in doc["hosts"].values():
        h["mem_available_gb"] = 1.0
    (home / alloc_client.PRESSURE_REL).write_text(json.dumps(doc), encoding="utf-8")
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    assert plan.take("alpha", job_id="t_on2") is None
    assert plan.refusal == "pool_unavailable (allocd: no_headroom)"
    assert not placement._read_ledger(home / placement.RESERVATIONS_REL)["rows"]


def test_on_candidates_are_filtered_before_allocd(home, allocd):
    """host_bound / reach ``only`` and the assignee's profile list are applied BEFORE allocd."""
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    assert plan.take("alpha", only=("ace-media",), job_id="t_on3").name == "ace-media"
    assert [r["candidates"] for p, r in allocd.requests if p == "/admit"] == [["ace-media"]]
    assert plan.take("nobody", job_id="t_on4") is None and plan.refusal == "pool_full"
    assert len([1 for p, _ in allocd.requests if p == "/admit"]) == 1


def test_on_band_closed_host_is_allocds_call(home, allocd):
    """The probe's pressure band closed ace-media (slots 0) but allocd's vector admits it:
    the structural slot is free, so allocd decides, and the refund restores no slot."""
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.slots["ace-media"] = 0
    plan.signal.allocd = kh.ALLOCD_ON
    host = plan.take("alpha", only=("ace-media",), job_id="t_on5")
    assert host.name == "ace-media" and plan.slots["ace-media"] == 0
    assert plan.budget > 0
    plan.release(host, plan.last_reservation)
    assert plan.slots["ace-media"] == 0


def test_dry_run_never_consults_allocd(home, monkeypatch):
    loud = FakeAllocd(loud=True)
    monkeypatch.setenv("ALLOCD_URL", loud.url)
    try:
        plan = _plan(home, Fleet(), now=time.time())
        for mode in (kh.ALLOCD_SHADOW, kh.ALLOCD_ON):
            plan.signal.allocd = mode
            plan.dry_run = True
            assert plan.take("alpha") is not None
    finally:
        loud.close()
    assert loud.requests == []


# -- the dispatcher seam: worker_placed carries the route, run end releases through it ----------

def test_dispatch_on_marks_the_reservation_and_run_end_releases_through_alloc_client(home, allocd):
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON

    def spawn(task, workspace, *, board=None, placement=None):
        return FAKE_PID

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=4, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        rid, via = kh.run_reservation(conn, tid, run_id)
        assert rid and via == kh.VIA_ALLOC_CLIENT
        assert kh.running_reservations(conn) == [(tid, rid, kh.VIA_ALLOC_CLIENT)]
        # the gate tick's renewal goes to allocd /renew
        got = kh.renew_running([{"slug": "default"}], lambda board=None: kb.connect(board=board),
                               home / placement.RESERVATIONS_REL)
        assert got == {"renewed": 1, "lapsed": 0}
        assert ("/renew", {"consumer": "kanban", "reservation_id": rid, "ttl_s": kh.RESERVE_TTL_S}) \
            in allocd.requests
        kb.complete_task(conn, tid, summary="done")
    assert ("/release", {"consumer": "kanban", "reservation_id": rid}) in allocd.requests
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)


def test_on_retry_of_a_card_never_reuses_the_previous_runs_live_row(home, allocd):
    """Prism 2de4f2d22204: the idempotency key is per admission, not per card. A card
    retried while its previous run's reservation is still live (release failed / in
    flight) books a NEW row; releasing the old one leaves the new one live."""
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    assert plan.take("alpha", job_id="t_retry") is not None
    first = plan.last_reservation
    assert plan.take("alpha", job_id="t_retry") is not None
    second = plan.last_reservation
    assert first and second and first != second
    keys = [r["job_id"] for p, r in allocd.requests if p == "/admit"]
    assert len(keys) == 2 and keys[0] != keys[1] and all(k.startswith("t_retry#") for k in keys)
    assert kh.release_alloc(first)                       # the previous run's late release
    live = placement._read_ledger(home / placement.RESERVATIONS_REL)["rows"]
    assert [r["id"] for r in live if r["until"] > time.time()] == [second]


def test_admission_key_is_stable_inside_one_admit_across_the_fallback(home, monkeypatch):
    """The one key alloc_client.admit gets is the one its fallback reuses: allocd
    unreachable -> the fallback books under that same key (one row, no double admit)."""
    seen = []
    real = alloc_client._fallback_admit

    def spy(consumer, **kw):
        seen.append(kw["job_id"])
        return real(consumer, **kw)

    monkeypatch.setenv("ALLOCD_URL", "http://127.0.0.1:9")          # refused: a liveness fault
    monkeypatch.setattr(alloc_client, "_fallback_admit", spy)
    calls = []
    real_admit = alloc_client.admit

    def admit_spy(consumer, **kw):
        calls.append(kw["job_id"])
        return real_admit(consumer, **kw)

    monkeypatch.setattr(alloc_client, "admit", admit_spy)
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    plan.take("alpha", job_id="t_fb")
    assert len(calls) == 1 and calls[0].startswith("t_fb#")
    assert seen == calls                                  # the fallback replays the SAME key


def test_replacement_reservation_survives_a_restart(home, allocd, monkeypatch):
    """Prism 43f676a1839e: alloc_client re-admits an expired fallback row under a NEW id
    and aliases it only in-process (_REPLACED). renew_running persists old -> new on the
    run, so after a restart (alias map empty) renewal and the run-end release target the
    replacement, not the obsolete id."""
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    rpath = home / placement.RESERVATIONS_REL

    def spawn(task, workspace, *, board=None, placement=None):
        return FAKE_PID

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=4, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        old, via = kh.run_reservation(conn, tid, run_id)
        assert old and via == kh.VIA_ALLOC_CLIENT
        row = [r for r in placement._read_ledger(rpath)["rows"] if r["id"] == old][0]
        # what alloc_client._readmit does: the old row is gone, a new one books the job, alias old -> new
        new = placement.reserve(row["host"], "kanban", placement._row_footprint(row),
                                ttl_s=60, job_id=row["job_id"], path=rpath)
        placement.release(old, path=rpath)

        def readmitting_renew(rid, **kw):
            assert rid == old
            alloc_client._REPLACED[old] = new
            return True

        with monkeypatch.context() as m:
            m.setattr(alloc_client, "renew", readmitting_renew)
            got = kh.renew_running([{"slug": "default"}], lambda board=None: kb.connect(board=board), rpath)
        assert got == {"renewed": 1, "lapsed": 0}
        alloc_client._REPLACED.clear()                    # the gateway restarts: the alias map is gone
        alloc_client._OWNER.clear()
        assert kh.run_reservation(conn, tid, run_id) == (new, kh.VIA_ALLOC_CLIENT)
        assert kh.running_reservations(conn) == [(tid, new, kh.VIA_ALLOC_CLIENT)]
        allocd.requests.clear()
        got = kh.renew_running([{"slug": "default"}], lambda board=None: kb.connect(board=board), rpath)
        assert got == {"renewed": 1, "lapsed": 0}
        assert ("/renew", {"consumer": "kanban", "reservation_id": new, "ttl_s": kh.RESERVE_TTL_S}) \
            in allocd.requests
        kb.complete_task(conn, tid, summary="done")
    assert ("/release", {"consumer": "kanban", "reservation_id": new}) in allocd.requests
    assert not placement.live_reservations(rpath)


@pytest.mark.parametrize("outcome", ["false", "raises"])
def test_replacement_set_on_a_failed_renew_is_persisted(home, allocd, monkeypatch, outcome):
    """Prism 82b5db46c257: alloc_client._readmit aliases old -> new and THEN can fail
    (``renew_not_extended`` -> False, or a Refused out of _extend -> PlacementError).
    The replacement row is live either way, so renew_running persists the alias on every
    outcome; after a restart the run-end release reaches the replacement, not the dead id."""
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    rpath = home / placement.RESERVATIONS_REL

    def spawn(task, workspace, *, board=None, placement=None):
        return FAKE_PID

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="c", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=4, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        old, _via = kh.run_reservation(conn, tid, run_id)
        row = [r for r in placement._read_ledger(rpath)["rows"] if r["id"] == old][0]
        new = placement.reserve(row["host"], "kanban", placement._row_footprint(row),
                                ttl_s=60, job_id=row["job_id"], path=rpath)
        placement.release(old, path=rpath)

        def failing_readmit(rid, **kw):
            alloc_client._REPLACED[old] = new             # _readmit aliases first ...
            if outcome == "raises":
                raise alloc_client.Refused("token unreadable")
            return False                                  # ... then renew_not_extended

        with monkeypatch.context() as m:
            m.setattr(alloc_client, "renew", failing_readmit)
            got = kh.renew_running([{"slug": "default"}], lambda board=None: kb.connect(board=board), rpath)
        assert got["renewed"] == 0
        alloc_client._REPLACED.clear()                    # the gateway restarts
        alloc_client._OWNER.clear()
        assert kh.run_reservation(conn, tid, run_id) == (new, kh.VIA_ALLOC_CLIENT)
        assert kh.running_reservations(conn) == [(tid, new, kh.VIA_ALLOC_CLIENT)]
        allocd.requests.clear()
        kb.complete_task(conn, tid, summary="done")
    assert ("/release", {"consumer": "kanban", "reservation_id": new}) in allocd.requests
    assert not placement.live_reservations(rpath)


def test_dispatch_off_worker_placed_has_no_route_marker(home, monkeypatch):
    loud = FakeAllocd(loud=True)
    monkeypatch.setenv("ALLOCD_URL", loud.url)
    plan = _plan(home, Fleet(), now=time.time())

    def spawn(task, workspace, *, board=None, placement=None):
        return FAKE_PID

    try:
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="c", assignee="alpha")
            kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=4, spawn_paused=None, spawn_limit=0,
                              spillover=plan, reconcile_orphans=False)
            payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                                              (tid, "worker_allocated")).fetchone()[0])
            kb.complete_task(conn, tid, summary="done")
    finally:
        loud.close()
    assert kh.RESERVATION_KEY in payload and kh.RESERVATION_VIA_KEY not in payload
    assert loud.requests == []



# -- t_26acfaa2: the run-end release carries the run's measured use (FWA footprints) ---------------

def _dispatch_on(home, *, door_on=False):
    f = Fleet()
    _pressure_from_fleet(home, f)
    plan = _plan(home, f, now=time.time())
    plan.signal.allocd = kh.ALLOCD_ON
    conn = kb.connect()
    tid = kb.create_task(conn, title="c", assignee="alpha")
    kbd.dispatch_once(conn, spawn_fn=lambda task, workspace, *, board=None, placement=None: FAKE_PID,
                      max_spawn=4, spawn_paused=None, spawn_limit=0, spillover=plan, reconcile_orphans=False)
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    rid, via = kh.run_reservation(conn, tid, run_id)
    # with the door on the run's lease is the door's (run_reservation hands it no rid: _end_run leaves it alone)
    assert (rid, via) == (None, None) if door_on else (rid and via == kh.VIA_ALLOC_CLIENT)
    placed = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'worker_allocated'",
                                     (tid,)).fetchone()[0])
    assert placed["target"].endswith(f"@{placed['host']}") and not placed["target"].startswith("kanbanw@")
    return conn, tid, run_id, rid, placed["host"], placed["target"]   # target: the owner login the pool row names


def _releases(allocd, rid):
    return [r for p, r in allocd.requests if p == "/release" and r["reservation_id"] == rid]


def _usage_rows(home):
    p = placement.class_usage_path()
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def test_run_end_release_carries_actual_source_and_host(home, allocd, monkeypatch):
    seen = []

    def measure(fp):
        seen.append(dict(fp))
        return {"host": fp["host"], "cpus": 0.4, "ram_gb": 1.25, "gpu_mib": 0.0, "cpu_s": 120.0, "wall_s": 300.0}

    monkeypatch.setattr(kh, "measure_run_footprint", measure)
    conn, tid, run_id, rid, host, target = _dispatch_on(home)
    try:
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()
    assert seen == [{"host": host, "target": target, "port": 22, "run_id": run_id}]
    [req] = _releases(allocd, rid)
    assert req["actual_source"] == kh.FOOTPRINT_SOURCE == "cgroup"
    assert req["actual"]["host"] == host and req["actual"]["ram_gb"] == 1.25
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)
    [row] = _usage_rows(home)
    assert row["host"] == host and row["ram_gb"] == 1.25


def test_measurement_failure_still_releases_without_actual(home, allocd, monkeypatch, caplog):
    def measure(fp):
        raise RuntimeError("no live ssh master")

    monkeypatch.setattr(kh, "measure_run_footprint", measure)
    conn, tid, run_id, rid, host, _target = _dispatch_on(home)
    with caplog.at_level("INFO", logger=kh.__name__):
        try:
            kb.complete_task(conn, tid, summary="done")
        finally:
            conn.close()
    assert _releases(allocd, rid) == [{"consumer": "kanban", "reservation_id": rid}]
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)
    assert [m for m in caplog.messages if "carries no actual" in m and "no live ssh master" in m]
    assert _usage_rows(home) == []


def test_host_mismatch_is_the_servers_refusal_not_a_crash(home, allocd, monkeypatch, caplog):
    monkeypatch.setattr(kh, "measure_run_footprint",
                        lambda fp: {"host": "some-other-box", "cpus": 0.4, "ram_gb": 1.0, "gpu_mib": 0.0})
    conn, tid, run_id, rid, host, _target = _dispatch_on(home)
    with caplog.at_level("WARNING", logger=kh.__name__):
        try:
            kb.complete_task(conn, tid, summary="done")
        finally:
            conn.close()
    [req] = _releases(allocd, rid)
    assert req["actual"]["host"] == "some-other-box"
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)   # the lease still ended
    assert _usage_rows(home) == []                                              # the measurement did not land
    assert [m for m in caplog.messages if "actual not recorded" in m]


# -- t_dd7a1b7d: the path live spawns take. With the door on (fcfs), the start upgrades the capacity lease to
# kanban@<host> and writes door_authorized; _end_run then leaves the lease to the door, and the NEXT dispatcher
# tick's lifecycle sweep (kanban_priority_door.maintain) is what releases it. That release must carry the actual.

def _door_on(home):
    (home / "fleet" / "admission-priority.json").write_text(json.dumps({"schema": 1, "priority_mode": "fcfs"}),
                                                           encoding="utf-8")
    d = home / alloc_client.TOKENS_REL
    d.mkdir(parents=True, exist_ok=True)
    for name in ("kanban", "asap-operator", "test-gate", "test-gate-local-venue",
                 *(f"kanban@{h}" for h in TABLE["hosts"])):
        (d / name).write_text(f"tok-{name.replace('@', '-')}\n", encoding="utf-8")


def _door_run_ended(home, monkeypatch, measure):
    """A pool-host spawn through the REAL dispatch tick with the door on, its run completed, then one more tick."""
    from hermes_cli import kanban_priority_door as door
    _door_on(home)
    assert door.enabled()
    monkeypatch.setattr(kh, "measure_run_footprint", measure)
    conn, tid, run_id, _cap_rid, host, target = _dispatch_on(home, door_on=True)
    try:
        grant = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=?",
                                        (tid, run_id, door.EVENT)).fetchone()[0])
        kb.complete_task(conn, tid, summary="done")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: FAKE_PID, max_spawn=4, spawn_paused=None, spawn_limit=0,
                          reconcile_orphans=False)
        released = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND run_id=? AND kind=?",
                                (tid, run_id, door.RELEASED)).fetchone()[0]
    finally:
        conn.close()
    return tid, run_id, grant, host, target, released


def test_door_run_release_carries_actual_through_the_lifecycle_sweep(home, allocd, monkeypatch):
    seen = []

    def measure(fp):
        seen.append(dict(fp))
        return {"host": fp["host"], "cpus": 0.4, "ram_gb": 1.25, "gpu_mib": 0.0, "cpu_s": 120.0, "wall_s": 300.0}

    tid, run_id, grant, host, target, released = _door_run_ended(home, monkeypatch, measure)
    assert grant["consumer"] == f"kanban@{host}"
    assert seen == [{"host": host, "target": target, "port": 22, "run_id": run_id}]
    [req] = _releases(allocd, grant["reservation_id"])
    assert req["consumer"] == f"kanban@{host}"
    assert req["actual_source"] == kh.FOOTPRINT_SOURCE == "cgroup"
    assert req["actual"]["host"] == host and req["actual"]["ram_gb"] == 1.25
    assert released == 1
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)
    [row] = _usage_rows(home)
    assert row["host"] == host and row["ram_gb"] == 1.25
    assert row["class"] == placement.job_class_for("kanban")


def test_door_run_measurement_failure_still_releases(home, allocd, monkeypatch, caplog):
    def measure(fp):
        raise RuntimeError("no live ssh master")

    with caplog.at_level("INFO"):
        tid, run_id, grant, host, _target, released = _door_run_ended(home, monkeypatch, measure)
    assert _releases(allocd, grant["reservation_id"]) == [
        {"consumer": f"kanban@{host}", "reservation_id": grant["reservation_id"]}]
    assert released == 1
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)
    assert [m for m in caplog.messages if "carries no actual" in m and "no live ssh master" in m]
    assert _usage_rows(home) == []


def test_door_run_refused_actual_is_a_warning_and_the_lease_ends(home, allocd, monkeypatch, caplog):
    measure = lambda fp: {"host": "some-other-box", "cpus": 0.4, "ram_gb": 1.0, "gpu_mib": 0.0}
    with caplog.at_level("WARNING"):
        tid, run_id, grant, host, _target, released = _door_run_ended(home, monkeypatch, measure)
    [req] = _releases(allocd, grant["reservation_id"])
    assert req["actual"]["host"] == "some-other-box"
    assert released == 1                                                        # marked: never released twice
    assert not placement.live_reservations(home / placement.RESERVATIONS_REL)
    assert _usage_rows(home) == []
    assert [m for m in caplog.messages if "actual not recorded" in m]


def _fake_ssh(script_out, *, check_rc=0, rc=0):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if "-O" in argv:
            return SimpleNamespace(returncode=check_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=rc, stdout=script_out, stderr="")
    return run, calls


@pytest.fixture
def sock(tmp_path, monkeypatch):
    import tempfile
    from tools.environments.ssh import control_socket_path
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    s = control_socket_path("kanbanw", "ace-media", 22, "run7")
    s.parent.mkdir(parents=True, exist_ok=True)
    s.touch()
    return s


FP = {"host": "ace-media", "target": "kanbanw@ace-media", "port": 22, "run_id": 7}


def test_measure_reads_the_runs_own_master(sock):
    run, calls = _fake_ssh("fp 1610612736 600000000   1200 512\n")
    got = kh.measure_run_footprint(FP, runner=run)
    assert got == {"host": "ace-media", "cpus": 0.5, "ram_gb": 1.5, "cpu_s": 600.0, "wall_s": 1200.0, "gpu_mib": 512.0}
    assert all(c[:3] == ["ssh", "-S", str(sock)] and "ControlMaster=no" in c for c in calls)
    assert calls[0][-3:] == ["-O", "check", "kanbanw@ace-media"]


def test_measure_without_nvidia_smi_leaves_gpu_unmeasured(sock):
    run, _ = _fake_ssh("fp 1073741824 1000000 10 -\n")
    assert "gpu_mib" not in kh.measure_run_footprint(FP, runner=run)


@pytest.mark.parametrize("out,check_rc,rc", [("", 255, 0), ("no session scope: /user.slice\n", 0, 3),
                                             ("garbage\n", 0, 0)])
def test_measure_refuses_a_dead_master_or_unreadable_scope(sock, out, check_rc, rc):
    run, calls = _fake_ssh(out, check_rc=check_rc, rc=rc)
    with pytest.raises(RuntimeError):
        kh.measure_run_footprint(FP, runner=run)
    if check_rc:
        assert len(calls) == 1   # never a fresh connection that would measure a new session


def _sibling(plain, tag="ab12cd34"):
    s = plain.with_name(f"{plain.stem[:8]}{tag}.sock")   # SSHEnvironment._control_socket_for's SendEnv master
    s.touch()
    return s


def _per_socket_ssh(outs):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = outs[argv[2]]
        if "-O" in argv:
            return SimpleNamespace(returncode=255 if out is None else 0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    return run, calls


def test_measure_sums_the_execution_master_with_the_plain_one(sock):
    sib = _sibling(sock)
    run, calls = _per_socket_ssh({str(sock): "fp 1073741824 1000000 100 -\n",
                                  str(sib): "fp 2147483648 199000000 400 256\n"})
    got = kh.measure_run_footprint(FP, runner=run)
    assert got == {"host": "ace-media", "cpus": 0.5, "ram_gb": 3.0, "cpu_s": 200.0, "wall_s": 400.0, "gpu_mib": 256.0}
    assert {c[2] for c in calls} == {str(sock), str(sib)}


def test_measure_reads_the_execution_master_when_the_plain_one_is_gone(sock):
    sock.unlink()
    sib = _sibling(sock)
    run, _ = _per_socket_ssh({str(sib): "fp 1073741824 2000000 10 -\n"})
    assert kh.measure_run_footprint(FP, runner=run)["ram_gb"] == 1.0


def test_measure_skips_a_dead_sibling_master(sock):
    sib = _sibling(sock)
    run, calls = _per_socket_ssh({str(sock): "fp 1073741824 1000000 10 -\n", str(sib): None})
    assert kh.measure_run_footprint(FP, runner=run)["ram_gb"] == 1.0
    assert len([c for c in calls if c[2] == str(sib)]) == 1   # its -O check only: never a fresh connection


@pytest.mark.skipif(sys.platform == "win32", reason="bash fragment")
@pytest.mark.parametrize("smi,want", [("exit 9", "-"), ("echo '41, 300'; echo '99, 7'", "300"), (None, "-")])
def test_gpu_fragment_never_reports_a_failed_query_as_zero(tmp_path, smi, want):
    import subprocess
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if smi is not None:
        (bindir / "nvidia-smi").write_text(f"#!/bin/sh\n{smi}\n")
        (bindir / "nvidia-smi").chmod(0o755)
    (tmp_path / "cgroup.procs").write_text("41\n")
    for tool in ("awk", "printf"):
        real = shutil.which(tool)
        if real:
            (bindir / tool).symlink_to(real)
    out = subprocess.run(["/bin/bash", "-c", f'd={tmp_path}; ' + kh._FOOTPRINT_GPU_SH + 'echo "$g"'],
                         env={"PATH": str(bindir)}, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.stdout.strip() == want, out.stderr


def test_measure_with_no_socket_never_runs_ssh(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    run, calls = _fake_ssh("fp 1 1 1 0\n")
    with pytest.raises(RuntimeError, match="no live ssh master"):
        kh.measure_run_footprint(FP, runner=run)
    assert calls == []
