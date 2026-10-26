"""Fleet pause hold (SPEC v0.3 §5.8, AC12 / AC9 / AC19 / AC20 / AC21; build card t_133d6efd).

Hermetic: a scratch board under HERMES_KANBAN_HOME, a profile-scoped HERMES_HOME pointing ELSEWHERE (the
manifest must be read from kanban_home(), STRIDE-T), the real non-dry ``dispatch_once`` with a recording
``spawn_fn``. Every arm runs with a fixture pool plan AND with ``spillover=None`` (the no-plan branch, N1).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pause_hold as kph
from hermes_cli import kanban_worker_pool as kwp

MID = "20261017T153000Z-ci-box-3f9a2c1d"


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in pause-hold test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "root"
    prof = h / "profiles" / "alpha"
    prof.mkdir(parents=True)
    # a PROFILE-scoped HERMES_HOME: the manifest must still be read under kanban_home() (= the root)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    # an inherited override must never steer init_db at a real board (Prism 131f3bd26253)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    assert kb.kanban_home() == h
    # a manifest under the PROFILE home must never be read (STRIDE-T arm)
    decoy = prof / "state" / "fleet-pause" / "ci-box"
    decoy.mkdir(parents=True)
    (decoy / "manifest.json").write_text(json.dumps({"host": "ci-box", "phase": "abandoned", "id": "decoy"}))
    return h


def _manifest(home, phase="paused", units=(), raw=None):
    d = home / "state" / "fleet-pause" / "ci-box"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(raw if raw is not None else json.dumps(
        {"schema": 2, "id": MID, "host": "ci-box", "phase": phase, "units": list(units)}))


def _host(name, slots=4):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=slots, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)


def _plan():
    hosts = [_host("ci-box"), _host("ace-ai")]
    cfg = kwp.PoolConfig(hosts=("ci-box", "ace-ai"), profiles=("alpha",), pool_hosts=tuple(hosts))
    p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=cfg)
    p.band, p.spill_reason = "admitting", None
    return p


def _tick(conn, plan, *, dry_run=False):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio"))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_limit=8, dry_run=dry_run,
                            spillover=plan, reconcile_orphans=False)
    return res, spawned


def _set(conn, tid, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*cols.values(), tid))


def _cards(conn):
    """A ready (reclaimed shape), B scheduled past its wake, C another host, D unpinned pool-placed,
    F a --continues child carrying the origin, E a review card. All but C have origin_host=ci-box."""
    ids = {}
    for key in "ABCDEF":
        ids[key] = kb.create_task(conn, title=f"card {key}", assignee="alpha", body="do the thing")
    for key in "ADF":
        _set(conn, ids[key], status="ready", origin_host="ci-box")
    _set(conn, ids["B"], status="scheduled", origin_host="ci-box", next_eligible_at=int(time.time()) - 60)
    _set(conn, ids["C"], status="ready", origin_host="ace-ai")
    _set(conn, ids["E"], status="review", origin_host="ci-box")
    return ids


def _routes(res):
    return {r["task_id"]: r for r in res.routes}


def _runs(conn, tid):
    return conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]


PLANS = [pytest.param(_plan, id="plan"), pytest.param(lambda: None, id="no-plan")]


# -- AC12 / arm (a) -------------------------------------------------------------

@pytest.mark.parametrize("mkplan", PLANS)
def test_paused_host_cards_are_held_in_both_plan_arms(home, mkplan):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home)
        res, spawned = _tick(conn, mkplan())
        routes = _routes(res)
        got = {t for t, _h in spawned}
        for key in "ABDF":
            r = routes[ids[key]]
            assert r["outcome"] == "held" and r["code"] == "pause_hold", (key, r)
            assert r["detail"] == f"ci-box:{MID}"
            assert ids[key] not in got and _runs(conn, ids[key]) == 0
        assert ids["B"] in res.woken_scheduled            # the tick's own wake made B ready
        assert ids["C"] in got                            # another host's card is untouched
        assert res.placement_waits[ids["E"]] == f"pause_hold (ci-box:{MID})"
        assert ids["E"] not in got and _runs(conn, ids["E"]) == 0
        # live tick wrote the placement_wait events (the status `held:` source)
        n = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='placement_wait' "
                         "AND json_extract(payload,'$.code')='pause_hold'").fetchone()[0]
        assert n >= 4


def test_profile_home_manifest_is_never_read(home):
    """Only the decoy under HERMES_HOME exists (phase abandoned): nothing is held. Then the real one holds."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        res, spawned = _tick(conn, None)
        assert ids["A"] in {t for t, _ in spawned}
    with kb.connect_closing() as conn:
        _set(conn, ids["D"], status="ready", claim_lock=None)
        _manifest(home)
        res, spawned = _tick(conn, None)
        assert _routes(res)[ids["D"]]["code"] == "pause_hold"


