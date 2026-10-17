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
    with kb.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, max_spawn=0, spillover=None,
                          reconcile_orphans=False, entry="cli")
        assert not (home / "state" / "fleet-pause" / "hold-status.json").exists()


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
    if not l1:
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

