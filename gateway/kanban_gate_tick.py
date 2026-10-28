"""The gateway dispatcher's per-tick gate contract (KWLB v0.1, PRD 5.2.3).

ONE ``GateTick`` per gateway tick, computed before the board loop and passed
to every board: the Studio band, the LOCAL allowance (an int whenever the
gate is enabled, 0 when paused or ``load1`` is unreadable) and ONE shared
``SpilloverPlan`` (or None). The Studio cost model sees LOCAL workers only
(PRD 5.2.4): ``observe(running_local)`` here, ``record_spawns(local)`` after
the board loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from hermes_cli import kanban_admission as kadm
from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import placement_ledger as pledger
from hermes_cli import placement_policy as ppolicy

logger = logging.getLogger("gateway.kanban_watchers")

WARN_INTERVAL_SECONDS = 300
_REMOTE_PIN_SQL = (
    "SELECT body FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
    "AND body LIKE '%host:%'"
)
_ORIGIN_SQL = ("SELECT 1 FROM tasks WHERE origin_host IS NOT NULL "
               "AND status IN ('ready','running','review') LIMIT 1")
_ORIGIN_PATHS_SQL = ("SELECT DISTINCT origin_host, workspace_path FROM tasks "
                     "WHERE origin_host IS NOT NULL AND status NOT IN ('done','archived')")
_READY_SQL = "SELECT 1 FROM tasks WHERE status = 'ready' AND claim_lock IS NULL LIMIT 1"
# no_worker=1 (operator-only) cards are dropped before spawn: never published as demand (Prism 8d48747ae751)
_READY_COUNT_SQL = ("SELECT COUNT(*) FROM tasks WHERE status = 'ready' AND claim_lock IS NULL"
                    " AND COALESCE(no_worker, 0) = 0")
# Cards dispatch_once itself makes ready this tick, before its ready loop: a
# timed wake that is due (wake_due_scheduled) and a todo card whose blocks
# parents are all terminal (recompute_ready). An open ``after-merge`` parent
# counts as waking on purpose: the same tick may release it (PR merged), and
# an extra plan costs less than a released child placed with none.
_WAKING_SQL = (
    "SELECT 1 FROM tasks t WHERE (t.status = 'scheduled' AND t.next_eligible_at IS NOT NULL "
    "AND t.next_eligible_at <= CAST(strftime('%s','now') AS INTEGER)) "
    "OR (t.status = 'todo' AND NOT EXISTS (SELECT 1 FROM task_links l JOIN tasks p "
    "ON p.id = l.parent_id WHERE l.child_id = t.id AND COALESCE(l.kind,'blocks') = 'blocks' "
    "AND p.status NOT IN ('done','archived'))) LIMIT 1"
)


def admitting_overflow(pool_cfg) -> bool:
    """``kanban.worker_pool.overflow`` (default true, t_7b1eca07): in the
    ``admitting`` band a portable card that finds no local slot goes to the
    pool. False = remote pins only while admitting (PRD 5.2.5, before)."""
    return not (isinstance(pool_cfg, dict) and pool_cfg.get("overflow", True) is False)


@dataclass(frozen=True)
class GateTick:
    band: str
    spill_reason: Optional[str]
    local_allowance: Optional[int]   # None ONLY when the gate is disabled
    spawn_paused: Optional[str]
    remote_plan: Optional[kwp.SpilloverPlan]


class _Every5Min:
    """One log line per message per 5 minutes."""

    def __init__(self) -> None:
        self._at: Dict[str, float] = {}

    def __call__(self, level: Callable, msg: str) -> None:
        now = time.monotonic()
        last = self._at.get(msg)
        if last is None or now - last >= WARN_INTERVAL_SECONDS:
            self._at[msg] = now
            level("%s", msg)


def running_split(ledger: Dict[str, Tuple[int, Dict[str, int]]]
                  ) -> Tuple[Optional[int], Dict[str, int]]:
    """``(running_local, running_remote_by_host)`` from ONE ledger pass.

    ``running_local = max(0, total - remote)``; None when no board was read
    (the gate then keeps plain floor(), as with an unknown count).
    """
    by_host: Dict[str, int] = {}
    for _t, hosts in (ledger or {}).values():
        for h, n in hosts.items():
            by_host[h] = by_host.get(h, 0) + int(n)
    if not ledger or any(t is None for t, _ in ledger.values()):
        # No board read, or ANY board unreadable: the local count is unknown
        # and the gate keeps its floor (never over-admit on an undercount).
        # Remote counts are still SUMMED across boards (Prism r10 :60).
        return None, by_host
    total = sum(t for t, _ in ledger.values())
    local = total - sum(by_host.values())
    if local < 0:
        logger.info("kanban load gate: running_local clamped (%d, %d)", total, sum(by_host.values()))
        local = 0
    return local, by_host


def read_signal_enabled(kanban_cfg) -> bool:
    """``kanban.placement.read_signal`` (default true): false = KWLB v0.1."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, dict) else None
    return not (isinstance(pl, dict) and pl.get("read_signal", True) is False)


