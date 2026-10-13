"""placement -- ONE placement scorer: balance by measured HEADROOM and reservations, never by rank.

VENDORED from hermes-home ``scripts/lib/placement.py`` @ aa1241fc4b (ANG-Ventures/hermes-home#3945 merge commit, t_499e107f;
sha256 547d606f539c357ff9e39c770db6f16b3ff589104e3d48b4f61641d7ee8b7e75) for the kanban dispatcher (t_d6ce856e):
the fork may not import hermes-home code. The ONLY local changes are ``fleet_root()`` plus ``encoding="utf-8"``
on two ``open()`` calls (the fork's PLW1514 lint); ``tests/hermes_cli/test_kanban_headroom_placement.py``
checks the rest against the canonical file.

Card t_499e107f (Ace 2026-10-12 16:34: "ace ai doesn't need to be last pick, we just need load balancing
instead"). PR #3925 made ace-ai the last pick for remote test-gate runs and the allocation handoff
(plans/2026-10-12_fleet-work-allocation-handoff.md, mechanism 4) proposed ci-box > ace-media > ace-ai.
Both were rank pins. This module replaces them for every consumer that places work (test-gate local +
remote, kanban dispatch, Prism router admission, babysit launch):

    import placement
    table = placement.load_hosts()                         # config/fleet-hosts.json, rank-free by construction
    samples = placement.samples_from_pressure(doc, now=time.time())   # var/fleet-pressure.json rows
    d = placement.choose(table, "test-gate", samples)      # Decision
    d.host, d.reason, d.lines()                            # "ci-box", "" / "no_headroom", human rows

THE RULE (Ace 2026-10-12 17:08, via Apollo 17:10; supersedes the card body's "no ordinal preference
anywhere"): "load balancing AND priority are both important. Generally saturate the CI box first, but load
balance so we have our pick of which box to spawn on based on affinity, continuity of work, priority, or
wanting a faster core."

  1. HARD GATE = headroom: measured available minus the host's reservation minus the job's footprint, on
     memory and on cores. A host that fails it is never picked. Stale / absent / unmeasured sampler = closed.
  2. DEFAULT PREFERENCE among the eligible: ci-box FIRST, saturated up to its comfortable ceiling
     (`saturate_to` x physical cores of load; the big box is the cheap one), then the most headroom. The
     expensive / latency-sensitive boxes stay free by default, not by rank: nothing is "last pick".
  3. PER-JOB OVERRIDES that beat the default (Job): `affinity` (the work lives there: files, a running
     service, a GPU) and `continuity` (the previous run of this card ran there, its caches are warm) pin to
     that host when it is eligible; `priority` 0/1 claims the fastest eligible core class even if ci-box has
     room; `core_class` "fast" -> the highest single-core clock among eligible, "wide" -> the most threads.
  ACE-AI is gated by its RESERVATION for the voice/ML stack and picked when headroom and the job's needs say so.
  Ace 18:1x (operative wording): "load balance but give priority to all the under-utilized workhorse resources on
  the ci box, unless something needs the faster cores or continuity/affinity of the other boxes in the fleet."
  The Mac Studio is NOT excluded: it is a host with `default_preference: low` (the coordination device) and a
  small operator-raisable slot count; the headroom layer takes it only when no normal-preference host has
  headroom, while affinity / continuity / priority / core_class select it like any other host.

Per host the inputs are the sampler's MemAvailable, load (load1, and load5 when the caller has it: the
LARGER of the two counts, so a box clears only when its 5-minute queue has), PSI cpu/mem some10, %system,
PLUS the reservation table: memory and cores held back for the host's residents (ace-ai: the voice hub +
ML services; ci-box: the runner pool burst; ace-media: the media stack). Headroom on each axis =
available - reserve - the job's declared footprint; the score is the BINDING axis as a fraction of the
host's capacity, so a 16-core and a 128-core box compare on the same scale. choose() applies the three
layers above with hysteresis (a previous pick is kept unless another host beats it by `margin`), refuses when
no host has headroom >= the footprint (the caller queues; it never spills), and treats an absent / stale /
unmeasured sample as CLOSED, never "ok". A host may be EXCLUDED (slots 0 for the consumer, or a row in the
exclusions file). The ONLY ordinal in the table is `default_first` (one host, the one to saturate); a table
carrying a ladder / prio / order / rank / last key is refused.

Reservation at pick time (A3, t_08de4ba5 folded in here): twenty picks in one tick must see twenty different
headrooms, so a pick is followed by ``reserve(...)`` which records the footprint against the host for the job's
expected duration (``ttl_s``) in var/placement/headroom-reservations.json; ``live_reservations(now)`` is
subtracted from every host's available memory and cores by ``choose(..., reservations=...)``, and every pick
is appended to var/placement/admissions.jsonl (the per-host "what I admitted" ledger the 07:10 incident lacked).

Notation (Ace 2026-10-12 08:21): every human-facing line prints the load as `N / Cc.Tt` (tasks over
physical cores and threads) and the headroom in GB.

Stdlib only; python3.9+.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

SCHEMA = 1
TABLE_REL = Path("config") / "fleet-hosts.json"
EXCLUSIONS_REL = Path("var") / "placement" / "exclusions.json"
RESERVATIONS_REL = Path("var") / "placement" / "headroom-reservations.json"
ADMISSIONS_REL = Path("var") / "placement" / "admissions.jsonl"
RANK_KEYS = frozenset({"prio", "ladder", "order", "rank", "last", "last_pick", "preference", "preferred"})
DEFAULT_SATURATE_TO = 1.0   # the default-first host is "comfortable" while load <= saturate_to x physical cores
DEFAULT_STALE_AFTER_S = 60.0
DEFAULT_MARGIN = 0.10
# A host whose sample trips any of these is CLOSED for the tick (the signals that predicted 10-12 07:20:
# %system 6 -> 33 with CPU 45 % idle; psi mem is the collapse itself).
DEFAULT_CLOSED_WHEN = {"psi_mem_some10": 10.0, "psi_cpu_some10": 40.0, "system_pct": 25.0}
# Axes a sample MUST carry to be eligible (an unmeasured safety axis is CLOSED, never "ok"). The sampler row
# (fleet-pressure.json) carries all of them; a direct probe that cannot read PSI passes a narrower set explicitly.
REQUIRED_AXES = ("mem_available_gb", "load1", "load5", "psi_cpu_some10", "psi_mem_some10", "system_pct")


class PlacementError(ValueError):
    """The host table is absent, unreadable, carries a rank, or fails validation."""


class LedgerError(PlacementError):
    """The reservation ledger exists but cannot be read or parsed: it is never overwritten (Prism P1 51e7cfc6)."""


def fleet_root() -> Path:
    env = os.environ.get("PLACEMENT_HOME")
    if env:
        return Path(env)
    # VENDORED: the shared kanban root (<root>/config/fleet-hosts.json, <root>/var/placement/), at call time.
    from hermes_cli import kanban_db
    return kanban_db.kanban_home()


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _now(now) -> float:
    """The decision clock: ``time.time()`` when None, else a FINITE epoch. A NaN/inf ``now`` makes every
    ``until > now`` / ``now - at > stale`` compare false, so a release() would prune the WHOLE ledger and a stale
    sample or a live exclusion would read as absent (Prism 83b0c929b421): refused before any read or write."""
    now = time.time() if now is None else now
    if not _num(now):
        raise PlacementError(f"now must be a finite epoch number, not {now!r}")
    return float(now)


def _stale_after(v) -> Optional[float]:
    """``stale_after_s``: None (caller vouches) or a finite number >= 0; NaN would never close a stale sample."""
    if v is not None and (not _num(v) or v < 0):
        raise PlacementError(f"stale_after_s must be a finite number >= 0 or None, not {v!r}")
    return v


def _walk_rank_keys(node, path="") -> List[str]:
    hits = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and not k.startswith("_") and k.lower() in RANK_KEYS:
                hits.append(f"{path}.{k}" if path else k)
            hits.extend(_walk_rank_keys(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits.extend(_walk_rank_keys(v, f"{path}[{i}]"))
    return hits


# ── the table ────────────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class Footprint:
    ram_gb: float
    threads: float

    def __post_init__(self):
        for k in ("ram_gb", "threads"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise PlacementError(f"footprint.{k} must be a finite number >= 0, not {v!r}")

    @classmethod
    def of(cls, d: Mapping) -> "Footprint":
        return cls(float(d.get("ram_gb", 0.0)), float(d.get("threads", 0.0)))


@dataclasses.dataclass(frozen=True)
class HostSpec:
    name: str
    cores: int
    threads: int
    mem_total_gb: float
    mem_reserve_gb: float
    cores_reserve: float
    slots: Mapping[str, int]
    aliases: tuple
    clock_mhz: float = 0.0           # max single-core clock (core_class "fast")
    default_preference: str = "normal"   # "low" = the headroom layer takes it only when no normal host has headroom
    unmeasurable_axes: tuple = ()        # axes this host's OS cannot report (darwin: no PSI, no %system); never required

    def slots_for(self, consumer: str) -> Optional[int]:
        s = self.slots.get(consumer)
        return None if s is None else int(s)


@dataclasses.dataclass(frozen=True)
class HostTable:
    hosts: Dict[str, HostSpec]
    footprints: Dict[str, Footprint]
    closed_when: Dict[str, float]
    path: Optional[Path]
    default_first: Optional[str] = None
    saturate_to: float = DEFAULT_SATURATE_TO

    def resolve(self, name: str) -> Optional[str]:
        """Canonical host id for ``name`` or one of its aliases (case-folded), else None."""
        if not isinstance(name, str):
            return None
        low = name.strip().lower()
        for hid, h in self.hosts.items():
            if hid.lower() == low or low in (a.lower() for a in h.aliases):
                return hid
        return None

    def footprint(self, consumer: str, override: Optional[Mapping] = None) -> Footprint:
        base = self.footprints.get(consumer)
        if base is None:
            raise PlacementError(f"no default footprint for consumer {consumer!r}")
        if not override:
            return base
        ram, thr = override.get("ram_gb", base.ram_gb), override.get("threads", base.threads)
        if not _num(ram) or not _num(thr):
            raise PlacementError(f"footprint override must be finite numbers, not {override!r}")
        return Footprint(float(ram), float(thr))


def validate(doc) -> List[str]:
    if not isinstance(doc, dict):
        return ["top level must be an object"]
    errs = []
    if doc.get("schema") != SCHEMA or isinstance(doc.get("schema"), bool):
        errs.append(f"schema must be {SCHEMA}, not {doc.get('schema')!r}")
    for hit in _walk_rank_keys(doc):
        errs.append(f"{hit}: a rank key; hosts are balanced by headroom, never ordered (t_499e107f)")
    fps = doc.get("footprints")
    if not isinstance(fps, dict) or not fps:
        errs.append("footprints must be a non-empty object")
    else:
        for c, fp in fps.items():
            if c.startswith("_"):
                continue
            if not isinstance(fp, dict) or not _num(fp.get("ram_gb")) or not _num(fp.get("threads")) \
                    or fp["ram_gb"] < 0 or fp["threads"] < 0:
                errs.append(f"footprints.{c} needs ram_gb >= 0 and threads >= 0")
    hosts = doc.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        errs.append("hosts must be a non-empty object")
        hosts = {}
    for hid, h in hosts.items():
        if not isinstance(h, dict):
            errs.append(f"hosts.{hid} must be an object")
            continue
        for k in ("cores", "threads"):
            if not isinstance(h.get(k), int) or isinstance(h.get(k), bool) or h[k] < 1:
                errs.append(f"hosts.{hid}.{k} must be an int >= 1")
        if isinstance(h.get("cores"), int) and isinstance(h.get("threads"), int) and h["threads"] < h["cores"]:
            errs.append(f"hosts.{hid}: threads {h['threads']} < cores {h['cores']}")
        for k in ("mem_total_gb", "mem_reserve_gb", "cores_reserve"):
            if not _num(h.get(k)) or h[k] < 0:
                errs.append(f"hosts.{hid}.{k} must be a number >= 0")
        if _num(h.get("mem_total_gb")) and _num(h.get("mem_reserve_gb")) and h["mem_reserve_gb"] >= h["mem_total_gb"]:
            errs.append(f"hosts.{hid}: mem_reserve_gb {h['mem_reserve_gb']} >= mem_total_gb {h['mem_total_gb']}")
        if _num(h.get("cores_reserve")) and isinstance(h.get("cores"), int) and h["cores_reserve"] >= h["cores"]:
            errs.append(f"hosts.{hid}: cores_reserve {h['cores_reserve']} >= cores {h['cores']}")
        cons = h.get("consumers")
        if not isinstance(cons, dict):
            errs.append(f"hosts.{hid}.consumers must be an object")
        else:
            for c, cv in cons.items():
                if not isinstance(cv, dict) or not isinstance(cv.get("slots"), int) or isinstance(cv.get("slots"), bool) \
                        or cv["slots"] < 0:
                    errs.append(f"hosts.{hid}.consumers.{c}.slots must be an int >= 0")
        al = h.get("aliases", [])
        if not isinstance(al, list) or not all(isinstance(a, str) and a for a in al):
            errs.append(f"hosts.{hid}.aliases must be a list of names")
    # every id and alias names exactly ONE host, case-folded: resolve() returns the first match, so a collision
    # would land samples and reservations on the wrong host (hermes-agent#1941 Prism 7ab54f13)
    owner: Dict[str, str] = {}
    for hid, h in hosts.items():
        al = h.get("aliases", []) if isinstance(h, dict) and isinstance(h.get("aliases", []), list) else []
        for name in {str(hid).strip().lower()} | {a.strip().lower() for a in al if isinstance(a, str) and a}:
            if name in owner and owner[name] != hid:
                errs.append(f"hosts.{hid}: name {name!r} also names hosts.{owner[name]} (ids and aliases must be unique)")
            owner.setdefault(name, hid)
    df = doc.get("default_first")
    if df is not None and (not isinstance(df, str) or df not in hosts):
        errs.append(f"default_first {df!r} must name one host in hosts")
    st = doc.get("saturate_to", DEFAULT_SATURATE_TO)
    if not _num(st) or st <= 0:
        errs.append("saturate_to must be a number > 0 (fraction of physical cores)")
    for hid, h in hosts.items():
        if isinstance(h, dict) and "clock_mhz" in h and (not _num(h["clock_mhz"]) or h["clock_mhz"] <= 0):
            errs.append(f"hosts.{hid}.clock_mhz must be a number > 0")
        if isinstance(h, dict) and not (isinstance(h.get("unmeasurable_axes", []), list)
                                        and all(a in REQUIRED_AXES and a not in ("mem_available_gb", "load1", "load5")
                                                for a in h.get("unmeasurable_axes", []))):
            errs.append(f"hosts.{hid}.unmeasurable_axes may list only psi_cpu_some10, psi_mem_some10, system_pct")
        if isinstance(h, dict) and h.get("default_preference", "normal") not in ("normal", "low"):
            errs.append(f"hosts.{hid}.default_preference must be normal|low (never an ordinal)")
    cw = doc.get("closed_when", {})
    if not isinstance(cw, dict) or any(not _num(v) or v < 0 for k, v in cw.items() if not k.startswith("_")):
        errs.append("closed_when must map axis -> number >= 0")
    elif isinstance(cw, dict):
        # an axis the Sample never carries would be a threshold nothing enforces (Prism 8479b28f): refuse it
        bad = [k for k in cw if not k.startswith("_") and k not in REQUIRED_AXES]
        if bad:
            errs.append(f"closed_when names unsupported axis(es) {bad}; supported: {list(REQUIRED_AXES)}")
    return errs


def load_hosts(path=None) -> HostTable:
    p = Path(path) if path is not None else fleet_root() / TABLE_REL
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlacementError(f"{p}: missing") from exc
    except (OSError, ValueError) as exc:
        raise PlacementError(f"{p}: unreadable: {type(exc).__name__}: {exc}") from exc
    errs = validate(doc)
    if errs:
        raise PlacementError(f"{p}: {len(errs)} error(s): " + "; ".join(errs))
    hosts = {}
    for hid, h in doc["hosts"].items():
        hosts[hid] = HostSpec(
            name=hid, cores=int(h["cores"]), threads=int(h["threads"]), mem_total_gb=float(h["mem_total_gb"]),
            mem_reserve_gb=float(h["mem_reserve_gb"]), cores_reserve=float(h["cores_reserve"]),
            slots={c: int(v["slots"]) for c, v in h["consumers"].items()},
            aliases=tuple(h.get("aliases", [])), clock_mhz=float(h.get("clock_mhz", 0.0)),
            default_preference=str(h.get("default_preference", "normal")),
            unmeasurable_axes=tuple(h.get("unmeasurable_axes", [])))
    fps = {c: Footprint.of(fp) for c, fp in doc["footprints"].items() if not c.startswith("_")}
    cw = dict(DEFAULT_CLOSED_WHEN)
    cw.update({k: float(v) for k, v in (doc.get("closed_when") or {}).items() if not k.startswith("_")})
    return HostTable(hosts=hosts, footprints=fps, closed_when=cw, path=p, default_first=doc.get("default_first"),
                     saturate_to=float(doc.get("saturate_to", DEFAULT_SATURATE_TO)))


class ExclusionsError(PlacementError):
    """The exclusions file exists but cannot be read or parsed: admission fails CLOSED (Prism P1 4d6e2458)."""


def read_exclusions(path=None, *, now: float) -> Dict[str, str]:
    """{host: reason} for every LIVE exclusion row (``until`` epoch in the future).

    Document shape, either form: ``{"ace-ai": {"until": 1791900000, "reason": "voice session"}}`` (the operator
    form the skills document) or ``{"hosts": {...same...}}``. A MISSING file = no exclusions; an unreadable or
    malformed one raises ExclusionsError so choose() refuses rather than silently admitting."""
    now = _now(now)
    p = Path(path) if path is not None else fleet_root() / EXCLUSIONS_REL
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ExclusionsError(f"{p}: unreadable: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ExclusionsError(f"{p}: unparseable: {exc}") from exc
    if not isinstance(doc, dict):
        raise ExclusionsError(f"{p}: not an object")
    if "hosts" in doc:
        rows = doc["hosts"]
        if not isinstance(rows, dict):
            raise ExclusionsError(f"{p}: \"hosts\" must be an object, not {type(rows).__name__}")
    else:
        rows = {k: v for k, v in doc.items() if not k.startswith("_")}
    out = {}
    # a malformed row fails CLOSED: skipping it would silently lift the operator's exclusion (Prism 5dd6339f /
    # hermes-agent#1941 a5504de2)
    for hid, row in rows.items():
        if not isinstance(row, dict):
            raise ExclusionsError(f"{p}: row {hid!r} must be an object, not {type(row).__name__}")
        until = row.get("until")
        if not _num(until):
            raise ExclusionsError(f"{p}: row {hid!r}: until must be a finite epoch number, not {until!r}")
        reason = row.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ExclusionsError(f"{p}: row {hid!r}: reason must be a string, not {reason!r}")
        if float(until) > now:
            out[hid] = reason or "excluded"
    return out


# ── samples ──────────────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Sample:
    """One host's measured state. ``load`` = the larger of load1/load5 the caller had (hysteresis on clear)."""
    mem_available_gb: Optional[float] = None
    load1: Optional[float] = None
    load5: Optional[float] = None
    psi_cpu_some10: Optional[float] = None
    psi_mem_some10: Optional[float] = None
    system_pct: Optional[float] = None
    at: Optional[float] = None
    status: str = "fresh"          # fresh | absent | stale (anything but fresh = closed)
    note: str = ""

    @property
    def load(self) -> Optional[float]:
        vals = [float(v) for v in (self.load1, self.load5) if _num(v)]
        return max(vals) if vals else None


