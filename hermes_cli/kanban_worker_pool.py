"""Kanban worker pool: which fleet hosts take spilled workers, and which cards may go.

KWLB v0.1 (PRD ``plans/2026-10-03_kanban-worker-load-balancing-PRD.md`` §5.1-§5.3).

Hosts come from two files under the fleet root (``<kanban_home>/fleet``):

* ``fleet-roles.json``: role ``kanban-worker`` = ``{"slots": N}`` (schema 1,
  the same validators as the CI and Prism readers);
* ``kanban-pool.json``: the kanban sidecar (``priority``, ``capacity_pct``,
  ``profiles``, ``studio_bound_skills``, ``hosts.<id>.{enabled, absence,
  capacity_pct?, profiles?}``, ``ssh_user`` locked to ``kanbanw``). Written
  only by hermes-home ``scripts/fleet-host``.

Drift (PRD §5.1 table): a structural error refuses the whole pool; a per-host
disagreement drops that host with a warning. ``kanban.worker_hosts`` is
retired: present next to a ``kanban-worker`` role it refuses the pool.

``portable()`` is THE classifier (I-7): the dispatcher and hermes-home's
``kanban-placement-watch.py`` both import it. This module is stdlib-only so
the watch can import it as a top-level module.

The ``SpilloverPlan`` ledger is planned ONCE per gateway tick and shared by
every board. ``take()``/``release()`` are unlocked: correct only while boards
tick sequentially in one thread (PRD I-12, ``gateway/kanban_watchers.py``
board loop). A change that ticks boards concurrently must add a lock here.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Dict, Iterable, List, Mapping, Optional, Tuple

ROLE = "kanban-worker"
SSH_USER = "kanbanw"
ROLES_FILE = "fleet-roles.json"
SIDECAR_FILE = "kanban-pool.json"
DEFAULT_CAPACITY = 0.80
PLACED_EVENT = "worker_placed"
PROBE_TIMEOUT_SECONDS = 8

_PLAIN_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")
_ROLE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_ROLES_TOP = frozenset({"_doc", "schema", "hosts"})
_ROLES_ROW = frozenset({"roles", "state"})
_SIDECAR_TOP = frozenset({"_doc", "schema", "ssh_user", "capacity_pct", "priority",
                          "profiles", "studio_bound_skills", "hosts"})
_SIDECAR_ROW = frozenset({"enabled", "absence", "capacity_pct", "profiles"})
_ABSENCE = ("required", "optional")
_STATES = ("active", "draining")

# Card-body pin: a line that is exactly ``host:<id>`` (case and surrounding
# whitespace ignored, CRLF line endings accepted). Line-anchored so prose that
# mentions a pin is not one.
HOST_PIN_RE = re.compile(r"^[ \t]*host:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*\r?$",
                         re.IGNORECASE | re.MULTILINE)
PIN_ANY = "any"
PIN_STUDIO = "studio"
IGNORED_PIN_WARN_SECONDS = 300

_log = logging.getLogger(__name__)
_ignored_pin_warned: Dict[str, float] = {}


class PoolError(ValueError):
    """A pool file is present but structurally unusable (whole pool refused)."""


@dataclass(frozen=True)
class PoolHost:
    name: str
    ssh_host: str
    ssh_user: str
    slots: int
    capacity_pct: float
    absence: str
    profiles: Tuple[str, ...]
    state: str
    enabled: bool
    priority: int
    ssh_port: int = 22

    @property
    def target(self) -> str:
        return f"{self.ssh_user}@{self.ssh_host}"


@dataclass(frozen=True)
class PoolConfig:
    """What ``portable()`` and the planner read. ``hosts`` = every host id the
    sidecar knows (priority order), enabled or not, so a pin can name one."""

    hosts: Tuple[str, ...] = ()
    profiles: Tuple[str, ...] = ()
    studio_bound_skills: Tuple[str, ...] = ()
    pool_hosts: Tuple[PoolHost, ...] = ()
    disabled: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    refused: Optional[str] = None   # why the whole pool was refused (load_gate.json)


# -- registry ---------------------------------------------------------------

def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def _pct(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0 < value <= 1


def parse_roles(doc) -> Dict[str, Tuple[Dict[str, int], str]]:
    """``fleet-roles.json`` with the router's validators; raises PoolError."""
    if not isinstance(doc, dict) or set(doc) - _ROLES_TOP:
        raise PoolError("fleet-roles.json: bad top level")
    if isinstance(doc.get("schema"), bool) or doc.get("schema") != 1:
        raise PoolError(f"fleet-roles.json: schema must be 1, not {doc.get('schema')!r}")
    hosts = doc.get("hosts")
    if not isinstance(hosts, dict):
        raise PoolError("fleet-roles.json: hosts must be an object")
    out: Dict[str, Tuple[Dict[str, int], str]] = {}
    for hid, row in hosts.items():
        if not isinstance(hid, str) or not _PLAIN_RE.fullmatch(hid):
            raise PoolError(f"fleet-roles.json: host id {hid!r} is not a plain name")
        if not isinstance(row, dict) or set(row) != _ROLES_ROW:
            raise PoolError(f"fleet-roles.json: host {hid}: fields must be roles, state")
        roles = row["roles"]
        if not isinstance(roles, dict) or not roles:
            raise PoolError(f"fleet-roles.json: host {hid}: roles must be a non-empty object")
        parsed: Dict[str, int] = {}
        for role, spec in roles.items():
            if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
                raise PoolError(f"fleet-roles.json: host {hid}: bad role {role!r}")
            if not isinstance(spec, dict) or set(spec) != {"slots"}:
                raise PoolError(f"fleet-roles.json: host {hid}: role {role} must be {{\"slots\": N}}")
            slots = spec["slots"]
            if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 64:
                raise PoolError(f"fleet-roles.json: host {hid}: role {role} slots must be 1..64")
            parsed[role] = slots
        if row["state"] not in _STATES:
            raise PoolError(f"fleet-roles.json: host {hid}: state must be one of {_STATES}")
        out[hid] = (parsed, row["state"])
    return out


