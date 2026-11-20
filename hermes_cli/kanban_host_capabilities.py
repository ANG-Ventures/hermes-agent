"""Host capabilities for kanban placement (Fleet Work Placement Phase 1, t_8c474c2e).

Spec: Obsidian ``AI/Infrastructure/Fleet Work Placement -- Host Affinity and
Continuation (spec, 2026-10-11).md`` v2, s1.3, s2.2, s2.3, s7 Phase 1.

A card can need something only some hosts have: Ace's signed-in browser
(``browser-session``), an unlocked GUI (``screen``), Messages.app... The pool
decides by capability, not by host name. Two hand-written files under the
fleet root say who has what and which skills need what:

* ``host-capabilities.json``: ``capabilities.<cap>`` (``what``, ``sniff``
  regexes) and ``hosts.<id>.capabilities``. It owns every term list (F13);
  this module holds none.
* ``skill-host-requires.json``: ``skills.<name>`` = capability list.

A card's requirements are the union of (s2.2, F12):

* attached skills via the index, and an explicit ``requires: a, b`` line: HARD;
* index skills NAMED in the body (backticked, "skill X", "load X") and
  body-sniff hits: SOFT. ``host: any`` or ``requires: none`` overrides them.

Kill switch ``kanban.placement.capabilities: off | warn | enforce`` (root
config, read per tick, F5; absent = ``warn``) plus
``kanban.placement.capabilities_enforce`` (default ``[browser-session,
screen]``): in ``enforce`` the listed capabilities change routes, every other
one is ``warn`` (event only). ``enforce`` takes effect only while the skill
index is GENERATED (``generated_by: fleet-host capabilities --sync`` and a
``source_hash`` that matches its ``skills``); a hand-written or hand-edited
index keeps the whole layer at ``warn`` and logs why once an hour.

Failure modes (s1.3, F11): an absent map means the layer is OFF and routes
are byte-identical to before it existed. A malformed map degrades every
capability to ``warn`` for the tick and never refuses the pool.

Stdlib only: ``kanban_worker_pool`` (and through it hermes-home's
placement watch) imports this module's results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from hermes_cli.kanban_placement_reasons import ACCESS_CONTROL_PLANE_READ as _ACCESS_CONTROL_PLANE_READ

MAP_FILE = "host-capabilities.json"
INDEX_FILE = "skill-host-requires.json"
CAPS_ENV = "KANBAN_PLACEMENT_CAPS"

OFF, WARN, ENFORCE = "off", "warn", "enforce"
MODES = (OFF, WARN, ENFORCE)
DEFAULT_MODE = WARN
# The index header that lets ``enforce`` act (t_8c474c2e review r2). The drift
# lint (hermes-home ``fleet-host capabilities --check``) re-derives the index
# from every SKILL.md and compares it against ``source_hash``.
INDEX_GENERATOR = "fleet-host capabilities --sync"
DEFAULT_ENFORCE = ("browser-session", "screen")
# t_f935cb6f: ``browser-session`` split into ``browser-identity`` (a signed-in identity, by lease),
# ``browser-public`` (the agent's own Chromium) and ``lan-reach``. The old word stays an alias of
# ``browser-identity`` for one release (``capabilities.browser-session.alias_of`` in the map). Both
# names of the identity capability: what ``browser_remote`` grants or vetoes on a pool host.
BROWSER_IDENTITY_CAPS = frozenset({"browser-identity", "browser-session"})
# kanban.placement.body_requires_mode (t_640a4ea0): ``advisory`` (default) = a
# body-sniffed pin that no host may serve never holds the card; ``enforce`` =
# the pre-t_640a4ea0 hold. An explicit ``requires:`` line is always enforced.
ADVISORY = "advisory"
BODY_MODES = (ADVISORY, ENFORCE)
# kanban.placement.prose_pins_advisory (t_c82eded0, root config, read per tick, default on): a capability
# inferred from card PROSE (a body sniff, a skill named in the body) that no pool host serves no longer
# holds an unpinned card on the Studio; it is recorded as an advisory and the card starts on a pool host.
# Only a declared ``requires:`` line, an attached skill or a structural fact holds it. false = the old hold.
PROSE_KNOB = "prose_pins_advisory"
BODY_IGNORED = "requires_body_ignored"
BODY_IGNORED_EVENT = "placement_body_ignored"
# ONE comment + ONE #logs line per card held at host=none past one tick.
HELD_EVENT = "placement_requires_held"
HELD_PREFIX = "held by requires:"
NONE_WORD = "none"   # reserved: ``requires: none`` overrides every SOFT requirement (F4/F12)

HARD, SOFT = "hard", "soft"
# matched_by values, in the order a pin is attributed (declared beats skill).
DECLARED, SKILL, NAMED_SKILL, BODY = "declared", "skill", "named_skill", "body"
# ``ssh <alias> ... sudo`` on an alias a capability serves (t_79c2259b): a structural access fact, not
# prose, so no body/prose advisory path may drop it while access_bound releases the alias (Prism 86b7cb2c2b27).
ACCESS = "access"
_SOURCE_RANK = {DECLARED: 0, ACCESS: 1, SKILL: 2, NAMED_SKILL: 3, BODY: 4}
PROSE_SOURCES = frozenset({NAMED_SKILL, BODY})   # inferred from card text, never declared (t_c82eded0)

RULE_PREFIX = "requires:"
UNKNOWN_PREFIX = "unknown:"
COMMENT_PREFIX = "auto-pinned:"
MAP_INVALID_EVENT = "placement_map_invalid"
# A warn-mode observation is NOT a binding: it has its own event kind so it
# never shadows the card's ``placement_affinity`` (Prism 415306946489).
WARN_EVENT = "placement_affinity_warn"
STUDIO_HOST = "mac-studio"
STUDIO_ROW = "studio"          # the Studio's row in host-capabilities.json
IDENTITY_ONLY = "identity-only"
UNSATISFIED = "requires_unsatisfied"

REQUIRES_RE = re.compile(r"^[ \t]*requires:[ \t]*(.*?)[ \t]*\r?$", re.IGNORECASE | re.MULTILINE)
_WORD_SPLIT_RE = re.compile(r"[,\s]+")
# A trailing human note on the line (``requires: none — API only``,
# ``requires: browser-session (cart)``, ``# why``) is not a capability word:
# without the cut, ``api``/``only`` read as unknown words and hold the card.
_REQUIRES_NOTE_RE = re.compile(r"\s(?:—|–|--|#)|[(#]")
_PLAIN_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")


@dataclass(frozen=True)
class Requirement:
    cap: str            # a capability name, or ``unknown:<word>``
    strength: str       # hard | soft
    matched_by: str     # declared | skill | named_skill | body
    term: str           # the skill name, the body text matched, or the declared word

    @property
    def unknown(self) -> bool:
        return self.cap.startswith(UNKNOWN_PREFIX)

    @property
    def rule(self) -> str:
        return RULE_PREFIX + self.cap


@dataclass(frozen=True)
class CapabilityMap:
    """The two files, read once per tick. ``error`` set = malformed (F11)."""

    capabilities: Tuple[str, ...] = ()
    sniff: Tuple[Tuple[str, Any], ...] = ()          # (cap, compiled regex), file order
    hosts: Mapping[str, frozenset] = field(default_factory=dict)
    index: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    error: Optional[str] = None
    index_error: Optional[str] = None
    index_unverified: Optional[str] = None   # why ``enforce`` is demoted to ``warn``
    # Per-capability hardness from the map (spec v0.3 §4 Phase 2-pre, t_90887924):
    # ``soft: false`` = enforced whenever the layer enforces, ``soft: true`` = warn
    # only. Absent = the root config's ``capabilities_enforce`` list decides.
    soft: Mapping[str, bool] = field(default_factory=dict)
    # Capabilities the Studio row serves to an explicit ``host: studio`` pin
    # only (``hosts.studio.studio_tier: {cap: "identity-only"}``, §3.4): an
    # unpinned card needing one waits for an advertising pool host.
    identity_only: frozenset = frozenset()
    # Read-only per-host transport observations. Missing/malformed legs fail closed;
    # advertising a browser daemon is not proof that an identity lease is reachable.
    browser_remote: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)
    # ``ssh-root:<alias>`` access a capability serves (``capabilities.<cap>.ssh_root: [alias, ...]``,
    # t_79c2259b): alias (lower) -> capability. An access-bound card on a mapped alias is judged as a
    # HARD requirement of that capability, not pinned to the Studio.
    ssh_root: Mapping[str, str] = field(default_factory=dict)
    # ``capabilities.<old>.alias_of: <new>`` (t_f935cb6f): a renamed capability's old word, read as
    # the new one in every declaration and index row. A host row naming either serves both.
    aliases: Mapping[str, str] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.error is None

    def canonical(self, cap: str) -> str:
        """The capability ``cap`` names: its ``alias_of`` target, else itself."""
        return self.aliases.get(cap, cap)

    def names(self, cap: str) -> frozenset:
        """Every word for the capability ``cap`` names (the target and its aliases)."""
        target = self.canonical(cap)
        return frozenset({cap, target, *(a for a, t in self.aliases.items() if t == target)})


@dataclass(frozen=True)
class Verdict:
    """What :func:`evaluate` decided for one card.

    ``hard_rule`` / ``soft_rule`` are the ``requires:<cap>`` rules
    ``portable()`` acts on (enforced and unsatisfied only). ``pin`` is the
    requirement that produced the rule; ``observed`` the warn-mode one.
    """

    hard_rule: Optional[str] = None
    soft_rule: Optional[str] = None
    pin: Optional[Requirement] = None
    enforced: Tuple[Requirement, ...] = ()
    observed: Optional[Requirement] = None
    # Pool hosts that advertise every enforced requirement (capability-aware
    # take, t_90887924): the card is portable to these hosts only.
    only: Tuple[str, ...] = ()
    # Body-sniffed requirements dropped under ``body_requires_mode: advisory``
    # because no host may serve them (t_640a4ea0): recorded, never held.
    body_ignored: Tuple["Requirement", ...] = ()
    # ``requires_unsatisfied:<cap>``: an identity-only capability no pool host
    # advertises. The card waits; it never spawns locally unpinned (AC-5).
    wait: Optional[str] = None

    def for_portable(self) -> Optional[Tuple[Optional[str], Optional[str]]]:
        if self.hard_rule is None and self.soft_rule is None:
            return None
        return self.hard_rule, self.soft_rule


@dataclass(frozen=True)
class CapabilityTick:
    """Everything one dispatcher tick needs: the map, the per-capability mode
    and the pool hosts a card could spill to."""

    cmap: CapabilityMap
    mode: str
    enforce: Tuple[str, ...]
    pool_hosts: Tuple[str, ...]
    body_mode: str = ADVISORY
    prose_advisory: bool = False

    def mode_of(self, cap: str) -> str:
        if self.mode == OFF:
            return OFF
        if self.mode == WARN or not self.cmap.valid:
            return WARN   # F11: a malformed map degrades every capability to warn
        if cap.startswith(UNKNOWN_PREFIX):
            return ENFORCE  # F4: an unknown word keeps the card local
        soft = group_soft(self.cmap, cap)   # an alias and its target share one mode (t_f935cb6f)
        if soft is not None:
            return WARN if soft else ENFORCE   # the map's kill switch, per class
        return ENFORCE if self.cmap.names(cap) & set(self.enforce) else WARN

    @property
    def error(self) -> Optional[str]:
        """The tick's map problem (map malformed, or skill index absent/bad)."""
        return self.cmap.error or self.cmap.index_error

    @property
    def legacy_body(self) -> bool:
        """Whether #1877's in-code body list still applies: only while the
        capability layer is not enforcing a valid map (F13 moves the terms
        into the map; absent/off/warn/malformed keep today's routes)."""
        return not (self.mode == ENFORCE and self.cmap.valid)


