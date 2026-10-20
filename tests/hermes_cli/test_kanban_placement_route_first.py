"""Traceability-vs-Placement spec v1.1, Phase 2a (t_7133e03d): route-first
ordering, the complete hold record and the ``placement`` record.

AC numbers are the spec's §10 table. Every dispatcher test runs under the
AC-5 seam of ``test_kanban_worker_pool``: the real spawn path raises.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import textwrap

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_placement_reasons as pr
from tests.hermes_cli.test_kanban_worker_pool import (  # noqa: F401  (fixtures by name)
    _make, _no_real_spawn, _plan, _tick, kanban_home,
)

PLACEMENT_KEYS = {"run_id", "route", "rule", "host", "considered", "gate", "plan_at", "entry",
                  # Control-Plane Protection v0.3 §3.1 (t_667f1f6a): the I-1 / AC-2 fields.
                  "route_class", "placement_default", "reason", "take_refusal", "pool_free_at_spawn"}


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _count(conn, kind, ids=None):
    sql, args = "SELECT COUNT(*) FROM task_events WHERE kind=?", [kind]
    if ids is not None:
        sql += f" AND task_id IN ({','.join('?' * len(ids))})"
        args += list(ids)
    return conn.execute(sql, args).fetchone()[0]


def _status(conn, tid, status):
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    conn.commit()


def _config(monkeypatch, kanban=None):
    from hermes_cli import config as _config
    cfg = {"kanban": kanban or {}}
    monkeypatch.setattr(_config, "load_config_readonly", lambda: cfg)
    monkeypatch.setattr(_config, "load_config", lambda: cfg)


def _ac5_board(conn):
    """1 pin + 3 portable + 1 studio + 1 review, priorities pin the loop order."""
    (pin,) = _make(conn, 1, body="host: ace-ai", priority=9)
    ports = _make(conn, 3, body="host: any", priority=5)
    (studio,) = _make(conn, 1, body="host: studio", priority=3)
    (rev,) = _make(conn, 1, priority=1)
    _status(conn, rev, "review")
    return pin, ports, studio, rev


# -- AC-5 -------------------------------------------------------------------

def test_ac5_paused_band_routes_every_row(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kb.connect_closing() as conn:
        pin, ports, studio, rev = _ac5_board(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        assert spawned == [(pin, "ace-ai"), (ports[0], "ace-ai")]
        routes = [e[0]["route"] for e in (_events(conn, pin, "placement"),
                                          _events(conn, ports[0], "placement"))]
        assert routes == ["remote-pin", "remote-policy"]
        assert res.placement_waits == {ports[1]: "pool_full", ports[2]: "pool_full",
                                       studio: "local_full"}
        assert _count(conn, "placement") == 2
        assert _count(conn, "placement_wait") == 3
        assert _events(conn, rev, "placement_wait") == [] and rev not in res.placement_waits
        assert rev not in [t for t, _h in spawned]


def test_ac5_one_shot_path_same_table(kanban_home, monkeypatch, capsys):
    """Same board through ``kanban dispatch`` with a gate returning allowance 0."""
    from hermes_cli import kanban_ops as kops
    _config(monkeypatch)
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(kops, "_one_shot_load_gate", lambda conn, args, cfg, add, defer=False: {
        "spawn_paused": "paused", "spawn_limit": 0, "limit_source": "load gate",
        "override": None, "info": None, "gate": None,
        "lines": ["Load gate: PAUSED load1=99.0 (pause_above=64.0) - local allowance 0: paused"]})
    monkeypatch.setattr(kops, "_one_shot_pool_plan", lambda *a, **k: _plan(free=2))
    spawned = []
    monkeypatch.setattr(kbd, "_default_spawn",
                        lambda task, ws, board=None, placement=None:
                        spawned.append((task.id, placement and placement.name)) or 4242)
    with kb.connect_closing() as conn:
        pin, ports, studio, rev = _ac5_board(conn)
    rc = kops._cmd_dispatch(argparse.Namespace(dry_run=False, json=False, max=None,
                                               max_running=None, failure_limit=2))
    assert rc == 0
    assert spawned == [(pin, "ace-ai"), (ports[0], "ace-ai")]
    with kb.connect_closing() as conn:
        assert _count(conn, "placement") == 2 and _count(conn, "placement_wait") == 3
        assert _events(conn, rev, "placement_wait") == []
        assert {e["entry"] for t in (pin, ports[0]) for e in _events(conn, t, "placement")} == {"cli"}


# -- AC-6 / AC-18 -------------------------------------------------------------

def _total_gate(monkeypatch, conn, gate):
    """Arrange one total gate to fire; returns extra dispatch_once kwargs."""
    if gate == "memory_critical":
        monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "critical")
        return {}
    if gate == "budget_paused":
        from hermes_cli import kanban_budget
        monkeypatch.setattr(kanban_budget, "evaluate_board_budget", lambda *a, **k: True)
        return {}
    if gate == "max_spawn":
        (busy,) = _make(conn, 1, assignee="beta")
        conn.execute("UPDATE tasks SET status='running', claim_lock='x' WHERE id=?", (busy,))
        conn.commit()
        return {"max_spawn": 1}
    if gate == "max_in_progress":
        (busy,) = _make(conn, 1, assignee="beta")
        conn.execute("UPDATE tasks SET status='running', claim_lock='x' WHERE id=?", (busy,))
        conn.commit()
        monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board=None: 0)
        return {"max_in_progress": 1}
    assert gate == "max_new"
    return {"max_new": 0}


@pytest.mark.parametrize("gate", ["budget_paused", "max_spawn", "max_in_progress",
                                  "memory_critical", "max_new"])
def test_ac18_total_gate_records_every_ready_row(kanban_home, monkeypatch, gate):
    with kb.connect_closing() as conn:
        extra = _total_gate(monkeypatch, conn, gate)
        (pin,) = _make(conn, 1, body="host: ace-ai")
        ports = _make(conn, 2, body="host: any")
        spawned = []
        kw = {"max_spawn": 64, **extra}

        def spawn(task, workspace, *, board=None, placement=None):
            spawned.append(task.id)
            return 4242
        kbd.dispatch_once(conn, spawn_fn=spawn, spawn_paused="test", spawn_limit=0,
                          spillover=_plan(free=2), reconcile_orphans=False, **kw)
        assert spawned == []
        for tid in (pin, *ports):
            (ev,) = _events(conn, tid, "placement_wait")
            assert ev["code"] == f"total:{gate}"
        # A second tick inside the window writes nothing new (dedup).
        kbd.dispatch_once(conn, spawn_fn=spawn, spawn_paused="test", spawn_limit=0,
                          spillover=_plan(free=2), reconcile_orphans=False, **kw)
        assert _count(conn, "placement_wait", [pin, *ports]) == 3


def test_ac6_memory_critical_holds_both_routes(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "critical")
    with kb.connect_closing() as conn:
        (local,) = _make(conn, 1, assignee="beta")
        (pin,) = _make(conn, 1, body="host: ace-ai")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
        assert spawned == []
        assert res.placement_waits == {local: "total:memory_critical", pin: "total:memory_critical"}
        assert _count(conn, "placement_wait") == 2


def test_ac6_memory_elevated_allows_one_total(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "elevated")
    with kb.connect_closing() as conn:
        _make(conn, 3, body="host: ace-ai")
        res, spawned = _tick(conn, spillover=_plan(free=3), spawn_limit=4)
        assert len(spawned) == 1
        assert sorted(set(res.placement_waits.values())) == ["total:ready_budget"]


def test_route_first_false_restores_the_silent_returns(kanban_home, monkeypatch):
    """Kill switch (spec §6): route_first false = no total holds, paused/no-plan
    returns early."""
    _config(monkeypatch, {"placement": {"route_first": False}})
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "critical")
    with kb.connect_closing() as conn:
        _make(conn, 2)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
        assert spawned == [] and res.placement_waits == {} and _count(conn, "placement_wait") == 0


def test_route_first_reads_the_root_config_per_tick(kanban_home):
    """The knob is registered and read through the real loader each tick."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["placement"]["route_first"] is True
    assert kbd._route_first_enabled() is True
    (kanban_home / "config.yaml").write_text("kanban:\n  placement:\n    route_first: false\n")
    assert kbd._route_first_enabled() is False  # cache keyed on the file's stat


