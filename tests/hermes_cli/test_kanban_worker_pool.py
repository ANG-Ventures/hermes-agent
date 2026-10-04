"""KWLB v0.1 pool reader, classifier, planner and the dispatcher's two budgets.

PRD ``plans/2026-10-03_kanban-worker-load-balancing-PRD.md`` §5, §11.
Every dispatcher test here runs under the AC-5 seam: ``_default_spawn`` is
replaced by a fake that raises, so no test can start a real worker on the
host it runs on. Tests pass their own ``spawn_fn``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp


# -- fixtures ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch: pytest.MonkeyPatch):
    """AC-5: the real spawn path raises inside this module."""
    def boom(*a, **kw):
        raise RuntimeError("real spawn in gate test")
    monkeypatch.setattr(kbd, "_default_spawn", boom)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    for p in ("alpha", "beta"):
        (home / "profiles" / p).mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return home


def _host(name="ace-ai", slots=2, priority=0, profiles=("alpha",), state="active"):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user=kwp.SSH_USER, slots=slots,
                        capacity_pct=0.8, absence="required", profiles=tuple(profiles),
                        state=state, enabled=True, priority=priority)


def _plan(free=2, band="paused", hosts=None, probe=lambda h: (1.0, 16)):
    hosts = hosts if hosts is not None else [_host(slots=free)]
    p = kwp.plan(hosts, {}, probe=probe)
    p.band, p.spill_reason = band, "load"
    return p


def _make(conn, n, *, assignee="alpha", body=None, priority=0, **kw):
    return [kb.create_task(conn, title=f"{assignee}-{priority}-{i}", assignee=assignee,
                           body=body, priority=priority, **kw) for i in range(n)]


def _tick(conn, *, spillover, spawn_limit, spawn_paused=None, spawned=None):
    spawned = [] if spawned is None else spawned

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused=spawn_paused,
                            spawn_limit=spawn_limit, spillover=spillover,
                            reconcile_orphans=False)
    return res, spawned


def test_seam_fixture_blocks_real_spawn():
    with pytest.raises(RuntimeError, match="real spawn in gate test"):
        kbd._default_spawn(None, "/nonexistent")


# -- reader (PRD §5.1) ------------------------------------------------------

def _write_pool(fleet: Path, *, roles=None, sidecar=None):
    fleet.mkdir(parents=True, exist_ok=True)
    roles = roles if roles is not None else {"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
        "ace-media": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
    }}
    sidecar = sidecar if sidecar is not None else {
        "schema": 1, "ssh_user": "kanbanw", "capacity_pct": 0.8,
        "priority": ["ci-box", "ace-ai", "ace-media"], "profiles": ["alpha"],
        "studio_bound_skills": ["mac-only"],
        "hosts": {"ci-box": {"enabled": False, "absence": "optional"},
                  "ace-ai": {"enabled": True, "absence": "required"},
                  "ace-media": {"enabled": True, "absence": "optional"}},
    }
    (fleet / kwp.ROLES_FILE).write_text(json.dumps(roles), encoding="utf-8")
    (fleet / kwp.SIDECAR_FILE).write_text(json.dumps(sidecar), encoding="utf-8")
    return sidecar


def test_reader_orders_by_priority_and_skips_disabled(tmp_path):
    _write_pool(tmp_path)
    hosts, warnings = kwp.load_pool(tmp_path, kanban_cfg={})
    assert [h.name for h in hosts] == ["ace-ai", "ace-media"] and warnings == []
    cfg = kwp.read_pool(tmp_path)
    assert cfg.disabled == ("ci-box",) and "ci-box" in cfg.hosts
    assert all(h.ssh_user == "kanbanw" and h.slots == 4 for h in hosts)


def test_reader_drops_one_host_on_disagreement_and_refuses_structure(tmp_path):
    side = _write_pool(tmp_path)
    side["hosts"]["ace-x"] = {"enabled": True, "absence": "optional"}
    side["priority"].append("ace-x")
    _write_pool(tmp_path, sidecar=side)
    hosts, warnings = kwp.load_pool(tmp_path, kanban_cfg={})
    assert [h.name for h in hosts] == ["ace-ai", "ace-media"]
    assert any("ace-x" in w for w in warnings)
    side["ssh_user"] = "ace"
    _write_pool(tmp_path, sidecar=side)
    assert kwp.load_pool(tmp_path, kanban_cfg={})[0] == []


def test_reader_refuses_coexistence_with_retired_worker_hosts(tmp_path):
    _write_pool(tmp_path)
    hosts, warnings = kwp.load_pool(tmp_path, kanban_cfg={"worker_hosts": [{"name": "x"}]})
    assert hosts == [] and "retired" in warnings[0]
    assert kwp.load_pool(tmp_path, kanban_cfg={"worker_hosts": []})[0]


# -- portable() (PRD §5.3, RC-1) --------------------------------------------

_POOL = kwp.PoolConfig(hosts=("ace-ai", "ace-media"), profiles=("alpha",),
                       studio_bound_skills=("mac-only",))


def _portable(**over):
    kw = dict(workspace_kind="scratch", has_links=False, workspace_has_content=False,
              assignee="alpha", body=None, skills=(), pool=_POOL)
    kw.update(over)
    return kwp.portable(**kw)


def test_portable_policy_default_and_routes():
    assert _portable() == (True, None, "policy")
    assert _portable(body="x\nhost:any\n") == (True, None, "any")
    assert _portable(body="host: ace-media") == (True, None, "pin")
    assert _portable(body="host:studio") == (False, "host_studio", None)
    assert _portable(skills=("mac-only",)) == (False, "studio_bound_skill", None)
    assert _portable(body="host:any", skills=("mac-only",))[0] is True
    assert _portable(body="see `host:any` below") == (True, None, "policy")


@pytest.mark.parametrize("over,rule", [
    (dict(workspace_kind="worktree"), "workspace_kind"),
    (dict(has_links=True), "linked"),
    (dict(workspace_has_content=True), "local_files"),
    (dict(assignee="beta"), "profile"),
])
@pytest.mark.parametrize("body", [None, "host:any", "host:ace-ai"])
def test_a_pin_never_overrides_hard_ok(over, rule, body):
    assert _portable(body=body, **over) == (False, rule, None)


# -- probe + plan -----------------------------------------------------------

class _Proc:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_probe_host_one_ssh_call_parses_or_fails_closed():
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw["timeout"]))
        return _Proc(0, "3.10 2.00 1.00 1/200 99\n16\n")

    assert kwp.probe_host(_host(), runner=runner) == (3.1, 16)
    argv, timeout = calls[0]
    assert len(calls) == 1 and timeout == 8
    assert "ConnectTimeout=4" in argv and argv[-1] == "cat /proc/loadavg; nproc"
    assert argv[-2] == "kanbanw@ace-ai"
    assert kwp.probe_host(_host(), runner=lambda a, **k: _Proc(255, "")) is None
    assert kwp.probe_host(_host(), runner=lambda a, **k: _Proc(0, "garbage")) is None


def test_plan_threshold_is_capacity_times_ncpu():
    assert kwp.host_threshold(16, 0.8) == pytest.approx(12.8)
    cold = kwp.plan([_host(slots=4)], {"ace-ai": 1}, probe=lambda h: (12.7, 16))
    hot = kwp.plan([_host(slots=4)], {"ace-ai": 1}, probe=lambda h: (12.8, 16))
    down = kwp.plan([_host(slots=4)], {}, probe=lambda h: None)
    assert (cold.budget, hot.budget, down.budget) == (3, 0, 0)
    assert down.detail["ace-ai"]["reachable"] is False


def test_plan_does_not_probe_a_full_or_draining_host():
    probed = []
    kwp.plan([_host("a", slots=2), _host("b", state="draining", priority=1)],
             {"a": 2}, probe=lambda h: probed.append(h.name) or (0.0, 8))
    assert probed == []


def test_take_walks_priority_and_pins_select_one_host():
    p = _plan(hosts=[_host("ace-ai", slots=1), _host("ace-media", slots=1, priority=1)])
    assert p.take("alpha").name == "ace-ai"
    assert p.take("alpha").name == "ace-media"
    assert p.take("alpha") is None and p.refusal == "pool_full"
    p = _plan(hosts=[_host("ace-ai", slots=1)])
    p.disabled = ("ci-box",)
    assert p.take("alpha", pin="ci-box") is None and p.refusal == "pin_host_disabled"
    assert p.take("alpha", pin="nope") is None and p.refusal == "pin_unknown_host"
    assert p.take("beta", pin="ace-ai") is None and p.refusal == "pin_host_full"
    host = p.take("alpha", pin="ace-ai")
    assert host.name == "ace-ai" and p.budget == 0
    p.release(host)
    assert p.budget == 1


# -- dispatcher: two budgets (PRD §5.2.6, RC-2) -----------------------------

def _claimed(conn, ids):
    marks = ",".join("?" * len(ids))
    return conn.execute(f"SELECT COUNT(*) FROM task_events WHERE kind='claimed' "
                        f"AND task_id IN ({marks})", ids).fetchone()[0]


def test_ac11_refused_take_never_falls_through_to_local(kanban_home):
    class Refusing(kwp.SpilloverPlan):
        def take(self, assignee, pin=None):
            self.refusal = "pool_full"
            return None

    base = _plan()
    plan = Refusing(hosts=base.hosts, slots=dict(base.slots), detail=base.detail,
                    config=base.config, band="paused")
    with kb.connect_closing() as conn:
        ids = _make(conn, 1, assignee="beta") + _make(conn, 2)
        res, spawned = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="test")
        assert spawned == [] and res.spawned == [] and res.placed == []
        assert _claimed(conn, ids) == 0
        assert res.placement_waits[ids[0]] == "local_full"


def test_ac12_spilling_non_portable_takes_the_local_slot(kanban_home):
    with kb.connect_closing() as conn:
        p1, p2 = _make(conn, 2, priority=5)            # portable, first by priority
        (np,) = _make(conn, 1, assignee="beta", priority=1)
        res, spawned = _tick(conn, spillover=_plan(free=1, band="spilling"), spawn_limit=1)
    assert sorted(spawned) == sorted([(np, None), (p1, "ace-ai")])
    assert res.placed == [(p1, "ace-ai")]
    assert res.placement_waits[p2] == "pool_full"


def test_ac17_paused_local_budget_zero_still_places(kanban_home):
    with kb.connect_closing() as conn:
        (np,) = _make(conn, 1, assignee="beta", priority=9)
        p = _make(conn, 3)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        assert spawned == [(p[0], "ace-ai"), (p[1], "ace-ai")]
        assert [h for _, h in res.placed] == ["ace-ai", "ace-ai"]
        assert res.placement_waits == {np: "local_full", p[2]: "pool_full"}
        rows = conn.execute(
            "SELECT e.payload FROM task_events e JOIN tasks t ON t.id=e.task_id "
            "WHERE e.kind=? AND e.run_id=t.current_run_id", (kwp.PLACED_EVENT,)).fetchall()
        payloads = [json.loads(r[0]) for r in rows]
        assert len(payloads) == 2
        assert all(x["band"] == "paused" and x["spill_reason"] == "load"
                   and x["reason"] == "band_paused" and x["probe"] == {"load1": 1.0, "ncpu": 16}
                   for x in payloads)


def test_ac17_local_one_plus_two_remote(kanban_home):
    with kb.connect_closing() as conn:
        (np,) = _make(conn, 1, assignee="beta")
        p = _make(conn, 3)
        res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=1)
    assert spawned[0] == (np, None)
    assert [h for _, h in spawned[1:]] == ["ace-ai", "ace-ai"]
    assert len(res.placed) == 2 and res.placement_waits == {p[2]: "pool_full"}


def test_ac17_memory_elevated_caps_the_total_at_one(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "elevated")
    with kb.connect_closing() as conn:
        _make(conn, 1, assignee="beta")
        _make(conn, 3)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert len(spawned) == 1 and len(res.placed) == 1


def test_rc1_pinned_worktree_waits_and_never_spawns_locally(kanban_home):
    with kb.connect_closing() as conn:
        (wt,) = _make(conn, 1, body="host:ace-ai", workspace_kind="worktree")
        res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=4)
    assert spawned == [] and res.placement_waits[wt] == "not_portable:workspace_kind"


def test_rc1_linked_card_with_host_any_is_not_placed(kanban_home):
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1, body="host:any")
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, child))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert res.placed == [] and all(tid != child for tid, _ in spawned)
    assert res.placement_waits[child] == "not_portable:linked"


def test_review_rows_spawn_locally_only(kanban_home, monkeypatch):
    """The spillover-is-None review guard is gone: review is offered, locally."""
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kb.connect_closing() as conn:
        (r,) = _make(conn, 1)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (r,))
        conn.commit()
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert all(h is None for _, h in spawned) and r not in [t for t, _ in spawned]


def test_count_running_by_placement_skips_unopenable_board(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    real = kb.connect

    def connect(board=None, **kw):
        if board == "broken":
            raise RuntimeError("cannot open")
        return real(board=board, **kw)

    monkeypatch.setattr(kb, "connect", connect)
    out = kb.count_running_by_placement([{"slug": "default"}, {"slug": "broken"}])
    assert out == {"default": (1, {"ace-ai": 1})}


def test_paused_tick_without_a_plan_spawns_nothing(kanban_home):
    with kb.connect_closing() as conn:
        _make(conn, 3)
        res, spawned = _tick(conn, spillover=None, spawn_limit=0, spawn_paused="test")
    assert spawned == [] and res.spawned == [] and res.spawn_paused == "test"


def test_running_placements_fill_the_host_for_the_next_plan(kanban_home):
    with kb.connect_closing() as conn:
        _make(conn, 3)
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        running = kwp.running_by_host(conn)
        assert running == {"ace-ai": 2}
        nxt = kwp.plan([_host(slots=2)], running, probe=lambda h: (0.0, 16))
        res, spawned = _tick(conn, spillover=nxt, spawn_limit=0, spawn_paused="test")
    assert spawned == [] and "running=2/2" in (res.spillover or "")


def test_cards_no_host_serves_are_never_claimed(kanban_home):
    """Budget is summed across hosts; per-assignee capacity is not."""
    hosts = [_host("ace-ai", slots=2), _host("host-b", slots=2, priority=1, profiles=("beta",))]
    with kb.connect_closing() as conn:
        ids = _make(conn, 4)
        res, spawned = _tick(conn, spillover=_plan(hosts=hosts), spawn_limit=0, spawn_paused="test")
        assert [h for _, h in spawned] == ["ace-ai", "ace-ai"]
        assert res.spawn_failed == [] and res.auto_blocked == []
        assert _claimed(conn, ids) == 2


def test_prefilled_local_workspace_stays_local(kanban_home, tmp_path):
    ws = tmp_path / "legacy-ws"
    ws.mkdir()
    (ws / "handoff.md").write_text("x", encoding="utf-8")
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host:any")
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(ws), tid))
        conn.commit()
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert spawned == [] and res.placement_waits[tid] == "not_portable:local_files"