# -- config -----------------------------------------------------------------

def read_modes(kanban_cfg: Any) -> Tuple[str, Tuple[str, ...]]:
    """``(mode, enforce_list)`` from the ROOT config's ``kanban`` section.

    YAML reads a bare ``off`` as False and ``on`` as True; both are accepted.
    Anything unrecognised reads as the default, so a typo never turns the
    layer off silently (it stays at the documented default).
    """
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    pl = pl if isinstance(pl, Mapping) else {}
    raw = pl.get("capabilities", DEFAULT_MODE)
    if raw is False:
        mode = OFF
    elif raw is True:
        mode = ENFORCE
    else:
        mode = str(raw).strip().lower()
        mode = mode if mode in MODES else DEFAULT_MODE
    enforce = pl.get("capabilities_enforce", DEFAULT_ENFORCE)
    if not isinstance(enforce, (list, tuple)):
        enforce = DEFAULT_ENFORCE
    return mode, tuple(str(c).strip().lower() for c in enforce if isinstance(c, str) and c.strip())


def read_body_mode(kanban_cfg: Any) -> str:
    """``kanban.placement.body_requires_mode`` from the ROOT config: ``advisory``
    (default; anything unrecognised) or ``enforce``."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    raw = pl.get("body_requires_mode", ADVISORY) if isinstance(pl, Mapping) else ADVISORY
    mode = str(raw).strip().lower()
    return mode if mode in BODY_MODES else ADVISORY


def read_prose_advisory(kanban_cfg: Any) -> bool:
    """``kanban.placement.prose_pins_advisory`` from the ROOT config (default on). False / "off" / "false" /
    "no" / 0 is the kill switch; anything else (a typo included) reads as on, the documented default."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    raw = pl.get(PROSE_KNOB, True) if isinstance(pl, Mapping) else True
    if isinstance(raw, str):
        return raw.strip().lower() not in ("off", "false", "no", "0")
    return raw is not False and raw != 0