def test_no_manifest_and_no_origin_cost_nothing(home):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="plain", assignee="alpha", body="x")
        _manifest(home)
        res, spawned = _tick(conn, None)
        assert t in {s for s, _ in spawned}


# -- AC9 / arm (b) --------------------------------------------------------------

def test_resumed_unit_pins_to_origin_until_its_first_run(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, "resumed", [{"lane": "kanban", "id": ids["A"], "resume": {"new_run_id": None}}])
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["route"] == "remote-pin" and r["host"] == "ci-box"
        assert (ids["A"], "ci-box") in spawned
        # D is not listed: ordinary routing (not held, not pinned)
        assert _routes(res)[ids["D"]]["outcome"] != "held"


def test_resumed_unit_with_new_run_routes_ordinarily(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, "resumed", [{"lane": "kanban", "id": ids["A"], "resume": {"new_run_id": 7}}])
        res, spawned = _tick(conn, _plan())
        assert _routes(res)[ids["A"]]["route"] != "remote-pin"


def test_resumed_unit_with_no_plan_waits_pool_unavailable_and_tick_survives(home):
    """RC-P3-1: a pin route with spillover=None would reach spillover.take(None) and kill the tick."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, "resumed", [{"lane": "kanban", "id": ids["A"], "resume": {"new_run_id": None}}])
        res, spawned = _tick(conn, None)
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and r["code"] == "pool_unavailable"
        assert ids["C"] in {t for t, _ in spawned}       # the rest of the tick ran


# -- AC19 dispatcher half / arm (c) --------------------------------------------

@pytest.mark.parametrize("mkplan", PLANS)
def test_unreadable_manifest_holds_every_card_of_the_host(home, mkplan, caplog):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, raw="{")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_pause_hold"):
            res, spawned = _tick(conn, mkplan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and r["code"] == "pause_hold" and "unreadable" in r["detail"]
        assert ids["C"] in {t for t, _ in spawned}
        assert sum("unreadable" in m for m in caplog.messages) == 1   # one line per tick (tick-local cache)


# -- kill switch / arm (d) + AC20 dispatcher half / arm (e) --------------------

def _status(home):
    return json.loads((home / "state" / "fleet-pause" / "hold-status.json").read_text())


def test_kill_switch_off_routes_as_today_and_reports_off(home, monkeypatch):
    monkeypatch.setattr(kph, "enabled", lambda: False)
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home)
        res, spawned = _tick(conn, None)
        assert ids["A"] in {t for t, _ in spawned}
        assert _status(home)["mode"] == "off"


def test_live_gateway_tick_publishes_status_and_dry_run_does_not(home):
    with kb.connect_closing() as conn:
        _tick(conn, None)
        st = _status(home)
        assert st["mode"] == "on" and st["pid"] == os.getpid() and abs(st["updated_at"] - time.time()) < 30
        assert st["interval_s"] == 60.0
        (home / "state" / "fleet-pause" / "hold-status.json").unlink()
        _tick(conn, None, dry_run=True)
        assert not (home / "state" / "fleet-pause" / "hold-status.json").exists()


def test_cli_entry_never_publishes_status(home):
    """RC-P3-2: a one-shot `hermes kanban dispatch` (entry=cli) must not vouch for the gateway's code."""
    status = home / "state" / "fleet-pause" / "hold-status.json"
    with kb.connect_closing() as conn:
        _cards(conn)
        kw = dict(spawn_fn=lambda *a, **k: 4242, max_spawn=64, spawn_limit=8, spillover=None,
                  reconcile_orphans=False)
        kbd.dispatch_once(conn, entry="cli", **kw)
        assert not status.exists()
        kbd.dispatch_once(conn, entry="gateway", **kw)   # control: the same call shape DOES publish
        assert status.exists()


