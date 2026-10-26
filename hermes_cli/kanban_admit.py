"""``hermes kanban admit <card> --host <host> --reason <why>`` (t_32e8796a, FWA).

An operator-only, per-card admission override. It spawns ONE ready card on ONE
host now, skipping the utilization LoadGate and the host admission band, and
still books the normal reservation so later admissions see the worker.

It runs the dispatcher's OWN ready lane (``kanban_db_dispatch.admit_once`` ->
``_dispatch_once_locked`` restricted to this card and this host) with exactly
one gate off, host admission. Routing/eligibility, the board budget, the pause
hold, the browser gate, the respawn guard, provider admission, the lane-model
override, workspace admission, the claim and the guarded launch are the tick's
own code, so a gate added to the tick binds admit too (Apollo r5).

What stays here: the operator token (``KANBAN_OPERATOR_TOKEN`` equal to the
board's ``operator-token`` file), the kill switch ``kanban.admin_admit.enabled``,
the measured-danger floor on the target host (memory available below the
host's ``mem_reserve_gb``, or load1 above threads x 1.5, refuses; unmeasured
refuses too, fail closed; the refusal prints the number and the threshold), and
the ``admission_override`` audit event (written by the dispatcher just before
launch).

There is no bulk form, no ``--max`` and no reordering: the 09-29 load-243
incident was a bulk override (t_689b81b7). Every admission writes an
``admission_override`` event naming the actor, host, reason and the band +
headroom it overrode.
"""

from __future__ import annotations

import functools
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

EVENT = kbd.ADMISSION_EVENT
LOAD_FACTOR = 1.5          # refuse when load1 > threads x LOAD_FACTOR
LOCAL = "studio"           # the canonical name of the dispatcher's own host (kwp.PIN_STUDIO)
_LOCK_WAIT_SECONDS = 30.0


class AdmitRefused(RuntimeError):
    """Admission refused; the message is the operator-facing line."""


@dataclass
class Measure:
    """The measured state of the target host the floor is judged on."""

    host: str
    load1: Optional[float]
    threads: Optional[int]
    mem_available_gb: Optional[float]
    mem_reserve_gb: float
    extra: dict = field(default_factory=dict)   # band / headroom the admission overrides
    threshold_error: Optional[str] = None       # why mem_reserve_gb is unknown (names the table)
    boards: Optional[frozenset] = None          # boards the host can host (None = not asked)


def enabled(kanban_cfg: Optional[dict]) -> bool:
    """``kanban.admin_admit.enabled`` (default true)."""
    sec = (kanban_cfg or {}).get("admin_admit") if isinstance(kanban_cfg, dict) else None
    if not isinstance(sec, dict):
        return True
    return sec.get("enabled", True) is not False


def _finite(x) -> bool:
    try:
        return x is not None and not isinstance(x, bool) and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def floor_refusal(m: Measure) -> Optional[str]:
    """The measured-danger refusal line for ``m``, or None to admit. A
    non-finite measurement or threshold (NaN, inf) is unmeasured: refuse."""
    if not _finite(m.mem_reserve_gb):
        why = f" ({m.threshold_error})" if m.threshold_error else ""
        return (f"{m.host}: mem_reserve_gb {m.mem_reserve_gb!r} is not a finite threshold{why}; "
                f"refusing, fail closed")
    if not _finite(m.mem_available_gb):
        return (f"{m.host}: memory available is unmeasured (threshold: mem_reserve_gb "
                f"{m.mem_reserve_gb:g} GB); refusing, fail closed")
    if m.mem_available_gb < m.mem_reserve_gb:
        return (f"{m.host}: memory available {m.mem_available_gb:.1f} GB < mem_reserve_gb "
                f"{m.mem_reserve_gb:g} GB")
    if not _finite(m.load1) or not _finite(m.threads) or m.threads <= 0:
        return f"{m.host}: load1/threads unmeasured (threshold: threads x {LOAD_FACTOR:g}); refusing, fail closed"
    limit = m.threads * LOAD_FACTOR
    if m.load1 > limit:
        return f"{m.host}: load1 {m.load1:.1f} > threads {m.threads} x {LOAD_FACTOR:g} = {limit:g}"
    return None


