"""KWLB v0.1 pool reader, classifier, planner and the dispatcher's two budgets.

PRD ``plans/2026-10-03_kanban-worker-load-balancing-PRD.md`` §5, §11.
Every dispatcher test here runs under the AC-5 seam: ``_default_spawn`` is
replaced by a fake that raises, so no test can start a real worker on the
host it runs on. Tests pass their own ``spawn_fn``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
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


def test_reader_accepts_the_last_change_audit_field_and_still_refuses_unknown_row_keys(tmp_path):
    """fleet-host stamps `last_change` {by, card, reason, at} on a row (who/why/when); it never drives placement."""
    lc = {"by": "sess-A", "card": "t_1234abcd", "reason": "drain: x", "at": "2026-10-12T09:00:00Z"}
    roles = {"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}}, "state": "draining", "last_change": lc},
        "ace-media": {"roles": {"kanban-worker": {"slots": 4}}, "state": "active"},
    }}
    _write_pool(tmp_path, roles=roles)
    assert kwp.parse_roles(roles) == {"ace-ai": ({"kanban-worker": 4}, "draining"),
                                      "ace-media": ({"kanban-worker": 4}, "active")}
    hosts, _ = kwp.load_pool(tmp_path, kanban_cfg={})
    assert {h.name: h.state for h in hosts}.get("ace-media") == "active"
    roles["hosts"]["ace-media"]["note"] = "x"
    with pytest.raises(kwp.PoolError, match="fields must be roles, state"):
        kwp.parse_roles(roles)


def test_ci_runner_role_is_count_free_and_other_roles_keep_their_count(tmp_path):
    """t_1acfaa85: hermes-home#6459 writes ``"ci-runner": {}``; the pool must still load.
    kanban-worker is count-free too since t_c834e0d8 (test_kanban_count_free_role.py)."""
    def doc(roles):
        return {"schema": 1, "hosts": {"a": {"roles": roles, "state": "active"}}}

    assert kwp.parse_roles(doc({"ci-runner": {}})) == {"a": ({"ci-runner": None}, "active")}
    assert kwp.parse_roles(doc({"ci-runner": {"slots": 3}})) == {"a": ({"ci-runner": 3}, "active")}
    assert kwp.parse_roles(doc({"kanban-worker": {}})) == {"a": ({"kanban-worker": None}, "active")}
    with pytest.raises(kwp.PoolError, match=r'must be \{"slots": N\}'):
        kwp.parse_roles(doc({"review-worker": {}}))
    with pytest.raises(kwp.PoolError, match="ci-runner must be"):
        kwp.parse_roles(doc({"ci-runner": {"slots": 3, "x": 1}}))
    with pytest.raises(kwp.PoolError, match="slots must be 1..64"):
        kwp.parse_roles(doc({"ci-runner": {"slots": 0}}))
    roles = {"schema": 1, "hosts": {
        "ace-ai": {"roles": {"kanban-worker": {"slots": 4}, "ci-runner": {}}, "state": "active"},
        "ace-media": {"roles": {"kanban-worker": {"slots": 4}, "ci-runner": {}}, "state": "active"},
    }}
    _write_pool(tmp_path, roles=roles)
    cfg = kwp.read_pool(tmp_path, kanban_cfg={})
    assert cfg.refused is None
    assert sorted(h.name for h in cfg.pool_hosts) == ["ace-ai", "ace-media"]
    assert {h.slots for h in cfg.pool_hosts} == {4}


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
    assert "ConnectTimeout=4" in argv and argv[-1] == "cat /proc/loadavg; getconf _NPROCESSORS_ONLN"
    assert argv[-2] == "kanbanw@ace-ai"
    assert kwp.probe_host(_host(), runner=lambda a, **k: _Proc(255, "")) is None
    assert kwp.probe_host(_host(), runner=lambda a, **k: _Proc(0, "garbage")) is None


def _cgroup_ssh(argv, **kw):
    """A cgroup-quota'd Linux host (t_2dc3be98): ``nproc`` honours the probing
    user's CPUQuota (8) while the host has 24 online cores. The fake answers
    each core-count command the way the real host did as kanbanw."""
    cmd = argv[-1]
    load = "12.00 9.00 7.00 1/900 99"  # ordinary ace-ai load1, under 0.8 x 24
    if "; getconf _NPROCESSORS_ONLN" in cmd:
        ncpu = "24"
    elif "; nproc" in cmd:
        ncpu = "8"
    else:
        raise AssertionError(f"unexpected probe command: {cmd!r}")
    tail = "\n1791200100\n{}" if "date +%s" in cmd else ""
    return _Proc(0, f"{load}\n{ncpu}{tail}\n")


def test_probe_reports_online_cores_not_cgroup_quota():
    """KWLB D-3: ncpu = cores. With the quota'd count the v0.1 refuse line was
    0.8 x 8 = 6.4 load1, so an ordinary load1 of 7-16 refused ace-ai."""
    assert kwp.probe_host(_host(), runner=_cgroup_ssh) == (12.0, 24)
    s = kwp.probe_host(_host(), runner=_cgroup_ssh, pressure_path="/var/lib/placement/host-pressure.json")
    assert s.ncpu == 24
    p = kwp.plan([_host(slots=4)], {"ace-ai": 1}, probe=lambda h: kwp.probe_host(h, runner=_cgroup_ssh))
    assert p.budget == 3  # the quota'd ncpu=8 put the line at 6.4 and refused this host


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
        def take(self, assignee, pin=None, board=None):
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
    assert res.placement_waits[child] == f"not_portable:linked(blocks->{parent})"


# -- t_ae87edff: only workspace-sharing link kinds hold a card local ---------

def _derived(conn, *, child_body=None):
    (parent,) = _make(conn, 1)  # alpha: the pool allowlist, so only links can hold it
    (child,) = _make(conn, 1, body=child_body, parents=[parent], parents_kind="derived-from")
    return parent, child


def test_derived_from_child_is_portable_on_policy(kanban_home):
    """(a) A provenance-only child (scratch, otherwise unlinked) spills."""
    with kb.connect_closing() as conn:
        _parent, child = _derived(conn)
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0] == "ready"
        assert kbd._route_row(conn, conn.execute("SELECT * FROM tasks WHERE id=?", (child,)).fetchone(),
                              _plan(free=2)) == ("portable", None)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert (child, "ace-ai") in spawned and child not in res.placement_waits


def test_derived_from_child_with_host_any_places_on_pool(kanban_home):
    with kb.connect_closing() as conn:
        _parent, child = _derived(conn, child_body="host: any")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert (child, "ace-ai") in res.placed


def test_blocks_child_is_not_portable_and_names_the_link(kanban_home):
    """(b) A gating child may read its parent's retained local workspace."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1, body="host:any")
        kb.link_tasks(conn, parent, child, kind="blocks")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (child,)).fetchone()
        assert kbd._route_row(conn, row, _plan(free=2)) == (
            "local", f"not_portable:linked(blocks->{parent})")


@pytest.mark.parametrize("terminal", ["done", "archived"])
def test_blocks_link_to_a_terminal_parent_pins_nothing(kanban_home, terminal):
    """t_6d9e7d90 (Ace/Apollo 14:3x): a link whose far end is terminal holds
    nothing. 12 browser-cron cards stayed Studio-local behind
    blocks->t_18554f6e, which closed 10-15."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1)
        kb.link_tasks(conn, parent, child, kind="blocks")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (terminal, parent))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
        assert child not in kbd._linked_ids(conn, [child])
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (child,)).fetchone()
        assert kbd._route_row(conn, row, _plan(free=2)) == ("portable", None)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert (child, "ace-ai") in spawned and child not in res.placement_waits


def test_terminal_child_does_not_pin_its_live_parent(kanban_home):
    """The other direction: a done CHILD no longer holds its ready parent."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1)
        (child,) = _make(conn, 1, assignee="beta")
        kb.link_tasks(conn, parent, child, kind="blocks")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()
        assert parent not in kbd._linked_ids(conn, [parent])


def test_live_far_end_still_pins_beside_a_terminal_one(kanban_home):
    """A terminal edge is skipped, never the card: a second, live blocks edge
    still holds it and is the one named."""
    with kb.connect_closing() as conn:
        done_p, live_p = _make(conn, 2, assignee="beta")
        (child,) = _make(conn, 1)
        kb.link_tasks(conn, done_p, child, kind="blocks")
        kb.link_tasks(conn, live_p, child, kind="blocks")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (done_p,))
        conn.commit()
        assert kbd._linked_ids(conn, [child])[child] == ("blocks", live_p)