def any_origin(boards, connect) -> bool:
    """A live (ready, running, review) card on any board carries an
    ``origin_host`` (t_8bfdcc07)."""
    return _any_board(boards, connect,
                      lambda conn: conn.execute(_ORIGIN_SQL).fetchone() is not None)


def any_remote_pin(boards, connect, known_hosts=(), aliases=None) -> bool:
    """A ready card on any board pins a KNOWN pool host (``host:<id>`` or a
    registry alias of one, t_cb2b888d)."""
    return _any_board(boards, connect, lambda conn: any(
        (pin := kwp.resolve_pin(body, known_hosts, aliases)[0]) is not None
        and pin not in (kwp.PIN_ANY, kwp.PIN_STUDIO)
        for (body,) in conn.execute(_REMOTE_PIN_SQL).fetchall()))


def origin_workspaces(boards, connect) -> Dict[str, list]:
    """host -> workspace paths of every live card holding continuation state
    on it, across boards (t_8bfdcc07). The pool probe reads their state."""
    out: Dict[str, list] = {}

    def collect(conn) -> bool:
        for host, path in conn.execute(_ORIGIN_PATHS_SQL).fetchall():
            if host and path and path not in out.setdefault(host, []):
                out[host].append(path)
        return False

    _any_board(boards, connect, collect)
    return out


def any_ready(boards, connect) -> bool:
    """A ready, unclaimed card on any board: overflow candidates exist."""
    return _any_board(boards, connect,
                      lambda conn: conn.execute(_READY_SQL).fetchone() is not None)


def any_waking(boards, connect) -> bool:
    """A card dispatch_once will make ready this tick (a due timed wake or a
    promotable todo) on any board: under ``placement.default: remote`` it
    needs the plan, or it is placed local-first with no plan."""
    return _any_board(boards, connect,
                      lambda conn: conn.execute(_WAKING_SQL).fetchone() is not None)


def _any_board(boards, connect, test) -> bool:
    for b in boards:
        slug = (b.get("slug") if isinstance(b, dict) else None) or "default"
        conn = None
        try:
            conn = connect(board=slug)
            if test(conn):
                return True
        except Exception:
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return False


_OUT_OF_TICK_SQL = (
    "SELECT r.id, r.started_at, r.claim_lock FROM task_runs r WHERE r.started_at >= ? "
    "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id = r.id AND e.kind = ?)")


def out_of_tick_local_starts(boards, connect, since: float, own_claimer: str) -> list:
    """``[(key, started_at)]`` of every LOCAL run (no worker_placed event)
    started at/after ``since`` on any board that THIS process did not claim
    (``task_runs.claim_lock`` != ``own_claimer``): a ``kanban admit``, a
    one-shot ``kanban dispatch`` or another dispatcher's spawn (t_32e8796a).
    The same rows ``kanban_ops._recent_local_starts`` books for the CLI path,
    minus this process's own spawns, which ``finish_tick`` already booked.
    ``key`` is ``(db path, run id)``. An unreadable board contributes nothing."""
    from hermes_cli import kanban_db as _kb

    out, seen = [], set()
    for b in boards or ():
        slug = (b.get("slug") if isinstance(b, dict) else None) or "default"
        conn = None
        try:
            path = str(_kb.kanban_db_path(slug).expanduser().resolve())
            if path in seen:
                continue
            seen.add(path)
            conn = connect(board=slug)
            for rid, started, lock in conn.execute(
                    _OUT_OF_TICK_SQL, (int(since), kwp.PLACED_EVENT)).fetchall():
                if lock != own_claimer:
                    out.append(((path, int(rid)), float(started)))
        except Exception:
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return out