# -- host names ---------------------------------------------------------------

def _pool_and_signal(kanban_cfg: dict):
    """``(pool, signal, fleet_dir)``; ``pool`` None when the pool files refuse."""
    from gateway.kanban_gate_tick import standalone_builder
    from hermes_cli import kanban_load_gate as klg
    from hermes_cli import kanban_worker_pool as kwp

    fleet_dir = kb.kanban_home() / "fleet"
    pool = kwp.read_pool(fleet_dir, kanban_cfg=kanban_cfg)
    if pool.refused is not None or not (pool.pool_hosts or pool.hosts):
        return None, None, fleet_dir
    builder = standalone_builder(klg.gate_from_config({"kanban": kanban_cfg}), lambda: kanban_cfg)
    try:
        signal = builder.target_signal(kanban_cfg)
    except Exception:
        signal = None
    return pool, signal, fleet_dir


def canonical_host(word: str, pool, fleet_dir) -> str:
    """``studio`` for the dispatcher's own host (any of its spellings), else the
    canonical pool host id (by id or registry alias), else the word lower-cased."""
    from hermes_cli import kanban_worker_pool as kwp

    w = (word or "").strip().lower()
    if w in kwp._studio_words():
        return LOCAL
    known = {h.lower(): h for h in ((pool.hosts if pool is not None else ()) or ())}
    if w in known:
        return known[w]
    try:
        alias = kwp.pin_aliases(fleet_dir).get(w)
    except Exception:
        alias = None
    if alias == kwp.PIN_STUDIO:
        return LOCAL
    return known.get((alias or "").lower(), alias or w)


# -- measurement --------------------------------------------------------------

def _host_row(table, word: str, threads):
    """``(threads, mem_reserve_gb, threshold_error)`` for ``word`` from the
    placement table: its row, else ``threads`` / 0 GB (no table file, or no row).
    ``table`` None is re-read from the file: a table that exists but cannot be
    read is an unknown threshold, never "no reserve", so the reserve is NaN and
    the floor refuses naming the table (Prism a6c5f61c871f, 6e8b5b41758b)."""
    from hermes_cli import placement as pl

    path = pl.fleet_root() / pl.TABLE_REL
    try:
        if table is None:
            if not path.exists():
                return threads, 0.0, None
            table = pl.load_hosts(path)
        hid = table.resolve(word)
        if hid:
            return table.hosts[hid].threads, float(table.hosts[hid].mem_reserve_gb), None
    except Exception as exc:
        return threads, float("nan"), f"placement table {path} unreadable: {exc}"
    return threads, 0.0, None


def measure_local() -> Measure:
    """This host: load1, threads and mem_reserve_gb from its placement-table row
    (``local`` alias; os.cpu_count() / 0 GB without one), memory from psutil, and
    the LoadGate band + allowance the admission overrides."""
    from hermes_cli import kanban_load_gate as klg

    ctx = klg.utilization_context()
    threads, reserve, terr = _host_row(None, klg.UTIL_HOST_ALIAS, os.cpu_count())
    load1, _ = klg.sample_loadavg()
    gate = klg.gate_from_config(None)
    try:
        allowance, paused = gate.admit_now(running=None, cpu_block=0.25)
    except Exception as exc:
        allowance, paused = None, f"gate error: {exc}"
    return Measure(LOCAL, load1, threads, ctx.get("mem_available_gb"), reserve, extra={
        "band": gate.band, "gate_state": getattr(gate, "state", None), "spill_reason": gate.spill_reason,
        "gate_allowance": allowance, "spawn_paused": paused, "admission": gate.admission,
    }, threshold_error=terr)


