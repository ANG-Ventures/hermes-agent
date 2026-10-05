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


# KWLB v0.1 contract: the Phase 1b pressure reader off (its tests live in
# test_kanban_target_pressure.py).
V01 = {"placement": {"read_signal": False}}


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


def _tick(conn, *, spillover, spawn_limit, spawn_paused=None, spawned=None, **kw):
    spawned = [] if spawned is None else spawned

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else None))
        return 4242

    res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused=spawn_paused,
                            spawn_limit=spawn_limit, spillover=spillover,
                            reconcile_orphans=False, **kw)
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


def _studio_registered(fleet: Path, policy_hosts=None):
    """The QA t_1302fe6c repro: mac-studio carries the kanban-worker role and
    sits FIRST in the sidecar priority, while the placement policy says
    ``targets: false``."""
    roles = {"schema": 1, "hosts": {
        h: {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"}
        for h in ("mac-studio", "ace-ai", "ace-media")}}
    side = {"schema": 1, "ssh_user": "kanbanw", "capacity_pct": 0.8,
            "priority": ["mac-studio", "ace-ai", "ace-media"], "profiles": ["alpha"],
            "hosts": {h: {"enabled": True, "absence": "optional"}
                      for h in ("mac-studio", "ace-ai", "ace-media")}}
    _write_pool(fleet, roles=roles, sidecar=side)
    if policy_hosts is not None:
        (fleet / "placement-policy.json").write_text(
            json.dumps({"schema": 1, "hosts": policy_hosts}), encoding="utf-8")


@pytest.mark.parametrize("policy_hosts", [
    {"mac-studio": {"class": "studio-source", "targets": False},
     "ace-ai": {"class": "linux-shared"}, "ace-media": {"class": "linux-shared"}},
    None,  # no policy file: the PRD fallback still names the Studio targets:false
], ids=["policy-file", "fallback"])
def test_targets_false_host_is_dropped_and_never_gets_a_slot(tmp_path, policy_hosts):
    """Placement PRD I-1 / AC-5: a ``targets: false`` host is skipped at
    runtime even when the pool files register it first."""
    _studio_registered(tmp_path, policy_hosts)
    cfg = kwp.read_pool(tmp_path, kanban_cfg={})
    assert [h.name for h in cfg.pool_hosts] == ["ace-ai", "ace-media"]
    assert any("mac-studio" in w and "targets:false" in w for w in cfg.warnings)
    p = kwp.plan(list(cfg.pool_hosts), {}, probe=lambda h: (0.0, 16), config=cfg)
    taken = [p.take("alpha") for _ in range(8)]
    assert [h.name for h in taken] == ["ace-ai"] * 4 + ["ace-media"] * 4
    assert "mac-studio" not in p.slots
    assert p.take("alpha", pin="mac-studio") is None and p.refusal == "pin_host_dropped"


def test_targets_true_or_absent_keeps_the_host(tmp_path):
    _studio_registered(tmp_path, {"mac-studio": {"class": "linux-shared", "targets": True}})
    assert [h.name for h in kwp.read_pool(tmp_path).pool_hosts] == ["mac-studio", "ace-ai", "ace-media"]


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
    assert p.take("beta", pin="ace-ai") is None and p.refusal == "pin_profile"
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
    # Prism r9 capacity undercount: an unreadable board is reported as UNKNOWN
    # (None), never silently dropped, so the gate falls back to its floor.
    assert out == {"default": (1, {"ace-ai": 1}), "broken": (None, {})}
    from gateway.kanban_gate_tick import running_split
    assert running_split(out) == (None, {"ace-ai": 1})
    # Prism r10 :60 — remote counts SUM across boards on the unknown path too.
    assert running_split({"a": (None, {"ace-ai": 2}), "b": (3, {"ace-ai": 1})}) == (None, {"ace-ai": 3})
    assert running_split({"default": (1, {"ace-ai": 1})}) == (0, {"ace-ai": 1})


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


# -- Prism round 1 (PR #1730) -----------------------------------------------

def test_per_host_profile_overrides_reach_the_classifier(tmp_path):
    side = _write_pool(tmp_path)
    side.pop("profiles")
    side["hosts"]["ace-media"]["profiles"] = ["beta"]
    _write_pool(tmp_path, sidecar=side)
    cfg = kwp.read_pool(tmp_path)
    assert "beta" in cfg.profiles
    assert kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                        assignee="beta", body="host:ace-media", pool=cfg) == (True, None, "pin")


