"""placement -- ONE placement scorer: balance by measured HEADROOM and reservations, never by rank.

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
  small operator-raisable ceiling; the headroom layer takes it only when no normal-preference host has
  headroom, while affinity / continuity / priority / core_class select it like any other host.

Per host the inputs are the sampler's MemAvailable, load (load1, and load5 when the caller has it: the
LARGER of the two counts, so a box clears only when its 5-minute queue has), PSI cpu/mem some10, %system,
PLUS the reservation table: memory and cores held back for the host's residents (ace-ai: the voice hub +
ML services; ci-box: the runner pool burst; ace-media: the media stack). Headroom on each axis =
available - reserve - the job's declared footprint; the score is the BINDING axis as a fraction of the
host's capacity, so a 16-core and a 128-core box compare on the same scale. choose() applies the three
layers above with hysteresis (a previous pick is kept unless another host beats it by `margin`), refuses when
no host has headroom >= the footprint (the caller queues; it never spills), and treats an absent / stale /
unmeasured sample as CLOSED, never "ok". A host may be EXCLUDED (``eligible: false`` for the consumer, or a live fwa
hold on its lane; B5-P3 t_00664b58 retired the exclusions file). The ONLY ordinal in the table is `default_first`
(one host, the one to saturate); a table carrying a ladder / prio / order / rank / last key is refused.

Reservation at pick time (A3, t_08de4ba5 folded in here): twenty picks in one tick must see twenty different
headrooms, so a pick is followed by ``reserve(...)`` which records the footprint against the host for the job's
expected duration (``ttl_s``) in var/placement/headroom-reservations.json; ``live_reservations(now)`` is
subtracted from every host's available memory and cores by ``choose(..., reservations=...)``, and every pick
is appended to var/placement/admissions.jsonl (the per-host "what I admitted" ledger the 07:10 incident lacked).

The admissions ledger (allocation 2.3, t_08de4ba5): one JSON line per decision, ``{at, host, consumer, job_id,
footprint{ram_gb,threads,scratch_gb,gpu_mb}, band, decision, reason, ...}``. ``decision`` is ``admitted`` (a
reservation was written), ``deferred`` (admit() refused: no host fits; the caller queues), ``released`` (the
holder ended the lease) or ``expired`` (the TTL lapsed without renew/release: the reaper, like
task_runs.claim_expires). ``band`` is the chosen host's band at decision time WITH live reservations (ok = room
for one more, warm = room for this one only); each ``hosts.<h>`` row carries its own. A job waiting on the same
refusal writes one ``deferred`` line, then again only when the reason changes or every DEFERRED_REEMIT_S.
``admitted`` and ``deferred`` lines carry ``admission_mode`` (legacy | utilization: the mode that decided them;
None for a pin booked without a decision), so a readback reads the mode off the ledger (t_f73c65a8).

Footprint axes: ram_gb and threads gate every pick; scratch_gb gates when the sample measures
``scratch_free_gb`` (host-pressure.json does; a direct probe does not) and is otherwise only reserved; gpu_mb is
declared and reserved, never gated (no sampler publishes free GPU memory yet). A job overrides its class default
with ``Job(footprint={...})``; ``footprint_from_text()`` reads a card/body line ``footprint: ram_gb=8 threads=4``.

Notation (Ace 2026-10-12 08:21): every human-facing line prints the load as `N / Cc.Tt` (tasks over
physical cores and threads) and the headroom in GB.

Stdlib only; python3.9+.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Dict, Iterable, List, Mapping, Optional

if __package__:                      # hermes_cli.placement (the fork's vendored copy) / lib.placement
    from . import admission, work_estimate
else:                                # scripts/lib on sys.path (every hermes-home caller)
    import admission
    import work_estimate

SCHEMA = 1
TABLE_REL = Path("config") / "fleet-hosts.json"
# Fleet-wide work-kind switches (t_47109d72, `fleet pause <host|all> --work <kind>`), written only by
# scripts/lib/fleet_kinds.py. A live row closes its kind's consumers on its scope (one host, or every host).
KINDS_REL = Path("state") / "fleet-pause" / "kinds.json"
CONSUMER_KIND = {"kanban": "kanban", "athena": "kanban", "prism": "prism", "ci": "ci"}
RESERVATIONS_REL = Path("var") / "placement" / "headroom-reservations.json"
ADMISSIONS_REL = Path("var") / "placement" / "admissions.jsonl"
# Utilization admission (t_dc960eab R2 / t_da6f84f7; Ace 2026-10-18 14:33 / 15:5x): the kill switch is config.yaml
# ``kanban.admission: utilization|legacy`` (read per call, mtime-cached); absent / unreadable / anything else = legacy,
# which is the pre-R2 path byte for byte. utilization = admission.admit() over the pushed sample + measured estimates.
CONFIG_REL = Path("config.yaml")
CLASS_USAGE_REL = work_estimate.CLASS_USAGE_REL
ADMISSION_MODES = ("legacy", "utilization")
CLASS_USAGE_WINDOW_S = work_estimate.CLASS_USAGE_WINDOW_S
# consumer -> the measured job class: owned by work_estimate (the ONE estimator, t_9e395789); re-exported for callers
CONSUMER_CLASS = work_estimate.CONSUMER_CLASS
RANK_KEYS = frozenset({"prio", "ladder", "order", "rank", "last", "last_pick", "preference", "preferred"})
DEFAULT_SATURATE_TO = 1.0   # the default-first host is "comfortable" while load <= saturate_to x physical cores
DEFAULT_STALE_AFTER_S = 60.0
# A reservation older than this is a RUNNING process, already inside the host's load1 (the 1-minute load average):
# its threads are not subtracted again on the cpu axis (t_bfb88c23: ace-ai 16 - 4 reserve - 7.7 load - 5 reserved
# thr - 1 = -1.7 on a box with 8 idle cores). Memory keeps the full promise (MemAvailable lags allocation).
LOAD1_WINDOW_S = 60.0
DEFAULT_MARGIN = 0.10
DEFERRED_REEMIT_S = 900.0    # a job still deferred for the same reason is re-logged at most this often
FOOTPRINT_KEYS = ("ram_gb", "threads", "scratch_gb", "gpu_mb")
# A host whose sample trips any of these is CLOSED for the tick (the signals that predicted 10-12 07:20:
# %system 6 -> 33 with CPU 45 % idle; psi mem is the collapse itself).
DEFAULT_CLOSED_WHEN = {"psi_mem_some10": 10.0, "psi_cpu_some10": 40.0, "system_pct": 25.0}
# Axes a sample MUST carry to be eligible (an unmeasured safety axis is CLOSED, never "ok"). The sampler row
# (fleet-pressure.json) carries all of them; a direct probe that cannot read PSI passes a narrower set explicitly.
REQUIRED_AXES = ("mem_available_gb", "load1", "load5", "psi_cpu_some10", "psi_mem_some10", "system_pct")
# ── scorer v2 (FWA R2c, t_f8a3b6c3; spec plans/2026-10-18_test-gate-onto-allocation-router-spec.md R2c) ──────────
# The kill switch is the table's top-level ``scorer: v1|v2`` (env PLACEMENT_SCORER beats it, for drills and the
# replay). v1 = the cpu axis as it was (physical cores - load - reserved threads, ``scorer_v1.cpu_saturable`` hosts
# penalised instead of refused). v2 = capacity cores x mt_yield, occupancy = runnable work, CLOSED on CPU stall (PSI),
# and speed enters the CHOICE (score = headroom_fraction x st_speed^alpha), never the gate.
SCORERS = ("v1", "v2")
DEFAULT_SCORER = "v1"
SPEED_REL = Path("var") / "placement" / "host-speed.json"     # written by R2b (host-speed-derive); read-only here
SEED_MT_YIELD_SMT2 = 1.3     # Ace 10-18 09:48: ci-box 166 = 128 x 1.3 until measured; SMT2 hosts only, else 1.0
# CLOSED on PSI cpu some avg10 >= 40 is closed_when (DEFAULT_CLOSED_WHEN, both scorers); v2 adds avg60 >= 25
V2_PSI_CPU_SOME60 = 25.0
# Intermittent hosts (t_58366dea, MBP-2026; Apollo ruling 2026-11-06 09:15): ``hosts.<h>.power_policy: ac_only`` closes
# the host unless its CURRENT sample says it runs on AC and sleep is disabled. The sampler measures both (macOS IOKit
# power source + pmset SleepDisabled); unmeasured = closed, like every other safety axis.
POWER_POLICIES = ("ac_only",)
ALPHA_DEFAULT, ALPHA_FAST, ALPHA_WIDE = 0.5, 1.0, 0.0     # D2: P0/P1 or core_class fast = 1, wide = 0
OCCUPANCY_TAU_S = 60.0       # the sampler's EWMA time constant for procs_running - 1


class PlacementError(ValueError):
    """The host table is absent, unreadable, carries a rank, or fails validation."""


class LedgerError(PlacementError):
    """The reservation ledger exists but cannot be read or parsed: it is never overwritten (Prism P1 51e7cfc6)."""


def fleet_root(root: Optional[Path] = None) -> Path:
    if __package__ == "fleet_admission":
        from .roots import resolve_root
        return resolve_root(root, source_file=__file__, package=__package__)
    if root is not None:
        return Path(root)
    return Path(os.environ["PLACEMENT_HOME"]) if os.environ.get("PLACEMENT_HOME") else _tree_root()


def _tree_root() -> Path:
    """The tree this file sits in; a release of the fleet home answers the home (t_5d7db1b6: during the 11-13 cut
    allocd's root became releases/<x>/, which holds no admission receipt, and it refused every request 503)."""
    here = Path(__file__).resolve().parents[2]
    try:
        from fleet_root import release_home
    except ImportError:   # a partial copy without the resolver is never a release
        return here
    return Path(release_home(here) or here)


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
    scratch_gb: float = 0.0
    gpu_mb: float = 0.0

    def __post_init__(self):
        for k in FOOTPRINT_KEYS:
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise PlacementError(f"footprint.{k} must be a finite number >= 0, not {v!r}")

    @classmethod
    def of(cls, d: Mapping) -> "Footprint":
        return cls(*(float(d.get(k, 0.0)) for k in FOOTPRINT_KEYS))

    def plus(self, other: "Footprint") -> "Footprint":
        return Footprint(*(getattr(self, k) + getattr(other, k) for k in FOOTPRINT_KEYS))


@dataclasses.dataclass(frozen=True)
class HostSpec:
    name: str
    cores: int
    threads: int
    mem_total_gb: float
    mem_reserve_gb: float
    cores_reserve: float
    ceilings: Mapping[str, int]
    aliases: tuple
    clock_mhz: float = 0.0           # max single-core clock (core_class "fast")
    default_preference: str = "normal"   # "low" = the headroom layer takes it only when no normal host has headroom
    unmeasurable_axes: tuple = ()        # axes this host's OS cannot report (darwin: no PSI, no %system); never required
    cpu_saturable: bool = False          # v1 only: negative cpu headroom is a score penalty, not a refusal (t_bfb88c23)
    # scorer v2 (t_f8a3b6c3): the active scorer and the host's MEASURED speed (var/placement/host-speed.json, R2b),
    # or the seed when unmeasured (mt_yield 1.3 on SMT2, else 1.0; st_speed 1.0). ``speed_src`` / ``st_speed_src`` name which.
    scorer: str = DEFAULT_SCORER
    mt_yield: float = 1.0
    st_speed: float = 1.0
    speed_src: str = "seed"              # mt_yield: measured | seed
    st_speed_src: str = "seed"           # st_speed: measured | seed (Prism e0c016b8: tracked apart from mt_yield)
    # a guest on another host's box with no sampler of its own (t_59943ed5: the Studio's OrbStack Prism member
    # studio-arm): admission reads THAT host's sample (sample_for), and v1 cpu_saturable is inherited from it
    pressure_from: Optional[str] = None
    # a pressure_from guest is DEFAULT-DENY (Prism ae77b59991cb, t_59943ed5 r7): eligible only for the consumers its
    # row names with ``eligible: true``; every other consumer is closed for it (``closed:guest-not-enrolled``)
    enrolled: frozenset = frozenset()
    # a pressure_from guest's OWN size in CPUs (its VM: OrbStack studio-arm is 12 vCPU while it is judged on the
    # Studio's 24P/32T): the cap on the work admitted ON the guest, apart from the domain's shared headroom. None on
    # a guest = unmeasured, and the guest is closed (guest_vm_refusal); unused on a non-guest.
    vm_cpus: Optional[int] = None
    # t_58366dea: "ac_only" = closed unless the sample shows AC power and sleep disabled (power_refusal); None = no gate
    power_policy: Optional[str] = None
    # t_7566ba56: ((job_class, cpus), ...) from consumers.<c>.ramp_cpus: an unsettled lease of that class is charged its
    # measured first-90-s CPU (admission.Limits.ramp_cpus); () = full estimate. A malformed value is dropped, never fatal
    ramp_cpus: tuple = ()

    @property
    def cpu_capacity(self) -> float:
        """v2 cpu capacity in cores: physical cores x mt_yield - cores_reserve."""
        return self.cores * self.mt_yield - self.cores_reserve

    def ceiling_for(self, consumer: str) -> Optional[int]:
        s = self.ceilings.get(consumer)
        return None if s is None else int(s)


@dataclasses.dataclass(frozen=True)
class HostTable:
    hosts: Dict[str, HostSpec]
    footprints: Dict[str, Footprint]
    closed_when: Dict[str, float]
    path: Optional[Path]
    default_first: Optional[str] = None
    saturate_to: float = DEFAULT_SATURATE_TO
    # {consumer: {profile: Footprint}}: per-PROFILE classes (``footprints.<c>.profiles``, t_edc220cc); a profile not
    # listed keeps the consumer's own class
    profile_footprints: Mapping[str, Mapping[str, Footprint]] = dataclasses.field(default_factory=dict)
    scorer: str = DEFAULT_SCORER                 # the kill switch (v1|v2); active_scorer() applies PLACEMENT_SCORER
    # {host id: (pressure_from or None, aliases)} for EVERY row the table declares, the ones load_hosts() left out
    # included: resource-domain membership is the registry's, not candidate eligibility's, so a closed guest's live
    # leases and reservations still count on its source (Prism 7e9f3e2ed307 / c2f0087775e6 / 03f3bf40fe6b)
    declared: Mapping[str, tuple] = dataclasses.field(default_factory=dict)
    # Optional hard reservation envelopes, keyed by the pressure-source host. Overhead is
    # held once for the whole domain, never partitioned among consumers.
    shared_budgets: Mapping[str, Mapping[str, float]] = dataclasses.field(default_factory=dict)

    def declared_id(self, name) -> Optional[str]:
        """Canonical id for ``name`` over the admitted hosts first, then every declared row (left-out ones too)."""
        hid = self.resolve(name)
        if hid is not None or not isinstance(name, str):
            return hid
        low = name.strip().lower()
        for d, (_src, aliases) in self.declared.items():
            if d.lower() == low or low in (a.lower() for a in aliases):
                return d
        return None

    def resolve(self, name: str) -> Optional[str]:
        """Canonical host id for ``name`` or one of its aliases (case-folded), else None."""
        if not isinstance(name, str):
            return None
        low = name.strip().lower()
        for hid, h in self.hosts.items():
            if hid.lower() == low or low in (a.lower() for a in h.aliases):
                return hid
        return None

    def footprint(self, consumer: str, override: Optional[Mapping] = None, profile: Optional[str] = None) -> Footprint:
        base = self.footprints.get(consumer)
        if base is None:
            raise PlacementError(f"no default footprint for consumer {consumer!r}")
        base = (self.profile_footprints.get(consumer) or {}).get(profile or "", base)
        if not override:
            return base
        bad = sorted(set(override) - set(FOOTPRINT_KEYS))
        vals = {k: override.get(k, getattr(base, k)) for k in FOOTPRINT_KEYS}
        if bad or not all(_num(v) for v in vals.values()):
            raise PlacementError(f"footprint override must be finite numbers for {list(FOOTPRINT_KEYS)}, not {override!r}")
        return Footprint(**{k: float(v) for k, v in vals.items()})


COORDINATION_HOST = "_fleet"   # allocd /lease fence rows (prism-portable R-4): zero need, never a work admission
MAX_CEILING = 1000   # prism-portable R-2 (t_24b005e5), = prism_pool_members.SAFETY_STOP and the prism-router registry ceiling (#823): the router's upper bound; an eligible row without a ceiling


def _eligible_errors(hid, c, cv) -> List[str]:
    """``consumers.<c>`` = {eligible: bool, ceiling: 1..MAX_CEILING optional} XOR the legacy count key (prism-portable R-1;
    read until t_21a7ace3's deploy cycle ends, then refused);
    ``source`` lives only in the top-level ``consumers_<c>`` block, never in a host row (Q5(b))."""
    errs = []
    if "source" in cv:
        errs.append(f"hosts.{hid}.consumers.{c}.source: the source switch is top-level consumers_{c}.source only")
    if "eligible" in cv and LEGACY_COUNT_KEY in cv:
        errs.append(f"hosts.{hid}.consumers.{c}: eligible and {LEGACY_COUNT_KEY} are exclusive (one key per row)")
    if "eligible" in cv and not isinstance(cv["eligible"], bool):
        errs.append(f"hosts.{hid}.consumers.{c}.eligible must be true|false")
    if "ceiling" in cv:
        cl = cv["ceiling"]
        if "eligible" not in cv:
            errs.append(f"hosts.{hid}.consumers.{c}.ceiling needs eligible")
        if not isinstance(cl, int) or isinstance(cl, bool) or not 1 <= cl <= MAX_CEILING:
            errs.append(f"hosts.{hid}.consumers.{c}.ceiling must be an int 1..{MAX_CEILING}")
    if "allocd" in cv:
        # t_1b9b2980: the CI start door's per-host kill switch, read on the pool host by fleet-ops-scripts
        # ci-runner-pool.sh / ci-runner-start.py / ci-runner-allocator.py (host_arm). CI only; a typo must not arm.
        # t_d3a0df9d: the Prism per-review door reads the same switch from consumers.prism (prism-router#892,
        # fwa.prism_allocd_mode). Only these two consumers have a start door.
        if c not in ("ci", "prism"):
            errs.append(f"hosts.{hid}.consumers.{c}.allocd: only consumers.ci and consumers.prism carry the start-door "
                        f"switch")
        elif cv["allocd"] not in ("armed", "spawn-only"):
            errs.append(f"hosts.{hid}.consumers.{c}.allocd must be armed|spawn-only")
    return errs


LEGACY_COUNT_KEY = "slots"   # slots-vocab: allow dual reader for one deploy cycle (t_21a7ace3), then removed


def ceiling_of(cv: Mapping) -> Optional[int]:
    """The live-admission ceiling a consumer row means: ``eligible: false`` = 0 (excluded); ``eligible: true`` +
    ``ceiling`` = that ceiling; the legacy count key as written (dual reader); otherwise none (headroom alone)."""
    if LEGACY_COUNT_KEY in cv:
        return int(cv[LEGACY_COUNT_KEY])
    if cv.get("eligible") is False:
        return 0
    if cv.get("eligible") is True and "ceiling" in cv:
        return int(cv["ceiling"])
    return None


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
            elif any(k in fp and (not _num(fp[k]) or fp[k] < 0) for k in ("scratch_gb", "gpu_mb")):
                errs.append(f"footprints.{c}: scratch_gb / gpu_mb must be numbers >= 0 when present")
            elif "profiles" in fp:
                profs = fp["profiles"]
                if not isinstance(profs, dict):
                    errs.append(f"footprints.{c}.profiles must map profile -> footprint")
                    continue
                for pn, pf in profs.items():
                    if pn.startswith("_"):
                        continue
                    if not isinstance(pf, dict) or not all(_num(pf.get(k)) and pf[k] >= 0 for k in FOOTPRINT_KEYS):
                        errs.append(f"footprints.{c}.profiles.{pn} needs all of {list(FOOTPRINT_KEYS)} as numbers >= 0")
    hosts = doc.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        errs.append("hosts must be a non-empty object")
        hosts = {}
    for hid, h in hosts.items():
        if not isinstance(h, dict):
            errs.append(f"hosts.{hid} must be an object")
            continue
        for k in ("cores", "threads"):
            # FWA R2a: cores/threads are DISCOVERED (var/placement/host-inventory.json); a table value is optional
            if k in h and (not isinstance(h.get(k), int) or isinstance(h.get(k), bool) or h[k] < 1):
                errs.append(f"hosts.{hid}.{k} must be an int >= 1")
        if h.get("count_cores", "physical") not in ("physical", "performance"):
            errs.append(f"hosts.{hid}.count_cores must be physical|performance")
        if isinstance(h.get("cores"), int) and isinstance(h.get("threads"), int) and h["threads"] < h["cores"]:
            errs.append(f"hosts.{hid}: threads {h['threads']} < cores {h['cores']}")
        for k in ("mem_total_gb", "mem_reserve_gb", "cores_reserve"):
            if not _num(h.get(k)) or h[k] < 0:
                errs.append(f"hosts.{hid}.{k} must be a number >= 0")
        if _num(h.get("mem_total_gb")) and _num(h.get("mem_reserve_gb")) and h["mem_reserve_gb"] >= h["mem_total_gb"]:
            errs.append(f"hosts.{hid}: mem_reserve_gb {h['mem_reserve_gb']} >= mem_total_gb {h['mem_total_gb']}")
        if _num(h.get("cores_reserve")) and isinstance(h.get("cores"), int) and h["cores_reserve"] >= h["cores"]:
            errs.append(f"hosts.{hid}: cores_reserve {h['cores_reserve']} >= cores {h['cores']}")
        if "shared_budget" in h:
            b = h["shared_budget"]
            keys = {"threads", "ram_gb", "overhead_threads", "overhead_ram_gb"}
            if not isinstance(b, dict) or set(b) != keys or any(not _num(b.get(k)) or b[k] < 0 for k in keys):
                errs.append(f"hosts.{hid}.shared_budget needs nonnegative finite {sorted(keys)}")
            elif b["threads"] <= b["overhead_threads"] or b["ram_gb"] <= b["overhead_ram_gb"]:
                errs.append(f"hosts.{hid}.shared_budget overhead must leave usable capacity")
            if h.get("pressure_from"):
                errs.append(f"hosts.{hid}.shared_budget belongs on the pressure source only")
        cons = h.get("consumers")
        if not isinstance(cons, dict):
            errs.append(f"hosts.{hid}.consumers must be an object")
        else:
            for c, cv in cons.items():
                # the legacy count key is OPTIONAL and only ever a CEILING (Ace 2026-10-18 13:13, t_dc960eab): absent =
                # admission by measured headroom alone; 0 = excluded; N > 0 = never more than N live, still gated
                lk = LEGACY_COUNT_KEY
                if not isinstance(cv, dict) or (lk in cv and (not isinstance(cv[lk], int)
                                                              or isinstance(cv[lk], bool) or cv[lk] < 0)):
                    errs.append(f"hosts.{hid}.consumers.{c}.{lk} must be an int >= 0 when present (absent = no ceiling)")
                    continue
                errs.extend(_eligible_errors(hid, c, cv))
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
    cp = doc.get("consumers_prism")
    if cp is not None and (not isinstance(cp, dict) or cp.get("source") not in ("fwa", "legacy")
                           or set(k for k in cp if not k.startswith("_")) != {"source"}):
        errs.append("consumers_prism must be {source: fwa|legacy} (prism-portable D-5)")
    if df is not None and (not isinstance(df, str) or df not in hosts):
        errs.append(f"default_first {df!r} must name one host in hosts")
    st = doc.get("saturate_to", DEFAULT_SATURATE_TO)
    if not _num(st) or st <= 0:
        errs.append("saturate_to must be a number > 0 (fraction of physical cores)")
    for hid, h in hosts.items():
        if isinstance(h, dict) and "clock_mhz" in h and (not _num(h["clock_mhz"]) or h["clock_mhz"] <= 0):
            errs.append(f"hosts.{hid}.clock_mhz must be a number > 0")
        # a list of strings, nothing more (t_ad5a855d): no name check. A reader never asks for an axis it does not
        # know, and a mistyped name leaves the real axis required, so only that host reads unmeasured and closes.
        if isinstance(h, dict) and not (isinstance(h.get("unmeasurable_axes", []), list)
                                        and all(isinstance(a, str) for a in h.get("unmeasurable_axes", []))):
            errs.append(f"hosts.{hid}.unmeasurable_axes must be a list of strings")
        if isinstance(h, dict) and "pressure_from" in h and (not isinstance(h["pressure_from"], str)
                                                             or h["pressure_from"] == hid or h["pressure_from"] not in hosts
                                                             or not isinstance(hosts[h["pressure_from"]], dict)
                                                             or "pressure_from" in hosts[h["pressure_from"]]):
            errs.append(f"hosts.{hid}.pressure_from must name another host in hosts that has its own sample")
        if isinstance(h, dict) and "power_policy" in h and h["power_policy"] not in POWER_POLICIES:
            errs.append(f"hosts.{hid}.power_policy must be one of {list(POWER_POLICIES)} when present")
        if isinstance(h, dict) and h.get("default_preference", "normal") not in ("normal", "low"):
            errs.append(f"hosts.{hid}.default_preference must be normal|low (never an ordinal)")
        if isinstance(h, dict) and "cpu_saturable" in h:
            # LEGACY (pre-R2c tables): still read for v1 so a tree that has the new code but not yet the new table
            # keeps today's decisions; the shipped table carries scorer_v1.cpu_saturable instead
            if not isinstance(h["cpu_saturable"], bool):
                errs.append(f"hosts.{hid}.cpu_saturable must be true|false")
            elif h["cpu_saturable"] and _num(h.get("cores_reserve")) and h["cores_reserve"] > 0:
                errs.append(f"hosts.{hid}: cpu_saturable with cores_reserve {h['cores_reserve']} > 0 (a saturable cpu axis keeps no cores back)")
    sc = doc.get("scorer", DEFAULT_SCORER)
    if sc not in SCORERS:
        errs.append(f"scorer must be one of {list(SCORERS)}, not {sc!r}")
    v1 = doc.get("scorer_v1", {})
    if not isinstance(v1, dict):
        errs.append("scorer_v1 must be an object")
    else:
        sat = v1.get("cpu_saturable", [])
        if not isinstance(sat, list) or not all(isinstance(x, str) for x in sat):
            errs.append("scorer_v1.cpu_saturable must be a list of host ids")
        else:
            for x in sat:
                h = hosts.get(x)
                if not isinstance(h, dict):
                    errs.append(f"scorer_v1.cpu_saturable names {x!r}, not a host in hosts")
                elif _num(h.get("cores_reserve")) and h["cores_reserve"] > 0:
                    errs.append(f"hosts.{x}: cpu_saturable with cores_reserve {h['cores_reserve']} > 0 (a saturable cpu axis keeps no cores back)")
    cw = doc.get("closed_when", {})
    if not isinstance(cw, dict) or any(not _num(v) or v < 0 for k, v in cw.items() if not k.startswith("_")):
        errs.append("closed_when must map axis -> number >= 0")
    elif isinstance(cw, dict):
        # an axis the Sample never carries would be a threshold nothing enforces (Prism 8479b28f): refuse it
        bad = [k for k in cw if not k.startswith("_") and k not in REQUIRED_AXES]
        if bad:
            errs.append(f"closed_when names unsupported axis(es) {bad}; supported: {list(REQUIRED_AXES)}")
    return errs


INVENTORY_REL = Path("var") / "placement" / "host-inventory.json"
# FWA R2a (t_e3f34b3e): HostSpec field -> discovered field. Discovered wins; the table is the fallback.
INVENTORY_FACTS = (("cores", "physical_cores"), ("threads", "threads"), ("clock_mhz", "max_mhz"))
_PRINTED_ONCE: set = set()


def _print_once(key, line: str) -> None:
    if key not in _PRINTED_ONCE:
        _PRINTED_ONCE.add(key)
        print(line, file=sys.stderr)


def inventory_path(table_path) -> Optional[Path]:
    """The discovered inventory beside the table: <root>/var/placement/host-inventory.json for <root>/config/<table>.
    PLACEMENT_INVENTORY overrides it (an empty value = no inventory)."""
    env = os.environ.get("PLACEMENT_INVENTORY")
    if env is not None:
        return Path(env) if env else None
    p = Path(table_path)
    return p.parent.parent / INVENTORY_REL if p.parent.name == "config" else None


def read_inventory(path) -> Dict[str, dict]:
    """{host: discovered facts} from host-inventory.json (placement-collector), {} when missing or unreadable."""
    if path is None:
        return {}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hosts = doc.get("hosts") if isinstance(doc, dict) else None
    return {h: r for h, r in hosts.items() if isinstance(r, dict)} if isinstance(hosts, dict) else {}


def _discovered(field: str, row: Mapping, h: Mapping):
    """One discovered value, or None. `count_cores: performance` (the Studio: macOS queues background QoS work on
    the E cores) makes `cores` the P-core count."""
    src = "p_cores" if field == "cores" and h.get("count_cores") == "performance" else dict(INVENTORY_FACTS)[field]
    v = row.get(src)
    if field == "clock_mhz":
        return float(v) if _num(v) and v > 0 else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else None


def host_facts(hid: str, h: Mapping, inv: Mapping) -> Dict[str, float]:
    """cores / threads / clock_mhz for one host: DISCOVERED first, the hand-typed table second (FWA R2a, Ace
    10-18 09:48). A discovered/table disagreement prints ONCE per process to stderr."""
    rows = [inv[n] for n in [hid, *(h.get("aliases") or [])] if isinstance(inv.get(n), dict)]
    # the NEWEST measured row wins (Prism P1 234ba6246f42): merged_inventory() keeps a stale canonical-id row
    # beside a fresh alias row after a collector rename; canonical first on a tie or with no `at`
    row = max(rows, key=lambda r: r["at"] if _num(r.get("at")) else float("-inf"), default=None)
    out = {}
    for field, _src in INVENTORY_FACTS:
        disc, typed = (_discovered(field, row, h) if row else None), h.get(field)
        if disc is not None and _num(typed) and float(disc) != float(typed):
            _print_once((hid, field, disc, typed), f"placement: {hid}.{field} discovered {disc:g} != table "
                                                   f"{typed:g}; discovered wins (var/placement/host-inventory.json)")
        val = disc if disc is not None else typed
        if val is not None:
            out[field] = val
    return out


def read_host_speed(path=None, *, root: Optional[Path] = None) -> Dict[str, dict]:
    """{host: {"st_speed": x, "mt_yield": y}} from var/placement/host-speed.json (R2b, t_ea50af1c), values > 0 only.

    Accepted shapes (the R2b contract is the first): ``{"hosts": {h: {"st_speed": 1.0, "mt_yield": 1.3, ...}}}``,
    or the same rows at top level; a value may also be ``{"value": x, ...}``. MISSING file = {} (every host on the
    seed); an unreadable / malformed file is ALSO {} plus one stderr line: speed only orders eligible hosts and the
    capacity seed is the ruled default, so a bad speed file must not close the fleet."""
    p = Path(path) if path is not None else fleet_root(root=root) / SPEED_REL
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"placement: {p}: unreadable ({type(exc).__name__}); every host on the speed seed\n")
        return {}
    rows = doc.get("hosts", doc) if isinstance(doc, dict) else None
    if not isinstance(rows, dict):
        return {}
    out: Dict[str, dict] = {}
    for hid, row in rows.items():
        if not isinstance(row, dict) or str(hid).startswith("_"):
            continue
        vals = {}
        for k in ("st_speed", "mt_yield"):
            v = row.get(k)
            v = v.get("value") if isinstance(v, dict) else v
            if _num(v) and v > 0:
                vals[k] = float(v)
        if vals:
            out[str(hid)] = vals
    return out