def test_parent_with_outgoing_after_merge_edge_is_placeable(kanban_home):
    """t_250a7fc5: t_041ad167 (host: ci-box) sat 20 h on
    ``not_portable:linked(after-merge->t_84e763a3)``, its OWN outgoing edge.
    The edge gates the child on the parent's PR; it never holds the parent."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, body="host: ace-ai")
        (child,) = _make(conn, 1, assignee="beta")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        assert parent not in kbd._linked_ids(conn, [parent, child])
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (parent,)).fetchone()
        assert kbd._route_row(conn, row, _plan(free=2)) == ("pin", "ace-ai")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert (parent, "ace-ai") in spawned and parent not in res.placement_waits


def test_child_of_unmerged_after_merge_parent_is_not_placeable(kanban_home):
    """The other direction still gates: the child is demoted to todo by the
    link (never spawned) and its incoming edge still names it linked."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1, body="host: ace-ai")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0] == "todo"
        assert kbd._linked_ids(conn, [child])[child] == ("after-merge", parent)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert all(s[0] != child for s in spawned)


def test_legacy_lineage_mode_still_pins_the_after_merge_parent(kanban_home):
    """Kill switch: ``lineage_pins: legacy`` keeps every link pinning."""
    from hermes_cli import kanban_placement_reasons as _pr
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1)
        (child,) = _make(conn, 1, assignee="beta")
        kb.link_tasks(conn, parent, child, kind="after-merge")
        assert kbd._linked_ids(conn, [parent], _pr.LINEAGE_PINS_LEGACY)[parent] == (
            "after-merge", child)


def test_legacy_lineage_mode_still_pins_a_terminal_parent(kanban_home):
    """Kill switch: ``lineage_pins: legacy`` keeps every link pinning."""
    from hermes_cli import kanban_placement_reasons as _pr
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1)
        kb.link_tasks(conn, parent, child, kind="blocks")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        conn.commit()
        assert kbd._linked_ids(conn, [child], _pr.LINEAGE_PINS_LEGACY)[child] == ("blocks", parent)


def test_human_review_lane_rows_are_dropped_before_routing(kanban_home, monkeypatch):
    """t_6d9e7d90: a ``human:apollo`` handback sitting in ready is skipped
    nonspawnable BEFORE the classifier, so it never shows as a placement
    hold; a worker card beside it still places."""
    monkeypatch.setattr("hermes_cli.profiles.profile_exists",
                        lambda name: not str(name).startswith("human"), raising=False)
    with kb.connect_closing() as conn:
        (review,) = _make(conn, 1, assignee="human:apollo")
        (work,) = _make(conn, 1)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (review,))]
    assert review in res.skipped_nonspawnable
    assert review not in res.placement_waits
    assert all(e["task_id"] != review for e in res.routes)
    assert kinds.count("skipped_nonspawnable") == 1
    assert (work, "ace-ai") in spawned


def test_legacy_null_kind_link_still_holds_local(kanban_home):
    """A NULL kind reads as ``blocks`` (normalize_link_kind), never un-holds."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1)
        conn.execute("PRAGMA ignore_check_constraints=1")
        conn.execute("DROP TABLE task_links")
        conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT, kind TEXT)")
        conn.execute("INSERT INTO task_links VALUES (?, ?, NULL)", (parent, child))
        conn.commit()
        assert kbd._linked_ids(conn, [child])[child] == ("blocks", parent)


def test_parent_of_derived_from_child_is_portable(kanban_home):
    """(c) The provenance edge does not hold the PARENT local either."""
    with kb.connect_closing() as conn:
        parent, _child = _derived(conn)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (parent,)).fetchone()
        assert kbd._route_row(conn, row, _plan(free=2)) == ("portable", None)
        assert kbd._linked_ids(conn, [parent, _child]) == {}


def test_dry_run_prints_the_placement_wait_reason(kanban_home, capsys, monkeypatch):
    """(d) `kanban dispatch --dry-run` says WHY a ready card was held."""
    from hermes_cli import kanban_ops as kops
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1, body="host:ace-ai", parents=[parent])
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
    monkeypatch.setattr(kops, "_one_shot_pool_plan", lambda *a, **k: _plan(free=2))
    import argparse
    rc = kops._cmd_dispatch(argparse.Namespace(dry_run=True, json=False, max=None,
                                               max_running=None, failure_limit=2))
    out = capsys.readouterr().out
    assert rc == 0
    # route_first (t_7133e03d): one line per ready row replaces ``Held (...)``.
    assert (f"  {child}  route=hold host=- rule=linked(blocks->{parent})  "
            f"-> holds not_portable:linked(blocks->{parent})") in out


def _lineage(monkeypatch, mode):
    from hermes_cli import config as _config
    monkeypatch.setattr(_config, "load_config_readonly",
                        lambda: {"kanban": {"placement": {"lineage_pins": mode}}})


def _placement_waits(conn, tid):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='placement_wait' ORDER BY id",
        (tid,))]


def test_placement_wait_event_written_once_then_deduplicated(kanban_home, monkeypatch):
    """Spec t_1cca39e1 (b): {reason, route, host, since}; a repeat within 900 s
    writes nothing, a dry run writes nothing, an aged repeat keeps ``since``."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1, body="host:ace-ai", parents=[parent])
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
        kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1, max_spawn=64,
                          spawn_limit=0, spawn_paused="t", spillover=_plan(free=2),
                          reconcile_orphans=False)
        assert _placement_waits(conn, child) == []
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        (ev,) = _placement_waits(conn, child)
        assert ev["reason"] == f"not_portable:linked(blocks->{parent})"
        assert ev["route"] == "wait" and ev["host"] is None and isinstance(ev["since"], int)
        conn.execute("UPDATE task_events SET created_at = created_at - 901 "
                     "WHERE task_id=? AND kind='placement_wait'", (child,))
        conn.commit()
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        evs = _placement_waits(conn, child)
    assert len(evs) == 2 and evs[1]["since"] == ev["since"]


def test_placement_wait_new_episode_after_run_is_not_deduplicated(kanban_home, monkeypatch):
    """Prism 331b10c66fe9: hold -> card runs -> back to ready with the same
    reason is a NEW wait episode: a fresh event (even inside 900 s) whose
    ``since`` is the new hold's start, never the first episode's."""
    with kb.connect_closing() as conn:
        (parent,) = _make(conn, 1, assignee="beta")
        (child,) = _make(conn, 1, body="host:ace-ai", parents=[parent])
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        conn.commit()
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        (first,) = _placement_waits(conn, child)
        # Backdate the first episode by 300 s (still inside the 900 s window).
        conn.execute("UPDATE task_events SET created_at = created_at - 300, payload = "
                     "json_set(payload, '$.since', json_extract(payload, '$.since') - 300) "
                     "WHERE task_id=? AND kind='placement_wait'", (child,))
        with kb.write_txn(conn):
            kb._append_event(conn, child, "claimed", {"run_id": 1})
            kb._append_event(conn, child, "requeued", {"reason": "test"})
        conn.commit()
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        evs = _placement_waits(conn, child)
    assert len(evs) == 2
    assert evs[1]["reason"] == first["reason"]
    assert evs[1]["since"] >= first["since"]  # first["since"] read before the backdate
    assert evs[1]["since"] - evs[0]["since"] >= 300


def test_lineage_pins_legacy_is_todays_routing(kanban_home, monkeypatch):
    """Spec t_1cca39e1 (c): ``legacy`` routes exactly as before t_ae87edff on
    real card shapes (this card's own pinned body, an umbrella's derived-from
    children, a blocks chain). Passes on the pre-fix base unchanged."""
    _lineage(monkeypatch, "legacy")
    umbrella_body = "origin: discord #ci-runner-box\nFix (hermes-agent, worktree off gh/main, PR)"
    with kb.connect_closing() as conn:
        (umb,) = _make(conn, 1, body=umbrella_body)
        (der_any,) = _make(conn, 1, body="host: any\nproof card", parents=[umb],
                           parents_kind="derived-from")
        (der_pin,) = _make(conn, 1, body="host: ace-ai\nbuild", parents=[umb],
                           parents_kind="derived-from")
        (der_plain,) = _make(conn, 1, body="no host line", parents=[umb],
                             parents_kind="derived-from")
        (blk,) = _make(conn, 1, body="host:ace-ai", parents=[umb])
        (lone,) = _make(conn, 1, body="host: ace-ai\norigin: no links")
        conn.execute("UPDATE tasks SET status='ready'")
        conn.commit()
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM tasks")}
        plan = _plan(free=2)
        got = {t: kbd._route_row(conn, rows[t], plan) for t in rows}
    assert got == {
        umb: ("local", None),
        der_any: ("local", "not_portable:linked"),
        der_pin: ("wait", "not_portable:linked"),
        der_plain: ("local", None),
        blk: ("wait", "not_portable:linked"),
        lone: ("pin", "ace-ai"),
    }