def test_remote_pin_without_a_plan_never_spawns_locally(kanban_home):
    _write_pool(kanban_home / "fleet")
    with kb.connect_closing() as conn:
        (pinned,) = _make(conn, 1, body="host:ace-ai")
        (plain,) = _make(conn, 1)
        res, spawned = _tick(conn, spillover=None, spawn_limit=4)
    assert spawned == [(plain, None)]
    assert res.placement_waits[pinned] == "pool_unavailable"


def test_dry_run_checks_the_pin_and_leaves_the_shared_plan_alone(kanban_home):
    plan = _plan(free=1, band="spilling")
    with kb.connect_closing() as conn:
        (bad,) = _make(conn, 1, body="host:nope")
        (good,) = _make(conn, 1, body="host:ace-ai")
        res, _ = _tick(conn, spillover=plan, spawn_limit=4, dry_run=True)
    # ``host:nope`` names no pool host: prose, not a pin (Apollo r4 A).
    assert res.placed == [(good, "ace-ai")]
    assert bad in [t for t, *_ in res.spawned] and bad not in res.placement_waits
    assert plan.budget == 1


def test_unassigned_pinned_card_routes_as_its_default_assignee(kanban_home):
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, assignee=None, body="host:ace-ai")
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0,
                             spawn_paused="test", default_assignee="alpha")
    assert spawned == [(tid, "ace-ai")] and res.placed == [(tid, "ace-ai")]


# -- Prism round 2 (PR #1730) -----------------------------------------------

@pytest.mark.parametrize("body,pin", [
    ("Fix it.\r\nhost:studio\r\nmore\r\n", "studio"),
    ("Fix it.\r\nhost: ace-ai \r\n", "ace-ai"),
    ("host:any\r\n", "any"),
])
def test_crlf_card_bodies_keep_their_pin(body, pin):
    assert kwp.card_pin(body) == pin


def test_crlf_studio_pin_stays_local():
    assert _portable(body="x\r\nhost:studio\r\n") == (False, "host_studio", None)


def test_retired_worker_hosts_alone_warns(tmp_path):
    hosts, warnings = kwp.load_pool(tmp_path, kanban_cfg={"worker_hosts": [{"name": "x"}]})
    assert hosts == [] and len(warnings) == 1 and "retired and ignored" in warnings[0]


def test_pin_to_a_host_that_does_not_serve_the_assignee_says_so():
    p = _plan(hosts=[_host("ace-ai", slots=2, profiles=("alpha",))])
    assert p.take("beta", pin="ace-ai") is None and p.refusal == "pin_profile"
    assert p.budget == 2


def test_pool_unavailable_pins_are_logged(kanban_home, caplog):
    import logging
    caplog.set_level(logging.INFO)
    _write_pool(kanban_home / "fleet")
    with kb.connect_closing() as conn:
        _make(conn, 1, body="host:ace-ai")
        _tick(conn, spillover=None, spawn_limit=4)
    assert any("wait pool_unavailable" in r.getMessage() for r in caplog.records)



# -- Apollo review r1 (PR #1730) -------------------------------------------

def test_reservation_is_refunded_when_launch_fails_before_spawn(kanban_home):
    """A reserved remote slot whose launch dies before the worker starts goes
    back to the shared plan, so a later card (any board) can still use it."""
    plan = _plan(free=1)
    calls = []

    def spawn(task, workspace, *, board=None, placement=None):
        calls.append(task.id)
        if len(calls) == 1:
            raise RuntimeError("worker host ace-ai: mkdir failed")
        return 4242

    with kb.connect_closing() as conn:
        first, second = _make(conn, 2)
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=64, spawn_paused="test",
                                spawn_limit=0, spillover=plan, reconcile_orphans=False)
    assert res.spawn_failed == [first]
    assert res.placed == [(second, "ace-ai")]


def test_native_command_profile_is_never_placed(kanban_home):
    (kanban_home / "profiles" / "alpha" / "config.yaml").write_text(
        "foreign_lane:\n  worker_command: [\"echo\", \"{task_id}\"]\n", encoding="utf-8")
    assert _portable(native_command=True) == (False, "native_command", None)
    with kb.connect_closing() as conn:
        (pinned,) = _make(conn, 1, body="host:ace-ai")
        (plain,) = _make(conn, 1)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert spawned == [] and res.placed == []
    assert res.placement_waits == {pinned: "not_portable:native_command", plain: "local_full"}


