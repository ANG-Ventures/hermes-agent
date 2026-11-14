"""Every kanban start authorizes through the door (t_02aa6d86, FWA gap 3/3; doctrine rule 15).

Measured 2026-11-06 19:05-21:05 PST: 35 of 41 worker spawns were Studio-local. The LoadGate admitted them
(admission.admit_class over the Studio's own sample) but the start booked NOTHING on the shared reservation ledger,
so a Prism / test-gate admission on the Studio the same tick could not see them, and the LoadGate in turn could not
see what other consumers had booked there.

Red-first arms (each fails on the pre-t_02aa6d86 dispatcher):
  book      a local start writes a ``kanban`` row for host mac-studio (the table's ``local`` alias) BEFORE launch and a
            ``local_admitted`` run event carrying the reservation; ``admissions.jsonl`` gets an ``admitted`` line
            whose job_id is ``<card>#<run_id>``.
  release   the run's end releases that row (``_end_run`` -> on_commit), as a pool run's worker_placed row is.
  renew     ``renew_running`` renews it while the run is live.
  closed    an unreadable shared ledger refuses the start: no worker process, no charged failure.
  foreign   the LoadGate counts another consumer's live lease on this host (a Prism review admitted a moment ago)
            and refuses the start that would cross the line; kanban's own rows are not counted twice.
  pin       a pinned (``bind_host`` / ``worktree_port``) continuation whose host is saturated waits typed capacity
            (``pin_host_unified_admission``) and books no row: the pin chooses the host, the door authorizes the start.
Kill switch: KANBAN_LOCAL_LEDGER=0 books nothing (the old behaviour).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_load_gate as klg
from hermes_cli.fleet_placement import admission as ad
from hermes_cli.fleet_placement import placement

TABLE = {
    "schema": 1, "default_first": "mac-studio", "saturate_to": 1.0,
    "footprints": {"kanban": {"ram_gb": 3, "threads": 1}, "prism": {"ram_gb": 4, "threads": 1}},
    "hosts": {"mac-studio": {"cores": 24, "threads": 32, "mem_total_gb": 512, "mem_reserve_gb": 128,
                             "cores_reserve": 4, "consumers": {}, "aliases": ["studio", "local"],
                             "unmeasurable_axes": ["psi_cpu_some10", "psi_mem_some10", "system_pct"]}},
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("PLACEMENT_HOME", raising=False)
    monkeypatch.delenv("KANBAN_LOCAL_LEDGER", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (home / "config").mkdir()
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    return home


def _ledger(home) -> list:
    p = home / placement.RESERVATIONS_REL
    return json.loads(p.read_text(encoding="utf-8"))["rows"] if p.exists() else []


def _admissions(home) -> list:
    p = home / placement.ADMISSIONS_REL
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _dispatch_one(conn, spawned: list):
    def spawn(task, workspace, **kw):
        # BEFORE launch: the row is already in the ledger when the worker process starts
        spawned.append((task.id, [r.get("job_id") for r in _ledger(Path(kb.kanban_home()))]))
        return None
    return kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=4)


def _events(conn, tid, kind):
    return [json.loads(r[0] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind)).fetchall()]


def test_local_start_books_the_shared_ledger_before_launch(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local", assignee="alpha")
        spawned: list = []
        res = _dispatch_one(conn, spawned)
        assert [s[0] for s in res.spawned] == [tid], res
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        unit = f"{tid}#{run_id}"
        assert spawned == [(tid, [unit])]                         # booked before the spawn
        rows = _ledger(home)
        assert [(r["host"], r["consumer"], r["job_id"]) for r in rows] == [("mac-studio", "kanban", unit)]
        (ev,) = _events(conn, tid, kh.LOCAL_ADMITTED_EVENT)
        assert ev[kh.RESERVATION_KEY] == rows[0]["id"] and ev["unit"] == unit
        assert not _events(conn, tid, "worker_placed")            # still a local run for every worker_placed reader
        assert [(a["consumer"], a["host"], a["job_id"], a["decision"]) for a in _admissions(home)] == [
            ("kanban", "mac-studio", unit, "admitted")]
        assert kh.reservation_for_run(conn, tid, run_id) == rows[0]["id"]
        assert kh.running_reservations(conn) == [(tid, rows[0]["id"], None)]


def test_run_end_releases_and_tick_renews_the_local_lease(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local", assignee="alpha")
        _dispatch_one(conn, [])
        (row,) = _ledger(home)
        got = kh.renew_running([{"slug": "default"}], lambda board=None: kb.connect(board=board),
                               home / placement.RESERVATIONS_REL, now=time.time() + 60)
        assert got == {"renewed": 1, "lapsed": 0}
        assert _ledger(home)[0]["until"] > row["until"]
        kb.complete_task(conn, tid, result="done", summary="done")
    assert _ledger(home) == []
    assert [a["decision"] for a in _admissions(home)][-1] == "released"


def test_unreadable_shared_ledger_refuses_the_local_start(home):
    rpath = home / placement.RESERVATIONS_REL
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text("{not json", encoding="utf-8")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local", assignee="alpha")
        spawned: list = []
        res = _dispatch_one(conn, spawned)
        assert spawned == [] and res.spawned == [], res           # no worker process
        assert tid in res.spawn_failed
        status, fails = conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id = ?",
                                     (tid,)).fetchone()
        assert status == "ready" and (fails or 0) == 0            # a host fault, never charged to the card


def test_kill_switch_books_nothing(home, monkeypatch):
    monkeypatch.setenv("KANBAN_LOCAL_LEDGER", "0")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local", assignee="alpha")
        res = _dispatch_one(conn, [])
        assert [s[0] for s in res.spawned] == [tid]
        assert _ledger(home) == [] and not _events(conn, tid, kh.LOCAL_ADMITTED_EVENT)


def test_no_local_host_in_the_table_books_nothing(home):
    t = json.loads(json.dumps(TABLE))
    t["hosts"]["mac-studio"]["aliases"] = ["studio"]
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(t), encoding="utf-8")
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="local", assignee="alpha")
        res = _dispatch_one(conn, [])
        assert len(res.spawned) == 1 and _ledger(home) == []


# ------------------------------------------------------------------- the LoadGate reads the other consumers' leases

def _studio_gate(monkeypatch):
    c = {"host": "mac-studio", "ceiling": None, "ceiling_reason": "-",
         "need": ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior"),
         "limits": ad.Limits(mem_reserve_gb=128.0, unmeasurable=("psi_cpu_some10", "psi_mem_some10")),
         "mem_available_gb": 360.0, "mem_total_gb": 512.0}
    monkeypatch.setattr(klg, "utilization_context", lambda: dict(c))
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0, "psi_io_some10": 0.0})
    monkeypatch.setattr(klg, "unified_admit", lambda env=None: False)
    return klg.LoadGate({"max_spawn_per_tick": 4}, 32)


def test_loadgate_counts_another_consumers_lease_on_this_host(home, monkeypatch):
    g = _studio_gate(monkeypatch)
    now = time.time()
    # alone: 0.85 + 1/32 = 0.881 < 0.90 -> one worker fits
    assert g.admit(5.0, now=now, running=0, cpu_busy=0.85)[0] == 1
    placement.reserve("mac-studio", "prism", placement.Footprint(ram_gb=4, threads=2), ttl_s=600, job_id="pr#1",
                      path=home / placement.RESERVATIONS_REL, now=now)
    g = _studio_gate(monkeypatch)
    a, why = g.admit(5.0, now=now, running=0, cpu_busy=0.85)    # + the Prism review just admitted: 0.944
    assert a == 0 and "saturated:cpu" in (why or ""), (a, why)


def test_loadgate_does_not_count_kanbans_own_rows_twice(home, monkeypatch):
    now = time.time()
    for i in range(3):
        placement.reserve("mac-studio", "kanban", placement.Footprint(ram_gb=3, threads=1), ttl_s=600,
                          job_id=f"t_{i}#1", path=home / placement.RESERVATIONS_REL, now=now)
    assert klg.shared_leases("mac-studio", root=home, now=now) == []
    assert _studio_gate(monkeypatch).admit(5.0, now=now, running=0, cpu_busy=0.50)[0] == 4


def test_loadgate_fails_closed_on_an_unreadable_shared_ledger(home, monkeypatch):
    rpath = home / placement.RESERVATIONS_REL
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text("{not json", encoding="utf-8")
    a, why = _studio_gate(monkeypatch).admit(5.0, now=time.time(), running=0, cpu_busy=0.10)
    assert a == 0 and "shared ledger unreadable" in why, why


def test_loadgate_default_clock_does_not_double_book_a_settled_foreign_lease(home, monkeypatch):
    """Prism c-79e25e334a28b343: admit_now() samples on time.monotonic() while a shared-ledger lease is dated on the
    wall clock. A Prism review admitted an hour ago is already inside cpu_busy; read on mismatched clocks it looked
    newer than the sample and was counted a second time (0.85 + 2/32 + 1/32 > 0.90: saturated:cpu)."""
    monkeypatch.setenv("PLACEMENT_HOME", str(home))
    placement.reserve("mac-studio", "prism", placement.Footprint(ram_gb=4, threads=2), ttl_s=7200, job_id="pr#1",
                      path=home / placement.RESERVATIONS_REL, now=time.time() - 3600)
    assert len(klg.shared_leases("mac-studio")) == 1
    g = _studio_gate(monkeypatch)
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (5.0, 5.0))
    monkeypatch.setattr(klg, "sample_cpu_busy", lambda prev=None, block=0.0: (0.85, object()))
    monkeypatch.setattr(klg, "sample_user_procs", lambda: (None, None))
    a, why = g.admit_now(running=0)                               # production clock: now = time.monotonic()
    assert a == 1 and why is None, (a, why)


def test_loadgate_admission_and_book_local_read_the_same_table(home, tmp_path, monkeypatch):
    """Prism c-8f2dec23b3afdc11: the LoadGate's admission (utilization_context) and book_local read ONE host table, the
    fleet root's. A ``local`` host defined only in the board-home table is resolved by neither (no admission on a row
    the start could not book); defined at the fleet root, both resolve it and the start books there."""
    no_local = json.loads(json.dumps(TABLE))
    no_local["hosts"]["mac-studio"]["aliases"] = ["studio"]
    fleet = tmp_path / "fleet-root"
    (fleet / "config").mkdir(parents=True)
    (fleet / "config" / "fleet-hosts.json").write_text(json.dumps(no_local), encoding="utf-8")
    monkeypatch.setenv("PLACEMENT_HOME", str(fleet))
    assert kh.table_path(home).is_file()                          # board home: mac-studio carries the local alias
    assert klg.utilization_context()["host"] == "local" and klg.utilization_context()["ceiling"] is None
    assert kh.book_local("t_x#1", None) is None
    (fleet / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    assert klg.utilization_context()["host"] == "mac-studio"
    rid = kh.book_local("t_x#2", None)
    assert rid and [r["host"] for r in _ledger(fleet)] == ["mac-studio"] and _ledger(home) == []


def test_unmeasured_cpu_busy_refuses_closed(home, monkeypatch):
    """Card item 2: ``(load N tasks / ..., not read)`` names load1 as NOT READ by admission (display only); the axis
    admission reads is cpu_busy. A tick with cpu_busy unmeasured refuses, never a load1-only admit."""
    g = _studio_gate(monkeypatch)
    a, why = g.admit(1.0, now=time.time(), running=0, cpu_busy=None)
    assert a == 0 and "unmeasured:cpu_busy" in why, (a, why)
    assert "not read" in g._line() and "cpu_busy=?" in g._line()


# ------------------------------------------- pinned continuations (bind_host / worktree_port) and null stall axes
# Replay of the 2026-11-06 20:00 frame shape (t_0f1de3b6 run 61634: route remote-pin, rule
# bind_host:ace-ai(worktree_port)). The pin chooses the host; the start still authorizes through the ONE rule under
# the ledger lock (book_pin(check=True)), so a pin onto a saturated host waits typed, books no row, and lands once the
# host has room. Fixtures from the unified-admit suite (same pool files, same probe).

from tests.hermes_cli.test_kanban_unified_admit import Fleet, _plan, home as pool_home  # noqa: E402,F401


def _pool_rows(home):
    return [r for r in _ledger(home) if r.get("consumer") == "kanban"]


def test_pinned_continuation_on_a_saturated_host_waits_and_books_nothing(pool_home):
    t0 = time.time()
    f = Fleet(t0)
    f.busy["ace-ai"] = 0.95                                       # over the 0.90 fleet line
    plan = _plan(pool_home, f)
    assert plan.take("alpha", pin="ace-ai", job_id="t_pin") is None
    assert plan.refusal.startswith("pin_host_unified_admission (ace-ai: saturated:cpu"), plan.refusal
    assert _pool_rows(pool_home) == []                            # the pin landed nothing, booked nothing


def test_pinned_continuation_with_room_books_its_lease(pool_home):
    t0 = time.time()
    f = Fleet(t0)
    plan = _plan(pool_home, f)
    assert plan.take("alpha", pin="ace-ai", job_id="t_pin").name == "ace-ai"
    assert [(r["host"], r["job_id"]) for r in _pool_rows(pool_home)] == [("ace-ai", "t_pin")]


@pytest.mark.parametrize("axis", ["psi_cpu_some10", "psi_mem_some10", "psi_io_some10"])
def test_a_null_stall_axis_on_a_linux_host_is_closed(pool_home, axis):
    """Operator addendum (a), 2026-11-06 21:3x: ace-media published psi_* = None. An unmeasured stall axis on a host
    that reports PSI reads CLOSED for a kanban start, on the pool pick and on a pinned take alike. Pinning test: this
    already holds on main (admit_class fails closed on a missing avg10 axis); it locks the behaviour the addendum asks
    for so a later change to the probe's sample_fields cannot reopen it."""
    from hermes_cli import kanban_headroom as _kh
    t0 = time.time()
    f = Fleet(t0)
    f.extra["ace-media"] = {axis: None}
    # hermes-home #11513 (t_e0fbc8c9): under cpu_busy 0.5 PSI cpu is quota throttling and exempt, measured or not;
    # at the floor the null cpu stall axis still closes the host
    f.busy["ace-media"] = ad.PSI_CPU_BUSY_FLOOR
    plan = _plan(pool_home, f)
    keep, held = _kh.unified_filter(plan, ["ace-media"], None)
    assert keep == [] and held["ace-media"].startswith(f"unmeasured:{axis}"), held
    assert plan.take("alpha", pin="ace-media", job_id="t_pin") is None
    assert plan.refusal.startswith(f"pin_host_unified_admission (ace-media: unmeasured:{axis}"), plan.refusal


