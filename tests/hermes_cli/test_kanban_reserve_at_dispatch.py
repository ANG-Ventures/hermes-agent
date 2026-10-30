"""Allocation 2.3 (t_08de4ba5): the kanban dispatcher RESERVES at dispatch.

Through the real dispatcher seam (``dispatch_once`` -> ``SpilloverPlan.take``
-> ``placement.admit``): 20 ready cards in ONE tick against a host budget for 4
-> exactly 4 placed, 16 wait with a named reason, 20 admissions.jsonl rows;
run end releases (a ``released`` row), an orphan is reaped at TTL by the gate
tick (an ``expired`` row), and a card's ``footprint:`` line overrides the class
default. The probe seam is the fake ssh runner of test_kanban_headroom_placement;
nothing opens a connection and the real spawn raises.
"""

from __future__ import annotations

import json
import time

from tests.hermes_cli.test_kanban_headroom_placement import (  # noqa: F401  (fixture)
    TABLE, Fleet, _plan, home,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_headroom as kh
from hermes_cli.fleet_placement import placement


def _budget_for_four(home):
    """ci-box only, memavail = 40 reserve + 4 x 3 GB kanban footprints."""
    doc = json.loads(json.dumps(TABLE))
    doc["hosts"] = {"ci-box": doc["hosts"]["ci-box"]}
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(doc), encoding="utf-8")
    f = Fleet()
    f.mem["ci-box"] = 52.0
    return f


def _adm(home):
    p = home / "var" / "placement" / placement.ADMISSIONS_REL.name
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _dispatch(home, fleet, n, *, bodies=None):
    plan = _plan(home, fleet, now=time.time())
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    with kb.connect_closing() as conn:
        ids = [kb.create_task(conn, title=f"c{i}", assignee="alpha", body=(bodies or {}).get(i))
               for i in range(n)]
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused=None, spawn_limit=0,
                                spillover=plan, reconcile_orphans=False)
    return ids, spawned, res


def test_twenty_dispatches_in_one_tick_against_budget_for_four(home):
    ids, spawned, res = _dispatch(home, _budget_for_four(home), 20)
    assert len(spawned) == 4 and {h for _, h in spawned} == {"ci-box"}
    waits = res.placement_waits
    assert len(waits) == 16 and set(waits) == set(ids) - {t for t, _ in spawned}
    assert all(w.startswith("pool_unavailable (no_headroom:") and "ci-box no_headroom:mem" in w
               for w in waits.values()), waits
    rows = _adm(home)
    assert len(rows) == 20 and sorted(r["job_id"] for r in rows) == sorted(ids)
    dec = sorted(r["decision"] for r in rows)
    assert dec == ["admitted"] * 4 + ["deferred"] * 16
    for r in rows:
        assert {"at", "host", "consumer", "job_id", "footprint", "band", "decision", "reason"} <= set(r)
        assert r["consumer"] == "kanban"
    adm = [r for r in rows if r["decision"] == "admitted"]
    assert sorted(r["band"] for r in adm) == ["ok", "ok", "ok", "warm"]
    # every placed run's worker_placed event names the reservation its ledger row carries
    with kb.connect_closing() as conn:
        rids = {kh.reservation_for_run(conn, t, conn.execute("SELECT current_run_id FROM tasks WHERE id = ?",
                                                                (t,)).fetchone()[0]) for t, _ in spawned}
    assert rids == {r["reservation"] for r in adm}
    # the next tick: the 16 waiting cards stay deferred and are NOT re-logged (same reason, < 15 min)
    plan = _plan(home, _budget_for_four(home), now=time.time())
    with kb.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1, max_spawn=64, spawn_paused=None, spawn_limit=0,
                          spillover=plan, reconcile_orphans=False)
    assert len(_adm(home)) == 20


def test_run_end_releases_and_the_tick_reaps_orphans(home):
    ids, spawned, _ = _dispatch(home, _budget_for_four(home), 4)
    assert len(spawned) == 4
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, spawned[0][0], summary="done")
    rel = [r for r in _adm(home) if r["decision"] == "released"]
    assert [r["job_id"] for r in rel] == [spawned[0][0]]
    # three holders die without a release; their leases lapse (TTL), the gate tick reaps them
    from gateway import kanban_gate_tick as kgt
    from types import SimpleNamespace
    gate = SimpleNamespace(band="spilling", spill_reason="load", remote_allowed=True, pool=None, cost=2.0)
    b = kgt.GateTickBuilder(gate, fleet_dir=lambda: home / "fleet", kanban_cfg=lambda: {},
                            ledger=lambda boards: {}, connect=lambda board=None: kb.connect(board=board))
    b._boards = []
    got = b.renew_reservations(now=time.time() + kh.RESERVE_TTL_S + 1)
    assert got is not None and got["reaped"] == 3
    exp = [r for r in _adm(home) if r["decision"] == "expired"]
    assert sorted(r["job_id"] for r in exp) == sorted(t for t, _ in spawned[1:])
    assert placement.live_reservations(home / placement.RESERVATIONS_REL, now=time.time() + kh.RESERVE_TTL_S + 2) == {}


def test_card_footprint_line_overrides_the_class_default(home):
    _, spawned, _ = _dispatch(home, _budget_for_four(home), 1,
                              bodies={0: "Build the thing.\nfootprint: ram_gb=12 threads=4 scratch_gb=5\n"})
    assert len(spawned) == 1, (_, spawned, _adm(home))
    row = _adm(home)[-1]
    assert row["decision"] == "admitted" and row["footprint"] == {"ram_gb": 12.0, "threads": 4.0,
                                                                  "scratch_gb": 5.0, "gpu_mb": 0.0}
    assert row["band"] == "warm"     # 12 GB of a 12 GB budget: admits this one, no room for another
    # the budget is spent by the ONE big card: the next default card defers
    _, spawned2, res = _dispatch(home, _budget_for_four(home), 1)
    assert spawned2 == [] and len(res.placement_waits) == 1