def active_scorer(doc_scorer=None) -> str:
    """PLACEMENT_SCORER (v1|v2) beats the table's ``scorer``; an unknown env value is refused, never guessed."""
    env = os.environ.get("PLACEMENT_SCORER", "").strip()
    if env:
        if env not in SCORERS:
            raise PlacementError(f"PLACEMENT_SCORER must be one of {list(SCORERS)}, not {env!r}")
        return env
    return doc_scorer or DEFAULT_SCORER


def load_hosts(path=None, inventory=None, *, speed_path=None, scorer: Optional[str] = None, root: Optional[Path] = None) -> HostTable:
    """The host table. Each host's cores / threads / clock come from ``inventory`` ({host: discovered row}; default
    read_inventory(inventory_path(table))) first, the table second (FWA R2a). A host with neither, or whose discovered
    facts contradict its policy (threads < cores, cores_reserve >= cores), is LEFT OUT (closed: never picked), printed
    once; it never fails the whole table. ``scorer`` (v1|v2) beats PLACEMENT_SCORER and the table's key (the replay
    compares both)."""
    p = Path(path) if path is not None else fleet_root(root=root) / TABLE_REL
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlacementError(f"{p}: missing") from exc
    except (OSError, ValueError) as exc:
        raise PlacementError(f"{p}: unreadable: {type(exc).__name__}: {exc}") from exc
    errs = validate(doc)
    if errs:
        raise PlacementError(f"{p}: {len(errs)} error(s): " + "; ".join(errs))
    if scorer is not None and scorer not in SCORERS:
        raise PlacementError(f"scorer must be one of {list(SCORERS)}, not {scorer!r}")
    scorer = scorer or active_scorer(doc.get("scorer"))
    saturable = set((doc.get("scorer_v1") or {}).get("cpu_saturable", [])) \
        | {hid for hid, h in doc["hosts"].items() if h.get("cpu_saturable") is True}
    # a pressure_from guest is judged on its host's sample, so it inherits that host's v1 cpu_saturable (t_59943ed5)
    saturable |= {hid for hid, h in doc["hosts"].items() if h.get("pressure_from") in saturable}
    # the speed file sits next to the table's state root (var/placement/), so a test table in tmp_path never reads
    # the live fleet's measurements
    speed = read_host_speed(speed_path if speed_path is not None else p.parent.parent / SPEED_REL, root=root) \
        if scorer == "v2" else {}
    inv = read_inventory(inventory_path(p)) if inventory is None else inventory
    hosts = {}
    for hid, h in doc["hosts"].items():
        f = host_facts(hid, h, inv)
        pressure_from = h.get("pressure_from")
        vm_cpus = None
        if pressure_from in doc["hosts"]:
            # the guest's own size: its own facts (a typed or discovered row of its own) when it has them, else the
            # VM size the collector discovered for it (vm_cpus, OrbStack machine config); never its host's
            own = [inv[n] for n in [hid, *(h.get("aliases") or [])] if isinstance(inv.get(n), dict)]
            vm = next((r["vm_cpus"] for r in own if isinstance(r.get("vm_cpus"), int)
                       and not isinstance(r.get("vm_cpus"), bool) and r["vm_cpus"] >= 1), None)
            # a measured vm_cpus bounds any thread fact the row carries (one inherited from the old hardware mirror
            # reads the host's 32): the VM's own measurement is authoritative (Prism c6575b98633e)
            own_thr = int(f["threads"]) if "cores" in f and "threads" in f else None
            vm_cpus = min(own_thr, vm) if own_thr is not None and vm is not None else (vm if vm is not None else own_thr)
        if pressure_from in doc["hosts"]:
            # EVERY pressure_from guest is sized from its host's DISCOVERED facts, resolved with the host's own row
            # (count_cores): it is judged on that host's sample, so its capacity is that host's too, like
            # sample_for() and the cpu_saturable inheritance. Its own facts only set vm_cpus above: a 16-vCPU VM on
            # an 8-thread box is never scored as 16 threads of capacity (Prism 8060dffe2210, t_b9ed3796).
            # Never a typed table fact (R2a); with no discovered host row the guest is left out like any host.
            own_f, f = f, host_facts(pressure_from, doc["hosts"][pressure_from], inv)
        else:
            own_f = f
        why = None
        if "cores" not in f or "threads" not in f:
            why = "cores/threads neither discovered (var/placement/host-inventory.json) nor in the table"
        elif f["threads"] < f["cores"]:
            why = f"threads {f['threads']} < cores {f['cores']}"
        elif "cores" in own_f and "threads" in own_f and own_f["threads"] < own_f["cores"]:
            # a guest's own row that contradicts itself is still bad data: closed (Prism c2f0087775e6)
            why = f"threads {own_f['threads']} < cores {own_f['cores']}"
        elif float(h["cores_reserve"]) >= f["cores"]:
            why = f"cores_reserve {h['cores_reserve']:g} >= cores {f['cores']}"
        if why:
            _print_once((hid, why), f"placement: host {hid} left out of the table (closed): {why}")
            continue
        cores, threads = int(f["cores"]), int(f["threads"])
        meas = speed.get(hid) or {}
        hosts[hid] = HostSpec(
            name=hid, cores=cores, threads=threads, mem_total_gb=float(h["mem_total_gb"]),
            mem_reserve_gb=float(h["mem_reserve_gb"]), cores_reserve=float(h["cores_reserve"]),
            ceilings={c: ceiling_of(v) for c, v in h["consumers"].items() if ceiling_of(v) is not None},
            aliases=tuple(h.get("aliases", [])), clock_mhz=float(f.get("clock_mhz", 0.0)),
            default_preference=str(h.get("default_preference", "normal")),
            unmeasurable_axes=tuple(h.get("unmeasurable_axes", [])),
            cpu_saturable=hid in saturable, scorer=scorer,
            mt_yield=meas.get("mt_yield", SEED_MT_YIELD_SMT2 if threads == 2 * cores else 1.0),
            st_speed=meas.get("st_speed", 1.0),
            speed_src="measured" if "mt_yield" in meas else "seed",
            st_speed_src="measured" if "st_speed" in meas else "seed",
            pressure_from=h.get("pressure_from"), vm_cpus=vm_cpus, power_policy=h.get("power_policy"),
            enrolled=frozenset(c for c, v in h["consumers"].items()
                               if isinstance(v, dict) and v.get("eligible") is True),
            ramp_cpus=tuple((job_class_for(c), float(v["ramp_cpus"])) for c, v in h["consumers"].items()
                            if isinstance(v, dict) and _num(v.get("ramp_cpus")) and v["ramp_cpus"] >= 0))
    for hid, s in list(hosts.items()):
        # a guest runs on its source's power: the source's power_policy binds it too, so another venue on the same
        # machine never bypasses the AC-only gate (Prism ed3ec771df33)
        src = doc["hosts"].get(s.pressure_from) if s.pressure_from else None
        if isinstance(src, dict):
            # A pressure_from guest shares its parent's unsupported-axis contract, even when its own newer
            # Linux sample wins. This declares platform limits; it neither fabricates PSI nor changes freshness.
            hosts[hid] = dataclasses.replace(
                s, power_policy=s.power_policy if s.power_policy is not None else src.get("power_policy"),
                unmeasurable_axes=tuple(dict.fromkeys((*s.unmeasurable_axes, *src.get("unmeasurable_axes", [])))))
    for hid in [g for g, s in hosts.items() if s.pressure_from and s.pressure_from not in hosts]:
        # the guest is judged on its host's sample and in its host's resource domain: with that host closed it would
        # be its own root, blind to every lease and reservation on the box (Prism 54f435021b7d). Closed with it.
        _print_once((hid, "pressure_from"), f"placement: host {hid} left out of the table (closed): its pressure_from "
                                            f"host {hosts[hid].pressure_from} is left out")
        del hosts[hid]
    fps = {c: Footprint.of(fp) for c, fp in doc["footprints"].items() if not c.startswith("_")}
    pfps = {c: {pn: Footprint.of(pf) for pn, pf in fp["profiles"].items() if not pn.startswith("_")}
            for c, fp in doc["footprints"].items() if not c.startswith("_") and isinstance(fp.get("profiles"), dict)}
    cw = dict(DEFAULT_CLOSED_WHEN)
    cw.update({k: float(v) for k, v in (doc.get("closed_when") or {}).items() if not k.startswith("_")})
    declared = {hid: (h.get("pressure_from"), tuple(h.get("aliases", []))) for hid, h in doc["hosts"].items()}
    return HostTable(hosts=hosts, footprints=fps, closed_when=cw, path=p, default_first=doc.get("default_first"),
                     saturate_to=float(doc.get("saturate_to", DEFAULT_SATURATE_TO)), profile_footprints=pfps,
                     scorer=scorer, declared=declared,
                     shared_budgets={hid: h["shared_budget"] for hid, h in doc["hosts"].items()
                                     if "shared_budget" in h})


