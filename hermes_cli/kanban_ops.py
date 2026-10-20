"""Dispatcher and maintenance verbs for ``hermes kanban``: ``dispatch``,
``daemon`` (deprecated standalone loop), ``tail``/``watch`` event streaming,
``gc`` and ``repair``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_output import _err, _fmt_ts, _print_json


def _kanban_config() -> dict:
    """``config.yaml`` ``kanban:`` section, or ``{}`` when config can't be loaded."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return (cfg.get("kanban", {}) if isinstance(cfg, dict) else {}) or {}
    except Exception:
        return {}


def _poll_loop(interval: float, tick) -> int:
    """Run ``tick()`` every ``interval`` seconds (floor 0.1) until Ctrl-C."""
    try:
        while True:
            tick()
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")

    def tick():
        nonlocal last_id
        with kbc.connect_closing() as conn:
            events = kb.list_events(conn, args.task_id)
        for e in events:
            if e.id > last_id:
                pl = f" {e.payload}" if e.payload else ""
                print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                last_id = e.id

    return _poll_loop(args.interval, tick)


def _one_shot_pool_plan(gate, config, builders=None):
    """The gateway's ONE pool plan for a one-shot dispatch (KWLB), or None.

    None (no gate, refused pool, nothing to plan) keeps remote pins waiting
    ``pool_unavailable``; they never run locally.
    """
    if gate is None:
        return None
    try:
        from gateway.kanban_gate_tick import live_boards, running_split, standalone_builder

        kcfg = (config or {}).get("kanban") if isinstance(config, dict) else None
        builder = standalone_builder(gate, lambda: kcfg if isinstance(kcfg, dict) else {})
        if builders is not None:
            builders.append(builder)
        boards = live_boards()
        _local, remote_by_host = running_split(builder._ledger(boards))
        # A one-shot plan skips build(); record_placements() keys the kanban
        # ledger off _remote_by_host, so seed it here or the running remote
        # workers' reservations and ramp stamps are dropped until the next
        # gateway tick (Prism 2febc7f296e1 on #1764).
        builder._remote_by_host = dict(remote_by_host)
        return builder.plan_pool(boards, remote_by_host)
    except Exception as exc:
        print(f"warning: kanban pool plan failed, remote pins wait: {exc}", file=sys.stderr)
        return None


def _one_shot_load_gate(conn, args, config, additive, defer=False):
    """Apply ``kanban.dispatch_load_gate`` to one ``kanban dispatch`` call.

    Same gate object, allowance math and pause text as the gateway loop and
    ``run_daemon``: allowance = ceil((pause_above - load1 - pending_ramp) /
    worker_load_cost) capped at ``max_spawn_per_tick``, load5 floor on
    resume. A one-shot process has no in-memory ramp, so this board's run
    starts inside ``ramp_seconds`` are booked as the pending ramp; otherwise
    back-to-back calls would each see an empty ramp and admit a full burst.

    ``additive`` (``--max``) intersects the allowance; it never raises it.
    ``--ignore-load-gate`` skips the gate but prints the load and returns an
    ``override`` payload the caller writes as an event on every spawn.

    Returns ``{spawn_paused, spawn_limit, limit_source, override, info}``.
    ``defer`` (route_first, t_7133e03d) collects the gate line in
    ``out["lines"]`` so the caller prints it after the pool plan exists.
    """
    from hermes_cli import kanban_load_gate as _klg

    out = {
        "spawn_paused": None,
        "spawn_limit": additive,
        "limit_source": "--max" if additive is not None else None,
        "override": None,
        "info": None,
        "lines": [],
    }
    stream = sys.stderr if getattr(args, "json", False) else sys.stdout

    def emit(text):
        if defer:
            out["lines"].append(text)
        else:
            print(text, file=stream, flush=True)
    try:
        gate = _klg.gate_from_config(config if isinstance(config, dict) else None)
    except Exception:
        return out
    out["gate"] = gate
    load1, load5 = _klg.sample_loadavg()
    if gate.enabled and gate.admission == "utilization":
        # t_da6f84f7: the SAME predicate as the gateway tick (admission.admit over cpu_busy / memory / procs,
        # measured-estimate leases, the host's kanban ceiling); load1 is printed, never read
        return _cli_utilization_gate(conn, args, gate, out, emit, load1)
    if not gate.enabled or load1 is None:
        out["info"] = {"state": "disabled" if not gate.enabled else "unavailable"}
        return out
    try:
        now_wall, now_mono = time.time(), time.monotonic()
        rows = conn.execute(
            "SELECT started_at FROM task_runs WHERE started_at >= ?",
            (int(now_wall - gate.ramp_seconds),),
        ).fetchall()
        for row in rows:
            gate.record_spawns(1, now=now_mono - max(0.0, now_wall - float(row[0])))
    except Exception:
        pass
    # CPU corroboration of a load1 pause (t_bf26e8f1): a one-shot process
    # has no previous sample, so take a 0.25 s one.
    cpu_busy, _ = _klg.sample_cpu_busy(None, block=0.25)
    procs, proc_limit = _klg.sample_user_procs()
    ecore = None
    if gate.enabled and gate.ecore_gate != "off":   # the same E-core leg as admit_now (Prism 3b7cbf596c2e)
        ecore, _ = _klg.sample_ecore_busy(_klg.ecore_cpu_indices(), None, block=0.25)
    allowance, reason = gate.admit(
        load1, load5=load5, cpu_busy=cpu_busy,
        running=_klg.count_running_workers(),
        procs=procs, proc_limit=proc_limit, ecore_busy=ecore,
    )
    info = gate.snapshot()
    info.pop("boards", None)
    out["info"] = info
    if getattr(args, "ignore_load_gate", False):
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except Exception:
            profile = "unknown"
        emit(
            f"Load gate: load1={load1:.1f} pause_above={gate.pause_above:.1f} "
            f"OVERRIDE by {profile} (--ignore-load-gate; gate state={gate.state}, "
            f"would have allowed {allowance}; load {load1:.1f} tasks / {_klg._denom(gate.ncpu)})"
        )
        out["override"] = {
            "source": "cli dispatch --ignore-load-gate",
            "profile": profile,
            "load1": round(load1, 2),
            "load5": None if load5 is None else round(load5, 2),
            "pause_above": gate.pause_above,
            "gate_state": gate.state,
            "gate_allowance": allowance,
        }
        info["override_by"] = profile
        return out
    if reason:
        label = {"paused": "PAUSED", "proc_paused": "PROC-PAUSED"}.get(
            gate.state, "SATURATED")
        emit(
            f"Load gate: {label} load1={load1:.1f} (pause_above={gate.pause_above:.1f}) - "
            + (f"local allowance 0: {reason}" if defer else f"spawning 0 this call: {reason}")
            + f" [load {load1:.1f} tasks / {_klg._denom(gate.ncpu)}]"
        )
        out["spawn_paused"] = reason
        out["spawn_limit"] = 0
        out["limit_source"] = "load gate"
        return out
    emit(
        f"Load gate: admitting load1={load1:.1f} pending_ramp={gate.pending_ramp:.1f} "
        f"allowance={allowance} (pause_above={gate.pause_above:.1f}, "
        f"max_spawn_per_tick={gate.max_spawn_per_tick}; load {load1:.1f} tasks / {_klg._denom(gate.ncpu)})"
    )
    if allowance is not None and (additive is None or allowance < additive):
        out["spawn_limit"] = allowance
        out["limit_source"] = "load gate allowance"
    return out