def test_lineage_pins_default_is_kinds(kanban_home, monkeypatch):
    # Unset -> the default. (An unrecognised value now reads as ``legacy``,
    # spec v1.2 §6 parse correction, t_6d5bc652: AC-21 owns that case.)
    from hermes_cli import config as _config
    monkeypatch.setattr(_config, "load_config_readonly", lambda: {"kanban": {}})
    with kb.connect_closing() as conn:
        _parent, child = _derived(conn, child_body="host: any")
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (child,)).fetchone()
        assert kbd._route_row(conn, row, _plan(free=2)) == ("portable", None)


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
    # ``host:nope`` names no pool host: refused, never run anywhere (t_229d6bca);
    # a dry run only reports it.
    assert res.placed == [(good, "ace-ai")]
    assert bad not in [t for t, *_ in res.spawned]
    assert res.placement_waits[bad].startswith("pin_unknown_host:nope")
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



def test_remote_pin_below_a_prose_host_header_still_waits_without_a_plan(kanban_home):
    """Prism 56085b64 (Pin Bypass): a ``Host:`` header above a real pin made
    ``resolve_pin`` return both a pin and an ignored word, and the no-plan
    branch only recorded ``pool_unavailable`` when nothing was ignored, so
    the remote pin spawned LOCALLY (breaks I-11)."""
    _write_pool(kanban_home / "fleet")
    with kb.connect_closing() as conn:
        (pinned,) = _make(conn, 1, body="Host: example.com\nhost:ace-ai")
        (plain,) = _make(conn, 1)
        res, spawned = _tick(conn, spillover=None, spawn_limit=4)
    assert spawned == [(plain, None)]
    assert res.placement_waits[pinned] == "pool_unavailable"


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
    # worker_pool.overflow off: the KWLB v0.1 admitting contract (pins only).
    monkeypatch.setattr(_config, "load_config",
                        lambda: {"kanban": {**V01, "worker_pool": {"overflow": False}}})
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
    # Remote-first (#2148, t_82b4db80): overflow off no longer narrows the plan to pins.
    assert isinstance(plan, kwp.SpilloverPlan) and plan.pins_only is False and plan.default_remote is True
    assert plan.take("alpha", pin="ace-media").name == "ace-media"


def test_count_running_by_placement_reads_one_snapshot(kanban_home, monkeypatch):
    """A placed run that ends between the two queries must not be read as a
    LOCAL worker (total counted it, the remote map did not).

    The interleaved write needs WAL: under a rollback journal the reader's
    SHARED lock blocks it for the whole busy_timeout (120 s), and the swallowed
    'database is locked' reads as an UNKNOWN board. Connect leaves new boards in
    DELETE on WAL-reset-vulnerable SQLite (3.46.1 on the Linux venues), so put
    this tmp board in WAL here; connect keeps an on-disk WAL file as WAL.
    """
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    raw = sqlite3.connect(str(kb.kanban_db_path("default")))
    try:
        assert raw.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    finally:
        raw.close()
    real = kwp.running_by_host
    wrote = []

    def finish_then_read(conn):
        # WAL: the write lands but the open read snapshot does not see it.
        with kb.connect_closing() as other:
            other.execute("PRAGMA busy_timeout=2000")  # a lock wait fails fast, not at 120 s
            other.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            other.commit()
        wrote.append(tid)
        return real(conn)

    monkeypatch.setattr(kwp, "running_by_host", finish_then_read)
    assert kb.count_running_by_placement([{"slug": "default"}]) == {"default": (1, {"ace-ai": 1})}
    assert wrote == [tid]  # the write really landed between the two reads
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"


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