def env_value(mode: str, enforce: Iterable[str]) -> str:
    """``KANBAN_PLACEMENT_CAPS``: the mode of every capability, ``*`` = the
    rest. Workers' rail gates (#1876) honour it instead of reading config."""
    if mode == ENFORCE:
        doc = {"*": WARN, **{c: ENFORCE for c in enforce}}
    else:
        doc = {"*": mode}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


# -- the files --------------------------------------------------------------

def _parse_map(doc) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, Any], ...], Dict[str, frozenset],
                             Dict[str, bool], frozenset]:
    if not isinstance(doc, dict) or doc.get("schema") != 1 or isinstance(doc.get("schema"), bool):
        raise ValueError("schema must be 1")
    caps = doc.get("capabilities")
    hosts = doc.get("hosts")
    if not isinstance(caps, dict) or not caps or not isinstance(hosts, dict):
        raise ValueError("capabilities and hosts must be objects")
    names: List[str] = []
    sniff: List[Tuple[str, Any]] = []
    for cap, row in caps.items():
        if not isinstance(cap, str) or not _PLAIN_RE.fullmatch(cap) or cap == NONE_WORD:
            raise ValueError(f"capability {cap!r} is not a plain name")
        if not isinstance(row, dict):
            raise ValueError(f"capabilities.{cap} must be an object")
        if "soft" in row and not isinstance(row["soft"], bool):
            raise ValueError(f"capabilities.{cap}.soft must be true or false")
        roots = row.get("ssh_root", [])
        if not isinstance(roots, list) or not all(isinstance(a, str) and a for a in roots):
            raise ValueError(f"capabilities.{cap}.ssh_root must be a list of ssh aliases")
        terms = row.get("sniff", [])
        if not isinstance(terms, list) or not all(isinstance(t, str) and t for t in terms):
            raise ValueError(f"capabilities.{cap}.sniff must be a list of regexes")
        for t in terms:
            try:
                sniff.append((cap, re.compile(t, re.IGNORECASE)))
            except re.error as exc:
                raise ValueError(f"capabilities.{cap}.sniff {t!r}: {exc}") from None
        names.append(cap)
    known = set(names)
    aliases = parse_aliases(caps)
    out: Dict[str, frozenset] = {}
    for hid, row in hosts.items():
        if not isinstance(hid, str) or not isinstance(row, dict) or not isinstance(row.get("capabilities"), list):
            raise ValueError(f"hosts.{hid!r} needs a capabilities list")
        bad = [c for c in row["capabilities"] if c not in known]
        if bad:
            raise ValueError(f"hosts.{hid} names unknown capabilities {bad}")
        have = set(row["capabilities"])
        for alias, target in aliases.items():   # either word serves both (t_f935cb6f)
            if alias in have or target in have:
                have |= {alias, target}
        out[hid.lower()] = frozenset(have)
    soft = {c: r["soft"] for c, r in caps.items() if "soft" in r}
    tier = (hosts.get(STUDIO_ROW) or {}).get("studio_tier", {})
    if not isinstance(tier, dict) or any(c not in known or v != IDENTITY_ONLY for c, v in tier.items()):
        raise ValueError(f"hosts.{STUDIO_ROW}.studio_tier must map known capabilities to {IDENTITY_ONLY!r}")
    return tuple(names), tuple(sniff), out, soft, frozenset(tier)


def group_soft(cmap: "CapabilityMap", cap: str) -> Optional[bool]:
    """The map's ``soft`` for ``cap``'s alias group (target and aliases share one mode, t_f935cb6f), the
    first set one in name order; None when no name of the group sets it."""
    for n in sorted(cmap.names(cap)):
        if n in cmap.soft:
            return cmap.soft[n]
    return None


def parse_aliases(caps: Mapping[str, Any]) -> Dict[str, str]:
    """``{alias: target}`` from ``capabilities.<alias>.alias_of`` (t_f935cb6f). The target must be
    a capability of the map that is not itself an alias; anything else is a malformed map."""
    out: Dict[str, str] = {}
    for cap, row in caps.items():
        if isinstance(row, dict) and "alias_of" in row:
            target = row["alias_of"]
            if not isinstance(target, str) or target == cap or not isinstance(caps.get(target), dict) \
                    or "alias_of" in caps[target]:
                raise ValueError(f"capabilities.{cap}.alias_of must name another, non-alias capability")
            out[cap] = target
    return out


def index_hash(skills: Mapping[str, Iterable[str]]) -> str:
    """``source_hash`` of a skill index: sha256 of its canonical ``skills``."""
    import hashlib
    canon = {k: list(v) for k, v in skills.items()}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _index_unverified(doc) -> Optional[str]:
    """None when the index is the generator's untouched output, else why not."""
    if doc.get("generated_by") != INDEX_GENERATOR:
        return f"{INDEX_FILE} is hand-written (no generated_by: {INDEX_GENERATOR})"
    if doc.get("source_hash") != index_hash(doc["skills"]):
        return f"{INDEX_FILE} source_hash does not match its skills (edited after --sync)"
    return None


def _parse_index(doc) -> Dict[str, Tuple[str, ...]]:
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("skills"), dict):
        raise ValueError("schema must be 1 with a skills object")
    out: Dict[str, Tuple[str, ...]] = {}
    for name, caps in doc["skills"].items():
        if not isinstance(name, str) or not name or not isinstance(caps, list) \
                or not all(isinstance(c, str) and c for c in caps):
            raise ValueError(f"skills.{name!r} must be a list of capability names")
        # Skill names are case-insensitive fleet-wide (Prism b7a3c37db15e):
        # keys fold to lower case; a case-only duplicate unions its caps.
        key = name.strip().lower()
        out[key] = tuple(dict.fromkeys([*out.get(key, ()), *(c.strip().lower() for c in caps)]))
    return out


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def load_map(fleet_dir: Path) -> Optional[CapabilityMap]:
    """The map, or None when ``host-capabilities.json`` is absent (layer off).
    Never raises: a bad file is a map with ``error`` set (F11)."""
    fleet_dir = Path(fleet_dir)
    try:
        map_bytes = _read_bytes(fleet_dir / MAP_FILE)
    except OSError as exc:
        return CapabilityMap(error=f"{MAP_FILE}: unreadable: {exc}")
    try:
        index_bytes, index_read_error = _read_bytes(fleet_dir / INDEX_FILE), None
    except OSError as exc:
        index_bytes, index_read_error = None, f"{INDEX_FILE}: {exc}"
    return map_from_bytes(map_bytes, index_bytes, index_read_error=index_read_error)