def validate_sidecar(doc) -> List[str]:
    """Structural errors of ``kanban-pool.json`` (any error refuses it whole)."""
    if not isinstance(doc, dict):
        return ["top level must be an object"]
    errs: List[str] = []
    if set(doc) - _SIDECAR_TOP:
        errs.append(f"unknown top-level keys {sorted(set(doc) - _SIDECAR_TOP)}")
    if isinstance(doc.get("schema"), bool) or doc.get("schema") != 1:
        errs.append(f"schema must be 1, not {doc.get('schema')!r}")
    if doc.get("ssh_user") != SSH_USER:
        errs.append(f"ssh_user must be {SSH_USER!r} (locked), not {doc.get('ssh_user')!r}")
    if "capacity_pct" in doc and not _pct(doc["capacity_pct"]):
        errs.append(f"capacity_pct must be in (0, 1], not {doc['capacity_pct']!r}")
    for key in ("profiles", "studio_bound_skills"):
        if key in doc and not _str_list(doc[key]):
            errs.append(f"{key} must be a list of non-empty strings")
    prio = doc.get("priority")
    if not isinstance(prio, list) or not all(isinstance(h, str) and _PLAIN_RE.fullmatch(h) for h in prio):
        errs.append("priority must be a list of plain host ids")
    elif len(set(prio)) != len(prio):
        errs.append("priority lists a host twice")
    hosts = doc.get("hosts")
    if not isinstance(hosts, dict):
        return errs + ["hosts must be an object"]
    for hid, row in hosts.items():
        if not isinstance(hid, str) or not _PLAIN_RE.fullmatch(hid) or not isinstance(row, dict):
            errs.append(f"hosts.{hid!r} is not a plain id with an object row")
            continue
        if "ssh_user" in row:
            errs.append(f"hosts.{hid}.ssh_user: per-host override refused (locked {SSH_USER})")
        if set(row) - _SIDECAR_ROW - {"ssh_user"}:
            errs.append(f"hosts.{hid}: unknown keys {sorted(set(row) - _SIDECAR_ROW - {'ssh_user'})}")
        if not isinstance(row.get("enabled"), bool):
            errs.append(f"hosts.{hid}.enabled must be true|false")
        if row.get("absence") not in _ABSENCE:
            errs.append(f"hosts.{hid}.absence must be one of {_ABSENCE}")
        if "capacity_pct" in row and not _pct(row["capacity_pct"]):
            errs.append(f"hosts.{hid}.capacity_pct must be in (0, 1]")
        if "profiles" in row and not _str_list(row["profiles"]):
            errs.append(f"hosts.{hid}.profiles must be a list of non-empty strings")
    return errs