def test_prose_host_line_with_a_pool_is_refused(kanban_home):
    """t_cb2b888d reverses the old 'prose routes by policy': a host: line
    that names no host may be a typo, so it never widens to the pool;
    t_229d6bca makes it a refusal (a pin is hard): not even run locally."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="Host: example.com")
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=4)
        assert kb.get_task(conn, tid).status == "blocked"
    assert spawned == [] and res.placed == []
    assert res.placement_waits[tid].startswith("pin_unknown_host:example.com")


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
        assert kbd._linked_ids(conn, ids) == {a: ("blocks", b), b: ("blocks", a)}


def test_link_reason_is_stable_across_ready_order_and_chunks(kanban_home):
    """Prism 9457394f1922: a card with two pinning links names the SAME edge
    whatever the ready-row order, even when the other endpoint lands in an
    earlier 250-id chunk than the card itself."""
    import itertools
    with kb.connect_closing() as conn:
        p_lo, p_hi = sorted(_make(conn, 2, assignee="beta"))
        (card,) = _make(conn, 1)
        kb.link_tasks(conn, p_lo, card)
        kb.link_tasks(conn, p_hi, card)
        want = ("blocks", p_lo)  # the single-chunk answer (ORDER BY parent_id, child_id)
        assert kbd._linked_ids(conn, [card])[card] == want
        pad = [f"t_fake{i:05d}" for i in range(kbd._LINK_CHUNK - 1)]
        # p_hi alone fills chunk 1 with the padding; card arrives in chunk 2.
        assert kbd._linked_ids(conn, [p_hi, *pad, card, p_lo])[card] == want
        for order in itertools.permutations([card, p_lo, p_hi]):
            got = kbd._linked_ids(conn, [order[0], *pad, *order[1:]])
            assert got[card] == want, order


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


# -- Studio host affinity (card t_3e6c9b2b, incident t_79436ebf) -----------

def test_studio_affinity_sources_and_pin_override():
    assert _portable(body="Use CDP on the signed-in browser :9224 on the Studio") == (
        False, "studio_body", None)
    assert _portable(body="drive it with peekaboo") == (False, "studio_body", None)
    assert _portable(skills=("browser-access",),
                     manifest={"browser-access": "manifest:host_affinity"}) == (
        False, "studio_manifest", None)
    # Prose that only mentions a port family, not a loopback CDP port, stays portable.
    assert _portable(body="port 9200 is elasticsearch") == (True, None, "policy")
    # The NARROW body list (spec s2.3): a REMOTE browser endpoint and the bare
    # word CDP are not Studio state (Prism f66cf7e1 / 115dad86).
    # A URL naming a pool host is not an ssh-shaped mention (t_696a9a06), so
    # it binds neither host_reach nor studio affinity.
    assert _portable(body="Connect via CDP to http://ace-ai:9222") == (True, None, "policy")
    assert _portable(body="connect to http://browser.example:9222") == (True, None, "policy")
    assert _portable(body="use CDP generally") == (True, None, "policy")
    assert _portable(body="add items to Ace's Amazon cart on localhost:9222") == (
        False, "studio_body", None)
    assert _portable(body="Add to cart the printer filament") == (False, "studio_body", None)
    # An explicit pin is the operator's call and wins over every affinity signal.
    assert _portable(body="host:ace-ai\nCDP :9222") == (True, None, "pin")
    assert _portable(body="host:any\nCDP :9222")[0] is True
    hit = kwp.studio_affinity("plain", ("mac-only", "x"), _POOL)
    assert hit == ("studio_bound_skill", "skill:mac-only")


def _skill(root, name, frontmatter, *, category="general", dir_name=None):
    d = root / category / (dir_name or name)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n{frontmatter}---\n\nbody\n", encoding="utf-8")


def test_manifest_affinity_reads_host_affinity_and_linux_less_platforms(tmp_path, monkeypatch):
    from hermes_cli import kanban_host_affinity as kha
    root = tmp_path / "skills"
    _skill(root, "cdp-login", "platforms: [macos, linux]\nmetadata:\n  hermes:\n"
                              "    host_affinity: studio\n")
    _skill(root, "mac-app", "platforms: [macos]\n")
    _skill(root, "anywhere", "platforms: [macos, linux]\n")
    _skill(root, "no-platforms", "description: x\n")
    # Nested two levels down (category/subcategory) and found by frontmatter
    # name, the way the worker resolves (Prism e16f8f5c: recursive lookup).
    _skill(root, "deep-login", "platforms: [macos]\n", category="general/logins")
    _skill(root, "by-fm-name", "platforms: [macos]\n", dir_name="some-dir")
    kha.clear_cache()
    monkeypatch.setattr(kha, "_profile_dirs", lambda assignee, now: (root,))
    got = kha.manifest_affinity(["cdp-login", "mac-app", "anywhere", "no-platforms",
                                 "missing", "ns:plugin", "deep-login", "by-fm-name"], "alpha")
    assert got == {"cdp-login": "manifest:host_affinity", "mac-app": "manifest:platforms=macos",
                   "deep-login": "manifest:platforms=macos", "by-fm-name": "manifest:platforms=macos"}
    # Literal match only: a glob-shaped name never borrows another skill's manifest.
    assert kha.manifest_affinity(["cdp-logi?", "mac-*", "general/mac-?pp"], "alpha") == {}
    # A path-qualified id the worker loads keeps its binding (Prism fbf21aa8cc13).
    assert kha.manifest_affinity(["general/mac-app"], "alpha") == {
        "general/mac-app": "manifest:platforms=macos"}
    kha.clear_cache()


def test_manifest_affinity_unions_every_root_and_never_caches_a_failure(tmp_path, monkeypatch):
    """A binding declared by ANY resolvable copy counts, whichever root is
    searched first (Prism e16f8f5c); a failed read is not cached as "no
    binding" for the TTL (Prism 7cfa01ca / 94f5d2a0)."""
    from hermes_cli import kanban_host_affinity as kha
    real_profile_dirs = kha._profile_dirs
    first, second = tmp_path / "local", tmp_path / "shared"
    _skill(first, "browser-login", "description: unbound copy\n")
    _skill(second, "browser-login", "platforms: [macos]\n")
    kha.clear_cache()
    monkeypatch.setattr(kha, "_profile_dirs", lambda assignee, now: (first, second))
    assert kha.manifest_affinity(["browser-login"], "alpha") == {
        "browser-login": "manifest:platforms=macos"}
    # A throwing index build fails open for THIS call and leaves no cache entry.
    kha.clear_cache()
    calls = {"n": 0}
    real = kha._build_index

    def flaky(dirs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(dirs)
    monkeypatch.setattr(kha, "_build_index", flaky)
    assert kha.manifest_affinity(["browser-login"], "alpha") == {}
    assert kha.manifest_affinity(["browser-login"], "alpha") == {
        "browser-login": "manifest:platforms=macos"}
    assert calls["n"] == 2
    # Same for the profile-dir lookup: a throwing read returns None and is
    # not cached as (), so the next tick asks again.
    kha.clear_cache()
    import hermes_cli.profiles as profiles

    def boom(_assignee):
        raise RuntimeError("profile store busy")
    monkeypatch.setattr(profiles, "get_profile_dir", boom)
    monkeypatch.setattr(kha, "_profile_dirs", real_profile_dirs)
    assert kha._profile_dirs("alpha", 0.0) is None
    assert "alpha" not in kha._dirs_cache
    assert kha.manifest_affinity(["browser-login"], "alpha") == {}
    kha.clear_cache()


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def test_browser_card_under_spilling_studio_waits_never_spills(kanban_home, monkeypatch):
    """The t_79436ebf shape: a browser-access card while the Studio spills."""
    from hermes_cli import kanban_host_affinity as kha
    monkeypatch.setattr(kha, "manifest_affinity",
                        lambda skills, assignee, now=None: {
                            s: "manifest:host_affinity" for s in skills if s == "browser-access"})
    with kb.connect_closing() as conn:
        (browser,) = _make(conn, 1, priority=9, skills=["browser-access"])
        (coding,) = _make(conn, 1, priority=1)
        plan = _plan(free=2, band="spilling")
        res, spawned = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="spilling")
        assert spawned == [(coding, "ace-ai")]          # the plain coding card still spills
        assert browser not in [t for t, _ in spawned]   # never ace-ai / ace-media
        assert res.placement_waits[browser] == "studio_affinity:studio_manifest"
        assert res.placement_affinity[browser]["host"] == "mac-studio"
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (browser,)).fetchone()[0]
        assert status == "ready"
        aff = _events(conn, browser, "placement_affinity")
        waits = _events(conn, browser, "placement_wait")
        assert aff == [{"host": "mac-studio", "rule": "studio_manifest",
                        "matched_by": "skill:browser-access (manifest:host_affinity)"}]
        assert waits == [{"host": "mac-studio", "reason": "studio_affinity:studio_manifest",
                          "matched_by": "skill:browser-access (manifest:host_affinity)"}]
        # A second identical tick writes no duplicate events.
        _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=0,
              spawn_paused="spilling")
        assert len(_events(conn, browser, "placement_affinity")) == 1
        assert len(_events(conn, browser, "placement_wait")) == 1
        # Spawned in between (a claim), then waiting again for the same reason:
        # that is a NEW wait and is recorded.
        kb._append_event(conn, browser, "claimed", {"lock": "test"})
        conn.commit()
        _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=0,
              spawn_paused="spilling")
        assert len(_events(conn, browser, "placement_wait")) == 2
        # A (re)assignment ends the episode too (shared end-kind set, Prism
        # 75f6107b): the same wait is re-written, so the view still lists it.
        assert kb.assign_task(conn, browser, "alpha")
        _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=0,
              spawn_paused="spilling")
        assert len(_events(conn, browser, "placement_wait")) == 3
        from hermes_cli import kanban_placement_view as kpv
        assert browser in [h["task_id"] for h in kpv._held(conn)]


def test_body_bound_card_runs_locally_when_the_studio_has_room(kanban_home):
    with kb.connect_closing() as conn:
        (cdp,) = _make(conn, 1, body="attach CDP to :9222 on the Studio")
        res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=1)
    assert spawned == [(cdp, None)] and res.placed == []
    assert res.placement_affinity[cdp]["matched_by"] == "body::9222"


def test_wait_cleared_without_a_claim_makes_the_next_wait_a_new_episode(kanban_home):
    """A bound card waits, then a tick evaluates it with no wait but it does
    not spawn (per-profile cap), then it waits again for the same reason:
    three episodes, three events (Prism 1ccf2233 / 5a51e524 on #1877)."""
    with kb.connect_closing() as conn:
        (cdp,) = _make(conn, 1, body="signed-in browser work")
        spill = lambda: _plan(free=2, band="spilling")  # noqa: E731
        _tick(conn, spillover=spill(), spawn_limit=0, spawn_paused="spilling")
        assert [e["reason"] for e in _events(conn, cdp, "placement_wait")] == [
            "studio_affinity:studio_body"]
        # Room on the Studio, but the assignee is at its per-profile cap: the
        # card is evaluated with NO wait reason and still not claimed.
        (busy,) = _make(conn, 1)
        conn.execute("UPDATE tasks SET status='running', claim_lock='x' WHERE id=?", (busy,))
        conn.commit()
        res, spawned = _tick(conn, spillover=spill(), spawn_limit=4,
                             max_in_progress_per_profile={"default": 8, "alpha": 1})
        assert cdp in [t for t, _who, _n in res.skipped_per_profile_capped]
        # Momus R-A (t_7133e03d): the per-profile hold is a named wait now,
        # so the episode turns over on the reason change instead of a clear.
        assert spawned == [] and res.placement_waits[cdp] == "per_profile_cap (alpha 1/1)"
        waits = _events(conn, cdp, "placement_wait")
        assert waits[-1]["reason"] == "per_profile_cap (alpha 1/1)"
        conn.execute("UPDATE tasks SET status='done', claim_lock=NULL WHERE id=?", (busy,))
        conn.commit()
        _tick(conn, spillover=spill(), spawn_limit=0, spawn_paused="spilling")
        waits = _events(conn, cdp, "placement_wait")
        assert [w.get("reason", "cleared") for w in waits] == [
            "studio_affinity:studio_body", "per_profile_cap (alpha 1/1)",
            "studio_affinity:studio_body"]
        # Repeating the same wait writes nothing new.
        _tick(conn, spillover=spill(), spawn_limit=0, spawn_paused="spilling")
        assert len(_events(conn, cdp, "placement_wait")) == 3


