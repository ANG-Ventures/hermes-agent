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

from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_worker_pool as kwp

logger = logging.getLogger("gateway.kanban_watchers")

WARN_INTERVAL_SECONDS = 300
_REMOTE_PIN_SQL = (
    "SELECT body FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
    "AND body LIKE '%host:%'"
)


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
    if not ledger or any(t is None for t, _ in ledger.values()):
        # No board read, or ANY board unreadable: the local count is unknown
        # and the gate keeps its floor (never over-admit on an undercount).
        return None, {h: n for _t, hosts in ledger.values() for h, n in hosts.items()} if ledger else {}
    total = sum(t for t, _ in ledger.values())
    by_host: Dict[str, int] = {}
    for _t, hosts in ledger.values():
        for h, n in hosts.items():
            by_host[h] = by_host.get(h, 0) + int(n)
    local = total - sum(by_host.values())
    if local < 0:
        logger.info("kanban load gate: running_local clamped (%d, %d)", total, sum(by_host.values()))
        local = 0
    return local, by_host


def any_remote_pin(boards, connect, known_hosts=()) -> bool:
    """A ready card on any board pins a KNOWN pool host (``host:<id>``)."""
    for b in boards:
        slug = (b.get("slug") if isinstance(b, dict) else None) or "default"
        conn = None
        try:
            conn = connect(board=slug)
            for (body,) in conn.execute(_REMOTE_PIN_SQL).fetchall():
                pin, _ignored = kwp.resolve_pin(body, known_hosts)
                if pin is not None and pin not in (kwp.PIN_ANY, kwp.PIN_STUDIO):
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

    def build(self, boards) -> GateTick:
        gate = self.gate
        running_local, remote_by_host = running_split(self._ledger(boards))
        allowance, paused = gate.admit_now(running=running_local if gate.enabled else None)
        if gate.enabled and allowance is None and klg.loadavg_supported():
            # Enabled gate, load1 unreadable on a host that has the sampler:
            # fail CLOSED locally (RC-5c). No sampler at all (Windows): the
            # gate has no signal, admission stays unrestricted as before.
            allowance, paused = 0, "gate_unreadable"
        plan = self.plan_pool(boards, remote_by_host)
        return GateTick(band=gate.band, spill_reason=gate.spill_reason,
                        local_allowance=allowance, spawn_paused=paused, remote_plan=plan)

    def plan_pool(self, boards, remote_by_host: Dict[str, int]) -> Optional[kwp.SpilloverPlan]:
        """The ONE pool plan for this tick, or None. Runs iff
        ``kanban.worker_pool.enabled`` AND ``remote_allowed`` AND (band in
        {spilling, paused} OR a ready card carries a remote pin)."""
        gate = self.gate
        cfg = self._kanban_cfg() or {}
        pool_cfg = cfg.get("worker_pool")
        pool_cfg = pool_cfg if isinstance(pool_cfg, dict) else {}
        if pool_cfg.get("enabled", True) is False:
            gate.pool = {"planned": False, "reason": "config_disabled"}
            self._log(logger.info, "kanban pool: disabled by config")
            return None
        if not gate.remote_allowed:
            gate.pool = {"planned": False, "reason": "proc_paused"}
            return None
        pins_only = gate.band not in ("spilling", "paused")
        if pins_only and not any_remote_pin(boards, self._connect,
                                            kwp.known_host_ids(self._fleet_dir())):
            gate.pool = {"planned": False, "reason": "not_needed"}
            return None
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
        plan = kwp.plan(list(pool.pool_hosts), remote_by_host,
                        probe=self._probe or kwp.probe_host,
                        disabled=pool.disabled, config=pool)
        plan.band, plan.spill_reason, plan.pins_only = gate.band, gate.spill_reason, pins_only
        gate.pool = plan.snapshot()
        return plan


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