def map_from_bytes(map_bytes: Optional[bytes], index_bytes: Optional[bytes], *,
                   index_read_error: Optional[str] = None) -> Optional[CapabilityMap]:
    """:func:`load_map` over bytes already read (None = file absent). The placement gates parse
    the ONE byte pair they captured, so host capabilities, requirements and the route hash all
    describe the same files (t_ecfd5591 23e10f0f6800). Never raises."""
    if map_bytes is None:
        return None
    try:
        doc = json.loads(map_bytes.decode("utf-8-sig"))
    except ValueError as exc:
        return CapabilityMap(error=f"{MAP_FILE}: unreadable: {exc}")
    try:
        names, sniff, hosts, soft, identity_only = _parse_map(doc)
        error = None
    except ValueError as exc:
        names, sniff, hosts, error = (), (), {}, f"{MAP_FILE}: {exc}"
        soft, identity_only = {}, frozenset()
    index: Dict[str, Tuple[str, ...]] = {}
    index_error = index_read_error
    unverified = None
    if index_error is None:
        try:
            idoc = json.loads(index_bytes.decode("utf-8-sig")) if index_bytes is not None else None
            index = _parse_index(idoc) if idoc is not None else {}
            if idoc is None:
                index_error = f"{INDEX_FILE}: absent"
            else:
                unverified = _index_unverified(idoc)
        except ValueError as exc:
            index_error = f"{INDEX_FILE}: {exc}"
    return CapabilityMap(capabilities=names, sniff=sniff, hosts=hosts, index=index,
                         error=error, index_error=index_error, soft=soft,
                         identity_only=identity_only,
                         browser_remote={h.lower(): (row["browser_remote"]
                                                     if isinstance(row["browser_remote"], dict) else {})
                                         for h, row in (doc["hosts"].items() if error is None else ())
                                         if "browser_remote" in row},
                         ssh_root={a.lower(): cap for cap, row in (doc["capabilities"].items()
                                                                   if error is None else ())
                                   for a in row.get("ssh_root", ())},
                         aliases=parse_aliases(doc["capabilities"]) if error is None else {},
                         index_unverified=unverified if index_error is None else None)


def effective_mode(mode: str, cmap: CapabilityMap) -> str:
    """The configured mode, except ``enforce`` drops to ``warn`` while the
    skill index is not the generator's verified output (or is absent/bad):
    a hand list routes nothing on its own (t_8c474c2e review r2)."""
    if mode == ENFORCE and (cmap.index_error is not None or cmap.index_unverified is not None):
        return WARN
    return mode


_DEMOTED_LOGGED: Dict[str, float] = {}


def _note_demoted(cmap: CapabilityMap, now: float) -> None:
    import logging
    why = cmap.index_unverified or cmap.index_error or ""
    last = _DEMOTED_LOGGED.get(why)
    if last is not None and now - last < MAP_INVALID_LOG_SECONDS:
        return
    _DEMOTED_LOGGED[why] = now
    logging.getLogger(__name__).warning(
        "kanban placement: capabilities configured enforce but running warn: %s "
        "(run `fleet-host capabilities --sync` and land the regenerated index)", why)


def tick(fleet_dir: Path, kanban_cfg: Any, pool_hosts: Iterable[str]) -> Optional[CapabilityTick]:
    """One tick's capability context, or None when the layer is off/absent."""
    mode, enforce = read_modes(kanban_cfg)
    if mode == OFF:
        return None
    cmap = load_map(fleet_dir)
    if cmap is None:
        return None
    eff = effective_mode(mode, cmap)
    if eff != mode:
        import time as _time
        _note_demoted(cmap, _time.monotonic())
    return CapabilityTick(cmap=cmap, mode=eff, enforce=enforce,
                          pool_hosts=tuple(h.lower() for h in pool_hosts or ()),
                          body_mode=read_body_mode(kanban_cfg),
                          prose_advisory=read_prose_advisory(kanban_cfg))


# -- derivation (s2.2) ------------------------------------------------------

def _line_words(value: str) -> List[str]:
    return [w for w in (w.strip("`'\".;") for w in
                        _WORD_SPLIT_RE.split(value.strip().strip("`").lower())) if w]


def declared_words(body: Optional[str]) -> List[str]:
    """Every word on every line-anchored ``requires:`` line, lower-cased."""
    out: List[str] = []
    for m in REQUIRES_RE.finditer(body or ""):
        raw = m.group(1)
        words = _line_words(_REQUIRES_NOTE_RE.split(raw, maxsplit=1)[0])
        # The note cut only trims AFTER a declaration. When it would leave
        # nothing (``requires: (browser-session)``), keep the raw words so
        # they fail closed as ``unknown:`` instead of vanishing (Prism 303c33bfe408).
        for w in words or _line_words(raw):
            if w not in out:
                out.append(w)
    return out


def skill_key(name: Any) -> str:
    """The index key for a skill reference in any spelling the loader accepts: ``name``,
    ``category/name`` and ``category:name`` (``skills_tool._resolve_plugin_skill`` falls a
    ``category:name`` through to the local ``category/name``) all key on ``name``. The ONE
    normalizer for derivation, the resource-lane lookup and the dispatch/boot/skill gates
    (t_ecfd5591 review 2972d9d25619 / 64301a687435)."""
    # A trailing separator the loader accepts ("personal/x/") keys on "x", not "" (f779b3d305d8).
    return re.split(r"[/:]", str(name or "").strip().rstrip("/"))[-1].strip().lower()


def _named_skill_re(name: str):
    n = re.escape(name)
    # backticked, "skill X", "load X", and "the X skill" (Apollo r2 repro:
    # "use the Browser-Access skill"). All SOFT, all case-insensitive.
    return re.compile(rf"`{n}`|\bskill[ \t:]+`?{n}(?![\w-])|\bload[ \t]+`?{n}(?![\w-])"
                      rf"|(?<![\w-]){n}`?[ \t]+skill\b", re.IGNORECASE)


def ssh_root_hits(body: Optional[str], cmap: CapabilityMap) -> List[Tuple[str, str]]:
    """``[(alias, capability)]`` for every ``ssh <alias> ... sudo`` body line whose alias the
    map's ``ssh_root`` lists, with the access-bound deriver's own parse (t_79c2259b)."""
    if not cmap.ssh_root:
        return []
    from hermes_cli import kanban_worker_pool as _kwp
    found: List[str] = []
    while True:
        hit = _kwp.access_bound_cap(body, cmap.ssh_root.keys(), served=found)
        prefix = _kwp._reasons.ACCESS_SSH_ROOT + ":"
        if not hit or not hit.startswith(prefix):
            break
        found.append(hit[len(prefix):])
    return [(a, cmap.ssh_root[a]) for a in found]