def test_lost_binding_is_recorded_and_rebinding_is_a_new_transition(kanban_home):
    """Removing a card's bound skill clears the binding in the event log;
    restoring it records the binding again (Prism d3ef126d on #1877)."""
    with kb.connect_closing() as conn:
        (card,) = _make(conn, 1, skills=["mac-only"])
        # One pool slot, always taken by a higher-priority portable filler
        # (a fresh one per tick): an UNBOUND card then waits pool_full and
        # stays ready across the ticks below instead of spilling.
        def spill():
            _make(conn, 1, priority=9, assignee="alpha")
            return _plan(free=1, band="spilling")
        plan = spill()
        plan.config = kwp.PoolConfig(hosts=plan.config.hosts, profiles=plan.config.profiles,
                                     studio_bound_skills=("mac-only",), pool_hosts=plan.config.pool_hosts)
        _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="spilling")
        assert _events(conn, card, "placement_affinity") == [
            {"host": "mac-studio", "rule": "studio_bound_skill", "matched_by": "skill:mac-only"}]
        # The operator edits the card: the bound skill is gone.
        conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (json.dumps([]), card))
        conn.commit()
        plan2 = spill()
        plan2.config = plan.config
        res, _ = _tick(conn, spillover=plan2, spawn_limit=0, spawn_paused="spilling")
        assert card not in res.placement_affinity
        assert _events(conn, card, "placement_affinity")[-1] == {"cleared": True}
        # The binding's wait ends with it (Prism 0f21fb8c); the card's own
        # pool wait is then a new placement_wait (main's per-card writer).
        assert [w.get("reason", "cleared") for w in _events(conn, card, "placement_wait")] == [
            "studio_affinity:studio_bound_skill", "cleared", "pool_full"]
        # Idempotent: an unbound card with a cleared history writes nothing.
        plan3 = spill()
        plan3.config = plan.config
        _tick(conn, spillover=plan3, spawn_limit=0, spawn_paused="spilling")
        assert len(_events(conn, card, "placement_affinity")) == 2
        # Restored: a new binding event, not a suppressed duplicate.
        conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (json.dumps(["mac-only"]), card))
        conn.commit()
        plan4 = spill()
        plan4.config = plan.config
        _tick(conn, spillover=plan4, spawn_limit=0, spawn_paused="spilling")
        aff = _events(conn, card, "placement_affinity")
        assert [a.get("rule", "cleared") for a in aff] == [
            "studio_bound_skill", "cleared", "studio_bound_skill"]
        # ...and the same wait after the re-bind is a new episode, not a duplicate.
        assert [w.get("reason", "cleared") for w in _events(conn, card, "placement_wait")] == [
            "studio_affinity:studio_bound_skill", "cleared", "pool_full",
            "studio_affinity:studio_bound_skill"]
        # A card that was never bound never gets a cleared event.
        (plain,) = _make(conn, 1)
        plan5 = spill()
        plan5.config = plan.config
        _tick(conn, spillover=plan5, spawn_limit=0, spawn_paused="spilling")
        assert _events(conn, plain, "placement_affinity") == []


def test_route_row_affinity_branch_is_reached_on_a_live_tick(kanban_home):
    """The studio-affinity branch in ``_route_row`` sits AFTER the remote-pin
    return and is reached for an unpinned bound card: the route carries
    ``studio_affinity:<rule>`` and the affinity map is filled (refutes Prism
    f3a0c70c "unreachable affinity" on #1877)."""
    with kb.connect_closing() as conn:
        (cdp,) = _make(conn, 1, body="signed-in browser work")
        (pinned,) = _make(conn, 1, body="host: ace-ai\nsigned-in browser work",
                          workspace_kind="worktree")
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker FROM tasks")}
        plan = _plan(free=2, band="spilling")
        out: dict = {}
        assert kbd._route_row(conn, rows[cdp], plan, None, {}, {}, affinity_out=out) == (
            "local", "studio_affinity:studio_body")
        assert out[cdp] == {"host": "mac-studio", "rule": "studio_body",
                            "matched_by": "body:signed-in browser"}
        # A REMOTE pin on a non-portable card returns before the branch.
        assert kbd._route_row(conn, rows[pinned], plan, None, {}, {}, affinity_out=out) == (
            "wait", "not_portable:workspace_kind")
        assert pinned not in out


def test_dry_run_shows_affinity_without_a_pool_plan_or_events(kanban_home):
    with kb.connect_closing() as conn:
        (cdp,) = _make(conn, 1, body="signed-in browser task")
        res = kbd.dispatch_once(conn, dry_run=True, max_spawn=8, spillover=None,
                                reconcile_orphans=False)
        assert res.placement_affinity[cdp] == {"host": "mac-studio", "rule": "studio_body",
                                               "matched_by": "body:signed-in browser"}
        assert _events(conn, cdp, "placement_affinity") == []


def test_dry_run_prose_host_line_is_not_a_pin(kanban_home):
    """A ``host: example.com`` header is prose, not a pin: affinity still shows."""
    with kb.connect_closing() as conn:
        (cdp,) = _make(conn, 1, body="host: example.com\nattach CDP :9222")
        (pinned,) = _make(conn, 1, body="host:any\nattach CDP :9222")
        res = kbd.dispatch_once(conn, dry_run=True, max_spawn=8, spillover=None,
                                reconcile_orphans=False)
    assert res.placement_affinity[cdp]["rule"] == "studio_body"
    assert pinned not in res.placement_affinity


# -- Remote-target affinity from skill manifests (card t_9eeb9c40) ----------
# ``host_affinity: <fleet host>`` means the work TARGETS that host over ssh
# (ACE-AI GPUs / Bambuddy). It feeds the reach matrix, never a pin: kanbanw on
# ace-ai cannot see the GPUs (I-4), the Studio reaches ace-ai (rc 0).

def _reach_cfg(rows, hosts=("ace-ai", "ace-media")):
    reach = {h: dict(r) for h, r in rows.items()}
    return kwp.PoolConfig(hosts=hosts, profiles=("alpha",), reach=reach,
                          reach_aliases=("ace-ai", "ace-media", "mac-studio-m3u"),
                          reach_status="ok")


def test_manifest_targets_reads_host_affinity_naming_a_fleet_host(tmp_path, monkeypatch):
    from hermes_cli import kanban_host_affinity as kha
    root = tmp_path / "skills"
    _skill(root, "vllm-gpu", "metadata:\n  hermes:\n    host_affinity: ACE-AI\n")
    _skill(root, "cdp-login", "metadata:\n  hermes:\n    host_affinity: studio\n")
    _skill(root, "plain", "description: x\n")
    kha.clear_cache()
    monkeypatch.setattr(kha, "_profile_dirs", lambda assignee, now: (root,))
    names = ["vllm-gpu", "cdp-login", "plain"]
    assert kha.manifest_targets(names, "alpha") == {"vllm-gpu": "ace-ai"}
    # ``studio`` stays the Studio-state binding, a remote target never is one.
    assert kha.manifest_affinity(names, "alpha") == {"cdp-login": "manifest:host_affinity"}
    kha.clear_cache()


def test_manifest_target_refused_on_non_reaching_pool_host_allowed_once_reached():
    targets = {"vllm-gpu": "ace-ai"}
    no_route = _reach_cfg({"ace-ai": {"ace-ai": 255}, "ace-media": {"ace-ai": 255}})
    # (1) every pool host is rc 255 to ace-ai: not portable, stays on the Studio.
    assert kwp.reach_allowed_hosts(body=None, skills=("vllm-gpu",), pool=no_route,
                                   targets=targets) == ()
    assert _portable(skills=("vllm-gpu",), pool=no_route, targets=targets) == (
        False, "host_reach", None)
    # The live matrix also carries the Studio's own row (rc 0 to ace-ai): the
    # allowed set is then the Studio only, which take() never matches.
    with_studio = _reach_cfg({"ace-ai": {"ace-ai": 255}, "ace-media": {"ace-ai": 255},
                              "mac-studio-m3u": {"ace-ai": 0}})
    assert kwp.reach_allowed_hosts(body=None, skills=("vllm-gpu",), pool=with_studio,
                                   targets=targets) == ("mac-studio-m3u",)
    # (2) a fixture row gives ace-media rc 0: portable, and take() is held to it.
    routed = _reach_cfg({"ace-ai": {"ace-ai": 255}, "ace-media": {"ace-ai": 0}})
    assert _portable(skills=("vllm-gpu",), pool=routed, targets=targets) == (True, None, "policy")
    assert kwp.reach_allowed_hosts(body=None, skills=("vllm-gpu",), pool=routed,
                                   targets=targets) == ("ace-media",)
    # Never a pin: the target host's own worker is not preferred for it.
    assert _portable(skills=("vllm-gpu",), pool=routed, targets=targets)[2] != "pin"
    # A target nobody probed fails closed; no target = unchanged (not host-bound).
    assert _portable(skills=("x",), pool=routed, targets={"x": "nas-9"}) == (
        False, "host_reach", None)
    assert kwp.reach_allowed_hosts(body=None, skills=("x",), pool=routed, targets={}) is None
    # (3) a studio binding is unaffected by the reach path.
    assert _portable(skills=("browser-access",), pool=routed,
                     manifest={"browser-access": "manifest:host_affinity"}) == (
        False, "studio_manifest", None)