def samples_from_pressure(doc: Mapping, *, now: float, stale_after_s: Optional[float] = None) -> Dict[str, Sample]:
    """Samples from a fleet-pressure.json document (placement-collector output). Every host row is a sample,
    the Studio included (Ace 2026-10-12 18:1x: low default preference, never excluded); whether a host is a
    candidate for a consumer is the table's slots, not the sampler's `targets` flag."""
    now = _now(now)
    stale = stale_after_s if stale_after_s is not None else doc.get("stale_after_s") or DEFAULT_STALE_AFTER_S
    if not _num(stale) or stale < 0:
        raise PlacementError(f"stale_after_s must be a finite number >= 0, not {stale!r}")
    stale = float(stale)
    # Clock normalization (Prism P1 6e15e2ae): row.at is the PRODUCER's clock; the collector computed age_s
    # against that same clock and stamped the document with its own. Age here = age_s + time elapsed since
    # the document was written, never (local now - a remote host's timestamp): a host clock two minutes
    # ahead would otherwise look fresh forever, one two minutes behind would look closed while healthy.
    doc_at = float(doc["at"]) if _num(doc.get("at")) else None
    elapsed = max(0.0, now - doc_at) if doc_at is not None else None
    out = {}
    for hid, row in (doc.get("hosts") or {}).items():
        if not isinstance(row, dict):
            continue
        s = Sample(
            mem_available_gb=row.get("mem_available_gb") if _num(row.get("mem_available_gb")) else None,
            load1=row.get("load1") if _num(row.get("load1")) else None,
            load5=row.get("load5") if _num(row.get("load5")) else None,
            psi_cpu_some10=row.get("psi_cpu_some10") if _num(row.get("psi_cpu_some10")) else None,
            psi_mem_some10=row.get("psi_mem_some10") if _num(row.get("psi_mem_some10")) else None,
            system_pct=row.get("system_pct") if _num(row.get("system_pct")) else None,
            at=float(row["at"]) if _num(row.get("at")) else None,
        )
        if row.get("status") not in (None, "fresh"):
            s.status, s.note = "absent", str(row.get("status"))
        else:
            if _num(row.get("age_s")) and elapsed is not None:
                age = float(row["age_s"]) + elapsed          # collector-normalized (producer clock + our elapsed)
                s.at = now - age                             # re-stamp on the CALLER's clock: headroom()'s decision-time
                #                                              re-check (baba105e) and every later `now` compare to this
            elif s.at is not None and elapsed is None:
                age = now - s.at                              # no document clock: a raw sampler row, same clock assumed
            else:
                age = None                                    # a collector document whose row has no age_s: unmeasured
                s.at = None
            if age is None or age > stale:
                s.status, s.note = "stale", f"sample {'age unmeasured' if age is None else '%.0fs old' % age}"
        out[hid] = s
    return out


