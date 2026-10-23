"""The generic ``needs`` record and the browser refusal tokens (t_ecfd5591).

Spec: affinity spec v2 s8 (``needs: [{kind: host-cap|resource, name}]``) and
Browser Sandboxes spec v1.2 s3.3/s3.10 (the ONE list of six refusals, RC-N6).

This module ships the SHAPE and the TOKENS. It resolves nothing: with
``migrated_lanes = []`` (the only value this card ships, Momus pass 2) every
``browser:*`` need is a ``legacy_route`` and the card is placed exactly as
today. Switching resolution on for a lane is P1-4's job (t_42728faf).

* :func:`needs_of` turns a card's derived requirements into the record:
  host capabilities become ``{kind: host-cap, name: <cap>}``; a
  ``browser:<app>-<account>`` / ``browser:ephemeral`` line becomes
  ``{kind: resource, name: ...}``.
* :func:`lane_of` is the s3.3 lane rule: the first card skill, in card order,
  that is in the lane registry (``migrated_lanes`` + ``legacy_allowlist``),
  else the card's ``origin:`` id if registered, else ``none``.
* :func:`resolve` is the s3.3 gate in its Phase-2 form: a browser need on a
  lane that is not migrated returns ``legacy_route``; anything else is a
  :class:`Refusal` with its named exit shape. It never leases.
* :data:`REFUSALS` maps each token to its exit: ``needs_input`` with the
  token as the first line, except ``BROWSER_LEASE_WAIT`` which keeps the
  card ``ready`` until its timeout. None of them re-routes the card (there
  is no wrong host for a browser).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

HOST_CAP, RESOURCE = "host-cap", "resource"
KINDS = (HOST_CAP, RESOURCE)

BROWSER_PREFIX = "browser:"
EPHEMERAL = "browser:ephemeral"
LEGACY_ROUTE_EVENT = "legacy_route"
LANE_NONE = "none"
REGISTRY_FILE = "browser-hosts.json"

NEEDS_INPUT, READY_UNTIL_TIMEOUT = "needs_input", "ready_until_timeout"

# The one list (RC-N6; spec v1.2 s3.3 "nowhere else counts them").
REFUSALS: Mapping[str, str] = {
    "BROWSER_IDENTITY_UNPROVISIONED": NEEDS_INPUT,
    "BROWSER_LEASE_AMBIGUOUS": NEEDS_INPUT,
    "BROWSER_LEASE_WAIT": READY_UNTIL_TIMEOUT,
    "BROWSER_CLASS_REFUSED": NEEDS_INPUT,
    "BROWSER_EGRESS_UNVERIFIED": NEEDS_INPUT,
    "BROWSER_LEDGER_UNREACHABLE": NEEDS_INPUT,
}

NEEDS_RE = re.compile(r"^[ \t]*needs:[ \t]*(.*?)[ \t]*\r?$", re.IGNORECASE | re.MULTILINE)
_NAME_RE = re.compile(r"^browser:[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class Need:
    kind: str
    name: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name}


@dataclass(frozen=True)
class Refusal:
    token: str
    detail: str = ""

    @property
    def exit(self) -> str:
        return REFUSALS[self.token]

    @property
    def rerouting(self) -> bool:
        return False   # a browser refusal never moves the card (spec v1.2 s3.10)

    def line(self) -> str:
        """The block reason: the token is always the first word of the first line."""
        return f"{self.token}: {self.detail}".rstrip(": ")


def refusal(token: str, detail: str = "") -> Refusal:
    if token not in REFUSALS:
        raise ValueError(f"{token!r} is not one of the six browser refusals")
    return Refusal(token, detail)


def declared_resources(body: Optional[str]) -> List[str]:
    """``browser:*`` names on line-anchored ``needs:`` lines, lower-cased."""
    out: List[str] = []
    for m in NEEDS_RE.finditer(body or ""):
        for w in re.split(r"[,\s\[\]]+", m.group(1).lower()):
            w = w.strip("`'\".;{}")
            if w.startswith(BROWSER_PREFIX) and _NAME_RE.fullmatch(w) and w not in out:
                out.append(w)
    return out


def needs_of(host_caps: Iterable[str], body: Optional[str]) -> List[dict]:
    """The generic record for one card: host capabilities then resources."""
    out = [Need(HOST_CAP, c) for c in dict.fromkeys(host_caps or ()) if c]
    out += [Need(RESOURCE, r) for r in declared_resources(body)]
    return [n.as_dict() for n in out]


def validate(needs: Sequence[Mapping]) -> List[str]:
    """Problems with a stored ``needs`` list (empty = valid)."""
    bad = []
    for i, n in enumerate(needs or ()):
        if not isinstance(n, Mapping) or set(n) != {"kind", "name"}:
            bad.append(f"needs[{i}] must be exactly {{kind, name}}")
        elif n["kind"] not in KINDS:
            bad.append(f"needs[{i}].kind {n['kind']!r} is not one of {KINDS}")
        elif not isinstance(n["name"], str) or not n["name"]:
            bad.append(f"needs[{i}].name must be a non-empty string")
        elif n["kind"] == RESOURCE and not _NAME_RE.fullmatch(n["name"]):
            bad.append(f"needs[{i}].name {n['name']!r} is not a browser:<app>-<account> resource")
    return bad


# -- lanes (s3.3) -------------------------------------------------------------

@dataclass(frozen=True)
class LaneRegistry:
    migrated: Tuple[str, ...] = ()
    legacy: Tuple[str, ...] = ()
    mode: str = "off"   # browsers.mode (AC-1.13 kill switch); absent = off
    # browsers.local_ephemeral_hosts (t_2dca69d0): Linux hosts whose OWN browserd serves their workers'
    # browser:ephemeral (ace-ai, ace-media). Absent/empty = every non-browserd worker host uses ci-box.
    local_ephemeral: Tuple[str, ...] = ()

    @property
    def names(self) -> frozenset:
        return frozenset(self.migrated) | frozenset(self.legacy)


def load_registry(fleet_dir: Path) -> LaneRegistry:
    """``browsers.{migrated_lanes, legacy_allowlist, mode}`` from ``browser-hosts.json``, read per
    call (never cached: a flip takes effect on the next tick). The lists live under ``browsers``
    (the schema browserd reads); a top-level copy is read as a fallback. Absent file or keys =
    empty registry, mode off (nothing migrated); an existing unreadable/malformed file RAISES."""
    return load_registry_file(Path(fleet_dir) / REGISTRY_FILE)


def load_registry_file(path: Path) -> LaneRegistry:
    """:func:`load_registry` for an explicit file (the host-local copy browserd reads, t_458bbb42)."""
    path = Path(path)
    if not path.exists():
        return LaneRegistry()
    # An EXISTING file that cannot be read or parsed raises (Prism da4f556e96bb): the dispatcher's browser gate
    # holds every browser card on that fault instead of treating the registry as "mode off".
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level is not an object")
    raw_sect = doc.get("browsers")
    sect: dict = raw_sect if isinstance(raw_sect, dict) else {}

    def _list(key):
        v = sect.get(key, doc.get(key))
        return tuple(str(x).strip().lower() for x in v if isinstance(x, str) and x.strip()) \
            if isinstance(v, list) else ()
    mode = sect.get("mode")
    return LaneRegistry(migrated=_list("migrated_lanes"), legacy=_list("legacy_allowlist"),
                        mode=str(mode).strip().lower() if isinstance(mode, str) else "off",
                        local_ephemeral=_list("local_ephemeral_hosts"))


_ORIGIN_RE = re.compile(r"^[ \t]*origin:[ \t]*([^\s·]+)", re.IGNORECASE | re.MULTILINE)


def lane_of(skills: Iterable[str], body: Optional[str], registry: LaneRegistry) -> str:
    from hermes_cli.kanban_host_capabilities import skill_key
    names = registry.names
    for s in skills or ():
        if isinstance(s, str) and skill_key(s) in names:
            return skill_key(s)
    m = _ORIGIN_RE.search(body or "")
    if m and m.group(1).strip().lower() in names:
        return m.group(1).strip().lower()
    return LANE_NONE


def resolve(need: str, lane: str, registry: LaneRegistry) -> "dict | Refusal":
    """The Phase-2 gate: an unmigrated lane is ``legacy_route``; a migrated
    one is not resolvable by this card (no ledger read yet) and refuses with
    ``BROWSER_IDENTITY_UNPROVISIONED`` rather than guessing a lease."""
    if lane == LANE_NONE or lane not in registry.migrated:
        return {"need": need, "lane": lane, "reason": "lane_not_migrated"}
    return refusal("BROWSER_IDENTITY_UNPROVISIONED",
                   f"no sealed identity is readable for {need} (lane {lane}); Ace must run the ritual")