class GateTickBuilder:
    """Builds the tick's ``GateTick``. Boards tick sequentially in ONE thread
    after this (PRD I-12): the shared plan's ledger is unlocked."""

    def __init__(self, load_gate, *, fleet_dir: Callable, kanban_cfg: Callable,
                 ledger: Callable, connect: Callable, probe: Optional[Callable] = None) -> None:
        self.gate = load_gate
        self._fleet_dir = fleet_dir
        self._kanban_cfg = kanban_cfg
        self._ledger = ledger
        self._connect = connect
        self._probe = probe
        self._log = _Every5Min()
        self._remote_by_host: Dict[str, int] = {}
        self.running_local: Optional[int] = None   # last build()'s local count
        self._signal: Optional[kwp.TargetSignal] = None
        self._boards = None                        # last build()'s boards (renewal)
        self._renewed_at: float = 0.0
        self._seeded: Dict[tuple, float] = {}      # out-of-tick local runs already booked

    def _root(self):
        return self._fleet_dir().parent

    def target_signal(self, cfg) -> Optional[kwp.TargetSignal]:
        """The Phase 1b pressure reader, or None (read_signal false)."""
        if not read_signal_enabled(cfg):
            return None
        root = self._root()
        cost = getattr(self.gate, "cost", None)
        from hermes_cli import kanban_headroom as kh
        from hermes_cli import placement

        return kwp.TargetSignal(
            policy=ppolicy.load(self._fleet_dir()),
            state_path=root / "var" / kwp.TARGET_STATE_FILE,
            ledger_dir=pledger.ledger_dir(root),
            cpu_est=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            admit_on_memory=kadm.enabled(cfg),
            reservation_gb=kadm.reservation_gb(cfg),
            placement_table=kh.load_table(root),
            placement_reservations=root / placement.RESERVATIONS_REL,
            allocd=kh.allocd_mode(cfg),
        )

    def build(self, boards) -> GateTick:
        gate = self.gate
        # kanban.admission (t_da6f84f7), read per tick: utilization | legacy (default)
        gate.admission = klg.admission_mode((self._kanban_cfg() or {}).get("admission"))
        running_local, remote_by_host = running_split(self._ledger(boards))
        self._remote_by_host = dict(remote_by_host)
        self._boards = boards
        self.running_local = running_local
        self.seed_out_of_tick(boards)
        self.publish_demand(boards)
        allowance, paused = gate.admit_now(running=running_local if gate.enabled else None)
        if gate.enabled and allowance is None and klg.loadavg_supported():
            # Enabled gate, load1 unreadable on a host that has the sampler:
            # fail CLOSED locally (RC-5c). No sampler at all (Windows): the
            # gate has no signal, admission stays unrestricted as before.
            allowance, paused = 0, "gate_unreadable"
        plan = self.plan_pool(boards, remote_by_host)
        return GateTick(band=gate.band, spill_reason=gate.spill_reason,
                        local_allowance=allowance, spawn_paused=paused, remote_plan=plan)

    def publish_demand(self, boards) -> Optional[int]:
        """Kanban's queue for the ONE admission rule (t_3694ab3c): the ready, unclaimed cards on every board ->
        <root>/var/placement/demand/kanban.json, so a lower class (ci, test-gate, batch) leaves kanban room while
        it waits. Off under KANBAN_UNIFIED_ADMIT=0. Returns the count published, or None (off, or the write failed:
        logged, and the next tick publishes again; Prism 8397e82995e1)."""
        if not klg.unified_admit():
            return None
        n = 0
        for b in boards:
            slug = (b.get("slug") if isinstance(b, dict) else None) or "default"
            conn = None
            try:
                conn = self._connect(board=slug)
                n += int(conn.execute(_READY_COUNT_SQL).fetchone()[0] or 0)
            except Exception as exc:
                self._log(logger.warning, f"kanban demand: board {slug} unreadable, its ready cards not counted: "
                                          f"{type(exc).__name__}")
                continue
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        err = klg.publish_ready_demand(n, root=self._root())
        if err:
            self._log(logger.warning, f"kanban demand: publish of {n} ready card(s) failed, retried next tick: {err}")
            return None
        return n

    def seed_out_of_tick(self, boards, *, now_wall: Optional[float] = None,
                         now_mono: Optional[float] = None) -> int:
        """Book every LOCAL run started inside ``admission.DEFAULT_SETTLE_S``
        that this process did not spawn into the gate's ramp, once each, aged
        by its ``started_at`` (as the CLI path does, kanban_ops
        ``_cli_utilization_gate``). Without it a ``kanban admit`` on this host
        is invisible to the next tick until its CPU shows (t_32e8796a)."""
        gate = self.gate
        if not getattr(gate, "enabled", False):
            return 0
        from hermes_cli import admission as _ad
        from hermes_cli import kanban_db as _kb

        now_wall = time.time() if now_wall is None else now_wall
        now_mono = time.monotonic() if now_mono is None else now_mono
        horizon = now_wall - _ad.DEFAULT_SETTLE_S
        self._seeded = {k: t for k, t in self._seeded.items() if t >= horizon}
        try:
            rows = out_of_tick_local_starts(boards, self._connect, horizon, _kb._claimer_id())
        except Exception:
            return 0
        n = 0
        for key, started in rows:
            if key in self._seeded:
                continue
            self._seeded[key] = started
            gate.record_spawns(1, now=now_mono - max(0.0, now_wall - started))
            n += 1
        return n

    def plan_pool(self, boards, remote_by_host: Dict[str, int]) -> Optional[kwp.SpilloverPlan]:
        """The ONE pool plan for this tick, or None. Runs iff
        ``kanban.worker_pool.enabled`` AND ``remote_allowed`` AND (band in
        {spilling, paused} OR a ready card carries a remote pin OR, with
        ``worker_pool.overflow`` on, any card is ready: the pool is the
        overflow for portable cards past the local allowance)."""
        gate = self.gate
        cfg = self._kanban_cfg() or {}
        pool_cfg = cfg.get("worker_pool")
        pool_cfg = pool_cfg if isinstance(pool_cfg, dict) else {}
        if self.pool_switch_refusal(cfg) is not None:
            return None
        # kanban.placement.default (spec v0.3 §3.1), read per tick: remote
        # offers every portable row to the pool first, so pins_only is off.
        default_remote = kwp.placement_default(cfg) == kwp.PLACEMENT_DEFAULT_REMOTE
        pins_only = (gate.band not in ("spilling", "paused") and not default_remote
                     and not admitting_overflow(pool_cfg))
        from hermes_cli import kanban_continuation as kc
        # Live continuation origins (ready, running, review) need the plan's
        # probe whatever else is ready: HOST_LOST and origin waits read it.
        if gate.band not in ("spilling", "paused") and not (
                (kc.mode(cfg) == "enforce" and any_origin(boards, self._connect)) or (
                    (any_ready(boards, self._connect)
                     or (default_remote and any_waking(boards, self._connect)))
                    if not pins_only else
                    any_remote_pin(boards, self._connect, kwp.known_host_ids(self._fleet_dir()),
                                   kwp.pin_aliases(self._fleet_dir())))):
            gate.pool = {"planned": False, "reason": "not_needed"}
            return None
        plan = self.build_pool_plan(boards, remote_by_host, cfg)
        if plan is not None:
            plan.band, plan.spill_reason, plan.pins_only = gate.band, gate.spill_reason, pins_only
            gate.pool = plan.snapshot()
        return plan

    def pool_switch_refusal(self, cfg: dict) -> Optional[str]:
        """The operator's pool off-switches, the ONE place they live: ``config_disabled``
        (``kanban.worker_pool.enabled: false``) or ``proc_paused`` (the gate's
        ``remote_allowed`` is off), recorded on ``gate.pool``; None when the pool may
        plan. :meth:`plan_pool` and :meth:`build_pool_plan` both apply it, so a direct
        ``build_pool_plan`` caller (``kanban admit``) honours them too (Prism ddce35a5f884)."""
        gate = self.gate
        pool_cfg = (cfg or {}).get("worker_pool")
        pool_cfg = pool_cfg if isinstance(pool_cfg, dict) else {}
        if pool_cfg.get("enabled", True) is False:
            gate.pool = {"planned": False, "reason": "config_disabled"}
            self._log(logger.info, "kanban pool: disabled by config")
            return "config_disabled"
        if not gate.remote_allowed:
            gate.pool = {"planned": False, "reason": "proc_paused"}
            return "proc_paused"
        return None

    def build_pool_plan(self, boards, remote_by_host: Dict[str, int],
                        cfg: Optional[dict] = None) -> Optional[kwp.SpilloverPlan]:
        """The plan itself (pool files, signal, the real probe with
        ``kanban.workspaces_root``, continuation paths), None when the pool
        refuses or has no hosts. :meth:`plan_pool` calls it once it decided a
        plan is needed this tick; ``kanban admit`` calls it directly, since that
        decision reads the Studio band, which is host admission (t_32e8796a)."""
        gate = self.gate
        cfg = (self._kanban_cfg() or {}) if cfg is None else cfg
        if self.pool_switch_refusal(cfg) is not None:
            return None
        default_remote = kwp.placement_default(cfg) == kwp.PLACEMENT_DEFAULT_REMOTE
        from hermes_cli import kanban_continuation as kc
        pool = kwp.read_pool(self._fleet_dir(), kanban_cfg=cfg)
        for warning in pool.warnings:
            self._log(logger.warning, warning)
        if pool.refused is not None:
            # A refused pool places nothing; remote pins wait pool_unavailable.
            gate.pool = {"planned": False, "reason": f"refused:{pool.refused}",
                         "warnings": list(pool.warnings)}
            return None
        if not pool.pool_hosts and not pool.disabled:
            gate.pool = {"planned": False, "reason": "no_hosts"}
            return None
        signal = self.target_signal(cfg)
        probe = self._probe or kwp.probe_host
        if probe is kwp.probe_host:
            kw = {}
            if signal is not None:
                kw["pressure_path"] = signal.policy.pressure_path
            # The same round-trip lists the boards each host can host
            # (t_9fb4d477); without a configured root nothing is filtered.
            root = cfg.get("workspaces_root")
            if isinstance(root, str) and root.startswith("/"):
                kw["workspaces_root"] = root
            def probe(h, _kw=kw, **extra):
                return kwp.probe_host(h, **_kw, **extra)
        # Continuation state (t_8bfdcc07) rides this same round-trip.
        paths = (origin_workspaces(boards, self._connect)
                 if kc.mode(cfg) != "off" else None)
        plan = kwp.plan(list(pool.pool_hosts), remote_by_host, probe=probe,
                        disabled=pool.disabled, config=pool, signal=signal,
                        workspace_paths=paths)
        plan.default_remote = default_remote
        if signal is not None:
            # WARNING (one per message per 5 min): a host with no sampler reads
            # UNKNOWN and takes nothing; that must be visible, not info noise.
            for name, d in plan.detail.items():
                if d.get("pressure"):
                    self._log(logger.warning, f"kanban pool: {name} refused: {d['pressure']}")
            # Saturation 4.2 readback (t_31350981): every tick that caps a
            # host, dry or enforcing; an all-clear line at most every 5 min.
            # D9 (t_3694ab3c): under KANBAN_UNIFIED_ADMIT the cap is reported, never enforced (admit_class owns memory)
            line = kadm.readback_line(plan.admission, enforcing=signal.admit_on_memory and not kwp._kanban_unified_admit(signal),
                                      reservation=signal.reservation_gb)
            if plan.admission:
                logger.info("%s", line)
            else:
                self._log(logger.info, line)
            if plan.budget <= 0:
                self._log(logger.warning, f"kanban pool: {plan.full_reason()}")
        return plan

    def record_placements(self, placed) -> None:
        """Write kanban's reservations (running remote + this tick's placements)
        to ``host-reservations.kanban.json``, TTL from the policy (I-4). Runs every
        tick with read_signal on, so a live gateway keeps its key fresh."""
        cfg = self._kanban_cfg() or {}
        if not read_signal_enabled(cfg):
            return
        try:
            root = self._root()
            policy = ppolicy.load(self._fleet_dir())
            fresh: Dict[str, int] = {}
            for _task, host in placed or ():
                fresh[host] = fresh.get(host, 0) + 1
            # remote_by_host was counted BEFORE this tick's spawns.
            cost = getattr(self.gate, "cost", None)
            cpu = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0 \
                else float(policy.consumer("kanban", "cpu_est_prior") or 0.0)
            ramp = float(policy.consumer("kanban", "ramp_s") or 0.0)
            ttl = float(policy.consumer("kanban", "ttl_s") or 180.0)
            now = time.time()

            def write(old):
                return {"at": now, "ttl_s": ttl,
                        "hosts": pledger.kanban_rows(old, self._remote_by_host, fresh,
                                                     cpu_est=cpu, ramp_s=ramp, now=now)}

            pledger.update(pledger.ledger_dir(root), "kanban", write)
        except Exception as exc:
            self._log(logger.warning, f"kanban pool: ledger_write_failed: {exc}")
        self.renew_reservations()

    def renew_reservations(self, *, now: Optional[float] = None) -> Optional[Dict[str, int]]:
        """Keep every live pool run's headroom reservation from lapsing
        (Prism 6c6c0c765a1a): at most every ``RENEW_EVERY_S``, over the boards
        the last ``build()`` read. A lapsed row of a live run is logged as a
        leak. None = not due / nothing to renew (no build yet, no table)."""
        from hermes_cli import kanban_headroom as kh
        from hermes_cli import placement

        now = time.time() if now is None else now
        if self._boards is None or now - self._renewed_at < kh.RENEW_EVERY_S:
            return None
        root = self._root()
        if not kh.table_path(root).is_file():
            return None
        self._renewed_at = now
        try:
            got = kh.renew_running(self._boards, self._connect, root / placement.RESERVATIONS_REL, now=now)
        except Exception as exc:
            self._log(logger.warning, f"kanban pool: reservation renewal failed: {exc}")
            return None
        if got["lapsed"]:
            logger.warning("kanban pool: %d live run reservation(s) had lapsed before renewal", got["lapsed"])
        # The reaper (allocation 2.3): rows whose holder died without a release
        # lapse at TTL, like claim_expires; each is ledgered as ``expired``.
        try:
            got["reaped"] = placement.reap(path=root / placement.RESERVATIONS_REL, now=now)
        except placement.PlacementError as exc:
            self._log(logger.warning, f"kanban pool: reservation reap failed: {exc}")
        return got