def test_placement_turns_ssh_file_sync_off(monkeypatch):
    """One kanbanw login serves every profile: a placed worker's ssh backend
    syncs nothing up (credentials) and nothing back down (other profiles)."""
    from hermes_cli import kanban_worker_hosts as kwh
    from tools import terminal_tool, terminal_tool_backends as tb

    env: dict = {}
    kwh.apply_placement(env, _host(), "/tmp/ws")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = terminal_tool._get_env_config()
    assert cfg["env_type"] == "ssh" and cfg["ssh_sync_files"] is False
    seen = {}
    monkeypatch.setattr(tb, "_SSHEnvironment", lambda **kw: seen.update(kw) or object())
    tb._build_ssh_env(cwd="/tmp/ws", timeout=5, ssh_config=tb._ssh_config_from_config(cfg))
    assert seen["sync_files"] is False and seen["user"] == "kanbanw"


def test_retired_worker_hosts_refusal_is_visible_in_load_gate(tmp_path):
    from gateway.kanban_gate_tick import GateTickBuilder
    from hermes_cli.kanban_load_gate import LoadGate

    _write_pool(tmp_path)
    gate = LoadGate({}, ncpu=32)
    gate.band = "paused"
    builder = GateTickBuilder(gate, fleet_dir=lambda: tmp_path,
                              kanban_cfg=lambda: {"worker_hosts": [{"name": "x"}]},
                              ledger=lambda b: {}, connect=None, probe=lambda h: (1.0, 16))
    assert builder.plan_pool([], {}) is None
    assert gate.pool["planned"] is False
    assert gate.pool["reason"] == "refused:legacy_worker_hosts"
    assert "retired" in gate.pool["warnings"][0]


def test_daemon_places_through_the_gateway_plan(kanban_home, monkeypatch):
    import threading

    from hermes_cli import config as _config
    from hermes_cli import kanban_load_gate as klg

    _write_pool(kanban_home / "fleet")
    monkeypatch.setattr(_config, "load_config", lambda: {"kanban": V01})
    monkeypatch.setattr(kwp, "probe_host", lambda h, runner=None: (1.0, 16))
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (146.0, 90.0))
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kw):
        captured.update(kw)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda _r: stop.set(),
                   load_gate=klg.LoadGate({}, ncpu=32))
    plan = captured["spillover"]
    assert captured["spawn_limit"] == 0 and isinstance(plan, kwp.SpilloverPlan)
    assert list(plan.hosts) == ["ace-ai", "ace-media"] and plan.budget == 8


def test_cli_dispatch_places_a_pin_through_the_gateway_plan(kanban_home, monkeypatch):
    import argparse

    from hermes_cli import config as _config
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_load_gate as klg

    _write_pool(kanban_home / "fleet")
    monkeypatch.setattr(_config, "load_config", lambda: {"kanban": V01})
    monkeypatch.setattr(kwp, "probe_host", lambda h, runner=None: (1.0, 16))
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (1.0, 1.0))
    monkeypatch.setattr(klg, "sample_cpu_busy", lambda prev=None, block=0.0: (0.1, None))
    with kb.connect_closing() as conn:
        _make(conn, 1, body="host:ace-media")
    captured: dict = {}

    def fake_dispatch_once(conn, **kw):
        captured.update(kw)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    kb_cli._cmd_dispatch(argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False))
    plan = captured["spillover"]
    assert isinstance(plan, kwp.SpilloverPlan) and plan.pins_only is True
    assert plan.take("alpha", pin="ace-media").name == "ace-media"