def test_local_start_books_renews_and_releases_on_the_fleet_ledger_when_homes_differ(home, tmp_path, monkeypatch):
    """Prism 681e7446f5d8: kanban_home() and placement.fleet_root() can be different directories. A local start must
    book on the FLEET ledger (the one Prism, allocd and test-gate read), and its release must hit the same row."""
    fleet = tmp_path / "fleet-root"
    (fleet / "config").mkdir(parents=True)
    (fleet / "config" / "fleet-hosts.json").write_text((home / "config" / "fleet-hosts.json").read_text())
    monkeypatch.setenv("PLACEMENT_HOME", str(fleet))
    from hermes_cli import kanban_headroom as kh
    now = time.time()
    rid = kh.book_local("t_x#1", None, now=now)
    assert rid, "no booking"
    assert placement.live_reservations(fleet / placement.RESERVATIONS_REL, now=now), "not on the fleet ledger"
    assert not (home / placement.RESERVATIONS_REL).exists() or not placement.live_reservations(
        home / placement.RESERVATIONS_REL, now=now), "booked on the board home instead"
    kh.release_local(rid)
    assert not placement.live_reservations(fleet / placement.RESERVATIONS_REL, now=now), "release missed the row"


# ---- one root (Prism 87f95a7e001f / baca55317404 / c17555de1567): the gate tick's renewal gate, its reaper and the
# target signal's table/ledger pair all read the fleet root (kh.ledger_root()), never the board home.