# -- AC21 / arm (f): layer 2 + the 2x2 layer matrix ----------------------------

def test_run_openers_refuse_a_held_card_directly(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home)
        for claim, key in ((kb.claim_task, "A"), (kb.claim_review_task, "E")):
            with pytest.raises(kph.PauseHeld) as ei:
                claim(conn, ids[key])
            assert (ei.value.host, ei.value.manifest_id) == ("ci-box", MID)
            assert _runs(conn, ids[key]) == 0
            st = conn.execute("SELECT status, claim_lock FROM tasks WHERE id=?", (ids[key],)).fetchone()
            assert st["status"] in ("ready", "review") and st["claim_lock"] is None
        # the opener writes nothing (its txn rolled back); the caller records the refusal (RC-P3-3)
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind=?", (kph.REFUSED_EVENT,)).fetchone()[0] == 0
        assert kb.claim_task(conn, ids["C"]) is not None


def test_cli_claim_refuses_with_rc2(home, capsys):
    from hermes_cli import kanban as kcli
    with kb.connect_closing() as conn:
        ids = _cards(conn)
    _manifest(home)
    import argparse
    rc = kcli._cmd_claim(argparse.Namespace(task_id=ids["A"], review=False, ttl=None))
    assert rc == 2
    assert MID in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert _runs(conn, ids["A"]) == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
                            (ids["A"], kph.REFUSED_EVENT)).fetchone()[0] == 1


def _matrix_tick(home, monkeypatch, *, l1, l2, plan):
    if not l1:   # layer 1 = the pre-route pass AND the post-route pass
        monkeypatch.setattr(kph, "held_routes", lambda rows, cache: {})
        monkeypatch.setattr(kph, "gate_rows",
                            lambda conn, rows, *a, lane, **k: rows)
    if not l2:
        monkeypatch.setattr(kph, "refuse_if_held", lambda conn, task_id: None)
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home)
        res, spawned = _tick(conn, plan)
        events = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind=?", (kph.REFUSED_EVENT,)).fetchone()[0]
    return ids, res, {t for t, _ in spawned}, events


def _stage_claim(res, ids):
    return {k for k in "ABDEF" if "stage=claim" in str(res.placement_waits.get(ids[k], ""))}


@pytest.mark.parametrize("mkplan", PLANS)
def test_matrix_both_layers_on(home, monkeypatch, mkplan, caplog):
    with caplog.at_level(logging.WARNING):
        ids, res, got, events = _matrix_tick(home, monkeypatch, l1=True, l2=True, plan=mkplan())
    assert not (got & {ids[k] for k in "ABDEF"})
    assert _stage_claim(res, ids) == set() and events == 0
    assert not any("bypassed gate_rows" in m for m in caplog.messages)


@pytest.mark.parametrize("mkplan", PLANS)
def test_matrix_layer1_off_layer2_catches_every_held_card(home, monkeypatch, mkplan, caplog):
    with caplog.at_level(logging.WARNING):
        ids, res, got, events = _matrix_tick(home, monkeypatch, l1=False, l2=True, plan=mkplan())
    assert not (got & {ids[k] for k in "ABDEF"})
    assert _stage_claim(res, ids) == set("ABDEF")
    assert events == 5
    assert any("bypassed gate_rows" in m for m in caplog.messages)