class ExclusionsError(PlacementError):
    """state/fleet-pause/kinds.json exists but cannot be read or parsed: admission fails CLOSED (Prism P1 4d6e2458).
    B5-P3 (t_00664b58): var/placement/exclusions.json is no longer read; a pause fence is an fwa hold."""


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
    scratch_free_gb: Optional[float] = None   # gates footprint.scratch_gb when measured
    d_threads: Optional[float] = None         # threads in D state at sample time (Linux sampler); None = not counted
    # scorer v2 inputs (Linux sampler, FWA R2a/R2c; absent = v2 falls back to ``load`` and says so on the line)
    runnable_ewma: Optional[float] = None     # EWMA (~60 s) of procs_running - 1: R-state only, so net of D-state
    procs_running: Optional[float] = None     # instantaneous /proc/stat procs_running (used when no EWMA)
    psi_cpu_some60: Optional[float] = None    # PSI cpu some avg60 (v2 closes at >= 25 when measured)

    @property
    def occupancy(self) -> tuple:
        """(cores of runnable work, source) for scorer v2: the sampler's EWMA of procs_running - 1, else the
        instantaneous procs_running - 1, else ``load`` (load1/load5 net of D-state; Mac and pre-R2a rows)."""
        if _num(self.runnable_ewma):
            return max(0.0, float(self.runnable_ewma)), "runnable_ewma"
        if _num(self.procs_running):
            return max(0.0, float(self.procs_running) - 1.0), "procs_running"
        return self.load, "load"
    # utilization axes (admission.admit, mode utilization): host-pressure.py / fleet-host busy --json row fields
    cpu_busy: Optional[float] = None          # 0..1 busy fraction of all CPUs (counter delta; never load1)
    ncpu: Optional[float] = None
    mem_total_gb: Optional[float] = None
    psi_io_some10: Optional[float] = None
    os: Optional[str] = None
    procs: Optional[float] = None
    maxproc: Optional[float] = None
    gpu_free_mib: Optional[float] = None
    slo_breach: Optional[bool] = None         # a latency-SLO guard (ACE-AI voice hub): True = saturated
    # t_58366dea: the darwin sampler's ``power`` block; read only for a ``power_policy`` host (power_refusal)
    power_source: Optional[str] = None        # "ac" | "battery" | "ups"; None = unmeasured
    sleep_disabled: Optional[bool] = None     # pmset SleepDisabled (clamshell stays awake); None = unmeasured

    @property
    def load(self) -> Optional[float]:
        """Runnable load: load1/load5 net of the D-state threads, which hold no CPU (t_e47d8d9a: 65 NFS-wedged
        threads read load1 65.9 on a 97 %-idle 32-thread box). Each term clamps at 0."""
        d = float(self.d_threads) if _num(self.d_threads) and self.d_threads > 0 else 0.0
        vals = [max(0.0, float(v) - d) for v in (self.load1, self.load5) if _num(v)]
        return max(vals) if vals else None


def sample_for(table: "HostTable", samples: Mapping[str, Optional[Sample]], hid: str) -> Optional[Sample]:
    """``hid``'s sample (any of its names). For a ``pressure_from`` guest (t_59943ed5: studio-arm has no sampler and
    must be refused by the whole Studio's load like any worker) the source's sample, unless the guest's own (the
    mirror) is at least as new: a mirror is never fresher than the sample it copied, so an older mirror left by a
    failed write never masks newer source pressure (Prism 901e6ec92ac9 / f50e486852d3)."""
    own = next((v for k, v in samples.items() if table.resolve(k) == hid and v is not None), None)
    spec = table.hosts.get(hid)
    src = table.resolve(spec.pressure_from) if spec is not None and spec.pressure_from else None
    if not src or src == hid:
        return own
    up = sample_for(table, samples, src)
    if up is None:
        return own
    if own is not None and _num(own.at) and _num(up.at) and float(own.at) >= float(up.at):
        return own
    return up


def domain_of(table: "HostTable", hid: str) -> set:
    """The resource domain ``hid`` is judged in: its root (``pressure_from`` or itself) and every guest of that root.
    A guest borrows its host's MEASURED headroom, so a lease on any member is in-flight load on the same box
    (Prism cb27fe636cb4, t_59943ed5). Membership is read from the table's DECLARED rows (``table.declared``), so a
    member load_hosts() left out (closed: discovery missing or contradicting policy) stays in the domain: closing a
    venue never erases what was already promised on the box (Prism 7e9f3e2ed307 / c2f0087775e6 / 03f3bf40fe6b)."""
    rows = {h: s.pressure_from for h, s in table.hosts.items()}
    rows.update({h: src for h, (src, _a) in table.declared.items() if h not in rows})

    def src(h):          # the validated source id even when the table has no row for it (Prism 54f435021b7d)
        s = rows.get(h)
        return (table.declared_id(s) or s) if s else None
    root = src(hid) or hid
    return {root} | {h for h in rows if src(h) == root}


def domain_resolve(table: "HostTable", hid: str) -> Callable[[str], Optional[str]]:
    """A name resolver (admission_demand.read_demand's ``resolve``) that names ``hid`` for every member of its resource
    domain: queued demand restricted to a sibling is load waiting for the same box (Prism d26743a7e1e6). A demand doc
    is one Demand whatever members it lists, so it is counted once. Outside a domain it is table.resolve."""
    dom = domain_of(table, hid)

    def resolve(name):
        got = table.declared_id(name) or (name if name in dom else None)
        return hid if got in dom else table.resolve(name)
    return resolve


def samples_from_pressure(doc: Mapping, *, now: float, stale_after_s: Optional[float] = None) -> Dict[str, Sample]:
    """Samples from a fleet-pressure.json document (placement-collector output). Every host row is a sample,
    the Studio included (Ace 2026-10-12 18:1x: low default preference, never excluded); whether a host is a
    candidate for a consumer is the table's consumer rows, not the sampler's `targets` flag."""
    now = _now(now)
    # an explicit 0 is "expire immediately", never "absent" (Prism 4102b9d1b28a): default only on missing / None
    stale = stale_after_s if stale_after_s is not None else doc.get("stale_after_s")
    if stale is None:
        stale = DEFAULT_STALE_AFTER_S
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
            scratch_free_gb=row.get("scratch_free_gb") if _num(row.get("scratch_free_gb")) else None,
            d_threads=row.get("d_threads") if _num(row.get("d_threads")) else None,
            runnable_ewma=row.get("runnable_ewma") if _num(row.get("runnable_ewma")) else None,
            procs_running=row.get("procs_running") if _num(row.get("procs_running")) else None,
            psi_cpu_some60=row.get("psi_cpu_some60") if _num(row.get("psi_cpu_some60")) else None,
            **{k: (row.get(k) if _num(row.get(k)) else None) for k in _UTIL_NUM_AXES},
            os=row.get("os") if isinstance(row.get("os"), str) else None,
            slo_breach=row.get("slo_breach") if isinstance(row.get("slo_breach"), bool) else None,
            **_power_fields(row.get("power")),
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


_UTIL_NUM_AXES = ("cpu_busy", "ncpu", "mem_total_gb", "psi_io_some10", "procs", "maxproc", "gpu_free_mib")


def _power_fields(p) -> dict:
    """Sample kwargs from a sampler row's ``power`` block ({source, sleep_disabled}); anything malformed = None."""
    p = p if isinstance(p, dict) else {}
    src = p.get("source")
    sd = p.get("sleep_disabled")
    return {"power_source": src if src in ("ac", "battery", "ups") else None,
            "sleep_disabled": sd if isinstance(sd, bool) else None}


def power_refusal(spec: HostSpec, sample: Sample) -> Optional[str]:
    """An intermittent host's power gate (t_58366dea). ``power_policy: ac_only`` -> closed unless the sample says AC
    and sleep disabled; an unmeasured value is closed, never assumed. headroom() and may_start() both call this after
    the freshness check, so a laptop that sleeps or leaves the network goes stale and closes on that path instead."""
    if spec.power_policy != "ac_only":
        return None
    if sample.power_source != "ac":
        return "closed:power " + (sample.power_source or "unmeasured") + " (power_policy ac_only)"
    if sample.sleep_disabled is not True:
        return "closed:sleep " + ("enabled" if sample.sleep_disabled is False else "unmeasured") \
            + " (power_policy ac_only needs SleepDisabled 1)"
    return None


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
    scratch_gb: Optional[float] = None   # scratch_free - reserved - footprint, when the sample measured scratch
    # scorer v2 only (None under v1, so v1 lines and rows are unchanged byte for byte)
    occupancy: Optional[float] = None    # cores of runnable work (Sample.occupancy)
    occupancy_src: str = ""
    capacity: Optional[float] = None     # cores x mt_yield - cores_reserve
    psi_cpu_some10: Optional[float] = None
    psi_cpu_some60: Optional[float] = None
    fraction: Optional[float] = None     # binding headroom fraction before speed
    alpha: Optional[float] = None        # the speed exponent choose() applied
    need: Optional["admission.Estimate"] = None   # utilization: this job's estimate on THIS host (reserved on a pick)
    cpu_busy: Optional[float] = None

    def line(self, spec: HostSpec) -> str:
        """`<host> load N / Cc.Tt, avail X GB, headroom Y GB / Z cores (binding axis), score S` or the refusal.
        Scorer v2: `<host> occupancy O / K (Cc.Tt, yield y), load N / Cc.Tt, PSI a/b, st_speed s, avail ..., headroom ...`."""
        if spec.scorer == "v2":
            return self._line_v2(spec)
        load = "?" if self.load is None else f"{self.load:g}"
        avail = "?" if self.mem_available_gb is None else f"{self.mem_available_gb:.1f}"
        res = f", reserved {self.reserved_gb:g} GB / {self.reserved_threads:g} thr" if (self.reserved_gb or self.reserved_threads) else ""
        head = ("" if self.mem_gb is None else
                f"{res}, headroom {self.mem_gb:.1f} GB / {self.cores:.1f} cores ({self.binding or '-'})")
        tail = f", score {self.score:.3f}" if self.eligible else f", {self.reason}"
        return f"{self.host} load {load} / {spec.cores}c.{spec.threads}t, avail {avail} GB{head}{tail}"

    def _line_v2(self, spec: HostSpec) -> str:
        def g(v, fmt="{:g}"):
            return "?" if v is None else fmt.format(v)
        cap = spec.cpu_capacity
        occ = g(None if self.occupancy is None else round(self.occupancy, 1))
        src = f" {self.occupancy_src}" if self.occupancy_src and self.occupancy_src != "runnable_ewma" else ""
        load = g(self.load)
        psi = f"{g(self.psi_cpu_some10)}/{g(self.psi_cpu_some60)}"
        avail = g(self.mem_available_gb, "{:.1f}")
        res = f", reserved {self.reserved_gb:g} GB / {self.reserved_threads:g} thr" if (self.reserved_gb or self.reserved_threads) else ""
        # a v2 closure (PSI avg60, unmeasured occupancy) returns after mem_gb is set but before cores (Prism 5c45efe6)
        head = ("" if self.mem_gb is None else
                f"{res}, headroom {self.mem_gb:.1f} GB / {g(self.cores, '{:.1f}')} cores ({self.binding or '-'})")
        tail = (f", score {self.score:.3f}" + (f" (frac {self.fraction:.3f} x st_speed^{self.alpha:g})"
                                              if self.fraction is not None and self.alpha is not None else "")
                if self.eligible else f", {self.reason}")
        return (f"{self.host} occupancy {occ}{src} / {cap:g} ({spec.cores}c.{spec.threads}t, yield {spec.mt_yield:g}"
                f"{'' if spec.speed_src == 'measured' else ' seed'}), load {load} / {spec.cores}c.{spec.threads}t, PSI {psi}, st_speed "
                f"{spec.st_speed:g}{'' if spec.st_speed_src == 'measured' else ' seed'}, avail {avail} GB{head}{tail}")


def policy_refusal(spec: HostSpec, consumer: str, excluded: Optional[str], admitted: float = 0.0) -> Optional[str]:
    """The OPERATOR controls every admission path applies before any measurement (t_e5124426 r3, Prism 26a9b46c6a7b):
    a live exclusion row for the host, a ``pressure_from`` guest's consumer it is not enrolled for (default-deny:
    only ``consumers.<c>.eligible: true``), ``consumers.<c>.ceiling`` <= 0 (the consumer is not eligible there), or a
    positive ceiling already reached by ``admitted`` live admissions of this consumer (Prism 696cce63, f88f81bb2e24).
    -> the refusal reason, or None. headroom() (so choose()) and may_start() both call this; never copy the checks."""
    if excluded:
        return f"excluded:{excluded}"
    if spec.pressure_from and consumer not in spec.enrolled:
        return "closed:guest-not-enrolled"
    ceiling = spec.ceiling_for(consumer)
    if ceiling is not None and ceiling <= 0:
        return f"excluded:{consumer} not eligible"
    if ceiling is not None and admitted >= ceiling:
        return f"at_ceiling:{consumer} {admitted:g}/{ceiling} in use"
    return None


def guest_vm_refusal(spec: HostSpec, ctx: "AdmitContext", need_cpus: float,
                     reservations: Optional[Mapping[str, "Footprint"]] = None) -> Optional[str]:
    """A ``pressure_from`` guest's own-size cap (Apollo 10-31 08:4x, t_7361659c): the work admitted ON the guest (every
    live lease and caller reservation booked under any of its names, settled or not: a running job holds its vCPUs)
    plus this job never exceeds its VM's ``vm_cpus``, whatever headroom the shared domain shows. ``ctx`` is the
    UNFOLDED context (a folded one attributes the host's own leases to the guest). ``reservations`` ({host: Footprint},
    UNAGED: admit() passes the ledger's live rows in full) replaces ``ctx.reservations`` for this check, whose threads
    the scorer ages out once load1 shows them (Prism f01d48b6d5c4). Unmeasured size = closed.
    -> the refusal reason, or None (not a guest, or it fits)."""
    if not spec.pressure_from:
        return None
    if spec.vm_cpus is None:
        return "closed:guest vm size unmeasured (var/placement/host-inventory.json vm_cpus)"
    names = {spec.name.lower(), *(a.lower() for a in spec.aliases)}
    resv = ctx.reservations if reservations is None else tuple(reservations.items())
    held = sum(float(le.need.cpus) for le in ctx.leases if str(le.host).lower() in names) \
        + sum(float(f.threads) for h, f in resv if str(h).lower() in names)
    if held + float(need_cpus) > spec.vm_cpus:
        return f"no_headroom:vm_cpus {held:g} admitted + {float(need_cpus):g} > {spec.vm_cpus} vCPU on the guest"
    return None


def caller_exclusions(table: "HostTable", *, extra: Optional[Mapping[str, Optional[str]]] = None) -> Dict[str, str]:
    """{host id: reason} = a caller's ``extra`` map, keyed by table id; an empty reason still excludes (Prism 95afdab6).
    B5-P3 (t_00664b58, spec §9 P3): the operator rows of var/placement/exclusions.json are no longer read; an operator
    exclusion is an fwa hold (`fleet-work pause`), read by fwa_hold below."""
    excl: Dict[str, str] = {}
    for k, v in (extra or {}).items():
        excl.setdefault(table.resolve(k) or k, (str(v) if v is not None else "") or "excluded")
    return excl


def fwa_fence(host: str, *, now: float, root=None, memo=None) -> Optional[str]:
    """B4c (t_fec4e1a6, spec §14.1 a): the why of a live fleet-pause fence hold on ``host``, else None. Consumer-blind
    (F5: only a fence record, owner prefix `fleet-pause:` and drive false, gets this all-consumer arm; every other hold
    keeps its lane semantics in fwa_hold) and independent of FWA_READERS (F6). A copy installed without fwa (the
    remote /usr/local/lib/placement) has no hold files to read: None there. A hold directory that cannot be listed
    fails CLOSED, like the exclusions file, and so does an unreadable record in it (whatever its owner)."""
    try:
        from . import fwa  # noqa: PLC0415  (late: fwa-less remote copies)
    except ImportError:
        try:
            import fwa  # noqa: PLC0415
        except ImportError:
            return None
    r = fleet_root(root=root)
    try:
        key = fwa.canon(str(host), r)
        files = memo.get(key, r, lambda: fwa.read_dir(key, r)) if memo is not None else fwa.read_dir(key, r)
    except fwa.FwaError:
        return None
    except OSError as exc:
        return f"fwa:unreadable hold directory for {host}: {exc}"
    for h in files:
        if h.live(now) and fwa.is_fence(h.doc):
            return h.why()
        # c-9bbafb28c8e8f875: cannot prove it is not a fence (bounded at mtime + 24 h, unbounded when a stat failure's
        # first-failure marker cannot persist: fwa gate_read / covered_lanes hold on the same clause)
        if h.unreadable_holding(now) or (h.stat_failed and not h.marker_persisted):
            return f"fwa:unreadable hold {h.id} on {host}: {h.error}"
    return None


def fwa_hold(host: str, consumer: str, *, now: float, root=None, memo=None) -> Optional[str]:
    """FWA §5.3 (t_d759c539): the why of a live fwa hold on ``host`` covering ``consumer``'s lane, else None. The lane
    is the consumer's work kind (CONSUMER_KIND: athena is a kanban worker), else the consumer name itself (§5.1: the
    may_start consumer names ARE fwa lanes). Fails CLOSED on an unreadable hold file, like the exclusions file.
    Kill switch FWA_READERS=0 (scripts/lib/fwa_readers.py): None, the legacy reads alone decide. A copy installed
    WITHOUT fwa (fleet/placement/install.sh -> /usr/local/lib/placement on ace-ai/ace-media) is a REMOTE consumer
    (§5.3: it reads its projections, never a Studio hold file): None there too.
    B4c (t_fec4e1a6, spec §14.1 F5/F6): a fleet-pause FENCE hold closes the host to every consumer name, read first
    and independent of FWA_READERS (the exclusions row it replaces ignored the switch too)."""
    fence = fwa_fence(host, now=now, root=root, memo=memo)
    if fence:
        return fence
    try:
        from . import fwa_readers
    except ImportError:
        try:
            import fwa_readers
        except ImportError:
            return None
    return fwa_readers.holding(host, CONSUMER_KIND.get(consumer, consumer), now=now,
                               root=fleet_root(root=root), on_unreadable="hold", memo=memo)


def _fwa_memo():
    """A per-call fwa.Memo (I4: dropped with the call), None when the kill switch is off (fwa never imported)."""
    try:
        from . import fwa_readers
    except ImportError:
        try:
            import fwa_readers
        except ImportError:
            return None
    return fwa_readers.memo()


def kind_exclusions(table: "HostTable", consumer: str, path=None, *, now: float, root: Optional[Path] = None) -> Dict[str, str]:
    """{host id: "kind_paused:<kind>@<scope> <id>"} for every live row of state/fleet-pause/kinds.json that pauses
    ``consumer``'s work kind (CONSUMER_KIND), on its one host or, for scope ``all``, on every table host. A consumer
    with no kind, or a MISSING file = {}. An unreadable or malformed file raises ExclusionsError (callers fail CLOSED,
    like the exclusions file): a pause nobody can read is still a pause."""
    kind = CONSUMER_KIND.get(consumer)
    if kind is None:
        return {}
    now = _now(now)
    p = Path(path) if path is not None else fleet_root(root=root) / KINDS_REL
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ExclusionsError(f"{p}: unreadable: {exc}") from exc
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, dict):
        raise ExclusionsError(f"{p}: \"rows\" must be an object")
    out: Dict[str, str] = {}
    for key, row in sorted(rows.items()):
        if (not isinstance(row, dict) or not isinstance(row.get("kind"), str) or not row["kind"].strip()
                or not isinstance(row.get("scope"), str) or not row["scope"].strip()
                or not _num(row.get("until"))):
            raise ExclusionsError(f"{p}: row {key!r} lacks kind/scope/until")
        if row.get("kind") != kind or float(row["until"]) <= now:
            continue
        why = f"kind_paused:{kind}@{row['scope']} {row.get('id') or '?'}"
        hosts = list(table.hosts) if row["scope"] == "all" else [table.resolve(row["scope"]) or row["scope"]]
        for hid in hosts:
            out.setdefault(hid, why)
    return out


