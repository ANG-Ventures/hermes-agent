"""``hermes kanban diagnostics --placement`` (Placement PRD v0.4 §6 Phase 1 "Verify with").

READ-ONLY view of what the pool planner sees, built from files the gateway
already wrote. It never probes a host over ssh and never advances a streak:

* ``<root>/load_gate.json`` ``pool`` block: band / hot / pressure / load1 /
  free per host, from the last gateway tick;
* ``<root>/var/kanban-target-state.json``: the per-host hot/clear streak;
* ``placement_ledger.read_all()`` + ``projected(host, load1)`` per host, with
  each reservation's unrealised share;
* the rung ``SpilloverPlan.take()`` would pick now, and why. The real
  ``take()`` runs on an in-memory plan rebuilt from the pool block, so the
  answer cannot drift from the dispatcher's rule.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import placement_ledger as pledger
from hermes_cli import placement_policy as ppolicy


def _read_signal(kanban_cfg: Mapping) -> bool:
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    return not (isinstance(pl, Mapping) and pl.get("read_signal", True) is False)


def _map(v) -> Mapping:
    return v if isinstance(v, Mapping) else {}


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def compute(root: Path, *, kanban_cfg: Optional[Mapping] = None, gate_state: Optional[Mapping] = None,
            now: Optional[float] = None, assignee: Optional[str] = None) -> Dict[str, Any]:
    """The placement report as a dict (the ``--json`` payload)."""
    root = Path(root)
    kanban_cfg = _map(kanban_cfg)
    now = time.time() if now is None else float(now)
    fleet_dir = root / "fleet"
    policy = ppolicy.load(fleet_dir)
    pool = kwp.read_pool(fleet_dir, kanban_cfg=kanban_cfg)
    gate_state = _map(gate_state)
    block = _map(gate_state.get("pool"))
    tick_hosts = _map(block.get("hosts"))
    signal_on = _read_signal(kanban_cfg)
    streaks = kwp._load_state(root / "var" / kwp.TARGET_STATE_FILE)
    reservations = pledger.read_all(pledger.ledger_dir(root), policy, now=now)
    cost = _num(gate_state.get("cost"))

    in_pool = {h.name for h in pool.pool_hosts}
    names = list(dict.fromkeys([*(h.name for h in pool.pool_hosts), *pool.disabled, *tick_hosts]))
    hosts: Dict[str, dict] = {}
    for name in names:
        d = _map(tick_hosts.get(name))
        load1 = _num(d.get("load1"))
        res_rows = []
        for r in reservations:
            if r.host != name:
                continue
            shares = [pledger.unrealised_fraction(now - t, r.ramp_s) for t in r.placed_at]
            res_rows.append({"consumer": r.consumer, "busy_units": r.busy_units, "cpu_est": r.cpu_est,
                             "ramp_s": r.ramp_s, "unrealised": [round(s, 4) for s in shares],
                             "pending": round(r.cpu_est * sum(shares), 4)})
        pending = pledger.pending(name, reservations, now=now)
        kb = policy.kanban_band(name)
        st = _map(streaks.get(name))
        hosts[name] = {
            # Eligible only when the CURRENT pool places on it: a host the
            # last tick saw but the pool files have since dropped is not.
            "enabled": name in in_pool,
            "band": d.get("band") or kwp.BAND_UNKNOWN,
            "hot": bool(d.get("hot")),
            "pressure": d.get("pressure"),
            "free": d.get("free"),
            "load1": load1,
            "pending": round(pending, 4),
            "projected": None if load1 is None else round(pledger.projected(name, load1, reservations, now=now), 4),
            "reservations": res_rows,
            "streak": dict(st) if st else None,
            "streak_policy": {"hot_streak": kb.hot_streak, "clear_streak": kb.clear_streak,
                              "warm": kb.warm, "hot": kb.hot},
        }

    if assignee is None:
        assignee = pool.profiles[0] if pool.profiles else None
    rung = pick_rung(pool, block, tick_hosts, policy=policy, root=root, signal_on=signal_on,
                     cost=cost, now=now, assignee=assignee, reservations=reservations)
    return {
        "read_signal": signal_on,
        "policy_source": policy.source,
        "pool_refused": pool.refused,
        "pool_warnings": list(pool.warnings),
        "tick": {"planned": bool(block.get("planned")), "reason": block.get("reason"),
                 "planned_at": block.get("planned_at"),
                 "gate_updated_at": gate_state.get("updated_at"), "band": gate_state.get("band")},
        "now": now,
        "kanban_cpu_est": cost,
        "hosts": hosts,
        "rung": rung,
    }


def pick_rung(pool: kwp.PoolConfig, block: Mapping, tick_hosts: Mapping, *, policy, root: Path,
              signal_on: bool, cost: Optional[float], now: float, assignee: Optional[str],
              reservations: List[pledger.Reservation]) -> dict:
    """``take()``'s pick for ``assignee`` on the last tick's plan, never persisted.
    ``reservations`` is the report's ONE ledger read: the host totals, the
    pick and its explanation all use it."""
    if pool.refused is not None:
        return {"host": None, "why": f"pool refused: {pool.refused}", "assignee": assignee}
    if not block.get("planned"):
        return {"host": None, "assignee": assignee,
                "why": f"no pool plan on the last tick ({block.get('reason') or 'no load_gate state'})"}
    if assignee is None:
        return {"host": None, "assignee": None, "why": "no pool profile to place"}
    hosts = list(pool.pool_hosts)
    slots = {h.name: int(_num(_map(tick_hosts.get(h.name)).get("free")) or 0) for h in hosts}
    detail = {h.name: {k: v for k, v in _map(tick_hosts.get(h.name)).items() if k != "free"}
              for h in hosts}
    signal = None
    if signal_on:
        signal = kwp.TargetSignal(policy=policy, state_path=root / "var" / kwp.TARGET_STATE_FILE,
                                  ledger_dir=pledger.ledger_dir(root), cpu_est=cost, clock=lambda: now,
                                  reservations=list(reservations))
    plan = kwp._finish_plan(hosts, slots, detail, pool.disabled, block.get("planned_at"), pool, signal)
    free = [n for n, h in plan.hosts.items() if slots.get(n, 0) > 0 and assignee in h.profiles]
    chosen = plan.take(assignee)  # in-memory plan only: nothing is written
    if chosen is None:
        return {"host": None, "assignee": assignee, "why": plan.refusal or "pool_full"}
    name = chosen.name
    if signal is None:
        why = "read_signal false (KWLB v0.1): first host in priority order with a free slot"
    else:
        bands = {n: detail[n].get("band") for n in free}
        cool = [n for n in free if bands[n] == kwp.BAND_OK]
        warm = [n for n in free if bands[n] == kwp.BAND_WARM]
        if cool:
            why = f"band ok: first cool host in priority order (free: {', '.join(free)})"
        else:
            res = reservations
            pool_set = warm or free
            projs = ", ".join(
                f"{n}={pledger.projected(n, float(_num(detail[n].get('load1')) or 0.0), res, now=now):.2f}"
                for n in pool_set)
            kind = "warm" if warm else "no ok/warm host; hot-not-streaked"
            why = f"{kind}: least projected() wins ({projs})"
    return {"host": name, "assignee": assignee, "why": why}


def format_lines(rep: Mapping) -> List[str]:
    tick = rep.get("tick") or {}
    out = [f"Placement (read_signal={'true' if rep.get('read_signal') else 'false'}, "
           f"policy={rep.get('policy_source')}, kanban cpu_est={rep.get('kanban_cpu_est')})"]
    if rep.get("pool_refused"):
        out.append(f"  pool refused: {rep['pool_refused']}")
    for w in rep.get("pool_warnings") or ():
        out.append(f"  warning: {w}")
    upd = _num(tick.get("gate_updated_at"))
    age = "?" if upd is None else f"{rep['now'] - upd:.0f}s ago"
    out.append(f"  last tick: planned={tick.get('planned')} reason={tick.get('reason') or '-'} "
               f"gate band={tick.get('band')} (updated {age})")
    for name, h in (rep.get("hosts") or {}).items():
        st = h.get("streak")
        sp = h["streak_policy"]
        streak = ("none" if not st else
                  f"hot={st.get('hot')} hot_run={st.get('hot_run')}/{sp['hot_streak']} "
                  f"clear_run={st.get('clear_run')}/{sp['clear_streak']}")
        proj = "? (no load1 this tick)" if h.get("projected") is None else f"{h['projected']:.2f}"
        out.append(f"  {name}: band={h['band']}{' HOT' if h.get('hot') else ''}"
                   f"{'' if h.get('enabled') else ' (disabled)'} free={h.get('free')} "
                   f"load1={h.get('load1')} pending=+{h['pending']:.2f} projected={proj}")
        if h.get("pressure"):
            out.append(f"    pressure: {h['pressure']}")
        out.append(f"    streak: {streak}")
        for r in h.get("reservations") or ():
            out.append(f"    reservation {r['consumer']}: units={r['busy_units']} cpu_est={r['cpu_est']} "
                       f"ramp={r['ramp_s']:g}s unrealised={r['unrealised']} -> +{r['pending']:.2f}")
    rung = rep.get("rung") or {}
    out.append(f"  rung for @{rung.get('assignee')}: {rung.get('host') or 'none'} ({rung.get('why')})")
    return out