@pytest.mark.parametrize("mkplan", PLANS)
def test_matrix_layer2_off_layer1_still_holds(home, monkeypatch, mkplan):
    ids, res, got, events = _matrix_tick(home, monkeypatch, l1=True, l2=False, plan=mkplan())
    assert not (got & {ids[k] for k in "ABDEF"})
    assert _stage_claim(res, ids) == set() and events == 0


@pytest.mark.parametrize("mkplan", PLANS)
def test_matrix_both_off_control_spawns(home, monkeypatch, mkplan):
    """The control: with both layers off the same fixtures DO spawn, so the green cells are not vacuous."""
    ids, res, got, events = _matrix_tick(home, monkeypatch, l1=False, l2=False, plan=mkplan())
    assert {ids[k] for k in "ABDEF"} <= got



# -- Prism aceb690c4071: a corrupt manifest holds its host and never raises ----

CORRUPT = [
    pytest.param(b"\xff\xfe{}", id="non-utf8"),
    pytest.param(None, id="resume-not-dict"),
    pytest.param(json.dumps({"schema": 2, "id": MID, "host": "ci-box", "phase": "resumed", "units": 5}).encode(),
                 id="units-not-list"),
    pytest.param(("[" * 100000 + "]" * 100000).encode(), id="deep-nesting"),
]


def _corrupt(home, raw, task_id):
    if raw is None:   # a PIN_PHASES manifest whose unit carries a truthy non-dict resume
        raw = json.dumps({"schema": 2, "id": MID, "host": "ci-box", "phase": "resumed",
                          "units": [{"lane": "kanban", "id": task_id, "resume": "x"}]}).encode()
    d = home / "state" / "fleet-pause" / "ci-box"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_bytes(raw)


@pytest.mark.parametrize("raw", CORRUPT)
@pytest.mark.parametrize("mkplan", PLANS)
def test_corrupt_manifest_holds_its_host_and_the_tick_survives(home, mkplan, raw):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _corrupt(home, raw, ids["A"])
        res, spawned = _tick(conn, mkplan())
        got = {t for t, _ in spawned}
        for key in "ADF":
            r = _routes(res)[ids[key]]
            assert r["outcome"] == "held" and r["code"] == "pause_hold" and "unreadable" in r["detail"], (key, r)
            assert ids[key] not in got and _runs(conn, ids[key]) == 0
        assert res.placement_waits[ids["E"]] == "pause_hold (ci-box:unreadable)"
        assert ids["C"] in got                          # another host's card still dispatches this tick


@pytest.mark.parametrize("raw", CORRUPT)
def test_corrupt_manifest_layer2_raises_pause_held_only(home, raw):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _corrupt(home, raw, ids["A"])
        for claim, key in ((kb.claim_task, "A"), (kb.claim_review_task, "E")):
            with pytest.raises(kph.PauseHeld) as ei:
                claim(conn, ids[key])
            assert ei.value.manifest_id == kph.UNREADABLE


@pytest.mark.parametrize("raw", CORRUPT)
def test_load_itself_classifies_every_corrupt_shape_unreadable(home, raw):
    """The validation layer on its own (decide()'s catch-all would otherwise mask a missing check)."""
    _corrupt(home, raw, "t_x")
    assert kph._load("ci-box", {}) is kph._UNREADABLE


def test_decide_never_raises_on_an_unexpected_error(home, monkeypatch):
    def boom(host, cache):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(kph, "_load", boom)
    assert kph.decide("ci-box", "t_x", {}) == ("wait", f"pause_hold (ci-box:{kph.UNREADABLE})")


# -- Prism 76b7233abe29: the hold runs before any routing that mutates the row --