def test_count_running_by_placement_reads_one_snapshot(kanban_home, monkeypatch):
    """A placed run that ends between the two queries must not be read as a
    LOCAL worker (total counted it, the remote map did not)."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    real = kwp.running_by_host

    def finish_then_read(conn):
        with kb.connect_closing() as other:
            other.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            other.commit()
        return real(conn)

    monkeypatch.setattr(kwp, "running_by_host", finish_then_read)
    assert kb.count_running_by_placement([{"slug": "default"}]) == {"default": (1, {"ace-ai": 1})}


def test_aborted_placement_is_not_reported_placed(kanban_home, monkeypatch):
    """``placed`` is a subset of ``spawned``: the gateway books local spawns
    by id, so a placement the loop aborted must not appear in either."""
    monkeypatch.setattr(kbd, "_set_worker_pid", lambda *a, **k: False)
    monkeypatch.setattr(kb, "_abort_lost_claim_spawn", lambda *a, **k: None)
    with kb.connect_closing() as conn:
        _make(conn, 1)
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert len(spawned) == 1 and res.spawned == [] and res.placed == []
    res = kb.DispatchResult(spawned=[("a", "x", ""), ("b", "x", "")], placed=[("c", "ace-ai")])
    assert kbd._local_spawn_count(res) == 2


# -- Apollo review r3 (PR #1730) -------------------------------------------

def test_boot_placement_is_captured_before_the_env_loses_it(monkeypatch):
    """RC-9 late capture: ``main()`` records placed=<host> while the variable
    is still there; a later drop is then LOUD, not a silent local run."""
    from hermes_cli import kanban_worker_hosts as kwh

    recorded = []
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.setattr(kwh, "_reapply", {"failed": 0, "warned": False})
    monkeypatch.setattr(kwh, "_record_reapply_failure", recorded.append)
    env: dict = {}
    kwh.apply_placement(env, _host(), "/tmp/ws")
    monkeypatch.setenv(kwh.PLACEMENT_ENV, env[kwh.PLACEMENT_ENV])
    kwh.capture_boot_placement()                   # what main() runs first
    monkeypatch.delenv(kwh.PLACEMENT_ENV)          # an env file / bridge dropped it
    kwh.capture_boot_placement()                   # a later import must not erase it
    assert kwh.BOOT_PLACED_HOST == "ace-ai"
    assert kwh.reapply_or_record("terminal") is None and recorded == [1]


def test_paused_review_does_not_hold_a_pool_admission(kanban_home, monkeypatch):
    """Reviews only run locally: a paused tick must not reserve the pool's
    last admission for one and starve a portable card that could place."""
    from hermes_cli import kanban_provider_health as kph

    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(kph, "configured_pool_spawns_per_eligible", lambda: 1)
    monkeypatch.setattr(kph, "pool_key", lambda provider: "P")
    monkeypatch.setattr(kph, "pool_budget_eligible", lambda *a, **k: 1)
    monkeypatch.setattr(kb, "_pool_in_flight", lambda conn: {})
    with kb.connect_closing() as conn:
        (rv,) = _make(conn, 1, priority=9)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (rv,))
        conn.commit()
        (rd,) = _make(conn, 1)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert spawned == [(rd, "ace-ai")] and res.placed == [(rd, "ace-ai")]


@pytest.mark.parametrize("cap,placed", [(0, 0), (1, 1), (2, 2)])
def test_max_new_caps_local_and_remote_spawns(kanban_home, cap, placed):
    """CLI ``--max N`` is a TOTAL cap: remote placements count against it."""
    with kb.connect_closing() as conn:
        _make(conn, 3, body="host:ace-ai")
        res, spawned = _tick(conn, spillover=_plan(free=3), spawn_limit=4, max_new=cap)
    assert len(spawned) == placed and len(res.placed) == placed


def test_cli_passes_max_as_the_total_cap(kanban_home, monkeypatch):
    import argparse

    from hermes_cli import config as _config
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_load_gate as klg

    monkeypatch.setattr(_config, "load_config", lambda: {"kanban": V01})
    monkeypatch.setattr(klg, "sample_loadavg", lambda: (1.0, 1.0))
    monkeypatch.setattr(klg, "sample_cpu_busy", lambda prev=None, block=0.0: (0.1, None))
    captured: dict = {}
    monkeypatch.setattr(kbd, "dispatch_once",
                        lambda conn, **kw: captured.update(kw) or kb.DispatchResult())
    kb_cli._cmd_dispatch(argparse.Namespace(dry_run=True, max=1, failure_limit=2, json=False))
    assert captured["max_new"] == 1


def _link_queries(conn, extra_beta):
    from hermes_cli import kanban_worker_hosts as kwh  # noqa: F401
    with kb.connect_closing() as c:
        c.execute("DELETE FROM tasks")
        c.commit()
        _make(c, extra_beta, assignee="beta")
        _make(c, 2)
        stmts = []
        c.set_trace_callback(stmts.append)
        _tick(c, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        c.set_trace_callback(None)
    return sum(1 for q in stmts if "task_links" in q)


def test_route_scan_is_bounded_per_tick(kanban_home, monkeypatch):
    """Link queries do not grow with the ready backlog, and a row a cheaper
    rule already refuses (non-allowlisted profile) costs no workspace scan."""
    from hermes_cli import kanban_worker_hosts as kwh

    scans = []
    monkeypatch.setattr(kwh, "local_workspace_has_content", lambda p: scans.append(p) or False)
    small = _link_queries(None, 0)
    scans.clear()
    big = _link_queries(None, 30)
    assert big == small
    assert len(scans) == 2


# -- Apollo review r4 (PR #1730) -------------------------------------------

def test_prose_host_line_with_no_pool_spawns_locally(kanban_home):
    """(A) ``Host: example.com`` is an HTTP header, not a pin: with no pool
    configured the card runs locally instead of waiting forever."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="Repro:\nHost: example.com\n")
        res, spawned = _tick(conn, spillover=None, spawn_limit=4)
    assert spawned == [(tid, None)] and tid not in res.placement_waits


