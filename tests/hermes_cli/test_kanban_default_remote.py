"""Phase 2 of Control-Plane Protection spec v0.3: ``kanban.placement.default:
remote`` (route order only, no cap) and the Momus pass 3 closeout gates
R1-R4 + R10 (card t_667f1f6a).

Hermetic: a fixture pool plan (``kwp.plan`` with a fake probe), the real
``dispatch_once`` and spawn loop, a fake ``spawn_fn``. Every mutation the spec
names is run here and asserted to turn the gate red.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_gate_series as series
from gateway import kanban_gate_tick as kgt
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_placement_gates as gates
from hermes_cli import kanban_placement_reasons as pr
from hermes_cli import kanban_worker_pool as kwp


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (h / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return h


def _host(name, prio=0, slots=4, profiles=("alpha",)):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=slots, capacity_pct=0.8,
                        absence="required", profiles=tuple(profiles), state="active", enabled=True,
                        priority=prio)


# Host A (ace-ai) is the ONLY host in plan.hosts. Host B (ci-box) is NOT in the
# plan at all (R3a: a 0-slot pool host is a different case); it is the only
# worker host the reach matrix proves can reach ace-media.
ALIASES = ("ace-ai", "ace-media", "ci-box")
REACH = {"ace-ai": {"ace-media": 255, "ace-ai": 0, "ci-box": 0},
         "ci-box": {"ace-media": 0, "ace-ai": 0, "ci-box": 0}}
REACH_BOUND_TO_B = "Sync the build: rsync -a ./out ace-media:/srv/drop/ and verify."
PORTABLE = "Refactor the parser and add tests."


def _plan(*, slots=4, remote=True, band="admitting"):
    hosts = [_host("ace-ai", 0, slots=slots)]
    cfg = kwp.PoolConfig(hosts=("ace-ai",), profiles=("alpha",), pool_hosts=tuple(hosts),
                         reach=REACH, reach_aliases=ALIASES, reach_status="ok")
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    assert "ci-box" not in p.hosts                      # R3(a), by construction
    p.band, p.spill_reason, p.default_remote = band, None, remote
    return p


def _tick(conn, plan, *, spawn_limit=4, board=None):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=spawn_limit,
                            spawn_paused=None if spawn_limit else "load gate: paused",
                            spillover=plan, reconcile_orphans=False, board=board)
    return res, spawned


def _cards(conn, body, n=1):
    return [kb.create_task(conn, title=f"card {i}", assignee="alpha", body=body) for i in range(n)]


def _placements(conn, tid):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='placement'", (tid,))]


# -- AC-9: flag off is today's route ---------------------------------------------

def test_flag_off_keeps_local_first(home):
    """``default`` absent/local: a portable row with local allowance spawns on
    the Studio and the pool is never asked (today's branch)."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        res, spawned = _tick(conn, _plan(remote=False))
        assert spawned == [(tid, "studio")] and not res.placed
        (rec,) = _placements(conn, tid)
        assert rec["placement_default"] == "local" and rec["reason"] == "local-first"
        assert rec["take_refusal"] is None


def test_flag_on_offers_the_pool_first(home):
    """MUTATION (AC-2): with the flag on the same row goes to the pool; revert
    the portable branch to `not local_free` and it would spawn locally with
    take_refusal null, which the I-1 query counts."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        res, spawned = _tick(conn, _plan())
        assert spawned == [(tid, "ace-ai")] and res.placed == [(tid, "ace-ai")]
        (rec,) = _placements(conn, tid)
        assert rec["reason"] == "pool" and rec["route_class"] == "portable"


@pytest.mark.parametrize("raw", [
    None, {}, {"placement": {"default": "local"}}, {"placement": {"default": "Remote "}},
    {"placement": {"default": True}}, {"placement": {"default": "wait"}},
])
def test_placement_default_is_always_remote(raw):
    """t_82b4db80 (Ace 2026-10-19 21:2x): the local/remote ORDERING knob is
    retired. Absent, `local`, junk: every value reads remote. MUTATION: restore
    the old parse and the absent/local/junk rows go red."""
    assert kwp.placement_default(raw) == kwp.PLACEMENT_DEFAULT_REMOTE


def test_a_retired_local_value_logs_one_deprecation_line(caplog, monkeypatch):
    monkeypatch.setattr(kwp, "_placement_default_warned", set())
    with caplog.at_level("WARNING", logger=kwp.__name__):
        for _ in range(3):
            assert kwp.placement_default({"placement": {"default": "local"}}) == "remote"
        assert kwp.placement_default({"placement": {"default": "remote"}}) == "remote"
    lines = [r.getMessage() for r in caplog.records if "placement.default" in r.getMessage()]
    assert len(lines) == 1 and "'local'" in lines[0] and "ignored" in lines[0], lines


def test_plan_pool_reads_the_flag_per_tick(home, monkeypatch):
    """t_82b4db80: remote-first on every plan whatever the key says; pins_only
    stays off even with worker_pool.overflow false."""
    monkeypatch.setattr(kgt, "any_remote_pin", lambda *a, **k: True)
    monkeypatch.setattr(kgt, "any_ready", lambda *a, **k: True)
    fleet = home / "fleet"
    fleet.mkdir()
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"}}}))
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "kanbanw", "priority": ["ace-ai"], "profiles": ["alpha"],
        "hosts": {"ace-ai": {"enabled": True, "absence": "optional"}}}))
    cfg = {"placement": {"read_signal": False}, "worker_pool": {"overflow": False}}
    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: fleet, kanban_cfg=lambda: cfg,
                            ledger=lambda boards: {}, connect=lambda board=None: None,
                            probe=lambda h: (1.0, 16))
    plan = b.plan_pool([], {})
    assert plan.pins_only is False and plan.default_remote is True     # key absent: remote
    cfg["placement"]["default"] = "local"
    plan = b.plan_pool([], {})
    assert plan.pins_only is False and plan.default_remote is True     # retired value ignored


# -- AC-3 hermetic (R3): the ineligible-slot fixture ------------------------------

def test_reach_bound_rows_spawn_locally_as_last_resort(home):
    """R3: allowance = max_spawn_per_tick (4), 4 rows reach-bound to B (not in
    plan.hosts), free slots on A. Every row: take() refuses host_reach, the
    row spawns locally, reason last-resort; the I-0 key reads 0."""
    with kb.connect_closing() as conn:
        tids = _cards(conn, REACH_BOUND_TO_B, 4)
        plan = _plan()
        res, spawned = _tick(conn, plan, spawn_limit=4)
        assert sorted(spawned) == sorted((t, "studio") for t in tids)
        assert plan.slots["ace-ai"] == 4                         # A was never used
        assert res.portable_waiting == 0 and res.portable_waiting_with_room == 0
        for t in tids:
            (rec,) = _placements(conn, t)
            assert (rec["route_class"], rec["reason"], rec["take_refusal"]) == \
                ("portable", pr.LAST_RESORT, "host_reach")
        out = gates.check_i1(conn, 0)
        assert out["green"], out
        assert out["last_resort_by_refusal"] == {"host_reach": 4}


@pytest.mark.parametrize("cap", [0, 1, 3])
def test_any_cap_on_the_last_resort_spawn_is_red(home, monkeypatch, cap):
    """MUTATION (AC-3, R3c): a count cap <= 3 on the last-resort spawn leaves
    4 - cap rows waiting with room on their board -> the I-0 key > 0."""
    real = kbd._last_resort_allowed
    used = {"n": 0}

    def capped(route, local_free, cap_only, plan):
        if not real(route, local_free, cap_only, plan):
            return False
        used["n"] += 1
        return used["n"] <= cap

    monkeypatch.setattr(kbd, "_last_resort_allowed", capped)
    with kb.connect_closing() as conn:
        _cards(conn, REACH_BOUND_TO_B, 4)
        res, spawned = _tick(conn, _plan(), spawn_limit=4)
        assert len(spawned) == cap
        assert res.portable_waiting_with_room == 4 - cap


def test_dropping_only_from_take_lands_the_row_on_a(home, monkeypatch):
    """MUTATION (R3b): without the reach restriction the row lands on A, so the
    fixture really exercises the route change, not take() alone."""
    monkeypatch.setattr(kwp, "reach_allowed_hosts", lambda **kw: None)
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, REACH_BOUND_TO_B)
        res, spawned = _tick(conn, _plan())
        assert spawned == [(tid, "ace-ai")]


def test_allowance_zero_waits_and_is_not_a_violation(home):
    """AC-3 second case: no local allowance -> the row waits; portable_waiting
    1, the I-0 key 0 (the accepted shortage case)."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, REACH_BOUND_TO_B)
        res, spawned = _tick(conn, _plan(), spawn_limit=0)
        assert spawned == []
        assert pr.take_refusal_code(res.placement_waits[tid]) == "host_reach"
        assert res.portable_waiting == 1 and res.portable_waiting_with_room == 0


def test_full_pool_spawns_locally_with_pool_full(home):
    """AC-2 fixture: every pool slot used -> the portable row spawns locally,
    last-resort, take_refusal pool_full."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        res, spawned = _tick(conn, _plan(slots=0))
        assert spawned == [(tid, "studio")]
        (rec,) = _placements(conn, tid)
        assert rec["take_refusal"] == "pool_full" and rec["reason"] == pr.LAST_RESORT
        assert gates.check_i1(conn, 0)["last_resort_by_refusal"] == {"pool_full": 1}


def test_a_refused_pin_never_falls_through(home):
    """I-11 holds under the flag: a pin to a full host waits."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, "host: ace-ai\n" + PORTABLE)
        res, spawned = _tick(conn, _plan(slots=0))
        assert spawned == [] and res.placement_waits[tid].startswith("pin_host_")


def test_dry_run_reports_the_take_refusal(home):
    from hermes_cli import kanban_ops
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, REACH_BOUND_TO_B)
        res = kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1, max_spawn=64,
                                spawn_limit=4, spillover=_plan(), reconcile_orphans=False)
        (entry,) = [e for e in res.routes if e["task_id"] == tid]
        assert entry["take_refusal"] == "host_reach"
        assert "take_refusal=host_reach" in kanban_ops._route_line(entry, True)