def _continuation_card(conn, home):
    tid = kb.create_task(conn, title="cont", assignee="alpha", body="do the thing")
    _set(conn, tid, status="ready", origin_host="ci-box", workspace_path="/tmp/ws")
    _manifest(home)
    return tid


@pytest.mark.parametrize("mkplan", PLANS)
def test_continuation_enforce_empty_probe_never_clears_a_held_origin(home, monkeypatch, mkplan):
    cfg = {"placement": {"continuation": "enforce"}}
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: cfg)
    monkeypatch.setattr(kb, "_board_home_kanban_cfg", lambda: cfg)
    with kb.connect_closing() as conn:
        tid = _continuation_card(conn, home)
        for _ in range(2):
            plan = mkplan()
            if plan is not None:
                plan.continuation_states = {("ci-box", "/tmp/ws"): "empty"}
                plan.continuation_probed = True
            res, spawned = _tick(conn, plan)
            assert tid not in {t for t, _ in spawned}
            row = conn.execute("SELECT origin_host FROM tasks WHERE id=?", (tid,)).fetchone()
            assert row["origin_host"] == "ci-box"
            assert _routes(res)[tid]["code"] == "pause_hold"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='origin_state_lost'",
                            (tid,)).fetchone()[0] == 0


def test_continuation_enforce_empty_probe_control_clears_without_a_pause(home, monkeypatch):
    """Control: the same fixture with NO manifest does clear the origin, so the held arm is not vacuous."""
    cfg = {"placement": {"continuation": "enforce"}}
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: cfg)
    monkeypatch.setattr(kb, "_board_home_kanban_cfg", lambda: cfg)
    with kb.connect_closing() as conn:
        tid = _continuation_card(conn, home)
        (home / "state" / "fleet-pause" / "ci-box" / "manifest.json").unlink()
        plan = _plan()
        plan.continuation_states = {("ci-box", "/tmp/ws"): "empty"}
        plan.continuation_probed = True
        _tick(conn, plan)
        assert conn.execute("SELECT origin_host FROM tasks WHERE id=?", (tid,)).fetchone()["origin_host"] is None


# -- Prism 536374336fb5: a resume pin never overrides a hard local restriction --

def test_resume_pin_never_overrides_a_hard_local_route(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="studio", assignee="alpha",
                             body="host: studio\nrequires: control-plane-read\nRead the board.")
        _set(conn, tid, status="ready", origin_host="ci-box")
        _manifest(home, "resumed", [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}])
        res, spawned = _tick(conn, _plan())
        assert (tid, "ci-box") not in spawned
        r = _routes(res)[tid]
        assert r["outcome"] == "held" and r["code"] == "pause_hold", r
        assert "resume_pin_conflict" in r["detail"]