def test_gpu_card_never_spills_to_a_pool_host_without_a_route(kanban_home, monkeypatch):
    """Dispatcher end to end: a ``host_affinity: ace-ai`` card under a spilling
    Studio is not placed on ace-ai/ace-media (rc 255); it runs on the Studio
    when a local slot is free, and spills to a pool host once its row is rc 0."""
    from hermes_cli import kanban_host_affinity as kha
    monkeypatch.setattr(kha, "manifest_targets", lambda skills, assignee, now=None: {
        s: "ace-ai" for s in skills if s == "vllm-gpu"})
    monkeypatch.setattr(kb, "_card_skills_refused", lambda *a, **kw: False)
    hosts = [_host("ace-ai", slots=2, priority=0), _host("ace-media", slots=2, priority=1)]

    def plan_for(rows):
        p = kwp.plan(hosts, {}, probe=lambda h: (1.0, 16), config=_reach_cfg(rows))
        p.band, p.spill_reason = "spilling", "load"
        return p

    no_route = {"ace-ai": {"ace-ai": 255}, "ace-media": {"ace-ai": 255},
                "mac-studio-m3u": {"ace-ai": 0}}       # the live matrix shape
    with kb.connect_closing() as conn:
        (gpu,) = _make(conn, 1, priority=9, skills=["vllm-gpu"])
        (coding,) = _make(conn, 1, priority=1)
        res, spawned = _tick(conn, spillover=plan_for(no_route), spawn_limit=0,
                             spawn_paused="spilling")
        assert spawned == [(coding, "ace-ai")]
        assert gpu not in [t for t, _ in spawned]
        assert res.placement_waits[gpu] == "host_reach"
    with kb.connect_closing() as conn:
        res, spawned = _tick(conn, spillover=plan_for(no_route), spawn_limit=4)
        assert (gpu, None) in spawned                      # local: the Studio reaches ace-ai
    with kb.connect_closing() as conn:
        (gpu2,) = _make(conn, 1, priority=9, skills=["vllm-gpu"])
        routed = {"ace-ai": {"ace-ai": 255}, "ace-media": {"ace-ai": 0}}
        res, spawned = _tick(conn, spillover=plan_for(routed), spawn_limit=0,
                             spawn_paused="spilling")
        assert (gpu2, "ace-media") in spawned


def test_unnamed_host_bound_card_still_requires_its_manifest_target():
    """Prism bf503f94e81a: a HOST_BOUND skill with no alias in the body takes
    the 'reach every alias' fallback; a manifest target must join that set,
    so a worker that reaches every alias but not the target is refused."""
    targets = {"vllm-gpu": "ace-ai"}
    skills = ("ssh-machines", "vllm-gpu")
    aliases_ok = {"ace-media": 0, "mac-studio-m3u": 0}
    no_route = _reach_cfg({"ace-ai": {**aliases_ok, "ace-ai": 255},
                           "ace-media": {**aliases_ok, "ace-ai": 255}})
    assert kwp.reach_allowed_hosts(body="fix it", skills=skills, pool=no_route,
                                   targets=targets) == ()
    assert _portable(body="fix it", skills=skills, pool=no_route, targets=targets) == (
        False, "host_reach", None)
    routed = _reach_cfg({"ace-ai": {**aliases_ok, "ace-ai": 255},
                         "ace-media": {**aliases_ok, "ace-ai": 0}})
    assert kwp.reach_allowed_hosts(body="fix it", skills=skills, pool=routed,
                                   targets=targets) == ("ace-media",)
    # An unprobed target (no column anywhere) fails closed in the fallback too.
    assert kwp.reach_allowed_hosts(body="fix it", skills=("ssh-machines", "x"), pool=routed,
                                   targets={"x": "nas-9"}) == ()


def test_manifest_binding_survives_a_later_unreadable_copy(tmp_path, monkeypatch):
    """Prism 883b1cd1ac29: a Studio binding found in one SKILL.md copy is kept
    when a later copy cannot be read; the incomplete lookup is not cached."""
    from hermes_cli import kanban_host_affinity as kha
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a, "cdp-login", "metadata:\n  hermes:\n    host_affinity: studio\n")
    _skill(b, "cdp-login", "description: x\n")
    kha.clear_cache()
    monkeypatch.setattr(kha, "_profile_dirs", lambda assignee, now: (a, b))
    bad = b / "general" / "cdp-login" / "SKILL.md"
    real_read = type(bad).read_text

    def flaky(self, *args, **kw):
        if self == bad:
            raise OSError("EIO")
        return real_read(self, *args, **kw)
    kha.manifest_affinity(["cdp-login"], "alpha", now=1.0)   # build the index first
    kha._skill_cache.clear()                                  # keep the index, re-read the copies
    monkeypatch.setattr(type(bad), "read_text", flaky)
    assert kha.manifest_affinity(["cdp-login"], "alpha", now=2.0) == {
        "cdp-login": "manifest:host_affinity"}
    assert not kha._skill_cache                                # incomplete lookup: not cached
    kha.clear_cache()


def test_manifest_target_honours_path_qualified_card_skill(tmp_path, monkeypatch):
    """Prism fbf21aa8cc13: ``mlops/vllm-gpu`` (a categorized id skill_view
    accepts) keeps its manifest target, keyed by the card's own identifier."""
    from hermes_cli import kanban_host_affinity as kha
    root = tmp_path / "skills"
    _skill(root, "vllm-gpu", "metadata:\n  hermes:\n    host_affinity: ace-ai\n", category="mlops")
    _skill(root, "cdp-login", "metadata:\n  hermes:\n    host_affinity: studio\n")
    kha.clear_cache()
    monkeypatch.setattr(kha, "_profile_dirs", lambda assignee, now: (root,))
    assert kha.manifest_targets(["mlops/vllm-gpu"], "alpha") == {"mlops/vllm-gpu": "ace-ai"}
    assert kha.manifest_affinity(["general/cdp-login"], "alpha") == {
        "general/cdp-login": "manifest:host_affinity"}
    # Traversal / wrong category never resolve.
    assert kha.manifest_targets(["../skills/mlops/vllm-gpu", "general/vllm-gpu"], "alpha") == {}
    kha.clear_cache()


# -- t_e10add5a: a pin to a DRAINED member is not a full one ----------------

def _drained_plan():
    p = _plan(hosts=[_host("ace-ai", slots=1), _host("ci-box", slots=2, priority=1, state="draining")])
    p.disabled = ("old-box",)
    return p


def test_pin_to_a_draining_host_reads_pin_host_draining():
    """2026-10-11: t_d69f0790 pinned host:ci-box while ci-box drained read
    ``pin_host_full`` for 25 min. Draining (re-pin) is not full (wait)."""
    p = _drained_plan()
    assert p.take("alpha", pin="ci-box") is None and p.refusal == "pin_host_draining"
    assert p.take("alpha", pin="old-box") is None and p.refusal == "pin_host_disabled"
    assert p.take("alpha", pin="ace-ai").name == "ace-ai"
    assert p.take("alpha", pin="ace-ai") is None and p.refusal == "pin_host_full"


def test_drained_pin_reason_reaches_placement_wait_and_dry_run(kanban_home, capsys, monkeypatch):
    from hermes_cli import kanban_ops as kops
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host: ci-box")
        _tick(conn, spillover=_drained_plan(), spawn_limit=4)
        assert [e["reason"] for e in _placement_waits(conn, tid)] == ["pin_host_draining"]
        assert _placement_waits(conn, tid)[0]["host"] == "ci-box"
    monkeypatch.setattr(kops, "_one_shot_pool_plan", lambda *a, **k: _drained_plan())
    import argparse
    rc = kops._cmd_dispatch(argparse.Namespace(dry_run=True, json=False, max=None,
                                               max_running=None, failure_limit=2))
    assert rc == 0 and f"  {tid}  route=remote-pin host=ci-box rule=pin  -> holds pin_host_draining" \
        in capsys.readouterr().out


def test_drained_pin_behind_the_no_slot_wall_still_names_the_state(kanban_home):
    """No local slot and no pool slot: the row that hits the wall keeps its state."""
    p = _plan(hosts=[_host("ci-box", slots=2, state="draining")])
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host: ci-box")
        res, spawned = _tick(conn, spillover=p, spawn_limit=0)
    assert spawned == [] and res.placement_waits.get(tid) == "pin_host_draining"


def test_drained_pin_wait_posts_one_logs_line_after_15_min(kanban_home, monkeypatch):
    from hermes_cli import kanban_budget as kbud
    sent = []
    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", lambda argv: sent.append(argv) or True)
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host: ci-box")
        _tick(conn, spillover=_drained_plan(), spawn_limit=4)
        assert sent == []  # a fresh wait is not news
        conn.execute("UPDATE task_events SET payload = json_set(payload, '$.since', "
                     "json_extract(payload, '$.since') - 16*60) WHERE task_id=? "
                     "AND kind='placement_wait'", (tid,))
        conn.commit()
        _tick(conn, spillover=_drained_plan(), spawn_limit=4)
        _tick(conn, spillover=_drained_plan(), spawn_limit=4)
    assert len(sent) == 1
    body = sent[0][-1]
    assert tid in body and "ci-box" in body and "draining" in body and "16 min" in body
    assert sent[0][sent[0].index("--target") + 1] == kbud.RECOVERY_TARGET