def _recent_local_starts(since: float) -> list:
    """``started_at`` of every run started at/after ``since`` on ANY live board of
    this host that did NOT run on a pool host (no worker_placed event; Prism
    9f811a3f59ff). Cross-board, like the running count: another board's spawn
    is the same load on this host (Prism d88c700fe427). Each DB file once; an
    unreadable board contributes nothing (its running count reads unknown)."""
    from gateway.kanban_gate_tick import live_boards
    from hermes_cli import kanban_worker_pool as _kwp

    out, seen = [], set()
    for meta in kb.enumerating_each(live_boards()):
        slug = (meta.get("slug") if isinstance(meta, dict) else None) or kb.DEFAULT_BOARD
        try:
            path = str(kb.kanban_db_path(slug).expanduser().resolve())
            if path in seen:
                continue
            seen.add(path)
            with kbc.connect_closing(board=slug) as bconn:
                out.extend(float(r[0]) for r in bconn.execute(
                    "SELECT r.started_at FROM task_runs r WHERE r.started_at >= ? AND NOT EXISTS ("
                    "SELECT 1 FROM task_events e WHERE e.run_id = r.id AND e.kind = ?)",
                    (int(since), _kwp.PLACED_EVENT)).fetchall())
        except Exception:
            continue
    return out


def _cli_utilization_gate(conn, args, gate, out, emit, load1):
    """``kanban.admission: utilization`` for the one-shot CLI: the gateway's
    utilization admit() with a 0.25 s cpu_busy sample, every board's LOCAL run
    starts inside admission.DEFAULT_SETTLE_S booked as leases, and the LOCAL
    running count from the gateway's all-board snapshot (count_running_by_placement
    + running_split: every board's remote placements out of every board's total)
    under the host's kanban ceiling."""
    from gateway.kanban_gate_tick import live_boards, running_split
    from hermes_cli import admission as _ad
    from hermes_cli import kanban_load_gate as _klg

    now_wall, now_mono = time.time(), time.monotonic()
    running = None
    try:
        for started in _recent_local_starts(now_wall - _ad.DEFAULT_SETTLE_S):
            gate.record_spawns(1, now=now_mono - max(0.0, now_wall - started))
        running, _remote = running_split(kb.count_running_by_placement(live_boards()))
    except Exception:
        running = None
    cpu_busy, _ = _klg.sample_cpu_busy(None, block=0.25)
    procs, proc_limit = _klg.sample_user_procs()
    allowance, reason = gate.admit(
        load1 if load1 is not None else 0.0, cpu_busy=cpu_busy, running=running,
        procs=procs, proc_limit=proc_limit, now=now_mono,
    )
    info = gate.snapshot()
    info.pop("boards", None)
    out["info"] = info
    l1 = "?" if load1 is None else f"{load1:.1f}"
    busy = "?" if cpu_busy is None else f"{cpu_busy:.2f}"
    if getattr(args, "ignore_load_gate", False):
        out["override"] = {"source": "cli dispatch --ignore-load-gate", "admission": "utilization",
                           "cpu_busy": cpu_busy, "gate_state": gate.state, "gate_allowance": allowance}
        emit(f"Load gate: utilization OVERRIDE (--ignore-load-gate; would have allowed {allowance}; cpu_busy={busy})")
        return out
    if reason:
        emit(f"Load gate: utilization SATURATED cpu_busy={busy} (load {l1} not read) - spawning 0 this call: {reason}")
        out["spawn_paused"] = reason
        out["spawn_limit"] = 0
        out["limit_source"] = "load gate"
        return out
    emit(f"Load gate: utilization admitting cpu_busy={busy} allowance={allowance} "
         f"(need {gate.util.get('need')}, ceiling {gate.util.get('ceiling')}; load {l1} not read)")
    additive = out["spawn_limit"]
    if allowance is not None and (additive is None or allowance < additive):
        out["spawn_limit"] = allowance
        out["limit_source"] = "load gate allowance"
    return out