def test_resume_pin_still_widens_an_ordinary_local_route(home):
    """Control for 536374336fb5: an unrestricted card (local with no hard rule) still pins to its origin."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, "resumed", [{"lane": "kanban", "id": ids["A"], "resume": {"new_run_id": None}}])
        res, spawned = _tick(conn, _plan())
        assert (ids["A"], "ci-box") in spawned


# -- Prism 01f1e61334ff: the resume fence holds for review cards ---------------

@pytest.mark.parametrize("mkplan", PLANS)
def test_resumed_review_card_never_spawns_locally(home, mkplan, monkeypatch):
    # the review lane prepares the remote workspace inline (ssh): record it instead
    monkeypatch.setattr(kb._kwh, "prepare_remote_workspace", lambda host, ws, **k: None)
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, "resumed", [{"lane": "kanban", "id": ids["E"], "resume": {"new_run_id": None}}])
        res, spawned = _tick(conn, mkplan())
        assert (ids["E"], "studio") not in spawned
        if mkplan() is None:
            assert ids["E"] not in {t for t, _ in spawned}
            assert res.placement_waits[ids["E"]] == "pool_unavailable"
        else:
            assert (ids["E"], "ci-box") in spawned


# -- Prism f2ecf9033a2d (t_8a8aad55): a lost review claim refunds its spillover take ---

def test_lost_review_claim_refunds_the_spillover_take(home, monkeypatch):
    monkeypatch.setattr(kb._kwh, "prepare_remote_workspace", lambda host, ws, **k: None)
    from hermes_cli import kanban_continuation as kc
    calls = []
    monkeypatch.setattr(kc, "record_placement", lambda *a, **k: calls.append(a) or False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rv", assignee="alpha", body="x")
        _set(conn, tid, status="review", origin_host="ci-box")
        _manifest(home, "resumed", [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}])
        plan = _plan()
        taken, slots = dict(plan.taken), dict(plan.slots)
        res, spawned = _tick(conn, plan)
        assert calls and calls[-1][3] == "ci-box"        # the review row reached the pinned record_placement
        assert tid not in {t for t, _ in spawned}
        assert {k: v for k, v in plan.taken.items() if v} == {k: v for k, v in taken.items() if v}
        assert plan.slots == slots


# -- Prism c3421355edd2: a resume pin never yields to a route to ANOTHER host ---

@pytest.mark.parametrize("route", [("pin", "ace-ai"), ("origin", "ace-ai")])
def test_resume_pin_waits_when_routing_chose_another_host(home, route):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="r", assignee="alpha", body="x")
        _set(conn, tid, status="ready", origin_host="ci-box")
        _manifest(home, "resumed", [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}])
        row = conn.execute("SELECT id, body, origin_host FROM tasks WHERE id=?", (tid,)).fetchone()
        rr = {tid: route}
        kph.gate_rows(conn, [row], None, rr, True, {}, lane="ready")
        assert rr[tid][0] == "wait" and "resume_pin_conflict:routed_to:ace-ai" in rr[tid][1]


def test_resume_pin_keeps_a_route_to_the_same_host(home):
    """Control: a pin/origin route that already targets the origin stays as routed."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="r", assignee="alpha", body="x")
        _set(conn, tid, status="ready", origin_host="ci-box")
        _manifest(home, "resumed", [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}])
        row = conn.execute("SELECT id, body, origin_host FROM tasks WHERE id=?", (tid,)).fetchone()
        rr = {tid: ("origin", "ci-box")}
        kph.gate_rows(conn, [row], None, rr, True, {}, lane="ready")
        assert rr[tid] == ("origin", "ci-box")


# -- Prism 904f143f5a38: a resumed review keeps the origin's hard checks --------

def test_resumed_review_requiring_studio_only_access_never_runs_on_origin(home, monkeypatch):
    cfg = {"placement": {"continuation": "enforce"}}
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: cfg)
    monkeypatch.setattr(kb, "_board_home_kanban_cfg", lambda: cfg)
    monkeypatch.setattr(kb._kwh, "prepare_remote_workspace", lambda host, ws, **k: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rv", assignee="alpha", body="requires: control-plane-read")
        _set(conn, tid, status="review", origin_host="ci-box")
        _manifest(home, "resumed", [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}])
        res, spawned = _tick(conn, _plan())
        assert (tid, "ci-box") not in spawned
        assert "origin_refused" in res.placement_waits[tid]
        assert conn.execute("SELECT origin_host FROM tasks WHERE id=?", (tid,)).fetchone()["origin_host"] == "ci-box"


@pytest.mark.parametrize("current, waits", [(("local", "studio_affinity:vault"), True), (("local", None), False),
                                            (("portable", None), False)])
