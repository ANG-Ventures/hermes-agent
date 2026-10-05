"""Placement PRD Phase 1b: kanban target pressure, hysteresis, ledger, kill switch (t_1e4b9684).

PRD ``plans/2026-10-04_fleet-resource-aware-placement-PRD.md`` v0.4: I-3, I-4,
I-7, I-8, §5.2, §5.3, F-5, F-6, AC-4, AC-7, AC-9, AC-11. The gate seam is a
fixture pressure file read through the real probe parser; nothing induces load
and no test opens a real ssh connection.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_gate_tick as kgt
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import placement_ledger as pl
from hermes_cli import placement_policy as pp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kanban_placement"
POLICY = pp.PlacementPolicy()  # PRD §5.3 values (fallback until schema 1 lands)
T0 = 1_791_200_000.0


def _host(name, priority, slots=4, profiles=("alpha",)):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=slots,
                        capacity_pct=0.8, absence="required", profiles=tuple(profiles),
                        state="active", enabled=True, priority=priority)


HOSTS = [_host("ace-ai", 1), _host("ace-media", 2)]


def _pressure(host, at, ratio):
    return json.dumps({"host": host, "at": at, "ncpu": 24, "load1": ratio * 24, "load_ratio": ratio})


class Fleet:
    """Per-host fixture: remote clock + the pressure file text."""

    def __init__(self):
        self.now = {h.name: T0 for h in HOSTS}
        self.text = {h.name: _pressure(h.name, T0, 0.1) for h in HOSTS}
        self.load1 = {h.name: 2.0 for h in HOSTS}

    def set(self, host, ratio, *, age=0.0, at=None):
        self.now[host] += 30.0 if at is None else 0.0
        at = self.now[host] - age if at is None else at
        self.text[host] = _pressure(host, at, ratio)

    def probe(self, h):
        return kwp.HostSample(self.load1[h.name], 24, self.now[h.name], self.text[h.name])


@pytest.fixture
def sig(tmp_path):
    clock = {"t": T0}
    s = kwp.TargetSignal(policy=POLICY, state_path=tmp_path / "var" / kwp.TARGET_STATE_FILE,
                         ledger_dir=tmp_path / "var" / "placement", clock=lambda: clock["t"])
    s.clock_box = clock  # type: ignore[attr-defined]
    return s


def _plan(fleet, sig, running=None):
    return kwp.plan(HOSTS, running or {}, probe=fleet.probe, signal=sig)


# -- I-3 / AC-4: stale or unreadable = UNKNOWN, never hot ---------------------

def test_read_pressure_uses_the_remote_clock_and_120s_bound():
    ok, why = kwp.read_pressure(T0 + 119, _pressure("a", T0, 0.9), stale_after_s=120, warm=0.7, hot=0.8)
    assert ok == (T0, kwp.BAND_HOT) and why == ""
    stale, why = kwp.read_pressure(T0 + 121, _pressure("a", T0, 0.9), stale_after_s=120, warm=0.7, hot=0.8)
    assert stale is None and why == "pressure unknown (stale 121s)"
    assert kwp.read_pressure(T0, "", stale_after_s=120, warm=0.7, hot=0.8)[0] is None
    assert kwp.read_pressure(None, _pressure("a", T0, 0.1), stale_after_s=120, warm=0.7, hot=0.8)[0] is None
    assert kwp.read_pressure(T0, "{nope", stale_after_s=120, warm=0.7, hot=0.8)[0] is None


def test_stale_pressure_is_unknown_not_hot(sig):
    """Two stale samples of a HOT file never make the host hot (AC-4 mutant:
    stale -> hot would set the streak). The host is refused as unreachable."""
    fleet = Fleet()
    for _ in range(3):
        fleet.set("ace-ai", 0.95, age=130)
        p = _plan(fleet, sig)
        d = p.detail["ace-ai"]
        assert p.slots["ace-ai"] == 0 and d["reachable"] is False and d["band"] == kwp.BAND_UNKNOWN
        assert d["hot"] is False and d["pressure"] == "pressure unknown (stale 130s)"
    state = json.loads(sig.state_path.read_text(encoding="utf-8-sig"))["hosts"]["ace-ai"]
    assert state["hot"] is False and state["hot_run"] == 0
    # The file turns fresh and hot: the streak starts from zero (UNKNOWN reset).
    fleet.set("ace-ai", 0.95)
    assert _plan(fleet, sig).slots["ace-ai"] == 4


def test_non_finite_pressure_is_unknown():
    """JSON NaN/Infinity (Python's decoder accepts them) never reads as a band."""
    for doc in ('{"at": %r, "load_ratio": NaN}' % T0, '{"at": Infinity, "load_ratio": 0.1}',
                '{"at": NaN, "load_ratio": 0.1}', '{"at": %r, "load_ratio": Infinity}' % T0):
        assert kwp.read_pressure(T0, doc, stale_after_s=120, warm=0.7, hot=0.8)[0] is None, doc
    assert kwp.read_pressure(float("nan"), _pressure("a", T0, 0.1),
                             stale_after_s=120, warm=0.7, hot=0.8)[0] is None


def test_non_finite_ledger_row_does_not_hide_other_consumers(tmp_path):
    (tmp_path / "host-reservations.ci.json").write_text(
        '{"consumer":"ci","at":%r,"ttl_s":900,"hosts":{"ace-ai":{"busy_units":NaN,"cpu_est":Infinity}}}' % T0,
        encoding="utf-8")
    pl.update(tmp_path, "kanban", lambda old: {
        "at": T0, "ttl_s": 180,
        "hosts": {"ace-ai": {"busy_units": 1, "cpu_est": 2.0, "ramp_s": 600, "placed_at": [T0]}}})
    res = pl.read_all(tmp_path, POLICY, now=T0)
    assert pl.projected("ace-ai", 10.0, res, now=T0) == pytest.approx(12.0)


def test_two_planners_on_one_root_do_not_lose_a_sample(sig):
    """Planner B read the state before planner A committed: B still advances
    from A's committed step, so two consecutive hot samples reach hot."""
    fleet = Fleet()
    fleet.set("ace-ai", 0.95)
    stale_view = kwp._load_state(sig.state_path)        # B's early read (empty)
    _plan(fleet, sig)                                     # A commits hot_run=1
    fleet.set("ace-ai", 0.95)
    real = kwp._load_state
    kwp._load_state = lambda path: stale_view            # B planned from the old view
    try:
        p = _plan(fleet, sig)
    finally:
        kwp._load_state = real
    assert p.slots["ace-ai"] == 0
    assert json.loads(sig.state_path.read_text(encoding="utf-8-sig"))["hosts"]["ace-ai"]["hot"] is True


# -- F-5 / AC-11: hysteresis advances in plan(), take() only reads ------------

def test_two_takes_on_one_hot_probe_still_admit(sig):
    fleet = Fleet()
    fleet.set("ace-ai", 0.95)
    p = _plan(fleet, sig)
    assert p.take("alpha", pin="ace-ai").name == "ace-ai"
    assert p.take("alpha", pin="ace-ai").name == "ace-ai"
    assert json.loads(sig.state_path.read_text(encoding="utf-8-sig"))["hosts"]["ace-ai"]["hot_run"] == 1


def test_hot_after_two_samples_clear_after_three(sig):
    fleet = Fleet()
    seen = []
    for ratio in (0.95, 0.95, 0.75, 0.75, 0.75):
        fleet.set("ace-ai", ratio)
        p = _plan(fleet, sig)
        seen.append(p.slots["ace-ai"])
    # [hot] admits, [hot, hot] refuses, [warm, warm] still refuses, 3rd warm admits.
    assert seen == [4, 0, 0, 0, 4], seen


def test_repeated_remote_at_is_a_noop(sig):
    fleet = Fleet()
    fleet.set("ace-ai", 0.95)
    for _ in range(4):  # same at four ticks running: one sample
        p = _plan(fleet, sig)
    assert p.slots["ace-ai"] == 4
    assert json.loads(sig.state_path.read_text(encoding="utf-8-sig"))["hosts"]["ace-ai"]["hot_run"] == 1


def test_streak_state_older_than_3x_stale_is_discarded(sig):
    fleet = Fleet()
    fleet.set("ace-ai", 0.95)
    _plan(fleet, sig)
    sig.clock_box["t"] += 361  # > STALE_AFTER_S x 3
    fleet.set("ace-ai", 0.95)
    p = _plan(fleet, sig)
    assert p.slots["ace-ai"] == 4  # one fresh hot sample, not the second


def test_refused_pin_names_the_band(sig):
    fleet = Fleet()
    for _ in range(2):
        fleet.set("ace-ai", 0.95)
        p = _plan(fleet, sig)
    assert p.take("alpha", pin="ace-ai") is None
    assert p.refusal.startswith("pin_host_hot (band hot")


# -- warm rungs (RC7) ---------------------------------------------------------

def test_warm_rung_is_deprioritised(sig):
    fleet = Fleet()
    fleet.set("ace-ai", 0.75)
    fleet.set("ace-media", 0.10)
    p = _plan(fleet, sig)
    assert p.take("alpha").name == "ace-media"


def test_all_warm_picks_least_projected_and_rereads_the_ledger(sig):
    fleet = Fleet()
    fleet.set("ace-ai", 0.75)
    fleet.set("ace-media", 0.75)
    fleet.load1.update({"ace-ai": 5.0, "ace-media": 9.0})
    p = _plan(fleet, sig)
    assert p.take("alpha").name == "ace-ai"           # 5 < 9
    # Another consumer reserves 12 fresh cores on ace-ai AFTER the plan:
    # the next warm pick re-reads the ledger and moves to ace-media.
    pl.update(sig.ledger_dir, "prism", lambda old: {
        "at": T0, "ttl_s": 180,
        "hosts": {"ace-ai": {"busy_units": 12, "cpu_est": 1.0, "ramp_s": 60, "placed_at": [T0] * 12}}})
    assert p.take("alpha").name == "ace-media"


def test_first_hot_sample_is_not_preferred_over_a_warm_rung(sig):
    """A host on its first hot sample is still admitted (streak 1) but never
    ranks as cool: a warm rung of lower priority wins."""
    fleet = Fleet()
    fleet.set("ace-ai", 0.95)     # priority 1, hot (streak 1)
    fleet.set("ace-media", 0.75)  # priority 2, warm
    p = _plan(fleet, sig)
    assert p.slots["ace-ai"] == 4
    assert p.take("alpha").name == "ace-media"


# -- I-8 / F-6 / AC-7: projected() -------------------------------------------

def _res(units, cpu, ramp, stamps, host="ace-ai", consumer="kanban"):
    return pl.Reservation(consumer, host, units, cpu, ramp, tuple(stamps))


def test_projected_foreign_load_plus_fresh_reservation():
    fresh = [_res(6, 2.0, 600, [T0] * 6)]
    assert pl.projected("ace-ai", 10.0, fresh, now=T0) == pytest.approx(22.0)
    assert pl.projected("ace-ai", 10.0, fresh, now=T0 + 600) == pytest.approx(10.0)
    assert pl.projected("ace-media", 10.0, fresh, now=T0) == pytest.approx(10.0)


def test_ramp_zero_contributes_nothing():
    assert pl.projected("ace-ai", 10.0, [_res(8, 3.0, 0, [T0] * 8, consumer="ci")], now=T0) == 10.0


def test_missing_placed_at_contributes_nothing(tmp_path):
    pl.update(tmp_path, "kanban", lambda old: {
        "at": T0, "ttl_s": 180, "hosts": {"ace-ai": {"busy_units": 3, "cpu_est": 2.0, "ramp_s": 600}}})
    res = pl.read_all(tmp_path, POLICY, now=T0)
    assert [r.busy_units for r in res] == [3]
    assert pl.projected("ace-ai", 10.0, res, now=T0) == 10.0


def test_ledger_clamps_busy_units_to_max_slots(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    pl.update(tmp_path, "kanban", lambda old: {
        "at": T0, "ttl_s": 180,
        "hosts": {"ace-ai": {"busy_units": 10**6, "cpu_est": 2.0, "ramp_s": 600, "placed_at": [T0] * 50}}})
    (r,) = pl.read_all(tmp_path, POLICY, now=T0)
    assert r.busy_units == 4 and len(r.placed_at) == 4
    assert pl.projected("ace-ai", 10.0, [r], now=T0) == pytest.approx(18.0)
    assert any("ledger_bounds_violation" in m for m in caplog.messages)


def test_ledger_entry_past_ttl_is_ignored(tmp_path):
    pl.update(tmp_path, "kanban", lambda old: {
        "at": T0, "ttl_s": 180,
        "hosts": {"ace-ai": {"busy_units": 2, "cpu_est": 2.0, "ramp_s": 600, "placed_at": [T0, T0]}}})
    assert len(pl.read_all(tmp_path, POLICY, now=T0 + 180)) == 1
    assert pl.read_all(tmp_path, POLICY, now=T0 + 181) == []


def test_kanban_writer_keeps_fresh_stamps_and_drops_aged_ones():
    rows = pl.kanban_rows({}, {}, {"ace-ai": 2}, cpu_est=2.0, ramp_s=600, now=T0)
    assert rows == {"ace-ai": {"busy_units": 2, "cpu_est": 2.0, "ramp_s": 600.0, "placed_at": [T0, T0]}}
    # Next tick: the two are running (counted busy), one more is placed.
    rows = pl.kanban_rows({"hosts": rows}, {"ace-ai": 2}, {"ace-ai": 1}, cpu_est=2.0, ramp_s=600, now=T0 + 60)
    assert rows["ace-ai"]["busy_units"] == 3 and rows["ace-ai"]["placed_at"] == [T0 + 60, T0, T0]
    # Past the ramp the stamps contribute 0 and are dropped; the count stays.
    rows = pl.kanban_rows({"hosts": rows}, {"ace-ai": 3}, {}, cpu_est=2.0, ramp_s=600, now=T0 + 700)
    assert rows["ace-ai"] == {"busy_units": 3, "cpu_est": 2.0, "ramp_s": 600.0, "placed_at": []}


# -- kill switch: read_signal=false is KWLB v0.1 byte for byte ----------------

def test_read_signal_false_plan_is_byte_identical_on_replay():
    sys.path.insert(0, str(FIXTURES))
    try:
        import replay_driver
    finally:
        sys.path.remove(str(FIXTURES))
    assert replay_driver.replay() == replay_driver.GOLDEN.read_text(encoding="utf-8-sig")


def test_read_signal_config_key():
    assert kgt.read_signal_enabled({}) is True
    assert kgt.read_signal_enabled({"placement": {"read_signal": True}}) is True
    assert kgt.read_signal_enabled({"placement": {"read_signal": False}}) is False


def test_probe_reads_pressure_in_the_same_round_trip():
    calls = []

    class _P:
        returncode = 0
        stdout = "3.10 2.00 1.00 1/200 99\n24\n1791200100\n" + _pressure("ace-ai", T0 + 90, 0.5) + "\n"

    def runner(argv, **kw):
        calls.append(argv)
        return _P()

    s = kwp.probe_host(HOSTS[0], runner=runner, pressure_path="/var/lib/placement/host-pressure.json")
    assert len(calls) == 1
    assert calls[0][-1] == ("cat /proc/loadavg; nproc; date +%s; "
                            "cat /var/lib/placement/host-pressure.json 2>/dev/null; true")
    assert (s.load1, s.ncpu, s.remote_now) == (3.1, 24, 1791200100.0)
    assert json.loads(s.pressure_text)["load_ratio"] == 0.5


# -- gate-seam E2E: real builder, real dispatcher, fixture pressure files -----

@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("real spawn")))
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (home / "profiles" / "alpha").mkdir(parents=True)
    fleet = home / "fleet"
    fleet.mkdir()
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        h: {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"} for h in ("ace-ai", "ace-media")}}))
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "kanbanw", "priority": ["ci-box", "ace-ai", "ace-media"],
        "profiles": ["alpha"],
        "hosts": {"ci-box": {"enabled": False, "absence": "optional"},
                  "ace-ai": {"enabled": True, "absence": "required"},
                  "ace-media": {"enabled": True, "absence": "optional"}}}))
    return home


