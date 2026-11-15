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

The pool plan is the dispatcher's own (``GateTickBuilder.build_pool_plan``:
pool files, signal, the real probe with ``kanban.workspaces_root``); None when
the pool refuses, exactly as a tick with no plan (Apollo r7). The forced host is
checked against the card's FINAL route, after the pause hold and browser gate.

What stays here: the operator token (``KANBAN_OPERATOR_TOKEN`` equal to the
board's ``operator-token`` file), the kill switch ``kanban.admin_admit.enabled``,
the canonical host name, the floor callable and the ``admission_override``
event payload. The dispatcher runs the floor INSIDE its tick lock right before
take/claim, so the measurement it judges is the one the launch uses (Prism
84845601b3b9).

The floor is the canonical FWA predicate, ``fleet_placement.admission.admit()``,
never a private threshold (Ace msg 1569308234441949345 "only fwa is the
guard/gate"; msg 1569529863298359387: no consumer-side vetoes). The override
contract: admit runs that predicate with ``cpu_saturable=True``, the predicate's
own switch that makes the CPU line a score penalty instead of a refusal. That is
the host admission band admit exists to override. Every other axis stands
exactly as FWA prices it: memory available after the host table's
``mem_reserve_gb`` (subtracted once, inside ``admit()``), the live leases of
every consumer on the host, this card's measured estimate, PSI mem/io, process
slots, the SLO guard, and an unmeasured or unreadable input refuses (fail
closed). There is no load1 bar and no memory floor of admit's own.

There is no bulk form, no ``--max`` and no reordering: the 09-29 load-243
incident was a bulk override (t_689b81b7). Every admission writes an
``admission_override`` event naming the actor, host, reason and the band +
headroom it overrode.
"""

from __future__ import annotations

import dataclasses
import functools
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_lazy import Module
kbd = Module("hermes_cli.kanban_db_dispatch")

from hermes_cli.kanban_dispatch_defaults import ADMISSION_EVENT as EVENT
CONSUMER = "kanban"        # the placement consumer whose estimate prices one card (kanban_headroom.CONSUMER)
LOCAL = "studio"           # the canonical name of the dispatcher's own host (kwp.PIN_STUDIO)
# A tick now holds the board lock only while it decides (kanban.dispatch.narrow_lock, t_f60f502f), so
# admit waits for the next gap instead of handing the operator a "retry".
_LOCK_WAIT_SECONDS = 300.0


class AdmitRefused(RuntimeError):
    """Admission refused; the message is the operator-facing line."""


@dataclass
class Measure:
    """The FWA inputs for the target host: the admission sample, this card's
    estimate, the host's limits and the live leases, as ``admission.admit()``
    takes them. ``hid`` is the placement-table id leases are booked under."""

    host: str
    sample: dict
    need: Any = None                            # admission.Estimate (None = unpriced: refuse)
    limits: Any = None                          # admission.Limits (None = no host facts: refuse)
    leases: tuple = ()
    hid: Optional[str] = None
    extra: dict = field(default_factory=dict)   # band / headroom the admission overrides
    threshold_error: Optional[str] = None       # why an FWA input is unreadable (names the file)
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
    """The canonical FWA verdict for ``m`` under the override contract (CPU line
    as a penalty, every other axis refusing), or None to admit."""
    from hermes_cli.fleet_placement import admission as _ad

    if m.threshold_error:
        return f"{m.host}: {m.threshold_error}; refusing, fail closed"
    if m.need is None or m.limits is None:
        return f"{m.host}: FWA inputs unmeasured (estimate or host limits); refusing, fail closed"
    v = _ad.admit(m.sample, m.need, m.leases, host=m.hid or m.host,
                  limits=dataclasses.replace(m.limits, cpu_saturable=True))
    m.extra["fwa_binding"], m.extra["fwa_free"] = v.binding, {k: round(float(x), 3) for k, x in v.free.items()}
    if v.ok:
        return None
    return f"{m.host}: FWA refused: {v.reason} (binding {v.binding})"


# -- host names ---------------------------------------------------------------

def _dispatcher_plan(kanban_cfg: dict):
    """``(plan, fleet_dir, pool_off)``: the pool plan the dispatcher's own builder
    makes (``GateTickBuilder.build_pool_plan``, the call ``plan_pool`` makes once
    it decided a plan is needed; that decision reads the Studio band, which is
    host admission). ``plan`` is None when the pool refuses or has no hosts, or
    the plan fails: the dispatcher then routes exactly as a tick with no plan.
    ``pool_off`` is the tick's own reason when an operator pool off-switch holds
    (``config_disabled`` / ``proc_paused``, ``pool_switch_refusal``), else None;
    the gate is sampled first, as the tick does, so ``remote_allowed`` is real
    (Prism ddce35a5f884)."""
    from gateway.kanban_gate_tick import live_boards, running_split, standalone_builder
    from hermes_cli import kanban_load_gate as klg

    fleet_dir = kb.kanban_home() / "fleet"
    cfg = kanban_cfg if isinstance(kanban_cfg, dict) else {}
    try:
        builder = standalone_builder(klg.gate_from_config({"kanban": cfg}), lambda: cfg)
        boards = live_boards()
        running_local, remote_by_host = running_split(builder._ledger(boards))
        builder._remote_by_host = dict(remote_by_host)
        gate = builder.gate
        gate.admit_now(running=running_local if gate.enabled else None, cpu_block=0.25)
        off = builder.pool_switch_refusal(cfg)
        if off is not None:
            return None, fleet_dir, off
        return builder.build_pool_plan(boards, remote_by_host, cfg), fleet_dir, None
    except Exception:
        return None, fleet_dir, None


def canonical_host(word: str, pool, fleet_dir) -> str:
    """``studio`` for the dispatcher's own host (any of its spellings), else the
    canonical pool host id (by id or registry alias), else the word lower-cased."""
    from hermes_cli import kanban_worker_pool as kwp

    w = (word or "").strip().lower()
    if w in kwp._studio_words():
        return LOCAL
    known = {h.lower(): h for h in ((getattr(pool, "hosts", None) if pool is not None else ()) or ())}
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

def _fwa_inputs(table, word: str, consumer_need, *, now: float):
    """``(hid, limits, need, leases, error)`` for ``word``: the placement-table row's
    limits (``placement._limits``, the same the router uses), this card's estimate
    on that host, and the shared ledger's live leases. ``table`` None is re-read
    from the file. A truly absent table (or no row) is default limits and the
    caller's estimate; the live ledger's leases still count. A table or ledger that exists but cannot be read is an
    error (fail closed, naming the file; Prism a6c5f61c871f, d16d6de8bc5b)."""
    from hermes_cli.fleet_placement import admission as _ad
    from hermes_cli.fleet_placement import placement as pl

    path = pl.fleet_root() / pl.TABLE_REL
    lpath = pl.fleet_root() / pl.RESERVATIONS_REL

    def _no_row(tbl):
        # No table row is default limits, never "no leases": the live ledger still binds, and an unreadable one
        # fails closed (Prism 891b269f644d / 5e9e08ea372d).
        try:
            return word, _ad.Limits(), consumer_need, tuple(pl.ledger_leases(lpath, now=now, table=tbl)), None
        except Exception as exc:
            return word, None, None, (), f"reservation ledger {lpath} unreadable: {exc}"

    try:
        if table is None:
            try:
                path.stat()
            except FileNotFoundError:
                # Only a truly absent table is "no row"; exists() would also
                # say False on PermissionError (py3.14), Prism d16d6de8bc5b.
                return _no_row(None)
            table = pl.load_hosts(path)
    except Exception as exc:
        return word, None, None, (), f"placement table {path} unreadable: {exc}"
    hid = table.resolve(word)
    if not hid:
        return _no_row(table)
    try:
        leases = tuple(pl.ledger_leases(lpath, now=now, table=table))
        need = consumer_need
        if need is None:
            fp = table.footprint(CONSUMER)
            need = pl.admit_context(CONSUMER, fp, now=now, table=table, leases=leases).need(hid)
    except Exception as exc:
        return hid, None, None, (), f"reservation ledger {lpath} unreadable: {exc}"
    return hid, pl._limits(table.hosts[hid]), need, leases, None


def measure_local() -> Measure:
    """This host: the same sample the LoadGate feeds ``admit_class`` (cpu_busy,
    MemAvailable, procs, PSI), the kanban estimate here, the host table's limits
    via the ``local`` alias, every consumer's live leases, and the LoadGate band
    + allowance the admission overrides."""
    from hermes_cli import kanban_load_gate as klg

    ctx = klg.utilization_context()
    gate = klg.gate_from_config(None)
    try:
        allowance, paused = gate.admit_now(running=None, cpu_block=0.25)
    except Exception as exc:
        allowance, paused = None, f"gate error: {exc}"
    now = time.time()
    hid, limits, need, leases, err = _fwa_inputs(None, klg.UTIL_HOST_ALIAS, ctx.get("need"), now=now)
    procs, maxproc = klg.sample_user_procs()
    sample = {"host": hid, "os": klg._os_name(), "at": now, "ncpu": gate.ncpu, "cpu_busy": gate.cpu_busy,
              "mem_available_gb": ctx.get("mem_available_gb"), "mem_total_gb": ctx.get("mem_total_gb"),
              "procs": procs, "maxproc": maxproc, **klg.sample_psi()}
    return Measure(LOCAL, sample, need, limits, leases, hid=hid, extra={
        "band": gate.band, "gate_state": getattr(gate, "state", None), "spill_reason": gate.spill_reason,
        "gate_allowance": allowance, "spawn_paused": paused, "admission": klg.ADMISSION, "load1": gate.load1,
    }, threshold_error=err)


def measure_remote(host, signal, workspaces_root: Optional[str] = None) -> Measure:
    """One fresh probe of pool host ``host``: its pushed pressure sample (the
    admission vector) read through the target signal's freshness policy; the
    table row's limits, the card's estimate and the live leases as FWA reads
    them; hot = the published kanban band. With ``workspaces_root`` (the tick's
    ``kanban.workspaces_root``) the same round-trip lists the boards the host
    can host, so the dispatcher's own board_refusal sees them (Prism
    9f96730083e6). No signal or a stale sample = an unmeasured sample: refuse."""
    from hermes_cli import kanban_headroom as kh
    from hermes_cli import kanban_worker_pool as kwp

    kw = {"pressure_path": signal.policy.pressure_path} if signal is not None else {}
    if workspaces_root:
        kw["workspaces_root"] = workspaces_root
    sample_raw = kwp.probe_host(host, **kw)
    now = time.time()
    sample: dict = {}
    load1 = ncpu = band = pressure = boards = None
    if isinstance(sample_raw, kwp.HostSample):
        load1, ncpu, boards = sample_raw.load1, sample_raw.ncpu, sample_raw.boards
        if signal is not None:
            stale = signal.policy.stale_after_s
            fields = kh.sample_fields(sample_raw.load5, sample_raw.remote_now, sample_raw.pressure_text or "",
                                      stale_after_s=stale, local_now=now)
            # A stale/undated pressure sample is unmeasured (Prism 142f1bce53e2 / caafe5f5edc2).
            if fields.get("sample_status") == "fresh":
                sample = {k: v for k, v in fields.items() if k not in ("sample_status", "sample_at", "load5")}
                sample["at"] = fields.get("sample_at")
                sample.setdefault("ncpu", ncpu)
            got, pressure = kwp.read_pressure(sample_raw.remote_now, sample_raw.pressure_text or "",
                                              stale_after_s=stale)
            band = got[1] if got else kwp.BAND_UNKNOWN
    elif sample_raw is not None:
        load1, ncpu = sample_raw
    # kh.load_table() gives the signal None for an INVALID table as well as an
    # absent one; _fwa_inputs re-reads the file, so only an absent table is no row.
    table = signal.placement_table if signal is not None else None
    hid, limits, need, leases, err = _fwa_inputs(table, host.name, None, now=now)
    if need is None and err is None:
        from hermes_cli.fleet_placement import admission as _ad
        need = _ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior")    # no table row: the table's kanban prior
    sample["host"] = hid
    # t_5d7e7171: no streak file; hot = the published band (admit_class's verdict) this probe
    hot = (band == kwp.BAND_HOT) if signal is not None and band != kwp.BAND_UNKNOWN else None
    return Measure(host.name, sample, need, limits, leases, hid=hid, extra={
        "band": band, "hot": hot, "pressure": pressure or None, "ncpu": ncpu, "load1": load1,
    }, threshold_error=err, boards=boards)


# -- the verb -------------------------------------------------------------------

def _running_host(conn, task, canon: Callable[[str], str] = lambda w: w) -> str:
    """The host the card's current run is on, canonicalised like ``--host``."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN (?, ?) AND run_id = ? "
        "ORDER BY id DESC LIMIT 1", (task.id, "worker_placed", "worker_allocated", task.current_run_id)).fetchone()
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
    plan, fleet_dir, *rest = _dispatcher_plan(kanban_cfg)
    pool_off = rest[0] if rest else None
    host = canonical_host(host_word, plan.config if plan is not None else None, fleet_dir)
    if pool_off is not None and host != LOCAL:
        # The operator's pool off-switch: the tick plans nothing remote, so
        # neither does admit; the reason is the tick's own (gate.pool).
        raise AdmitRefused(f"{task_id}: worker pool is off ({pool_off}); "
                           f"{host} is a pool host, admit places nothing remote")
    if measure_remote_fn is None:
        root = (kanban_cfg or {}).get("workspaces_root") if isinstance(kanban_cfg, dict) else None
        # The same root the gateway tick probes with (kanban_gate_tick.plan_pool).
        measure_remote_fn = functools.partial(
            measure_remote, workspaces_root=root if isinstance(root, str) and root.startswith("/") else None)
    canon = lambda w: canonical_host(w, plan.config if plan is not None else None, fleet_dir)  # noqa: E731
    with kbc.connect_closing(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise AdmitRefused(f"{task_id}: no such card")
        if task.status == "running" or (task.current_run_id is not None and task.claim_lock):
            raise AdmitRefused(f"{task_id}: already running on {_running_host(conn, task, canon)}")
        if task.status != "ready" or task.claim_lock:
            raise AdmitRefused(f"{task_id}: status is {task.status}, admit takes a ready card only")
        payload = {"source": "cli admit", "actor": _actor(), "host": host, "reason": reason.strip()}
        floor = _floor(measure_local_fn or measure_local, measure_remote_fn, payload)
        try:
            res = kbd.admit_once(conn, task_id, host, board=board, spawn_fn=spawn_fn, spillover=plan,
                                 admission_event=payload, admit_floor=floor,
                                 **_tick_knobs(kanban_cfg), lock_wait_s=_LOCK_WAIT_SECONDS)
        except Exception as exc:
            # The claim names THIS process; once it exits nothing heartbeats or
            # finishes the run, so a launch that raised does not leave it behind (t_bf2c4ade).
            raise AdmitRefused(f"{task_id}: launch failed ({exc})"
                               f"{_release_failed_launch_claim(conn, task_id, f'admit launch raised: {exc}')}"
                               ) from exc
        if res.skipped_locked:
            raise AdmitRefused(f"{task_id}: board tick lock held for {_LOCK_WAIT_SECONDS:.0f}s by "
                               f"{kb.format_dispatch_lock_skip(res.lock_holder)}")
        spawned = [x for x in res.spawned if x[0] == task_id]
        if not spawned:
            raise AdmitRefused(f"{task_id}: {refusal_from(res, task_id)}"
                               f"{_release_failed_launch_claim(conn, task_id, 'admit: not spawned')}")
        after = kb.get_task(conn, task_id)
    out = {"task_id": task_id, "host": host, "pid": getattr(after, "worker_pid", None),
           "workspace": spawned[0][2], "payload": payload}
    if out["pid"] is None:
        # Reported spawned, no pid recorded yet. A missing pid is not proof that no
        # worker launched (Prism 3515cb6bd1ed), so the claim stays; the dead-claimer
        # launch bound releases it if no worker ever shows up (t_bf2c4ade).
        out["note"] = ("no worker pid recorded yet; the worker may be starting. The claim is kept: "
                       "if no worker shows up, the launch bound releases it")
    return out


def _release_failed_launch_claim(conn, task_id: str, why: str) -> str:
    """Release the claim THIS process holds on ``task_id`` after a launch that
    definitively did not happen (admit_once raised, or did not spawn the card)
    (t_bf2c4ade). Re-checked under the release's write lock: a stamped worker
    pid or a ``spawned``/``heartbeat`` row on the run keeps the claim. Returns
    the operator-facing suffix, ``""`` when the claim is not ours to release."""
    task = kb.get_task(conn, task_id)
    if (task is None or task.status != "running" or task.worker_pid
            or task.current_run_id is None or task.claim_lock != kb._claimer_id()):
        return ""
    run_id = int(task.current_run_id)

    def _no_worker_evidence(c) -> bool:
        row = c.execute("SELECT worker_pid FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is not None and row[0]:
            return False
        return c.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                         "AND kind IN ('spawned','heartbeat') LIMIT 1", (task_id, run_id)).fetchone() is None

    kbd._record_task_failure(conn, task_id, why, outcome="spawn_failed", release_claim=True,
                             end_run=True, infrastructure=True,
                             expected_run_id=run_id, precondition=_no_worker_evidence)
    after = kb.get_task(conn, task_id)
    if after is not None and after.claim_lock:
        return "; a worker was recorded, so the claim was kept"
    return f"; claim released, card is {getattr(after, 'status', 'gone')}"


def _floor(measure_local_fn, measure_remote_fn, payload: dict):
    """The measured-danger floor the dispatcher calls INSIDE its tick lock, right
    before take/claim: ``(host, plan) -> (refusal, measured payload, boards)``.
    The measurement also lands in ``payload`` (the operator's return line)."""
    def floor(host: str, plan):
        pool_host = (plan.hosts.get(host) if plan is not None else None)
        if host != LOCAL and pool_host is None:
            return f"{host}: not a pool host of this plan; nothing measured", {}, None
        m = measure_local_fn() if host == LOCAL else measure_remote_fn(
            pool_host, getattr(plan, "signal", None))
        refusal = floor_refusal(m)
        mem = m.sample.get("mem_available_gb")
        measured = {
            "cpu_busy": m.sample.get("cpu_busy"),
            "mem_available_gb": round(float(mem), 2) if _finite(mem) else None,
            "mem_reserve_gb": getattr(m.limits, "mem_reserve_gb", None),
            "need_ram_gb": getattr(m.need, "ram_gb", None), **m.extra,
        }
        payload.update(measured)
        return refusal, measured, m.boards
    return floor


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
        if why.startswith(kbd.ADMIT_FLOOR + ": "):
            return f"refused on measured danger: {why[len(kbd.ADMIT_FLOOR) + 2:]}"
        if why.startswith(kbd.ADMIT_INELIGIBLE + ": "):
            return why[len(kbd.ADMIT_INELIGIBLE) + 2:]
        from hermes_cli.kanban_studio_allowlist import override_command
        cmd = override_command(task_id, why)
        if cmd is not None:
            # t_5d63270d (Prism c-e7592fee5f5910ad): admit never starts what default-deny holds; the override is the
            # audited path (studio_override event + capability-gap ledger line), then admit again.
            return f"held: {why}; to start it on the Studio run `{cmd}` first, then admit again"
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
          f"FWA binding={p.get('fwa_binding')} cpu_busy={p.get('cpu_busy')}, mem {p.get('mem_available_gb')} GB "
          f"/ reserve {p.get('mem_reserve_gb')} GB) event={EVENT}")
    if res.get("note"):
        print(f"kanban admit: {res['task_id']}: {res['note']}")
    return 0