# -- AC-2 / I-1 query (R4) --------------------------------------------------------

def _event(conn, tid, payload, kind="placement", at=None):
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                 (tid, kind, json.dumps(payload), int(at or time.time())))
    conn.commit()


def _rec(**kw):
    base = {"route_class": "portable", "placement_default": "remote", "host": "mac-studio-m3u",
            "reason": pr.LAST_RESORT, "take_refusal": "pool_full", "pool_free_at_spawn": 0}
    base.update(kw)
    return base


@pytest.mark.parametrize("refusal, violation", [
    ("pool_full", False), ("host_reach", False), ("board_unhostable (ci-box: x no_board_root)", False),
    ("pool_unavailable (ace-ai: hot)", False), ("unified_admission (ace-ai: held_back:kanban for prism 4 queued)", False),
    (None, True), ("profile_unserved", True),
    ("pool_fullish", True),
])
def test_i1_query_keys_on_the_frozen_vocabulary(home, refusal, violation):
    """R4: the predicate is generated from TAKE_REFUSALS; an unknown code and a
    null refusal on a local portable spawn are violations."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        _event(conn, tid, _rec(take_refusal=refusal, reason=pr.LAST_RESORT if refusal else "local-first"))
        out = gates.check_i1(conn, 0)
        assert bool(out["violations"]) is violation, out


def test_i1_guards_fail_closed(home):
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        assert not gates.check_i1(conn, 0)["green"]                      # nothing proven
        _event(conn, tid, _rec())
        assert gates.check_i1(conn, 0)["green"]
        _event(conn, tid, {k: v for k, v in _rec().items() if k != "pool_free_at_spawn"})
        assert any("field guard" in r for r in gates.check_i1(conn, 0)["red"])
        _event(conn, tid, _rec(), kind="spawned")
        assert any("kind guard" in r for r in gates.check_i1(conn, 0)["red"])


def test_every_unpinned_take_refusal_is_in_the_vocabulary(monkeypatch):
    """R4: drive every unpinned refusal path of take() and check its code is
    in TAKE_REFUSALS (a new code fails here before it reaches the query)."""
    seen = set()
    p = _plan()
    assert p.take("alpha", only=()) is None
    seen.add(p.refusal)
    p = _plan(slots=0)
    assert p.take("alpha") is None
    seen.add(p.refusal)
    p = _plan()
    assert p.take("beta") is None                                     # profile served nowhere
    seen.add(p.refusal)
    p = _plan()
    p.detail["ace-ai"]["boards"] = ["other"]
    assert p.take("alpha", board="default") is None
    seen.add(p.refusal)
    p = _plan(slots=0)
    p.signal = SimpleNamespace(placement_table=None)
    p.detail["ace-ai"]["pressure"] = "hot 2/2"
    assert p.take("alpha") is None
    seen.add(p.refusal)
    # the ONE rule refused every candidate (Prism 43117075a644)
    from hermes_cli import kanban_headroom as kh
    monkeypatch.setattr(kh, "unified_filter", lambda plan, names, job: ([], {n: "held_back:kanban" for n in names}))
    p = _plan()
    p.signal = SimpleNamespace(placement_table=object())
    assert p.take("alpha") is None
    seen.add(p.refusal)
    codes = {pr.take_refusal_code(r) for r in seen}
    assert codes == pr.TAKE_REFUSALS, codes


# -- R1: per-board key vs stranded quota (2 boards, real dispatch) -----------------

def _two_board_tick(home, *, y_limit, x_limit, allowance):
    with kb.connect_closing() as conn:
        kb.create_board(slug="y", name="Y")
    plan = _plan()
    results = []
    with kb.connect_closing(board="y") as cy:
        _cards(cy, REACH_BOUND_TO_B)
        res_y, _ = _tick(cy, plan, spawn_limit=y_limit, board="y")
        results.append(("y", res_y))
    with kb.connect_closing() as cx:
        res_x, spawned_x = _tick(cx, plan, spawn_limit=x_limit, board="default")
        results.append(("default", res_x))
    local = sum(kbd._local_spawn_count(r) for _s, r in results)
    tick = SimpleNamespace(remote_plan=plan, band="admitting", spill_reason=None)
    return series.tick_record(ts=time.time(), gate=SimpleNamespace(pool=plan.snapshot()), tick=tick,
                              builder=SimpleNamespace(), allowance=allowance, pool_budget=plan.budget,
                              spawn_paused=None, local_spawned=local, results=results,
                              tick_ms_cpu=1.0, tick_ms_wall=1.0, boards=2, queries=0)


def test_stranded_quota_is_reported_not_gated(home):
    """R1: board Y (quota 0, a pool-refused portable row) ticks before X
    (quota 1, no work). Y waits, X returns the quota too late: the tick ends
    portable_waiting 1 AND allowance_unused 1, but the I-0 key is 0 and the
    quota is reported as stranded_allowance 1."""
    rec = _two_board_tick(home, y_limit=0, x_limit=1, allowance=1)
    assert (rec["portable_waiting"], rec["allowance_unused"]) == (1, 1)
    assert rec["portable_waiting_with_room"] == 0 and rec["stranded_allowance"] == 1
    out = gates.check_i0([rec], since=rec["ts"] - 1, until=rec["ts"] + 1, interval=60)
    assert out["violations"] == 0 and out["stranded_ticks"] == 1
    assert out["legacy_portable_and_unused"] == 1          # the pre-R1 query would read red


def test_a_cap_on_the_same_tick_is_gated(home, monkeypatch):
    """R1 other half: the same 2 boards, but Y HAS the quota and a cap refuses
    the last-resort spawn -> the I-0 key reads 1 (forbidden)."""
    monkeypatch.setattr(kbd, "_last_resort_allowed", lambda *a: False)
    rec = _two_board_tick(home, y_limit=1, x_limit=0, allowance=1)
    assert rec["portable_waiting_with_room"] == 1 and rec["stranded_allowance"] == 0
    out = gates.check_i0([rec], since=rec["ts"] - 1, until=rec["ts"] + 1, interval=60)
    assert out["violations"] == 1 and not out["green"]


# -- R2: the live leg fails closed on missing data -----------------------------------

def _line(ts, **kw):
    rec = {f: 0 for f in series.FIELDS}
    rec.update(ts=ts, path="gateway", pool_planned=True, pool_reason=None, placement_default="remote")
    rec.update(kw)
    return rec


def test_i0_green_on_a_complete_series():
    lines = [_line(1000 + 60 * i) for i in range(60)]
    out = gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)
    assert out["green"], out


def test_i0_green_at_the_live_cadence():
    """Ticks land every interval + tick work (median 70 s live): no gap, so green."""
    lines = [_line(1000 + 70 * i) for i in range(52)]
    out = gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)
    assert out["green"], out


def test_i0_red_when_null_inputs_eat_coverage():
    lines = [_line(1000 + 60 * i) for i in range(60)]
    for r in lines[:5]:
        r["allowance_unused"] = None
    out = gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)
    assert not out["green"] and out["null_ticks"] == 5


def test_i0_null_or_non_numeric_records_are_not_coverage():
    """Prism P1: a null I-0 counter or a non-bool pool_planned measured nothing;
    it must not count as coverage nor read as 0."""
    lines = [_line(1000 + 60 * i) for i in range(60)]
    for r in lines:
        r["portable_waiting_with_room"] = None
    out = gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)
    assert not out["green"] and out["null_ticks"] == 60 and out["coverage"] == 0
    assert any(r.startswith("invalid: 60") for r in out["red"]), out["red"]
    lines = [_line(1000 + 60 * i) for i in range(60)]
    lines[3]["pool_planned"] = None
    lines[4]["stranded_allowance"] = "0"
    out = gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)
    assert out["null_ticks"] == 2 and any(r.startswith("invalid: 2") for r in out["red"])


def test_i0_red_on_an_empty_series():
    out = gates.check_i0([], since=1000, until=1000 + 3600, interval=60)
    assert not out["green"] and out["gaps"]


def test_i0_red_on_a_gap_of_more_than_two_ticks_and_prints_it():
    lines = [_line(1000 + 60 * i) for i in range(60) if not 20 <= i < 23]
    out = gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)
    assert not out["green"]
    assert any(r.startswith("gap: 240s") for r in out["red"]), out["red"]
    # two missing ticks (a 180 s hole) are inside the tolerance
    lines = [_line(1000 + 60 * i) for i in range(60) if not 20 <= i < 22]
    assert not any(r.startswith("gap") for r in
                   gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)["red"])


def test_i0_red_on_missing_fields_and_counts_unplanned_ticks():
    lines = [_line(1000 + 60 * i) for i in range(60)]
    del lines[5]["portable_waiting_with_room"]
    lines[6].update(pool_planned=False, pool_reason="refused:bad json")
    out = gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)
    assert any("field guard" in r for r in out["red"])
    assert out["unplanned_ticks"] == 1 and out["unplanned_reasons"] == ["refused:bad json"]


def test_i0_ignores_lines_not_from_the_gateway():
    lines = [_line(1000 + 60 * i, path="oneshot") for i in range(60)]
    assert not gates.check_i0(lines, since=1000, until=1000 + 3600, interval=60)["green"]


# -- R10: post-flip bad-effect detector -----------------------------------------------

def _run(conn, tid, *, outcome, summary="", event=None, host="ace-ai", ended=None):
    ended = int(ended or time.time())
    cur = conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, summary) "
                       "VALUES (?,?,?,?,?,?)", (tid, "done", ended - 60, ended, outcome, summary))
    rid = cur.lastrowid
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                 (tid, rid, "worker_placed", json.dumps({"host": host}), ended - 60))
    if event:
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                     "VALUES (?,?,?,?,?)", (tid, rid, event, "{}", ended - 30))
    conn.commit()
    return rid


def test_r10_counts_failed_and_half_done_remote_runs(home):
    with kb.connect_closing() as conn:
        a, b, c, d = _cards(conn, PORTABLE, 4)
        half = _run(conn, a, outcome="completed", summary="done except one check")
        conn.execute("UPDATE task_runs SET error = ? WHERE id = ?", ("turns.db not on this host", half))
        conn.commit()
        gap = _run(conn, b, outcome="blocked", event="worker_capability_gap")
        _run(conn, c, outcome="completed", summary="all green")
        rail = _run(conn, d, outcome="blocked", summary="BROWSER_RAIL_ABSENT: no browser session on ace-ai")
        hits = gates.missing_capability_runs(conn, time.time() - 3600, time.time() + 1)
        assert sorted(h["run_id"] for h in hits) == sorted([half, gap, rail])
        assert {h["run_id"]: h["half_done"] for h in hits}[half] is True
        rep = gates.r10_report(conn, days=1, now=time.time() + 1, baseline_daily=0)
        assert rep["trigger"] == gates.ROLLBACK_TRIGGER_PER_DAY and rep["green"]
        for _ in range(gates.ROLLBACK_TRIGGER_PER_DAY):
            _run(conn, c, outcome="gave_up", summary="sudo: a password is required")
        assert not gates.r10_report(conn, days=1, now=time.time() + 1, baseline_daily=0)["green"]
        # Prism P1: no pre-flip baseline -> fail closed, never a self-derived trigger.
        rep = gates.r10_report(conn, days=1, now=time.time() + 1)
        assert not rep["green"] and rep["trigger"] is None and rep["red"]


def test_r10_a_healthy_completed_summary_is_not_evidence(home):
    """Apollo review a1d1bdcb2f76 / Prism 0a23381c00c5: a completed run whose
    SUMMARY names kanban.db is healthy; only an event or the error field makes
    a completed run half-done."""
    with kb.connect_closing() as conn:
        a, b, c, d = _cards(conn, PORTABLE, 4)
        _run(conn, a, outcome="completed", summary="Updated kanban.db; all tests passed")
        gap = _run(conn, b, outcome="completed", summary="Updated kanban.db", event="worker_capability_gap")
        crash = _run(conn, c, outcome="crashed")
        conn.execute("UPDATE task_runs SET error = ? WHERE id = ?", ("docker: command not found", crash))
        err = _run(conn, d, outcome="completed", summary="partial")
        conn.execute("UPDATE task_runs SET error = ? WHERE id = ?", ("turns.db not on this host", err))
        conn.commit()
        hits = {h["run_id"]: h for h in gates.missing_capability_runs(conn, time.time() - 3600, time.time() + 1)}
        assert sorted(hits) == sorted([gap, crash, err])
        assert hits[gap]["half_done"] and hits[err]["half_done"] and not hits[crash]["half_done"]
        for _ in range(gates.ROLLBACK_TRIGGER_PER_DAY + 2):
            _run(conn, a, outcome="completed", summary="Updated kanban.db; all tests passed")
        rep = gates.r10_report(conn, days=1, now=time.time() + 1, baseline_daily=0)
        assert rep["green"], rep


def test_i1_records_without_placement_default_are_red(home):
    """Apollo review 8111932ced7d: a field-less (half-migrated) record is
    counted invalid, never silently excluded by the remote-only filters."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        _event(conn, tid, _rec())
        assert gates.check_i1(conn, 0)["green"]
        for _ in range(3):
            _event(conn, tid, {k: v for k, v in _rec(reason="local-first", take_refusal=None).items()
                               if k != "placement_default"})
        out = gates.check_i1(conn, 0)
        assert not out["green"] and out["missing_placement_default"] == 3
        assert any("3 record(s) missing placement_default" in r for r in out["red"]), out["red"]