def served_ssh_root(ctx: Optional[CapabilityTick]) -> frozenset:
    """Aliases whose ssh-root access a pool capability carries this tick, so :func:`access_bound_cap`
    stops treating it as Studio-only: the capability is ENFORCED (a real hold, placed only on the hosts
    that advertise it), or every pool host advertises it (nowhere to land without it). Layer off, a
    malformed map, or a warn-mode capability some pool host lacks: empty (today's Studio-only rule)."""
    if ctx is None or not ctx.cmap.valid or ctx.mode == OFF:
        return frozenset()
    return frozenset(a for a, cap in ctx.cmap.ssh_root.items()
                     if ctx.mode_of(cap) == ENFORCE or _satisfied(cap, ctx, None))


def derive(body: Optional[str], skills: Iterable[str], cmap: CapabilityMap
           ) -> Tuple[List[Requirement], bool]:
    """``(requirements, none_declared)`` for one card, every source.

    A capability word the map does not define (declared or from the index)
    becomes ``unknown:<word>`` (F4): never dropped, never satisfiable.
    """
    known = set(cmap.capabilities)
    if not cmap.valid:
        # A malformed map still names what the index asks for, so the warn
        # tick records the right capability, not ``unknown:<cap>`` (F11).
        known = {c for caps in cmap.index.values() for c in caps}

    def cap_of(word: str) -> str:
        # An alias reads as its target (t_f935cb6f): `requires: browser-session` is browser-identity.
        return cmap.canonical(word) if word in known else UNKNOWN_PREFIX + word

    reqs: List[Requirement] = []
    words = declared_words(body)
    none = NONE_WORD in words
    for w in words:
        # ``requires: control-plane-read`` is the access-bound declaration
        # (spec §2.6), owned by portable(), not a capability of the map.
        if w != NONE_WORD and w != _ACCESS_CONTROL_PLANE_READ:
            reqs.append(Requirement(cap_of(w), HARD, DECLARED, w))
    attached = [s for s in dict.fromkeys(skills or ()) if isinstance(s, str)]
    attached_keys = {skill_key(s) for s in attached}
    if cmap.index_error is None:
        for s in attached:
            for c in cmap.index.get(skill_key(s), ()):
                reqs.append(Requirement(cap_of(c), HARD, SKILL, s))
        # Skill names are case-insensitive everywhere in the fleet: the cheap
        # prefilter must not drop "use the Browser-Access skill" (Prism eac2f2e68fe7).
        text = body or ""
        folded = text.lower()
        for name, caps in cmap.index.items():
            if name in attached_keys or not caps or name not in folded:
                continue
            if _named_skill_re(name).search(text):
                for c in caps:
                    reqs.append(Requirement(cap_of(c), SOFT, NAMED_SKILL, name))
    for alias, cap in ssh_root_hits(body, cmap):
        # ``ssh <alias> ... sudo`` on an alias a capability serves (t_79c2259b): HARD, like a
        # declared word, because the work cannot run without that access.
        reqs.append(Requirement(cap_of(cap), HARD, ACCESS, f"ssh-root:{alias}"))
    seen = set()
    for cap, rx in cmap.sniff:
        if cap in seen:
            continue
        m = rx.search(body or "")
        if m:
            seen.add(cap)
            reqs.append(Requirement(cap, SOFT, BODY, m.group(0).strip()))
    return reqs, none


def browser_remote_refusal(record: Any) -> Optional[str]:
    """P3 contract: HTTPS data plane AND SSH control plane, strict booleans.

    The reachability producer owns probing; placement never opens network
    connections per card. An absent observation is not an admission grant.
    """
    record = record if isinstance(record, Mapping) else {}
    for leg in ("front_door", "ssh"):
        if record.get(leg) is not True:
            return "BROWSER_REMOTE_UNREACHABLE:" + leg
    return None


def _host_caps(ctx: CapabilityTick, host: str) -> frozenset:
    caps = ctx.cmap.hosts.get(host, frozenset())
    record = ctx.cmap.browser_remote.get(host)
    if ctx.cmap.browser_remote:
        # Once remote probing is enabled, an unobserved host is not a grant.
        # Failed legs also veto stale/static browser advertisements.
        if browser_remote_refusal(record) is None:
            return caps | BROWSER_IDENTITY_CAPS
        return caps - BROWSER_IDENTITY_CAPS
    return caps


def _satisfied(cap: str, ctx: CapabilityTick, pin_host: Optional[str]) -> bool:
    """A pinned card asks only its pin host. An unpinned one asks every known
    pool host: ``take()`` is not capability-aware until Phase 4, so a
    capability counts only when no host the card could land on lacks it. A
    host with no row in the map has none (s1.3, the safe direction)."""
    if cap.startswith(UNKNOWN_PREFIX):
        return False
    hosts = (pin_host.lower(),) if pin_host else ctx.pool_hosts
    if not hosts:
        return False
    return all(cap in _host_caps(ctx, h) for h in hosts)


def _hard_class(ctx: CapabilityTick, cap: str) -> bool:
    """A capability prose may still pin under ``prose_pins_advisory`` (t_c82eded0): the map marks it
    ``soft: false`` (docker, fleet-ssh, studio-state) or the Studio serves it identity-only."""
    return ctx.cmap.soft.get(cap) is False or cap in ctx.cmap.identity_only


def _first(reqs: List[Requirement]) -> Optional[Requirement]:
    return min(reqs, key=lambda r: (_SOURCE_RANK[r.matched_by], r.cap)) if reqs else None