def sample_from_probe(load1: float, memavail_gb: Optional[float], *, load5: Optional[float] = None,
                      now: Optional[float] = None) -> Sample:
    """A sample from a direct remote probe (test-gate's path): fresh by construction. Only load1 (and load5 when
    the probe carried it) and memory are measured; the PSI / %system axes are UNMEASURED and therefore CLOSE the
    host unless ``require_axes`` is relaxed at choose() time (test-gate passes the axes its probe reads).
    A non-finite reading (NaN, inf, a string) is UNMEASURED, never a number the gate compares against."""
    def fin(v):
        return float(v) if _num(v) else None
    # a probe sample is stamped NOW by default so the decision-time freshness check can vouch for it (Prism 6704368b)
    return Sample(mem_available_gb=fin(memavail_gb), load1=fin(load1), load5=fin(load5),
                  at=_now(now))


# ── scoring ──────────────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Headroom:
    host: str
    eligible: bool
    reason: str                       # "" when eligible, else why not (closed:<x> | excluded:<x> | no_headroom:<axis>)
    mem_gb: Optional[float] = None    # avail - reserve - footprint
    cores: Optional[float] = None     # cores - reserve - load - threads
    score: float = float("-inf")      # binding fraction of capacity
    binding: str = ""
    load: Optional[float] = None
    mem_available_gb: Optional[float] = None
    reserved_gb: float = 0.0          # live reservations already promised on the host
    reserved_threads: float = 0.0

    def line(self, spec: HostSpec) -> str:
        """`<host> load N / Cc.Tt, avail X GB, headroom Y GB / Z cores (binding axis), score S` or the refusal."""
        load = "?" if self.load is None else f"{self.load:g}"
        avail = "?" if self.mem_available_gb is None else f"{self.mem_available_gb:.1f}"
        res = f", reserved {self.reserved_gb:g} GB / {self.reserved_threads:g} thr" if (self.reserved_gb or self.reserved_threads) else ""
        head = ("" if self.mem_gb is None else
                f"{res}, headroom {self.mem_gb:.1f} GB / {self.cores:.1f} cores ({self.binding or '-'})")
        tail = f", score {self.score:.3f}" if self.eligible else f", {self.reason}"
        return f"{self.host} load {load} / {spec.cores}c.{spec.threads}t, avail {avail} GB{head}{tail}"