def _ssh(fleet: Fleet):
    """A fake ``ssh`` runner answering the real probe command from the fixture."""
    def runner(argv, **kw):
        host = argv[-2].split("@", 1)[1]
        assert argv[-1].startswith("cat /proc/loadavg; nproc; date +%s; cat ")
        out = f"{fleet.load1[host]:.2f} 1.00 1.00 1/100 1\n24\n{int(fleet.now[host])}\n{fleet.text[host]}\n"
        return SimpleNamespace(returncode=0, stdout=out)
    return runner


def _gate_tick(home, fleet):
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    return kgt.GateTickBuilder(
        gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
        ledger=lambda boards: {}, connect=lambda board=None: None,
        probe=lambda h: kwp.probe_host(h, runner=_ssh(fleet), pressure_path=POLICY.pressure_path))


def _dispatch(plan):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    with kb.connect_closing() as conn:
        # Spilling with the local budget used up: every card routes to the pool.
        # (A PAUSED tick with an empty pool returns before the row scan, KWLB v0.1.)
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused=None,
                                spawn_limit=0, spillover=plan, reconcile_orphans=False)
    return res, spawned


def _card(body="host:any"):
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title="e2e", assignee="alpha", body=body)


def test_e2e_hot_ace_ai_places_on_ace_media(board, caplog):
    caplog.set_level(logging.INFO)
    fleet = Fleet()
    builder = _gate_tick(board, fleet)
    for _ in range(2):
        fleet.set("ace-ai", 0.95)
        fleet.set("ace-media", 0.10)
        plan = builder.plan_pool([], {})
    tid = _card()
    _res, spawned = _dispatch(plan)
    assert spawned == [(tid, "ace-media")]
    builder.record_placements([(tid, "ace-media")])
    led = json.loads((board / "var" / "placement" / "host-reservations.kanban.json").read_text(encoding="utf-8-sig"))
    assert led["consumer"] == "kanban" and led["ttl_s"] == 180
    assert led["hosts"]["ace-media"]["busy_units"] == 1
    for line in caplog.messages:
        if "kanban pool:" in line:
            print("E2E-LOG", line)
    assert any(m.startswith("kanban pool: ace-ai refused: band hot") for m in caplog.messages)