def evaluate(body: Optional[str], skills: Iterable[str], ctx: Optional[CapabilityTick], *,
             pin_host: Optional[str] = None, any_pin: bool = False) -> Verdict:
    """The card's verdict under this tick's map and modes. ``pin_host`` is a
    resolved ``host:<pool-id>`` pin (never ``any``/``studio``)."""
    if ctx is None:
        return Verdict()
    reqs, none = derive(body, skills, ctx.cmap)
    if none or any(r.matched_by == DECLARED for r in reqs):
        # F12: ``none`` overrides soft only. Any explicit ``requires:`` line
        # does too (t_6d9e7d90): the author named what the card needs, so a
        # sniffed prose word ("docker compose", "SSH keys") no longer adds one.
        reqs = [r for r in reqs if r.strength == HARD]
    # An identity-only capability is never "satisfied by every pool host" for
    # an unpinned card: the Studio must not take it as portable overflow even
    # when the whole pool advertises it (spec v0.3 §3.4).
    live = [r for r in reqs if ctx.mode_of(r.cap) != OFF and (
        not _satisfied(r.cap, ctx, pin_host)
        or (pin_host is None and (r.cap in ctx.cmap.identity_only
                                 or (r.cap in BROWSER_IDENTITY_CAPS and ctx.cmap.browser_remote))))]
    enforced = [r for r in live if ctx.mode_of(r.cap) == ENFORCE]
    ignored: Tuple[Requirement, ...] = ()
    if ctx.body_mode == ADVISORY and pin_host is None:
        # A body-sniffed word whose capability no host may serve (identity-only,
        # no pool host advertises it) would hold the card at host=none forever
        # (t_640a4ea0: "docker run" held 3 cards 9.5 h). It is advice, not a pin.
        ignored = tuple(r for r in enforced if r.matched_by == BODY and r.cap in ctx.cmap.identity_only
                        and not any(r.cap in _host_caps(ctx, h) for h in ctx.pool_hosts))
        enforced = [r for r in enforced if r not in ignored]
    if ctx.prose_advisory and pin_host is None:
        # t_c82eded0: a capability inferred from PROSE (body sniff, a skill named in the body) that no pool
        # host serves would hold the card on the Studio. It is advice: recorded, the card starts on a pool
        # host. One a pool host does serve still narrows the take to those hosts (``only`` below).
        # A HARD class (the map's ``soft: false``: docker, fleet-ssh, studio-state) or an identity-only
        # capability is never dropped here: those keep their hold or wait (Apollo review of #255, F1).
        prose = tuple(r for r in enforced if r.matched_by in PROSE_SOURCES
                      and not _hard_class(ctx, r.cap)
                      and not any(r.cap in _host_caps(ctx, h) for h in ctx.pool_hosts))
        ignored += prose
        enforced = [r for r in enforced if r not in prose]
    warned = [r for r in live if ctx.mode_of(r.cap) == WARN]
    hard = _first([r for r in enforced if r.strength == HARD])
    soft = _first([r for r in enforced if r.strength == SOFT])
    # `host: any` overrides SOFT requirements (F12): they must not come back
    # as a host restriction or an identity-only hold (Prism dab1f537f2a0 /
    # a5b769beaf55 on #1902). HARD ones still apply.
    binding = [r for r in enforced if r.strength == HARD] if any_pin else enforced
    if binding and pin_host is None and not any(r.unknown for r in binding):
        needed = {r.cap for r in binding}
        servers = tuple(h for h in ctx.pool_hosts if needed <= _host_caps(ctx, h))
        if servers:
            # Some pool host advertises everything the card needs: portable,
            # placed on those hosts only (t_90887924, AC-5 "host X").
            return Verdict(pin=hard or soft, enforced=tuple(enforced),
                           observed=_first(warned), only=servers, body_ignored=ignored)
        idle = sorted(needed & ctx.cmap.identity_only)
        if idle:
            return Verdict(hard_rule=hard.rule if hard else None,
                           soft_rule=soft.rule if soft else None, pin=hard or soft,
                           enforced=tuple(enforced), observed=_first(warned),
                           wait=f"{UNSATISFIED}:{idle[0]}", body_ignored=ignored)
    return Verdict(
        hard_rule=hard.rule if hard else None,
        soft_rule=soft.rule if soft else None,
        pin=hard or soft,
        enforced=tuple(enforced),
        observed=_first(warned),
        body_ignored=ignored,
        wait=(browser_remote_refusal(ctx.cmap.browser_remote.get(pin_host.lower()))
              if pin_host and ctx.cmap.browser_remote
              and any(r.cap in BROWSER_IDENTITY_CAPS for r in binding) else None),
    )


# -- presentation -----------------------------------------------------------

def affinity_payload(req: Requirement, verdict: Verdict, mode: str, *,
                     host: str = STUDIO_HOST) -> Dict[str, Any]:
    """The ``placement_affinity`` event payload (s2.3). ``host`` is where the
    card may run: the Studio, ``"|"``-joined advertising pool hosts, or
    ``"none"`` (an identity-only capability nobody advertises: it waits)."""
    return {
        "host": host, "rule": req.rule,
        "requires": sorted({r.cap for r in verdict.enforced}) if mode == ENFORCE else [req.cap],
        "matched_by": req.matched_by, "term": req.term, "strength": req.strength, "mode": mode,
    }


def _source_text(req: Requirement) -> str:
    if req.matched_by == SKILL:
        return f"skill {req.term}"
    if req.matched_by == NAMED_SKILL:
        return f"skill {req.term} named in the body"
    if req.matched_by == DECLARED:
        return f"requires: {req.term}"
    if req.matched_by == ACCESS:
        return f"body {req.term} (ssh ... sudo)"
    return f'body "{req.term}"'


def pin_comment(req: Requirement, known: Iterable[str], *, host: str = STUDIO_HOST) -> str:
    """The one card comment per (card, capability, matched_by) pin."""
    head = f"{COMMENT_PREFIX} {req.cap} ({_source_text(req)})"
    if host == "none":
        return (f"{head}. No pool host advertises {req.cap} (its probe never passed as the pool "
                f"user) and the Mac Studio serves it only to an explicit `host: studio` pin, so "
                f"this card WAITS ({UNSATISFIED}). Add `host: studio` to run it on the Studio.")
    if host != STUDIO_HOST:
        return f"{head}. Placed only on a pool host that advertises it: {host.replace('|', ', ')}."
    if req.unknown:
        word = req.cap[len(UNKNOWN_PREFIX):]
        return (f"{COMMENT_PREFIX} unknown capability \"{word}\" ({_source_text(req)}). "
                "The card stays on the Mac Studio until the word is fixed. Known capabilities: "
                + ", ".join(sorted(known)) + ".")
    if req.strength == SOFT:
        undo = "To let it spill, add a line `host: any` or `requires: none`."
    elif req.matched_by == DECLARED:
        undo = "To let it spill, remove the `requires:` line."
    elif req.matched_by == ACCESS:
        undo = f"To let it spill, advertise {req.cap} on a pool host or remove the ssh ... sudo line."
    else:
        undo = f"To let it spill, remove the skill {req.term} from the card."
    return (f"{head}. This card needs a capability no pool host has, so it runs on the "
            f"Mac Studio and never spills. {undo}")


def pin_lacks_comment(host: str, req: Requirement) -> str:
    """A ``host:<pool-id>`` pin whose host lacks a HARD requirement (F3)."""
    return (f"pin host:{host} lacks {req.cap}; remove the pin or the requirement "
            f"({_source_text(req)}). Parked needs_input instead of waiting silently.")


# -- the dispatcher's side effects (never on a dry run; never break a tick) ---

def spawn_env(fleet_dir: Path, kanban_cfg: Any) -> str:
    """``KANBAN_PLACEMENT_CAPS`` for a worker spawned now (F5): ``off`` when
    the layer is off or the map absent, all-``warn`` when it is malformed."""
    mode, enforce = read_modes(kanban_cfg)
    if mode == OFF:
        return env_value(OFF, ())
    cmap = load_map(fleet_dir)
    if cmap is None:
        return env_value(OFF, ())
    if not cmap.valid:
        return env_value(WARN, ())
    mode = effective_mode(mode, cmap)
    if mode == ENFORCE and cmap.soft:
        # The map's per-class hardness wins, exactly as CapabilityTick.mode_of
        # decides placement (Prism b8ddb038892b on #1902), judged per alias group (Prism 5f58fcfad85b).
        enforce = tuple(c for c in dict.fromkeys([*enforce, *(k for k, v in cmap.soft.items() if not v)])
                        if not group_soft(cmap, c))
    if mode == ENFORCE and cmap.aliases:
        # The worker's gates ask by either word (t_f935cb6f): every name of an enforced capability is enforced.
        enforce = tuple(dict.fromkeys(n for c in enforce for n in (c, *sorted(cmap.names(c) - {c}))))
    return env_value(mode, enforce)


