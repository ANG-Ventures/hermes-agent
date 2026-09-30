"""Kanban worker hosts: spill tool execution onto a second machine.

Config: ``kanban.worker_hosts`` in config.yaml (default: empty = feature off).

One board, one dispatcher. When the dispatcher host's load gate
(``kanban.dispatch_load_gate``) pauses spawns, a configured worker host may
take NEW workers instead. A spilled worker is claimed and spawned exactly like
a local one (same process, same pid-anchored owner grant, same kanban tools on
the same ``kanban.db``); only its terminal and file tools run on the worker
host, over the ``ssh`` terminal backend. The agent loop itself stays on the
dispatcher host: measured 2026-09-29 19:34 PT on the Studio, 44 worker agent
processes used 100% CPU in total (about 2.3% of a core each) out of 958%, so
the load the gate protects against is the tools, not the loop.

Eligibility is deliberately narrow: only ``scratch`` cards (their workspace is
a plain directory, created at the same absolute path on the worker host) and
only assignees on the host's ``profiles`` allowlist.

Example::

    kanban:
      worker_hosts:
        - name: ace-ai
          ssh_host: ace-ai
          ssh_user: kanbanw      # locked user: no sudo, no docker, no GPU devices
          max_workers: 4
          pause_above: 18        # remote load1; no new placement above it
          profiles: [daedalus-opus]

The worker process receives ``KANBAN_WORKER_PLACEMENT`` (internal bridge,
JSON of ``TERMINAL_*`` values). ``tools.terminal_tool`` re-applies it after
the config.yaml -> env bridge, so a profile's ``terminal.backend: local``
cannot silently pull a spilled worker back onto the dispatcher host.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, MutableMapping, Optional, Sequence

PLACEMENT_ENV = "KANBAN_WORKER_PLACEMENT"
PLACED_EVENT = "worker_placed"

_PROBE_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class WorkerHost:
    name: str
    ssh_host: str
    ssh_user: str
    max_workers: int
    pause_above: float
    profiles: tuple = ()
    ssh_port: int = 22

    @property
    def target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}"


def parse_worker_hosts(cfg: Any) -> List[WorkerHost]:
    """Parse ``kanban.worker_hosts``; invalid or disabled entries are dropped."""
    if not isinstance(cfg, list):
        return []
    hosts: List[WorkerHost] = []
    seen = set()
    for raw in cfg:
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        name = str(raw.get("name") or "").strip()
        ssh_host = str(raw.get("ssh_host") or "").strip()
        ssh_user = str(raw.get("ssh_user") or "").strip()
        if not name or not ssh_host or not ssh_user or name in seen:
            continue
        try:
            max_workers = int(raw.get("max_workers", 0))
            pause_above = float(raw.get("pause_above", 0))
            port = int(raw.get("ssh_port", 22))
        except (TypeError, ValueError):
            continue
        profiles = raw.get("profiles") or []
        if not isinstance(profiles, list):
            continue
        profiles_t = tuple(str(p).strip() for p in profiles if str(p).strip())
        if max_workers <= 0 or pause_above <= 0 or not profiles_t:
            continue
        seen.add(name)
        hosts.append(WorkerHost(name, ssh_host, ssh_user, max_workers,
                                pause_above, profiles_t, port))
    return hosts


def _ssh_argv(host: WorkerHost, remote_cmd: str) -> List[str]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4"]
    if host.ssh_port != 22:
        argv += ["-p", str(host.ssh_port)]
    return argv + [host.target, remote_cmd]


def probe_load1(host: WorkerHost, runner: Callable = subprocess.run) -> Optional[float]:
    """Remote load1, or None when the host cannot be read (fail closed)."""
    try:
        proc = runner(
            _ssh_argv(host, "cat /proc/loadavg"),
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        return float((proc.stdout or "").split()[0])
    except (IndexError, ValueError):
        return None


def running_by_host(conn) -> Dict[str, int]:
    """Count running tasks whose CURRENT run was placed on each worker host."""
    rows = conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
        "WHERE t.status = 'running' AND e.kind = ? "
        "AND e.run_id IS NOT NULL AND e.run_id = t.current_run_id",
        (PLACED_EVENT,),
    ).fetchall()
    counts: Dict[str, int] = {}
    for row in rows:
        try:
            name = json.loads(row[0] or "{}").get("host")
        except (TypeError, ValueError):
            continue
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


@dataclass
class SpilloverPlan:
    """Per-tick capacity on the worker hosts, consumed by :meth:`take`."""

    slots: Dict[str, int]
    hosts: Dict[str, WorkerHost]
    detail: Dict[str, dict] = field(default_factory=dict)

    @property
    def budget(self) -> int:
        return sum(self.slots.values())

    def eligible(self, assignee: Optional[str], workspace_kind: Optional[str]) -> bool:
        if (workspace_kind or "scratch") != "scratch" or not assignee:
            return False
        return any(self.slots.get(n, 0) > 0 and assignee in h.profiles
                   for n, h in self.hosts.items())

    def take(self, assignee: Optional[str]) -> Optional[WorkerHost]:
        """Reserve one slot on the host with the most free slots."""
        best = None
        for name, host in self.hosts.items():
            if self.slots.get(name, 0) <= 0 or assignee not in host.profiles:
                continue
            if best is None or self.slots[name] > self.slots[best.name]:
                best = host
        if best is not None:
            self.slots[best.name] -= 1
        return best

    def summary(self) -> str:
        parts = []
        for name, d in self.detail.items():
            parts.append(
                f"{name}: load1={d.get('load1')} running={d.get('running')}/"
                f"{d.get('max_workers')} slots={d.get('slots')}"
            )
        return "; ".join(parts)


def plan_spillover(
    conn,
    hosts: Sequence[WorkerHost],
    *,
    probe: Callable[[WorkerHost], Optional[float]] = probe_load1,
) -> Optional[SpilloverPlan]:
    """Capacity on every worker host for this tick; None when there is none."""
    if not hosts:
        return None
    counts = running_by_host(conn)
    slots: Dict[str, int] = {}
    detail: Dict[str, dict] = {}
    for host in hosts:
        running = counts.get(host.name, 0)
        free = max(0, host.max_workers - running)
        load1 = probe(host) if free else None
        if load1 is None or load1 > host.pause_above:
            free = 0
        slots[host.name] = free
        detail[host.name] = {"load1": load1, "running": running,
                             "max_workers": host.max_workers, "slots": free}
    plan = SpilloverPlan(slots, {h.name: h for h in hosts}, detail)
    return plan


def placement_env(host: WorkerHost, workspace: str) -> Dict[str, str]:
    """The ``TERMINAL_*`` values a spilled worker's tools must run under."""
    values = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": host.ssh_host,
        "TERMINAL_SSH_USER": host.ssh_user,
        "TERMINAL_SSH_PORT": str(host.ssh_port),
        "TERMINAL_CWD": workspace,
    }
    return values


def apply_placement(env: Dict[str, str], host: WorkerHost, workspace: str) -> Dict[str, str]:
    values = placement_env(host, workspace)
    env.update(values)
    env[PLACEMENT_ENV] = json.dumps({"host": host.name, "env": values}, sort_keys=True)
    return values


def prepare_remote_workspace(host: WorkerHost, workspace: str,
                             runner: Callable = subprocess.run) -> None:
    """Create the card's workspace at the same absolute path on the host."""
    proc = runner(
        _ssh_argv(host, f"mkdir -p -- {shlex.quote(workspace)}"),
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(
            f"worker host {host.name}: mkdir {workspace} failed: "
            f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}"
        )


def reapply_placement_env(environ: Optional[MutableMapping[str, str]] = None) -> Optional[str]:
    """Re-assert a dispatcher placement over config-bridged ``TERMINAL_*``.

    Returns the host name when a placement was applied, else None.
    """
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    raw = env.get(PLACEMENT_ENV)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        values = data.get("env") or {}
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(values, dict):
        return None
    for key, value in values.items():
        if isinstance(key, str) and key.startswith("TERMINAL_") and isinstance(value, str):
            env[key] = value
    return str(data.get("host") or "") or None