# -- AC-7 ---------------------------------------------------------------------

def test_ac7_provider_admission_applies_to_pins(kanban_home, monkeypatch):
    from hermes_cli import kanban_provider_health as kph
    monkeypatch.setattr(kph, "configured_pool_spawns_per_eligible", lambda: 1)
    monkeypatch.setattr(kph, "pool_key", lambda provider: "P")
    monkeypatch.setattr(kph, "pool_budget_eligible", lambda *a, **k: 1)
    monkeypatch.setattr(kb, "_pool_in_flight", lambda conn: {})
    with kb.connect_closing() as conn:
        (local,) = _make(conn, 1, assignee="beta", priority=9)
        (pin,) = _make(conn, 1, body="host: ace-ai")
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=4)
    assert spawned == [(pin, "ace-ai")]


# -- AC-8 ---------------------------------------------------------------------

def test_ac8_every_spawn_writes_one_complete_placement(kanban_home):
    with kb.connect_closing() as conn:
        (local,) = _make(conn, 1, assignee="beta")
        (remote,) = _make(conn, 1, body="host: ace-ai")
        _res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=1)
        assert sorted(spawned, key=str) == sorted([(local, None), (remote, "ace-ai")], key=str)
        (lp,) = _events(conn, local, "placement")
        (rp,) = _events(conn, remote, "placement")
        for ev in (lp, rp):
            assert set(ev) == PLACEMENT_KEYS
            assert set(ev["gate"]) == {"band", "spill_reason", "local_allowance",
                                       "spawn_paused", "total"}
            assert set(ev["gate"]["total"]) == {"budget_paused", "max_spawn", "max_in_progress",
                                                "memory_pressure", "max_new"}
            assert ev["entry"] == "gateway" and ev["considered"][0]["host"] == "ace-ai"
        assert lp["route"] == "local" and rp["route"] == "remote-pin" and rp["host"] == "ace-ai"
        assert lp["rule"] == "profile" and rp["rule"] == "pin"
        run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (remote,)).fetchone()[0]
        assert rp["run_id"] == run