def test_e2e_both_hot_waits_pool_unavailable_naming_the_band(board, caplog):
    caplog.set_level(logging.INFO)
    fleet = Fleet()
    builder = _gate_tick(board, fleet)
    for _ in range(2):
        fleet.set("ace-ai", 0.95)
        fleet.set("ace-media", 0.90)
        plan = builder.plan_pool([], {})
    tid = _card()
    res, spawned = _dispatch(plan)
    assert spawned == []
    wait = res.placement_waits[tid]
    print("E2E-WAIT", tid, wait)
    assert wait.startswith("pool_unavailable (ace-ai: band hot") and "ace-media: band hot" in wait
    assert any(m.startswith("kanban pool: pool_unavailable (ace-ai: band hot") for m in caplog.messages)


def test_e2e_stale_pressure_waits_unknown_naming_staleness(board):
    fleet = Fleet()
    builder = _gate_tick(board, fleet)
    fleet.set("ace-ai", 0.10, age=130)
    fleet.set("ace-media", 0.10, age=200)
    plan = builder.plan_pool([], {})
    tid = _card()
    res, spawned = _dispatch(plan)
    wait = res.placement_waits[tid]
    print("E2E-WAIT", tid, wait)
    assert spawned == [] and "pressure unknown (stale 130s)" in wait and "stale 200s" in wait
    assert plan.detail["ace-ai"]["reachable"] is False and plan.detail["ace-ai"]["hot"] is False
    # A pin on a stale host is refused as unknown, not hot.
    assert plan.take("alpha", pin="ace-ai") is None and plan.refusal.startswith("pin_host_unknown")