def _builder(home, cfg, connect):
    fleet = home / "fleet"
    fleet.mkdir(exist_ok=True)
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"}}}))
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps({
        "schema": 1, "ssh_user": "kanbanw", "priority": ["ace-ai"], "profiles": ["alpha"],
        "hosts": {"ace-ai": {"enabled": True, "absence": "optional"}}}))
    gate = SimpleNamespace(band="admitting", spill_reason=None, remote_allowed=True, pool=None)
    return kgt.GateTickBuilder(gate, fleet_dir=lambda: fleet, kanban_cfg=lambda: cfg,
                               ledger=lambda boards: {}, connect=connect,
                               probe=lambda h: (1.0, 16)), gate


@pytest.mark.parametrize("remote", [True, False])
def test_a_scheduled_card_woken_inside_dispatch_gets_the_plan(home, remote):
    """Apollo review cafa4cce2458: no ready card, one portable card whose timed
    wake is due. The tick still plans, and dispatch_once (which wakes it)
    places it on the pool; t_82b4db80: a retired `local` value changes nothing."""
    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        assert kb.schedule_task(conn, tid, reason="later")
        conn.execute("UPDATE tasks SET next_eligible_at = ? WHERE id = ?", (int(time.time()) - 5, tid))
        conn.commit()
        assert conn.execute("SELECT count(*) FROM tasks WHERE status='ready'").fetchone()[0] == 0
    cfg = {"placement": {"read_signal": False, "default": "remote" if remote else "local"},
           "worker_pool": {"overflow": False}}
    b, gate = _builder(home, cfg, lambda board=None: kb.connect(board=board))
    plan = b.plan_pool([{"slug": "default"}], {})
    assert plan is not None and plan.default_remote is True
    with kb.connect_closing() as conn:
        res, spawned = _tick(conn, plan)
        assert tid in res.woken_scheduled
        assert spawned == [(tid, "ace-ai")], spawned
        (rec,) = _placements(conn, tid)
        assert rec["reason"] == "pool" and rec["placement_default"] == "remote"