def test_ac8_portable_on_studio_allowance_is_rule_default(kanban_home):
    with kb.connect_closing() as conn:
        (card,) = _make(conn, 1, body="host: any")
        _res, spawned = _tick(conn, spillover=_plan(free=2, band="spilling"), spawn_limit=1)
        assert spawned == [(card, None)]
        (ev,) = _events(conn, card, "placement")
        assert (ev["route"], ev["rule"]) == ("local", "default")


def test_ac8_dry_run_writes_no_placement(kanban_home):
    with kb.connect_closing() as conn:
        _make(conn, 1, assignee="beta")
        _make(conn, 1, body="host: ace-ai")
        res = kbd.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **k: 1, max_spawn=64,
                                spawn_limit=1, spillover=_plan(free=2), reconcile_orphans=False)
        assert len(res.spawned) == 2
        assert _count(conn, "placement") == 0 and _count(conn, "placement_wait") == 0


# -- AC-9 + R-B -----------------------------------------------------------------

def _hold(conn, monkeypatch, tid, reason, *, route=("portable", None)):
    res = kb.DispatchResult()
    res.placement_waits = {tid: reason}
    kbd._record_placement_waits(conn, res, {tid: route}, False)


def test_ac9_dedup_on_code_not_detail(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        for stale in (10, 70, 130):
            _hold(conn, monkeypatch, tid, f"pool_full (stale {stale}s)")
        assert len(_events(conn, tid, "placement_wait")) == 1
        _hold(conn, monkeypatch, tid, "pin_host_unknown (ace-ai: stale 9s)")
        evs = _events(conn, tid, "placement_wait")
        assert [e["code"] for e in evs] == ["pool_full", "pin_host_unknown"]
        assert evs[1]["detail"] == "ace-ai: stale 9s"
        assert evs[1]["reason"] == "pin_host_unknown (ace-ai: stale 9s)"
        conn.execute("UPDATE task_events SET created_at = created_at - 901 "
                     "WHERE task_id=? AND kind='placement_wait'", (tid,))
        conn.commit()
        _hold(conn, monkeypatch, tid, "pin_host_unknown (ace-ai: stale 99s)")
        assert len(_events(conn, tid, "placement_wait")) == 3


def test_rb_host_change_is_a_new_hold(kanban_home, monkeypatch):
    """Same code, different refusing host: a new row (Momus R-B)."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        _hold(conn, monkeypatch, tid, "pool_unavailable (ace-ai: stale 12s)")
        _hold(conn, monkeypatch, tid, "pool_unavailable (ace-ai: stale 40s)")
        _hold(conn, monkeypatch, tid, "pool_unavailable (ci-box: band hot)")
        evs = _events(conn, tid, "placement_wait")
        assert [e["host"] for e in evs] == ["ace-ai", "ci-box"]


def test_rb_flapping_is_capped_at_one_row(kanban_home, monkeypatch):
    """A->B->A->B... inside one window: A, B, one ``flapping``, then nothing."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        for reason in ("pool_full", "local_full") * 5:
            _hold(conn, monkeypatch, tid, reason)
        evs = _events(conn, tid, "placement_wait")
        assert [e["code"] for e in evs] == ["pool_full", "local_full", "flapping"]
        assert evs[2]["detail"] == "local_full|pool_full"


def test_rg_one_read_per_tick_and_no_txn_when_nothing_to_write(kanban_home, monkeypatch):
    """Momus R-G: the writer's read cost does not grow with held cards, and a
    tick that writes nothing opens no write transaction."""
    with kb.connect_closing() as conn:
        ids = _make(conn, 30)
        res = kb.DispatchResult()
        res.placement_waits = {t: "pool_full" for t in ids}
        kbd._record_placement_waits(conn, res, {}, False)
        stmts: list = []
        conn.set_trace_callback(stmts.append)
        kbd._record_placement_waits(conn, res, {}, False)
        conn.set_trace_callback(None)
    assert sum(1 for q in stmts if q.lstrip().upper().startswith("SELECT")) == 1
    assert not any(q.lstrip().upper().startswith(("BEGIN", "INSERT")) for q in stmts)


def test_split_reason_agrees_with_the_view_reader():
    """I-S5: the writer's code and the fleet view's legacy-reason reader agree."""
    from hermes_cli import kanban_placement_view as kpv
    for reason in ("pool_full", "pool_unavailable (ace-ai: stale 12s)",
                   "not_portable:linked(blocks->t_x)", "per_profile_cap (alpha 4/4)",
                   "pin_host_hot (cpu 0.9)", "total:max_in_progress"):
        assert pr.split_reason(reason)[0] == kpv.wait_code({"reason": reason})


# -- R-A ------------------------------------------------------------------------

def test_ra_unassigned_and_per_profile_holds_are_recorded(kanban_home):
    with kb.connect_closing() as conn:
        (orphan,) = _make(conn, 1, assignee=None)
        (busy,) = _make(conn, 1)
        conn.execute("UPDATE tasks SET status='running', claim_lock='x' WHERE id=?", (busy,))
        conn.commit()
        (capped,) = _make(conn, 1)
        _tick(conn, spillover=None, spawn_limit=4, max_in_progress_per_profile={"alpha": 1})
        assert [e["code"] for e in _events(conn, orphan, "placement_wait")] == ["unassigned"]
        (ev,) = _events(conn, capped, "placement_wait")
        assert ev["code"] == "per_profile_cap" and ev["detail"] == "alpha 1/1"


def test_ra_assignment_ends_the_wait_episode(kanban_home, monkeypatch):
    """Prism 57fba9daaa3c: unassigned/per_profile_cap are keyed on the
    assignee, so (re)assigning the card is a reason boundary. The next hold is a
    new episode (since = now), never a continuation or an A->B flap."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1, assignee=None)
        _hold(conn, monkeypatch, tid, "unassigned")
        _hold(conn, monkeypatch, tid, "local_full")
        conn.execute("UPDATE task_events SET created_at = created_at - 600, "
                     "payload = json_set(payload, '$.since', "
                     "json_extract(payload, '$.since') - 600) "
                     "WHERE task_id=? AND kind='placement_wait'", (tid,))
        conn.commit()
        assert kb.assign_task(conn, tid, "alpha")
        _hold(conn, monkeypatch, tid, "unassigned")
        evs = _events(conn, tid, "placement_wait")
        assert [e["code"] for e in evs] == ["unassigned", "local_full", "unassigned"]
        assert abs(evs[-1]["since"] - int(__import__("time").time())) <= 2
        _hold(conn, monkeypatch, tid, "local_full")
        assert "flapping" not in [e["code"] for e in _events(conn, tid, "placement_wait")]


# -- AC-10 ----------------------------------------------------------------------

def test_ac10_one_shot_dry_run_header_and_rows(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_ops as kops
    _config(monkeypatch)
    monkeypatch.setattr(kops, "_one_shot_load_gate", lambda conn, args, cfg, add, defer=False: {
        "spawn_paused": "paused", "spawn_limit": 0, "limit_source": "load gate",
        "override": None, "info": None, "gate": None,
        "lines": ["Load gate: PAUSED load1=99.0 (pause_above=64.0) - local allowance 0: paused"]})
    monkeypatch.setattr(kops, "_one_shot_pool_plan", lambda *a, **k: _plan(free=1))
    with kb.connect_closing() as conn:
        ids = _make(conn, 3, body="host: any")
    rc = kops._cmd_dispatch(argparse.Namespace(dry_run=True, json=False, max=None,
                                               max_running=None, failure_limit=2))
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[0] == kops.CLI_PATH_LINE
    assert out[1].endswith("local allowance 0: paused; remote: 1 slot(s) on ace-ai")
    lines = [ln for ln in out if any(ln.startswith(f"  {t}  ") for t in ids)]
    assert len(lines) == 3
    assert "route=remote-policy host=ace-ai rule=policy  -> would place" in lines[0]
    assert lines[1].endswith("rule=policy  -> holds pool_full")
    assert lines[2].endswith("rule=policy  -> holds pool_full")
    rc = kops._cmd_dispatch(argparse.Namespace(dry_run=True, json=True, max=None,
                                               max_running=None, failure_limit=2))
    payload = json.loads(capsys.readouterr().out)
    assert [r["task_id"] for r in payload["routes"]] == ids
    assert set(payload["routes"][0]) == {"task_id", "route", "rule", "host", "outcome",
                                         "code", "detail"}


def test_ac10_total_gate_rows_print_in_dry_run(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_ops as kops
    _config(monkeypatch)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **k: "critical")
    monkeypatch.setattr(kops, "_one_shot_pool_plan", lambda *a, **k: None)
    with kb.connect_closing() as conn:
        ids = _make(conn, 2)
    kops._cmd_dispatch(argparse.Namespace(dry_run=True, json=False, max=None,
                                          max_running=None, failure_limit=2))
    out = capsys.readouterr().out
    for tid in ids:
        assert f"  {tid}  -> holds total:memory_critical" in out
    with kb.connect_closing() as conn:
        assert _count(conn, "placement_wait") == 0


# -- AC-11 ----------------------------------------------------------------------

def test_ac11_show_prints_the_placement_block(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban as kcli
    _config(monkeypatch)
    with kb.connect_closing() as conn:
        (placed,) = _make(conn, 1, body="host: ace-ai", priority=9)
        (held,) = _make(conn, 1, body="host: any")
        _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
        # The event, not the body, decides: rewrite the body after the fact.
        conn.execute("UPDATE tasks SET body='host: studio' WHERE id=?", (placed,))
        conn.commit()
    kcli._cmd_show(argparse.Namespace(task_id=placed, json=False))
    out = capsys.readouterr().out
    assert f"Placement: {placed} (running)" in out
    assert "  last placement: route=remote-pin rule=pin host=ace-ai " in out
    kcli._cmd_show(argparse.Namespace(task_id=held, json=False))
    out = capsys.readouterr().out
    assert f"Placement: {held} (ready)" in out and "  held: pool_full host=- since " in out


# -- AC-16 ----------------------------------------------------------------------

def test_ac16_review_rows_never_routed_or_held(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kb.connect_closing() as conn:
        (rev,) = _make(conn, 1)
        _status(conn, rev, "review")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        assert spawned == [] and res.placed == []
        assert _events(conn, rev, "placement_wait") == []
        assert rev not in res.placement_waits and rev not in [r["task_id"] for r in res.routes]
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=1)
        assert spawned == [(rev, None)]


def test_ac16_route_row_only_sees_ready_rows():  # noqa: source-proxy structural call-graph check over every _route_row call site (AC-16)
    """Call-graph check: every ``_route_row`` call in ``_dispatch_once_locked``
    iterates ``ready_rows``, which only ever holds ``status = 'ready'``."""
    src = textwrap.dedent(inspect.getsource(kbd._dispatch_once_locked))
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_route_row"]
    assert calls, "_route_row is no longer called from dispatch_once"
    comps = [n for n in ast.walk(tree) if isinstance(n, ast.DictComp)
             and any(c in ast.walk(n) for c in calls)]
    assert len(comps) == len(calls) == 1
    (gen,) = comps[0].generators
    assert isinstance(gen.iter, ast.Name) and gen.iter.id == "ready_rows"
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "ready_rows" for t in n.targets)]
    for a in assigns:
        text = ast.unparse(a.value)
        assert "review" not in text
        assert "status = 'ready'" in text or "ready_rows" in text


# -- AC-17 ----------------------------------------------------------------------

def test_ac17_admitting_pins_only_holds_portable_local_full(kanban_home):
    plan = _plan(free=2, band="admitting")
    plan.pins_only = True
    with kb.connect_closing() as conn:
        (pin,) = _make(conn, 1, body="host: ace-ai", priority=1)
        (port,) = _make(conn, 1, body="host: any")
        res, spawned = _tick(conn, spillover=plan, spawn_limit=0)
        assert spawned == [(pin, "ace-ai")]
        assert res.placement_waits == {port: "local_full"}
        assert [e["code"] for e in _events(conn, port, "placement_wait")] == ["local_full"]


# -- AC-19 ----------------------------------------------------------------------

def test_ac19_every_row_past_the_slot_wall_is_recorded(kanban_home):
    with kb.connect_closing() as conn:
        ids = _make(conn, 6, body="host: any")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        assert len(spawned) == 2
        held = [t for t in ids if t not in {s for s, _ in spawned}]
        assert len(held) == 4 and all(len(_events(conn, t, "placement_wait")) == 1 for t in held)


def test_ac19_ready_budget_one_records_five(kanban_home):
    with kb.connect_closing() as conn:
        ids = _make(conn, 6, body="host: any")
        res, spawned = _tick(conn, spillover=_plan(free=6), spawn_limit=0, spawn_paused="test",
                             max_new=1)
        assert len(spawned) == 1
        codes = [e["code"] for t in ids for e in _events(conn, t, "placement_wait")]
        assert codes == ["total:ready_budget"] * 5


def test_paused_tick_with_no_plan_still_records_holds(kanban_home):
    """spawn_paused + no plan no longer returns early: each row says local_full."""
    with kb.connect_closing() as conn:
        ids = _make(conn, 3)
        res, spawned = _tick(conn, spillover=None, spawn_limit=0, spawn_paused="test")
        assert spawned == []
        assert res.placement_waits == {t: "local_full" for t in ids}


# -- wait-episode boundaries (Prism P2 residue of #1907, t_74441abc) ----------

def test_cleared_wait_ends_the_episode_for_flap_detection(kanban_home, monkeypatch):
    """Prism 5ff168657c9a: the affinity writer's ``{cleared: true}`` ends the
    episode, so A, B, cleared, A is a new A hold, never a ``flapping`` row."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        _hold(conn, monkeypatch, tid, "pool_full")
        _hold(conn, monkeypatch, tid, "local_full")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "placement_wait", {"cleared": True})
        _hold(conn, monkeypatch, tid, "pool_full")
        evs = _events(conn, tid, "placement_wait")
        assert [e.get("code") for e in evs] == ["pool_full", "local_full", None, "pool_full"]
        assert abs(evs[-1]["since"] - int(__import__("time").time())) <= 2