def test_unassigned_pin_is_refused_pin_profile_and_takes_no_slot():
    """Prism 6884d3dc366b: an unassigned card pinned to a member is refused
    ``pin_profile`` (base semantics) and reserves nothing."""
    p = _plan(hosts=[_host("ace-ai", slots=1)])
    assert p.take(None, pin="ace-ai") is None and p.refusal == "pin_profile"
    assert p.slots["ace-ai"] == 1 and p.taken.get("ace-ai", 0) == 0 and p.budget == 1
    p = _drained_plan()
    before = dict(p.slots)
    assert p.take(None, pin="ci-box") is None and p.refusal == "pin_profile"
    assert p.slots == before


def test_drained_pin_latch_survives_a_tick_the_card_is_missing_from(kanban_home, monkeypatch):
    """Prism a54ce82ab27c: a card absent from one tick's waits (no-slot wall,
    the first ticks after a restart) keeps its latch and is not re-posted."""
    from hermes_cli import kanban_budget as kbud
    from hermes_cli import kanban_drained_pin as kdp
    sent = []
    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", lambda argv: sent.append(argv) or True)
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host: ci-box")
        _tick(conn, spillover=_drained_plan(), spawn_limit=4)
        conn.execute("UPDATE task_events SET payload = json_set(payload, '$.since', "
                     "json_extract(payload, '$.since') - 16*60) WHERE task_id=? "
                     "AND kind='placement_wait'", (tid,))
        conn.commit()
        now = int(time.time())
        assert len(kdp.notify_drained_pins(conn, None, {tid: "pin_host_draining"}, now)) == 1
        kdp.notify_drained_pins(conn, None, {}, now + 60)  # card not evaluated this tick
        assert kdp.notify_drained_pins(conn, None, {tid: "pin_host_draining"}, now + 120) == []
    assert len(sent) == 1


def test_drained_pin_corrupt_marker_posts_nothing_and_is_rewritten(kanban_home, monkeypatch):
    """Prism ac5ce939aace: a torn/unreadable latch marker is latched-unknown,
    not empty: no #logs storm this tick, and the marker is rewritten valid."""
    from hermes_cli import kanban_budget as kbud
    from hermes_cli import kanban_drained_pin as kdp
    sent = []
    monkeypatch.setattr(kbud, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kbud, "_run_notify", lambda argv: sent.append(argv) or True)
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host: ci-box")
        _tick(conn, spillover=_drained_plan(), spawn_limit=4)
        conn.execute("UPDATE task_events SET payload = json_set(payload, '$.since', "
                     "json_extract(payload, '$.since') - 16*60) WHERE task_id=? "
                     "AND kind='placement_wait'", (tid,))
        conn.commit()
        marker = kb.board_state_dir(None) / kdp._MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"t_', encoding="utf-8")  # torn write
        now = int(time.time())
        assert kdp.notify_drained_pins(conn, None, {tid: "pin_host_draining"}, now) == []
        latched = json.loads(marker.read_text(encoding="utf-8"))
        assert isinstance(latched.get(tid), int)
        assert kdp.notify_drained_pins(conn, None, {tid: "pin_host_draining"}, now + 60) == []
    assert sent == []
    assert not list(marker.parent.glob(kdp._MARKER + ".*"))  # no tmp left behind


# -- host: pins by hostname/alias; unknown pins are loud (t_cb2b888d) ------

def _write_ci_hosts(fleet: Path):
    (fleet / kwp.CI_HOSTS_FILE).write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"ssh_alias": "ace-ai", "transport": "ssh", "host_label": "ace-ai"},
        "studio": {"ssh_alias": None, "transport": "local", "host_label": "studio"},
        "ci-box": {"ssh_alias": "ci.ace", "transport": "ssh", "host_label": "ci-box"},
        "unrelated": {"ssh_alias": "elsewhere", "transport": "ssh", "host_label": "x"},
    }}), encoding="utf-8")


@pytest.fixture
def fleet_aliases(kanban_home, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "mac-studio-m3u.local")
    fleet = kanban_home / "fleet"
    _write_pool(fleet)
    _write_ci_hosts(fleet)
    return kwp.known_host_ids(fleet), kwp.pin_aliases(fleet)


@pytest.mark.parametrize("body,pin", [
    ("host:mac-studio-m3u", "studio"),   # the Studio's hostname -s (incident t_cccdee67)
    ("host:MAC-STUDIO", "studio"),
    ("host:local", "studio"),
    ("host:ci.ace", "ci-box"),           # ssh alias from ci-hosts.json
    ("host:ci", "ci-box"),               # its short DNS label
    ("host:ACE-AI", "ace-ai"),           # hostname -s on ace-ai is upper case
    ("Repro:\nHost: example.com\n\nhost:ci.ace\n", "ci-box"),  # Prism r8 preserved
])
def test_pin_resolves_hostname_and_registry_aliases_to_the_canonical_pin(fleet_aliases, body, pin):
    known, aliases = fleet_aliases
    assert kwp.resolve_pin(body, known, aliases)[0] == pin


def test_pin_aliases_come_only_from_the_registry(fleet_aliases):
    _known, aliases = fleet_aliases
    assert "elsewhere" not in aliases and "unrelated" not in aliases  # not a pool host
    assert aliases["ci.ace"] == "ci-box" and aliases["studio"] == "studio"


def test_typo_pin_is_not_portable_and_names_suggestions(fleet_aliases):
    known, aliases = fleet_aliases
    assert kwp.resolve_pin("host:mac-studio-m3", known, aliases) == (None, "mac-studio-m3")
    pool = kwp.PoolConfig(hosts=known, profiles=("alpha",), pin_aliases=aliases)
    assert _portable(body="host:mac-studio-m3", pool=pool) == (False, "pin_unknown_host", None)
    assert "mac-studio-m3u->studio" in kwp.pin_suggestions("mac-studio-m3", known, aliases)


def test_alias_pin_routes_to_the_canonical_pool_host(fleet_aliases):
    known, aliases = fleet_aliases
    pool = kwp.PoolConfig(hosts=known, profiles=("alpha",), pin_aliases=aliases)
    assert _portable(body="host:ACE-AI", pool=pool) == (True, None, "pin")
    assert _portable(body="host:mac-studio-m3u", pool=pool) == (False, "host_studio", None)