_MAP_INVALID_LOGGED: Dict[str, float] = {}
MAP_INVALID_LOG_SECONDS = 3600


def _note_map_invalid(error: str, now: float) -> bool:
    last = _MAP_INVALID_LOGGED.get(error)
    if last is not None and now - last < MAP_INVALID_LOG_SECONDS:
        return False
    _MAP_INVALID_LOGGED[error] = now
    return True


def _has_pin_event(conn, tid: str, payload: Mapping[str, Any]) -> bool:
    """The persisted dedup key of a pin comment (F12): a ``placement_affinity``
    event with the same card, rule and matched_by already exists."""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'placement_affinity' "
        "AND json_extract(payload, '$.rule') = ? AND json_extract(payload, '$.matched_by') = ? "
        "AND json_extract(payload, '$.mode') = 'enforce' LIMIT 1",
        (tid, payload.get("rule"), payload.get("matched_by"))).fetchone() is not None


def pending_comments(conn, result, caps: Optional[CapabilityTick], caps_out: Mapping) -> List[Tuple[str, str]]:
    """``[(task_id, body)]`` to post this tick, decided BEFORE the affinity
    events are written (the events are the dedup key)."""
    if caps is None:
        return []
    out: List[Tuple[str, str]] = []
    for tid, aff in (result.placement_affinity or {}).items():
        if not str(aff.get("rule", "")).startswith(RULE_PREFIX) or _has_pin_event(conn, tid, aff):
            continue
        req = Requirement(aff["rule"][len(RULE_PREFIX):], aff["strength"], aff["matched_by"], aff["term"])
        out.append((tid, pin_comment(req, caps.cmap.capabilities, host=aff.get("host", STUDIO_HOST))))
    for tid, req in (caps_out.get("unknown") or {}).items():
        aff = (result.placement_affinity or {}).get(tid)
        if aff is not None and aff.get("rule") == req.rule:
            continue   # the pin comment above already says it
        out.append((tid, pin_comment(req, caps.cmap.capabilities)))
    return out