def headroom(spec: HostSpec, sample: Optional[Sample], fp: Footprint, *, consumer: str,
             closed_when: Mapping[str, float], excluded: Optional[str] = None,
             reserved: Optional[Footprint] = None, require_axes: Iterable[str] = REQUIRED_AXES,
             admitted: float = 0.0, now: Optional[float] = None, stale_after_s: Optional[float] = None,
             mode: str = "legacy", need: Optional["admission.Estimate"] = None,
             leases: Iterable["admission.Lease"] = ()) -> Headroom:
    """``reserved`` = the sum of LIVE reservations on this host (promised, not yet in the sample).
    Its memory is subtracted in full; its threads should be only the reservations younger than LOAD1_WINDOW_S
    (``live_reservations(..., cpu_seen_after_s=LOAD1_WINDOW_S)``), because an older one is in load1 already.
    A ``cpu_saturable`` host is never refused on negative cpu headroom: the cpu fraction stays negative as a score
    PENALTY (it ranks below every host with cpu room), binding ``cpu-saturated``; memory and scratch still refuse.
    ``admitted`` = live reservations of THIS consumer on this host; a positive ``consumers.<c>.ceiling`` caps it.
    ``require_axes``: every named Sample field must be a finite number, else the host is closed (unmeasured).
    ``stale_after_s`` (with ``now``): re-check the sample's age at decision time; a sample marked fresh when it
    was read but older than this now is CLOSED (Prism baba105e: admit() can wait on the ledger lock).
    ``mode`` "utilization": after the exclusion / ceiling / freshness checks the host is admitted by admission.admit()
    over the sample's utilization axes, ``need`` (the job's estimate on this host) and ``leases`` (live reservations the
    sample cannot show yet); cores_reserve, load1 and closed_when are not read (t_da6f84f7)."""
    if now is not None:
        now = _now(now)
    stale_after_s = _stale_after(stale_after_s)
    h = Headroom(host=spec.name, eligible=False, reason="")
    refused = policy_refusal(spec, consumer, excluded, admitted)
    if refused:
        h.reason = refused
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
    refused = power_refusal(spec, sample)
    if refused:
        h.reason = refused
        return h
    if mode == "utilization":
        return _headroom_utilization(h, spec, sample, fp, need=need, leases=leases, reserved=reserved, now=now)
    if h.load is None or h.mem_available_gb is None:
        h.reason = "closed:unmeasured " + ("load" if h.load is None else "memory")
        return h
    if spec.scorer == "v2":
        # the denominators print on every v2 line, a closed host's included (doctrine rule 2)
        h.psi_cpu_some10, h.psi_cpu_some60 = sample.psi_cpu_some10, sample.psi_cpu_some60
        h.occupancy, h.occupancy_src = sample.occupancy
        h.capacity = spec.cpu_capacity
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
    if spec.scorer == "v2":
        return _headroom_v2(h, spec, sample, fp, reserved)
    h.cores = spec.cores - spec.cores_reserve - h.load - r_thr - fp.threads
    if fp.scratch_gb > 0 and _num(sample.scratch_free_gb):
        h.scratch_gb = float(sample.scratch_free_gb) - (reserved.scratch_gb if reserved else 0.0) - fp.scratch_gb
    mem_frac = h.mem_gb / spec.mem_total_gb
    cpu_frac = h.cores / spec.cores
    h.score, h.binding = (mem_frac, "mem") if mem_frac <= cpu_frac else (cpu_frac, "cpu")
    if h.binding == "cpu" and h.cores < 0 and spec.cpu_saturable:
        # clamped at -1 (a full box of oversubscription): the penalty orders it below every host with room, and a
        # load spike cannot push it below a host that is merely thinner on memory
        h.score, h.binding = max(-1.0, cpu_frac), "cpu-saturated"
    if h.mem_gb < 0:
        h.reason = f"no_headroom:mem {h.mem_gb:.1f} GB"
        return h
    if h.cores < 0 and not spec.cpu_saturable:
        h.reason = f"no_headroom:cpu {h.cores:.1f} cores"
        return h
    if h.scratch_gb is not None and h.scratch_gb < 0:
        h.reason = f"no_headroom:scratch {h.scratch_gb:.1f} GB"
        return h
    h.eligible = True
    return h


def _headroom_v2(h: Headroom, spec: HostSpec, sample: Sample, fp: Footprint,
                 reserved: Optional[Footprint]) -> Headroom:
    """Scorer v2 cpu axis (R2c). Capacity = cores x mt_yield - cores_reserve; occupancy = runnable work
    (Sample.occupancy: EWMA of procs_running - 1, net of D-state by construction; else load net of D-state);
    CLOSED on CPU stall: PSI cpu some avg10 >= 40 (closed_when) or avg60 >= 25 (load itself never closes a host). A negative
    cpu headroom REFUSES (headroom is the hard gate, 10-12 17:08): with SMT capacity and runnable-only occupancy the
    axis no longer reads a 59 %-busy 256-thread box as 'over', which is what cpu_saturable papered over. The score
    is the binding fraction; choose() multiplies it by st_speed^alpha among the eligible."""
    occ = h.occupancy
    p60 = sample.psi_cpu_some60       # avg10 >= 40 already closed the host above (closed_when)
    if _num(p60) and float(p60) >= V2_PSI_CPU_SOME60:
        h.reason = f"closed:psi_cpu_some60 {p60:g} >= {V2_PSI_CPU_SOME60:g}"
        return h
    if occ is None:
        h.reason = "closed:unmeasured occupancy"
        return h
    r_thr = reserved.threads if reserved else 0.0
    h.cores = h.capacity - occ - r_thr - fp.threads
    if fp.scratch_gb > 0 and _num(sample.scratch_free_gb):
        h.scratch_gb = float(sample.scratch_free_gb) - (reserved.scratch_gb if reserved else 0.0) - fp.scratch_gb
    mem_frac = h.mem_gb / spec.mem_total_gb
    cpu_frac = h.cores / h.capacity if h.capacity > 0 else float("-inf")
    h.score, h.binding = (mem_frac, "mem") if mem_frac <= cpu_frac else (cpu_frac, "cpu")
    h.fraction = h.score
    if h.mem_gb < 0:
        h.reason = f"no_headroom:mem {h.mem_gb:.1f} GB"
        return h
    if h.cores < 0:
        h.reason = f"no_headroom:cpu {h.cores:.1f} cores"
        return h
    if h.scratch_gb is not None and h.scratch_gb < 0:
        h.reason = f"no_headroom:scratch {h.scratch_gb:.1f} GB"
        return h
    h.eligible = True
    return h


def speed_alpha(job: Optional["Job"]) -> float:
    """D2: alpha 1 for priority <= 1 or core_class fast, 0 for core_class wide (batch), 0.5 otherwise."""
    if job is not None and job.core_class == "wide":
        return ALPHA_WIDE
    if job is not None and (job.core_class == "fast" or (job.priority is not None and job.priority <= 1)):
        return ALPHA_FAST
    return ALPHA_DEFAULT


def _sample_row(spec: HostSpec, sample: Sample) -> dict:
    row = {k: getattr(sample, k) for k in ("at", "os", "ncpu", "cpu_busy", "mem_available_gb", "mem_total_gb",
                                            "psi_cpu_some10", "psi_cpu_some60", "psi_mem_some10", "psi_io_some10",
                                            "procs", "maxproc", "gpu_free_mib", "slo_breach")}
    row["host"] = spec.name
    if row["mem_total_gb"] is None:
        row["mem_total_gb"] = spec.mem_total_gb
    return row


def _limits(spec: HostSpec) -> "admission.Limits":
    # cpu_saturable is scorer v1 ONLY (fleet-hosts.json scorer_v1._doc: "R2c retires it"): under v2 a Linux host's
    # PSI cpu avg10 >= 40 / avg60 >= 25 and a saturated cpu axis REFUSE (FWA F1, t_d456e9cd). scorer v1 restores it.
    return admission.Limits(mem_reserve_gb=spec.mem_reserve_gb,
                            cpu_saturable=spec.cpu_saturable and spec.scorer == "v1", r2c=spec.scorer == "v2",
                            unmeasurable=tuple(a for a in spec.unmeasurable_axes if a.startswith("psi_")),
                            psi_cpu_busy_floor=admission.PSI_CPU_BUSY_FLOOR, ramp_cpus=spec.ramp_cpus)


def need_of(fp: Footprint) -> "admission.Estimate":
    """A declared footprint as an admission estimate (threads = CPUs kept busy): the prior, or a card's own line."""
    return admission.Estimate(cpus=fp.threads, ram_gb=fp.ram_gb, gpu_mib=fp.gpu_mb, source="declared")


@dataclasses.dataclass(frozen=True)
class AdmitContext:
    """What utilization admission reads beyond the sample (t_f64c6d34 r2): the job's class, the class-usage history
    its estimate comes from, and the leases (ledger rows + held-back reserve) the sample cannot show yet. choose() and
    band() both build it with admit_context(), so the published band is the admitter's verdict, never a parallel one."""
    job_class: str
    seed: "admission.Estimate"
    history: tuple
    leases: tuple
    scratch: tuple = ()     # ((host, GB), ...): the ledger's live scratch, which admit() subtracts (Lease has none)
    reservations: tuple = ()    # ((host, Footprint), ...): a caller's booked reservations (choose()'s ``reservations``)

    # every field above that names a host: for_host() folds each of them over the resource domain, and
    # test_every_host_attributed_context_kind_is_folded_over_the_domain fails on a new one that is not listed here
    HOST_ATTRIBUTED = ("leases", "scratch", "reservations")

    def for_host(self, table: Optional["HostTable"], hid: str) -> "AdmitContext":
        """THE resource-domain fold (t_59943ed5 r5): a copy in which every lease, scratch GB and reservation booked on
        a member of domain_of(hid) is attributed to ``hid``. A pressure_from guest borrows its host's MEASURED
        headroom, so load promised to any member is in-flight load on the same box (Prism cb27fe636cb4, 07eb3f05ac19,
        faf2f501a1d4, 394aa1f12887). may_start(), choose() and bands_from_pressure() read only this copy; the ledger
        and decision rows keep their own host ids. A host with no guest gets ``self`` unchanged."""
        dom = domain_of(table, hid) if table is not None else {hid}
        if len(dom) == 1:
            return self

        def mine(h) -> bool:
            return h != hid and (table.declared_id(h) or h) in dom
        return dataclasses.replace(
            self,
            leases=tuple(dataclasses.replace(le, host=hid) if mine(le.host) else le for le in self.leases),
            scratch=tuple((hid if mine(h) else h, gb) for h, gb in self.scratch),
            reservations=tuple((hid if mine(h) else h, f) for h, f in self.reservations))

    def reserved_on(self, hid: str) -> Optional["Footprint"]:
        """The summed reservation booked on ``hid`` (after for_host(), on its whole domain); None when there is none."""
        tot = None
        for h, f in self.reservations:
            if h == hid:
                tot = tot.plus(f) if tot is not None else f
        return tot

    def scratch_for(self, names) -> float:
        """The ledger's reserved scratch on a host (any of its lower-case ``names``), as admit()'s
        live_reservations() sums it into ``reserved``."""
        return sum(gb for h, gb in self.scratch if h.lower() in names)

    def need(self, host: str) -> "admission.Estimate":
        """The same call choose() makes inline (test_work_estimate's lint reads it there)."""
        return work_estimate.estimate(self.job_class, host, self.history, seed=self.seed)


def history_snapshot(root=None) -> tuple:
    """Estimator observations, NOT capacity authority. Acquire before any ledger/writer lock.

    History writers do not take the reservation lock, so that lock never guaranteed a newer
    observation. Copy rows to detach from the shared cache; one snapshot feeds verdict AND booking.
    Keep all timestamps here: the admission window is applied at the post-lock decision clock.
    ``root`` resolves exactly as admit_context(root=) does (default fleet_root(); Prism b57fa1f567bc).
    """
    path = class_usage_path(Path(root) / CLASS_USAGE_REL if root is not None else None, root=root)
    return tuple(MappingProxyType(dict(r)) for r in admission.read_history_cached(path))


def history_window(history: Iterable[Mapping], now: float) -> tuple:
    rows = []
    for row in history:
        try:
            if float(row.get("at") or 0) < now - CLASS_USAGE_WINDOW_S:
                continue
        except (TypeError, ValueError):
            continue
        rows.append(row)
    return tuple(rows)


def resource_fit_refusal(job_class: str, consumer: str, rows: Iterable["Headroom"], table: "HostTable",
                         samples: Mapping[str, Optional["Sample"]]) -> Optional[str]:
    """The typed refusal for a class NO evaluated host can ever admit (Prism 1ea08e48d747, ruling t_d0c8e8f0 23:1x):
    each host's ceiling is admission.ceilings(), the same static check admit() refuses with (the host fully idle), and
    the need is the one that host booked, never lowered. Every candidate falls in one of three classes (ruling
    t_d0c8e8f0 05:5x, Prism c-8406618bb08f08fb):
      permanent-closed  not in the table, or the table policy closes it to ``consumer`` for good (policy_refusal with
                        no exclusion row and nothing admitted: consumer not eligible / ceiling 0, guest not enrolled):
                        ruled out, counts toward "every host ruled out" whatever its sample says;
      unknown           no sample, no need or no measured ceiling (a TTL'd exclusion or a paused kind is a sampled host
                        like any other): proves nothing, so the refusal stays transient (None, Prism 03e79438432e);
      measured          its ceiling holds the need (a wait, None) or does not (a fit failure).
    The refusal needs at least one measured fit failure: a fleet that is only permanent-closed stays all_closed."""
    best = None
    for r in rows:
        spec, sample = table.hosts.get(r.host), samples.get(r.host)
        if spec is None or policy_refusal(spec, consumer, None) is not None:
            continue
        if r.need is None or sample is None:
            return None
        c = admission.ceilings(_sample_row(spec, sample), _limits(spec))
        if not c:
            return None
        if all(getattr(r.need, ax) <= v for ax, v in c.items()):
            return None
        key = (c.get("ram_gb", math.inf), c.get("cpus", math.inf), r.host)
        if best is None or key > best[0]:
            best = (key, r, c)
    if best is None:
        return None
    _, r, c = best
    admits = " ".join(f"{ax}={c[ax]:g}" for ax in ("cpus", "ram_gb") if ax in c)
    return (f"unschedulable:resource-fit class {job_class} needs cpus={r.need.cpus:g} ram_gb={r.need.ram_gb:g}, "
            f"largest host admits {r.host} {admits}")


def admit_context(consumer: str, fp: Footprint, *, now: float, job: Optional["Job"] = None,
                  history: Optional[List[dict]] = None, leases: Optional[Iterable["admission.Lease"]] = None,
                  table: Optional["HostTable"] = None, held: Optional[Mapping[str, Footprint]] = None,
                  root=None, host: Optional[str] = None) -> AdmitContext:
    """The ONE builder of the utilization admission context: estimate history (default the class-usage ledger over
    CLASS_USAGE_WINDOW_S), leases (default the reservation ledger's live rows) plus ``held`` footprints as leases that
    never settle (allocd's extra_reserved, band()'s ``reserved``), and the ledger's live scratch per host (a Lease
    carries cpu/ram/gpu only; admit() subtracts the ledger's scratch through ``reserved``, t_f64c6d34 r3).
    ``root`` (default fleet_root()) is the state root the ledgers are read from (the collector's --home).
    ``host`` (with ``table``): the context comes back folded over that host's resource domain (AdmitContext.for_host)."""
    # Cache validation may re-hash the entire prefix; locked callers must supply a pre-acquired snapshot.
    hist = history if history is not None else admission.read_history_cached(
        class_usage_path(Path(root) / CLASS_USAGE_REL if root is not None else None, root=root), since=now - CLASS_USAGE_WINDOW_S)
    ledger = Path(root) / RESERVATIONS_REL if root is not None else None
    # caller-supplied leases are canonicalised like the ledger's: one booked under an alias counts on its host (c6568e9a9850)
    base = ([dataclasses.replace(le, host=table.resolve(le.host) or le.host) if table else le for le in leases]
            if leases is not None else ledger_leases(ledger, now=now, table=table, root=root))
    # caller-supplied leases (admit() under its lock, tests) bring their own ledger: scratch only on the default read
    scratch = () if leases is not None else tuple(
        (h, f.scratch_gb) for h, f in live_reservations(ledger, now=now, table=table, root=root).items() if f.scratch_gb > 0)
    ctx = AdmitContext(job_class_for(consumer, job), need_of(fp), tuple(hist),
                       tuple(base + held_leases(held or {}, table)), scratch)
    return ctx.for_host(table, host) if host is not None else ctx


def _headroom_utilization(h: Headroom, spec: HostSpec, sample: Sample, fp: Footprint, *, need, leases,
                          reserved: Optional[Footprint], now: Optional[float] = None) -> Headroom:
    need = need or need_of(fp)
    row, lim = _sample_row(spec, sample), _limits(spec)
    leases = list(leases or ())
    if spec.scorer == "v2":
        # R2c denominators on every utilization line too, a closed host's included (doctrine rule 2; FWA F1)
        h.psi_cpu_some10, h.psi_cpu_some60 = sample.psi_cpu_some10, sample.psi_cpu_some60
        h.occupancy, h.occupancy_src = sample.occupancy
        h.capacity = spec.cpu_capacity
    v = admission.admit(row, need, leases, host=spec.name, limits=lim, now=now)
    h.need, h.cpu_busy, h.binding = need, sample.cpu_busy, v.binding
    if "mem_gb" in v.free and "cpu" in v.free and _num(sample.ncpu):
        h.mem_gb, h.cores = v.free["mem_gb"], v.free["cpu"] * float(sample.ncpu)    # cores = CPUs of room left
    # the leases admit() counted, at admit()'s price (Prism 8e3aa5ad12d5; ramp t_7566ba56)
    held = admission.live_leases(leases, spec.name, row.get("at"), ramp=dict(lim.ramp_cpus), now=now)
    h.reserved_gb, h.reserved_threads = held.ram_gb, held.cpus
    if fp.scratch_gb > 0 and _num(sample.scratch_free_gb):
        h.scratch_gb = float(sample.scratch_free_gb) - (reserved.scratch_gb if reserved else 0.0) - fp.scratch_gb
    if not v.ok and v.reason.startswith("saturated"):
        h.reason = f"no_headroom:{v.binding} ({v.reason})"
        return h
    # a measured scratch refusal is reported before an unmeasured axis can read unknown (Prism 62945eddba74)
    if h.scratch_gb is not None and h.scratch_gb < 0:
        h.reason = f"no_headroom:scratch {h.scratch_gb:.1f} GB"
        return h
    if not v.ok:
        h.reason = f"closed:{v.reason}"
        return h
    h.score = admission.score(row, need, leases, host=spec.name, limits=lim, now=now)
    if spec.scorer == "v2":
        h.fraction = h.score            # choose() multiplies it by st_speed^alpha among the eligible (R2c, D2)
    h.eligible = True
    return h


PRESSURE_REL = Path("var") / "fleet-pressure.json"