def headroom(spec: HostSpec, sample: Optional[Sample], fp: Footprint, *, consumer: str,
             closed_when: Mapping[str, float], excluded: Optional[str] = None,
             reserved: Optional[Footprint] = None, require_axes: Iterable[str] = REQUIRED_AXES,
             slots_used: float = 0.0, now: Optional[float] = None, stale_after_s: Optional[float] = None) -> Headroom:
    """``reserved`` = the sum of LIVE reservations on this host (promised, not yet in the sample).
    ``slots_used`` = live reservations of THIS consumer on this host; a positive ``consumers.<c>.slots`` caps it.
    ``require_axes``: every named Sample field must be a finite number, else the host is closed (unmeasured).
    ``stale_after_s`` (with ``now``): re-check the sample's age at decision time; a sample marked fresh when it
    was read but older than this now is CLOSED (Prism baba105e: admit() can wait on the ledger lock)."""
    if now is not None:
        now = _now(now)
    stale_after_s = _stale_after(stale_after_s)
    h = Headroom(host=spec.name, eligible=False, reason="")
    if excluded:
        h.reason = f"excluded:{excluded}"
        return h
    slots = spec.slots_for(consumer)
    if slots is not None and slots <= 0:
        h.reason = f"excluded:{consumer} slots 0"
        return h
    if slots is not None and slots_used >= slots:
        # a positive slot count is a concurrency CAP, counted against live admissions (Prism P1 696cce63)
        h.reason = f"no_slots:{consumer} {slots_used:g}/{slots} in use"
        return h
    if sample is None:
        h.reason = "closed:no sample"
        return h
    h.load, h.mem_available_gb = sample.load, sample.mem_available_gb
    if sample.status != "fresh":
        h.reason = f"closed:{sample.status}" + (f" ({sample.note})" if sample.note else "")
        return h
    if stale_after_s is not None and now is not None:
        if not _num(sample.at):
            h.reason = "closed:stale (sample time missing)"
            return h
        if now - float(sample.at) > stale_after_s:
            h.reason = f"closed:stale (sample {now - float(sample.at):.0f}s old at decision time)"
            return h
    if h.load is None or h.mem_available_gb is None:
        h.reason = "closed:unmeasured " + ("load" if h.load is None else "memory")
        return h
    missing = [a for a in require_axes if a not in spec.unmeasurable_axes and not _num(getattr(sample, a, None))]
    if missing:
        h.reason = "closed:unmeasured " + ",".join(missing)
        return h
    required = set(require_axes) - set(spec.unmeasurable_axes)
    for axis, thr in closed_when.items():
        v = getattr(sample, axis, None)
        if not _num(v):
            if axis in required:
                h.reason = f"closed:unmeasured {axis}"
                return h
            continue          # the caller narrowed require_axes: this axis is declared unavailable, not ignored
        if float(v) >= thr:
            h.reason = f"closed:{axis} {v:g} >= {thr:g}"
            return h
    r_mem, r_thr = (reserved.ram_gb, reserved.threads) if reserved else (0.0, 0.0)
    h.reserved_gb, h.reserved_threads = r_mem, r_thr
    h.mem_gb = h.mem_available_gb - spec.mem_reserve_gb - r_mem - fp.ram_gb
    h.cores = spec.cores - spec.cores_reserve - h.load - r_thr - fp.threads
    mem_frac = h.mem_gb / spec.mem_total_gb
    cpu_frac = h.cores / spec.cores
    h.score, h.binding = (mem_frac, "mem") if mem_frac <= cpu_frac else (cpu_frac, "cpu")
    if h.mem_gb < 0:
        h.reason = f"no_headroom:mem {h.mem_gb:.1f} GB"
        return h
    if h.cores < 0:
        h.reason = f"no_headroom:cpu {h.cores:.1f} cores"
        return h
    h.eligible = True
    return h


