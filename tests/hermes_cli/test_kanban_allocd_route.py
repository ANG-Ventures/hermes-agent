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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.hermes_cli.test_kanban_headroom_placement import (  # noqa: F401  (fixture)
    FAKE_PID, TABLE, Fleet, _plan, home,
)
from hermes_cli import alloc_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import placement
from hermes_cli import placement_policy as pp

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
            return (200, {"released": True}) if placement.release(req["reservation_id"]) else (410, {"gone": True})
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
        if d.host:
            return 200, {"host": d.host, "reservation_id": rid, "rule": d.rule, "band": None}
        return 409, {"refused": True, "reason": d.reason}


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
    sig = kwp.TargetSignal(policy=pp.PlacementPolicy(), state_path=home / "var" / kwp.TARGET_STATE_FILE,
                           ledger_dir=home / "var" / "placement", clock=lambda: at, placement_table=table,
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
                                              (tid, "worker_placed")).fetchone()[0])
            kb.complete_task(conn, tid, summary="done")
    finally:
        loud.close()
    assert kh.RESERVATION_KEY in payload and kh.RESERVATION_VIA_KEY not in payload
    assert loud.requests == []


# -- the vendored client stays the canonical one -----------------------------------------------

def test_vendored_alloc_client_matches_canonical():
    """hermes_cli/alloc_client.py == hermes-home scripts/lib/alloc_client.py except the
    vendoring header and the placement import. ALLOC_CLIENT_CANONICAL names the file (CI skips)."""
    import os
    env = os.environ.get("ALLOC_CLIENT_CANONICAL")
    if not env:
        pytest.skip("ALLOC_CLIENT_CANONICAL not set (no hermes-home checkout to cross-read)")
    ours = Path(alloc_client.__file__).read_text(encoding="utf-8").splitlines()
    theirs = Path(env).read_text(encoding="utf-8").splitlines()
    ours = [x for x in ours if not x.startswith("# VENDORED") and not x.startswith("# sha256")
            and not x.startswith("# local change") and not x.startswith("# the rest against")]
    theirs_n = []
    skip = 0
    for x in theirs:
        if x.startswith("sys.path.insert(0, str(Path(__file__).resolve().parent))"):
            skip = 1
            continue
        if skip and x.startswith("import placement"):
            theirs_n.append("from hermes_cli import placement  # VENDORED: the fork's placement copy "
                            "(hermes-home: sys.path + import placement)")
            skip = 0
            continue
        theirs_n.append(x)
    assert ours == theirs_n