def may_start(host: str, consumer: str, footprint: Optional[Mapping] = None, *, job: Optional["Job"] = None,
              samples: Optional[Mapping[str, Optional[Sample]]] = None, table: Optional[HostTable] = None,
              now: Optional[float] = None, root=None, leases: Optional[Iterable["admission.Lease"]] = None,
              history: Optional[List[dict]] = None, demand: Optional[Iterable["admission.Demand"]] = None,
              priority: Optional["admission.Priority"] = None, exclusions_path=None,
              admitted: Optional[float] = None, stale_after_s: float) -> tuple:
    """THE "may this work start on host H now" question, for EVERY workload class (t_e5124426): CI runner, Prism
    review, kanban worker, test-gate run, batch. -> (ok, why). One predicate (admission.admit_class) over the SAME
    measured inputs for every class: the host's pushed sample (cpu_busy, PSI cpu/mem/io, MemAvailable, procs), the
    class's MEASURED estimate on that host (work_estimate over the class-usage ledger; ``footprint`` / the table's
    class row is only the seed), the reservation ledger's live leases, the ONE host line (fleet/admission-priority.json
    ``line``, 0.90) and the queued demand of every class that outranks this one (var/placement/demand/*.json).
    Read-only: it reserves nothing (placement.admit / allocd /admit book the lease); a caller that only needs the verdict
    (Prism's gate, the CI allocator, test-gate's local wait) calls this. ``root`` = the fleet root the table, ledgers,
    pressure doc, priority, demand and the fwa holds are read from (default fleet_root()); ``exclusions_path`` is
    accepted and ignored (B5-P3 t_00664b58: exclusions.json is no longer read). ``admitted`` = this consumer's live
    admissions on the host (default: counted from the reservation ledger, as admit() counts them for choose()); a positive
    ``consumers.<c>.ceiling`` caps it (Prism f88f81bb2e24). Demand ``hosts`` restrictions resolve through the same
    HostTable, so a queue restricted to an alias holds work back on its host (Prism 364936434c3d).
    ``stale_after_s`` (REQUIRED, no default): the CALLER's freshness policy for the host sample, so may_start never
    refuses a sample its own caller's scorer calls fresh (Prism 5113510bdb9a: a hardcoded 60 s here dropped kanban
    hosts its 120 s policy admitted). Kanban passes placement_policy.stale_after_s; CI, batch and test-gate pass theirs.
    It also overrides the pressure doc's own ``stale_after_s`` when the samples are read here."""
    try:                             # noqa: PLC0415  (imported like admission / work_estimate above)
        from . import admission_demand as dmd
    except ImportError:
        import admission_demand as dmd
    if _stale_after(stale_after_s) is None:
        raise PlacementError("may_start: stale_after_s is required (the caller's sample freshness policy)")
    stale_after_s = float(stale_after_s)
    now = _now(now)
    base = Path(root) if root is not None else fleet_root(root=root)
    table = table if table is not None else load_hosts(base / TABLE_REL, root=root)
    hid = table.resolve(host)
    if hid is None:
        return False, f"closed:{host} not in config/fleet-hosts.json"
    # operator controls first, through the SAME helper headroom()/choose() use (Prism 26a9b46c6a7b): a drained host or
    # an ineligible consumer is refused before any measurement; an unreadable kinds file fails CLOSED
    try:
        excl = kind_exclusions(table, consumer, base / KINDS_REL, now=now, root=root)
    except ExclusionsError as exc:
        return False, f"closed:exclusions unreadable ({exc})"
    held = fwa_hold(hid, consumer, now=now, root=base)   # FWA §5.3: the ONE hold record (B5-P3: no exclusions.json)
    if held:
        return False, f"closed:fwa {held}"
    if admitted is None:
        try:          # the count admit() hands choose() (live_admissions), so a full allocation refuses here too
            admitted = live_admissions(consumer, base / RESERVATIONS_REL, now=now, table=table, root=root).get(hid, 0)
        except LedgerError as exc:
            return False, f"closed:ledger unreadable ({exc})"
    refused = policy_refusal(table.hosts[hid], consumer, excl.get(hid), admitted)
    if refused:
        return False, refused
    if samples is None:
        try:
            doc = json.loads((base / PRESSURE_REL).read_text(encoding="utf-8"))
            samples = samples_from_pressure(doc, now=now, stale_after_s=stale_after_s) if isinstance(doc, dict) else {}
        except (OSError, ValueError, PlacementError):
            samples = {}
    sample = sample_for(table, samples, hid)
    if sample is None:
        return False, "closed:no sample"
    if sample.status != "fresh":
        return False, f"closed:{sample.status}" + (f" ({sample.note})" if sample.note else "")
    if not _num(sample.at) or now - float(sample.at) > stale_after_s:
        return False, "closed:stale (sample time missing or older than %gs)" % stale_after_s
    spec = table.hosts[hid]
    refused = power_refusal(spec, sample)
    if refused:
        return False, refused
    fp = table.footprint(consumer, footprint if footprint is not None else (job.footprint if job else None),
                         profile=job.profile if job else None)
    raw = admit_context(consumer, fp, now=now, job=job, history=history, leases=leases, table=table, root=root)
    ctx = raw.for_host(table, hid)       # folded over hid's resource domain: leases AND scratch
    held = {}
    for lease in raw.leases:
        amount = Footprint(lease.need.ram_gb, lease.need.cpus)
        held[lease.host] = held.get(lease.host, Footprint(0, 0)).plus(amount)
    refused = shared_budget_refusal(table, hid, Footprint(ctx.need(hid).ram_gb, ctx.need(hid).cpus), held)
    if refused:
        return False, refused
    refused = guest_vm_refusal(spec, raw, ctx.need(hid).cpus)     # the guest's own VM size, apart from the domain
    if refused:
        return False, refused
    if fp.scratch_gb > 0 and _num(sample.scratch_free_gb):
        left = float(sample.scratch_free_gb) - ctx.scratch_for({hid.lower(), *(a.lower() for a in spec.aliases)}) \
            - fp.scratch_gb
        if left < 0:
            return False, f"saturated:scratch_gb {left:+.2f}"
    # demand restricted to any member of hid's resource domain is queued load on this box (Prism d26743a7e1e6)
    demand = list(demand) if demand is not None else dmd.read_demand(hid, root=root, now=now,
                                                                     resolve=domain_resolve(table, hid))
    priority = priority if priority is not None else dmd.load_priority(root=root)
    v = admission.admit_class(_sample_row(spec, sample), dmd.workload_of(consumer), ctx.need(hid),
                              ctx.leases,
                              demand=demand, priority=priority, limits=_limits(spec), host=hid,
                              class_line=class_line(dmd.workload_of(consumer), hid, root=root), now=now)
    return v.ok, (v.reason or f"ok (binding {v.binding})")


#: t_71471192: a workload class's own CPU line on a host. Only CI has one: cpu_lines lane `ci` kind `admit`, whose
#: per-host mirror is hosts.<h>.ci_allocator.admit_ci_cpu_pct (lint-load-lines keeps the two equal), read through
#: load_lines, the ONE reader of the registry. Shipped at 0.90 = the fleet line: no change. Lowering it (proposed 0.75
#: on ci-box / ace-ai, Ace decides) stops CI starts at that busy fraction, so the kanban class (fleet line 0.90) keeps
#: the band between. admission.admit_class never lets a class line RAISE the fleet line.
CLASS_LINE_LANES = {"ci": "ci"}
POLICY_REL = Path("fleet") / "placement-policy.json"


def class_line(workload: str, hid: str, *, root=None) -> Optional[float]:
    """``workload``'s own CPU admit line on ``hid`` (0..1 of the host's CPUs) from the cpu_lines registry, or None (no
    lane for this class, or the registry reader is absent: the fleet line alone applies)."""
    lane = CLASS_LINE_LANES.get(workload)
    policy = (Path(root) if root is not None else fleet_root()) / POLICY_REL
    if lane is None or not policy.exists():
        return None
    try:
        if __package__:   # the installed wheel ships the reader as a sibling module (Prism 932522213fc3)
            from . import load_lines
        else:
            import load_lines  # noqa: PLC0415  (scripts/lib sibling; a vendored copy without it = the fleet line)
    except ImportError:
        return None
    v = load_lines.line(hid, lane, "admit", policy=policy)
    return float(v) if _num(v) else None


# ── bands: a NAME for the admission vector, never a second scorer (allocation §2.2, t_260e8379) ──────
BAND_ORDER = (None, "ok", "warm", "hot")    # None = UNKNOWN (stale / absent / unmeasured, nothing measured is hot)


def band(spec: HostSpec, sample: Optional[Sample], fp: Footprint, *, consumer: str,
         closed_when: Mapping[str, float], reserved: Optional[Footprint] = None,
         require_axes: Iterable[str] = REQUIRED_AXES, mode: Optional[str] = None, now: Optional[float] = None,
         ctx: Optional[AdmitContext] = None, root=None,
         stale_after_s: Optional[float] = DEFAULT_STALE_AFTER_S, table: Optional["HostTable"] = None) -> tuple:
    """_band_pressure() (the domain's pressure band, below), then a ``pressure_from`` guest's own VM size: one job
    over ``vm_cpus`` is hot, a second one over it caps ok at warm, the same guest_vm_refusal() choose() and may_start()
    apply (Prism 0b6cf97fb659). The check reads the UNFOLDED context (``ctx`` as given, or the one built here)."""
    util = sample is not None and sample.status == "fresh" and band_basis(mode, root=root) == "utilization"
    if util and ctx is None:
        ctx = admit_context(consumer, fp, now=_now(now), held={spec.name: reserved} if reserved else None,
                            root=root, table=table)
    b, why = _band_pressure(spec, sample, fp, consumer=consumer, closed_when=closed_when, reserved=reserved,
                            require_axes=require_axes, mode="utilization" if util else "legacy", now=now, ctx=ctx,
                            root=root, stale_after_s=stale_after_s, table=table)
    if not spec.pressure_from or b == "hot" or sample is None or sample.status != "fresh":
        return b, why
    if b is None and util and any(str(w).startswith("closed:stale") for w in why):
        # _band_pressure() found the sample expired at ``now``: no verdict from stale input, the guest cap included
        # (Prism e6d9ea2e4127 / b74234696720 / 869d7324e953)
        return b, why
    if util:
        raw, cpus = ctx, float(ctx.need(spec.name).cpus)
    else:
        # legacy too reads the live guest leases (``ctx`` as given, else the ledger's), never an empty list: a full
        # guest is hot whether or not ``reserved`` is passed (Prism 3b1a9e851d14, t_b9ed3796)
        # ``reserved`` is the caller's per-host total (live_reservations), i.e. the same rows the ledger leases
        # carry: a context built HERE from the ledger already holds it, so it is never appended again (Prism
        # cadbb61dcd6a); a caller-supplied ``ctx`` (admit() under its lock, tests) brings its own lease set, and
        # ``reserved`` adds only what that set does not already book on the guest.
        raw = ctx if ctx is not None else admit_context(consumer, fp, now=_now(now), root=root, table=table)
        if reserved:
            # one rule for both branches (Prism 05227baeb76d): the context's leases + reservations on the guest count
            # first, and only the per-axis shortfall of the explicit total is added, never the whole total on top
            # and never dropped because the context books part of it (Prism b5bf2173a5b7)
            names = {spec.name.lower(), *(a.lower() for a in spec.aliases)}
            booked = Footprint(ram_gb=0.0, threads=0.0, scratch_gb=raw.scratch_for(names))
            for le in raw.leases:
                if str(le.host).lower() in names:
                    booked = booked.plus(Footprint(ram_gb=float(le.need.ram_gb), threads=float(le.need.cpus),
                                                   gpu_mb=float(le.need.gpu_mib)))
            for h, f in raw.reservations:
                if str(h).lower() in names:
                    booked = booked.plus(f)
            short = Footprint(*(max(0.0, getattr(reserved, k) - getattr(booked, k)) for k in FOOTPRINT_KEYS))
            if any(getattr(short, k) > 0 for k in FOOTPRINT_KEYS):
                raw = dataclasses.replace(raw, reservations=tuple(raw.reservations) + ((spec.name, short),))
        cpus = float(fp.threads)
    one = guest_vm_refusal(spec, raw, cpus)
    if one:
        return "hot", [one]
    two = guest_vm_refusal(spec, raw, 2 * cpus)
    if two and b == "ok":
        return "warm", [f"room for one {consumer} job, not two: {two}"]
    return b, why


def _band_pressure(spec: HostSpec, sample: Optional[Sample], fp: Footprint, *, consumer: str,
                   closed_when: Mapping[str, float], reserved: Optional[Footprint] = None,
                   require_axes: Iterable[str] = REQUIRED_AXES, mode: Optional[str] = None,
                   now: Optional[float] = None, ctx: Optional[AdmitContext] = None, root=None,
                   stale_after_s: Optional[float] = DEFAULT_STALE_AFTER_S,
                   table: Optional["HostTable"] = None) -> tuple:
    """(band, reasons) DERIVED from headroom(): hot = headroom() refuses the footprint, warm = it admits this one
    footprint but not a second, ok = room for two, None = the sample cannot be read (absent/stale/unmeasured) and
    no measured axis already refuses. ``reasons`` names EVERY axis that fails, not only the first headroom()
    returns (10-12 07:10:18 tripped psi_mem, %system and memory at once). The load axis is headroom()'s
    max(load1, load5): enter on load1, clear only when load5 has (the 07:19:26 false ok, load5 154 on 16c).
    Exclusions and ceilings are admission state, not host pressure, and are not part of the band.
    Utilization basis: ``ctx`` (default admit_context() at ``now``) is the admitter's own context: the learned
    estimate, the ledger's live leases and ``reserved`` as a held lease (t_f64c6d34 r2). ``root`` (default
    fleet_root()) is the state root whose config.yaml, kill switch and ledgers decide (t_f64c6d34 r3).
    ``stale_after_s`` (with ``now``): the utilization band re-checks the sample's age at ``now`` as may_start() does,
    so a sample read fresh but older than this names no verdict: unknown, never hot/ok (Prism 0006db333fc1)."""
    if sample is None or sample.status != "fresh":
        return None, ["no sample" if sample is None else f"{sample.status}" + (f" ({sample.note})" if sample.note else "")]
    # pressure only: ceilings / exclusions are admission policy, not host pressure (Prism bfc9d97abdd5)
    spec = dataclasses.replace(spec, ceilings={}, enrolled=frozenset(), pressure_from=None)
    if band_basis(mode, root=root) == "utilization":
        stale_after_s = _stale_after(stale_after_s)
        if stale_after_s is not None and now is not None and (
                not _num(sample.at) or _now(now) - float(sample.at) > stale_after_s):
            return None, ["closed:stale (sample time missing or older than %gs)" % stale_after_s]
        if ctx is None:
            ctx = admit_context(consumer, fp, now=_now(now), held={spec.name: reserved} if reserved else None,
                                root=root, table=table)
        if table is not None:
            # the SAME resource-domain fold may_start() reads (idempotent on an already-folded ctx), so a lease on a
            # parent / sibling counts here too (Prism 326e66e95630 / 10037992878c / eb5c7206c4c2)
            ctx = ctx.for_host(table, spec.name)
        return _band_utilization(spec, sample, fp, consumer=consumer, closed_when=closed_when, ctx=ctx,
                                 reserved=reserved, now=_now(now), root=root, table=table)
    req = tuple(a for a in require_axes if a not in spec.unmeasurable_axes)
    measured = tuple(a for a in req if _num(getattr(sample, a, None)))
    missing = [f"{a} unmeasured" for a in req if a not in measured]
    tripped = [f"{a} {float(getattr(sample, a)):g} >= {thr:g}" for a, thr in closed_when.items()
               if _num(getattr(sample, a, None)) and float(getattr(sample, a)) >= thr]
    if sample.load is None or sample.mem_available_gb is None:
        # a measured refusal stays hot even with load/memory missing (Prism b30ecb4466e4)
        return ("hot", tripped + missing) if tripped else (None, missing or ["load/memory unmeasured"])
    # the scorer itself, over what was measured: an unmeasured axis is declared unavailable, never read as 0
    h = headroom(spec, sample, fp, consumer=consumer, closed_when=closed_when, reserved=reserved,
                 require_axes=measured)
    # headroom on both axes even when a closed_when axis already refused (reasons list every failing axis)
    room = h if h.mem_gb is not None else headroom(spec, sample, fp, consumer=consumer, closed_when={},
                                                   reserved=reserved, require_axes=measured)
    if room.mem_gb is not None and room.mem_gb < 0:
        tripped.append(f"headroom mem {room.mem_gb:.1f} GB < 0 (avail {sample.mem_available_gb:g} - reserve "
                       f"{spec.mem_reserve_gb:g} - {consumer} {fp.ram_gb:g})")
    if room.cores is not None and room.cores < 0:
        tripped.append(f"headroom cpu {room.cores:.1f} cores < 0 (at {fmt_load(sample.load, spec.cores, spec.threads)})")
    if not h.eligible:
        return "hot", (tripped + missing) or [h.reason]
    if missing:
        return None, missing
    if h.mem_gb is None or h.cores is None:            # unreachable: an eligible Headroom carries both
        return None, ["headroom unmeasured"]
    # scratch too, the same test as band_of() (Prism e4e9359966e3): a second job that fits RAM/CPU but not scratch
    # does not fit, so the published band must not say ok where the admission ledger says warm
    if h.mem_gb < fp.ram_gb or h.cores < fp.threads or (h.scratch_gb is not None and h.scratch_gb < fp.scratch_gb):
        scr = "" if h.scratch_gb is None else f" / {h.scratch_gb:.1f} GB scratch"
        scr_fp = "" if h.scratch_gb is None else f" / {fp.scratch_gb:g} GB scratch"
        return "warm", [f"room for one {consumer} job: headroom {h.mem_gb:.1f} GB / {h.cores:.1f} cores{scr} "
                        f"< 2 x {fp.ram_gb:g} GB / {fp.threads:g} threads{scr_fp}"]
    return "ok", [f"headroom {h.mem_gb:.1f} GB / {h.cores:.1f} cores"]


BAND_BASIS_READER, BAND_BASIS_ENV = "admission-band", "PLACEMENT_BAND_BASIS"


def band_basis(mode: Optional[str] = None, root=None) -> str:
    """Which formula names the published band (t_f64c6d34): "utilization" = the SAME admission.admit() the admitter
    runs (measured cpu_busy + the job's estimate + leases still inside their settle window; cores_reserve and
    procs_running are not read), "legacy" = the scorer's cores axis (cores x yield - cores_reserve - occupancy -
    reserved threads). ``mode`` (tests) decides outright. Else utilization only while config.yaml kanban.admission is
    utilization (the band names what the admitter does) AND the busy_basis kill switch for reader ``admission-band``
    (env PLACEMENT_BAND_BASIS, else fleet/placement-policy.json busy_basis.admission-band) is cpu_busy (its default);
    ``load1`` there restores the legacy band. ``root`` reads that root's config.yaml and fleet/placement-policy.json."""
    if mode is not None:
        return "utilization" if mode == "utilization" else "legacy"
    if admission_mode(Path(root) / CONFIG_REL if root is not None else None, root=root) != "utilization":
        return "legacy"
    try:
        if __package__:
            from . import busy_basis
        else:
            import busy_basis  # noqa: PLC0415
        # root omitted: BUSY_BASIS_POLICY, else fleet_root()'s (PLACEMENT_HOME) policy, never busy_basis's own
        # checkout (Prism a922d9125c6e); an explicit root reads that root's file, as before
        pol = (Path(root) / "fleet" / "placement-policy.json" if root is not None else
               os.environ.get("BUSY_BASIS_POLICY") or fleet_root(root=root) / "fleet" / "placement-policy.json")
        basis = busy_basis.basis(BAND_BASIS_READER, BAND_BASIS_ENV, pol)
    except ImportError:
        basis = os.environ.get(BAND_BASIS_ENV) or "cpu_busy"
    return "legacy" if basis == "load1" else "utilization"


def _band_class_rule(spec: HostSpec, sample: Sample, consumer: str, leases, *, now: Optional[float], root=None,
                     table: Optional["HostTable"] = None):
    """The check may_start() runs for ``consumer`` on this host, as a function of the job's estimate, or None while
    the class's kill switch (``<WORKLOAD>_UNIFIED_ADMIT=0``) keeps its old rule (admit(), which headroom() runs).
    Same sample row, limits, leases, ``root`` demand and priority as may_start() (Prism b22e585970ce).
    The switch is the ADMITTER's, as it published it under ``root`` (demand file ``unified``), never this process's
    env: the band runs in the placement collector, the admitter elsewhere (Prism 59574d3a240b). No fresh published
    mode = the switch's default (the one rule), the same answer unified() gives an admitter with the switch unset."""
    try:                             # noqa: PLC0415  (imported as may_start() does)
        from . import admission_demand as dmd
    except ImportError:
        import admission_demand as dmd
    workload = dmd.workload_of(consumer)
    if dmd.published_unified(workload, root=root, now=_now(now)) is False:
        return None
    names = {spec.name.lower(), *(a.lower() for a in spec.aliases)}
    # may_start() resolves demand ``hosts`` over the resource domain (domain_resolve); with the table this is the same
    # match, without it this host's own aliases are (Prism d26743a7e1e6)
    demand = dmd.read_demand(spec.name, root=root, now=_now(now),
                             resolve=domain_resolve(table, spec.name) if table is not None and spec.name in table.hosts
                             else (lambda h: spec.name if str(h).lower() in names else h))
    priority = dmd.load_priority(root=root)
    row, lim = _sample_row(spec, sample), _limits(spec)

    def verdict(need):
        return admission.admit_class(row, workload, need, leases, demand=demand, priority=priority, limits=lim,
                                     host=spec.name, now=_now(now))
    return verdict