@dataclasses.dataclass(frozen=True)
class Job:
    """Per-job overrides (layer 3). Every field optional; an override naming a host that is not eligible is
    ignored (the gate always wins) and the fallthrough is recorded in Decision.rule."""
    affinity: Optional[str] = None      # the work lives there (files, running service, GPU)
    continuity: Optional[str] = None    # the previous run of this card ran there (warm workspace/caches)
    priority: Optional[int] = None      # 0/1 = may claim the fastest eligible core class
    core_class: Optional[str] = None    # "fast" (highest single-core clock) | "wide" (most threads) | None


@dataclasses.dataclass
class Decision:
    host: Optional[str]
    reason: str                              # "" on a pick; all_closed | no_headroom | no_candidates
    rows: List[Headroom]
    consumer: str
    footprint: Footprint
    kept_previous: bool = False
    rule: str = ""                           # which layer picked: affinity | continuity | priority | core_class:<c> | default_first | headroom

    def lines(self, table: HostTable) -> List[str]:
        return [r.line(table.hosts[r.host]) for r in self.rows]

    def summary(self, table: HostTable) -> str:
        head = (f"pick {self.host} by {self.rule}" + (" (kept, within margin)" if self.kept_previous else "")) \
            if self.host else f"refuse: {self.reason}"
        return f"{head} for {self.consumer} footprint {self.footprint.ram_gb:g} GB / {self.footprint.threads:g} threads; " \
               + "; ".join(self.lines(table))

    def as_row(self) -> dict:
        return {"picked": self.host, "why": self.reason or None, "rule": self.rule or None, "kept_previous": self.kept_previous,
                "consumer": self.consumer, "footprint": dataclasses.asdict(self.footprint),
                "hosts": {r.host: {"eligible": r.eligible, "reason": r.reason or None, "load": r.load,
                                   "memavail_gb": r.mem_available_gb,
                                   "reserved_gb": r.reserved_gb, "reserved_threads": r.reserved_threads,
                                   "headroom_gb": None if r.mem_gb is None else round(r.mem_gb, 1),
                                   "headroom_cores": None if r.cores is None else round(r.cores, 1),
                                   "score": None if not r.eligible else round(r.score, 4), "binding": r.binding or None}
                          for r in self.rows}}