def measure_remote(host, signal, workspaces_root: Optional[str] = None) -> Measure:
    """One fresh probe of pool host ``host``: load1/ncpu plus, with the target
    signal, its pressure file (memory, band); threads + mem_reserve_gb from its
    placement-table row (ncpu / 0 GB without one); hot streak from the state file.
    With ``workspaces_root`` (the tick's ``kanban.workspaces_root``) the same
    round-trip lists the boards the host can host, so the dispatcher's own
    board_refusal sees them (Prism 9f96730083e6)."""
    from hermes_cli import kanban_headroom as kh
    from hermes_cli import kanban_worker_pool as kwp

    kw = {"pressure_path": signal.policy.pressure_path} if signal is not None else {}
    if workspaces_root:
        kw["workspaces_root"] = workspaces_root
    sample = kwp.probe_host(host, **kw)
    load1 = ncpu = mem = None
    band = pressure = None
    boards = None
    if isinstance(sample, kwp.HostSample):
        load1, ncpu, boards = sample.load1, sample.ncpu, sample.boards
        if signal is not None:
            stale = signal.policy.stale_after_s
            fields = kh.sample_fields(sample.load5, sample.remote_now, sample.pressure_text or "",
                                      stale_after_s=stale, local_now=time.time())
            # A stale/undated pressure sample is unmeasured memory (Prism 142f1bce53e2 / caafe5f5edc2).
            mem = fields.get("mem_available_gb") if fields.get("sample_status") == "fresh" else None
            got, pressure = kwp.read_pressure(sample.remote_now, sample.pressure_text or "", stale_after_s=stale)
            band = got[1] if got else kwp.BAND_UNKNOWN
    elif sample is not None:
        load1, ncpu = sample
    # kh.load_table() gives the signal None for an INVALID table as well as an
    # absent one; _host_row re-reads the file, so only an absent table is 0 GB.
    table = signal.placement_table if signal is not None else None
    threads, reserve, terr = _host_row(table, host.name, ncpu)
    hot = None
    if signal is not None:
        st = kwp._load_state(signal.state_path).get(host.name)
        hot = bool(st.get("hot")) if isinstance(st, dict) else None
    return Measure(host.name, load1, threads, mem, reserve, extra={
        "band": band, "hot": hot, "pressure": pressure or None, "ncpu": ncpu,
    }, threshold_error=terr, boards=boards)


# -- the verb -------------------------------------------------------------------