def _band_utilization(spec: HostSpec, sample: Sample, fp: Footprint, *, consumer: str,
                      closed_when: Mapping[str, float], ctx: AdmitContext,
                      reserved: Optional[Footprint] = None, now: Optional[float] = None, root=None,
                      table: Optional["HostTable"] = None) -> tuple:
    """The band over the admitter's own verdict on the admitter's context: hot = it refuses one job of this class,
    warm = one fits but not two (the estimate AND the footprint doubled, so scratch is checked for two), ok = two fit.
    A measured refusal is hot even when another axis is unmeasured (admit() reports saturation before unmeasured).
    The 10-25 Studio read hot ("headroom cpu -12.0 cores", 24 runnable threads vs 24 - 10 reserve) at cpu_busy
    0.47 while the admitter, on this formula, admitted (t_f64c6d34). While the class runs the ONE rule
    (``<WORKLOAD>_UNIFIED_ADMIT`` not 0) the admitter is may_start(): admission.admit_class() with the fleet line and
    the queued demand of every higher class (read from ``root``), so a host held back for Prism reads hot, not ok
    (Prism b22e585970ce)."""
    names = {spec.name.lower(), *(a.lower() for a in spec.aliases)}
    leases = [le if le.host == spec.name or le.host.lower() not in names else dataclasses.replace(le, host=spec.name)
              for le in ctx.leases]          # a ledger row booked under an alias is this host's (as table.resolve)
    need = ctx.need(spec.name)
    # the ledger's scratch, as admit() passes it in ``reserved`` (utilization headroom reads only scratch from it;
    # cpu/ram ride as leases, so they are never booked twice; Prism c52539c7304c)
    held_scr = ctx.scratch_for(names)
    if held_scr > 0:
        reserved = (dataclasses.replace(reserved, scratch_gb=reserved.scratch_gb + held_scr) if reserved
                    else Footprint(ram_gb=0.0, threads=0.0, scratch_gb=held_scr))
    busy = f"cpu_busy {sample.cpu_busy:g}" if _num(sample.cpu_busy) else "cpu_busy ?"
    one = headroom(spec, sample, fp, consumer=consumer, closed_when=closed_when, mode="utilization", need=need,
                   leases=leases, reserved=reserved, now=_now(now))
    rule = _band_class_rule(spec, sample, consumer, leases, now=now, root=root, table=table)
    v1 = rule(need) if rule else None
    if v1 is not None and not v1.ok and not v1.reason.startswith("unmeasured"):
        return "hot", [f"{v1.reason} ({busy}, utilization, admit_class)"]
    if not one.eligible:
        if one.reason.startswith("closed:unmeasured") or one.reason.startswith("closed:stale"):
            return None, [one.reason]
        return "hot", [f"{one.reason} ({busy}, utilization)"]
    two = headroom(spec, sample, fp.plus(fp), consumer=consumer, closed_when=closed_when, mode="utilization",
                   need=need.plus(need), leases=leases, reserved=reserved, now=_now(now))
    room = f"headroom {one.mem_gb:.1f} GB / {one.cores:.1f} cpus after one job ({busy}, utilization)"
    if v1 is not None and not v1.ok:                 # the class rule cannot measure an axis: unknown, as headroom()
        return None, [f"closed:{v1.reason}"]
    v2 = rule(need.plus(need)) if rule else None
    if v2 is not None and not v2.ok:
        return "warm", [f"room for one {consumer} job, not two: {v2.reason} ({busy}, utilization, admit_class)"]
    if not two.eligible:
        return "warm", [f"room for one {consumer} job: {room}"]
    return "ok", [room]


def bands_from_pressure(table: HostTable, doc: Mapping, *, now: float,
                        consumers: Optional[Iterable[str]] = None, home=None, root: Optional[Path] = None) -> Dict[str, dict]:
    """{host: {"band": {consumer: band}, "reason": {consumer: "a; b"}}} for every host row of a fleet-pressure.json
    document that the table knows, over the table's footprints (or ``consumers``). Same sample read and the same
    staleness rule as choose(), so a published band can never say ok where choose() would refuse. ``home`` (default
    fleet_root()) is the collection's root: its mode, kill switch and ledgers name the band (Prism 241f9c71263b)."""
    if root is not None:
        if home is not None and Path(home) != Path(root):
            raise PlacementError("home and root disagree")
        home = root
    samples = samples_from_pressure(doc, now=now)
    stale = float(doc["stale_after_s"]) if _num(doc.get("stale_after_s")) else DEFAULT_STALE_AFTER_S
    names = [c for c in (consumers or table.footprints) if c in table.footprints]
    out: Dict[str, dict] = {}
    # the admitter's context once per consumer (one ledger + history read per document, not per host)
    ctxs = ({c: admit_context(c, table.footprint(c), now=now, table=table, root=home) for c in names}
            if band_basis(root=home) == "utilization" else {})
    for key, s in samples.items():
        hid = table.resolve(key)
        if hid is None:
            continue
        spec, row = table.hosts[hid], {"band": {}, "reason": {}}
        s = sample_for(table, samples, hid) if spec.pressure_from else s   # never an older mirror (901e6ec92ac9)
        for c in names:
            # unfolded: band() folds it over hid's domain itself and reads it raw for the guest's VM cap (0b6cf97fb659)
            ctx = ctxs.get(c)
            b, why = band(spec, s, table.footprint(c), consumer=c, closed_when=table.closed_when, now=now,
                          ctx=ctx, root=home, stale_after_s=stale, table=table)
            row["band"][c], row["reason"][c] = b, "; ".join(why)
        out[key] = row
    return out


@dataclasses.dataclass(frozen=True)
class Job:
    """Per-job overrides (layer 3). Every field optional; an override naming a host that is not eligible is
    ignored (the gate always wins) and the fallthrough is recorded in Decision.rule."""
    affinity: Optional[str] = None      # the work lives there (files, running service, GPU)
    continuity: Optional[str] = None    # the previous run of this card ran there (warm workspace/caches)
    priority: Optional[int] = None      # 0/1 = may claim the fastest eligible core class
    core_class: Optional[str] = None    # "fast" (highest single-core clock) | "wide" (most threads) | None
    footprint: Optional[Mapping] = None  # overrides the class default (card/body ``footprint:`` line)
    profile: Optional[str] = None        # picks the consumer's per-profile class when the table has one (t_edc220cc)
    job_class: Optional[str] = None      # utilization: the measured class (e.g. kanban-worker-pytest-heavy)