def choose(table: HostTable, consumer: str, samples: Mapping[str, Optional[Sample]], *,
           candidates: Optional[Iterable[str]] = None, footprint: Optional[Mapping] = None,
           previous: Optional[str] = None, margin: float = DEFAULT_MARGIN,
           exclusions: Optional[Mapping[str, str]] = None, job: Optional[Job] = None,
           reservations: Optional[Mapping[str, Footprint]] = None,
           require_axes: Iterable[str] = REQUIRED_AXES, exclusions_path=None, now: Optional[float] = None,
           slots_used: Optional[Mapping[str, float]] = None, stale_after_s: Optional[float] = DEFAULT_STALE_AFTER_S) -> Decision:
    """The three layers over ``candidates`` (default: every host in the table).

    1. gate: a candidate with no sample, a stale/absent one, an unmeasured axis, a tripped closed_when axis,
       slots 0, a positive slot count already taken by ``slots_used`` live admissions of this consumer,
       an exclusion row, or headroom < 0 on either axis is NOT eligible. No eligible host = REFUSE
       (host None): the caller queues, never spills.
    2. default: ``job`` overrides first (affinity, continuity, priority, core_class; only an ELIGIBLE host
       is ever taken, so the gate always wins); else ``table.default_first`` while its load is at or under
       ``saturate_to`` x physical cores; else the most headroom. ``previous`` (the last pick) is kept while
       the best other host does not beat it by ``margin`` of capacity (hysteresis; headroom layer only).
    ``stale_after_s`` (default 60 s) re-checks every sample's age against ``now`` at decision time (Prism fda1ea43);
    pass None only when the caller has just produced the samples itself and vouches for their freshness.
    """
    stale_after_s = _stale_after(stale_after_s)
    fp = table.footprint(consumer, footprint)
    names = list(candidates) if candidates is not None else list(table.hosts)
    folded: Dict[str, Optional[Sample]] = {}
    for k, v in samples.items():
        hid = table.resolve(k)
        if hid is not None and (hid not in folded or folded[hid] is None):
            folded[hid] = v
    now = _now(now)
    require_axes = tuple(require_axes)   # a generator would be exhausted by the first candidate (Prism P1 16d4270a)
    # live operator exclusions (var/placement/exclusions.json, TTL rows) ALWAYS apply; the caller's map adds to them
    # (Prism 95afdab6): a caller entry never REPLACES an operator row, and an empty reason still excludes
    excl: Dict[str, str] = {}
    for k, v in read_exclusions(exclusions_path, now=now).items():
        excl.setdefault(table.resolve(k) or k, str(v) or "excluded")
    for k, v in (exclusions or {}).items():
        excl.setdefault(table.resolve(k) or k, (str(v) if v is not None else "") or "excluded")
    resv: Dict[str, Footprint] = {}
    for k, v in (reservations or {}).items():      # aliases of one host SUM, never overwrite (Prism P1 8b7fd838)
        hid = table.resolve(k) or k
        prev = resv.get(hid)
        resv[hid] = Footprint(prev.ram_gb + v.ram_gb, prev.threads + v.threads) if prev else v
    used: Dict[str, float] = {}
    for k, v in (slots_used or {}).items():           # per-consumer live admissions, aliases summed likewise
        hid = table.resolve(k) or k
        used[hid] = used.get(hid, 0.0) + float(v)
    rows: List[Headroom] = []
    seen = set()
    for n in names:
        hid = table.resolve(n)
        if hid is None:
            rows.append(Headroom(host=str(n), eligible=False, reason="closed:not in config/fleet-hosts.json"))
            continue
        if hid in seen:
            continue
        seen.add(hid)
        rows.append(headroom(table.hosts[hid], folded.get(hid), fp, consumer=consumer,
                             closed_when=table.closed_when, excluded=excl.get(hid), reserved=resv.get(hid),
                             require_axes=require_axes, slots_used=used.get(hid, 0.0),
                             now=now, stale_after_s=stale_after_s))
    eligible = [r for r in rows if r.eligible]
    if not rows:
        return Decision(None, "no_candidates", rows, consumer, fp)
    if not eligible:
        why = "no_headroom" if any(r.reason.startswith("no_headroom") for r in rows) else "all_closed"
        return Decision(None, why, rows, consumer, fp)
    by_id = {r.host: r for r in eligible}

    def pick(row: Headroom, rule: str, kept: bool = False) -> Decision:
        return Decision(row.host, "", rows, consumer, fp, kept_previous=kept, rule=rule)

    # layer 3: per-job overrides (an ineligible target falls through; the gate always wins)
    job = job or Job()
    for field in ("affinity", "continuity"):
        want = table.resolve(getattr(job, field) or "")
        if want and want in by_id:
            return pick(by_id[want], field)
    core_class = job.core_class or ("fast" if job.priority is not None and job.priority <= 1 else None)
    if core_class == "fast":
        best = max(eligible, key=lambda r: (table.hosts[r.host].clock_mhz, r.score, r.host))
        return pick(best, "priority" if job.core_class is None else "core_class:fast")
    if core_class == "wide":
        best = max(eligible, key=lambda r: (table.hosts[r.host].threads, r.score, r.host))
        return pick(best, "core_class:wide")
    # layer 2: saturate the default-first host while it is comfortable
    df = table.default_first
    if df and df in by_id:
        spec, row = table.hosts[df], by_id[df]
        # pending admissions count against the ceiling (Prism prism-router#511 838b2eb6)
        if row.load is not None and row.load + row.reserved_threads + fp.threads <= table.saturate_to * spec.cores:
            return pick(row, "default_first")
    # then the most headroom among normal-preference hosts; a low-preference host (the Studio) only when none
    # of them has headroom (Ace 18:1x: "prioritize them lower unless there's a good reason")
    normal = [r for r in eligible if table.hosts[r.host].default_preference != "low"]
    pool = normal or eligible
    best = max(pool, key=lambda r: (r.score, r.host))
    prev_id = table.resolve(previous) if previous else None
    if prev_id and prev_id != best.host and prev_id in by_id and best.score - by_id[prev_id].score < margin \
            and table.hosts[prev_id].default_preference == table.hosts[best.host].default_preference:
        return pick(by_id[prev_id], "headroom", kept=True)
    return pick(best, "headroom" if normal else "headroom:low-preference")