def _running_host(conn, task, canon: Callable[[str], str] = lambda w: w) -> str:
    """The host the card's current run is on, canonicalised like ``--host``."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND run_id = ? "
        "ORDER BY id DESC LIMIT 1", (task.id, kb._kwh.PLACED_EVENT, task.current_run_id)).fetchone()
    if row is not None:
        try:
            import json
            host = json.loads(row[0]).get("host")
            if host:
                return canon(str(host))
        except Exception:
            pass
    word = (task.claim_lock or "").split(":", 1)[0]
    return canon(word) if word else "?"


def _actor() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "unknown"
    except Exception:
        return "unknown"


def admit(task_id: str, host_word: str, reason: str, *, kanban_cfg: Optional[dict] = None,
          board: Optional[str] = None, spawn_fn: Optional[Callable] = None,
          measure_local_fn: Callable[[], Measure] = None,
          measure_remote_fn: Callable[..., Measure] = None) -> dict:
    """Admit ONE ready card onto ONE host now. Returns ``{task_id, host, pid,
    payload}``; raises :class:`AdmitRefused` with the operator line otherwise."""
    if kanban_cfg is None:
        from hermes_cli.kanban_ops import _kanban_config
        kanban_cfg = _kanban_config()
    if not (enabled(kanban_cfg) and enabled(kbd._root_kanban_cfg())):
        # Either the caller's profile or the board owner's root config can
        # turn it off (Prism 6220ec27e4d9).
        raise AdmitRefused("kanban admit is disabled (kanban.admin_admit.enabled: false)")
    if not (reason or "").strip():
        raise AdmitRefused("--reason is required: quote the ruling")
    state = kb._operator_token_state()
    if state != "ok":
        raise AdmitRefused(f"kanban admit is operator-only: {kb.OPERATOR_TOKEN_ENV} {state.replace('_', ' ')}")
    pool, signal, fleet_dir = _pool_and_signal(kanban_cfg)
    host = canonical_host(host_word, pool, fleet_dir)
    if measure_remote_fn is None:
        root = (kanban_cfg or {}).get("workspaces_root") if isinstance(kanban_cfg, dict) else None
        # The same root the gateway tick probes with (kanban_gate_tick.plan_pool).
        measure_remote_fn = functools.partial(
            measure_remote, workspaces_root=root if isinstance(root, str) and root.startswith("/") else None)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    with kbc.connect_closing(board=board) as conn:
        while True:
            try:
                return _admit_locked(conn, task_id, host, reason.strip(), pool, signal, board,
                                     spawn_fn, measure_local_fn or measure_local,
                                     measure_remote_fn,
                                     canon=lambda w: canonical_host(w, pool, fleet_dir),
                                     kanban_cfg=kanban_cfg)
            except kbc_LockBusy:
                pass
            if time.monotonic() >= deadline:
                raise AdmitRefused(f"{task_id}: board tick lock busy for {_LOCK_WAIT_SECONDS:.0f}s; retry")
            time.sleep(0.2)


def _admit_locked(conn, task_id, host, reason, pool, signal, board, spawn_fn,
                  measure_local_fn, measure_remote_fn, canon: Callable[[str], str] = lambda w: w,
                  kanban_cfg: Optional[dict] = None) -> dict:
    task = kb.get_task(conn, task_id)
    if task is None:
        raise AdmitRefused(f"{task_id}: no such card")
    if task.status == "running" or (task.current_run_id is not None and task.claim_lock):
        raise AdmitRefused(f"{task_id}: already running on {_running_host(conn, task, canon)}")
    if task.status != "ready" or task.claim_lock:
        raise AdmitRefused(f"{task_id}: status is {task.status}, admit takes a ready card only")
    pool_hosts = {h.name: h for h in (pool.pool_hosts if pool is not None else ())}
    if host != LOCAL and host not in pool_hosts:
        raise AdmitRefused(f"{task_id}: {host} is not eligible for this card (not a pool host); "
                           f"eligible hosts are {LOCAL} and the pool: {', '.join(sorted(pool_hosts)) or 'none'}")
    pool_host = pool_hosts.get(host)
    m = measure_local_fn() if pool_host is None else measure_remote_fn(pool_host, signal)
    danger = floor_refusal(m)
    if danger is not None:
        raise AdmitRefused(f"{task_id}: refused on measured danger: {danger}")
    payload = {
        "source": "cli admit", "actor": _actor(), "host": host, "reason": reason,
        "load1": None if m.load1 is None else round(m.load1, 2), "threads": m.threads,
        "load_limit": None if not m.threads else m.threads * LOAD_FACTOR,
        "mem_available_gb": None if m.mem_available_gb is None else round(m.mem_available_gb, 2),
        "mem_reserve_gb": m.mem_reserve_gb, **m.extra,
    }
    res = kbd.admit_once(conn, task_id, host, board=board, spawn_fn=spawn_fn,
                         spillover=_admit_plan(pool, signal, host, m.boards), admission_event=payload,
                         **_tick_knobs(kanban_cfg), lock_wait_s=0.0)
    if res.skipped_locked:
        raise kbc_LockBusy()
    spawned = [s for s in res.spawned if s[0] == task_id]
    if not spawned:
        raise AdmitRefused(f"{task_id}: {refusal_from(res, task_id)}")
    after = kb.get_task(conn, task_id)
    return {"task_id": task_id, "host": host, "pid": getattr(after, "worker_pid", None),
            "workspace": spawned[0][2], "payload": payload}


class kbc_LockBusy(Exception):
    """The board's dispatch lock is held by a tick; admit retries."""


