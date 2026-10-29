"""Kanban worker pool: which fleet hosts take spilled workers, and which cards may go.

KWLB v0.1 (PRD ``plans/2026-10-03_kanban-worker-load-balancing-PRD.md`` §5.1-§5.3).

Hosts come from two files under the fleet root (``<kanban_home>/fleet``):

* ``fleet-roles.json``: role ``kanban-worker`` = ``{"slots": N}`` (schema 1,
  the same validators as the CI and Prism readers);
* ``kanban-pool.json``: the kanban sidecar (``priority``, ``capacity_pct``,
  ``studio_only`` (deny-list: every other profile is a pool profile) or the
  legacy ``profiles`` allow-list, ``studio_bound_skills``, ``hosts.<id>.{enabled, absence,
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

Placement Phase 1b (PRD ``plans/2026-10-04_fleet-resource-aware-placement-PRD.md``
v0.4, I-3, I-8, F-5, F-6; card t_1e4b9684): with a ``TargetSignal`` (config
``kanban.placement.read_signal``, default true) the same ssh probe also reads
the host's ``host-pressure.json`` and the remote clock. ``plan()`` advances a
per-host hot/clear streak once per new remote ``at`` and persists it;
``take()`` only reads it. Stale/unreadable pressure is UNKNOWN (refused,
``reachable: false``, streak reset), never hot. Without a signal the planner
is KWLB v0.1, byte for byte.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from hermes_cli import kanban_admission as _admission
from hermes_cli import kanban_placement_reasons as _reasons
from hermes_cli import placement_ledger as _ledger
from hermes_cli import placement_policy as _policy

ROLE = "kanban-worker"
SSH_USER = "kanbanw"
ROLES_FILE = "fleet-roles.json"
SIDECAR_FILE = "kanban-pool.json"
# Roles that are membership only (t_940fc400): ``{}``, or the legacy count object
# until hermes-home drops the count (t_1acfaa85). Every other role keeps its count.
COUNT_FREE_ROLES = frozenset({"ci-runner"})
# Per-worker-host SSH reach matrix (card t_1e9ea7e9): written daily by the
# hermes-home no_agent cron ``kanban-reach-matrix`` (``ssh -o BatchMode=yes
# <alias> true`` from EVERY worker host, rc recorded), read here fail-closed.
REACH_FILE = "kanban-reach-matrix.json"
REACH_STALE_SECONDS = 36 * 3600
# Skills whose card drives a fleet HOST over ssh: the card is host-bound.
HOST_BOUND_SKILLS = ("fleet-host-diagnosis", "ssh-machines", "unifi", "local-dns",
                     "media-homelab-servarr")
DEFAULT_CAPACITY = 0.80
PLACED_EVENT = _reasons.WORKER_PLACED_EVENT
PROBE_TIMEOUT_SECONDS = 8

_PLAIN_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")
_ROLE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_ROLES_TOP = frozenset({"_doc", "schema", "hosts"})
_ROLES_ROW = frozenset({"roles", "state"})
# Optional audit field {by, card, reason, at} the fleet-host CLI stamps on a row (who/why/when of the last state
# change); validated by the router's registry, never read for placement here.
_ROLES_ROW_OPTIONAL = frozenset({"last_change"})
_SIDECAR_TOP = frozenset({"_doc", "schema", "ssh_user", "capacity_pct", "priority",
                          "profiles", "studio_only", "studio_bound_skills", "hosts"})
_SIDECAR_ROW = frozenset({"enabled", "absence", "capacity_pct", "profiles"})
_ABSENCE = ("required", "optional")
_STATES = ("active", "draining")

# Card-body pin: a line that is exactly ``host:<id>`` (case and surrounding
# whitespace ignored, CRLF line endings accepted). Line-anchored so prose that
# mentions a pin is not one.
HOST_PIN_RE = re.compile(r"^[ \t]*host:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*\r?$",
                         re.IGNORECASE | re.MULTILINE)
# What resolve_pin reads (t_229d6bca): a HOST_PIN_RE line, the same with a
# trailing full stop, or a pin that ENDS a line after a sentence
# (``... Admin-merge lane. host: studio``). Three cards on 2026-10-20 carried
# that shape, the anchored regex saw no pin and the pool took them as overflow.
# A ``host:`` in the middle of a sentence is still prose.
PIN_LINE_RE = re.compile(
    r"(?:^[ \t]*|[.;!?)][ \t]+)host:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE)
# The placement table (<root>/config/fleet-hosts.json, t_d6ce856e) carries the
# per-host ``aliases`` a pin may use (t_229d6bca).
FLEET_HOSTS_FILE = "fleet-hosts.json"
PIN_ANY = "any"
PIN_STUDIO = "studio"
STUDIO_HOST = "mac-studio"
# kanban.placement.default is RETIRED (t_82b4db80, Ace 2026-10-19 21:2x): every
# portable row is offered to the pool (admission by measured headroom over every
# eligible host, the Studio included) before a local slot. ``local`` pre-ordered
# the Studio first, an ORDERING rather than a weight; the R10 armer flipped to it
# on 10-18 15:31 and parked the fleet on the Studio for a day. The key is read
# only to log that it is ignored. PLACEMENT_DEFAULT_LOCAL stays as the record
# value for a plan that never ran the gate tick (``plan.default_remote`` False).
PLACEMENT_DEFAULT_REMOTE = "remote"
PLACEMENT_DEFAULT_LOCAL = "local"
_placement_default_warned: set = set()


def placement_default(kanban_cfg: Any) -> str:
    """Always ``remote``; a configured ``kanban.placement.default`` other than
    remote is ignored with one deprecation line per distinct value."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    val = pl.get("default") if isinstance(pl, Mapping) else None
    if val is not None and not (isinstance(val, str) and val.strip().lower() == PLACEMENT_DEFAULT_REMOTE):
        key = repr(val)
        if key not in _placement_default_warned:
            _placement_default_warned.add(key)
            logging.getLogger(__name__).warning(
                "kanban.placement.default=%s is deprecated and ignored: placement is remote-first "
                "(admission by measured headroom); remove the key (t_82b4db80)", key)
    return PLACEMENT_DEFAULT_REMOTE

# Words that name the dispatcher's own machine (resolve to PIN_STUDIO); the
# live ``socket.gethostname()`` short form joins them at call time.
_STUDIO_PIN_WORDS = frozenset({PIN_STUDIO, STUDIO_HOST, "local"})
# Per-host alias source (t_cb2b888d): ``hosts.<id>.{ssh_alias, host_label,
# transport}``; ``transport: local`` is the dispatcher's own machine.
CI_HOSTS_FILE = "ci-hosts.json"

# Card-body signals that the work needs the Studio's signed-in browser or GUI
# session (card t_3e6c9b2b; incident t_79436ebf placed a ":9222/:9224 on the
# Studio" card on ace-media). The NARROW list from the placement spec
# (Obsidian "Fleet Work Placement -- Host Affinity and Continuation" s2.3):
# a bare or loopback CDP port (``:9222``, ``localhost:9223``, never
# ``http://ace-ai:9222``, a remote browser is not Studio state), "signed-in
# browser", "add to cart", peekaboo. A bare ``CDP`` matched 460 of 5,350
# card bodies in 14 days, mostly infra, and is deliberately NOT a signal.
STUDIO_BODY_RE = re.compile(
    r"(?<![\w.-])(?:localhost|127\.0\.0\.1)?:922\d\b|signed-in browser|add to cart|\bpeekaboo\b",
    re.IGNORECASE)
# Studio-only resources a card body names (card t_aea826a9: t_dec6d40f named
# the vault + skills-shared, t_6af0c092 the relay logs + a sub-vps ssh; both
# spilled to ci-box and blocked). THE one table: ``card_needs`` derives a
# card's needs from it, the reach matrix row's ``caps`` says which needs a
# worker host covers, and hermes-home's kanban_pool_lint imports both.
# Over 14 days (5,045 bodies) the table matched 1,000; the bare words
# ``relay`` (+334, mostly prose) and ``CDP`` are deliberately absent.
CARD_NEEDS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("vault", re.compile(r"Obsidian/Ace Place|(?<![\w-])vault(?![\w-])", re.IGNORECASE)),
    ("skills_tree", re.compile(r"\.hermes/skills-shared\b|\bskill_view\b")),
    ("studio_relay", re.compile(
        r"\.hermes/logs/|(?<![\w-])deploy/claude-[\w-]+|(?<![\w.-])(?:localhost|127\.0\.0\.1)?:1881\d\b"
        r"|\blive gate\b|\bpinned (?:relay )?turn\b", re.IGNORECASE)),
    ("sub_ssh", re.compile(r"(?<![\w-])sub-vps-\d+\b", re.IGNORECASE)),
    ("studio_launchd", re.compile(r"\blaunchctl\b|Library/LaunchAgents\b")),
    # t_40abb42b: the card RUNS something in the Studio's live home tree
    # (t_50f2fa37 said run `~/.hermes/scripts/dpx-alias-key-leak-lint.py`,
    # spilled to ci-box, which has no ~/.hermes, and blocked). An executor
    # word right before a HOME-ABSOLUTE scripts/config/state/var path; a
    # repo-relative ``scripts/x.py`` or a bare path mention is not a need.
    # 14 days, 7,124 bodies: a bare mention matched 515 (48 placed
    # off-Studio, mostly PR work that never touched the path); this matches 87.
    ("studio_tree", re.compile(
        r"(?:\brun|\bDO:|\bpython[\d.]*|\bbash|\bz?sh)\s+[`'\"]?"
        r"(?:~|\$\{?HOME\}?|/Users/[^/\s]+|/home/[^/\s]+)/\.hermes/(?:scripts|config|state|var)/",
        re.IGNORECASE)),
)
STUDIO_NEEDS_RULE = "studio_needs"
# Card header lines that name where a card CAME FROM, never where it runs.
_HEADER_LINE_RE = re.compile(r"^[ \t]*(?:origin|prism lineage|lineage)[ \t]*:", re.IGNORECASE)
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
    # Reach matrix (t_1e9ea7e9): ``reach[host][alias] = rc``, only rows whose
    # sample is fresh. ``reach_aliases`` = every alias the file names at ALL
    # (top-level list + every row, stale rows included): the body-scan
    # vocabulary must not shrink when samples age, or a target silently
    # stops being a target (Prism da8af4dbc8be). An absent/stale/unreadable
    # matrix has reach {}: nothing is reachable from anywhere, so every
    # host-bound card stays local.
    reach: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    reach_aliases: Tuple[str, ...] = ()
    reach_status: str = "absent"   # ok | absent | stale | unreadable
    # Literal ``Host`` aliases of the dispatcher's ~/.ssh/config (t_f1b135a8):
    # a word-shaped alias (ci, ai, media) binds a card only if listed here.
    # None = unknown (unread / unreadable): fail closed, the check is skipped.
    ssh_hosts: Optional[frozenset] = None
    # ``{alias: canonical pin}`` from :func:`pin_aliases` (t_cb2b888d).
    pin_aliases: Mapping[str, str] = field(default_factory=dict)
    # ``{host: needs it covers}`` from fresh reach-matrix rows (t_aea826a9).
    # A host with no row or no ``caps`` covers nothing: fail closed.
    caps: Mapping[str, frozenset] = field(default_factory=dict)


# -- registry ---------------------------------------------------------------