def _read_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_pool(fleet_dir: Path, *, kanban_cfg: Optional[Mapping] = None) -> PoolConfig:
    """The pool, drift-checked (PRD §5.1). Never raises: a refusal is an empty
    pool plus a warning."""
    fleet_dir = Path(fleet_dir)
    try:
        roles_doc = _read_json(fleet_dir / ROLES_FILE)
        sidecar = _read_json(fleet_dir / SIDECAR_FILE)
    except (OSError, ValueError) as exc:
        return PoolConfig(warnings=(f"kanban pool: unreadable pool file: {exc}",),
                          refused="unreadable")
    try:
        roles = parse_roles(roles_doc) if roles_doc is not None else {}
    except PoolError as exc:
        return PoolConfig(warnings=(f"kanban pool: roles refused: {exc}",), refused="roles")
    with_role = {h for h, (r, _s) in roles.items() if ROLE in r}
    legacy = (kanban_cfg or {}).get("worker_hosts") if isinstance(kanban_cfg, Mapping) else None
    if legacy not in (None, [], {}, ""):
        if with_role:
            return PoolConfig(warnings=(
                "kanban pool: kanban.worker_hosts is set while fleet-roles.json has a "
                f"{ROLE} role ({', '.join(sorted(with_role))}): two sources, placing nothing "
                "(kanban.worker_hosts is retired)",), refused="legacy_worker_hosts")
        # Never silent: the old key alone no longer places anything.
        return PoolConfig(refused="legacy_worker_hosts", warnings=(
            "kanban pool: kanban.worker_hosts is retired and ignored; no host has a "
            f"{ROLE} role in {ROLES_FILE}, so nothing spills (register hosts with "
            "`fleet-host kanban-enable`)",))
    if sidecar is None:
        return PoolConfig(warnings=tuple(
            f"kanban pool: {h} has a {ROLE} role but {SIDECAR_FILE} is absent (host dropped)"
            for h in sorted(with_role)))
    errs = validate_sidecar(sidecar)
    if errs:
        return PoolConfig(warnings=("kanban pool: sidecar refused: " + "; ".join(errs),),
                          refused="sidecar")
    prio: List[str] = sidecar["priority"]
    rows: dict = sidecar["hosts"]
    gprofiles = tuple(sidecar.get("profiles") or ())
    gcap = float(sidecar.get("capacity_pct", DEFAULT_CAPACITY))
    warnings: List[str] = []
    hosts: List[PoolHost] = []
    disabled: List[str] = []
    for i, hid in enumerate(prio):
        row = rows.get(hid)
        if row is None:
            warnings.append(f"kanban pool: priority lists {hid} but hosts has no row (host dropped)")
            continue
        if not row["enabled"]:
            disabled.append(hid)
            continue
        if hid not in with_role:
            warnings.append(f"kanban pool: {hid} is enabled but has no {ROLE} role (host dropped)")
            continue
        role_slots, state = roles[hid]
        hosts.append(PoolHost(
            name=hid, ssh_host=hid, ssh_user=SSH_USER, slots=role_slots[ROLE],
            capacity_pct=float(row.get("capacity_pct", gcap)), absence=row["absence"],
            profiles=tuple(row.get("profiles", gprofiles)), state=state, enabled=True,
            priority=i,
        ))
    for hid in rows:
        if hid not in prio:
            warnings.append(f"kanban pool: hosts.{hid} is not in priority (host dropped)")
    for hid in sorted(with_role - set(rows)):
        warnings.append(f"kanban pool: {hid} has a {ROLE} role but no sidecar row (host dropped)")
    # The classifier allowlist is every profile SOME enabled host serves
    # (global list + per-host overrides); take() still checks the host.
    served = tuple(dict.fromkeys([*gprofiles, *(p for h in hosts for p in h.profiles)]))
    # ``hosts`` is every host the pool files KNOW, placement-eligible or not:
    # a pin on a dropped host (role but no row, row not in priority) must
    # still resolve as a pin and wait at take(), never fall back (Prism 4271004b).
    return PoolConfig(
        hosts=tuple(dict.fromkeys([*(h for h in prio if h in rows), *known_host_ids(fleet_dir)])),
        profiles=served,
        studio_bound_skills=tuple(sidecar.get("studio_bound_skills") or ()),
        pool_hosts=tuple(hosts), disabled=tuple(disabled), warnings=tuple(warnings),
    )


