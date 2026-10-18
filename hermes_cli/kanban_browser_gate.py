"""The Browser Sandboxes resolution gate, switched on for the Phase 1 lanes (t_42728faf, P1-4).

Spec v1.2 s3.3 (three keys), s3.8 step 2, pass-3 R2 (three Phase 1 lanes) and R4 (creator provenance),
AC-1.13 (kill switch) and AC-1.15 (unmigrated lanes do not move; a claimed lane does not grant).

ONE function, :func:`decide`, answers for one ``browser:*`` need on one card. The live dispatcher tick
and ``kanban dispatch --dry-run`` both reach it through :func:`gate_rows` (one code path, pass-3 lens).
Order of the checks is load-bearing:

1. ``browsers.mode`` != ``on`` -> ``legacy_route {reason: mode_off}``. Read per tick from
   the host-local browser-hosts.json browserd reads (:func:`registry_path`); nothing is cached across
   ticks, so a flip needs no restart (AC-1.13).
2. the card's lane (``lane_of``) is not in ``migrated_lanes`` -> ``legacy_route {reason:
   lane_not_migrated}``. No ledger read, no ``BROWSER_*`` token: an unmigrated lane is placed exactly
   as today (AC-1.15 (i)/(ii)).
3. ``browser:ephemeral`` holds no identity: a lease plan on the T3 host (ci-box when the worker's host
   runs no ``browserd``, s3.3).
4. identity row: absent -> ``BROWSER_IDENTITY_UNPROVISIONED``; ``sealed_at`` unset -> ``legacy_route
   {reason: unsealed}``; ledger unreadable -> a wait (the card stays ``ready``).
5. class: ``read`` unless the body carries ``browser_class: act``.
6. creator provenance (R4): a card whose ``created`` event says ``minted_by.class = worker`` is refused
   ``BROWSER_CLASS_REFUSED {reason: creator_provenance}`` unless the identity's policy lists ``worker``
   under ``creators``. LABELLED RESIDUAL: ``minted_by`` is derived from the minting process's env
   (``HERMES_KANBAN_TASK``), so a trusted worker that mints from a scrubbed env is classified
   ``script``/``hand``. Accepted for Phase 1 ``read`` (no money path); Phase 2 ``act`` policy keys on it.
7. policy (key 3): the assignee must be listed for the derived class -> else ``BROWSER_CLASS_REFUSED
   {reason: policy}``.

A passing card is a ``lease`` plan. The lease itself is the s7 browser-resolution JOIN, which stays OFF
until P1-8 (``kanban.browser_join``, default off): with the join off a lease plan holds the card
``ready`` (``browser_join_off``) and never spawns it on a legacy profile.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from hermes_cli import kanban_needs as kn

MODE_ON = "on"
EPHEMERAL_HOST = "ci-box"            # s3.3 / AC-1.1: the T3 default host when the worker's host has no browserd
BROWSERD_HOSTS = ("studio", "ci-box")  # hosts that run a browserd in Phase 1
CLASS_RE = re.compile(r"^[ \t]*browser_class:[ \t]*([a-z]+)[ \t]*\r?$", re.IGNORECASE | re.MULTILINE)
LEASE_PLAN_EVENT = "browser_lease_plan"
REFUSED_EVENT = "browser_refused"
WAIT_JOIN_OFF = "browser_join_off"
WAIT_LEDGER = "browser_ledger_unreadable"
WAIT_GATE_ERROR = "browser_gate_error"


class LedgerUnreadable(Exception):
    """The identity lookup could not reach the ledger this tick."""


@dataclass(frozen=True)
class Plan:
    """A need that passed the gate: lease it before spawn (s3.8 step 2)."""
    need: str
    lane: str
    klass: str
    identity: Optional[str] = None
    host: Optional[str] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in (("need", self.need), ("lane", self.lane), ("class", self.klass),
                                  ("identity", self.identity), ("host", self.host)) if v is not None}


@dataclass(frozen=True)
class Wait:
    reason: str
    need: str
    lane: str


@dataclass
class CardVerdict:
    """Everything the gate decided for one card this tick."""
    legacy: list = field(default_factory=list)    # legacy_route payloads
    refusal: Optional[kn.Refusal] = None          # the first refusal (it parks the card)
    refusal_payload: Optional[dict] = None
    plans: list = field(default_factory=list)     # Plan
    wait: Optional[Wait] = None

    @property
    def holds(self) -> bool:
        """True when the card must not spawn this tick (refused, waiting, or a lease is due)."""
        return self.refusal is not None or self.wait is not None or bool(self.plans)


def derive_class(body: Optional[str]) -> str:
    m = CLASS_RE.search(body or "")
    return "act" if m and m.group(1).lower() == "act" else "read"


def identity_name(need: str) -> str:
    return "id-" + need[len(kn.BROWSER_PREFIX):]


def _names(v) -> frozenset:
    """A policy list as a set of profile names; any other shape (a typo'd ``true``, a string) grants nobody."""
    return frozenset(x for x in v if isinstance(x, str)) if isinstance(v, (list, tuple)) else frozenset()


def _policy(row: Mapping) -> dict:
    p = row.get("policy")
    if isinstance(p, str):
        try:
            p = json.loads(p or "{}")
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def ephemeral_host(worker_host: Optional[str], local: Iterable[str] = ()) -> str:
    """The browserd host for a worker's ``browser:ephemeral``: its own when it runs one (the Studio, ci-box, or a
    host in ``browsers.local_ephemeral_hosts``, t_2dca69d0), else ci-box."""
    return worker_host if worker_host in BROWSERD_HOSTS or (worker_host and worker_host in local) else EPHEMERAL_HOST


def decide(need: str, *, lane: str, registry: kn.LaneRegistry, assignee: Optional[str], body: Optional[str],
           minted_by: Optional[Mapping], lookup: Callable[[str], Optional[Mapping]],
           worker_host: Optional[str] = None):
    """One need -> a legacy_route payload (dict), a :class:`kn.Refusal`, a :class:`Wait` or a :class:`Plan`."""
    if registry.mode != MODE_ON:
        return {"need": need, "lane": lane, "reason": "mode_off"}
    if lane == kn.LANE_NONE or lane not in registry.migrated:
        return {"need": need, "lane": lane, "reason": "lane_not_migrated"}
    if need == kn.EPHEMERAL:
        return Plan(need, lane, "act", host=ephemeral_host(worker_host, registry.local_ephemeral))
    name = identity_name(need)
    try:
        row = lookup(name)
    except LedgerUnreadable:
        return Wait(WAIT_LEDGER, need, lane)
    if row is None:
        return kn.refusal("BROWSER_IDENTITY_UNPROVISIONED", f"Ace must run the ritual for {name} (lane {lane})")
    if not row.get("sealed_at"):
        return {"need": need, "lane": lane, "reason": "unsealed"}
    klass = derive_class(body)
    pol = _policy(row)
    if (minted_by or {}).get("class") == "worker" and "worker" not in _names(pol.get("creators")):
        return kn.refusal("BROWSER_CLASS_REFUSED",
                          f"reason=creator_provenance {name} class={klass}: card minted by a worker run "
                          f"({(minted_by or {}).get('task') or '?'})")
    if not assignee or assignee not in _names(pol.get(klass)):
        return kn.refusal("BROWSER_CLASS_REFUSED",
                          f"reason=policy {name} class={klass}: profile {assignee or '(none)'} is not allowed")
    return Plan(need, lane, klass, identity=name, host=row.get("host"))


def card_verdict(row: Mapping, *, registry: kn.LaneRegistry, lookup, minted_by: Optional[Mapping],
                 worker_host: Optional[str] = None) -> Optional[CardVerdict]:
    """The gate for every ``browser:*`` need of one ready card, or None when it has none."""
    needs = kn.declared_resources(row["body"])
    if not needs:
        return None
    try:
        skills = json.loads(row["skills"] or "[]") or []
    except (TypeError, ValueError):
        skills = []
    lane = kn.lane_of(skills, row["body"], registry)
    out = CardVerdict()
    for need in needs:
        v = decide(need, lane=lane, registry=registry, assignee=row["assignee"], body=row["body"],
                   minted_by=minted_by, lookup=lookup, worker_host=worker_host)
        if isinstance(v, kn.Refusal):
            if out.refusal is None:
                out.refusal = v
                out.refusal_payload = {"need": need, "lane": lane, "token": v.token, "detail": v.detail}
        elif isinstance(v, Wait):
            out.wait = out.wait or v
        elif isinstance(v, Plan):
            out.plans.append(v)
        else:
            out.legacy.append(v)
    return out


def minted_by_of(conn, task_ids: Iterable[str]) -> dict:
    """``{task_id: created.minted_by}`` (the kernel's provenance, kanban_db._minted_by)."""
    ids = list(dict.fromkeys(task_ids))
    out: dict = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for tid, payload in conn.execute(
                f"SELECT task_id, payload FROM task_events WHERE kind = 'created' AND task_id IN ({marks})", chunk):
            try:
                mb = (json.loads(payload or "{}") or {}).get("minted_by")
            except ValueError:
                mb = None
            if isinstance(mb, dict):
                out[tid] = mb
    return out


def socket_lookup(sock_path: str, *, call=None) -> Callable[[str], Optional[Mapping]]:
    """Identity lookup through the local browserd (``identity`` verb, read-only). The ledger is
    ``_browserd``-owned 0700 on the Studio, so the dispatcher never opens it directly."""
    from hermes_cli import kanban_browser_lease as kbl
    call = call or kbl._call
    cache: dict = {}

    def lookup(name: str):
        if name not in cache:
            try:
                resp = call(sock_path, {"verb": "identity", "args": {"name": name}, "auth": {}})
            except (OSError, ValueError) as exc:
                raise LedgerUnreadable(str(exc)) from exc
            if not resp.get("ok"):
                raise LedgerUnreadable(str(resp.get("refusal") or "refused"))
            cache[name] = resp.get("identity")
        return cache[name]
    return lookup


DEFAULT_SOCKET = "/var/run/browserd/browserd.sock"


def gate_rows(rows, *, registry: kn.LaneRegistry, lookup, minted_by: Mapping, worker_host=None) -> dict:
    """``{task_id: CardVerdict}`` for the ready rows that carry a browser need. ONE path: the live
    tick and the dry run both call this (and nothing else decides a browser need)."""
    out = {}
    for r in rows:
        wh = worker_host(r) if callable(worker_host) else worker_host
        v = card_verdict(r, registry=registry, lookup=lookup, minted_by=minted_by.get(r["id"]),
                         worker_host=wh if isinstance(wh, str) else None)
        if v is not None:
            out[r["id"]] = v
    return out


# The host-local registry browserd reads on each host (fleet/browsers/install-studio.sh, install-linux.sh).
HOST_LOCAL_REGISTRY = {"darwin": "/usr/local/etc/browsers/browser-hosts.json",
                       "linux": "/opt/browsers/browser-hosts.json"}
FALLBACK_EVENT = "browser_registry_fallback"
_fallback_logged: set = set()


def registry_path(fleet_dir: Path, configured=None, *, platform: Optional[str] = None) -> tuple:
    """``(path, source)``: the browser-hosts.json the gate reads this tick (t_458bbb42).

    Order: ``kanban.browser_hosts_file`` (root config) when that file exists, then this OS's host-local
    copy (the one browserd reads), then the repo seed ``<fleet_dir>/browser-hosts.json`` (source
    ``fallback``). The repo file is committed ``mode: off``, so the kill switch lives on the host."""
    if platform is None:
        from hermes_platform.host.facts import os_family
        platform = os_family()
    for source, p in (("config", configured), ("host_local", HOST_LOCAL_REGISTRY.get(platform))):
        if isinstance(p, str) and p.strip() and Path(p).expanduser().exists():
            return Path(p).expanduser(), source
    return Path(fleet_dir) / kn.REGISTRY_FILE, "fallback"


def load(fleet_dir: Path, configured=None, *, platform: Optional[str] = None, log=None) -> kn.LaneRegistry:
    """The lane registry, read per tick from :func:`registry_path`. A fallback to the repo seed logs one
    ``browser_registry_fallback`` warning per process and path (visible, never per tick)."""
    if platform is None:
        from hermes_platform.host.facts import os_family
        platform = os_family()
    path, source = registry_path(fleet_dir, configured, platform=platform)
    if source == "fallback" and str(path) not in _fallback_logged:
        _fallback_logged.add(str(path))
        if log is not None:
            log.warning("%s: no host-local browser-hosts.json (kanban.browser_hosts_file=%r, %s); "
                        "reading the repo seed %s", FALLBACK_EVENT, configured,
                        HOST_LOCAL_REGISTRY.get(platform) or "no default for this OS", path)
    return kn.load_registry_file(path)