CLI_PATH_LINE = "path: cli (one-shot plan; the gateway's shared plan is not consulted)"


def _remote_part(gate_obj, plan) -> str:
    """``remote: N slot(s) on <hosts>`` or ``remote: no plan (<reason>)`` (spec §3.1)."""
    if plan is not None:
        free = [n for n, k in plan.slots.items() if k > 0]
        return (f"remote: {plan.budget} slot(s) on {', '.join(free)}" if free
                else f"remote: 0 slot(s) ({plan.full_reason()})")
    pool = getattr(gate_obj, "pool", None)
    why = pool.get("reason") if isinstance(pool, dict) else None
    return f"remote: no plan ({why or 'pool not planned'})"


def _print_gate_lines(gate, plan, stream) -> None:
    """The route-first header: the ``path:`` line, then the gate line(s)
    with the remote half, printed after the pool plan exists."""
    print(CLI_PATH_LINE, file=stream, flush=True)
    lines = list(gate.get("lines") or [])
    remote = _remote_part(gate.get("gate"), plan)
    if lines:
        lines[-1] = f"{lines[-1]}; {remote}"
    else:
        lines = [f"Load gate: off; {remote}"]
    for line in lines:
        print(line, file=stream, flush=True)


def _route_line(entry: dict, dry_run: bool, reason=None) -> str:
    """One dry-run/tick line per ready row (spec §4.3); ``reason`` is the
    held row's reason string as written (code + detail)."""
    outcome = entry.get("outcome")
    if outcome in ("spawned", "would_place"):
        verdict = "would place" if dry_run else "spawned"
    elif outcome == "held":
        verdict = f"holds {reason or entry.get('code')}"
    else:
        verdict = "skipped (see below)"
    if entry.get("route") is None:
        return f"  {entry['task_id']}  -> {verdict}"
    tail = f" take_refusal={entry['take_refusal']}" if entry.get("take_refusal") else ""
    return (f"  {entry['task_id']}  route={entry['route']} host={entry.get('host') or '-'} "
            f"rule={entry.get('rule') or '-'}  -> {verdict}{tail}")


def _dispatch_limit_notes(res, gate, max_spawn_source):
    """Name which limit stopped a one-shot dispatch (``--max`` is additive)."""
    notes = []
    capped = getattr(res, "spawn_capped", None) or ""
    if capped.startswith("max_spawn="):
        notes.append(
            f"running ceiling {max_spawn_source} fired ({capped}); "
            f"--max N is additive and is not this ceiling"
        )
    limit = gate.get("spawn_limit")
    spawned = len(getattr(res, "spawned", None) or [])
    if limit is not None and limit > 0 and spawned >= limit:
        src = gate.get("limit_source")
        if src == "--max":
            notes.append(f"--max={limit} reached: {spawned} spawned this call (additive)")
        elif src:
            notes.append(f"{src}={limit} reached: {spawned} spawned this call")
    return notes