def _reconcile_warn_events(conn, task_ids: Iterable[str], observed: Mapping[str, Any],
                           kind: str = WARN_EVENT) -> None:
    """Inside the caller's txn: write each card's warn observation on change,
    and ``{"cleared": true}`` once for a card whose last one no longer holds."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_host_affinity import _last_payloads
    ids = list(dict.fromkeys(task_ids))
    last_by = _last_payloads(conn, ids, kind)
    for tid in ids:
        last = last_by.get(tid)
        want = observed.get(tid)
        if want is not None and last != want:
            _kb._append_event(conn, tid, kind, dict(want))
        elif want is None and last is not None and not last.get("cleared"):
            _kb._append_event(conn, tid, kind, {"cleared": True})


def _clear_warn_events(conn, evaluated_ids: Iterable[str]) -> None:
    """Layer off or map absent: no observation holds (Prism 08dfcc4dc097).
    Reads first, so a board that never had one pays one SELECT, no write."""
    import logging
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli.kanban_host_affinity import _last_payloads
    ids = list(dict.fromkeys(evaluated_ids))
    if not ids or not any(not p.get("cleared") for p in _last_payloads(conn, ids, WARN_EVENT).values()):
        return
    try:
        with _kbc.write_txn(conn):
            _reconcile_warn_events(conn, ids, {})
    except Exception:
        logging.getLogger(__name__).warning("kanban dispatch: could not clear capability warn events",
                                            exc_info=True)


def record_capability_events(conn, result, caps: Optional[CapabilityTick], caps_out: Mapping,
                             evaluated_ids: Iterable[str], comments: List[Tuple[str, str]]) -> None:
    """Post the pin comments, park ``host:<pool-id>`` cards that lack a hard
    requirement (F3), write warn-mode observations and the map-invalid
    event. Runs after :func:`kanban_host_affinity.record_affinity_events`."""
    import logging
    import time as _time
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    log = logging.getLogger(__name__)
    if caps is None:
        _clear_warn_events(conn, evaluated_ids)
        return
    try:
        with _kbc.write_txn(conn):
            for tid, body in comments:
                if conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND body = ? LIMIT 1",
                                (tid, body)).fetchone() is None:
                    _kb.add_comment(conn, tid, "dispatcher", body)
            observed = caps_out.get("observed") or {}
            _reconcile_warn_events(conn, [*evaluated_ids, *observed], observed)
            ignored = {tid: body_ignored_payload(req)
                       for tid, req in (caps_out.get("body_ignored") or {}).items()}
            _reconcile_warn_events(conn, [*evaluated_ids, *ignored], ignored, BODY_IGNORED_EVENT)
            error = caps.error
            if error:
                # On the cards the bad file demoted (a warn-mode requirement,
                # or attached skills the index could not read), once per error.
                for tid in dict.fromkeys([*observed, *(caps_out.get("touched") or ())]):
                    last = conn.execute(
                        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
                        "ORDER BY id DESC LIMIT 1", (tid, MAP_INVALID_EVENT)).fetchone()
                    if last is None or json.loads(last[0] or "{}").get("error") != error:
                        _kb._append_event(conn, tid, MAP_INVALID_EVENT,
                                          {"error": error, "mode": WARN if not caps.cmap.valid else caps.mode})
    except Exception:
        log.warning("kanban dispatch: could not record capability events", exc_info=True)
    if caps.error and _note_map_invalid(caps.error, _time.monotonic()):
        log.warning("kanban placement: %s; every capability is warn this tick, the pool is NOT "
                    "refused (fix the file or set kanban.placement.capabilities: off)", caps.error)
    for tid, (host, req) in (caps_out.get("pin_lacks") or {}).items():
        try:
            reason = pin_lacks_comment(host, req)
            # Fenced (Prism 55912e00bffe): the decision was made on the
            # pre-loop snapshot; a card claimed since is left alone.
            if _kb.block_task(conn, tid, reason=reason, kind="needs_input",
                              require_unclaimed_ready=True):
                _kb.add_comment(conn, tid, "dispatcher", reason)
                (result.placement_waits or {}).pop(tid, None)
                result.placement_caps.setdefault(tid, {})["action"] = "needs_input"
            else:
                log.info("kanban dispatch: %s no longer ready+unclaimed; pin-lacks park skipped", tid)
        except Exception:
            log.warning("kanban dispatch: could not park %s needs_input (pin lacks %s)",
                        tid, req.cap, exc_info=True)


# -- prose-only Studio signals: advisory, with the hand-back named (t_c82eded0) --

PROSE_ADVISORY_PREFIX = "placement advisory:"


def prose_advisory_comment(advisories: Iterable[Mapping[str, Any]]) -> str:
    """The ONE card comment for prose-only Studio signals: what matched, and what a worker on a pool host does
    when the work really needs the Studio."""
    seen = []
    for a in advisories:
        what = a.get("need") or a.get("capability") or "?"
        src = "skill named in the body" if a.get("source") == NAMED_SKILL else "body"
        item = f'{what} ({src}: "{str(a.get("evidence") or "")[:80]}")'
        if item not in seen:
            seen.append(item)
    return (f"{PROSE_ADVISORY_PREFIX} the card text suggests the Mac Studio ({'; '.join(seen)}), but prose no "
            f"longer pins a card, so it may start on a pool host. If the work truly needs something only the "
            f"Studio has, stop at once with kanban_block(kind=\"capability\", reason=\"needs <what> only the Studio "
            f"has; re-run on the Studio\"): the dispatcher re-routes it there once with host: studio and a "
            f"requires: line. Authors: a line `requires: <capability>` pins on purpose.")


def record_prose_advisories(conn, advisories: Mapping[str, Any]) -> List[str]:
    """Post :func:`prose_advisory_comment` once per card (any earlier advisory comment is the latch).
    Best-effort: never breaks a tick. Returns the card ids commented this call."""
    import logging
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    posted: List[str] = []
    for tid, adv in (advisories or {}).items():
        if not adv:
            continue
        try:
            exists = ("SELECT 1 FROM task_comments WHERE task_id = ? AND substr(body, 1, ?) = ? LIMIT 1",
                      (tid, len(PROSE_ADVISORY_PREFIX), PROSE_ADVISORY_PREFIX))
            if conn.execute(*exists).fetchone():
                continue
            with _kbc.write_txn(conn):
                if conn.execute(*exists).fetchone():
                    continue
                _kb.add_comment(conn, tid, "dispatcher", prose_advisory_comment(adv))
            posted.append(tid)
        except Exception:
            logging.getLogger(__name__).warning("kanban dispatch: could not post the prose advisory on %s", tid,
                                                exc_info=True)
    return posted


# -- body-sniffed pins: advisory, and a held enforce pin is never silent (t_640a4ea0) --

def body_ignored_payload(req: Requirement) -> Dict[str, Any]:
    """The ``placement_body_ignored`` event: the card was placed without it."""
    return {"reason": f"{BODY_IGNORED}:{req.cap}", "cap": req.cap, "term": req.term,
            "matched_by": req.matched_by}


def matched_line(body: Optional[str], affinity: Mapping[str, Any]) -> Optional[str]:
    """The body line a pin came from: the ``requires:`` line naming the word,
    or the first line holding the sniffed term. None for a skill pin."""
    term = str(affinity.get("term") or "")
    by = affinity.get("matched_by")
    if not term or by == SKILL:
        return None
    for line in (body or "").splitlines():
        if by == DECLARED:
            if REQUIRES_RE.match(line) and term.lower() in _line_words(line.split(":", 1)[1]):
                return line.strip()
        elif term.lower() in line.lower():
            return line.strip()
    return None


def held_comment(affinity: Mapping[str, Any], line: Optional[str]) -> str:
    cap = str(affinity.get("rule", ""))[len(RULE_PREFIX):]
    req = Requirement(cap, affinity.get("strength") or HARD, affinity.get("matched_by") or BODY,
                      affinity.get("term") or "")
    where = f" (line: `{line}`)" if line else ""
    if req.matched_by == DECLARED:
        undo = "or replace that line with `requires: none`"
    elif req.matched_by == SKILL:
        undo = f"or remove the skill {req.term} from the card"
    elif req.matched_by == ACCESS:
        undo = "or remove the ssh ... sudo line"
    else:
        undo = "or add a line `requires: none`"
    return (f"{HELD_PREFIX} {cap} from {_source_text(req)}{where}. No host that may run this card "
            f"has {cap} (host=none), so it waits and will not clear on its own. Override: add a line "
            f"`host: studio` (the Mac Studio serves {cap} to an explicit pin) {undo}.")


def _open_none_wait(conn, tid: str) -> bool:
    """Whether the card's latest ``placement_wait`` (written by an EARLIER tick)
    is an open host=none hold."""
    from hermes_cli import kanban_placement_reasons as _pr
    row = conn.execute(f"SELECT id, payload FROM task_events WHERE task_id = ? AND kind IN ({_pr.kind_marks('placement_wait')}) "
                       "ORDER BY id DESC LIMIT 1", (tid, *_pr.kind_names("placement_wait"))).fetchone()
    if row is None:
        return False
    try:
        last = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        return False
    if last.get("host") != "none" or not str(last.get("reason", "")).startswith(UNSATISFIED):
        return False
    kinds = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    return conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN (%s) LIMIT 1"
                        % ",".join("?" * len(kinds)), (tid, row[0], *kinds)).fetchone() is None


def held_pending(conn, result) -> List[Tuple[str, Dict[str, Any]]]:
    """``[(task_id, affinity)]`` held at host=none this tick AND on the previous
    one, with no notice yet. Read BEFORE this tick's wait events are written."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    waits = result.placement_waits or {}
    for tid, aff in (result.placement_affinity or {}).items():
        if aff.get("host") != "none" or aff.get("mode") != ENFORCE \
                or not str(waits.get(tid, "")).startswith(UNSATISFIED):
            continue
        if conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
                        (tid, HELD_EVENT)).fetchone() is not None:
            continue   # once per card
        if _open_none_wait(conn, tid):
            out.append((tid, dict(aff)))
    return out


def notify_held(conn, board: Optional[str], pending: List[Tuple[str, Dict[str, Any]]]) -> List[str]:
    """Post the one comment per held card (the event is its latch) and one
    #logs line, never #alerts. Best-effort: never breaks a tick."""
    import logging
    from hermes_cli import kanban_budget as _kbudget
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    log = logging.getLogger(__name__)
    posted: List[str] = []
    for tid, aff in pending:
        try:
            row = conn.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()
            line = matched_line(row[0] if row else None, aff)
            text = held_comment(aff, line)
            with _kbc.write_txn(conn):
                _kb._append_event(conn, tid, HELD_EVENT, {"rule": aff.get("rule"), "term": aff.get("term"),
                                                         "matched_by": aff.get("matched_by"), "line": line})
                _kb.add_comment(conn, tid, "dispatcher", text)
            posted.append(tid)
        except Exception:
            log.warning("kanban dispatch: could not post the requires-held notice on %s", tid, exc_info=True)
            continue
        script = _kbudget._notify_script_path()
        if script is not None:
            import sys
            _kbudget._run_notify([sys.executable, script, "--channel", "discord", "--target",
                                  _kbudget.RECOVERY_TARGET, "--send",
                                  f"📌 Kanban '{board or 'default'}': {tid} {text}"])
    return posted