def test_resume_pin_versus_a_local_route(home, current, waits):
    """536374336fb5 in isolation: only a local route that carries a rule blocks the resume pin."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="r", assignee="alpha", body="x")
        _set(conn, tid, status="ready", origin_host="ci-box")
        _manifest(home, "resumed", [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}])
        row = conn.execute("SELECT id, body, origin_host FROM tasks WHERE id=?", (tid,)).fetchone()
        rr = {tid: current}
        kph.gate_rows(conn, [row], None, rr, True, {}, lane="ready")
        if waits:
            assert rr[tid][0] == "wait" and "resume_pin_conflict:studio_affinity:vault" in rr[tid][1]
        else:
            assert rr[tid] == ("pin", "ci-box")


def test_resumed_review_pin_matches_the_registry_case_insensitively(home, monkeypatch):
    """Prism f4198478cbb2: origin `CI-Box` resumes on registered host `ci-box`, not origin_host_absent forever."""
    monkeypatch.setattr(kb._kwh, "prepare_remote_workspace", lambda host, ws, **k: None)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rv", assignee="alpha", body="x")
        _set(conn, tid, status="review", origin_host="CI-Box")
        d = home / "state" / "fleet-pause" / "CI-Box"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"schema": 2, "id": MID, "host": "CI-Box", "phase": "resumed",
                                                     "units": [{"lane": "kanban", "id": tid,
                                                                "resume": {"new_run_id": None}}]}))
        res, spawned = _tick(conn, _plan())
        assert (tid, "ci-box") in spawned, res.placement_waits.get(tid)


# -- t_be5633d4 (Apollo 05:58): the hold gates SPAWNS, not operator review verbs ------------------------------

def _review_held_card(conn, home):
    """A ci-box card that went through a real implementer run into review, then the host paused."""
    tid = kb.create_task(conn, title="rv", assignee="alpha", body="do the thing")
    impl = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="ready", reviewer="human", expected_run_id=impl.current_run_id)
    _set(conn, tid, origin_host="ci-box")
    _manifest(home)
    return tid


def _coverage():
    from hermes_cli.kanban_review_schema import REQUIRED_REVIEW_LENSES as LENSES
    return json.dumps({"verdict": "CHANGES_REQUESTED", "head_sha": "1ee13c3d39", "lenses": {k: "done" for k in LENSES},
                       "findings": 1, "items": ["fix the guard"], "review_minutes": 4, "batch_id": "b1"})


def test_operator_review_claim_is_allowed_while_paused(home):
    """`hermes kanban claim --review` spawns nothing: the pause never refuses it."""
    with kb.connect_closing() as conn:
        tid = _review_held_card(conn, home)
        task = kb.claim_review_task(conn, tid, session_ref="s-op", operator_claim=True)
        assert task is not None and task.status == "running"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind=?", (kph.REFUSED_EVENT,)).fetchone()[0] == 0


def test_dispatched_review_claim_is_still_refused_while_paused(home):
    with kb.connect_closing() as conn:
        tid = _review_held_card(conn, home)
        with pytest.raises(kph.PauseHeld):
            kb.claim_review_task(conn, tid)


def test_request_changes_is_allowed_while_paused_and_the_rework_stays_held(home, monkeypatch):
    """The operator send-back closes the review and hands the card back; the implementer's NEXT run is the
    spawn, and that one is still held (dispatcher tick + the ready opener)."""
    session = "20261018_060000_operator"
    monkeypatch.setenv("HERMES_SESSION_ID", session)
    with kb.connect_closing() as conn:
        tid = _review_held_card(conn, home)
        runs_before = _runs(conn, tid)
        ok, detail = kb.request_changes(conn, tid, reason="fix it", claimer="apollo", coverage=_coverage(),
                                        session_ref=kb.derive_session_ref(session))
        assert ok, detail
        assert _runs(conn, tid) == runs_before + 1          # the reviewer run the send-back opened and closed
        status = kb.get_task(conn, tid).status
        assert status in ("ready", "todo"), status
        _set(conn, tid, status="ready")
        res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}
        assert _routes(res)[tid]["code"] == "pause_hold"
        with pytest.raises(kph.PauseHeld):
            kb.claim_task(conn, tid)