def _cmd_dispatch(args: argparse.Namespace) -> int:
    # Honour kanban.default_assignee, kanban.max_in_progress,
    # kanban.max_in_progress_per_profile and kanban.max_spawn with the same
    # semantics as the gateway dispatch path.
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _kanban_cfg = _cfg.get("kanban", {}) if isinstance(_cfg, dict) else {}
        default_assignee = (_kanban_cfg.get("default_assignee") or "").strip() or None
        _raw_per_profile = _kanban_cfg.get("max_in_progress_per_profile")
        if isinstance(_raw_per_profile, dict):
            # {default: N, <profile>: M} — resolved per assignee in the
            # dispatcher (kanban_db.resolve_per_profile_cap).
            max_in_progress_per_profile = _raw_per_profile
        else:
            max_in_progress_per_profile = kbd._positive_int(_raw_per_profile, None)
        # Memory-derived default when unset — same fallback the gateway applies.
        max_in_progress = kbd.resolve_max_in_progress(
            kbd._positive_int(_kanban_cfg.get("max_in_progress"), None)
        )
        # --max-running N (else kanban.max_spawn) is the board's live
        # running-count CEILING. --max N is ADDITIVE ("spawn up to N more this
        # call") and never a ceiling: on 2026-09-29 `--max 6` was read as
        # "stop at 6 running" and spawned nothing with 13 running (t_689b81b7).
        cli_max_running = getattr(args, "max_running", None)
        max_spawn = (
            cli_max_running if cli_max_running is not None
            else kbd._positive_int(_kanban_cfg.get("max_spawn"), None)
        )
        max_spawn_source = "--max-running" if cli_max_running is not None else "kanban.max_spawn"
    except Exception:
        _cfg = None
        default_assignee = max_in_progress_per_profile = max_in_progress = None
        max_spawn = getattr(args, "max_running", None)
        max_spawn_source = "--max-running"
    additive = getattr(args, "max", None)
    with kbc.connect_closing() as conn:
        # Host load gate (kanban.dispatch_load_gate): the SAME gate the
        # gateway loop and `kanban daemon` apply. The one-shot verb used to
        # skip it; `dispatch --max 128` at load1 66 (pause_above 64) spawned
        # 33 workers and took the Studio to load1 243 (t_689b81b7).
        route_first = kbd._route_first_enabled()
        gate = _one_shot_load_gate(conn, args, _cfg, additive, defer=route_first)
        _pool_builders: list = []
        # Route first (spec §3.1): the pool plan exists before the gate line.
        plan = _one_shot_pool_plan(gate.get("gate"), _cfg, _pool_builders)
        if route_first:
            _print_gate_lines(gate, plan,
                              sys.stderr if getattr(args, "json", False) else sys.stdout)
        res = kbd.dispatch_once(
            conn,
            dry_run=args.dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            spawn_paused=gate["spawn_paused"],
            spawn_limit=gate["spawn_limit"],
            spillover=plan,
            # --max is additive across local AND remote spawns (total budget).
            max_new=additive,
            entry="cli",
        )
        if not args.dry_run:
            for _b in _pool_builders:  # placement 1b: publish kanban's reservations
                _b.record_placements(list(res.placed or []))
        if gate["override"] is not None and not args.dry_run and res.spawned:
            # Attribute any load episode to the override on every card it
            # spawned (the overview reads task_events).
            try:
                with kbc.write_txn(conn):
                    for _tid, _who, _ws in res.spawned:
                        kb._append_event(conn, _tid, "load_gate_override", dict(gate["override"]))
            except Exception as exc:
                print(f"warning: could not record load_gate_override events: {exc}", file=sys.stderr)
        limit_notes = _dispatch_limit_notes(res, gate, max_spawn_source)
        # Spawned cards nobody is watching will finish silently — surface
        # that on every dispatch run instead of relying on the operator
        # remembering to notify-subscribe. Best-effort: notification
        # bookkeeping must never fail the dispatch.
        spawned_unwatched: list[str] = []
        for _tid, _who, _ws in res.spawned:
            try:
                if not kbn.list_notify_subs(conn, _tid):
                    spawned_unwatched.append(_tid)
            except Exception:
                pass
    from hermes_cli.model_policy import route_kind
    # Review-awaiting-human detector + stranded-by-triage banner live in the
    # facade (they own the Discord alert path); late import, sibling -> facade.
    from hermes_cli.kanban import (
        _collect_review_awaiting_human, _fmt_respawn_guard_detail,
        _print_review_awaiting_human, _print_stranded_by_triage,
    )

    if getattr(args, "json", False):
        # --dry-run is the documented SAFE probe: report the detector but
        # only arm/send alerts on a real tick (same rule as the text path).
        _print_json({
            "review_awaiting_human": _collect_review_awaiting_human(alert=not args.dry_run),
            **{k: getattr(res, k)
               for k in ("reclaimed", "crashed", "timed_out", "stale", "auto_blocked", "promoted",
                         "reaped_terminal_workers")},
            "skipped_locked": res.skipped_locked,
            "budget_paused": res.budget_paused,
            "lock_holder": res.lock_holder,
            "spawned": [
                {
                    "task_id": tid, "assignee": who, "workspace": ws,
                    "route": res.spawn_routes.get(tid),
                    "route_kind": route_kind(res.spawn_routes.get(tid)),
                    "route_source": res.spawn_route_sources.get(tid),
                }
                for (tid, who, ws) in res.spawned
            ],
            "expired_lane_models": [
                {
                    "lane": lane, "route": route,
                    "successor": (res.expired_lane_successors or {}).get(lane, "profile default"),
                }
                for (lane, route) in res.expired_lane_models or []
            ],
            "spawned_unwatched": spawned_unwatched,
            "placement_affinity": getattr(res, "placement_affinity", None) or {},
            "placement_waits": getattr(res, "placement_waits", None) or {},
            "placement_caps": getattr(res, "placement_caps", None) or {},
            "skipped_unassigned": res.skipped_unassigned,
            "flagship_refused": res.flagship_refused,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            # Count only, like the text line: operator-only cards are never
            # listed by dispatch output.
            "skipped_no_worker_count": len(res.skipped_no_worker or []),
            "stranded_by_triage": [
                {"task_id": child, "parent_id": parent}
                for (child, parent) in res.stranded_by_triage
            ],
            "woken_scheduled": list(res.woken_scheduled or []),
            "unwoken_scheduled": [
                {"task_id": tid, "parked_seconds": age}
                for (tid, age) in res.unwoken_scheduled or []
            ],
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "auto_assigned_default": res.auto_assigned_default,
            "respawn_guarded": [
                {"task_id": tid, "reason": reason, **res.respawn_guard_details.get(tid, {})}
                for (tid, reason) in res.respawn_guarded
            ],
            "rate_limited": res.rate_limited,
            "collision_warnings": [
                {"task_id": task_id, "existing_task_id": existing_task_id, "paths": paths}
                for task_id, existing_task_id, paths in res.collision_warnings
            ],
            "collision_scope_unknown": res.collision_scope_unknown,
            "collision_scope_unreported": [
                {"task_id": task_id, "unchecked_task_ids": unchecked_ids}
                for task_id, unchecked_ids in res.collision_scope_unreported
            ],
            "collision_check_failed": res.collision_check_failed,
            "gate_auto_resolved": res.gate_auto_resolved,
            "gate_closed_unmerged": res.gate_closed_unmerged,
            "spawn_paused": res.spawn_paused,
            "spawn_capped": res.spawn_capped,
            "load_gate": gate["info"],
            "limit_notes": limit_notes,
            "memory_pressure": res.memory_pressure,
            "placement_waits": dict(res.placement_waits or {}),
            "routes": list(res.routes or []),
        }, ascii=True)
        return 0
    if res.skipped_locked:
        print(kb.format_dispatch_lock_skip(res.lock_holder))
    if res.budget_paused:
        # Loud: otherwise a budget-paused board prints "Spawned: 0" and is
        # byte-identical to an idle board.
        print(
            "BUDGET PAUSED — this board's rolling-window worker spend has "
            "reached kanban.budget.usd_per_24h; no new workers spawned. "
            "Details: hermes kanban budget"
        )
    print(f"Reclaimed:    {res.reclaimed}")
    if res.reaped_terminal_workers:
        print(f"Reaped workers of finished tasks: {', '.join(res.reaped_terminal_workers)}")
    for label, items in (
        ("Crashed:     ", res.crashed),
        ("Flagship refused:", res.flagship_refused),
        ("Timed out:   ", res.timed_out),
        ("Stale:       ", res.stale),
        ("Auto-blocked:", res.auto_blocked),
    ):
        print(f"{label} {len(items)}")
        if items:
            print(f"  {', '.join(items)}")
    print(f"Promoted:     {res.promoted}")
    if res.gate_auto_resolved:
        print(f"Gate auto-resolved (referenced PR(s) merged): {', '.join(res.gate_auto_resolved)}")
    if res.gate_closed_unmerged:
        print(
            "WARNING — gate PR closed WITHOUT merging; card left blocked for "
            f"a human: {', '.join(res.gate_closed_unmerged)}"
        )
    print(f"Spawned:      {len(res.spawned)}")
    # Say WHY nothing (or less than asked) spawned: a bare "Spawned: 0" with
    # dispatchable cards on the board is indistinguishable from an idle board
    # (t_f78d1938: manual `dispatch --max 3` spawned 0 of 2 with no reason).
    if res.spawn_capped:
        print(f"  capped: {res.spawn_capped}")
    if res.spawn_paused:
        print(f"  paused: {res.spawn_paused}")
    for _note in limit_notes:
        print(f"  limit: {_note}")
    if res.memory_pressure:
        print(
            f"  memory pressure {res.memory_pressure}: "
            + ("no new workers this tick" if res.memory_pressure == "critical"
               else "at most 1 new worker this tick")
        )
    prios: dict = {}
    if res.spawned:
        try:
            with kbc.connect_closing() as _pc:
                ids = [t for t, _w, _s in res.spawned]
                prios = {
                    r["id"]: r["priority"] for r in _pc.execute(
                        f"SELECT id, priority FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids,
                    )
                }
        except Exception:
            prios = {}
    tag = " (dry)" if args.dry_run else ""
    for tid, who, ws in res.spawned:
        prio_part = f" prio={prios[tid]}" if prios.get(tid) else ""
        route = res.spawn_routes.get(tid, "unknown/unknown")
        source = res.spawn_route_sources.get(tid)
        # source= names WHICH layer chose the route (card / lane window /
        # profile); without it a lane override looks like a silent profile change.
        source_part = f" source={source}" if source else ""
        print(
            f"  - {tid}  ->  {who}  @ {ws or '-'}  route={route}{source_part} "
            f"kind={route_kind(route)}{prio_part}{tag}"
        )
    successors = res.expired_lane_successors or {}
    for lane, route in res.expired_lane_models or []:
        print(f"lane-model expired ({lane}: {route}) -> {successors.get(lane, 'profile default')}")
    if res.collision_warnings:
        print("WARNING — pre-dispatch file collision(s); dispatch continued:")
        for task_id, existing_task_id, paths in res.collision_warnings:
            print(f"  - {task_id} overlaps {existing_task_id}: {', '.join(paths)}")
    if res.collision_scope_unknown:
        print(
            "WARNING — collision scope unknown (no explicit file paths): "
            f"{', '.join(res.collision_scope_unknown)}"
        )
    for task_id, unchecked_ids in res.collision_scope_unreported:
        print(
            f"WARNING — collision check partial for {task_id}; no reported "
            f"changed_files from: {', '.join(unchecked_ids)}"
        )
    if res.collision_check_failed:
        print(
            "WARNING — collision check failed open; dispatch continued for: "
            f"{', '.join(res.collision_check_failed)}"
        )
    if spawned_unwatched:
        print(
            f"{len(spawned_unwatched)} spawned card(s) have no notify "
            "subscription — finishes will be silent "
            "(kanban.cli_auto_subscribe or notify-subscribe)"
        )
    if res.auto_assigned_default:
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    for tid, who, current in res.skipped_per_profile_capped:
        print(f"Deferred ({who} at per-profile cap, {current} running): {tid}")
    for tid, aff in (getattr(res, "placement_affinity", None) or {}).items():
        wait = (res.placement_waits or {}).get(tid)
        why = aff.get("matched_by")
        if aff.get("term"):  # capability pin (t_8c474c2e): rule + source + term
            why = f"{aff.get('rule')}, {why} {aff.get('term')!r}, {aff.get('strength')}"
        print(
            f"Placement affinity: {tid} -> {aff.get('host')} ({why})"
            + (f"; waits {wait}, never spills" if wait else "")
        )
    _caps = getattr(res, "placement_caps", None) or {}
    if _caps.get("_map_error"):
        print(f"Placement capabilities: map invalid, every capability warn this tick: {_caps['_map_error']}")
    for tid, seen in _caps.items():
        if tid == "_map_error":
            continue
        if seen.get("warn"):
            w = seen["warn"]
            print(f"Placement capability (warn, no route change): {tid} would need {w.get('rule')} "
                  f"({w.get('matched_by')} {w.get('term')!r})")
        if seen.get("unknown"):
            print(f"Placement capability: {tid} names unknown capability {seen['unknown']!r}; stays local")
        if seen.get("pin_lacks"):
            print(f"Placement capability: {tid} {seen['pin_lacks']}; parked needs_input")
    for tid, line in (getattr(res, "access_contradictions", None) or {}).items():
        print(f"Access-bound: {tid} {line}; "
              + ("would park needs_input" if args.dry_run else "parked needs_input"))
    for tid, reason in res.respawn_guarded:
        print(
            f"Deferred (respawn guard {reason}): {tid}"
            + _fmt_respawn_guard_detail(res.respawn_guard_details.get(tid))
        )
    # Why a ready card was not spawned anywhere this tick (pool routing); a
    # held card used to be invisible without reading the source (t_ae87edff).
    if route_first and res.routes:
        print(f"Routes ({len(res.routes)}):")
        for entry in res.routes:
            print(_route_line(entry, args.dry_run,
                              (res.placement_waits or {}).get(entry["task_id"])))
    else:
        for tid, reason in (res.placement_waits or {}).items():
            print(f"Held ({tid}): {reason}")
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — HUMAN review required): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    if res.skipped_no_worker:
        # Count only: an operator-only card never appears in the spawn listing.
        print(f"Skipped (no-worker, operator-only): {len(res.skipped_no_worker)} card(s)")
    if res.rate_limited:
        print(f"Rate-limited (released to ready, no failure counted): {', '.join(res.rate_limited)}")
    # --dry-run must not arm alerts (durable review_stale_alerted event) or
    # send (real Discord message); the detector still PRINTS.
    _print_review_awaiting_human(alert=not args.dry_run)
    _print_stranded_by_triage(res.stranded_by_triage)
    if res.woken_scheduled:
        print(f"Woken (timed schedule elapsed): {', '.join(res.woken_scheduled)}")
    unwoken = kb.format_unwoken_scheduled(res.unwoken_scheduled)
    if unwoken:
        print(unwoken)
    return 0