def test_prose_host_line_with_a_pool_routes_by_policy(kanban_home):
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="Host: example.com")
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert spawned == [(tid, "ace-ai")] and res.placed == [(tid, "ace-ai")]


def test_known_pool_host_still_pins(kanban_home):
    _write_pool(kanban_home / "fleet")
    assert kwp.resolve_pin("host:ace-ai", kwp.known_host_ids(kanban_home / "fleet")) == ("ace-ai", None)
    assert kwp.resolve_pin("host:ci-box", kwp.known_host_ids(kanban_home / "fleet")) == ("ci-box", None)
    assert kwp.resolve_pin("  host: localhost", ("ace-ai",)) == (None, "localhost")
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host:ace-ai")
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=4)
    assert spawned == [(tid, "ace-ai")]


def test_ignored_pin_warns_once_per_card_per_5_minutes(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger=kwp.__name__)
    monkeypatch.setattr(kwp, "_ignored_pin_warned", {})
    clock = [1000.0]
    monkeypatch.setattr(kwp.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        kwp.note_ignored_pin("t_1", "example.com")
    kwp.note_ignored_pin("t_2", "localhost")
    clock[0] += kwp.IGNORED_PIN_WARN_SECONDS
    kwp.note_ignored_pin("t_1", "example.com")
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 3 and all("ignored unknown host pin" in m for m in msgs)


def test_link_scan_binds_under_the_sqlite_variable_limit(kanban_home):
    """(B) 1200 ids on a real sqlite capped at the historical 999 variables."""
    import sqlite3
    with kb.connect_closing() as conn:
        a, b = _make(conn, 2)
        kb.link_tasks(conn, a, b)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        ids = [f"t_fake{i:05d}" for i in range(1198)] + [a, b]
        assert kbd._linked_ids(conn, ids) == {a, b}


# -- Apollo review r5 (PR #1730) -------------------------------------------

def test_pin_on_a_known_but_dropped_host_waits_and_never_spawns(kanban_home):
    """Prism 4271004b: ace-media has the kanban-worker role but no sidecar
    row, so the pool drops it. A card pinned to it still IS pinned: with a
    healthy ace-ai plan and free local slots it waits ``pin_host_dropped``."""
    fleet = kanban_home / "fleet"
    side = _write_pool(fleet)
    del side["hosts"]["ace-media"]
    side["priority"].remove("ace-media")
    _write_pool(fleet, sidecar=side)
    cfg = kwp.read_pool(fleet)
    spill = kwp.plan(list(cfg.pool_hosts), {}, probe=lambda h: (1.0, 16),
                     disabled=cfg.disabled, config=cfg)
    spill.band, spill.spill_reason = "spilling", "load"
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host:ace-media")
        res, spawned = _tick(conn, spillover=spill, spawn_limit=4)
    assert spawned == []
    assert res.placement_waits.get(tid) == "pin_host_dropped"
    assert [h.name for h in cfg.pool_hosts] == ["ace-ai"]


def test_r8_prose_host_line_above_a_real_pin_does_not_hide_the_pin():
    """Prism r8: the first host: line is prose (an HTTP header), the second is
    the real pin. The pin wins; the prose word is reported as ignored."""
    body = "Repro:\nhost: example.com\n\nhost:ace-ai\n"
    assert kwp.resolve_pin(body, ("ace-ai",)) == ("ace-ai", "example.com")
    assert kwp.resolve_pin("host: example.com\nhost:any\n", ()) == ("any", "example.com")
    assert kwp.resolve_pin("host: example.com\n", ("ace-ai",)) == (None, "example.com")