def load_pool(fleet_dir: Path, *, kanban_cfg: Optional[Mapping] = None) -> Tuple[List[PoolHost], List[str]]:
    """(enabled hosts in priority order, warnings)."""
    cfg = read_pool(fleet_dir, kanban_cfg=kanban_cfg)
    return list(cfg.pool_hosts), list(cfg.warnings)


# -- the classifier (PRD §5.3) ----------------------------------------------

def card_pin(body: Optional[str]) -> Optional[str]:
    """The card's raw ``host:<x>`` line, lower-cased (first line-anchored
    match). Raw: use :func:`resolve_pin` to decide whether it IS a pin."""
    m = HOST_PIN_RE.search(body or "")
    return m.group(1).lower() if m else None


def resolve_pin(body: Optional[str], known_hosts: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(pin, ignored)``. A pin is ONLY ``any``, ``studio`` or a host id the
    pool registry knows; any other ``host: <word>`` line (an HTTP header, a
    config snippet) is prose: ``pin`` None, the word returned as ``ignored``.
    So a card never waits on a host that does not exist."""
    raw = card_pin(body)
    if raw is None or raw in (PIN_ANY, PIN_STUDIO) or raw in set(known_hosts or ()):
        return raw, None
    return None, raw


def note_ignored_pin(card_id: str, word: str) -> None:
    """One WARN per card per 5 minutes for a ``host:`` line that is not a pin."""
    now = time.monotonic()
    last = _ignored_pin_warned.get(card_id)
    if last is None or now - last >= IGNORED_PIN_WARN_SECONDS:
        _ignored_pin_warned[card_id] = now
        _log.warning("kanban pool: card %s: ignored unknown host pin %s (routed by policy)",
                     card_id, word)


def known_host_ids(fleet_dir: Path) -> Tuple[str, ...]:
    """Every host id the pool files name (roles hosts with the kanban-worker
    role, sidecar rows, priority), read WITHOUT validation: a disabled or
    refused pool still knows its hosts, so a real pin stays fail-closed
    while prose never becomes one. Unreadable files name nothing."""
    out: List[str] = []
    try:
        roles = _read_json(Path(fleet_dir) / ROLES_FILE)
    except (OSError, ValueError):
        roles = None
    try:
        side = _read_json(Path(fleet_dir) / SIDECAR_FILE)
    except (OSError, ValueError):
        side = None
    if isinstance(roles, dict) and isinstance(roles.get("hosts"), dict):
        for hid, row in roles["hosts"].items():
            r = row.get("roles") if isinstance(row, dict) else None
            if isinstance(hid, str) and isinstance(r, dict) and ROLE in r:
                out.append(hid.lower())
    if isinstance(side, dict):
        hosts = side.get("hosts")
        out += [h.lower() for h in (hosts if isinstance(hosts, dict) else {}) if isinstance(h, str)]
        prio = side.get("priority")
        out += [h.lower() for h in (prio if isinstance(prio, list) else []) if isinstance(h, str)]
    return tuple(dict.fromkeys(out))


def portable(*, workspace_kind: Optional[str], has_links: bool, workspace_has_content: bool,
             assignee: Optional[str], body: Optional[str], skills: Iterable[str] = (),
             pool, native_command: bool = False) -> Tuple[bool, Optional[str], Optional[str]]:
    """``(portable, not_portable_rule, route_class)``; route_class in
    {'pin', 'any', 'policy'} when portable, else None.

    ``hard_ok`` (scratch, unlinked, empty local workspace, allowlisted
    assignee whose profile is not a native-command lane) is required for every
    remote placement; a pin only picks the
    route and never overrides it (RC-1). ``pool`` needs ``profiles``,
    ``studio_bound_skills`` and ``hosts`` (known host ids).
    """
    if (workspace_kind or "scratch") != "scratch":
        return False, "workspace_kind", None
    if has_links:
        return False, "linked", None
    if workspace_has_content:
        return False, "local_files", None
    if not assignee or assignee not in tuple(pool.profiles or ()):
        return False, "profile", None
    if native_command:
        # foreign_lane.worker_command runs the lane with a LOCAL Popen; the
        # ssh terminal backend never sees it, so placement would be a lie.
        return False, "native_command", None
    pin, _ignored = resolve_pin(body, pool.hosts or ())
    if pin == PIN_STUDIO:
        return False, "host_studio", None
    if pin == PIN_ANY:
        return True, None, "any"
    if pin is not None:
        # A KNOWN pool host: a remote pin. A disabled one waits at take()
        # and never falls back to a local spawn. An unknown word is no pin.
        return True, None, "pin"
    bound = set(pool.studio_bound_skills or ())
    if any(s in bound for s in (skills or ())):
        return False, "studio_bound_skill", None
    return True, None, "policy"


# -- probe + plan -----------------------------------------------------------

def host_threshold(ncpu: int, capacity_pct: float) -> float:
    """Remote load1 bar: ONE encoding (I-7)."""
    return capacity_pct * ncpu


def _ssh_argv(host: PoolHost, remote_cmd: str) -> List[str]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4"]
    if host.ssh_port != 22:
        argv += ["-p", str(host.ssh_port)]
    return argv + [host.target, remote_cmd]


def probe_host(h: PoolHost, runner: Callable = subprocess.run) -> Optional[Tuple[float, int]]:
    """``(load1, ncpu)`` from one ssh call, or None (fail closed: 0 slots)."""
    try:
        proc = runner(
            _ssh_argv(h, "cat /proc/loadavg; nproc"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    lines = (proc.stdout or "").split("\n")
    try:
        load1 = float(lines[0].split()[0])
        ncpu = int(lines[1].strip())
    except (IndexError, ValueError):
        return None
    return (load1, ncpu) if ncpu > 0 else None


def running_by_host(conn) -> Dict[str, int]:
    """Running tasks whose CURRENT run carries a ``worker_placed`` event, per host."""
    counts: Dict[str, int] = {}
    for row in conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
        "WHERE t.status = 'running' AND e.kind = ? "
        "AND e.run_id IS NOT NULL AND e.run_id = t.current_run_id",
        (PLACED_EVENT,),
    ).fetchall():
        try:
            name = json.loads(row[0] or "{}").get("host")
        except (TypeError, ValueError, AttributeError):
            continue
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


@dataclass
class SpilloverPlan:
    """One gateway tick's remote capacity, shared by every board (unlocked, I-12)."""

    hosts: Dict[str, PoolHost]           # priority order (dict order)
    slots: Dict[str, int]
    detail: Dict[str, dict] = field(default_factory=dict)
    disabled: Tuple[str, ...] = ()
    planned_at: float = 0.0
    refusal: Optional[str] = None        # why the last take() returned None
    config: PoolConfig = field(default_factory=PoolConfig)  # what portable() reads
    band: Optional[str] = None           # the Studio gate band this tick
    spill_reason: Optional[str] = None
    pins_only: bool = False              # admitting band: remote PINS only (PRD 5.2.5)

    @property
    def budget(self) -> int:
        return sum(self.slots.values())

    def probe(self, name: str) -> Optional[dict]:
        d = self.detail.get(name) or {}
        if d.get("load1") is None:
            return None
        return {"load1": d.get("load1"), "ncpu": d.get("ncpu")}

    def take(self, assignee: Optional[str], pin: Optional[str] = None) -> Optional[PoolHost]:
        """Reserve one slot: on ``pin`` only, else the first host in priority
        order with a free slot that serves ``assignee``. None sets ``refusal``."""
        self.refusal = None
        if pin is not None:
            host = self.hosts.get(pin)
            if host is None:
                self.refusal = ("pin_host_disabled" if pin in self.disabled
                                else "pin_host_dropped" if pin in (self.config.hosts or ())
                                else "pin_unknown_host")
                return None
            if assignee not in host.profiles:
                self.refusal = "pin_profile"  # the pinned host does not serve this assignee
                return None
            if self.slots.get(pin, 0) <= 0:
                reachable = (self.detail.get(pin) or {}).get("reachable")
                self.refusal = "pin_host_unreachable" if reachable is False else "pin_host_full"
                return None
            self.slots[pin] -= 1
            return host
        for name, host in self.hosts.items():
            if self.slots.get(name, 0) > 0 and assignee in host.profiles:
                self.slots[name] -= 1
                return host
        self.refusal = "pool_full"
        return None

    def release(self, host: PoolHost) -> None:
        if host.name in self.slots:
            self.slots[host.name] += 1

    def snapshot(self) -> dict:
        """The ``load_gate.json`` ``pool`` block for this tick."""
        hosts = {}
        for name, d in self.detail.items():
            hosts[name] = dict(d, free=self.slots.get(name, 0))
        return {"planned": True, "planned_at": self.planned_at, "hosts": hosts}

    def summary(self) -> str:
        return "; ".join(
            f"{n}: load1={d.get('load1')} ncpu={d.get('ncpu')} running={d.get('running')}/"
            f"{d.get('slots')} free={self.slots.get(n, 0)}"
            for n, d in self.detail.items()
        )


def plan(hosts: List[PoolHost], running_by_host: Mapping[str, int], *,
         probe: Callable[[PoolHost], Optional[Tuple[float, int]]] = probe_host,
         disabled: Collection[str] = (), planned_at: Optional[float] = None,
         config: Optional[PoolConfig] = None) -> SpilloverPlan:
    """ONE plan per gateway tick. A host takes work only when it is active,
    enabled, under its slots and probed under ``capacity_pct x ncpu`` (I-3).
    A host with no free slot or not active is not probed."""
    slots: Dict[str, int] = {}
    detail: Dict[str, dict] = {}
    for h in sorted(hosts, key=lambda x: x.priority):
        running = int(running_by_host.get(h.name, 0) or 0)
        free = max(0, h.slots - running) if (h.enabled and h.state == "active") else 0
        probed = free > 0
        sample = probe(h) if probed else None
        load1: Optional[float] = None
        ncpu: Optional[int] = None
        if sample is None:
            free = 0
        else:
            load1, ncpu = sample
            if load1 >= host_threshold(ncpu, h.capacity_pct):
                free = 0
        slots[h.name] = free
        detail[h.name] = {
            "slots": h.slots, "running": running, "load1": load1, "ncpu": ncpu,
            "state": h.state, "enabled": h.enabled,
            # None = not probed this tick (full, draining): unknown, not down.
            "reachable": (sample is not None) if probed else None,
        }
    if config is None:
        config = PoolConfig(
            hosts=tuple(h.name for h in hosts) + tuple(disabled),
            profiles=tuple(dict.fromkeys(p for h in hosts for p in h.profiles)),
            pool_hosts=tuple(hosts), disabled=tuple(disabled),
        )
    return SpilloverPlan(
        hosts={h.name: h for h in sorted(hosts, key=lambda x: x.priority)},
        slots=slots, detail=detail, disabled=tuple(disabled),
        planned_at=time.time() if planned_at is None else planned_at,
        config=config,
    )