def _fleet_only(home, tmp_path, monkeypatch, table=TABLE):
    fleet = tmp_path / "fleet-root"
    (fleet / "config").mkdir(parents=True)
    (fleet / "config" / "fleet-hosts.json").write_text(json.dumps(table), encoding="utf-8")
    (home / "config" / "fleet-hosts.json").unlink()
    monkeypatch.setenv("PLACEMENT_HOME", str(fleet))
    return fleet


def _builder(**kw):
    from gateway import kanban_gate_tick as gt
    b = gt.GateTickBuilder(None, fleet_dir=lambda: kb.kanban_home() / "fleet", kanban_cfg=lambda: kw.get("cfg", {}),
                           ledger=lambda boards: kb.count_running_by_placement(boards),
                           connect=lambda board=None: kb.connect(board=board))
    b._boards = [{"slug": "default"}]
    return b


def test_gate_tick_renews_local_lease_when_table_only_at_fleet_root(home, tmp_path, monkeypatch):
    fleet = _fleet_only(home, tmp_path, monkeypatch)
    t0 = time.time()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local", assignee="alpha")
        _dispatch_one(conn, [])
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "running"
    (row,) = _ledger(fleet)
    assert not kh.table_path(home).exists() and _ledger(home) == []
    got = _builder().renew_reservations(now=time.time() + kh.RENEW_EVERY_S + 60)
    assert got is not None and got["renewed"] == 1 and got["lapsed"] == 0, got
    assert _ledger(fleet)[0]["until"] > row["until"]
    assert placement.live_reservations(fleet / placement.RESERVATIONS_REL, now=t0 + kh.RESERVE_TTL_S + 120)