def _admit_plan(pool, signal, host: Optional[str] = None, boards=None):
    """The plan the dispatcher routes this card against: the pool config and
    signal, no slots (admit skips the band/slots). The probed ``boards`` of
    ``host`` go into ``detail[host]`` exactly as _plan_v01 puts them, so the
    dispatcher's own board_refusal refuses ``no_board_root`` (Prism 9f96730083e6)."""
    from hermes_cli import kanban_worker_pool as kwp

    if pool is None:
        # An EMPTY plan, not None: the tick's _route_row still runs (capabilities,
        # waits) and the forced-host check reads its route (Prism 4cc3d57f613b).
        return kwp.SpilloverPlan(hosts={}, slots={}, config=kwp.PoolConfig(), signal=None)
    detail = {host: {"boards": sorted(boards)}} if host and boards is not None else {}
    return kwp.SpilloverPlan(hosts={h.name: h for h in pool.pool_hosts}, slots={}, detail=detail,
                             config=pool, signal=signal)


def _tick_knobs(kanban_cfg: Optional[dict]) -> dict:
    """The tick's knobs that are not host admission: default assignee, the
    board's running ceiling (``kanban.max_spawn``) and the per-profile cap.
    ``max_in_progress`` (the host-wide ceiling) is host admission: not passed."""
    cfg = kanban_cfg if isinstance(kanban_cfg, dict) else {}
    per = cfg.get("max_in_progress_per_profile")
    if not isinstance(per, dict):
        per = kbd._positive_int(per, None)
    return {"default_assignee": (cfg.get("default_assignee") or "").strip() or None,
            "max_spawn": kbd._positive_int(cfg.get("max_spawn"), None),
            "max_in_progress_per_profile": per}


def refusal_from(res, task_id: str) -> str:
    """The dispatcher's own reason it did not spawn ``task_id``, as one line."""
    if res.budget_paused:
        return "budget_paused: board is over kanban.budget.usd_per_24h (dispatch holds it the same way)"
    if res.spawn_capped:
        return f"spawn_capped: {res.spawn_capped}"
    for tid, reason in res.respawn_guarded:
        if tid == task_id:
            d = res.respawn_guard_details.get(task_id) or {}
            if d.get("owner"):
                return f"{reason}: {d.get('pr')} is owned by running card {d['owner']}"
            extra = ", ".join(f"{k}={v}" for k, v in d.items())
            return f"respawn guard holds it: {reason}" + (f" ({extra})" if extra else "")
    wait = res.placement_waits.get(task_id)
    if wait is not None:
        why = str(wait)
        if why.startswith(kbd.ADMIT_INELIGIBLE + ": "):
            return why[len(kbd.ADMIT_INELIGIBLE) + 2:]
        return f"held: {why}"
    for tid, why in res.workspace_refused:
        if tid == task_id:
            return f"workspace unavailable: {why}"
    for tid, skills in res.skill_refused:
        if tid == task_id:
            return f"card skills refused: {', '.join(skills)}"
    for tid, who, n in res.skipped_per_profile_capped:
        if tid == task_id:
            return f"per_profile_cap: {who} has {n} running"
    named = [("skipped_no_worker", "operator-only card (no_worker), never spawned"),
             ("skipped_nonspawnable", "assignee is not a spawnable profile here"),
             ("skipped_unassigned", "unassigned and no kanban.default_assignee"),
             ("flagship_refused", "flagship model refused (see the dispatcher comment)"),
             ("auto_blocked", "blocked by the dispatcher (see the card)"),
             ("spawn_failed", "spawn failed; the claim was released (see the run)")]
    for attr, line in named:
        if task_id in (getattr(res, attr, None) or ()):
            return line
    return "the dispatcher did not spawn it (card no longer ready/unclaimed?)"


def cmd_admit(args) -> int:
    """CLI entry: one line on success, one refusal line (rc 1) otherwise."""
    import sys

    try:
        res = admit(args.task_id, args.host, args.reason)
    except AdmitRefused as exc:
        print(f"kanban admit: REFUSED {exc}", file=sys.stderr)
        return 1
    p = res["payload"]
    print(f"kanban admit: {res['task_id']} spawned on {res['host']} pid={res['pid']} "
          f"(overrode band={p.get('band')} allowance={p.get('gate_allowance', '-')}; "
          f"load1={p.get('load1')} / limit {p.get('load_limit')}, mem {p.get('mem_available_gb')} GB "
          f"/ reserve {p.get('mem_reserve_gb')} GB) event={EVENT}")
    return 0
