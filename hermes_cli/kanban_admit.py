"""``hermes kanban admit <card> --host <host> --reason <why>`` (t_32e8796a, FWA).

An operator-only, per-card admission override. It spawns ONE ready card on ONE
host now, skipping the utilization LoadGate and the host admission band, and
still books the normal reservation so later admissions see the worker.

What it never skips:

* the card's eligible host set (pins, host_affinity / reach, capabilities,
  workspace kind and binds): the dispatcher's own ``_route_row`` and
  ``_portable_only`` decide it, so admit never moves a card where it cannot run;
* the measured-danger floor on the target host: memory available below the
  host's ``mem_reserve_gb``, or load1 above threads x 1.5, refuses. An
  unmeasured value refuses too (fail closed), and the refusal prints the number
  and the threshold;
* the operator token (``KANBAN_OPERATOR_TOKEN`` equal to the board's
  ``operator-token`` file) and the kill switch ``kanban.admin_admit.enabled``.

There is no bulk form, no ``--max`` and no reordering: the 09-29 load-243
incident was a bulk override (t_689b81b7). Every admission writes an
``admission_override`` event naming the actor, host, reason and the band +
headroom it overrode.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw

EVENT = "admission_override"
LOAD_FACTOR = 1.5          # refuse when load1 > threads x LOAD_FACTOR
LOCAL = "studio"           # the canonical name of the dispatcher's own host (kwp.PIN_STUDIO)
_LOCK_WAIT_SECONDS = 30.0


class AdmitRefused(RuntimeError):
    """Admission refused; the message is the operator-facing line."""


class _WorkspaceRefused(Exception):
    """The workspace admission refused (WorkspaceUnavailable) before launch."""


@dataclass
class Measure:
    """The measured state of the target host the floor is judged on."""

    host: str
    load1: Optional[float]
    threads: Optional[int]
    mem_available_gb: Optional[float]
    mem_reserve_gb: float
    extra: dict = field(default_factory=dict)   # band / headroom the admission overrides


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
        return f"{m.host}: mem_reserve_gb {m.mem_reserve_gb!r} is not a finite threshold; refusing, fail closed"
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


# -- eligibility --------------------------------------------------------------

def eligible_hosts(conn, row, pool, signal, assignee: str, board: Optional[str]) -> "tuple[set, str]":
    """``(hosts, why)``: every host this card may run on now, by the dispatcher's
    own routing (``_route_row``, dry: nothing is persisted) and, for a portable
    card, its pool reach (``_portable_only``). ``why`` names the route."""
    from hermes_cli import kanban_worker_pool as kwp

    if pool is None:
        # No pool: a remote pin never runs locally (I-11); anything else is local.
        known = kwp.known_host_ids(kb.kanban_home() / "fleet")
        pin, ignored = kwp.resolve_pin(row["body"], known, kwp.pin_aliases(kb.kanban_home() / "fleet"))
        if pin not in (None, kwp.PIN_ANY, kwp.PIN_STUDIO) or (pin is None and ignored and known):
            return set(), f"pinned to {pin or ignored} and no pool plan is possible"
        # Still the dispatcher's routing (capabilities, waits) over an empty
        # pool: only the pool part is absent, never the local checks (Prism 4cc3d57f613b).
        empty = kwp.PoolConfig()
        plan = kwp.SpilloverPlan(hosts={}, slots={}, config=empty, signal=None, dry_run=True)
        plan.continuation_dry_run = True
        route, arg = kbd._route_row(conn, row, plan, assignee, {}, None, None, None,
                                    caps=kbd._capability_tick(empty),
                                    binds=kbd._workspace_binds(conn, [row], dry_run=True), board=board)
        if route == "wait":
            return set(), f"wait: {arg}"
        if route != "local":
            return set(), f"{route} {arg or ''} and no pool plan is possible".replace("  ", " ")
        return {LOCAL}, "local (no worker pool configured)"
    plan = kwp.SpilloverPlan(hosts={h.name: h for h in pool.pool_hosts}, slots={}, config=pool,
                             signal=signal, dry_run=True)
    plan.continuation_dry_run = True
    caps = kbd._capability_tick(pool)
    caps_out: dict = {}
    wt_out: dict = {}
    route, arg = kbd._route_row(conn, row, plan, assignee, {}, None, None, None,
                                caps=caps, caps_out=caps_out, binds=kbd._workspace_binds(conn, [row], dry_run=True),
                                worktree_out=wt_out, board=board)
    if route == "wait":
        return set(), f"wait: {arg}"
    if route == "local":
        return {LOCAL}, f"local{f' ({arg})' if arg else ''}"
    # A draining host takes no new work, override or not (Prism c477ed3abe8a).
    serves = {n for n, h in plan.hosts.items() if assignee in h.profiles and h.enabled
              and getattr(h, "state", "active") == "active" and plan.board_refusal(n, board) is None}
    if route in ("pin", "origin") and wt_out.get(row["id"]) is not None:
        # admit never ports a worktree (Prism 5eb6b21cd4cc / 80052f93b9ce).
        return set(), f"{route} {arg}: a worktree card on a pool host needs the dispatcher's port; admit runs worktrees local only"
    if route in ("pin", "origin"):
        host = {n.lower(): n for n in plan.hosts}.get(str(arg).lower(), arg)
        if host not in serves:
            return set(), f"{route} {arg}: host does not serve {assignee} or cannot host this board"
        return {host}, f"{route} {host}"
    # portable
    cap_only = caps_out.get("only", {}).get(row["id"])
    if wt_out.get(row["id"]) is not None:
        # admit never ports a worktree: that clone is the dispatcher's job.
        return ({LOCAL} if not cap_only else set()), "portable worktree (admit runs it local only)"
    only, _job = kbd._portable_only(conn, row, assignee, plan, caps, cap_only, None)
    remote = serves if only is None else {n for n in serves if n.lower() in {o.lower() for o in only}}
    return remote | (set() if cap_only else {LOCAL}), "portable"


def browser_refusal(conn, row, host: str, assignee: str) -> Optional[str]:
    """The dispatcher's browser resolution gate for this card on ``host`` (dry,
    no lease join): None when the card has no browser need or routes legacy;
    otherwise the refusal / wait it would hit. admit takes no browser lease, so a
    lease plan refuses too (Prism ea7a86c55e01 / 5a903db1ddcf). A gate fault refuses."""
    route = {row["id"]: ("pin", host) if host != LOCAL else ("local", None)}
    result = kb.DispatchResult()
    try:
        kbd._browser_gate(conn, [row], result, route, True, assignee, park=False, join=False)
    except Exception as exc:
        return f"browser gate error: {exc}"
    got = route[row["id"]]
    if got[0] == "wait":
        return f"browser admission refuses on {host}: {got[1]}"
    return None


# -- measurement --------------------------------------------------------------

def measure_local() -> Measure:
    """This host: load1, threads and mem_reserve_gb from its placement-table row
    (``local`` alias; os.cpu_count() / 0 GB without one), memory from psutil, and
    the LoadGate band + allowance the admission overrides."""
    from hermes_cli import kanban_load_gate as klg

    ctx = klg.utilization_context()
    threads, reserve = os.cpu_count(), 0.0
    try:
        from hermes_cli import placement as pl
        if (pl.fleet_root() / pl.TABLE_REL).exists():
            # A table that exists but cannot be read is an unknown threshold,
            # never "no reserve": the floor refuses on NaN (Prism a6c5f61c871f).
            table = pl.load_hosts()
            hid = table.resolve(klg.UTIL_HOST_ALIAS)
            if hid:
                threads, reserve = table.hosts[hid].threads, float(table.hosts[hid].mem_reserve_gb)
    except Exception:
        reserve = float("nan")
    load1, _ = klg.sample_loadavg()
    gate = klg.gate_from_config(None)
    try:
        allowance, paused = gate.admit_now(running=None, cpu_block=0.25)
    except Exception as exc:
        allowance, paused = None, f"gate error: {exc}"
    return Measure(LOCAL, load1, threads, ctx.get("mem_available_gb"), reserve, extra={
        "band": gate.band, "gate_state": getattr(gate, "state", None), "spill_reason": gate.spill_reason,
        "gate_allowance": allowance, "spawn_paused": paused, "admission": gate.admission,
    })


def measure_remote(host, signal) -> Measure:
    """One fresh probe of pool host ``host``: load1/ncpu plus, with the target
    signal, its pressure file (memory, band); threads + mem_reserve_gb from its
    placement-table row (ncpu / 0 GB without one); hot streak from the state file."""
    from hermes_cli import kanban_headroom as kh
    from hermes_cli import kanban_worker_pool as kwp

    kw = {"pressure_path": signal.policy.pressure_path} if signal is not None else {}
    sample = kwp.probe_host(host, **kw)
    load1 = ncpu = mem = None
    band = pressure = None
    if isinstance(sample, kwp.HostSample):
        load1, ncpu = sample.load1, sample.ncpu
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
    threads, reserve = ncpu, 0.0
    table = signal.placement_table if signal is not None else None
    if table is not None:
        hid = table.resolve(host.name)
        if hid:
            threads, reserve = table.hosts[hid].threads, float(table.hosts[hid].mem_reserve_gb)
    hot = None
    if signal is not None:
        st = kwp._load_state(signal.state_path).get(host.name)
        hot = bool(st.get("hot")) if isinstance(st, dict) else None
    return Measure(host.name, load1, threads, mem, reserve, extra={
        "band": band, "hot": hot, "pressure": pressure or None, "ncpu": ncpu,
    })


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
    db_path = kb.kanban_db_path(board=board)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    with kbc.connect_closing(board=board) as conn:
        while True:
            with kbc._dispatch_tick_lock(db_path) as held:
                if held:
                    return _admit_locked(conn, task_id, host, reason.strip(), pool, signal, board,
                                         spawn_fn, measure_local_fn or measure_local,
                                         measure_remote_fn or measure_remote,
                                         canon=lambda w: canonical_host(w, pool, fleet_dir))
            if time.monotonic() >= deadline:
                raise AdmitRefused(f"{task_id}: board tick lock busy for {_LOCK_WAIT_SECONDS:.0f}s; retry")
            time.sleep(0.2)


def _admit_locked(conn, task_id, host, reason, pool, signal, board, spawn_fn,
                  measure_local_fn, measure_remote_fn, canon: Callable[[str], str] = lambda w: w) -> dict:
    from hermes_cli import kanban_headroom as kh

    task = kb.get_task(conn, task_id)
    if task is None:
        raise AdmitRefused(f"{task_id}: no such card")
    if task.status == "running" or (task.current_run_id is not None and task.claim_lock):
        raise AdmitRefused(f"{task_id}: already running on {_running_host(conn, task, canon)}")
    if task.status != "ready" or task.claim_lock:
        raise AdmitRefused(f"{task_id}: status is {task.status}, admit takes a ready card only")
    if getattr(task, "no_worker", False):
        raise AdmitRefused(f"{task_id}: operator-only card (no_worker), never spawned")
    assignee = task.assignee
    exists = kbd._profile_exists_fn()
    if not assignee or (exists is not None and not exists(assignee)):
        raise AdmitRefused(f"{task_id}: assignee {assignee or '-'} is not a spawnable profile here")
    row = conn.execute(
        "SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker, priority, "
        "origin_host, workspace_of, branch_name FROM tasks WHERE id = ?", (task_id,)).fetchone()
    hosts, why = eligible_hosts(conn, row, pool, signal, assignee, board)
    if host not in hosts:
        raise AdmitRefused(f"{task_id}: {host} is not eligible for this card ({why}); "
                           f"eligible: {', '.join(sorted(hosts)) or 'none'}")
    browser = browser_refusal(conn, row, host, assignee)
    if browser is not None:
        raise AdmitRefused(f"{task_id}: {browser}")
    pool_host = None if host == LOCAL else {h.name: h for h in pool.pool_hosts}[host]
    m = measure_local_fn() if pool_host is None else measure_remote_fn(pool_host, signal)
    danger = floor_refusal(m)
    if danger is not None:
        raise AdmitRefused(f"{task_id}: refused on measured danger: {danger}")
    payload = {
        "source": "cli admit", "actor": _actor(), "host": host, "reason": reason, "route": why,
        "load1": None if m.load1 is None else round(m.load1, 2), "threads": m.threads,
        "load_limit": None if not m.threads else m.threads * LOAD_FACTOR,
        "mem_available_gb": None if m.mem_available_gb is None else round(m.mem_available_gb, 2),
        "mem_reserve_gb": m.mem_reserve_gb, **m.extra,
    }
    return _spawn(conn, task_id, host, pool_host, payload, signal, board, spawn_fn, kh)


def _spawn(conn, task_id, host, pool_host, payload, signal, board, spawn_fn, kh) -> dict:
    from hermes_cli import kanban_pause_hold as kph
    try:
        claimed = kb.claim_task(conn, task_id)
    except kph.PauseHeld as exc:
        raise AdmitRefused(f"{task_id}: fleet pause hold: {exc.reason}") from exc
    if claimed is None:
        raise AdmitRefused(f"{task_id}: not claimable (claimed by a tick meanwhile?)")
    rid = None
    plan = None
    if pool_host is not None and signal is not None and signal.placement_table is not None:
        # The normal reservation row, so later admissions see this worker
        # (book_pin: the same booking a pinned take writes, no band check).
        from hermes_cli import kanban_worker_pool as kwp
        plan = kwp.SpilloverPlan(hosts={host: pool_host}, slots={host: 0}, signal=signal)
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            rid = kh.book_pin(plan, host, task_id, kh.job_for_row(conn, row))
        except Exception as exc:
            kbd._record_task_failure(conn, claimed.id, f"admit: reservation: {exc}", outcome="spawn_failed",
                                     release_claim=True, end_run=True, expected_run_id=claimed.current_run_id)
            raise AdmitRefused(f"{task_id}: reservation failed: {exc}") from exc
    launch = kbd._LaunchState()
    try:
        from hermes_cli.kanban_workspace_policy import (
            WorkspaceUnavailable, validate_mount, validate_persisted, validate_target,
        )
        try:
            # The dispatcher's own workspace admission (kanban_db_dispatch, ready
            # path): protected root, mount, target and persisted checks.
            protected = kb._validate_workspace_admission(claimed, board=board, conn=conn)
            if claimed.workspace_kind == "worktree":
                workspace, branch = kbw._resolve_worktree_workspace(claimed, board=board)
            else:
                workspace, branch = kbw.resolve_workspace(claimed, board=board), None
            if protected is not None:
                validate_mount(protected.root, expected_mount=protected.mount_path)
                validate_target(protected.root, workspace)
                validate_persisted(workspace)
        except WorkspaceUnavailable as exc:
            raise _WorkspaceRefused(str(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"workspace: {exc}") from exc
        kbw.set_workspace_path(conn, claimed.id, str(workspace))
        if claimed.workspace_kind == "worktree":
            kbw.set_branch_name(conn, claimed.id, branch or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
        spawn = spawn_fn if spawn_fn is not None else kbd._default_spawn
        kwargs: dict = {}
        if kbd._accepts_kw(spawn, "board"):
            kwargs["board"] = board
        if pool_host is not None:
            if not kbd._accepts_kw(spawn, "placement"):
                raise RuntimeError("worker-host placement: spawn_fn takes no placement")
            kwargs["placement"] = pool_host
        elif kbd._accepts_kw(spawn, "placement"):
            kwargs["placement"] = None
        if kbd._accepts_kw(spawn, "on_popen"):
            kwargs["on_popen"] = lambda proc: setattr(launch, "proc", proc)
        with kbc.write_txn(conn):
            kb._append_event(conn, claimed.id, EVENT, payload, run_id=claimed.current_run_id)

        def after_launch(pid):
            if pool_host is None:
                return
            kb._append_event(conn, claimed.id, kb._kwh.PLACED_EVENT, {
                "host": host, "target": pool_host.target, "workspace": str(workspace),
                "reason": "admission_override", "band": payload.get("band"), "spill_reason": None,
                "probe": {"load1": payload.get("load1"), "ncpu": payload.get("ncpu")},
                "local_gate": None, "pid": pid,
                **({kh.RESERVATION_KEY: rid} if rid else {}),
            }, run_id=claimed.current_run_id)

        gate = {"admission_override": True, "band": payload.get("band"),
                "spawn_paused": payload.get("spawn_paused")}
        outcome = kbd._launch_claimed(
            conn, claimed, str(workspace), board=board, spawn=spawn, spawn_kwargs=kwargs,
            host=host if pool_host is not None else None, pool=None, state=launch,
            after_launch=after_launch,
            placement_record=lambda: kbd._placement_payload(
                run_id=claimed.current_run_id, route="pin" if pool_host is not None else "local",
                arg=host, rule="admission_override", placed_host=pool_host, plan=None,
                entry="admit", gate=gate, take_refusal=None),
        )
    except _WorkspaceRefused as exc:
        # Nothing launched: release the claim exactly as the dispatcher does
        # (workspace_refused run, no failure count), refund the reservation.
        if rid:
            kh.release_id(signal, rid)
        from types import SimpleNamespace
        kb._release_claim_for_workspace_refusal(
            conn, claimed.id, SimpleNamespace(workspace_refused=[], stranded_by_mount_loss=[]), str(exc))
        raise AdmitRefused(f"{task_id}: workspace unavailable: {exc}") from exc
    except Exception as exc:
        kbd._stop_own_spawn(launch.proc)
        if launch.launched and launch.pid:
            kb._abort_lost_claim_spawn(conn, claimed, int(launch.pid))
        if rid and not launch.launched:
            kh.release_id(signal, rid)
        kbd._record_task_failure(conn, claimed.id, f"admit: {exc}", outcome="spawn_failed",
                                 release_claim=True, end_run=True, expected_run_id=claimed.current_run_id)
        raise AdmitRefused(f"{task_id}: spawn failed: {exc}") from exc
    if outcome != kbd._LAUNCH_OK:
        if rid and not launch.launched:
            kh.release_id(signal, rid)
        raise AdmitRefused(f"{task_id}: run lost the card ({outcome})")
    return {"task_id": task_id, "host": host, "pid": launch.pid, "reservation": rid, "payload": payload}


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