def standalone_builder(load_gate, kanban_cfg: Callable) -> GateTickBuilder:
    """The gateway's tick path for the standalone daemon and one-shot CLI
    dispatch: same pool files, ledger and plan as ``kanban_watchers``."""
    from hermes_cli import kanban_db as _kb

    return GateTickBuilder(
        load_gate,
        fleet_dir=lambda: _kb.kanban_home() / "fleet",
        kanban_cfg=kanban_cfg,
        ledger=lambda boards: _kb.count_running_by_placement(boards),
        connect=lambda board=None: _kb.connect(board=board),
    )


def live_boards() -> list:
    from hermes_cli import kanban_db as _kb

    try:
        return _kb.list_boards(include_archived=False)
    except Exception:
        return [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]


def format_tick_line(gate, tick: GateTick, local: int, placed: list) -> str:
    """``kanban load gate: <band> (<spill_reason>: cpu <x>, load <y>) local=<n> remote=<m> -> <hosts>``."""
    cpu = "?" if gate.cpu_busy is None else f"{gate.cpu_busy:.2f}"
    load = "?" if gate.load1 is None else f"{gate.load1:.1f}"
    hosts = ",".join(sorted({h for _t, h in placed})) or "-"
    return (f"kanban load gate: {tick.band} ({tick.spill_reason or '-'}: cpu {cpu}, load {load}) "
            f"local={local} remote={len(placed)} -> {hosts}")