_DAEMON_DEPRECATED = (
    "hermes kanban daemon: DEPRECATED — the dispatcher now runs\ninside the gateway. To use "
    "kanban:\n\n    hermes gateway start       # starts the gateway + embedded dispatcher\n\nReady "
    "tasks will be picked up on the next dispatcher tick\n(default: every 60 seconds). Configure "
    "via config.yaml:\n\n    kanban:\n      dispatch_in_gateway: true      # default\n      "
    "dispatch_interval_seconds: 60\n      failure_limit: 2              # consecutive non-success "
    "attempts before auto-block\n\nRunning both the gateway AND this standalone daemon will\nrace "
    "for claims. If you truly need the old standalone\ndaemon (no gateway available), rerun with "
    "--force."
)


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway. Kept so old
    scripts/systemd units get a clear migration message; ``--force`` (hidden
    from --help) keeps the standalone loop for hosts that truly cannot run the
    gateway. The default path exits 2 so nobody accidentally runs two
    dispatchers against the same kanban.db."""
    if not getattr(args, "force", False):
        return _err(_DAEMON_DEPRECATED, 2)

    # Init before printing "started" so the DB path is right and init errors
    # surface immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force (interval={args.interval}s, "
        f"pid={os.getpid()}). Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but spawns
    # nothing (broken profile, PATH drift, missing venv, credential loss) —
    # the per-task breaker auto-blocks quietly, so the operator needs a signal.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health_state = {"bad_ticks": 0, "last_warn_at": 0}

    def _ready_queue_nonempty() -> bool:
        """Is there a ready+assigned+unclaimed task the dispatcher would spawn for?
        Control-plane lanes pulled via ``claim_task`` are correctly idle, not stuck."""
        try:
            with kbc.connect_closing() as conn:
                return kbd.has_spawnable_ready(conn)
        except Exception:
            return False

    def _on_tick(res):
        ready_pending = bool(res.skipped_unassigned) or _ready_queue_nonempty()
        if ready_pending and not res.spawned:
            health_state["bad_ticks"] += 1
        else:
            health_state["bad_ticks"] = 0
        # Warn once per HEALTH_WINDOW bad ticks, at most every 5 minutes.
        if health_state["bad_ticks"] >= HEALTH_WINDOW:
            now = int(time.time())
            if now - health_state["last_warn_at"] >= 300:
                held = kbd.describe_suppression([res])
                held = f" Last tick held back: {held}." if held else ""
                print(
                    f"[{_fmt_ts(now)}] WARN dispatcher stuck: ready queue non-empty for "
                    f"{health_state['bad_ticks']} consecutive ticks but 0 workers spawned "
                    f"successfully.{held} Check profile health (venv, PATH, credentials) and `hermes "
                    f"kanban list --status ready` / `hermes kanban list --status blocked` for "
                    f"recent spawn_failed tasks.",
                    file=sys.stderr, flush=True,
                )
                health_state["last_warn_at"] = now
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
            or res.parent_satisfied_sticky
        )
        if did_work:
            sticky_ids = sorted(res.parent_satisfied_sticky)
            sticky_summary = (
                f"parents_done_sticky={len(sticky_ids)}"
                + (f" ({', '.join(sticky_ids)})" if sticky_ids else "")
            )
            print(
                f"[{_fmt_ts(int(time.time()))}] reclaimed={res.reclaimed} "
                f"crashed={len(res.crashed)} timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)} {sticky_summary}",
                flush=True,
            )

    try:
        # Same host load gate as the gateway loop (kanban.dispatch_load_gate).
        from hermes_cli.kanban_load_gate import gate_from_config

        kbd.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kbd.DEFAULT_FAILURE_LIMIT),
            on_tick=_on_tick,
            load_gate=gate_from_config(),
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else None
    print(f"Watching kanban events (initial board '{kb.get_current_board()}'). Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kbc.connect_closing() as conn:
        cursor = int(conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"])

    def tick():
        nonlocal cursor
        with kbc.connect_closing() as conn:
            rows = conn.execute(
                "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at,        t.assignee, "
                "t.tenant FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id WHERE e.id > ? "
                "ORDER BY e.id ASC LIMIT 200",
                (cursor,),
            ).fetchall()
        for r in rows:
            cursor = max(cursor, int(r["id"]))
            if (kinds and r["kind"] not in kinds) or (args.assignee and r["assignee"] != args.assignee) \
                    or (args.tenant and r["tenant"] != args.tenant):
                continue
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            pl = f" {payload}" if payload else ""
            print(
                f"[{_fmt_ts(r['created_at'])}] {r['task_id']:10s} "
                f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                flush=True,
            )

    return _poll_loop(args.interval, tick)


def _gc_remove_workspaces(rows, scratch_root: Path) -> int:
    removed_ws = 0
    for row in rows:
        if row['workspace_kind'] in ('scratch', 'worktree') and row['workspace_path']:
            from hermes_cli.kanban_continuation import remote_cleanup_host
            with kbc.connect_closing() as conn:
                host = remote_cleanup_host(conn, row['id'])
                if host and kb.safe_remove_workspace_dir(
                    Path(row['workspace_path']), task_id=row['id'], reason='gc_archived',
                    conn=conn, remote_host=host,
                ):
                    removed_ws += 1
        if row["workspace_kind"] == "worktree":
            # Backstop for worktrees that escaped the completion/archive hook.
            # Same safety predicate: only clean, fully-pushed worktrees go;
            # liveness + audit run inside the worktree lane too (t_63fb42f9).
            wt_path = row["workspace_path"]
            if wt_path and Path(wt_path).is_dir():
                with kbc.connect_closing() as conn:
                    kbw._cleanup_worktree_workspace(
                        row["id"], wt_path, row["branch_name"], conn=conn, reason="gc_archived",
                    )
                if not Path(wt_path).is_dir():
                    removed_ws += 1
            continue
        if row["workspace_kind"] != "scratch":
            continue
        path = Path(row["workspace_path"] or (scratch_root / row["id"]))
        # A bare ``relative_to`` SUCCEEDS on an equal path, so a row whose
        # workspace_path was the workspaces ROOT passed containment and rmtree'd
        # every live card's scratch dir (2026-09-20). safe_remove_workspace_dir
        # requires STRICT descendancy, refuses any card with a live run, audits
        # both outcomes and removes through kanban_survivor.remove_workspace_dir.
        with kbc.connect_closing() as conn:
            if kb.safe_remove_workspace_dir(path, task_id=row["id"], reason="gc_archived", conn=conn):
                removed_ws += 1
    return removed_ws


def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove archived (and aged done) tasks' workspaces, old events, and old worker logs."""
    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    if event_days < 0 or log_days < 0:
        return _err("kanban gc: retention days must be >= 0 (0 disables that sweep)", 2)
    # Not placement: a stale worker-shell pin must not break gc (config wins).
    scratch_root = kb.workspaces_root(stale_pin_ok=True)
    dry_run = bool(getattr(args, "dry_run", False))
    done_days = getattr(args, "done_retention_days", 3)
    # DONE cards are swept too once finished for done_days: completion cleanup
    # is best-effort and refusals were never retried, so a done card's
    # workspace otherwise lives until someone archives it. Every removal still
    # goes through the same survivor/liveness/audit gates.
    done_cutoff = (
        int(time.time()) - done_days * 24 * 3600 if done_days is not None and done_days >= 0
        else None
    )
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks "
            "WHERE status = 'archived' OR (status = 'done' AND ? IS NOT NULL "
            "AND COALESCE(completed_at, started_at, created_at) <= ?)",
            (done_cutoff, done_cutoff),
        ).fetchall()
    if dry_run:
        would = []
        for row in rows:
            if row["workspace_kind"] not in ("scratch", "worktree"):
                continue
            p = row["workspace_path"] or (
                str(scratch_root / row["id"]) if row["workspace_kind"] == "scratch" else None
            )
            if p and Path(p).is_dir():
                would.append((row["id"], p))
        for tid, p in would:
            print(f"would-try {tid} {p}")
        print(f"GC dry-run: {len(would)} workspace candidate(s); nothing removed "
              "(survivor/liveness gates are evaluated only on a real run)")
        return 0
    # One machine-wide process-cwd scan serves every candidate's liveness
    # probe (t_ee808d83); a per-path `lsof +D` timed out on large workspaces.
    with kb.process_cwd_snapshot_scope():
        removed_ws = _gc_remove_workspaces(rows, scratch_root)

    removed_events = removed_audit = 0
    if event_days:
        with kbc.connect_closing() as conn:
            removed_events = kb.gc_events(conn, older_than_seconds=event_days * 24 * 3600)
            removed_audit = kb.gc_status_audit(conn, older_than_seconds=event_days * 24 * 3600)
    removed_logs = kb.gc_worker_logs(older_than_seconds=log_days * 24 * 3600) if log_days else 0
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_audit} status-audit row(s), "
          f"{removed_logs} log file(s) removed")
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    """Integrity check + narrow index-REINDEX auto-repair. Dispatched BEFORE
    the auto ``kb.init_db()`` (init refuses corrupt DBs). Exit 0 = healthy /
    repaired / no DB file, 1 = still corrupt."""
    if getattr(args, "reclassify_quota_crashes", False):
        import sqlite3
        from hermes_cli.kanban_quota_repair import reclassify_quota_crashes

        conn = None
        try:
            dry_run = bool(getattr(args, "dry_run", False))
            conn = kb.connect_readonly() if dry_run else kbc.connect()
            conn.row_factory = sqlite3.Row
            quota_report = reclassify_quota_crashes(conn, dry_run=dry_run)
        except Exception as exc:
            return _err(f"kanban repair: {exc}")
        finally:
            if conn is not None:
                conn.close()
        if getattr(args, "json", False):
            _print_json(quota_report)
        else:
            prefix = "Would repair" if dry_run else "Repaired"
            print(f"{prefix} {quota_report['reclassified']} quota-crashed runs; "
                  f"{len(quota_report['unblocked'])} cards unblocked.")
        return 0
    if getattr(args, "dry_run", False):
        return _err("kanban repair: --dry-run requires --reclassify-quota-crashes", 2)
    try:
        report = kbc.repair_db()
    except Exception as exc:  # locked/busy probe, unexpected I/O
        return _err(f"kanban repair: {exc}")

    if getattr(args, "json", False):
        _print_json({
            "status": report.status,
            "db_path": str(report.db_path),
            "messages": report.messages,
            "post_repair_messages": report.post_repair_messages,
            "backup_path": str(report.backup_path) if report.backup_path else None,
            "reindexed": report.reindexed,
        }, ascii=True)
        return 0 if report.status in {"ok", "repaired", "missing"} else 1

    if report.status == "missing":
        print(f"No kanban DB at {report.db_path} — nothing to repair.")
        return 0
    if report.status == "ok":
        print(f"{report.db_path}: integrity_check ok — no repair needed.")
        return 0
    if report.status == "repaired":
        print(f"{report.db_path}: repaired.")
        print(f"  reindexed: {', '.join(report.reindexed)}")
        if report.backup_path:
            print(f"  pre-repair backup: {report.backup_path}")
        print("  integrity_check now ok.")
        return 0
    # still corrupt
    def err(line: str) -> None:
        print(line, file=sys.stderr)

    err(f"{report.db_path}: CORRUPT.")
    for line in (report.messages or [])[:10]:
        err(f"  {line}")
    if report.reindexed:
        err(f"  REINDEX ({', '.join(report.reindexed)}) attempted but integrity_check is still failing:")
        for line in (report.post_repair_messages or [])[:10]:
            err(f"    {line}")
    else:
        err("  Not an index-only failure — automatic REINDEX repair does not apply (fail-closed).")
    if report.backup_path:
        err(f"  corrupt copy quarantined at: {report.backup_path}")
    err(
        "  Recover manually (copy kanban.db aside FIRST, then run "
        "`sqlite3 <copy> \".recover\"` into a fresh file — never against "
        "the live path, a WAL-reset-vulnerable sqlite3 CLI can corrupt it "
        "further) or move the file aside to start a new board."
    )
    return 1