def test_later_gate_clears_the_open_wait(kanban_home, monkeypatch):
    """Prism 8db25fd42347: a row held local_full that later gets a slot but is
    stopped by the respawn guard no longer reads as held local_full."""
    from hermes_cli import kanban_placement_view as kpv
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        _tick(conn, spillover=None, spawn_limit=0, spawn_paused="test")
        assert [e["code"] for e in _events(conn, tid, "placement_wait")] == ["local_full"]
        monkeypatch.setattr(kbd, "check_respawn_guard", lambda *a, **k: "active_pr")
        res, spawned = _tick(conn, spillover=None, spawn_limit=4)
        assert spawned == [] and res.respawn_guarded == [(tid, "active_pr")]
        assert _events(conn, tid, "placement_wait")[-1] == {"cleared": True}
        assert tid not in {h["task_id"] for h in kpv._held(conn)}
        _tick(conn, spillover=None, spawn_limit=4)  # guard still on: no second clear
        assert _count(conn, "placement_wait", [tid]) == 2


def test_linked_hold_changing_edge_is_a_new_row(kanban_home, monkeypatch):
    """Prism 01bdaad47969: ``not_portable:linked(<edge>)`` is held by that
    edge; a different edge inside the window is a new row, not a duplicate."""
    with kb.connect_closing() as conn:
        (tid,) = _make(conn, 1)
        _hold(conn, monkeypatch, tid, "not_portable:linked(blocks->t_a)")
        _hold(conn, monkeypatch, tid, "not_portable:linked(blocks->t_a)")
        _hold(conn, monkeypatch, tid, "not_portable:linked(blocks->t_b)")
        evs = _events(conn, tid, "placement_wait")
        assert [e["detail"] for e in evs] == ["blocks->t_a", "blocks->t_b"]
        assert {e["code"] for e in evs} == {"not_portable:linked"}