# ── reservation ledger (A3): promises the sample cannot see yet ─────────────────────────────────
@contextlib.contextmanager
def _locked(path: Path):
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_ledger(path: Path) -> dict:
    """The ledger document. A MISSING file is an empty ledger; anything else that fails is LedgerError (a
    reserve()/release() must never replace a ledger it could not read: that drops live reservations)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema": SCHEMA, "rows": []}
    except OSError as exc:
        raise LedgerError(f"{path}: unreadable: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise LedgerError(f"{path}: unparseable: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
        raise LedgerError(f"{path}: not a ledger document")
    for i, row in enumerate(doc["rows"]):
        bad = _ledger_row_error(row)
        if bad:
            # a malformed row is never read as expired: that would drop it on the next write (Prism 61a946fa)
            raise LedgerError(f"{path}: rows[{i}]: {bad}")
    return doc


def _ledger_row_error(row) -> str:
    if not isinstance(row, dict):
        return f"must be an object, not {type(row).__name__}"
    if not isinstance(row.get("id"), str) or not row["id"]:
        return f"id must be a non-empty string, not {row.get('id')!r}"
    if not isinstance(row.get("host"), str) or not row["host"]:
        return f"host must be a non-empty string, not {row.get('host')!r}"
    if not isinstance(row.get("consumer"), str) or not row["consumer"]:
        return f"consumer must be a non-empty string, not {row.get('consumer')!r}"
    for k in ("ram_gb", "threads"):
        if not _num(row.get(k)) or row[k] < 0:
            return f"{k} must be a finite number >= 0, not {row.get(k)!r}"
    if not _num(row.get("until")):
        return f"until must be a finite epoch number, not {row.get('until')!r}"
    if "at" in row and row["at"] is not None and not _num(row["at"]):
        return f"at must be a finite epoch number, not {row['at']!r}"
    if "released" in row and not isinstance(row["released"], bool):
        return f"released must be a bool, not {row['released']!r}"
    return ""


def _write_ledger(path: Path, doc: dict) -> None:
    """Atomic AND durable: the temp file is fsync'd before the rename and the directory after it, so a power
    loss right after a remote job launched cannot forget the reservation it was launched on (Prism 9da46cee)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    # a directory fsync that fails is a write we cannot call durable: raise, never report success (Prism f60d925a)
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
    except OSError as exc:
        raise LedgerError(f"{path}: directory not durable: {exc}") from exc
    try:
        os.fsync(dfd)
    except OSError as exc:
        raise LedgerError(f"{path}: directory fsync failed: {exc}") from exc
    finally:
        os.close(dfd)


def _row_live(row: Mapping, now: float) -> bool:
    return _num(row.get("until")) and float(row["until"]) > now and not row.get("released")


def reservations_path(path=None) -> Path:
    return Path(path) if path is not None else fleet_root() / RESERVATIONS_REL


def live_reservations(path=None, *, now: Optional[float] = None, table: Optional[HostTable] = None) -> Dict[str, Footprint]:
    """{host: summed Footprint} of every reservation whose TTL has not expired and that was not released.
    With ``table`` the keys are canonical host ids (aliases summed). Raises LedgerError on a corrupt ledger."""
    now = _now(now)
    doc = _read_ledger(reservations_path(path))
    out: Dict[str, List[float]] = {}
    for row in doc["rows"]:          # every row validated by _read_ledger
        if _row_live(row, now):
            key = (table.resolve(row["host"]) or row["host"]) if table else row["host"]
            acc = out.setdefault(key, [0.0, 0.0])
            acc[0] += float(row["ram_gb"])
            acc[1] += float(row["threads"])
    return {h: Footprint(a[0], a[1]) for h, a in out.items()}