def test_typo_pin_is_refused_with_one_comment_per_park(fleet_aliases):
    """host:mac-studio-m3 (typo): never spawned anywhere, placement_wait names
    the word and suggestions, the card parks needs_input with ONE comment; it
    stays parked across ticks; unblocked with another bad word, it parks again."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="Do it.\nhost:mac-studio-m3\n")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
        assert spawned == [] and res.placed == []
        reason = res.placement_waits[tid]
        assert reason.startswith("pin_unknown_host:mac-studio-m3 (did you mean: ")
        assert "mac-studio-m3u->studio" in reason
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = "
                          "'placement_wait' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        assert json.loads(ev[0])["reason"] == reason
        for _ in range(2):
            _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
            assert spawned == []
        assert kb.get_task(conn, tid).status == "blocked"
        comments = [c.body for c in kb.list_comments(conn, tid)]
        assert len(comments) == 1 and "host:mac-studio-m3 names no host" in comments[0]
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("host:ace-ia", tid))
        assert kb.unblock_task(conn, tid)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
        # A second refusal is a recurrence: the block loop-breaker may route it to triage.
        assert spawned == [] and kb.get_task(conn, tid).status in ("blocked", "triage")
        assert len(kb.list_comments(conn, tid)) == 2


# -- pins are HARD: trailing spelling, fleet-hosts aliases, refusal (t_229d6bca) --

def _write_fleet_hosts(home: Path):
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "fleet-hosts.json").write_text(json.dumps({"schema": 1, "hosts": {
        "ace-ai": {"aliases": ["aceai", "ace-ai.local"]},
        "ace-media": {"aliases": ["ace-media-ubuntu"]},
        "mac-studio": {"aliases": ["studio", "mac-studio-m3u", "local"]},
    }}), encoding="utf-8")


@pytest.fixture
def fleet_hosts_home(kanban_home, monkeypatch):
    """The Studio dispatcher's hostname is NOT one of the spellings, so a
    mac-studio-m3u pin must come from config/fleet-hosts.json aliases."""
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "elsewhere-box")
    _write_pool(kanban_home / "fleet")
    _write_fleet_hosts(kanban_home)
    return kanban_home


@pytest.mark.parametrize("body", [
    "host: studio",
    "host: mac-studio",
    "host: mac-studio-m3u",
    "host: STUDIO",
    # The three cards of 2026-10-20 01:1x (t_e6452500, t_fbf79823, t_229d6bca):
    # the pin trails the last sentence, so the line-anchored parser saw none.
    "origin: Ace 23:46 ... Step 1 measured. Admin-merge lane. host: studio\n",
    "Hand back with: PR, head sha, deviations (none expected). host: studio.\n\nscope: any",
    "Then t_16b9087f pages on any pinned card placed elsewhere. host: mac-studio\n",
])
def test_every_studio_spelling_resolves_to_the_studio_pin(fleet_hosts_home, body):
    fleet = fleet_hosts_home / "fleet"
    assert kwp.resolve_pin(body, kwp.known_host_ids(fleet), kwp.pin_aliases(fleet)) == ("studio", None)


def test_prose_mid_sentence_host_word_is_still_not_a_pin(fleet_hosts_home):
    fleet = fleet_hosts_home / "fleet"
    assert kwp.resolve_pin("set the header host: example.com on it", kwp.known_host_ids(fleet),
                           kwp.pin_aliases(fleet)) == (None, None)


def test_fleet_hosts_aliases_canonicalise_pool_hosts(fleet_hosts_home):
    aliases = kwp.pin_aliases(fleet_hosts_home / "fleet")
    assert aliases["aceai"] == "ace-ai" and aliases["ace-media-ubuntu"] == "ace-media"
    assert aliases["mac-studio-m3u"] == "studio" and aliases["studio"] == "studio"


def test_trailing_studio_pin_dispatches_to_the_studio_never_the_pool(fleet_hosts_home):
    """Red on main: the trailing pin was no pin, the card was portable and the
    pool took it (ace-ai here, ace-media tonight)."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="Do the thing on the live tree. host: studio\n")
        res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=0,
                             spawn_paused="test")
        assert spawned == [] and res.placed == []
        res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=4)
    assert spawned == [(tid, None)] and res.placed == []


def test_unknown_pin_blocks_the_card_naming_the_accepted_spellings(fleet_hosts_home):
    """Red on main: an unknown word was held local and then ran locally."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="Do it.\nhost: nosuchbox\n")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
        assert spawned == [] and res.placed == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        comments = [c.body for c in kb.list_comments(conn, tid)]
    assert len(comments) == 1
    line = comments[0]
    assert "host:nosuchbox" in line
    for word in ("studio", "mac-studio", "mac-studio-m3u", "ace-ai", "aceai", "ace-media", "any"):
        assert re.search(rf"(?<![\w.-]){re.escape(word)}(?![\w-])", line), word


def test_hostname_pin_runs_local_never_spills(fleet_aliases):
    """The incident: host:mac-studio-m3u under a spilling band went to ace-ai."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, body="host:mac-studio-m3u")
        res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=0,
                             spawn_paused="test")
        assert kb.list_comments(conn, tid) == []
    assert spawned == [] and res.placed == []
    assert not res.placement_waits[tid].startswith("pin_unknown_host")


# -- overflow (t_7b1eca07) ----------------------------------------------------

def test_studio_only_derives_pool_profiles_from_the_profiles_dir(tmp_path):
    """Deny-list: every profile with a config.yaml is a pool profile unless
    studio_only names it; a new profile is admitted with no sidecar edit."""
    home = tmp_path / ".hermes"
    for p in ("alpha", "beta", "cadmus", "new-worker"):
        (home / "profiles" / p).mkdir(parents=True)
        (home / "profiles" / p / "config.yaml").write_text("{}\n")
    (home / "profiles" / "shell-only").mkdir()          # no config.yaml: not a profile
    sidecar = _write_pool(home / "fleet")
    del sidecar["profiles"]
    sidecar["studio_only"] = ["cadmus"]
    _write_pool(home / "fleet", sidecar=sidecar)
    cfg = kwp.read_pool(home / "fleet")
    assert cfg.refused is None and cfg.warnings == ()
    assert cfg.profiles == ("alpha", "beta", "new-worker")
    assert all(h.profiles == cfg.profiles for h in cfg.pool_hosts)
    ok, rule, _ = kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                               assignee="cadmus", body="", pool=cfg)
    assert (ok, rule) == (False, "profile")
    assert kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                        assignee="new-worker", body="", pool=cfg)[0] is True


def test_studio_only_also_filters_per_host_profile_overrides(tmp_path):
    """Prism 34f523dde2d6: a hosts.<h>.profiles row naming a denied profile must
    not re-admit it, neither on the host nor in the classifier allowlist."""
    home = tmp_path / ".hermes"
    for p in ("alpha", "cadmus"):
        (home / "profiles" / p).mkdir(parents=True)
        (home / "profiles" / p / "config.yaml").write_text("{}\n")
    sidecar = _write_pool(home / "fleet")
    del sidecar["profiles"]
    sidecar["studio_only"] = ["Cadmus"]
    sidecar["hosts"]["ace-ai"]["profiles"] = ["cadmus", "alpha"]
    _write_pool(home / "fleet", sidecar=sidecar)
    cfg = kwp.read_pool(home / "fleet")
    assert "cadmus" not in cfg.profiles
    assert all("cadmus" not in h.profiles for h in cfg.pool_hosts)
    assert kwp.portable(workspace_kind="scratch", has_links=False, workspace_has_content=False,
                        assignee="cadmus", body="", pool=cfg)[:2] == (False, "profile")


def test_studio_only_must_be_a_string_list(tmp_path):
    sidecar = _write_pool(tmp_path)
    sidecar["studio_only"] = "cadmus"
    _write_pool(tmp_path, sidecar=sidecar)
    assert kwp.read_pool(tmp_path).refused == "sidecar"


def _admitting_builder(fleet, kanban_cfg):
    from gateway.kanban_gate_tick import GateTickBuilder
    from hermes_cli.kanban_load_gate import LoadGate

    gate = LoadGate({}, ncpu=32)
    gate.band, gate.remote_allowed = "admitting", True
    return gate, GateTickBuilder(gate, fleet_dir=lambda: fleet, kanban_cfg=lambda: kanban_cfg,
                                 ledger=lambda b: {}, connect=lambda board=None: kb.connect(board=board),
                                 probe=lambda h: (1.0, 16))


def test_admitting_band_plans_the_pool_for_a_bare_ready_card(kanban_home):
    """Red on base: admitting + a bare card (no host line) planned NO pool, so
    it could only ever run locally. Overflow plans it and it is not pins-only."""
    _write_pool(kanban_home / "fleet")
    with kb.connect_closing() as conn:
        _make(conn, 1, body="build the thing")
    gate, b = _admitting_builder(kanban_home / "fleet", V01)
    plan = b.plan_pool([{"slug": "default"}], {})
    assert isinstance(plan, kwp.SpilloverPlan) and plan.pins_only is False and plan.budget == 8
    # Remote-first (#2148, t_82b4db80): overflow off plans the same card too.
    gate, b = _admitting_builder(kanban_home / "fleet", {**V01, "worker_pool": {"overflow": False}})
    plan = b.plan_pool([{"slug": "default"}], {})
    assert isinstance(plan, kwp.SpilloverPlan) and plan.pins_only is False


def test_admitting_band_with_nothing_ready_does_not_probe(kanban_home):
    _write_pool(kanban_home / "fleet")
    gate, b = _admitting_builder(kanban_home / "fleet", V01)
    assert b.plan_pool([{"slug": "default"}], {}) is None
    assert gate.pool == {"planned": False, "reason": "not_needed"}


def test_admitting_overflow_places_a_bare_card_past_the_local_allowance(kanban_home):
    """A bare scratch card with a pool profile routes to the pool once the
    local allowance is spent; the first one still runs locally; host: studio
    never leaves."""
    plan = _plan(free=2, band="admitting")
    with kb.connect_closing() as conn:
        (first,) = _make(conn, 1, body="no host line", priority=9)
        (second,) = _make(conn, 1, body="no host line", priority=8)
        (studio,) = _make(conn, 1, body="host: studio", priority=7)
        res, spawned = _tick(conn, spillover=plan, spawn_limit=1)
        assert (first, None) in spawned or (studio, None) in spawned
        assert (second, "ace-ai") in spawned
        assert all(t != studio or h is None for t, h in spawned)
        placed = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (second, kwp.PLACED_EVENT))]
        assert placed and placed[0]["reason"] == "overflow" and placed[0]["band"] == "admitting"