def test_gate_tick_reaps_the_fleet_ledger(home, tmp_path, monkeypatch):
    fleet = _fleet_only(home, tmp_path, monkeypatch)
    now = time.time()
    placement.reserve("mac-studio", "kanban", placement.Footprint(ram_gb=3, threads=1), ttl_s=5,
                      job_id="t_dead#1", path=fleet / placement.RESERVATIONS_REL, now=now - 100)
    got = _builder().renew_reservations(now=now + kh.RENEW_EVERY_S + 60)
    assert got is not None and got.get("reaped") == 1, got
    assert _ledger(fleet) == []
    assert [(a["job_id"], a["decision"]) for a in _admissions(fleet)][-1] == ("t_dead#1", "expired")
    assert not (home / placement.RESERVATIONS_REL).exists()


def test_target_signal_table_and_ledger_share_the_fleet_root(home, tmp_path, monkeypatch):
    other = json.loads(json.dumps(TABLE))
    other["hosts"]["board-only"] = other["hosts"].pop("mac-studio")
    other["default_first"] = "board-only"
    (home / "config" / "fleet-hosts.json").write_text(json.dumps(other), encoding="utf-8")
    fleet = tmp_path / "fleet-root"
    (fleet / "config").mkdir(parents=True)
    (fleet / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    monkeypatch.setenv("PLACEMENT_HOME", str(fleet))
    from gateway import kanban_gate_tick as gt
    monkeypatch.setattr(gt, "read_signal_enabled", lambda cfg: True)
    sig = _builder().target_signal({})
    assert sig is not None and sig.placement_table is not None
    assert Path(sig.placement_reservations).parent.parent.parent == fleet
    assert "mac-studio" in sig.placement_table.hosts and "board-only" not in sig.placement_table.hosts


def test_gate_tick_renewal_control_table_at_both_roots(home, tmp_path, monkeypatch):
    """Control: with the table at both roots (the pre-split layout) renewal and reaping still target the fleet ledger."""
    fleet = tmp_path / "fleet-root"
    (fleet / "config").mkdir(parents=True)
    (fleet / "config" / "fleet-hosts.json").write_text(json.dumps(TABLE), encoding="utf-8")
    monkeypatch.setenv("PLACEMENT_HOME", str(fleet))
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="local", assignee="alpha")
        _dispatch_one(conn, [])
    (row,) = _ledger(fleet)
    got = _builder().renew_reservations(now=time.time() + kh.RENEW_EVERY_S + 60)
    assert got["renewed"] == 1 and got["reaped"] == 0, got
    assert _ledger(fleet)[0]["until"] > row["until"] and _ledger(home) == []