def test_a_promotable_todo_card_also_gets_the_plan(home):
    with kb.connect_closing() as conn:
        (parent,) = _cards(conn, PORTABLE)
        child = kb.create_task(conn, title="child", assignee="alpha", body=PORTABLE, parents=[parent])
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0] == "todo"
    cfg = {"placement": {"read_signal": False, "default": "remote"}, "worker_pool": {"overflow": False}}
    b, _gate = _builder(home, cfg, lambda board=None: kb.connect(board=board))
    assert b.plan_pool([{"slug": "default"}], {}) is not None


def test_a_browser_joined_row_keeps_its_host_under_remote(home, monkeypatch):
    """Prism 6341bf7d12fb: a browser join is cut for the Studio (the portable
    row's host at gate time). Remote-first must not move it, or the lease
    refuses BROWSER_JOIN_HOST_MOVED every tick."""
    leased = []

    def gate(conn, ready_rows, result, row_route, dry_run, default_assignee=None, **kw):
        for r in ready_rows:
            result.browser_joins[r["id"]] = {"plans": [{"host": "ci-box"}], "source_host": "studio"}

    def lease(conn, claimed, join, placed_host):
        leased.append(placed_host.name if placed_host is not None else "studio")
        return ({}, None) if leased[-1] == join["source_host"] else (None, "BROWSER_JOIN_HOST_MOVED")

    monkeypatch.setattr(kbd, "_browser_gate", gate)
    monkeypatch.setattr(kbd, "_browser_join_lease", lease)
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None, browser_env=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    with kb.connect_closing() as conn:
        (tid,) = _cards(conn, PORTABLE)
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=4, spawn_paused=None,
                          spillover=_plan(), reconcile_orphans=False)
        assert leased == ["studio"] and spawned == [(tid, "studio")]