_FOOTPRINT_LINE = re.compile(r"^[ \t>*-]*footprint\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def footprint_from_text(text: Optional[str]) -> Optional[Dict[str, object]]:
    """The ``footprint: ram_gb=8 threads=4 scratch_gb=20 gpu_mb=0`` line of a card/body, or None. Values that are
    not numbers are kept as given so ``HostTable.footprint()`` REFUSES them (a typo never silently means the
    default); an unknown key is refused the same way."""
    if not isinstance(text, str):
        return None
    m = _FOOTPRINT_LINE.search(text)
    if not m:
        return None
    out: Dict[str, object] = {}
    for tok in m.group(1).replace(",", " ").split():
        k, sep, v = tok.partition("=")
        if not sep:
            out[tok] = None
            continue
        try:
            out[k.strip().lower()] = float(v)
        except ValueError:
            out[k.strip().lower()] = v
    return out or None


def band_of(row: "Headroom", fp: Footprint) -> Optional[str]:
    """A decision row's band, the same names as band(): hot = measured and refused (no headroom, or a closed_when
    axis tripped), warm = admits this footprint only, ok = room for one more, None = unknown / not a pressure
    verdict (stale, absent, unmeasured, excluded, at ceiling)."""
    if not row.eligible:
        r = row.reason or ""
        return "hot" if r.startswith("no_headroom") or (r.startswith("closed:") and " >= " in r) else None
    if row.mem_gb is None or row.cores is None:
        return None
    roomy = row.mem_gb >= fp.ram_gb and row.cores >= fp.threads \
        and (row.scratch_gb is None or row.scratch_gb >= fp.scratch_gb)
    return "ok" if roomy else "warm"


@dataclasses.dataclass
class Decision:
    host: Optional[str]
    reason: str                              # "" on a pick; all_closed | no_headroom | no_candidates
    rows: List[Headroom]
    consumer: str
    footprint: Footprint
    kept_previous: bool = False
    rule: str = ""                           # which layer picked: affinity | continuity | priority | core_class:<c> | default_first | headroom
    scorer: str = DEFAULT_SCORER             # which scorer produced it (v2 adds denominators to as_row)
    mode: str = ""                           # admission mode that decided it (legacy | utilization; t_f73c65a8)

    def lines(self, table: HostTable) -> List[str]:
        # a candidate that is not in the table (rows reason `closed:not in config/fleet-hosts.json`) has no spec: name
        # it, never KeyError (t_e2d546db: test-gate on a pool host crashed here on 'ci' with a table lacking it)
        return [r.line(table.hosts[r.host]) if r.host in table.hosts else f"{r.host} {r.reason or 'not in the table'}"
                for r in self.rows]

    def summary(self, table: HostTable) -> str:
        head = (f"pick {self.host} by {self.rule}" + (" (kept, within margin)" if self.kept_previous else "")) \
            if self.host else f"refuse: {self.reason}"
        return f"{head} for {self.consumer} footprint {self.footprint.ram_gb:g} GB / {self.footprint.threads:g} threads; " \
               + "; ".join(self.lines(table))

    def as_row(self) -> dict:
        row = {"picked": self.host, "why": self.reason or None, "rule": self.rule or None, "kept_previous": self.kept_previous,
               "consumer": self.consumer, "footprint": dataclasses.asdict(self.footprint),
               "hosts": {r.host: {"eligible": r.eligible, "reason": r.reason or None, "load": r.load,
                                  "band": band_of(r, self.footprint),
                                  "memavail_gb": r.mem_available_gb,
                                  "reserved_gb": r.reserved_gb, "reserved_threads": r.reserved_threads,
                                  "headroom_gb": None if r.mem_gb is None else round(r.mem_gb, 1),
                                  "headroom_cores": None if r.cores is None else round(r.cores, 1),
                                  "score": None if not r.eligible else round(r.score, 4), "binding": r.binding or None}
                         for r in self.rows}}
        if self.scorer == "v2":
            # v2 denominators on every host row (doctrine rule 2); a v1 row stays byte for byte what it was
            row["scorer"] = "v2"
            for r in self.rows:
                row["hosts"][r.host].update({
                    "occupancy": None if r.occupancy is None else round(r.occupancy, 2),
                    "occupancy_src": r.occupancy_src or None,
                    "capacity": None if r.capacity is None else round(r.capacity, 2),
                    "psi_cpu_some10": r.psi_cpu_some10, "psi_cpu_some60": r.psi_cpu_some60,
                    "fraction": None if r.fraction is None else round(r.fraction, 4), "alpha": r.alpha})
        return row


def shared_budget_refusal(table: HostTable, hid: str, need: Footprint,
                          held: Mapping[str, Footprint]) -> Optional[str]:
    """Strict promises, including settled jobs, over the existing pressure-source domain.

    Called inside admit's ledger lock. The kernel ancestor remains the authority for
    actual consumption (including expired holders and undeclared guest overhead).
    """
    domain = domain_of(table, hid)
    budget = next((b for h, b in table.shared_budgets.items() if h in domain), None)
    if budget is None:
        return None
    total = Footprint(budget["overhead_ram_gb"], budget["overhead_threads"]).plus(need)
    for name, fp in held.items():
        if (table.declared_id(name) or name) in domain:
            total = total.plus(fp)
    if total.threads > budget["threads"] or total.ram_gb > budget["ram_gb"]:
        return f"closed:shared_budget threads={total.threads:g}/{budget['threads']:g} ram_gb={total.ram_gb:g}/{budget['ram_gb']:g}"
    return None


def _booked_reservations(util: bool, vm_reservations: Optional[Mapping[str, Footprint]],
                         ctx: AdmitContext) -> Dict[str, Footprint]:
    """choose()'s reservations that count beside ``ctx.leases`` for the guest VM cap AND the shared budget: under
    utilization the leases replace the caller's reservations (only an explicit ``vm_reservations`` adds), under
    legacy ``vm_reservations`` when given, else the caller's reservations (Prism c-dd52632d2ecf3a32)."""
    if util:
        return dict(vm_reservations or {})
    return dict(vm_reservations) if vm_reservations is not None else dict(ctx.reservations)


def choose(table: HostTable, consumer: str, samples: Mapping[str, Optional[Sample]], *,
           candidates: Optional[Iterable[str]] = None, footprint: Optional[Mapping] = None,
           previous: Optional[str] = None, margin: float = DEFAULT_MARGIN,
           exclusions: Optional[Mapping[str, str]] = None, job: Optional[Job] = None,
           reservations: Optional[Mapping[str, Footprint]] = None,
           require_axes: Iterable[str] = REQUIRED_AXES, exclusions_path=None, now: Optional[float] = None,
           admitted: Optional[Mapping[str, float]] = None, stale_after_s: Optional[float] = DEFAULT_STALE_AFTER_S,
           mode: Optional[str] = None, leases: Optional[Iterable["admission.Lease"]] = None,
           history: Optional[List[dict]] = None,
           vm_reservations: Optional[Mapping[str, Footprint]] = None, kinds_path=None, root: Optional[Path] = None) -> Decision:
    """The three layers over ``candidates`` (default: every host in the table).

    1. gate: a candidate with no sample, a stale/absent one, an unmeasured axis, a tripped closed_when axis,
       not eligible, a positive ceiling already reached by ``admitted`` live admissions of this consumer,
       an exclusion row, or headroom < 0 on either axis is NOT eligible. No eligible host = REFUSE
       (host None): the caller queues, never spills.
    2. default: ``job`` overrides first (affinity, continuity, priority, core_class; only an ELIGIBLE host
       is ever taken, so the gate always wins); else ``table.default_first`` while its load is at or under
       ``saturate_to`` x physical cores; else the most headroom. ``previous`` (the last pick) is kept while
       the best other host does not beat it by ``margin`` of capacity (hysteresis; headroom layer only).
    ``stale_after_s`` (default 60 s) re-checks every sample's age against ``now`` at decision time (Prism fda1ea43);
    pass None only when the caller has just produced the samples itself and vouches for their freshness.
    ``mode`` (default: config.yaml ``kanban.admission``, legacy when unset) "utilization" gates every host with
    admission.admit(): the job's estimate is work_estimate.estimate() for its class on that host (p90 of the last
    runs in ``history``, default the class-usage ledger; the declared footprint is the seed), and ``leases``
    (default: the reservation ledger's live rows) stand in for counts until the sample shows them.
    """
    stale_after_s = _stale_after(stale_after_s)
    fp = table.footprint(consumer, footprint if footprint is not None else (job.footprint if job else None),
                         profile=job.profile if job else None)
    names = list(candidates) if candidates is not None else list(table.hosts)
    folded: Dict[str, Optional[Sample]] = {}
    for k, v in samples.items():
        hid = table.resolve(k)
        if hid is not None and (hid not in folded or folded[hid] is None):
            folded[hid] = v
    for hid in table.hosts:                       # a pressure_from guest reads its host's sample (t_59943ed5), its
        if table.hosts[hid].pressure_from:        # mirror only when as new (sample_for; Prism 901e6ec92ac9)
            folded[hid] = sample_for(table, samples, hid)
    now = _now(now)
    mode = admission_mode(root=root) if mode is None else mode     # after the input checks: a refused call reads nothing
    require_axes = tuple(require_axes)   # a generator would be exhausted by the first candidate (Prism P1 16d4270a)
    # the caller's map (an empty reason still excludes, Prism 95afdab6); B5-P3 (t_00664b58): exclusions_path is
    # ignored, an operator exclusion is an fwa hold (read below)
    excl = caller_exclusions(table, extra=exclusions)
    # a paused work kind (state/fleet-pause/kinds.json) closes this consumer like an exclusion row (t_47109d72)
    for k, v in kind_exclusions(table, consumer, kinds_path, now=now, root=root).items():
        excl.setdefault(k, v)
    memo = _fwa_memo()                            # FWA §5.3: an fwa hold closes the host (I4)
    for hid in table.hosts:
        held = fwa_hold(hid, consumer, now=now, root=root, memo=memo)
        if held:
            excl.setdefault(hid, f"fwa {held}")
    resv: Dict[str, Footprint] = {}
    for k, v in (reservations or {}).items():      # aliases of one host SUM, never overwrite (Prism P1 8b7fd838)
        hid = table.resolve(k) or k
        prev = resv.get(hid)
        resv[hid] = prev.plus(v) if prev else v
    used: Dict[str, float] = {}
    for k, v in (admitted or {}).items():             # per-consumer live admissions, aliases summed likewise
        hid = table.resolve(k) or k
        used[hid] = used.get(hid, 0.0) + float(v)
    util = mode == "utilization"
    # leases replace ``reservations`` for cpu/ram on the utilization path (a lease ages out of the count once the
    # sample shows its job); both paths read every host-attributed input through ONE per-host fold (for_host)
    ctx = (admit_context(consumer, fp, now=now, job=job, history=history, leases=leases, table=table, root=root) if util
           else AdmitContext("", need_of(fp), (), ()))
    ctx = dataclasses.replace(ctx, reservations=tuple(resv.items()))
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
        hc = ctx.for_host(table, hid)
        need = work_estimate.estimate(ctx.job_class, hid, ctx.history, seed=ctx.seed) if util else ctx.seed
        if util:
            rows.append(headroom(table.hosts[hid], folded.get(hid), fp, consumer=consumer,
                                 closed_when=table.closed_when, excluded=excl.get(hid), reserved=hc.reserved_on(hid),
                                 require_axes=require_axes, admitted=used.get(hid, 0.0), now=now,
                                 stale_after_s=stale_after_s, mode="utilization",
                                 need=need,
                                 leases=hc.leases))
        else:
            rows.append(headroom(table.hosts[hid], folded.get(hid), fp, consumer=consumer,
                                 closed_when=table.closed_when, excluded=excl.get(hid), reserved=hc.reserved_on(hid),
                                 require_axes=require_axes, admitted=used.get(hid, 0.0),
                                 now=now, stale_after_s=stale_after_s))
        # ONE occupancy rule for the guest VM cap and the shared budget below: ``vm_reservations`` (admit(): the
        # ledger's live rows UNAGED), never the scorer's load1-aged ``reservations`` (Prism f01d48b6d5c4); under
        # utilization the leases replace the caller's reservations, so they are never summed on top of them
        # (admit() passes vm_reservations={}, Prism 3e890a858c01; direct callers too, Prism c-dd52632d2ecf3a32)
        booked = _booked_reservations(util, vm_reservations, ctx)
        vm = guest_vm_refusal(table.hosts[hid], ctx, need.cpus, reservations=booked) \
            if rows[-1].eligible else None
        if vm:                                     # the guest's own VM size caps it apart from the domain (t_7361659c)
            rows[-1].eligible, rows[-1].reason = False, vm
        if rows[-1].eligible:
            held = dict(booked)
            if util:    # the leases carry the ledger
                for lease in ctx.leases:
                    amount = Footprint(lease.need.ram_gb, lease.need.cpus)
                    held[lease.host] = held.get(lease.host, Footprint(0, 0)).plus(amount)
            budget = shared_budget_refusal(table, hid, Footprint(need.ram_gb, need.cpus), held)
            if budget:
                rows[-1].eligible, rows[-1].reason = False, budget
    eligible = [r for r in rows if r.eligible]
    # speed enters the CHOICE on both paths (R2c, D2; FWA F1 t_d456e9cd): under utilization the fraction is
    # admission.score()'s binding fraction, under legacy the v2 headroom fraction
    v2 = table.scorer == "v2"

    def done(d: Decision) -> Decision:
        d.scorer, d.mode = table.scorer, mode
        return d
    if not rows:
        return done(Decision(None, "no_candidates", rows, consumer, fp))
    if not eligible:
        why = "no_headroom" if any(r.reason.startswith("no_headroom") for r in rows) else "all_closed"
        if util:    # no host can EVER admit this need: say so by name, never a bare all_closed (Prism 1ea08e48d747)
            why = resource_fit_refusal(ctx.job_class, consumer, rows, table, folded) or why
        return done(Decision(None, why, rows, consumer, fp))
    by_id = {r.host: r for r in eligible}
    job = job or Job()
    if v2:
        # speed enters the CHOICE, never the gate: score = headroom_fraction x st_speed^alpha (R2c, D2)
        alpha = speed_alpha(job)
        for r in eligible:
            r.alpha = alpha
            r.score = r.fraction * (table.hosts[r.host].st_speed ** alpha)

    def pick(row: Headroom, rule: str, kept: bool = False) -> Decision:
        return done(Decision(row.host, "", rows, consumer, fp, kept_previous=kept, rule=rule))

    # layer 3: per-job overrides (an ineligible target falls through; the gate always wins)
    for field in ("affinity", "continuity"):
        want = table.resolve(getattr(job, field) or "")
        if want and want in by_id:
            return pick(by_id[want], field)
    core_class = job.core_class or ("fast" if job.priority is not None and job.priority <= 1 else None)
    if core_class == "fast":
        rule = "priority" if job.core_class is None else "core_class:fast"
        if v2 and any(table.hosts[r.host].st_speed_src == "measured" for r in eligible):
            # measured st_speed (alpha 1) replaces the lscpu clock; until R2b measures, the clock rule stands
            return pick(max(eligible, key=lambda r: (r.score, table.hosts[r.host].clock_mhz, r.host)), rule)
        best = max(eligible, key=lambda r: (table.hosts[r.host].clock_mhz, r.score, r.host))
        return pick(best, rule)
    if core_class == "wide":
        best = max(eligible, key=lambda r: (table.hosts[r.host].threads, r.score, r.host))
        return pick(best, "core_class:wide")
    # layer 2: saturate the default-first host while it is comfortable
    df = table.default_first
    if df and df in by_id:
        spec, row = table.hosts[df], by_id[df]
        # pending admissions count against the ceiling (Prism prism-router#511 838b2eb6); v2 measures the ceiling in
        # runnable work against cores x mt_yield, v1 in load against physical cores
        if util:
            # utilization: "comfortable" = measured busy CPUs + leases + this job <= saturate_to x physical cores
            smp = folded.get(df)
            busy = float(smp.cpu_busy) * float(smp.ncpu) if smp and _num(smp.cpu_busy) and _num(smp.ncpu) else None
            if busy is not None and busy + row.reserved_threads + (row.need.cpus if row.need else fp.threads) \
                    <= table.saturate_to * spec.cores:
                return pick(row, "default_first")
        elif v2:
            if row.occupancy is not None and row.occupancy + row.reserved_threads + fp.threads \
                    <= table.saturate_to * spec.cpu_capacity:
                return pick(row, "default_first")
        elif row.load is not None and row.load + row.reserved_threads + fp.threads <= table.saturate_to * spec.cores:
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



# ── utilization admission: kill switch, classes, leases, measured usage (t_da6f84f7) ─────────────
_MODE_CACHE: Dict[str, tuple] = {}
_ADMISSION_LINE = re.compile(r"^([ \t]+)admission:[ \t]*['\"]?([A-Za-z_-]*)['\"]?[ \t]*(?:#.*)?$")
_KEY_LINE = re.compile(r"^([ \t]+)[^ \t#]")
_FLOW_KANBAN = re.compile(r"^kanban:[ \t]*\{(.*)\}[ \t]*(?:#.*)?$")
_FLOW_ADMISSION = re.compile(r"(?:^|,)[ \t]*admission:[ \t]*['\"]?([A-Za-z_-]*)['\"]?[ \t]*(?:,|$)")


def _parse_admission(text: str, source: str = "config.yaml") -> str:
    """``kanban.admission`` from config.yaml text. PyYAML when importable (the same parse the other config readers
    do); else a stdlib reader of the top-level ``kanban:`` mapping's DIRECT child at whatever indent the mapping uses,
    and of the flow form ``kanban: {admission: x}`` (Prism 7ca95688be5a / 39c7eb0a3015). A file that does not parse
    reads legacy and says so on stderr (once per file version: admission_mode caches the verdict)."""
    try:
        import yaml  # noqa: PLC0415  (optional: launchd hooks run /usr/bin/python3 without it)
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            sys.stderr.write(f"placement: kanban.admission reads legacy: {source} does not parse ({first})\n")
            return "legacy"
        kb = doc.get("kanban") if isinstance(doc, dict) else None
        v = kb.get("admission") if isinstance(kb, dict) else None
        return v if isinstance(v, str) and v in ADMISSION_MODES else "legacy"
    inside, child = False, None
    for ln in text.splitlines():
        if ln and not ln[0].isspace() and not ln.startswith("#"):
            fm = _FLOW_KANBAN.match(ln)
            if fm:
                am = _FLOW_ADMISSION.search(fm.group(1))
                return am.group(1) if am and am.group(1) in ADMISSION_MODES else "legacy"
            inside, child = ln.split("#", 1)[0].rstrip() == "kanban:", None
            continue
        if not inside:
            continue
        km = _KEY_LINE.match(ln)
        if not km:
            continue
        if child is None:
            child = km.group(1)           # the first key fixes the mapping's direct-child indent
        m = _ADMISSION_LINE.match(ln)
        if m and m.group(1) == child:
            return m.group(2) if m.group(2) in ADMISSION_MODES else "legacy"
    return "legacy"


def admission_mode(path=None, *, root: Optional[Path] = None) -> str:
    """``kanban.admission`` of config.yaml: "utilization" or "legacy" (the default; absent, unreadable or any other
    value = legacy, so the kill switch fails to today's path). Read per call, cached on (mtime, size)."""
    p = Path(path) if path is not None else fleet_root(root=root) / CONFIG_REL
    try:
        st = p.stat()
    except OSError:
        return "legacy"
    key, sig = str(p), (st.st_mtime_ns, st.st_size)
    hit = _MODE_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        mode = _parse_admission(p.read_text(encoding="utf-8"), str(p))
    except ValueError as exc:   # undecodable bytes: a parse failure, as loud as a YAMLError (t_bc197d33)
        sys.stderr.write(f"placement: kanban.admission reads legacy: {p} does not parse ({exc})\n")
        mode = "legacy"
    except OSError:
        mode = "legacy"
    _MODE_CACHE[key] = (sig, mode)
    return mode


def class_usage_path(path=None, *, root: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else fleet_root(root=root) / CLASS_USAGE_REL


def job_class_for(consumer: str, job: Optional["Job"] = None) -> str:
    """The measured class a job's estimate is keyed on (work_estimate.job_class): Job.job_class, else the consumer's
    class, per profile for a kanban-family job that names one."""
    return work_estimate.job_class(consumer, explicit=job.job_class if job is not None else None,
                                   profile=job.profile if job is not None else None)


def held_leases(extra: Mapping[str, Footprint], table: Optional[HostTable] = None) -> List["admission.Lease"]:
    """A caller's held-back footprints (allocd's P0 reserve) as leases that never settle into the sample."""
    return [admission.Lease(host=(table.resolve(k) or k) if table else k, job_class="_held", need=need_of(v),
                            at=float("inf")) for k, v in (extra or {}).items()]


def ledger_leases(path=None, *, now: Optional[float] = None, table: Optional[HostTable] = None, root: Optional[Path] = None) -> List["admission.Lease"]:
    """Every LIVE reservation row as an admission.Lease (host canonical with ``table``): admission.admit() counts
    only those younger than the sample (``live_leases``), so a running job is never booked twice."""
    now = _now(now)
    out = []
    for row in _read_ledger(reservations_path(path, root=root))["rows"]:
        if not _row_live(row, now):
            continue
        host = (table.resolve(row["host"]) or row["host"]) if table else row["host"]
        need = admission.Estimate(cpus=float(row["threads"]), ram_gb=float(row["ram_gb"]),
                                  gpu_mib=float(row.get("gpu_mb") or 0.0), source="lease")
        out.append(admission.Lease(host=host, job_class=str(row.get("job_class") or job_class_for(row["consumer"])), need=need,
                                   at=float(row["at"]) if _num(row.get("at")) else float("inf"),
                                   job_id=row.get("job_id")))
    return out


# ── reservation ledger (A3): promises the sample cannot see yet ─────────────────────────────────
class OwnerMismatch(PlacementError):
    """renew()/release() with ``owner`` found the row booked by another consumer (checked under the ledger lock)."""


class LockTimeout(PlacementError):
    """The ledger flock was not acquired within ``lock_timeout_s`` (a wedged holder; Router Core t_552773f1)."""


_LEDGER_DIRS = contextvars.ContextVar("placement_ledger_directories", default={})


def _durability():
    try:
        from . import admission_demand
    except ImportError:
        import admission_demand
    return admission_demand


@contextlib.contextmanager
def _ledger_directory(path, *, root=None):
    path = Path(path).absolute()
    current = _LEDGER_DIRS.get().get(str(path.parent))
    if current is not None:
        yield current
        return
    root = fleet_root(root=root).absolute()
    with _durability().root_directory(root, path.parent.relative_to(root)) as directory:
        token = _LEDGER_DIRS.set({**_LEDGER_DIRS.get(), str(path.parent): directory})
        try:
            yield directory
        finally:
            _LEDGER_DIRS.reset(token)


@contextlib.contextmanager
def _locked(path: Path, timeout_s: Optional[float] = None, *, root=None):
    """LOCK_EX on <path>.lock. ``timeout_s`` None = block (every existing caller); a number = LOCK_NB polled until
    the deadline, then LockTimeout (alloc_client's fallback must answer, never hang behind a wedged allocd)."""
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover
        raise LedgerError("durable allocation storage requires flock") from exc
    with _ledger_directory(path, root=root) as directory, (directory / (path.name + ".lock")).open("a+") as fh:
        t_wait = time.monotonic()
        with _durability()._stage("ledger_flock_wait"):   # t_2f30f5b5: only the wait; the caller keeps its label
            if timeout_s is None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + max(0.0, float(timeout_s))
                while True:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LockTimeout(f"{path}.lock: held past {timeout_s:.2f}s") from None
                        time.sleep(0.02)
        t_held = time.monotonic()
        try:
            yield
            directory.validate()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            # t_1d801469: name the holder (route, consumer, caller, pid) of every long hold, in any process
            _durability()._note_hold("ledger_flock", Path(path).absolute().parent / "flock-holds.jsonl",
                                     t_held - t_wait, time.monotonic() - t_held)


def _read_ledger(path: Path) -> dict:
    """The ledger document. A MISSING file is an empty ledger; anything else that fails is LedgerError (a
    reserve()/release() must never replace a ledger it could not read: that drops live reservations)."""
    try:
        bound = _LEDGER_DIRS.get().get(str(Path(path).absolute().parent))
        text = (bound / path.name).read_text() if bound is not None else path.read_text(encoding="utf-8")
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
    for k in ("scratch_gb", "gpu_mb"):       # optional: rows written before 2.3 carry neither
        if k in row and (not _num(row[k]) or row[k] < 0):
            return f"{k} must be a finite number >= 0, not {row[k]!r}"
    if not _num(row.get("until")):
        return f"until must be a finite epoch number, not {row.get('until')!r}"
    if "at" in row and row["at"] is not None and not _num(row["at"]):
        return f"at must be a finite epoch number, not {row['at']!r}"
    if "released" in row and not isinstance(row["released"], bool):
        return f"released must be a bool, not {row['released']!r}"
    return ""


def _write_ledger(path: Path, doc: dict, *, root=None) -> None:
    """Atomic AND durable: the temp file is fsync'd before the rename and the directory after it, so a power
    loss right after a remote job launched cannot forget the reservation it was launched on (Prism 9da46cee)."""
    with _ledger_directory(path, root=root) as directory:
        with _durability()._stage("store_serialize"):
            data = json.dumps(doc, sort_keys=True)
        directory.replace(path.name, data)


def _row_live(row: Mapping, now: float) -> bool:
    return _num(row.get("until")) and float(row["until"]) > now and not row.get("released")


def reservations_path(path=None, *, root: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else fleet_root(root=root) / RESERVATIONS_REL


def admissions_path_for(ledger: Path, admissions_path=None) -> Path:
    """The admissions ledger: as given, else next to the reservation ledger (the default ledger's sibling is
    var/placement/admissions.jsonl), so a reservation and its ledger lines share one state root."""
    return Path(admissions_path) if admissions_path is not None else Path(ledger).parent / ADMISSIONS_REL.name


def _row_footprint(row: Mapping) -> Footprint:
    return Footprint(*(float(row.get(k) or 0.0) for k in FOOTPRINT_KEYS))


def _append_admissions(ap: Path, lines: List[dict], *, root=None) -> None:
    """Append lifecycle lines DURABLY (fsync, Prism d91b204660f6) right after the fsync'd ledger write they describe.
    A failure is reported on stderr, never raised: the reservation change it describes already committed, and the
    reservation ledger (not this audit journal) is what admission accounting reads."""
    if not lines:
        return
    try:
        stage = _durability()._stage
        with _ledger_directory(ap, root=root) as directory, (directory / ap.name).open("a") as fh:
            # t_2f30f5b5: the append and its fsyncs are their own stages (admissions.jsonl is ~0.5 GB on the Studio)
            with stage("admissions_write"):
                fh.write("".join(json.dumps(x, sort_keys=True) + "\n" for x in lines))
                fh.flush()
            with stage("admissions_fsync"):
                os.fsync(fh.fileno())
                os.fsync(directory.fd)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"placement: admissions journal {ap} not written ({len(lines)} line(s) lost): {exc}\n")


def _ledger_line(*, at: float, host, consumer: str, job_id, fp: Footprint, decision: str, reason: Optional[str],
                 band: Optional[str] = None, reservation: Optional[str] = None, **extra) -> dict:
    """ONE admissions.jsonl row (allocation 2.3): at, host, consumer, job id, footprint, band at admission,
    decision, reason. ``ram_gb`` / ``threads`` stay top-level for rows read before 2.3."""
    return {"at": at, "host": host, "consumer": consumer, "job_id": job_id, "footprint": dataclasses.asdict(fp),
            "ram_gb": fp.ram_gb, "threads": fp.threads, "band": band, "decision": decision, "reason": reason,
            "reservation": reservation, **extra}


def _prune(rows: List[dict], now: float) -> tuple:
    """(live rows, ``expired`` ledger lines for rows whose TTL lapsed unreleased): the reaper, like claim_expires.
    A released row was already logged by release()."""
    live, gone = [], []
    for r in rows:
        if _row_live(r, now):
            live.append(r)
        elif not r.get("released"):
            gone.append(_ledger_line(at=now, host=r["host"], consumer=r["consumer"], job_id=r.get("job_id"),
                                     fp=_row_footprint(r), decision="expired", reservation=r["id"],
                                     reason=f"ttl lapsed at {float(r['until']):.0f} without renew/release"))
    return live, gone


def live_reservations(path=None, *, now: Optional[float] = None, table: Optional[HostTable] = None,
                      cpu_seen_after_s: Optional[float] = None, root: Optional[Path] = None) -> Dict[str, Footprint]:
    """{host: summed Footprint} of every reservation whose TTL has not expired and that was not released.
    With ``table`` the keys are canonical host ids (aliases summed). Raises LedgerError on a corrupt ledger.
    ``cpu_seen_after_s`` (the scorer passes LOAD1_WINDOW_S): a row booked at least that long ago contributes
    0 threads, because its process is in the sampled load1 already (t_bfb88c23); a row with no ``at`` keeps
    its threads. Memory, scratch and gpu are never aged. None = every live row in full (the promise)."""
    now = _now(now)
    doc = _read_ledger(reservations_path(path, root=root))
    out: Dict[str, Footprint] = {}
    for row in doc["rows"]:          # every row validated by _read_ledger
        if _row_live(row, now):
            key = (table.resolve(row["host"]) or row["host"]) if table else row["host"]
            fp = _row_footprint(row)
            if cpu_seen_after_s is not None and _num(row.get("at")) and now - float(row["at"]) >= cpu_seen_after_s:
                fp = dataclasses.replace(fp, threads=0.0)
            prev = out.get(key)
            out[key] = prev.plus(fp) if prev else fp
    return out


def live_admissions(consumer: str, path=None, *, now: Optional[float] = None,
                    table: Optional[HostTable] = None, root: Optional[Path] = None) -> Dict[str, int]:
    """{host: number of LIVE reservations held by ``consumer``}: what a positive ``consumers.<c>.ceiling`` caps
    (Prism 696cce63). Same liveness and alias rules as live_reservations(); a row with no consumer is not counted."""
    now = _now(now)
    doc = _read_ledger(reservations_path(path, root=root))
    out: Dict[str, int] = {}
    for row in doc["rows"]:
        if _row_live(row, now) and row.get("consumer") == consumer:
            key = (table.resolve(row["host"]) or row["host"]) if table else row["host"]
            out[key] = out.get(key, 0) + 1
    return out


def reserve(host: str, consumer: str, fp: Footprint, *, ttl_s: float, job_id: Optional[str] = None,
            path=None, now: Optional[float] = None, admissions_path=None, decision: Optional[Decision] = None, root: Optional[Path] = None) -> str:
    """Record ``fp`` against ``host`` for ``ttl_s`` seconds; returns the reservation id. Expired and released
    rows are pruned on every write. The pick is also appended to the admissions ledger (one line per pick).
    The default ``now`` is taken AFTER the lock is held: a lock wait longer than ``ttl_s`` must not write an
    already-expired row (Prism fe666f92). An explicit ``now`` is kept as given."""
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    p = reservations_path(path, root=root)
    with _locked(p, root=root):
        now = _now(now)
        return _reserve_unlocked(p, host, consumer, fp, ttl_s=ttl_s, job_id=job_id, now=now,
                                 admissions_path=admissions_path, decision=decision, root=root)


def _reserve_unlocked(p: Path, host: str, consumer: str, fp: Footprint, *, ttl_s: float, job_id, now: float,
                      admissions_path, decision: Optional[Decision], job_class: Optional[str] = None,
                      reservation_id: Optional[str] = None, root=None) -> str:
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    if not isinstance(fp, Footprint):
        raise PlacementError("fp must be a Footprint")
    rid = reservation_id or uuid.uuid4().hex[:12]
    doc = _read_ledger(p)
    rows, expired = _prune(doc["rows"], now)
    row = {"id": rid, "host": host, "consumer": consumer, "job_id": job_id, "ram_gb": fp.ram_gb,
           "threads": fp.threads, "scratch_gb": fp.scratch_gb, "gpu_mb": fp.gpu_mb, "at": now, "until": now + float(ttl_s)}
    if job_class:
        row["job_class"] = job_class
    bad = _ledger_row_error(row)
    if bad:
        # never write a row the reader would reject: that would wedge every later admit/renew/release (Prism 0824b4b2)
        raise PlacementError(f"invalid reservation ({bad}); ledger left unchanged")
    rows.append(row)
    _write_ledger(p, {"schema": SCHEMA, "rows": rows}, root=root)
    chosen = next((r for r in decision.rows if r.host == host), None) if decision else None
    # admission_mode on every decision line, so a readback reads the mode off the ledger (t_f73c65a8); a pin booked
    # without a decision carries None (no mode decided it)
    extra = {"rule": decision.rule, "hosts": decision.as_row()["hosts"]} if decision else {}
    extra["admission_mode"] = (decision.mode or None) if decision else None
    _append_admissions(admissions_path_for(p, admissions_path), expired + [_ledger_line(
        at=now, host=host, consumer=consumer, job_id=job_id, fp=fp, decision="admitted", reservation=rid,
        band=band_of(chosen, fp) if chosen else None, ttl_s=float(ttl_s),
        reason=(f"{decision.rule}: fits with live reservations" if decision else "booked without a decision (pin)"),
        **extra)], root=root)
    return rid


# job -> (reason signature, at) of its last ``deferred`` line, per admissions file (one dispatcher = one process)
_DEFERRED_SEEN: Dict[tuple, tuple] = {}


def _deferred_line(ap: Path, d: Decision, *, job_id, now: float) -> List[dict]:
    """The ``deferred`` line for a refusal, or [] when this job already logged the same refusal within
    DEFERRED_REEMIT_S (a card waiting through 60 ticks is one line, not 60)."""
    why = "; ".join(f"{r.host} {r.reason}" for r in d.rows if not r.eligible)
    reason = f"{d.reason}: {why}" if why else d.reason
    # the signature is the refusal CLASS, not its magnitude: "no_headroom:cpu -2.0 cores" and "-12.0 cores" are the
    # same reason, or a storm re-logs every retry as the live load moves (t_39c8a948, drill D5)
    key, sig = (str(ap), d.consumer, job_id), re.sub(r"-?\d+(?:\.\d+)?", "#", reason)
    seen = _DEFERRED_SEEN.get(key)
    if job_id is not None and seen and seen[0] == sig and now - seen[1] < DEFERRED_REEMIT_S:
        return []
    if len(_DEFERRED_SEEN) > 4096:       # bounded: a long-lived dispatcher forgets, at worst re-logs once
        _DEFERRED_SEEN.clear()
    _DEFERRED_SEEN[key] = (sig, now)
    return [_ledger_line(at=now, host=None, consumer=d.consumer, job_id=job_id, fp=d.footprint, decision="deferred",
                         reason=reason, hosts=d.as_row()["hosts"], admission_mode=d.mode or None)]


def admit(table: HostTable, consumer: str, samples: Mapping[str, Optional[Sample]], *, ttl_s: float,
          job_id: Optional[str] = None, path=None, admissions_path=None, now: Optional[float] = None,
          root: Optional[Path] = None, **choose_kw):
    """ONE locked transaction: read the live ledger, choose(), reserve() (Prism P1 02405b39 / b7233dbf).
    Two dispatchers admitting concurrently serialize on the ledger lock, so the second sees the first's
    reservation. Returns (Decision, reservation id or None). ``choose_kw`` = choose()'s keyword arguments.
    The default ``now`` is resolved AFTER the lock is held (Prism fe666f92), and every sample's age is re-checked
    against it (``stale_after_s``, default 60 s; Prism baba105e): a sample that went stale while this admit waited
    on the lock closes its host instead of admitting on outdated capacity."""
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    stale_after_s = choose_kw.pop("stale_after_s", DEFAULT_STALE_AFTER_S)
    # Router Core (t_552773f1), both opt-in, default off = unchanged for every existing caller:
    # reuse_job: a live row of this (consumer, job_id) is returned as is (an admit whose reply was lost, asked again
    #   by allocd or by alloc_client's fallback on the same ledger, never books the job twice).
    # extra_reserved: {host: Footprint} held back for THIS decision only, never written (allocd's P0 reserve).
    # lock_timeout_s: bounded flock wait (LockTimeout on expiry) for alloc_client's fallback; None = block.
    reuse_job = bool(choose_kw.pop("reuse_job", False))
    extra_reserved = choose_kw.pop("extra_reserved", None) or {}
    lock_timeout_s = choose_kw.pop("lock_timeout_s", None)
    mode = choose_kw.pop("mode", None) or admission_mode(root=root)
    snapshot = history_snapshot(root=root) if mode == "utilization" and choose_kw.get("history") is None else None
    p = reservations_path(path, root=root)
    with _locked(p, lock_timeout_s, root=root):
        now = _now(now)
        if snapshot is not None:
            choose_kw["history"] = history_window(snapshot, now)
        if reuse_job and job_id is not None:
            for row in _read_ledger(p)["rows"]:
                # a coordination row (allocd /lease, host _fleet) is never work: a /admit whose job_id equals a fence
                # holder must not get the fence back (Prism b6542bbfc43b on #5643)
                if _row_live(row, now) and row["consumer"] == consumer and row.get("job_id") == job_id \
                        and row["host"] != COORDINATION_HOST:
                    return Decision(row["host"], "", [], consumer, _row_footprint(row), rule="reuse"), row["id"]
        resv = live_reservations(p, now=now, table=table, cpu_seen_after_s=LOAD1_WINDOW_S, root=root)
        for k, v in extra_reserved.items():
            hid = table.resolve(k) or k
            resv[hid] = resv[hid].plus(v) if hid in resv else v
        used = live_admissions(consumer, p, now=now, table=table, root=root)    # positive ceilings count live admissions (696cce63)
        if mode == "utilization":
            # the held-back reserve is a lease whether or not the caller supplied its own (Prism 8d9b939dcb9f)
            base = choose_kw.get("leases")
            choose_kw["leases"] = (list(base) if base is not None else ledger_leases(p, now=now, table=table, root=root)) \
                + held_leases(extra_reserved, table)
        # the guest VM cap's own occupancy: every live row in full (a settled job still holds its vCPUs). Under
        # utilization the leases already carry the ledger and the held-back reserve, so nothing is added twice
        vm_resv: Dict[str, Footprint] = {}
        if mode != "utilization":
            vm_resv = live_reservations(p, now=now, table=table, root=root)
            for k, v in extra_reserved.items():
                hid = table.resolve(k) or k
                vm_resv[hid] = vm_resv[hid].plus(v) if hid in vm_resv else v
        d = choose(table, consumer, samples, reservations=resv, admitted=used, now=now,
                   stale_after_s=stale_after_s, mode=mode, vm_reservations=vm_resv, **choose_kw, root=root)
        if not d.host:
            ap = admissions_path_for(p, admissions_path)
            _append_admissions(ap, _deferred_line(ap, d, job_id=job_id, now=now), root=root)
            return d, None
        fp_res, jclass = d.footprint, None
        row = next((r for r in d.rows if r.host == d.host), None)
        if mode == "utilization" and row is not None and row.need is not None:
            # the lease is the ESTIMATE for this class on this host, not the declared default (Ace 15:5x point 2)
            fp_res = Footprint(ram_gb=row.need.ram_gb, threads=row.need.cpus, scratch_gb=d.footprint.scratch_gb,
                               gpu_mb=row.need.gpu_mib)
            jclass = job_class_for(consumer, choose_kw.get("job"))
        rid = _reserve_unlocked(p, d.host, consumer, fp_res, ttl_s=ttl_s, job_id=job_id, now=now,
                                admissions_path=admissions_path, decision=d, job_class=jclass, root=root)
    return d, rid


def _check_owner(rows: List[dict], reservation_id: str, owner: Optional[str]) -> None:
    """``owner`` given: the row must be that consumer's, verified under the same lock as the mutation (Prism
    dc3cb6019576: an ownership read that failed earlier must never let alloc_client's fallback touch another
    consumer's row). An absent row is not a mismatch (the caller sees False)."""
    if owner is None:
        return
    for r in rows:
        if r.get("id") == reservation_id and r.get("consumer") != owner:
            raise OwnerMismatch(f"reservation {reservation_id} belongs to {r.get('consumer')!r}, not {owner!r}")


def restore_existing_unlocked(path: Path, authorized: Mapping, *, ttl_s: float, now: float,
                              sample=None, table=None, root=None):
    """Caller holds demand writer THEN ledger lock and validates durable authority.

    Existing use is accounted even with zero new-start headroom. Preserve original
    at (settled CPU age), exact footprint, owner and RID; never mint a new charge ID.
    """
    if _ledger_row_error(authorized) or not _num(ttl_s) or not 1 <= ttl_s <= 1800:
        raise PlacementError('invalid existing-work authority')
    doc = _read_ledger(path)
    rows, expired = _prune(doc['rows'], now)
    matches = [r for r in rows if r['id'] == authorized['id']
               or (r.get('consumer') == authorized['consumer'] and r.get('job_id') == authorized.get('job_id'))]
    if len(matches) > 1 or (matches and any(matches[0].get(k) != authorized.get(k)
            for k in ('id', 'host', 'consumer', 'job_id', 'threads', 'ram_gb', 'gpu_mb', 'scratch_gb'))):
        raise PlacementError('existing-work ledger identity conflict')
    row = matches[0] if matches else dict(authorized)
    if not matches:
        rows.append(row)
    row['until'] = max(float(row['until']), now + float(ttl_s))
    _write_ledger(path, {'schema': SCHEMA, 'rows': rows}, root=root)
    deficit = {}
    if sample is not None and table is not None:
        domain = domain_of(table, row['host'])
        booked = [r for r in rows if table.declared_id(r['host']) in domain]
        if _num(sample.mem_available_gb):
            deficit['ram_gb'] = max(0.0, sum(r['ram_gb'] for r in booked) - sample.mem_available_gb)
        if _num(sample.scratch_free_gb):
            deficit['scratch_gb'] = max(0.0, sum(r.get('scratch_gb', 0) for r in booked) - sample.scratch_free_gb)
        if _num(sample.cpu_busy) and _num(sample.ncpu):
            deficit['cpus'] = max(0.0, (sample.cpu_busy - admission.HOST_LINE) * sample.ncpu)
    _append_admissions(admissions_path_for(path), expired + [_ledger_line(
        at=now, host=row['host'], consumer=row['consumer'], job_id=row.get('job_id'),
        fp=_row_footprint(row), decision='reconciled', reservation=row['id'],
        reason='existing-work accounting restored from durable authorization',
        restored=not bool(matches), deficit=deficit or None)], root=root)
    return row, bool(matches)


def renew(reservation_id: str, *, ttl_s: float, path=None, now: Optional[float] = None, admissions_path=None,
          lock_timeout_s: Optional[float] = None, owner: Optional[str] = None, root: Optional[Path] = None) -> bool:
    """Extend a LIVE reservation to ``now + ttl_s`` (a lease the holder keeps renewing while its job runs, so a
    run longer than any fixed TTL stays counted; a holder that dies stops renewing and the row lapses; Prism
    796aae4f). False when the row is gone, expired or released (it is never resurrected)."""
    if not _num(ttl_s) or ttl_s <= 0:
        raise PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
    p = reservations_path(path, root=root)
    with _locked(p, lock_timeout_s, root=root):
        now = _now(now)
        doc = _read_ledger(p)
        _check_owner(doc["rows"], reservation_id, owner)
        rows, expired = _prune(doc["rows"], now)
        hit = False
        for r in rows:
            if r["id"] == reservation_id:
                r["until"] = max(float(r["until"]), now + float(ttl_s))
                hit = True
        if hit or expired:
            _write_ledger(p, {"schema": SCHEMA, "rows": rows}, root=root)
            _append_admissions(admissions_path_for(p, admissions_path), expired, root=root)
    return hit


def renew_many(items: List[tuple], *, path=None, now: Optional[float] = None, admissions_path=None,
               root: Optional[Path] = None) -> list:
    """t_2b90a362: renew() for a BATCH of ``(reservation_id, ttl_s, owner)`` under ONE ledger flock: one read, one
    durable (fsync'd) ledger write and one admissions append for the whole batch, so N concurrent renewals hold the
    flock once instead of N times. Each item gets exactly what renew() would have answered for it: True / False, or
    the exception it raised (an OwnerMismatch or a bad ttl_s is that item's alone and changes nothing for it). A
    ledger fault (LedgerError, OSError) raises for the whole batch, before anything is written."""
    out: list = [None] * len(items)
    todo = []
    for i, (rid, ttl_s, owner) in enumerate(items):
        if not _num(ttl_s) or ttl_s <= 0:
            out[i] = PlacementError(f"ttl_s must be a finite number > 0, not {ttl_s!r}")
        else:
            todo.append(i)
    if not todo:
        return out
    p = reservations_path(path, root=root)
    with _locked(p, root=root):
        now = _now(now)
        doc = _read_ledger(p)
        rows, expired = _prune(doc["rows"], now)
        hit = False
        for i in todo:
            rid, ttl_s, owner = items[i]
            try:
                _check_owner(doc["rows"], rid, owner)
            except OwnerMismatch as exc:
                out[i] = exc
                continue
            out[i] = False
            for r in rows:
                if r["id"] == rid:
                    r["until"] = max(float(r["until"]), now + float(ttl_s))
                    out[i] = hit = True
        if hit or expired:
            _write_ledger(p, {"schema": SCHEMA, "rows": rows}, root=root)
            _append_admissions(admissions_path_for(p, admissions_path), expired, root=root)
    return out


#: FWA instrumentation spec I-9 (plans/2026-10-19_fwa-instrumentation-SPEC.md, D-10): the (consumer, actual_source)
#: pairs whose producer measures the LEASE host. Anything else is refused: a Studio-side kanban measurement would
#: poison class-usage.jsonl for a pool-host lease (Momus CB-1). t_d0c8e8f0 (FWA gap 4) adds one producer per
#: consumer, each measured ON the host it ran on: kanban/athena = the worker's cgroup scope or its process-tree sum
#: sampled there (Q4-A), ci = the runner container's docker stats at exit, prism = the review arm's container.
ACTUAL_SOURCES = frozenset({("test-gate", "cgroup"),
                            ("kanban", "cgroup"), ("kanban", "pidtree"), ("athena", "cgroup"), ("athena", "pidtree"),
                            ("ci", "docker"), ("prism", "docker")})
#: The t_d0c8e8f0 producers run off the lease's own process (CB-1's shape), so their actual must NAME the host it was
#: measured on (``actual["host"]``) and that host must be the lease's: a measurement of another box is refused. The
#: test-gate cgroup producer is the remote agent inside the lease's own scope (I-9 v0.1) and keeps its contract.
HOST_BOUND_SOURCES = ACTUAL_SOURCES - {("test-gate", "cgroup")}


class ActualRefused(PlacementError):
    """release(actual=) with an actual_source the I-9 allow-list does not accept for the row's consumer."""


def _same_host(measured, lease_host) -> bool:
    """A host-bound actual names the host it was measured on; it must be the lease's host id (case-insensitive:
    a Linux hostname reads ACE-MEDIA for the table's ace-media). Absent / not a string = refused."""
    return isinstance(measured, str) and bool(measured) and measured.strip().lower() == str(lease_host or "").lower()


def release(reservation_id: str, *, root: Optional[Path] = None, **kwargs) -> bool:
    """Fence recoverable CI work before deleting its charge, even after ledger pruning.

    All release callers (HTTP and local fallback) use demand-writer -> ledger order.
    Fence-first failure leaves a conservative charge; a retry completes deletion.
    """
    import inspect
    inspect.signature(_release_ledger).bind(reservation_id, **kwargs)
    if kwargs.get('now') is not None:
        _now(kwargs['now'])  # reject invalid clocks before persisting retirement
    try:
        from . import admission_demand as dm
    except ImportError:
        import admission_demand as dm
    canonical = reservations_path(root=root).resolve()
    if kwargs.get('path') is not None and reservations_path(kwargs['path'], root=root).resolve() != canonical:
        return _release_ledger(reservation_id, **kwargs, root=root)
    kwargs['path'] = canonical
    if not (fleet_root(root=root) / dm.DEMAND_REL / 'ci.json').exists():
        return _release_ledger(reservation_id, **kwargs, root=root)
    with dm._writer(fleet_root(root=root), timeout_s=kwargs.get('lock_timeout_s')) as directory:
        p = directory / 'ci.json'
        stored = dm._existing(p, 'ci', fleet_root(root=root))
        records = dm.consumed_units(stored)
        hit = next((v for v in records.values() if v.get('reservation_id') == reservation_id
                    and isinstance(v.get('accounting'), dict)), None)
        if hit is not None:
            if kwargs.get('owner') != hit['accounting']['consumer']:
                raise OwnerMismatch('existing-work release requires original owner')
            if kwargs.get('actual') is not None:
                consumer = str(hit['accounting']['consumer']).split('@', 1)[0]
                source = kwargs.get('actual_source') or 'none'
                pair = (consumer, source)
                lease_host = hit['accounting'].get('host')
                if pair not in ACTUAL_SOURCES or (pair in HOST_BOUND_SOURCES
                                                  and not _same_host(kwargs['actual'].get('host'), lease_host)):
                    raise ActualRefused(f'actual_source {source} cannot measure host {lease_host} '
                                        f'for consumer {consumer}')
            hit['released'] = True
            stored['consumed'] = records
            dm._replace(p, stored)
        return _release_ledger(reservation_id, **kwargs, root=root)


def _release_ledger(reservation_id: str, *, path=None, now: Optional[float] = None, admissions_path=None,
            lock_timeout_s: Optional[float] = None, owner: Optional[str] = None, actual: Optional[Mapping] = None,
            usage_path=None, actual_source: Optional[str] = None, root: Optional[Path] = None) -> bool:
    """End a reservation (the job ended); True when it was live. Logged as a ``released`` ledger line.
    ``owner``: refuse (OwnerMismatch) a row booked by another consumer, checked under the lock.
    ``actual`` {cpus, ram_gb, gpu_mib}: the job's MEASURED peak (cgroup / pid tree), appended to the class-usage
    ledger next to the lease's estimate, so the next estimate() for (class, host) moves (Ace 15:5x point 4).
    ``actual_source``: who measured it; (row consumer, source) must be in ACTUAL_SOURCES (I-9), None = "none".
    A refused actual raises ActualRefused before anything is written (callers that must end the lease anyway
    release again without ``actual``: allocd and alloc_client do)."""
    p = reservations_path(path, root=root)
    with _locked(p, lock_timeout_s, root=root):
        now = _now(now)
        doc = _read_ledger(p)
        _check_owner(doc["rows"], reservation_id, owner)
        if actual is not None:
            src = actual_source or "none"
            for r in doc["rows"]:
                if r.get("id") != reservation_id:
                    continue
                pair = (str(r.get("consumer", "")).split("@", 1)[0], src)
                if pair not in ACTUAL_SOURCES or (pair in HOST_BOUND_SOURCES
                                                  and not _same_host(actual.get("host"), r.get("host"))):
                    raise ActualRefused(f"actual_source {src} cannot measure host {r.get('host')} "
                                        f"for consumer {r.get('consumer')}"
                                        + (f" (measured on {actual.get('host')!r})" if pair in ACTUAL_SOURCES else ""))
        live, lines = _prune(doc["rows"], now)
        rows, done = [], []
        for r in live:
            if r.get("id") == reservation_id:
                lines.append(_ledger_line(at=now, host=r["host"], consumer=r["consumer"], job_id=r.get("job_id"),
                                          fp=_row_footprint(r), decision="released", reservation=r["id"],
                                          reason=f"held {now - float(r.get('at') or now):.0f}s"))
                done.append(r)
                continue
            rows.append(r)
        _write_ledger(p, {"schema": SCHEMA, "rows": rows}, root=root)
        _append_admissions(admissions_path_for(p, admissions_path), lines, root=root)
        # the measurement only after the release committed: a failed ledger write must not leave a usage row
        # that a retried release() appends again (Prism ca81355a9fd0 / f5f4e1fe5c62)
        for r in done:
            if actual:
                record_usage(r, actual, now=now, path=usage_path, root=root)
    return len(rows) < len(live)


def record_usage(row: Mapping, actual: Mapping, *, now: float, path=None, root: Optional[Path] = None) -> Optional[dict]:
    """One measured peak for a ledger row's (class, host), with the row's lease as the estimate. A malformed
    ``actual`` is reported on stderr and skipped: the release it rides on already committed."""
    try:
        est = admission.Estimate(cpus=float(row.get("threads") or 0), ram_gb=float(row.get("ram_gb") or 0),
                                 gpu_mib=float(row.get("gpu_mb") or 0), source="lease")
        # every axis the lease estimates must be measured: an unmeasured axis is never recorded as 0 (Prism
        # dd0cbea04139); gpu_mib absent = no GPU only when the lease estimated none (t_bc197d33)
        missing = [k for k in ("cpus", "ram_gb") + (("gpu_mib",) if est.gpu_mib > 0 else ()) if actual.get(k) is None]
        if missing:
            raise ValueError(f"actual is missing {','.join(missing)}: {dict(actual)!r}")
        vals = {k: actual[k] for k in ("cpus", "ram_gb")}
        vals["gpu_mib"] = 0.0 if actual.get("gpu_mib") is None else actual["gpu_mib"]
        # numbers only: float() would take JSON true as 1.0 and "2.5" as 2.5 (t_53e2d32c)
        if not all(_num(v) and v >= 0 for v in vals.values()):
            raise ValueError(f"actual must be finite numbers >= 0, not {dict(actual)!r}")
        vals = {k: float(v) for k, v in vals.items()}
        # the run's cpu-seconds / wall seconds (work_estimate's cpu_s / wall_s axes; a Prism review sends both,
        # t_e3260e9f): finite numbers >= 0 only, absent or malformed = not measured
        runs = {k: float(actual[k]) for k in ("cpu_s", "wall_s")
                if _num(actual.get(k)) and float(actual[k]) >= 0}
        # a door row's consumer is the pair identity <workload>@<host>: its class is the workload's (t_d0c8e8f0)
        klass = str(row.get("job_class") or job_class_for(str(row["consumer"]).split("@", 1)[0]))
        return admission.record_actual(class_usage_path(path, root=root), job_class=klass,
                                       host=row["host"], at=now, job_id=row.get("job_id"), estimated=est, **vals, **runs)
    except (OSError, ValueError, TypeError) as exc:
        sys.stderr.write(f"placement: class usage not recorded for {row.get('id')}: {exc}\n")
        return None


def reap(*, path=None, now: Optional[float] = None, admissions_path=None, root: Optional[Path] = None) -> int:
    """Drop every reservation whose TTL lapsed (its holder died without release) and log each as ``expired``;
    the number reaped. Idle ledgers are reaped by the holder's tick, not only by the next admit."""
    p = reservations_path(path, root=root)
    with _locked(p, root=root):
        now = _now(now)
        doc = _read_ledger(p)
        rows, expired = _prune(doc["rows"], now)
        if len(rows) != len(doc["rows"]):
            _write_ledger(p, {"schema": SCHEMA, "rows": rows}, root=root)
            _append_admissions(admissions_path_for(p, admissions_path), expired, root=root)
    return len(expired)


def fmt_load(load: Optional[float], cores: int, threads: int) -> str:
    """Ace's notation: `N / Cc.Tt`."""
    return f"{'?' if load is None else '%g' % load} / {cores}c.{threads}t"