def live_slots(consumer: str, path=None, *, now: Optional[float] = None, table: Optional[HostTable] = None) -> Dict[str, int]:
    """{host: number of LIVE reservations held by ``consumer``}: what a positive ``consumers.<c>.slots`` caps
    (Prism 696cce63). Same liveness and alias rules as live_reservations(); a row with no consumer is not counted."""
    now = _now(now)
    doc = _read_ledger(reservations_path(path))
    out: Dict[str, int] = {}
    for row in doc["rows"]:
        if _row_live(row, now) and row.get("consumer") == consumer:
            key = (table.resolve(row["host"]) or row["host"]) if table else row["host"]
            out[key] = out.get(key, 0) + 1
    return out


def reserve(host: str, consumer: str, fp: Footprint, *, ttl_s: float, job_id: Optional[str] = None,
            path=None, now: Optional[float] = None, admissions_path=None, decision: Optional[Decision] = None) -> str:
    """Record ``fp`` against ``host`` for ``ttl_s`` seconds; returns the reservation id. Expired and released
    rows are pruned on every write. The pick is also appended to the admissions ledger (one line per pick).
    The default ``now`` is taken AFTER the lock is held: a lock wait longer than ``ttl_s`` must not write an
    already-expired row (Prism fe666f92). An explicit ``now`` is kept as given."""
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    p = reservations_path(path)
    with _locked(p):
        now = _now(now)
        return _reserve_unlocked(p, host, consumer, fp, ttl_s=ttl_s, job_id=job_id, now=now,
                                 admissions_path=admissions_path, decision=decision)


def _reserve_unlocked(p: Path, host: str, consumer: str, fp: Footprint, *, ttl_s: float, job_id, now: float,
                      admissions_path, decision: Optional[Decision]) -> str:
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    if not isinstance(fp, Footprint):
        raise PlacementError("fp must be a Footprint")
    rid = uuid.uuid4().hex[:12]
    doc = _read_ledger(p)
    rows = [r for r in doc["rows"] if _row_live(r, now)]
    row = {"id": rid, "host": host, "consumer": consumer, "job_id": job_id, "ram_gb": fp.ram_gb,
           "threads": fp.threads, "at": now, "until": now + float(ttl_s)}
    bad = _ledger_row_error(row)
    if bad:
        # never write a row the reader would reject: that would wedge every later admit/renew/release (Prism 0824b4b2)
        raise PlacementError(f"invalid reservation ({bad}); ledger left unchanged")
    rows.append(row)
    _write_ledger(p, {"schema": SCHEMA, "rows": rows})
    ap = Path(admissions_path) if admissions_path is not None else fleet_root() / ADMISSIONS_REL
    try:
        ap.parent.mkdir(parents=True, exist_ok=True)
        with open(ap, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": now, "host": host, "consumer": consumer, "job_id": job_id, "reservation": rid,
                                 "ram_gb": fp.ram_gb, "threads": fp.threads, "ttl_s": float(ttl_s),
                                 **({"rule": decision.rule, "hosts": decision.as_row()["hosts"]} if decision else {})},
                                sort_keys=True) + "\n")
    except OSError:
        pass
    return rid


def admit(table: HostTable, consumer: str, samples: Mapping[str, Optional[Sample]], *, ttl_s: float,
          job_id: Optional[str] = None, path=None, admissions_path=None, now: Optional[float] = None,
          **choose_kw):
    """ONE locked transaction: read the live ledger, choose(), reserve() (Prism P1 02405b39 / b7233dbf).
    Two dispatchers admitting concurrently serialize on the ledger lock, so the second sees the first's
    reservation. Returns (Decision, reservation id or None). ``choose_kw`` = choose()'s keyword arguments.
    The default ``now`` is resolved AFTER the lock is held (Prism fe666f92), and every sample's age is re-checked
    against it (``stale_after_s``, default 60 s; Prism baba105e): a sample that went stale while this admit waited
    on the lock closes its host instead of admitting on outdated capacity."""
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    stale_after_s = choose_kw.pop("stale_after_s", DEFAULT_STALE_AFTER_S)
    p = reservations_path(path)
    with _locked(p):
        now = _now(now)
        resv = live_reservations(p, now=now, table=table)
        used = live_slots(consumer, p, now=now, table=table)     # positive slot caps count live admissions (696cce63)
        d = choose(table, consumer, samples, reservations=resv, slots_used=used, now=now,
                   stale_after_s=stale_after_s, **choose_kw)
        if not d.host:
            return d, None
        rid = _reserve_unlocked(p, d.host, consumer, d.footprint, ttl_s=ttl_s, job_id=job_id, now=now,
                                admissions_path=admissions_path, decision=d)
    return d, rid


def renew(reservation_id: str, *, ttl_s: float, path=None, now: Optional[float] = None) -> bool:
    """Extend a LIVE reservation to ``now + ttl_s`` (a lease the holder keeps renewing while its job runs, so a
    run longer than any fixed TTL stays counted; a holder that dies stops renewing and the row lapses; Prism
    796aae4f). False when the row is gone, expired or released (it is never resurrected)."""
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    p = reservations_path(path)
    with _locked(p):
        now = _now(now)
        doc = _read_ledger(p)
        rows = [r for r in doc["rows"] if _row_live(r, now)]
        hit = False
        for r in rows:
            if r["id"] == reservation_id:
                r["until"] = max(float(r["until"]), now + float(ttl_s))
                hit = True
        if hit:
            _write_ledger(p, {"schema": SCHEMA, "rows": rows})
    return hit


def release(reservation_id: str, *, path=None, now: Optional[float] = None) -> bool:
    """Mark a reservation released (the job ended early); True when it was live."""
    p = reservations_path(path)
    with _locked(p):
        now = _now(now)
        doc = _read_ledger(p)
        hit = False
        rows = []
        for r in doc["rows"]:
            if not _row_live(r, now):
                continue
            if r.get("id") == reservation_id:
                hit = True
                continue
            rows.append(r)
        _write_ledger(p, {"schema": SCHEMA, "rows": rows})
    return hit


def fmt_load(load: Optional[float], cores: int, threads: int) -> str:
    """Ace's notation: `N / Cc.Tt`."""
    return f"{'?' if load is None else '%g' % load} / {cores}c.{threads}t"
