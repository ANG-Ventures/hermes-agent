"""The Studio's own HARD kanban-worker cap (t_b0a8a427).

2026-10-18 09:12: with ci-box's 24 slots out of the pool (first real
fleet-pause), 20 kanban workers landed on the Studio (load1 147 on 32 cores):
fleet-roles.json had no Studio row, so the local route had no slot count. A
``studio`` kanban-worker row now caps local workers like a remote host's slots,
and ``kanban-pool.json`` ``studio.capacity_pct`` is its load1/ncpu bar.
Portable cards spill to the pool at the cap; local-bound cards wait
``total:studio_cap`` / ``total:studio_load``.

Every dispatcher test runs under the AC-5 seam (no real spawn).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("real spawn in studio-cap test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    # A quiet host unless a test says otherwise: the load bar is not the subject.
    monkeypatch.setattr(kbd.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(kbd.os, "cpu_count", lambda: 32)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return home


def _write_fleet(home: Path, *, studio_slots=None, studio_state="active", studio_pct=None,
                 ci_box_state="active"):
    fleet = home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    hosts = {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
        "ace-media": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
        "ci-box": {"roles": {"kanban-worker": {"slots": 24}}, "state": ci_box_state},
    }
    if studio_slots is not None:
        hosts["studio"] = {"roles": {"kanban-worker": {"slots": studio_slots}}, "state": studio_state}
    side = {"schema": 1, "ssh_user": "kanbanw", "capacity_pct": 0.8,
            "priority": ["ci-box", "ace-ai", "ace-media"], "profiles": ["alpha"],
            "hosts": {"ci-box": {"enabled": True, "absence": "required"},
                      "ace-ai": {"enabled": True, "absence": "required"},
                      "ace-media": {"enabled": True, "absence": "optional"}}}
    if studio_pct is not None:
        side["studio"] = {"capacity_pct": studio_pct}
    (fleet / kwp.ROLES_FILE).write_text(json.dumps({"schema": 1, "hosts": hosts}), encoding="utf-8")
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps(side), encoding="utf-8")
    return fleet


def _host(name, slots, priority, state="active"):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=slots,
                        capacity_pct=0.8, absence="required", profiles=("alpha",),
                        state=state, enabled=True, priority=priority)


def _make(conn, n, *, assignee="alpha", priority=0):
    return [kb.create_task(conn, title=f"{assignee}-{i}", assignee=assignee, priority=priority)
            for i in range(n)]


def _tick(conn, *, spillover, spawn_limit=None):
    spawned = []

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=128, spawn_limit=spawn_limit,
                            spillover=spillover, reconcile_orphans=False)
    return res, spawned


def _local(spawned):
    return [t for t, h in spawned if h is None]


# -- reader ------------------------------------------------------------------

def test_studio_row_is_the_local_cap_never_a_pool_host(kanban_home):
    fleet = _write_fleet(kanban_home, studio_slots=6, studio_pct=0.9)
    cap, warning = kwp.read_local_cap(fleet)
    assert cap == kwp.LocalCap(slots=6, capacity_pct=0.9, state="active") and warning is None
    cfg = kwp.read_pool(fleet, kanban_cfg={})
    assert "studio" not in [h.name for h in cfg.pool_hosts]
    assert "studio" not in kwp.known_host_ids(fleet)
    assert not any("studio" in w for w in cfg.warnings), cfg.warnings


def test_no_studio_row_is_uncapped_and_a_draining_row_is_zero(kanban_home):
    fleet = _write_fleet(kanban_home)
    assert kwp.read_local_cap(fleet) == (None, None)
    fleet = _write_fleet(kanban_home, studio_slots=6, studio_state="draining")
    assert kwp.read_local_cap(fleet)[0].slots == 0


def test_sidecar_refuses_the_studio_as_a_pool_row_and_a_bad_bar():
    base = {"schema": 1, "ssh_user": "kanbanw", "priority": [], "hosts": {}}
    assert kwp.validate_sidecar({**base, "studio": {"capacity_pct": 0.9}}) == []
    assert kwp.validate_sidecar({**base, "studio": {"capacity_pct": 1.5}})
    assert kwp.validate_sidecar({**base, "studio": {"slots": 6}})
    assert kwp.validate_sidecar({**base, "hosts": {"studio": {"enabled": True, "absence": "optional"}}})


# -- the dispatcher ----------------------------------------------------------

def test_local_bound_cards_wait_total_studio_cap_at_the_slot_count(kanban_home):
    _write_fleet(kanban_home, studio_slots=3)
    with kb.connect_closing() as conn:
        ids = _make(conn, 10, assignee="beta")           # not a pool profile: local-bound
        res, spawned = _tick(conn, spillover=None)
    assert len(spawned) == 3 and _local(spawned) == [t for t, _ in spawned]
    held = [t for t in ids if t not in {s for s, _ in spawned}]
    assert len(held) == 7
    assert all(res.placement_waits[t].startswith("total:studio_cap (0/3 running)") for t in held), \
        res.placement_waits


def test_running_local_workers_count_against_the_cap(kanban_home):
    _write_fleet(kanban_home, studio_slots=3)
    with kb.connect_closing() as conn:
        _make(conn, 2, assignee="beta")
        _tick(conn, spillover=None)                       # 2 now running locally
        _make(conn, 5, assignee="beta")
        res, spawned = _tick(conn, spillover=None)
    assert len(spawned) == 1                              # 3 - 2 running
    assert any(r.startswith("total:studio_cap (2/3 running)") for r in res.placement_waits.values())


def test_one_unreadable_board_fails_the_cap_closed(kanban_home, monkeypatch):
    """Prism 03b2950a655c: a board that cannot be read hides its running
    workers; a partial count must not admit past the cap."""
    _write_fleet(kanban_home, studio_slots=6)
    real = kb.connect

    def connect(*a, board=None, **kw):
        if board == "other":
            raise PermissionError("board other unreadable")
        return real(*a, board=board, **kw)

    monkeypatch.setattr(kb, "list_boards",
                        lambda include_archived=False: [{"slug": "default"}, {"slug": "other"}])
    monkeypatch.setattr(kb, "connect", connect)
    assert kbd._local_running_now() is None

    def no_boards(include_archived=False):
        raise OSError("board registry unreadable")      # Prism 07a21b82824b

    with monkeypatch.context() as m:
        m.setattr(kb, "list_boards", no_boards)
        assert kbd._local_running_now() is None
    with kb.connect_closing() as conn:
        ids = _make(conn, 3, assignee="beta")
        res, spawned = _tick(conn, spillover=None)
    assert spawned == []
    assert all(res.placement_waits[t].startswith("total:studio_cap (running count unreadable)") for t in ids)


def test_no_studio_row_keeps_todays_unbounded_local_route(kanban_home):
    _write_fleet(kanban_home)                             # mutation guard: no row = no cap
    with kb.connect_closing() as conn:
        _make(conn, 10, assignee="beta")
        res, spawned = _tick(conn, spillover=None)
    assert len(spawned) == 10 and res.studio_cap_hold is None


def test_load_bar_refuses_local_placement_with_free_slots(kanban_home, monkeypatch):
    _write_fleet(kanban_home, studio_slots=6, studio_pct=0.8)
    monkeypatch.setattr(kbd.os, "getloadavg", lambda: (30.0, 30.0, 30.0))   # bar 25.6 on 32
    with kb.connect_closing() as conn:
        ids = _make(conn, 3, assignee="beta")
        res, spawned = _tick(conn, spillover=None)
    assert spawned == []
    assert all(res.placement_waits[t].startswith("total:studio_load (load1 30.0 >= 25.6") for t in ids)


def test_ci_box_paused_30_ready_cards_at_most_the_cap_land_on_the_studio(kanban_home):
    """Card AC 3: a paused (draining) ci-box's slots never become Studio slots.
    30 ready portable cards, ci-box draining, ace-ai + ace-media 4 each, Studio
    cap 6: <= 6 local, 8 placed on the pool, the rest queue (placement_wait)."""
    _write_fleet(kanban_home, studio_slots=6, ci_box_state="draining")
    hosts = [_host("ci-box", 24, 0, state="draining"), _host("ace-ai", 4, 1), _host("ace-media", 4, 2)]
    plan = kwp.plan(hosts, {}, probe=lambda h: (1.0, 32))
    plan.band, plan.spill_reason = "admitting", None
    with kb.connect_closing() as conn:
        ids = _make(conn, 30)
        res, spawned = _tick(conn, spillover=plan)
    on_studio = _local(spawned)
    assert len(on_studio) <= 6, f"{len(on_studio)} landed on the Studio"
    assert len(on_studio) == 6
    assert sorted(h for _, h in res.placed) == ["ace-ai"] * 4 + ["ace-media"] * 4
    assert "ci-box" not in {h for _, h in res.placed}
    waiting = [t for t in ids if t in res.placement_waits]
    assert len(waiting) == 30 - 6 - 8


def test_default_route_spills_to_the_pool_when_the_studio_is_at_cap(kanban_home, monkeypatch):
    """Apollo 10:2x: the cap binds the ``default`` (portable) route first; it
    spills to the pool instead of queueing behind the Studio."""
    _write_fleet(kanban_home, studio_slots=6, studio_pct=0.8)
    monkeypatch.setattr(kbd.os, "getloadavg", lambda: (30.0, 30.0, 30.0))
    plan = kwp.plan([_host("ace-ai", 4, 0)], {}, probe=lambda h: (1.0, 32))
    plan.band, plan.spill_reason = "admitting", None
    with kb.connect_closing() as conn:
        _make(conn, 3)
        res, spawned = _tick(conn, spillover=plan)
    assert _local(spawned) == [] and len(res.placed) == 3