# ---- Prism b9b45a76cc17: a kanban kind pause (state/fleet-pause/kinds.json) closes LOCAL starts too. The pool path
# closed the paused hosts in placement.choose(); the LoadGate (admit_class) and _book_local_start -> book_local never
# read the file, so a spawn-only all-host kanban pause still launched every Studio-local worker.

def _kinds(home, **rows):
    p = home / placement.KINDS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"rows": rows}), encoding="utf-8")


def _row(scope="all", until=None, kind="kanban"):
    return {"kind": kind, "scope": scope, "until": time.time() + 3600 if until is None else until, "id": "p1"}


@pytest.mark.parametrize("ledger", ["1", "0"])
def test_all_host_kanban_kind_pause_refuses_the_local_start(home, monkeypatch, ledger):
    monkeypatch.setenv("KANBAN_LOCAL_LEDGER", ledger)              # the kill switch never reopens a paused host
    _kinds(home, k1=_row())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local", assignee="alpha")
        spawned: list = []
        res = _dispatch_one(conn, spawned)
        assert spawned == [] and res.spawned == [], res           # no worker process
        status, fails = conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id = ?",
                                     (tid,)).fetchone()
        assert status == "ready" and (fails or 0) == 0            # a host pause, never charged to the card
    assert _ledger(home) == []
    assert kh.local_kind_pause() == "kind_paused:kanban@all p1"