def test_e2e_one_hot_probe_then_ok_still_places_on_ace_ai(board):
    fleet = Fleet()
    builder = _gate_tick(board, fleet)
    fleet.set("ace-ai", 0.95)
    builder.plan_pool([], {})
    fleet.set("ace-ai", 0.10)
    plan = builder.plan_pool([], {})
    tid = _card()
    _res, spawned = _dispatch(plan)
    assert spawned == [(tid, "ace-ai")]


def test_e2e_read_signal_false_ignores_pressure_and_ledger(board):
    fleet = Fleet()
    builder = _gate_tick(board, fleet)
    builder._kanban_cfg = lambda: {"placement": {"read_signal": False}}
    builder._probe = lambda h: (2.0, 24)
    fleet.set("ace-ai", 0.99)
    plan = builder.plan_pool([], {})
    assert plan.signal is None and plan.budget == 8
    builder.record_placements([("t_x", "ace-ai")])
    assert not (board / "var" / "placement").exists()
    assert not (board / "var" / kwp.TARGET_STATE_FILE).exists()


def test_standalone_daemon_writes_the_kanban_ledger(board, monkeypatch):
    """The systemd daemon path publishes reservations like the gateway loop."""
    import threading

    from hermes_cli import config as _config
    from hermes_cli import kanban_load_gate as klg

    monkeypatch.setattr(_config, "load_config", lambda: {"kanban": {}})
    monkeypatch.setattr(kwp, "probe_host", lambda h, runner=None, **kw: None)
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (146.0, 90.0))
    stop = threading.Event()

    def fake_dispatch_once(conn, **kw):
        res = kb.DispatchResult()
        res.placed.append(("t_x", "ace-media"))
        return res

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda _r: stop.set(),
                   load_gate=klg.LoadGate({}, ncpu=32))
    led = json.loads((board / "var" / "placement" / "host-reservations.kanban.json")
                     .read_text(encoding="utf-8-sig"))
    assert led["hosts"]["ace-media"]["busy_units"] == 1


def test_one_shot_pool_plan_seeds_remote_by_host_for_the_ledger(monkeypatch):
    """A one-shot `kanban dispatch` skips build(); record_placements() keys the kanban
    ledger off builder._remote_by_host, so the plan must seed it or running remote
    workers' reservations are dropped until the next gateway tick (Prism 2febc7f296e1)."""
    import gateway.kanban_gate_tick as gt
    from hermes_cli import kanban_ops

    class _B:
        _remote_by_host = {}
        plan_calls = []

        def _ledger(self, boards):
            return {"default": (1, {"ace-ai": 2})}

        def plan_pool(self, boards, remote_by_host):
            self.plan_calls.append(dict(remote_by_host))
            return "PLAN"

    b = _B()
    monkeypatch.setattr(gt, "live_boards", lambda: ["default"])
    monkeypatch.setattr(gt, "running_split", lambda ledger: (1, {"ace-ai": 2}))
    monkeypatch.setattr(gt, "standalone_builder", lambda gate, cfg: b)
    assert kanban_ops._one_shot_pool_plan(object(), {"kanban": {}}) == "PLAN"
    assert b._remote_by_host == {"ace-ai": 2}
    assert b.plan_calls == [{"ace-ai": 2}]