def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def _pct(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0 < value <= 1


def parse_roles(doc) -> Dict[str, Tuple[Dict[str, Optional[int]], str]]:
    """``fleet-roles.json`` with the router's validators; raises PoolError.
    A ``COUNT_FREE_ROLES`` role may be ``{}`` (parsed as None) or a legacy count."""
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
        if not isinstance(row, dict) or not _ROLES_ROW <= set(row) <= _ROLES_ROW | _ROLES_ROW_OPTIONAL:
            raise PoolError(f"fleet-roles.json: host {hid}: fields must be roles, state (+ optional last_change)")
        roles = row["roles"]
        if not isinstance(roles, dict) or not roles:
            raise PoolError(f"fleet-roles.json: host {hid}: roles must be a non-empty object")
        parsed: Dict[str, Optional[int]] = {}
        for role, spec in roles.items():
            if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
                raise PoolError(f"fleet-roles.json: host {hid}: bad role {role!r}")
            if role in COUNT_FREE_ROLES and spec == {}:
                # Membership only (t_940fc400): the per-box allocator owns CI capacity.
                parsed[role] = None
                continue
            if not isinstance(spec, dict) or set(spec) != {"slots"}:
                shape = "{} or {\"slots\": N}" if role in COUNT_FREE_ROLES else "{\"slots\": N}"  # slots-vocab: allow legacy fleet-roles.json schema key
                raise PoolError(f"fleet-roles.json: host {hid}: role {role} must be {shape}")
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
    for key in ("profiles", "studio_only", "studio_bound_skills"):
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


def derived_profiles(profiles_dir: Path, studio_only: Iterable[str]) -> Tuple[str, ...]:
    """Pool profiles under the deny-list (t_7b1eca07): every named profile with a
    ``config.yaml`` under ``profiles_dir``, minus ``studio_only``, sorted. A
    native-command lane in the result is still refused by ``portable()``."""
    deny = {s.strip().lower() for s in studio_only or ()}
    try:
        names = sorted(p.name for p in Path(profiles_dir).iterdir()
                       if not p.name.startswith(".") and (p / "config.yaml").is_file())
    except OSError:
        return ()
    return tuple(n for n in names if n.lower() not in deny)


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
    # studio_only binds EVERY list a host serves, per-host overrides included,
    # so no row can re-admit a denied profile (Prism 34f523dde2d6).
    deny = {s.strip().lower() for s in sidecar.get("studio_only") or ()}
    if "studio_only" in sidecar:
        # Deny-list (t_7b1eca07): every live profile is a pool profile unless the
        # sidecar keeps it on the Studio, so a new profile never lands Studio-bound.
        gprofiles = derived_profiles(fleet_dir.parent / "profiles", sidecar["studio_only"])
    gcap = float(sidecar.get("capacity_pct", DEFAULT_CAPACITY))
    warnings: List[str] = []
    hosts: List[PoolHost] = []
    disabled: List[str] = []
    policy = _policy.load(fleet_dir)
    for i, hid in enumerate(prio):
        row = rows.get(hid)
        if row is None:
            warnings.append(f"kanban pool: priority lists {hid} but hosts has no row (host dropped)")
            continue
        if not row["enabled"]:
            disabled.append(hid)
            continue
        if not policy.targets(hid):
            # Placement PRD I-1: the runtime assertion, not only the lint.
            warnings.append(f"kanban pool: {hid} is targets:false in placement-policy.json "
                            "(never a placement target; host dropped)")
            continue
        if hid not in with_role:
            warnings.append(f"kanban pool: {hid} is enabled but has no {ROLE} role (host dropped)")
            continue
        role_slots, state = roles[hid]
        hosts.append(PoolHost(
            name=hid, ssh_host=hid, ssh_user=SSH_USER, slots=role_slots[ROLE],
            capacity_pct=float(row.get("capacity_pct", gcap)), absence=row["absence"],
            profiles=tuple(p for p in row.get("profiles", gprofiles) if p.lower() not in deny),
            state=state, enabled=True,
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
    # Not a pool-drift warning: a missing matrix is the normal pre-cron state
    # and the classifier is fail-closed without it (reach_status carries it).
    reach, aliases, reach_status = read_reach(fleet_dir)
    # ``hosts`` is every host the pool files KNOW, placement-eligible or not:
    # a pin on a dropped host (role but no row, row not in priority) must
    # still resolve as a pin and wait at take(), never fall back (Prism 4271004b).
    return PoolConfig(
        hosts=tuple(dict.fromkeys([*(h for h in prio if h in rows), *known_host_ids(fleet_dir)])),
        profiles=served,
        studio_bound_skills=tuple(sidecar.get("studio_bound_skills") or ()),
        pool_hosts=tuple(hosts), disabled=tuple(disabled), warnings=tuple(warnings),
        reach=reach, reach_aliases=aliases, reach_status=reach_status,
        ssh_hosts=known_ssh_hosts(fleet_dir),
        pin_aliases=pin_aliases(fleet_dir),
        caps=read_caps(fleet_dir),
    )


def card_needs(body: Optional[str]) -> frozenset:
    """The Studio-only resources a card body names (:data:`CARD_NEEDS`).
    The origin/lineage header is provenance, never a need: an Obsidian note
    title with "vault" in it pinned t_cb47a348 to the Studio (t_641d1ab8)."""
    work = without_header_lines(body)
    return frozenset(name for name, rx in CARD_NEEDS if rx.search(work))


def without_header_lines(body: Optional[str]) -> str:
    """``body`` minus its ``origin:`` / ``lineage:`` / ``Prism lineage:`` lines."""
    return "\n".join(ln for ln in (body or "").splitlines() if not _HEADER_LINE_RE.match(ln))


def read_caps(fleet_dir: Path, *, now: Optional[float] = None,
              stale_after_s: float = REACH_STALE_SECONDS) -> Dict[str, frozenset]:
    """``{host: needs covered}`` from ``kanban-reach-matrix.json`` row
    ``caps: {<need>: true|false}``. Same freshness rule as :func:`read_reach`;
    an absent, unreadable or stale row covers nothing (fail closed)."""
    now = time.time() if now is None else now
    try:
        doc = _read_json(Path(fleet_dir) / REACH_FILE)
    except (OSError, ValueError):
        return {}
    rows = doc.get("hosts") if isinstance(doc, dict) and doc.get("schema") == 1 else None
    out: Dict[str, frozenset] = {}
    for hid, row in (rows.items() if isinstance(rows, dict) else ()):
        if not isinstance(hid, str) or not isinstance(row, dict):
            continue
        at, caps = row.get("at"), row.get("caps")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not isinstance(caps, dict):
            continue
        if now - float(at) > stale_after_s or at > now + 3600:
            continue
        out[hid.lower()] = frozenset(k for k, v in caps.items() if isinstance(k, str) and v is True)
    return out


def declares_requirements(body: Optional[str]) -> bool:
    """True when the body's line-anchored ``requires:`` lines name something
    the capability layer judges (``none`` or a capability word). The
    ``control-plane-read`` access word alone does not count: portable() owns
    it, and it says nothing about the prose needs."""
    from hermes_cli import kanban_host_capabilities as _khc
    return any(w != _reasons.ACCESS_CONTROL_PLANE_READ for w in _khc.declared_words(body))


def needs_allowed_hosts(body: Optional[str], pool, *,
                        honor_declared: bool = False) -> Optional[Tuple[str, ...]]:
    """None when the body names no Studio-only resource; else the enabled
    pool hosts whose fresh reach-matrix ``caps`` cover every need (``()`` =
    none: the card runs on the Studio or waits, never spills).
    ``honor_declared`` (the capability layer enforces this tick): a body with
    an explicit ``requires:`` line is judged by that line, not by prose
    (None, t_6d9e7d90)."""
    if honor_declared and declares_requirements(body):
        return None
    needs = card_needs(body)
    if not needs:
        return None
    caps = getattr(pool, "caps", None) or {}
    return tuple(h.name for h in (getattr(pool, "pool_hosts", ()) or ())
                 if needs <= caps.get(h.name.lower(), frozenset()))


def read_reach(fleet_dir: Path, *, now: Optional[float] = None,
               stale_after_s: float = REACH_STALE_SECONDS,
               ) -> Tuple[Dict[str, Dict[str, int]], Tuple[str, ...], str]:
    """``(reach, aliases, status)`` from ``<fleet_dir>/kanban-reach-matrix.json``.

    Schema 1: ``{"schema": 1, "hosts": {"<worker host>": {"at": <epoch>,
    "reach": {"<alias>": <rc>}}}}``. FAIL CLOSED (t_1e9ea7e9): an absent,
    unreadable or malformed file, or a row older than ``stale_after_s``, is
    DROPPED, never read as reachable. ``aliases`` is the union of the
    top-level ``aliases`` list and every alias ANY row probed, fresh or
    stale, in file order: the target vocabulary does not depend on
    freshness, only the proof does.
    """
    now = time.time() if now is None else now
    try:
        doc = _read_json(Path(fleet_dir) / REACH_FILE)
    except (OSError, ValueError):
        return {}, (), "unreadable"
    if doc is None:
        return {}, (), "absent"
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("hosts"), dict):
        return {}, (), "unreadable"
    reach: Dict[str, Dict[str, int]] = {}
    aliases: List[str] = [a.lower() for a in (doc.get("aliases") or ())
                          if isinstance(a, str) and a] if isinstance(doc.get("aliases"), list) else []
    stale = 0
    for hid, row in doc["hosts"].items():
        if not isinstance(hid, str) or not isinstance(row, dict):
            continue
        at = row.get("at")
        table = row.get("reach")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not isinstance(table, dict):
            continue
        clean: Dict[str, int] = {}
        for alias, rc in table.items():
            if isinstance(alias, str) and alias and not isinstance(rc, bool) and isinstance(rc, int):
                clean[alias.lower()] = rc
                aliases.append(alias.lower())
        if now - float(at) > stale_after_s or at > now + 3600:
            stale += 1
            continue
        reach[hid.lower()] = clean
    vocab = tuple(dict.fromkeys(aliases))
    if not reach:
        return {}, vocab, "stale" if stale else "unreadable"
    return reach, vocab, "ok"


_HOST_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# The ssh-shaped ways a body drives a host (t_696a9a06, t_f1b135a8). A bare
# word is prose: "ci-box burst 2/6" or "the ci-box build sheet" drive nothing,
# and neither does a word that merely FOLLOWS a verb ("ssh in and check the ai
# lane", "rsync the media library"): only the TARGET position binds.
#   ssh/sftp/mosh [opts] [user@]<alias> ...      (first non-option operand)
#   scp/rsync ... [user@]<alias>:<path>          (a remote operand of the verb)
#   [user@]<alias>:<path>                        (anywhere: separator-bearing aliases only)
#   -H <alias>                                   (docker -H, pdsh -H ...)
# A WORD-SHAPED alias (no '-', '.', '_' or digit: ci, ai, media, unifi) is an
# ordinary English word too, so it additionally needs a ``Host`` entry in the
# dispatcher's ~/.ssh/config (:func:`ssh_config_hosts`), never a bare word.
_SSH_VERB_RE = re.compile(r"(?<![\w-])(ssh|scp|sftp|rsync|mosh)(?=\s)", re.IGNORECASE)
_SCP_RE = re.compile(rf"(?:^|[\s@'\"])({_HOST_TOKEN}):(?=[~/.$A-Za-z_])")
_REMOTE_OPERAND_RE = re.compile(
    rf"^(?:rsync://)?(?:[^\s@/]+@)?({_HOST_TOKEN})(?:::?(?=[~/.$A-Za-z_]|$)|(?<=\w)/)")
_DASH_H_RE = re.compile(rf"(?:^|\s)-H[ \t]*[\"']?(?:ssh://)?(?:[\w.-]+@)?({_HOST_TOKEN})")
# Per verb: (short options that take a value, long options that take a SEPARATE
# value). A letter not in its verb's set is a FLAG: misreading a flag as
# arg-taking swallows the target (``sftp -p host``: -p is preserve), which
# fails open; the reverse at worst binds a value that is no alias.
#   ssh(1):  -B -b -c -D -E -e -F -I -i -J -L -l -m -O -o -P -p -Q -R -S -W -w
#   sftp(1): -B -b -c -D -F -i -J -l -o -P -R -S -s -X
#   mosh(1): -p, and Getopt::Long ``--port 60001`` / ``--ssh CMD`` ...
_VERB_ARG_OPTS: Dict[str, Tuple[frozenset, frozenset]] = {
    "ssh": (frozenset("BbcDEeFIiJLlmOoPpQRSWw"), frozenset()),
    "sftp": (frozenset("BbcDFiJloPRSsX"), frozenset()),
    "mosh": (frozenset("p"), frozenset({"client", "server", "ssh", "predict", "family", "port",
                                        "bind-server", "experimental-remote-ip"})),
}
_CMD_BREAK = frozenset({";", "&&", "||", "|", "&"})
_WORD_ALIAS_RE = re.compile(r"[a-z]+")
_ssh_config_cache: Dict[str, Tuple[Tuple[float, ...], Optional[frozenset]]] = {}


def _strip_token(tok: str) -> str:
    return tok.strip("`'\"()[]{}<>,;")


def _ssh_target(rest: str, verb: str = "ssh") -> Optional[str]:
    """``[user@]host`` of the first non-option operand of ssh/sftp/mosh."""
    short_args, long_args = _VERB_ARG_OPTS[verb]
    toks = rest.split()
    i = 0
    while i < len(toks):
        tok = _strip_token(toks[i])
        i += 1
        if not tok:
            continue
        if tok in _CMD_BREAK:
            return None
        if tok == "--":
            continue
        if tok.startswith("--"):
            if "=" not in tok and tok[2:] in long_args:
                i += 1         # ``--port 60001``; ``--x=y`` carries its value
            continue
        if tok.startswith("-") and len(tok) > 1:
            flags = tok[1:]
            for j, ch in enumerate(flags):
                if ch in short_args:
                    if j == len(flags) - 1:
                        i += 1     # value is the next token
                    break          # else the value is attached (-p22, -oX=y)
            continue
        if tok.lower().startswith("ssh://"):
            tok = tok[6:]
        tok = tok.rsplit("@", 1)[-1]
        m = re.match(_HOST_TOKEN, tok)
        return m.group(0) if m else None
    return None


def _remote_operands(rest: str) -> List[str]:
    """Hosts of the ``[user@]host:path`` / ``rsync://host/`` operands of scp/rsync."""
    out: List[str] = []
    for raw in rest.split():
        tok = _strip_token(raw)
        if tok in _CMD_BREAK:
            break
        if tok.startswith("-"):
            continue
        m = _REMOTE_OPERAND_RE.match(tok)
        if m and (":" in tok or tok.lower().startswith("rsync://")):
            out.append(m.group(1))
    return out


def _ssh_shaped_hosts(line: str) -> List[str]:
    """Host tokens the line addresses in an ssh TARGET position, in order.

    Every verb on the line is parsed on its own (``ssh a ..., then ssh b``)."""
    out: List[str] = []
    verbs = list(_SSH_VERB_RE.finditer(line))
    for k, m in enumerate(verbs):
        rest = line[m.end():verbs[k + 1].start() if k + 1 < len(verbs) else len(line)]
        verb = m.group(1).lower()
        if verb in ("scp", "rsync"):
            out += _remote_operands(rest)
        else:
            t = _ssh_target(rest, verb)
            if t:
                out.append(t)
    # A verbless ``ace-ai:/path`` still binds a separator-bearing alias; the
    # \0 mark tells host_bound_targets it is verbless so a WORD alias never
    # binds this way (``ci:lint``, ``media:...`` in prose).
    out += [f"\0{h}" for h in _SCP_RE.findall(line)]
    out += _DASH_H_RE.findall(line)
    return out


def word_shaped_alias(alias: str) -> bool:
    """An alias that is also an ordinary word (ci, ai, media, unifi): no
    separator and no digit. Such an alias binds only as a known ssh host."""
    return _WORD_ALIAS_RE.fullmatch(alias.lower()) is not None


# ssh_config(5): ``Keyword value`` or ``Keyword=value`` (optional whitespace
# around exactly one ``=``).
_SSH_CFG_KV_RE = re.compile(r"\s*([A-Za-z]+)(?:\s*=\s*|\s+)(\S.*)")


def _ssh_config_kv(line: str) -> Tuple[str, str]:
    m = _SSH_CFG_KV_RE.match(line)
    return (m.group(1).lower(), m.group(2)) if m else ("", "")


def _ssh_config_files(path: Path, depth: int = 0,
                      base_dir: Optional[Path] = None) -> List[Path]:
    """``path`` plus its Includes. A relative Include resolves against
    ``base_dir`` (the top-level user config's directory, ~/.ssh) at every
    depth, never the including file's directory (ssh_config(5))."""
    base_dir = path.parent if base_dir is None else base_dir
    files = [path]
    if depth > 4:
        return files
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return files
    for line in text.splitlines():
        key, val = _ssh_config_kv(line)
        if key == "include":
            for pat in val.split():
                pat = str(Path(pat).expanduser())
                if not pat.startswith("/"):
                    pat = str(base_dir / pat)
                for inc in sorted(Path("/").glob(pat.lstrip("/"))):
                    files += _ssh_config_files(inc, depth + 1, base_dir)
    return files


def ssh_config_hosts(path: Optional[Path] = None) -> Optional[frozenset]:
    """Every literal ``Host`` alias in ``~/.ssh/config`` (and its Includes),
    lower-cased; wildcard/negated patterns are skipped. ``None`` when the
    file is absent or unreadable: the caller then FAILS CLOSED (a word alias
    in target position still binds, the card stays local). Cached by mtime."""
    path = Path(path) if path is not None else Path.home() / ".ssh" / "config"
    try:
        files = _ssh_config_files(path)
        stamp = tuple(f.stat().st_mtime for f in files)
    except OSError:
        return None
    key = str(path)
    hit = _ssh_config_cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    names: List[str] = []
    try:
        for f in files:
            for line in f.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                key, val = _ssh_config_kv(line)
                if key == "host":
                    names += [n.lower() for n in val.split()
                              if not any(c in n for c in "*?!")]
    except OSError:
        return None
    out = frozenset(names)
    _ssh_config_cache[key] = (stamp, out)
    return out


def known_ssh_hosts(fleet_dir: Path, *, ssh_config: Optional[Path] = None) -> Optional[frozenset]:
    """The ``PoolConfig.ssh_hosts`` set: literal ``Host`` aliases of the
    dispatcher's ~/.ssh/config, plus every alias ANY reach-matrix row (fresh
    or stale) recorded ``rc == 0`` for: ssh resolved and reached it, so it is
    a real destination even with no Host entry (a DNS name like ``unifi``).
    Stale rows count because this widens what binds (fail closed); freshness
    only gates the proof. ``None`` when ~/.ssh/config is unreadable."""
    cfg = ssh_config_hosts(ssh_config)
    if cfg is None:
        return None
    reached: List[str] = []
    try:
        doc = _read_json(Path(fleet_dir) / REACH_FILE)
    except (OSError, ValueError):
        doc = None
    rows = doc.get("hosts") if isinstance(doc, dict) else None
    for row in (rows.values() if isinstance(rows, dict) else ()):
        table = row.get("reach") if isinstance(row, dict) else None
        for alias, rc in (table.items() if isinstance(table, dict) else ()):
            if isinstance(alias, str) and rc == 0 and not isinstance(rc, bool):
                reached.append(alias.lower())
    return cfg | frozenset(reached)


def host_bound_targets(body: Optional[str], skills: Iterable[str], aliases: Iterable[str],
                       *, pin_hosts: Iterable[str] = (),
                       ssh_hosts: Optional[Collection[str]] = None,
                       ) -> Tuple[bool, Tuple[str, ...]]:
    """``(host_bound, targets)``: is this card host-bound (a HOST_BOUND_SKILLS
    skill, or a body line that addresses a probed ssh alias in an ssh shape),
    and which aliases it names. A skill-bound card with no named alias has
    targets ``()``: it needs a host that reaches EVERYTHING the matrix probed.

    Only ssh-shaped mentions count (``ssh [opts] [user@]ace-ai``,
    ``ace-ai:/path``, ``-H ace-ai``), never a bare word: "ci-box burst 2/6"
    or "the ci-box build sheet" is prose (t_696a9a06). Aliases compare whole,
    never as substrings. A ``host:<pin>`` line is not a target when it IS a
    pin (``any``, ``studio`` or one of ``pin_hosts``). Skill ids compare on
    their basename (``operations/ssh-machines``, Prism e5e1f41acb09).

    Only the ssh TARGET position binds (t_f1b135a8): ``ssh in and check the
    ai lane`` names no target. A word-shaped alias (``ci``, ``ai``, ``media``)
    binds only when it is a literal ``Host`` in ``ssh_hosts`` (read_pool
    fills it from the dispatcher's ~/.ssh/config). ``ssh_hosts=None`` means
    unknown (file absent/unreadable): the check is skipped, fail closed
    (bound, stays local). A verbless ``word:path`` never binds a word alias
    (``ci:lint``, ``media: notes``).
    """
    bound = any(s.rsplit("/", 1)[-1] in HOST_BOUND_SKILLS for s in (skills or ()))
    alias_set = {a for a in (aliases or ()) if a}
    pins = {PIN_ANY, PIN_STUDIO, *(h.lower() for h in (pin_hosts or ()))}
    found: List[str] = []
    known = None if ssh_hosts is None else {h.lower() for h in ssh_hosts}
    if alias_set and body:
        for line in body.splitlines():
            m = HOST_PIN_RE.match(line)
            if m and m.group(1).lower() in pins:
                continue
            for word in _ssh_shaped_hosts(line):
                verbless = word.startswith("\0")
                w = word.lstrip("\0").lower().rstrip("._-")
                if w not in alias_set or w in found:
                    continue
                if word_shaped_alias(w):
                    if verbless:
                        continue
                    if known is not None and w not in known:
                        continue
                found.append(w)
    return (bound or bool(found)), tuple(found)


# Access-bound derivation (spec v1.2 §2.6, Phase 2c, t_e99ef75c). ``sudo`` is
# the whole ssh-root trigger (Q9 recommendation (a)); a root-only verb list
# would be a lint, never a rule. control-plane-read binds on a PATH-shaped
# mention only (Momus pass 2 R-F): "the kanban.db schema" is prose, while
# ``~/.hermes/kanban.db`` or ``fleet/kanban-pool.json`` is a file the work opens.
_SUDO_RE = re.compile(r"(?<![\w-])sudo(?![\w-])")
_CONTROL_PLANE_PATH_RE = re.compile(
    r"(?:^|(?<=[\s`'\"(=:,]))(?:~|\.\.?|[\w.@*${}-]+)?(?:/[\w.@*${}-]+)*/"
    r"(kanban\.db|gateway\.log|load_gate\.json|kanban-pool\.json)(?![\w-])")


def _sudo_before_break(remote: str) -> bool:
    """``sudo`` in the remote command, before a ``;``/``&&``/``|`` outside
    quotes ends it (``ssh ci uptime && sudo reboot`` runs sudo on the Studio;
    ``ssh ci 'uptime && sudo dmesg'`` runs it on ci).

    Character scan (Prism 5bdfe1dcf55b): a separator outside quotes ends the
    remote command even when glued to a word (``uptime;``, ``a&&sudo``) or to
    a closing quote (``"echo hi"; sudo x``)."""
    quote = None
    for i, ch in enumerate(remote):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in ";&|":
            if ch == "&" and (remote[i - 1:i] in ("<", ">") or remote[i + 1:i + 2] == ">"):
                continue  # ``2>&1`` / ``&>f`` is a redirection, not a separator
            return False
        if _SUDO_RE.match(remote, i):
            return True
    return False


def access_bound_cap(body: Optional[str], aliases: Iterable[str], *,
                     pin_hosts: Iterable[str] = (),
                     ssh_hosts: Optional[Collection[str]] = None,
                     canon: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The Studio-only ACCESS the card needs, or None: ``ssh-root:<host>`` or
    ``control-plane-read`` (first hit; ssh-root wins on a tie).

    ``ssh-root:<host>``: a body line ``ssh [opts] [user@]<alias> ... sudo ...``
    for a probed alias, with :func:`host_bound_targets`' discipline: only the
    ssh TARGET binds, aliases compare whole, a word-shaped alias must be a
    known ssh host, a ``host:<pin>`` line is never a target. ``<host>`` is the
    canonical pin of the alias when ``canon`` names one (``ci`` -> ``ci-box``).

    ``control-plane-read``: a path-shaped mention of kanban.db, gateway.log,
    load_gate.json or kanban-pool.json, or an explicit
    ``requires: control-plane-read`` line. A bare word never binds.
    """
    from hermes_cli import kanban_host_capabilities as _khc
    alias_set = {a.lower() for a in (aliases or ()) if a}
    pins = {PIN_ANY, PIN_STUDIO, *(h.lower() for h in (pin_hosts or ()))}
    known = None if ssh_hosts is None else {h.lower() for h in ssh_hosts}
    canon = {k.lower(): v for k, v in (canon or {}).items()}
    cp_read = False
    for line in (body or "").splitlines():
        m = HOST_PIN_RE.match(line)
        if m and m.group(1).lower() in pins:
            continue
        verbs = list(_SSH_VERB_RE.finditer(line))
        for k, v in enumerate(verbs):
            if v.group(1).lower() != "ssh":
                continue
            rest = line[v.end():verbs[k + 1].start() if k + 1 < len(verbs) else len(line)]
            target = _ssh_target(rest, "ssh")
            if not target:
                continue
            w = target.lower().rstrip("._-")
            if w not in alias_set:
                continue
            if word_shaped_alias(w) and known is not None and w not in known:
                continue
            at = re.search(rf"(?<![\w.-]){re.escape(target)}(?![\w-])", rest)
            if at and _sudo_before_break(rest[at.end():]):
                return _reasons.ssh_root(canon.get(w, w))
        if not cp_read and _CONTROL_PLANE_PATH_RE.search(line):
            cp_read = True
    if cp_read or _reasons.ACCESS_CONTROL_PLANE_READ in _khc.declared_words(body):
        return _reasons.ACCESS_CONTROL_PLANE_READ
    return None


def reaching_hosts(reach: Mapping[str, Mapping[str, int]], targets: Iterable[str],
                   aliases: Iterable[str]) -> Tuple[str, ...]:
    """Worker hosts whose fresh row shows ``rc == 0`` for EVERY target (or,
    with no named target, for every alias the matrix knows). Empty when the
    matrix is empty: fail closed.
    """
    want = tuple(targets) or tuple(aliases)
    if not want:
        return ()
    return tuple(h for h, row in reach.items() if all(row.get(a) == 0 for a in want))


def load_pool(fleet_dir: Path, *, kanban_cfg: Optional[Mapping] = None) -> Tuple[List[PoolHost], List[str]]:
    """(enabled hosts in priority order, warnings)."""
    cfg = read_pool(fleet_dir, kanban_cfg=kanban_cfg)
    return list(cfg.pool_hosts), list(cfg.warnings)


# -- the classifier (PRD §5.3) ----------------------------------------------

def card_pin(body: Optional[str]) -> Optional[str]:
    """The card's raw ``host:<x>`` line, lower-cased (first line-anchored
    match). Raw: use :func:`resolve_pin` to decide whether it IS a pin."""
    m = PIN_LINE_RE.search(body or "")
    return m.group(1).rstrip(".").lower() if m else None


def resolve_pin(body: Optional[str], known_hosts: Iterable[str],
                aliases: Optional[Mapping[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(pin, ignored)``. A pin is ``any``, the Studio (``studio``,
    ``mac-studio``, ``local``, this machine's short hostname) or a host the
    pool registry knows, by id or by an alias in ``aliases`` (``{word:
    canonical}``, :func:`pin_aliases`); the CANONICAL pin is returned. Any
    other ``host: <word>`` line is not a pin: ``pin`` None, the word
    returned as ``ignored``. A caller treats ``(None, word)`` as an UNKNOWN
    pin (t_cb2b888d): held local, never portable."""
    known = {h.lower() for h in (known_hosts or ())}
    alias_map = {k.lower(): v for k, v in (aliases or {}).items()}
    studio = _studio_words()
    ignored: Optional[str] = None
    # Every line-anchored host: line, in order: prose lines (an HTTP header
    # pasted above the real pin) are skipped, the first REAL pin wins
    # (Prism r8 "skip prose host lines before choosing the effective pin").
    for m in PIN_LINE_RE.finditer(body or ""):
        raw = m.group(1).rstrip(".").lower()
        if raw == PIN_ANY or raw in known:
            return raw, ignored
        if raw in studio:
            return PIN_STUDIO, ignored
        if raw in alias_map:
            return alias_map[raw], ignored
        if ignored is None:
            ignored = raw
    return None, ignored


def _studio_words() -> frozenset:
    """Words for the dispatcher's own machine: the constants plus the live
    ``socket.gethostname()`` short form, lower-cased."""
    import socket
    try:
        short = socket.gethostname().split(".", 1)[0].strip().lower()
    except OSError:
        short = ""
    return _STUDIO_PIN_WORDS | ({short} if short else set())


def pin_aliases(fleet_dir: Path) -> Dict[str, str]:
    """``{alias: canonical pin}`` from the registry (t_cb2b888d), the ONE
    alias source. For every host the pool files know (:func:`known_host_ids`)
    and every ``ci-hosts.json`` row: the id, its ``ssh_alias`` and
    ``host_label``, and the first DNS label of each (``ci.ace`` -> ``ci``);
    plus every ``aliases`` entry of a ``<root>/config/fleet-hosts.json`` row
    (``mac-studio`` is the Studio, t_229d6bca).
    A row with ``transport: local`` is the Studio (``studio``). A row that is
    neither a known pool host nor local adds nothing. An alias two hosts
    claim is dropped (ambiguous: it stays unknown, never guesses). Matching
    is case-insensitive. Unreadable files add nothing."""
    known = set(known_host_ids(fleet_dir))
    try:
        ci = _read_json(Path(fleet_dir) / CI_HOSTS_FILE)
    except (OSError, ValueError):
        ci = None
    claims: Dict[str, set] = {}

    def claim(word, canon):
        if isinstance(word, str) and word.strip():
            w = word.strip().lower()
            for v in dict.fromkeys((w, w.split(".", 1)[0])):
                if v:
                    claims.setdefault(v, set()).add(canon)

    for hid in known:
        claim(hid, hid)
    rows = ci.get("hosts") if isinstance(ci, dict) else None
    for hid, row in (rows.items() if isinstance(rows, dict) else ()):
        if not isinstance(hid, str) or not isinstance(row, dict):
            continue
        canon = (PIN_STUDIO if row.get("transport") == "local"
                 else hid.lower() if hid.lower() in known else None)
        if canon is None:
            continue
        for word in (hid, row.get("ssh_alias"), row.get("host_label")):
            claim(word, canon)
    # config/fleet-hosts.json ``hosts.<id>.aliases`` (t_229d6bca): the Studio
    # row (mac-studio) lists studio / mac-studio-m3u / local, a pool host row
    # its own spellings. A row that is neither adds nothing.
    try:
        table = _read_json(Path(fleet_dir).parent / "config" / FLEET_HOSTS_FILE)
    except (OSError, ValueError):
        table = None
    rows = table.get("hosts") if isinstance(table, dict) else None
    for hid, row in (rows.items() if isinstance(rows, dict) else ()):
        if not isinstance(hid, str) or not isinstance(row, dict):
            continue
        canon = (PIN_STUDIO if hid.lower() == STUDIO_HOST
                 else hid.lower() if hid.lower() in known else None)
        if canon is None:
            continue
        al = row.get("aliases")
        for word in (hid, *(al if isinstance(al, list) else ())):
            claim(word, canon)
    return {w: next(iter(c)) for w, c in sorted(claims.items()) if len(c) == 1}


def pin_suggestions(word: str, known_hosts: Iterable[str],
                    aliases: Optional[Mapping[str, str]] = None, n: int = 3) -> List[str]:
    """Closest real pins to an unknown ``host:`` word, as ``alias->canonical``
    (or the canonical alone); never empty: falls back to every canonical pin."""
    import difflib
    table: Dict[str, str] = {w: PIN_STUDIO for w in _studio_words()}
    table[PIN_ANY] = PIN_ANY
    table.update({h.lower(): h.lower() for h in (known_hosts or ())})
    table.update({k.lower(): v for k, v in (aliases or {}).items()})
    close = difflib.get_close_matches((word or "").lower(), list(table), n=n, cutoff=0.6)
    if not close:
        return sorted(set(table.values()))
    return [w if table[w] == w else f"{w}->{table[w]}" for w in close]


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


def portable(*, workspace_kind: Optional[str], workspace_has_content: bool,
             assignee: Optional[str], body: Optional[str], skills: Iterable[str] = (),
             pool, has_links: bool = False,
             workspace_bind: Optional[Tuple[str, str, Optional[str]]] = None,
             native_command: bool = False,
             manifest: Optional[Mapping[str, str]] = None,
             targets: Optional[Mapping[str, str]] = None,
             requires: Optional[Tuple[Optional[str], Optional[str]]] = None,
             body_signals: bool = True,
             worktree_hosts: Optional[Tuple[str, ...]] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """``(portable, not_portable_rule, route_class)``; route_class in
    {'pin', 'any', 'policy'} when portable, else None.

    New cards are portable unless a constraint says otherwise; a parent link
    is not a constraint. ``workspace_bind`` is ``(rule, other_id, host)``
    (spec §2.4, t_6d5bc652): the card shares ``other_id``'s directory
    (``workspace_of:<id>`` or ``workspace_shared:<id>``) and runs where that
    card ran: ``host`` None = local (the bind rule is the reason), else a
    remote pin to ``host`` (route class ``pin``; a ``host:`` line naming a
    different host contradicts the bind and holds the card). ``has_links``
    is only the ``lineage_pins: legacy | kinds`` kill switch; under ``none``
    the dispatcher passes False.

    ``requires`` is ``(hard_rule, soft_rule)`` from
    :mod:`kanban_host_capabilities` (t_8c474c2e): the ``requires:<cap>``
    rule of an enforced capability no candidate pool host has, or None.
    The HARD one is checked before pin resolution, so ``host: any`` never
    spills it; the SOFT one after it, so ``host: any`` overrides it (spec
    s2.2 rule order, F3). ``body_signals`` False drops the in-code body list
    (the capability map owns the terms while it enforces, F13).

    ``hard_ok`` (scratch, unlinked, empty local workspace, allowlisted
    assignee whose profile is not a native-command lane) is required for every
    remote placement; a pin only picks the
    route and never overrides it (RC-1). ``pool`` needs ``profiles``,
    ``studio_bound_skills`` and ``hosts`` (known host ids). An unpinned card
    whose work is bound to the Studio (:func:`studio_affinity`) is not
    portable; ``manifest`` carries the skill-manifest half of that check.
    ``targets`` (``{skill: host}``, manifest ``host_affinity: <host>``) makes
    the card host-bound to those hosts (:func:`reach_allowed_hosts`).

    ``worktree_hosts`` (t_6d9e7d90, :mod:`kanban_worktree_port`): the pool
    hosts holding a git mirror of a ``worktree`` card's repo. Non-empty, the
    card is judged like a scratch card (the dispatcher clones it on the
    chosen host); a pin to a host outside that set keeps it local.
    """
    kind = workspace_kind or "scratch"
    if kind != "scratch" and not (kind == "worktree" and worktree_hosts):
        return False, _reasons.WORKSPACE_KIND, None
    bind_rule, _bind_other, bind_host = workspace_bind or (None, None, None)
    if bind_rule and bind_host is None:
        return False, bind_rule, None
    # A remote bind (Apollo r1 on #2032, Prism f399797453ae): the directory is
    # on ``bind_host``, so the Studio-local facts (a lineage link, content at
    # the same path on the Studio) say nothing about it. The rules that ask
    # whether ANY pool host may run the card still apply; a refusal there is
    # a wait at the dispatcher, never a local run against an empty path.
    if has_links and not bind_rule:
        return False, _reasons.LINKED, None
    if workspace_has_content and not bind_rule:
        return False, _reasons.LOCAL_FILES, None
    if not assignee or assignee not in tuple(pool.profiles or ()):
        return False, _reasons.PROFILE, None
    if native_command:
        # foreign_lane.worker_command runs the lane with a LOCAL Popen; the
        # ssh terminal backend never sees it, so placement would be a lie.
        return False, _reasons.NATIVE_COMMAND, None
    hard_rule, soft_rule = requires or (None, None)
    if hard_rule:
        return False, hard_rule, None
    # Access-bound (spec v1.2 §2.6, t_e99ef75c): the ACCESS is Studio-only, so
    # no pin and no reach matrix can move it. Decided before host_reach (Momus
    # pass 2 R-F: a stale matrix must not decide the name) and before every pin
    # branch (a pool pin is a contradiction the dispatcher parks needs_input).
    access = access_bound_for(body, pool)
    if access is not None:
        return False, _reasons.access_bound(access), None
    pin, ignored = resolve_pin(body, pool.hosts or (), getattr(pool, "pin_aliases", None))
    if bind_rule:
        # The bind is the pin; a body pin may only agree with it (spec §5).
        if pin not in (None, PIN_ANY, bind_host.lower()):
            return False, bind_rule, None
        pin, ignored = bind_host.lower(), None
    if pin == PIN_STUDIO:
        return False, _reasons.HOST_STUDIO, None
    if kind == "worktree" and pin not in (None, PIN_ANY) and pin not in worktree_hosts:
        return False, _reasons.WORKSPACE_KIND, None  # no mirror on the pinned host
    if pin is None and ignored is not None:
        # A host: line that names no host (typo, unregistered name): never
        # portable, a typo must not widen the blast radius (t_cb2b888d).
        return False, _reasons.PIN_UNKNOWN_HOST, None
    # Host-bound card (t_1e9ea7e9): it drives a fleet host over ssh, so only a
    # worker host with a PROVEN route (fresh reach matrix row, rc 0 to every
    # target) may take it. No such host, or no fresh matrix: it stays local
    # (a remote pin to a non-reaching host waits, never runs locally, I-11).
    allowed = reach_allowed_hosts(body=body, skills=skills, pool=pool, targets=targets)
    if allowed is not None:
        if pin is not None and pin != PIN_ANY and pin not in allowed:
            return False, _reasons.HOST_REACH, None
        if pin in (None, PIN_ANY) and not allowed:
            return False, _reasons.HOST_REACH, None
    if pin == PIN_ANY:
        return True, None, "any"
    if pin is not None:
        # A KNOWN pool host: a remote pin. A disabled one waits at take()
        # and never falls back to a local spawn. An unknown word is no pin.
        return True, None, "pin"
    if soft_rule:
        return False, soft_rule, None
    affinity = studio_affinity(body, skills, pool, manifest=manifest, body_signals=body_signals)
    if affinity is not None:
        return False, affinity[0], None
    return True, None, "policy"


def access_bound_for(body: Optional[str], pool) -> Optional[str]:
    """:func:`access_bound_cap` against this pool's alias vocabulary: the
    reach-matrix aliases plus every known pool host (the same set
    :func:`reach_allowed_hosts` binds on), canonicalised by ``pin_aliases``."""
    pool_hosts = tuple(getattr(pool, "hosts", ()) or ())
    aliases = (*(getattr(pool, "reach_aliases", ()) or ()), *(h.lower() for h in pool_hosts))
    return access_bound_cap(body, aliases, pin_hosts=pool_hosts,
                            ssh_hosts=getattr(pool, "ssh_hosts", None),
                            canon=getattr(pool, "pin_aliases", None))


def reach_allowed_hosts(*, body: Optional[str], skills: Iterable[str], pool,
                        targets: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, ...]]:
    """None when the card is not host-bound (any pool host may take it);
    else the worker hosts the reach matrix proves can ssh to every target
    the card names (``()`` = none: stays local). FAIL CLOSED: the target
    vocabulary is every alias the matrix file ever named PLUS every known
    pool host (so recognition never depends on a fresh sample, Prism
    13bf80849c6b), while the proof is fresh rows only: an absent or stale
    matrix recognises the target and proves nothing, so the card gets
    ``()``. ``pool`` needs ``reach``, ``reach_aliases`` and ``hosts``.

    ``targets`` (``{skill: host}``) are the hosts a skill manifest says the
    work targets (``host_affinity: ace-ai``, card t_9eeb9c40): they bind the
    card and join the named targets. A target the matrix never probed has no
    rc 0 anywhere, so it fails closed (stays local). A skill-bound card with
    no named alias still needs a host that reaches EVERYTHING."""
    pool_hosts = tuple(getattr(pool, "hosts", ()) or ())
    aliases = tuple(dict.fromkeys([*(getattr(pool, "reach_aliases", ()) or ()),
                                   *(h.lower() for h in pool_hosts)]))
    bound, named = host_bound_targets(body, skills, aliases, pin_hosts=pool_hosts,
                                      ssh_hosts=getattr(pool, "ssh_hosts", None))
    wanted = tuple(dict.fromkeys(h.lower() for h in (targets or {}).values() if h))
    if not bound and not wanted:
        return None
    reach = getattr(pool, "reach", {}) or {}
    base = named
    if bound and not named:
        # Unnamed host-bound skill (t_641d1ab8): the pool host the body names
        # ("on ACE-AI", ``host: ace-ai``) is the target; only with none, every
        # alias SOME pool host reaches. An alias nobody reaches (haos, unifi at
        # rc 255 everywhere) would hold every such card forever.
        base = body_host_targets(body, pool) or reached_aliases(reach, aliases, pool_hosts)
        if not base:
            return ()
    # Manifest targets always join (an unprobed one still fails closed,
    # Prism bf503f94e81a).
    named = tuple(dict.fromkeys([*base, *wanted]))
    return reaching_hosts(reach, named, aliases)


def body_host_targets(body: Optional[str], pool) -> Tuple[str, ...]:
    """Pool hosts a body names, as canonical ids: a ``host:`` pin to a pool
    host, then every whole-word mention of a pool host id or a separator-
    bearing registry alias (``ACE-AI``, ``ci.ace``) outside the origin/lineage
    header. ``ace-media-boot-heal.service`` is no mention (the id must end at
    a non-word, non-dash char). Word-shaped aliases (``ci``, ``media``) never
    count: they are English."""
    hosts = {h.lower() for h in (getattr(pool, "hosts", ()) or ())}
    if not hosts or not body:
        return ()
    alias_map = getattr(pool, "pin_aliases", None) or {}
    words = {k.lower(): v.lower() for k, v in alias_map.items()
             if isinstance(v, str) and v.lower() in hosts}
    words.update({h: h for h in hosts})
    words = {w: c for w, c in words.items() if not word_shaped_alias(w)}
    out: List[str] = []
    pin, _ignored = resolve_pin(body, hosts, alias_map)
    if pin in hosts:
        out.append(pin)
    if words:
        rx = re.compile(r"(?<![\w.-])(" + "|".join(
            re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")(?![\w-])",
            re.IGNORECASE)
        out += [words[m.group(1).lower()] for m in rx.finditer(without_header_lines(body))]
    return tuple(dict.fromkeys(out))


def reached_aliases(reach: Mapping[str, Mapping[str, int]], aliases: Iterable[str],
                    pool_hosts: Iterable[str] = ()) -> Tuple[str, ...]:
    """The aliases at least one fresh POOL-host row reaches (rc 0), in
    ``aliases`` order. With no pool host named, every fresh row counts.
    Empty matrix: ``()`` (fail closed)."""
    hosts = {h.lower() for h in pool_hosts or ()}
    rows = [row for h, row in reach.items() if not hosts or h.lower() in hosts]
    return tuple(a for a in aliases if any(row.get(a) == 0 for row in rows))


def studio_affinity(body: Optional[str], skills: Iterable[str], pool, *,
                    manifest: Optional[Mapping[str, str]] = None,
                    body_signals: bool = True) -> Optional[Tuple[str, str]]:
    """``(rule, matched_by)`` when the card's WORK is bound to the Studio's state
    (signed-in browsers, Messages.app, a macOS GUI session), else None.

    Four sources, first hit wins: the pool sidecar's ``studio_bound_skills``
    (rule ``studio_bound_skill``), a skill manifest that declares the binding
    (``manifest``: ``{skill: why}`` from :mod:`kanban_host_affinity`; rule
    ``studio_manifest``), the card body (rule ``studio_body``), and the
    Studio-only resources the body names that no pool host covers (rule
    ``studio_needs``, :func:`card_needs`). An explicit
    ``host:`` pin is decided before this and wins.
    """
    names = [s for s in (skills or ()) if isinstance(s, str)]
    bound = set(pool.studio_bound_skills or ())
    for s in names:
        if s in bound:
            return "studio_bound_skill", f"skill:{s}"
    for s in names:
        why = (manifest or {}).get(s)
        if why:
            return "studio_manifest", f"skill:{s} ({why})"
    if not body_signals and declares_requirements(body):
        # The capability layer enforces this tick (``body_signals`` False) and
        # judges the card's explicit ``requires:`` line; prose words never
        # outrank it (t_6d9e7d90). With the layer off the declaration is
        # enforced by nothing, so the prose signals stay (Prism cf1b3bd689e8).
        return None
    m = STUDIO_BODY_RE.search(body or "") if body_signals else None
    if m:
        return "studio_body", f"body:{m.group(0)}"
    # Studio-only resources the body names (t_aea826a9): bound unless some
    # pool host's fresh reach-matrix caps cover every one of them.
    if needs_allowed_hosts(body, pool, honor_declared=not body_signals) == ():
        return STUDIO_NEEDS_RULE, "needs:" + ",".join(sorted(card_needs(body)))
    return None


# -- probe + plan -----------------------------------------------------------

def host_threshold(ncpu: int, capacity_pct: float) -> float:
    """Remote load1 bar: ONE encoding (I-7)."""
    return capacity_pct * ncpu


def _ssh_argv(host: PoolHost, remote_cmd: str) -> List[str]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4"]
    if host.ssh_port != 22:
        argv += ["-p", str(host.ssh_port)]
    return argv + [host.target, remote_cmd]


@dataclass(frozen=True)
class HostSample:
    """One probe round-trip: ``/proc/loadavg``, the online core count, the
    remote clock, the raw pressure file text ("" when unreadable) and the
    boards the host can host (None = not asked)."""

    load1: float
    ncpu: int
    remote_now: Optional[float] = None
    pressure_text: str = ""
    boards: Optional[frozenset] = None
    load5: Optional[float] = None
    # Continuation affinity (t_8bfdcc07): path -> empty/nonempty/unknown for
    # the workspaces asked about in the same round-trip. None = not asked.
    workspace_states: Optional[Mapping[str, str]] = None


# ncpu = online cores (KWLB D-3), not ``nproc``: load1 counts every runnable
# task on the host, while ``nproc`` honours the probing user's cgroup quota
# (kanbanw's user-1001.slice CPUQuota=800% made it print 8 on 24/32 cores,
# t_2dc3be98), which shrank the v0.1 refuse line to 0.8 x 8 = 6.4 load1.
_NCPU_CMD = "getconf _NPROCESSORS_ONLN"


# Separates the board list (t_9fb4d477) from the load/pressure lines.
BOARDS_MARKER = "@@hermes-hostable-boards"


def _boards_cmd(workspaces_root: str) -> str:
    """The board dirs under ``workspaces_root`` that exist AND that the probing
    uid (the worker's ``kanbanw``) can create a workspace in."""
    import shlex
    root = shlex.quote(str(workspaces_root).rstrip("/") or "/")
    return (f"echo {BOARDS_MARKER}; for d in {root}/*/; do "
            f'[ -d "$d" ] && [ -w "$d" ] && [ -x "$d" ] && basename "$d"; done; true')


# Separates the workspace-state JSON (t_8bfdcc07) from everything before it.
STATES_MARKER = "@@hermes-workspace-states"
WORKSPACE_STATES = ("empty", "nonempty", "unknown")

# Paths arrive as argv (shlex-quoted), never inside the script text. Absence
# counts as ``empty`` only when the board dir above it exists: an unmounted
# workspace root must read ``unknown``, not "state lost".
_STATES_SCRIPT = (
    "import os,sys,json\n"
    "o={}\n"
    "for p in sys.argv[1:]:\n"
    " try:\n"
    "  with os.scandir(p) as it: o[p]='nonempty' if next(it,None) is not None else 'empty'\n"
    " except FileNotFoundError: o[p]='empty' if not os.path.lexists(p) and os.path.isdir(os.path.dirname(p.rstrip('/'))) else 'unknown'\n"
    " except OSError: o[p]='unknown'\n"
    "print(json.dumps(o))\n"
)


class HostKeyChanged(Exception):
    """ssh refused the host key: the host may have been re-imaged (F17)."""


def _states_cmd(paths: Sequence[str]) -> str:
    import shlex
    args = " ".join(shlex.quote(str(p)) for p in paths)
    return f"echo {STATES_MARKER}; python3 -c {shlex.quote(_STATES_SCRIPT)} {args}; true"


def _probe_cmd(pressure_path: Optional[str], workspaces_root: Optional[str] = None,
               workspace_paths: Sequence[str] = ()) -> str:
    tail = f"; {_boards_cmd(workspaces_root)}" if workspaces_root else ""
    if workspace_paths:
        tail += f"; {_states_cmd(workspace_paths)}"
    if pressure_path is None:
        return f"cat /proc/loadavg; {_NCPU_CMD}{tail}"
    import shlex
    # ONE round-trip (RC9): the age is remote_now - at, both from this host.
    return (f"cat /proc/loadavg; {_NCPU_CMD}; date +%s; cat {shlex.quote(pressure_path)} 2>/dev/null; true"
            f"{tail}")


def probe_host(h: PoolHost, runner: Callable = subprocess.run, *,
               pressure_path: Optional[str] = None,
               workspaces_root: Optional[str] = None,
               workspace_paths: Sequence[str] = ()) -> Union[None, Tuple[float, int], HostSample]:
    """``(load1, ncpu)`` from one ssh call, or None (fail closed: 0 slots).
    With ``pressure_path`` the same call also returns the remote clock and
    the pressure file as a :class:`HostSample`. With ``workspaces_root`` it
    also lists the boards the host can host (``HostSample.boards``,
    t_9fb4d477): a board whose root is missing or unwritable is absent.
    With ``workspace_paths`` the same call reports each path's state
    (``HostSample.workspace_states``, t_8bfdcc07) and a changed host key
    raises :class:`HostKeyChanged` instead of reading as plain unreachable."""
    try:
        proc = runner(
            _ssh_argv(h, _probe_cmd(pressure_path, workspaces_root, workspace_paths)),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or "").lower()
        if workspace_paths and getattr(proc, "returncode", 1) == 255 and (
                "host key verification failed" in err
                or "remote host identification has changed" in err):
            raise HostKeyChanged(h.name)
        return None
    out = proc.stdout or ""
    states: Optional[Dict[str, str]] = None
    if workspace_paths:
        out, sep, listed = out.partition(STATES_MARKER + "\n")
        if not sep:
            return None  # the state list never printed: fail closed
        states = {}
        try:
            got = json.loads(listed.strip().split("\n")[0] or "null")
        except ValueError:
            got = None
        # The host answered (the marker printed); a state script that failed
        # (no python3, bad output) proves nothing about the files: unknown,
        # never "unreachable" (HOST_LOST) and never "empty" (state lost).
        got = got if isinstance(got, dict) else {}
        states = {p: got[p] if got.get(p) in WORKSPACE_STATES else "unknown" for p in workspace_paths}
    boards: Optional[frozenset] = None
    if workspaces_root:
        out, sep, listed = out.partition(BOARDS_MARKER + "\n")
        if not sep:
            return None  # the board list never printed: fail closed
        boards = frozenset(b.strip() for b in listed.split("\n") if b.strip())
    lines = out.split("\n")
    try:
        load1 = float(lines[0].split()[0])
        ncpu = int(lines[1].strip())
    except (IndexError, ValueError):
        return None
    if ncpu <= 0:
        return None
    if pressure_path is None:
        if boards is None and states is None:
            return (load1, ncpu)
        return HostSample(load1, ncpu, boards=boards, workspace_states=states)
    try:
        remote_now: Optional[float] = float(lines[2].strip())
    except (IndexError, ValueError):
        remote_now = None
    try:
        load5: Optional[float] = float(lines[0].split()[1])
    except (IndexError, ValueError):
        load5 = None
    return HostSample(load1, ncpu, remote_now, "\n".join(lines[3:]).strip(), boards,
                      load5=load5, workspace_states=states)


# -- per-(host, board) workspace faults (t_9fb4d477) --------------------------
# A spawn whose remote ``mkdir`` failed is a HOST fault: the pair takes no
# card for BOARD_FAULT_SECONDS. In-process on purpose: the dispatcher is the
# one long-lived reader, and after a restart the probe's board list re-proves
# (or refuses) the pair on the first tick.
BOARD_FAULT_SECONDS = 600
_board_faults: Dict[Tuple[str, str], Tuple[float, str]] = {}


def mark_board_fault(host: str, board: str, why: str, *, now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    _board_faults[(host.lower(), board)] = (now + BOARD_FAULT_SECONDS, why)


def board_fault(host: str, board: str, *, now: Optional[float] = None) -> Optional[str]:
    """Why the pair is quarantined, or None."""
    got = _board_faults.get((host.lower(), board))
    if got is None:
        return None
    now = time.monotonic() if now is None else now
    if now >= got[0]:
        _board_faults.pop((host.lower(), board), None)
        return None
    return got[1]


# -- target pressure + hysteresis (Placement PRD Phase 1b) -------------------

BAND_OK, BAND_WARM, BAND_HOT, BAND_UNKNOWN = "ok", "warm", "hot", "unknown"
TARGET_STATE_FILE = "kanban-target-state.json"


def _kanban_unified_admit(signal: Any = None) -> bool:
    """True only where the ONE rule actually replaces the 6 GB/worker cap (D9, t_3694ab3c; Prism f4d9c1273272):
    KANBAN_UNIFIED_ADMIT on, a placement table on the signal, and ``kanban.admission: utilization``, i.e. exactly
    when kanban_headroom.unified_filter runs for every take (pinned ones included). Anywhere else the cap binds."""
    from hermes_cli import kanban_load_gate as _klg
    from hermes_cli.fleet_placement import placement as _pl
    return (_klg.unified_admit() and signal is not None and getattr(signal, "placement_table", None) is not None
            and _pl.admission_mode() == "utilization")


@dataclass
class TargetSignal:
    """What ``plan()`` needs to read target pressure (read_signal = true)."""

    policy: Any                       # placement_policy.PlacementPolicy
    state_path: Path                  # <root>/var/kanban-target-state.json
    ledger_dir: Path                  # <root>/var/placement
    cpu_est: Optional[float] = None   # kanban's measured slope; None = policy prior
    clock: Callable[[], float] = time.time
    # A fixed ledger read for _warm_pick (diagnostics --placement keeps one
    # snapshot per report). None = read fresh per pick (RC7, the dispatcher).
    reservations: Optional[List[Any]] = None
    # Saturation 4.2 (t_31350981): kanban.placement.admit_on_memory. False =
    # the memory/band cap is computed and reported only (D1: Ace's yes flips it).
    admit_on_memory: bool = False
    reservation_gb: float = _admission.DEFAULT_RESERVATION_GB
    # The ONE placement scorer (t_d6ce856e): <root>/config/fleet-hosts.json and
    # the headroom reservation ledger. None = no table: the band pick below.
    placement_table: Any = None
    placement_reservations: Optional[Path] = None
    # A fixed headroom-ledger read (diagnostics); None = read per pick.
    placement_live: Optional[Mapping[str, Any]] = None
    # kanban.placement.allocd (t_4187c8f1): off | shadow | on, read per tick.
    allocd: str = "off"

    def kanban_cpu_est(self) -> float:
        if self.cpu_est is not None and self.cpu_est > 0:
            return float(self.cpu_est)
        return float(self.policy.consumer("kanban", "cpu_est_prior") or 0.0)


def read_pressure(remote_now: Optional[float], text: str, *,
                  stale_after_s: float) -> Tuple[Optional[Tuple[float, str]], str]:
    """``((at, band), "")`` or ``(None, reason)`` = UNKNOWN (I-3).

    ``age = remote_now - at``, both read on the target in one round-trip, so
    the reader's clock is never involved (RC9). The band is the one the host's
    sampler PUBLISHED for kanban (``band.kanban`` in host-pressure.json,
    ``scripts/host-pressure.py``); it is consumed, never recomputed from
    ``load_ratio`` here (allocation 2.3, t_08de4ba5: the gap #3917 named). A
    file with no ``band.kanban``, or one the sampler left null, is UNKNOWN.
    """
    if not text:
        return None, "pressure unreadable"
    if remote_now is None:
        return None, "pressure unknown (no remote clock)"
    try:
        doc = json.loads(text)
    except ValueError:
        return None, "pressure unparseable"
    if not isinstance(doc, dict):
        return None, "pressure unparseable"
    if not math.isfinite(remote_now):
        return None, "pressure unknown (no remote clock)"
    at = doc.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None, "pressure has no at"
    age = remote_now - float(at)
    if age > stale_after_s:
        return None, f"pressure unknown (stale {age:.0f}s)"
    bands = doc.get("band")
    band = bands.get("kanban") if isinstance(bands, dict) else None
    if band not in (BAND_OK, BAND_WARM, BAND_HOT):
        reasons = doc.get("band_reason")
        why = reasons.get("kanban") if isinstance(reasons, dict) else None
        return None, "pressure has no band.kanban" + (f" ({why})" if why else "")
    return (float(at), band), ""


def _load_state(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    hosts = doc.get("hosts") if isinstance(doc, dict) else None
    return hosts if isinstance(hosts, dict) else {}


def advance_streak(prev: Optional[Mapping], at: Optional[float], band: str, *,
                   hot_streak: int, clear_streak: int, now: float,
                   discard_after_s: float) -> dict:
    """One sample's step of the per-host hysteresis (F-5). A repeated ``at``
    is a no-op; state older than ``discard_after_s`` is discarded first.

    UNKNOWN neither builds nor erases a streak (I-3, F-5 addendum t_36840c8a):
    the state is kept as-is, ``updated`` included, so an earned hot verdict
    still needs ``clear_streak`` fresh non-hot samples and a host that stays
    UNKNOWN past ``discard_after_s`` starts over. ``plan()`` refuses the
    UNKNOWN tick itself."""
    st = dict(prev) if isinstance(prev, Mapping) else {}
    updated = st.get("updated")
    if not isinstance(updated, (int, float)) or now - float(updated) > discard_after_s:
        st = {}
    if band == BAND_UNKNOWN or at is None:
        return {"at": st.get("at"), "hot": bool(st.get("hot")), "hot_run": int(st.get("hot_run") or 0),
                "clear_run": int(st.get("clear_run") or 0), "updated": st.get("updated")}
    if st.get("at") == at:
        return st
    hot, hot_run, clear_run = bool(st.get("hot")), int(st.get("hot_run") or 0), int(st.get("clear_run") or 0)
    if band == BAND_HOT:
        hot_run, clear_run = hot_run + 1, 0
        hot = hot or hot_run >= hot_streak
    else:
        hot_run, clear_run = 0, clear_run + 1
        if hot and clear_run >= clear_streak:
            hot = False
    return {"at": at, "hot": hot, "hot_run": hot_run, "clear_run": clear_run, "updated": now}


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
    pins_only: bool = False              # admitting band, worker_pool.overflow off: remote PINS only
    # kanban.placement.default == remote this tick (spec v0.3 §3.1): every
    # portable row is offered to take() before a local slot.
    default_remote: bool = False
    signal: Optional[TargetSignal] = None  # None = KWLB v0.1 (read_signal false)
    taken: Dict[str, int] = field(default_factory=dict)  # this tick's takes, per host
    # host -> why the memory/band cap is below its free slots (t_31350981);
    # filled with the flag off too, so the dry readback has its numbers.
    admission: Dict[str, str] = field(default_factory=dict)
    last_pick: Optional[str] = None      # hysteresis for placement.choose()
    last_decision: Any = None            # the placement.Decision behind the last take()
    last_reservation: Optional[str] = None  # the headroom reservation the last take() wrote
    dry_run: bool = False                # True = choose() only, never write the ledger
    # Per pool host, one entry per take, newest last: the reservation id that
    # take wrote, or None (dry run / no table). release() refunds exactly one.
    _rids: Dict[str, List[Optional[str]]] = field(default_factory=dict)
    # Dry-run takes per host: never in the ledger, so choose() adds them.
    _simulated: Dict[str, int] = field(default_factory=dict)
    # Reservations alloc_client booked (kanban.placement.allocd on): rid -> whether
    # that take debited ``slots`` (a band-closed host has none to debit).
    _alloc: Dict[str, bool] = field(default_factory=dict)
    # Continuation affinity (t_8bfdcc07): (host, workspace) -> state, read in
    # this tick's probe round-trip. ``continuation_probed`` = the paths were
    # asked for, so an absent pair means the host did not answer.
    continuation_states: Dict[Tuple[str, str], str] = field(default_factory=dict)
    continuation_probed: bool = False

    @property
    def budget(self) -> int:
        """Free pool slots this tick. With kanban.placement.allocd on, the band /
        memory cap is allocd's call, so only the structural slots count here."""
        from hermes_cli import kanban_headroom as _kh
        if _kh.plan_allocd_mode(self) == _kh.ALLOCD_ON:
            return sum(_kh.structural_free(self, n) for n in self.hosts)
        return sum(self.slots.values())

    def full_reason(self) -> str:
        """Why nothing is free: ``pool_full``, or with the signal on and a
        host held back by pressure, ``pool_unavailable (<host>: <why>; ...)``."""
        if self.signal is None:
            return "pool_full"
        held = [f"{n}: {d['pressure']}" for n, d in self.detail.items() if d.get("pressure")]
        return f"pool_unavailable ({'; '.join(held)})" if held else "pool_full"

    def _warm_pick(self, names: List[str]) -> str:
        """Every free rung is warm: the least ``projected()`` wins, after a
        fresh ledger read (RC7) plus this tick's own takes (not written yet)."""
        sig = self.signal
        assert sig is not None
        now = sig.clock()
        if sig.reservations is not None:
            res = list(sig.reservations)
        else:
            try:
                res = _ledger.read_all(sig.ledger_dir, sig.policy, now=now)
            except Exception:  # an unreadable ledger never blocks a pick
                res = []
        cost = sig.kanban_cpu_est()

        def proj(n: str) -> float:
            load1 = float((self.detail.get(n) or {}).get("load1") or 0.0)
            return _ledger.projected(n, load1, res, now=now) + cost * self.taken.get(n, 0)

        return min(names, key=lambda n: (proj(n), n))

    def placement_reserved(self) -> Mapping[str, Any]:
        """Live headroom reservations (other ticks' spawns) per host."""
        sig = self.signal
        if sig is None or sig.placement_table is None:
            return {}
        if sig.placement_live is not None:
            return sig.placement_live
        from hermes_cli.fleet_placement import placement as _placement
        # A corrupt ledger raises LedgerError: take() refuses (never reads it as empty).
        return _placement.live_reservations(sig.placement_reservations, now=sig.clock(),
                                            table=sig.placement_table)

    def probe(self, name: str) -> Optional[dict]:
        d = self.detail.get(name) or {}
        if d.get("load1") is None:
            return None
        return {"load1": d.get("load1"), "ncpu": d.get("ncpu")}

    def pin_host_refusal(self, pin: str, skip_admission: bool = False) -> Optional[str]:
        """Why the HOST behind ``pin`` cannot give a slot now, or None.

        Host state only: no profile check and no reservation, so the no-slot
        wall can name the state too. ``take()`` checks the assignee before
        this. A host that is not ``active`` in fleet-roles.json (``draining``)
        reads ``pin_host_<state>``, never ``pin_host_full``: full means wait,
        a drained member means re-pin or wait for re-admit (t_e10add5a)."""
        pin = {n.lower(): n for n in self.hosts}.get(pin.lower(), pin)
        host = self.hosts.get(pin)
        if host is None:
            low = pin.lower()
            return ("pin_host_disabled" if low in {d.lower() for d in self.disabled}
                    else "pin_host_dropped"
                    if low in {h.lower() for h in (self.config.hosts or ())}
                    else "pin_unknown_host")
        if skip_admission:
            # kanban admit (t_32e8796a): no slot / band / pressure verdict; the
            # host's fleet-roles state still binds.
            return None if host.state == "active" else f"pin_host_{host.state}"
        if self.slots.get(pin, 0) > 0:
            return None
        if host.state != "active":
            return f"pin_host_{host.state}"
        d = self.detail.get(pin) or {}
        if self.signal is not None and d.get("pressure"):
            return f"pin_host_{'hot' if d.get('hot') else 'unknown'} ({d['pressure']})"
        return "pin_host_unreachable" if d.get("reachable") is False else "pin_host_full"

    def board_refusal(self, name: str, board: Optional[str]) -> Optional[str]:
        """Why host ``name`` cannot host ``board``'s workspaces, or None
        (t_9fb4d477). The probe lists the boards whose workspace root exists
        and is writable by the worker uid; a host the probe did not ask
        (no ``kanban.workspaces_root``) is not filtered."""
        if not board:
            return None
        fault = board_fault(name, board)
        if fault is not None:
            return f"board_fault: {fault}"
        boards = (self.detail.get(name) or {}).get("boards")
        if boards is not None and board not in boards:
            return "no_board_root"
        return None

    def take(self, assignee: Optional[str], pin: Optional[str] = None,
             only: Optional[Collection[str]] = None,
             board: Optional[str] = None, job: Any = None,
             job_id: Optional[str] = None, skip_admission: bool = False) -> Optional[PoolHost]:
        """Reserve one slot: on ``pin`` only, else (signal + placement table)
        ``placement.choose()`` over the hosts that serve ``assignee`` with
        ``job`` overrides (t_d6ce856e); without a table the band pick. ``only``
        (a host-bound card, t_1e9ea7e9) restricts the unpinned pick to those
        hosts; an empty ``only`` takes nothing (``host_reach``). ``board`` drops
        every host that cannot host that board (:meth:`board_refusal`) before
        the pick: the card spills to another host or waits ``board_unhostable``.
        None sets ``refusal``."""
        self.refusal = None
        self.last_decision = None
        self.last_reservation = None
        # Pins and reach rows are lower-cased; host keys are compared case-
        # folded so a mixed-case pool id still matches (Prism d4be9ccd2c7f).
        fold = {n.lower(): n for n in self.hosts}
        if pin is not None:
            pin = fold.get(pin.lower(), pin)
            host = self.hosts.get(pin)
            if host is not None and assignee not in host.profiles:
                self.refusal = "pin_profile"  # the pinned host does not serve this assignee
                return None
            self.refusal = self.pin_host_refusal(pin, skip_admission=skip_admission)
            if self.refusal is not None:
                return None
            why = self.board_refusal(pin, board)
            if why is not None:
                self.refusal = f"pin_host_board_unhostable ({pin}: {board} {why})"
                return None
            # a pin keeps its precedence, not an exemption from the ONE rule (Prism f4d9c1273272: with the
            # memory cap folded into admit_class, a pinned take must pass may_start too)
            unified = not skip_admission and _kanban_unified_admit(self.signal)
            if unified and self.dry_run:          # a dry run books nothing: the read-only verdict is its answer
                from hermes_cli import kanban_headroom as _kh
                _keep, held = _kh.unified_filter(self, [pin], job)
                if pin in held:
                    self.refusal = f"pin_host_unified_admission ({pin}: {held[pin]})"
                    return None
            if self.signal is not None and self.signal.placement_table is not None:
                # A pin keeps its precedence but its footprint is booked like any
                # take, so later admissions see it (Prism 4609a040ad20 / b8c13f6c2e7a).
                rid = None
                if self.dry_run:
                    self._simulated[pin] = self._simulated.get(pin, 0) + 1
                else:
                    from hermes_cli import kanban_headroom as _kh
                    from hermes_cli.fleet_placement import placement as _placement
                    try:
                        # unified: may_start under the ledger lock, booking what it admitted on
                        # (Prism c8d0343d7fb6 / 8e0e895ec5d4)
                        rid = _kh.book_pin(self, pin, job_id, job, check=unified)
                    except _kh.ClassRefusal as exc:
                        self.refusal = f"pin_host_unified_admission ({pin}: {exc})"
                        return None
                    except _placement.PlacementError as exc:  # corrupt ledger: fail closed
                        self.refusal = f"pool_unavailable ({type(exc).__name__}: {exc})"
                        return None
                    self.last_reservation = rid
                self._rids.setdefault(pin, []).append(rid)
            self.slots[pin] = self.slots.get(pin, 0) - 1
            self.taken[pin] = self.taken.get(pin, 0) + 1
            return host
        if only is not None:
            allowed = {fold.get(n.lower(), n) for n in only}
            if not any(n in allowed for n in self.hosts):
                self.refusal = "host_reach"
                return None
        else:
            allowed = set(self.hosts)
        unhostable = {n: w for n in allowed if n in self.hosts
                      for w in [self.board_refusal(n, board)] if w is not None}
        if unhostable:
            allowed = allowed - set(unhostable)
            if not allowed:
                self.refusal = "board_unhostable (" + "; ".join(
                    f"{n}: {board} {w}" for n, w in sorted(unhostable.items())) + ")"
                return None
        if self.signal is not None and self.signal.placement_table is not None:
            from hermes_cli import kanban_headroom as _kh
            names = [n for n, h in self.hosts.items() if n in allowed and assignee in h.profiles]
            if not names:
                self.refusal = "pool_full"
                return None
            names, held = _kh.unified_filter(self, names, job)
            if not names:
                self.refusal = "unified_admission (" + "; ".join(
                    f"{n}: {w}" for n, w in sorted(held.items())) + ")"
                return None
            mode = _kh.plan_allocd_mode(self)
            if mode == _kh.ALLOCD_ON:
                return self._take_allocd(names, job, job_id)
            ask = _kh.shadow_ask(self, names, job, job_id) if mode == _kh.ALLOCD_SHADOW else None
            host = self._take_scored(names, job, job_id)
            if ask is not None:
                _kh.shadow_record(self, ask, host, job_id)
            return host
        if self.signal is not None:
            free = [n for n, h in self.hosts.items()
                    if n in allowed and self.slots.get(n, 0) > 0 and assignee in h.profiles]
            if free:
                # Only an OK rung is cool. Warm rungs, then hot-but-not-yet-
                # streaked rungs, are picked by projected load (RC7).
                bands = {n: (self.detail.get(n) or {}).get("band") for n in free}
                cool = [n for n in free if bands[n] == BAND_OK]
                warm = [n for n in free if bands[n] == BAND_WARM]
                name = cool[0] if cool else self._warm_pick(warm or free)
                self.slots[name] -= 1
                self.taken[name] = self.taken.get(name, 0) + 1
                return self.hosts[name]
            self.refusal = self.full_reason()
            return None
        for name, host in self.hosts.items():
            if name in allowed and self.slots.get(name, 0) > 0 and assignee in host.profiles:
                self.slots[name] -= 1
                return host
        self.refusal = "pool_full"
        return None

    def _take_scored(self, names: List[str], job: Any, job_id: Optional[str]) -> Optional[PoolHost]:
        """The in-process scorer take (kanban.placement.allocd off / shadow): one
        locked ``placement.admit`` (``choose`` on a dry run) over ``names``."""
        from hermes_cli import kanban_headroom as _kh
        from hermes_cli.fleet_placement import placement as _placement
        rid = None
        try:
            if self.dry_run:
                d = _kh.choose(self, names, job)
            else:
                d, rid = _kh.admit(self, names, job, job_id)
        except _placement.PlacementError as exc:  # LedgerError / ExclusionsError: fail closed
            self.refusal = f"pool_unavailable ({type(exc).__name__}: {exc})"
            return None
        self.last_decision = d
        key = _kh.pool_key(self.signal.placement_table, names, d.host)
        if key is None or self.slots.get(key, 0) <= 0:
            _kh.release_id(self.signal, rid)
            self.refusal = f"pool_unavailable ({d.reason or 'no_slot'}: " + "; ".join(
                f"{r.host} {r.reason}" for r in d.rows if not r.eligible) + ")"
            return None
        if self._class_refused(key, job, rid, release=lambda: _kh.release_id(self.signal, rid)):
            return None
        self.last_reservation = rid
        self._rids.setdefault(key, []).append(rid)
        if self.dry_run:
            self._simulated[key] = self._simulated.get(key, 0) + 1
        self.slots[key] -= 1
        self.taken[key] = self.taken.get(key, 0) + 1
        self.last_pick = key
        return self.hosts[key]

    def _take_allocd(self, names: List[str], job: Any, job_id: Optional[str]) -> Optional[PoolHost]:
        """kanban.placement.allocd on (t_4187c8f1): ``alloc_client.admit`` picks AND
        books among ``names`` that still have a structural pool slot; the pressure
        band is allocd's. A refusal is final this tick (never a local spill)."""
        from hermes_cli import kanban_headroom as _kh
        cands = [n for n in names if _kh.structural_free(self, n) > 0]
        if not cands:
            self.refusal = "pool_full"
            return None
        try:
            r = _kh.allocd_admit(self, cands, job, job_id)
        except Exception as exc:  # alloc_client.Refused, or the fallback's PlacementError: fail closed
            self.refusal = f"pool_unavailable (allocd {type(exc).__name__}: {exc})"
            return None
        self.last_decision = r
        if not r.host or not r.reservation_id:
            self.refusal = f"pool_unavailable (allocd: {r.reason or 'refused'})"
            return None
        key = _kh.pool_key(self.signal.placement_table, cands, r.host)
        if key is None:
            _kh.release_alloc(r.reservation_id)
            self.refusal = f"pool_unavailable (allocd picked {r.host}, not a candidate)"
            return None
        if self._class_refused(key, job, r.reservation_id, release=lambda: _kh.release_alloc(r.reservation_id)):
            return None
        debit = self.slots.get(key, 0) > 0
        if debit:
            self.slots[key] -= 1
        self._alloc[r.reservation_id] = debit
        self.last_reservation = r.reservation_id
        self._rids.setdefault(key, []).append(r.reservation_id)
        self.taken[key] = self.taken.get(key, 0) + 1
        self.last_pick = key
        return self.hosts[key]

    def _class_refused(self, key: str, job: Any, rid: Optional[str], *, release) -> bool:
        """The ONE rule's verdict for a take the old locked rule just booked, UNDER the ledger lock (Prism
        c8d0343d7fb6): refused = the row is already dropped, ``release`` clears the booker's own state, refusal
        set. An unreadable ledger fails closed the same way. False = admitted (or the rule is off)."""
        from hermes_cli import kanban_headroom as _kh
        try:
            why = _kh.confirm_class(self, key, job, rid)
        except Exception as exc:  # LedgerError / lock fault: fail closed
            why = f"closed:{type(exc).__name__}: {exc}"
        if why is None:
            return False
        release()
        self.refusal = f"unified_admission ({key}: {why})"
        return True

    def reservation_via(self, rid: Optional[str]) -> Optional[str]:
        """``alloc_client`` when ``rid`` was booked by :meth:`_take_allocd`, else None."""
        from hermes_cli import kanban_headroom as _kh
        return _kh.VIA_ALLOC_CLIENT if rid and rid in self._alloc else None

    def release(self, host: PoolHost, reservation: Optional[str] = None) -> None:
        """Refund a take() whose claim/spawn failed: the slot, and the headroom
        reservation THAT take wrote. ``reservation`` names it (the dispatcher
        passes ``last_reservation``); without it the newest take on the host is
        refunded. Every take, pinned or not, has its own entry, so a failed take
        never releases an earlier live worker's promise (Prism b711acd04da4).
        A ``reservation`` this plan does not hold (already refunded, another
        plan's) is a no-op plus a journal line: never the newest row instead
        (Prism 8b0ce035f2c1)."""
        rids = self._rids.get(host.name)
        if reservation is not None and reservation not in (rids or ()):
            _log.warning("kanban pool: refund of reservation %s on %s ignored: not held by this plan "
                         "(already refunded?)", reservation, host.name)
            return
        rid = reservation if reservation is not None else (rids[-1] if rids else None)
        via_alloc = bool(rid) and rid in self._alloc
        # an allocd take on a band-closed host debited no slot, so it refunds none
        debited = self._alloc.pop(rid) if via_alloc else True
        if host.name in self.slots:
            if debited:
                self.slots[host.name] += 1
            if self.taken.get(host.name):
                self.taken[host.name] -= 1
            if self._simulated.get(host.name):
                self._simulated[host.name] -= 1
        if not rids:
            return
        if reservation is not None:
            rids.remove(reservation)
        else:
            rids.pop()
        if rid:
            from hermes_cli import kanban_headroom as _kh
            if via_alloc:
                _kh.release_alloc(rid)
            else:
                _kh.release_id(self.signal, rid)

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
            + (f" band={d.get('band')}{' HOT' if d.get('hot') else ''}" if self.signal is not None else "")
            for n, d in self.detail.items()
        )


def plan(hosts: List[PoolHost], running_by_host: Mapping[str, int], *,
         probe: Callable[[PoolHost], Any] = probe_host,
         disabled: Collection[str] = (), planned_at: Optional[float] = None,
         config: Optional[PoolConfig] = None,
         signal: Optional[TargetSignal] = None,
         workspace_paths: Optional[Mapping[str, Sequence[str]]] = None) -> SpilloverPlan:
    """ONE plan per gateway tick. A host takes work only when it is active,
    enabled, under its slots and probed under ``capacity_pct x ncpu`` (I-3).
    A host with no free slot or not active is not probed.

    With ``signal`` the load bar is the host's pressure band instead: the
    per-host streak advances here, once per new remote ``at`` (F-5), and a
    host is refused when its streak says hot or its pressure is UNKNOWN.

    ``workspace_paths`` (host -> paths, t_8bfdcc07) rides the same probe: a
    host holding continuation state is probed even with no free slot, and
    the states land on ``plan.continuation_states``."""
    states: Dict[Tuple[str, str], str] = {}
    if signal is not None:
        out = _plan_with_signal(hosts, running_by_host, probe=probe, disabled=disabled,
                                planned_at=planned_at, config=config, signal=signal,
                                workspace_paths=workspace_paths or {}, states=states)
    else:
        out = _plan_v01(hosts, running_by_host, probe=probe, disabled=disabled,
                        planned_at=planned_at, config=config,
                        workspace_paths=workspace_paths or {}, states=states)
    out.continuation_states = states
    out.continuation_probed = workspace_paths is not None
    return out


def _probe_states(probe, h: PoolHost, probed: bool, paths: Sequence[str],
                  states: Dict[Tuple[str, str], str]):
    """One probe call: the load sample when ``probed`` (a host with a free
    slot), and the workspace states when ``paths``. A host probed only for
    its states never gains a slot from that sample."""
    if not (probed or paths):
        return None
    try:
        sample = probe(h, workspace_paths=tuple(paths)) if paths else probe(h)
    except HostKeyChanged:
        states.update({(h.name, p): "host_key_mismatch" for p in paths})
        return None
    got = getattr(sample, "workspace_states", None)
    if paths and isinstance(got, Mapping):
        states.update({(h.name, p): got[p] for p in paths if got.get(p) in WORKSPACE_STATES})
    return sample if probed else None


def _plan_v01(hosts, running_by_host, *, probe, disabled, planned_at, config,
              workspace_paths, states) -> SpilloverPlan:
    slots: Dict[str, int] = {}
    detail: Dict[str, dict] = {}
    for h in sorted(hosts, key=lambda x: x.priority):
        running = int(running_by_host.get(h.name, 0) or 0)
        free = max(0, h.slots - running) if (h.enabled and h.state == "active") else 0
        probed = free > 0
        sample = _probe_states(probe, h, probed, workspace_paths.get(h.name, ()), states)
        load1: Optional[float] = None
        ncpu: Optional[int] = None
        boards = None
        if sample is None:
            free = 0
        else:
            if isinstance(sample, HostSample):
                load1, ncpu, boards = sample.load1, sample.ncpu, sample.boards
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
            **({"boards": sorted(boards)} if boards is not None else {}),
        }
    return _finish_plan(hosts, slots, detail, disabled, planned_at, config)


def _finish_plan(hosts, slots, detail, disabled, planned_at, config, signal=None) -> SpilloverPlan:
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
        config=config, signal=signal,
    )


def _plan_with_signal(hosts, running_by_host, *, probe, disabled, planned_at, config,
                      signal: TargetSignal, workspace_paths=None, states=None) -> SpilloverPlan:
    workspace_paths = workspace_paths or {}
    states = {} if states is None else states
    policy = signal.policy
    now = signal.clock()
    discard_after = policy.stale_after_s * 3
    ordered = sorted(hosts, key=lambda x: x.priority)
    # Pass 1: probe. samples[name] = (at, band) for every host probed this tick.
    probed: Dict[str, dict] = {}
    samples: Dict[str, Tuple[Optional[float], str]] = {}
    for h in ordered:
        running = int(running_by_host.get(h.name, 0) or 0)
        free = max(0, h.slots - running) if (h.enabled and h.state == "active") else 0
        sample = _probe_states(probe, h, free > 0, workspace_paths.get(h.name, ()), states)
        row = {"running": running, "free": free, "load1": None, "ncpu": None,
               "band": None, "why": "", "probed": free > 0, "cap": (None, ""), "boards": None}
        if isinstance(sample, HostSample):
            row["load1"], row["ncpu"], row["boards"] = sample.load1, sample.ncpu, sample.boards
            row["cap"] = _admission.cap(sample.pressure_text, reservation=signal.reservation_gb)
            if signal.placement_table is not None:
                from hermes_cli import kanban_headroom as _kh
                row["placement"] = _kh.sample_fields(sample.load5, sample.remote_now, sample.pressure_text,
                                                     stale_after_s=policy.stale_after_s, local_now=now)
            got, row["why"] = read_pressure(sample.remote_now, sample.pressure_text,
                                            stale_after_s=policy.stale_after_s)
            at, row["band"] = (None, BAND_UNKNOWN) if got is None else got
            samples[h.name] = (at, row["band"])
        elif sample is not None:  # a v0.1-shape probe: no pressure read
            row["load1"], row["ncpu"] = sample
            row["band"], row["why"] = BAND_UNKNOWN, "pressure not probed"
            samples[h.name] = (None, BAND_UNKNOWN)
        elif free > 0:
            row["band"], row["why"] = BAND_UNKNOWN, "host unreachable"
            samples[h.name] = (None, BAND_UNKNOWN)
        probed[h.name] = row

    # Pass 2: advance every streak against the COMMITTED state, under the
    # state file's lock, so two planners on one root never lose a sample.
    def advance(old: Mapping) -> Mapping:
        hs = old.get("hosts") if isinstance(old.get("hosts"), dict) else {}
        out = dict(hs)
        for name, (at, band) in samples.items():
            kb = policy.kanban_band(name)
            out[name] = advance_streak(hs.get(name), at, band, hot_streak=kb.hot_streak,
                                       clear_streak=kb.clear_streak, now=now,
                                       discard_after_s=discard_after)
        return {"hosts": out, "updated": now}

    try:
        committed = _ledger.update_json(signal.state_path, advance).get("hosts") or {}
    except Exception as exc:  # unsaved state: decide on this tick's step from what is on disk
        _log.warning("kanban pool: target state not saved: %s", exc)
        committed = advance({"hosts": _load_state(signal.state_path)})["hosts"]

    slots: Dict[str, int] = {}
    detail: Dict[str, dict] = {}
    admission: Dict[str, str] = {}
    for h in ordered:
        r = probed[h.name]
        band, why, free = r["band"], r["why"], r["free"]
        st = committed.get(h.name)
        hot = bool(isinstance(st, Mapping) and st.get("hot")) and band not in (None, BAND_UNKNOWN)
        if band == BAND_UNKNOWN:
            free = 0
        elif hot:
            free = 0
            why = f"band hot (streak {policy.kanban_band(h.name).hot_streak}, {band} now)"
        else:
            limit, cap_why = r["cap"]
            if limit is not None and limit < free:
                admission[h.name] = f"cap {limit} < free {free}: {cap_why}"
                # D9 (t_3694ab3c): under KANBAN_UNIFIED_ADMIT the memory axis is admit_class's (measured estimate,
                # not 6 GB per worker); the cap is still computed and reported, never enforced
                if signal.admit_on_memory and not _kanban_unified_admit(signal):
                    free = limit
                    if free == 0:
                        why = f"admission: {cap_why}"
        slots[h.name] = free
        detail[h.name] = {
            "slots": h.slots, "running": r["running"], "load1": r["load1"], "ncpu": r["ncpu"],
            "state": h.state, "enabled": h.enabled,
            "reachable": (band not in (None, BAND_UNKNOWN)) if r["probed"] else None,
            "band": band, "hot": hot,
            **({"pressure": why} if why else {}),
            **({"admission": admission[h.name]} if h.name in admission else {}),
            **({"boards": sorted(r["boards"])} if r["boards"] is not None else {}),
            **(r.get("placement") or ({"sample_status": "absent"} if signal.placement_table is not None
                                      and r["probed"] else {})),
        }
    plan = _finish_plan(hosts, slots, detail, disabled, planned_at, config, signal)
    plan.admission = admission
    return plan