def test_kanban_kind_pause_closes_the_loadgate(home, monkeypatch):
    _kinds(home, k1=_row(scope="studio"))                         # alias of the local host
    g = _studio_gate(monkeypatch)
    a, why = g.admit(5.0, now=time.time(), running=0, cpu_busy=0.10)
    assert a == 0 and why == "kind_paused:kanban@studio p1", (a, why)
    assert g.remote_allowed is True                               # other hosts answer for their own pause


@pytest.mark.parametrize("row", [_row(scope="ace-ai"), _row(until=1.0), _row(kind="prism")])
def test_unrelated_or_expired_kind_pause_leaves_the_local_start(home, row):
    _kinds(home, k1=row)
    assert kh.local_kind_pause() is None
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local", assignee="alpha")
        res = _dispatch_one(conn, [])
        assert [s[0] for s in res.spawned] == [tid], res


def test_unreadable_kinds_file_refuses_the_local_start_and_the_gate(home, monkeypatch):
    p = home / placement.KINDS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    a, why = _studio_gate(monkeypatch).admit(5.0, now=time.time(), running=0, cpu_busy=0.10)
    assert a == 0 and "kind pause unreadable" in why, why
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="local", assignee="alpha")
        res = _dispatch_one(conn, [])
        assert res.spawned == [], res
