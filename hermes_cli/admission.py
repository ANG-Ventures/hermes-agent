"""admission: ONE admission predicate for every consumer on every host (t_dc960eab R2; Ace 2026-10-18 13:13 / 14:33 /
15:4x / 15:5x). Imported by placement/allocd (kanban, test-gate, Prism) and by the CI runner allocator (t_5b8758f3
item A). Pure: no I/O except the two small history helpers at the bottom. No slots, no per-worker constant.

    "It asks whether there's room for THIS thing and it ESTIMATES how much that thing will take." (Ace 15:5x)

admit(sample, need, leases) -> Verdict
  sample   one host row of the pushed utilization vector (scripts/host-pressure.py / `fleet-host busy --json`):
           at, os, ncpu, cpu_busy (0..1, /proc/stat or host_statistics delta; never load1), mem_available_gb,
           psi_cpu_some10, psi_mem_some10, psi_io_some10 (Linux; None on darwin), optional procs/maxproc,
           gpu_free_mib, slo_breach (a latency-SLO guard, e.g. the ACE-AI voice hub: True = saturated).
  need     Estimate for THIS job class on THIS host: cpus, ram_gb, gpu_mib (from work_estimate.estimate(), the ONE
           estimator every consumer uses (t_9e395789); never a constant).
  leases   estimates already admitted on this host whose job the sample does not show yet (Lease.at > sample.at -
           settle_s): the lease is the ONLY stand-in for a count (doctrine rule 3/4).
Every axis must have room; the first saturated axis is `binding`. An axis the host's OS cannot measure (PSI on
darwin) is skipped; any other unmeasured required axis REFUSES (fail closed, `unmeasured:<axis>`).

Limits are utilization fractions / pressure percentages per host (Limits), not counts. `cpu_saturable` (ci-box,
ace-ai, ace-media today) makes the CPU axis a score penalty instead of a refusal; memory, PSI mem/io, procs, GPU and
the SLO guard still refuse.

A consumer that runs inside its own cgroup (kanban workers in kanbanw.slice: ci-box MemoryHigh 56G / MemoryMax 60G,
ace-ai / ace-media 24G) passes ``slice_mem`` (read_slice_memory()): the slice is a second memory axis, free =
slice limit - slice memory.current - live leases of the slice's classes - this job, so admission refuses at the cap
the kernel would enforce, not at the (much larger) host MemAvailable bound (Prism 063a212ff63e).

score(sample, need, leases) orders admitted hosts (Ace's (b): spread by free capacity, never by load-vs-cores): the
binding free fraction after this job, PSI cpu as a penalty.
"""
from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

SCHEMA = 1
DEFAULT_SETTLE_S = 90.0     # a lease older than (sample.at - this) is assumed inside the sample (2 x 30 s sampler + slack)
DARWIN_UNMEASURABLE = ("psi_cpu_some10", "psi_mem_some10", "psi_io_some10")


@dataclasses.dataclass(frozen=True)
class Estimate:
    cpus: float = 0.0          # CPUs the job keeps busy at its p90 (1.0 = one CPU fully busy)
    ram_gb: float = 0.0
    gpu_mib: float = 0.0
    source: str = "prior"      # work_estimate: "p75:<class>@<host> n=<k>" | "p75:<class>@fleet n=<k>" | "seed"
    cpu_s: Optional[float] = None    # expected cpu-seconds per run (work_estimate; not an admit() axis)
    wall_s: Optional[float] = None   # expected wall seconds per run (work_estimate; not an admit() axis)

    def plus(self, o: "Estimate") -> "Estimate":
        return Estimate(self.cpus + o.cpus, self.ram_gb + o.ram_gb, self.gpu_mib + o.gpu_mib, "sum")


@dataclasses.dataclass(frozen=True)
class Lease:
    host: str
    job_class: str
    need: Estimate
    at: float                  # admit time
    job_id: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Limits:
    cpu_busy_max: float = 0.90         # fraction of all CPUs busy after this job
    mem_reserve_gb: float = 0.0        # held back for residents (the host table's mem_reserve_gb)
    psi_cpu_max: float = 40.0          # PSI some avg10 %, a refusal unless cpu_saturable
    psi_cpu60_max: float = 25.0        # PSI cpu some avg60 % (FWA R2c), a refusal when MEASURED, unless cpu_saturable
    psi_mem_max: float = 20.0          # 14:33 readback: refuse at PSI mem some > 20 % even at low CPU
    psi_io_max: float = 30.0
    procs_frac_max: float = 0.85       # procs / maxproc
    cpu_saturable: bool = False        # CPU over the line = score penalty, never a refusal
    unmeasurable: tuple = ()           # axes this host's OS cannot report (never required)


@dataclasses.dataclass(frozen=True)
class SliceMem:
    """The consumer's own cgroup memory cap, sampled with the host sample (read_slice_memory())."""
    name: str                          # e.g. "kanbanw.slice"
    limit_gb: float                    # the lower of the finite MemoryHigh / MemoryMax
    current_gb: Optional[float]        # the slice's memory.current at the sample; None = unreadable (admit refuses)
    classes: tuple = ("kanban",)       # job_class prefixes whose leases live inside this slice


@dataclasses.dataclass(frozen=True)
class Verdict:
    ok: bool
    binding: str               # the axis that refused, or the tightest axis when admitted
    reason: str
    free: Mapping[str, float]  # per-axis headroom AFTER leases + this job (fraction or GB/MiB, axis-named)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def live_leases(leases: Iterable[Lease], host: str, sample_at: Optional[float], *,
                settle_s: float = DEFAULT_SETTLE_S) -> Estimate:
    """Sum of the leases on ``host`` the sample cannot show yet. A lease admitted before (sample.at - settle_s) is in
    the sample already; counting it again would double-book the job (the load1 double-count of t_bfb88c23)."""
    tot = Estimate()
    for le in leases:
        if le.host != host:
            continue
        if sample_at is None or le.at > float(sample_at) - settle_s:
            tot = tot.plus(le.need)
    return tot


def admit(sample: Mapping, need: Estimate, leases: Iterable[Lease] = (), *, host: Optional[str] = None,
          limits: Limits = Limits(), settle_s: float = DEFAULT_SETTLE_S,
          slice_mem: Optional[SliceMem] = None) -> Verdict:
    host = host or str(sample.get("host") or "")
    if not host:                           # no host: no lease can match, so every in-flight lease would be ignored
        return Verdict(False, "host", "unmeasured:host", {})      # fail closed (Prism 5702b20dda6c)
    unmeasurable = set(limits.unmeasurable) | (set(DARWIN_UNMEASURABLE) if sample.get("os") == "darwin" else set())
    leases = list(leases)
    held = live_leases(leases, host, sample.get("at"), settle_s=settle_s)
    free: dict = {}
    if slice_mem is not None:
        if not (_num(slice_mem.limit_gb) and _num(slice_mem.current_gb)):
            return Verdict(False, "slice_mem_gb", f"unmeasured:slice_mem_gb ({slice_mem.name})", free)
        inside = [le for le in leases if any(le.job_class.startswith(c) for c in slice_mem.classes)]
        held_slice = live_leases(inside, host, sample.get("at"), settle_s=settle_s)
        free["slice_mem_gb"] = (float(slice_mem.limit_gb) - float(slice_mem.current_gb)
                                - held_slice.ram_gb - need.ram_gb)

    def missing(axis):
        return Verdict(False, axis, f"unmeasured:{axis}", free)

    # memory: available - residents' reserve - live leases - this job
    if not _num(sample.get("mem_available_gb")):
        return missing("mem_available_gb")
    free["mem_gb"] = float(sample["mem_available_gb"]) - limits.mem_reserve_gb - held.ram_gb - need.ram_gb
    # cpu: measured busy fraction + leases + this job, over all CPUs (never load1, never a core count)
    ncpu = sample.get("ncpu")
    if not _num(ncpu) or ncpu <= 0:
        return missing("ncpu")
    if not _num(sample.get("cpu_busy")):
        return missing("cpu_busy")
    busy_after = float(sample["cpu_busy"]) + (held.cpus + need.cpus) / float(ncpu)
    free["cpu"] = limits.cpu_busy_max - busy_after
    for axis, lim in (("psi_mem_some10", limits.psi_mem_max), ("psi_io_some10", limits.psi_io_max),
                      ("psi_cpu_some10", limits.psi_cpu_max)):
        if axis in unmeasurable:
            continue
        if not _num(sample.get(axis)):
            return missing(axis)
        free[axis] = lim - float(sample[axis])
    # R2c avg60 stall closure (t_d456e9cd): read when the sampler pushed it; a pre-avg60 sample is not refused for it
    if "psi_cpu_some10" not in unmeasurable and _num(sample.get("psi_cpu_some60")):
        free["psi_cpu_some60"] = limits.psi_cpu60_max - float(sample["psi_cpu_some60"])
    if _num(sample.get("procs")) and _num(sample.get("maxproc")) and sample["maxproc"] > 0:
        free["procs"] = limits.procs_frac_max - float(sample["procs"]) / float(sample["maxproc"])
    if need.gpu_mib > 0:
        if not _num(sample.get("gpu_free_mib")):
            return missing("gpu_free_mib")
        free["gpu_mib"] = float(sample["gpu_free_mib"]) - held.gpu_mib - need.gpu_mib

    if sample.get("slo_breach") is True:
        return Verdict(False, "slo", "saturated:slo (latency guard breached)", free)
    order = ("slice_mem_gb", "mem_gb", "psi_mem_some10", "psi_io_some10", "procs", "gpu_mib", "psi_cpu_some10",
             "psi_cpu_some60", "cpu")
    for axis in order:
        # PSI cpu closes AT its line (R2c: avg10 >= 40 OR avg60 >= 25, as closed_when does on the legacy path)
        if axis not in free or free[axis] > 0 or (free[axis] == 0 and axis not in ("psi_cpu_some10", "psi_cpu_some60")):
            continue
        if axis in ("cpu", "psi_cpu_some10", "psi_cpu_some60") and limits.cpu_saturable:
            continue                       # a score penalty on a saturable host (score() reads it), never a refusal
        return Verdict(False, axis, f"saturated:{axis} {free[axis]:+.2f}", free)
    binding = min(_fractions(free, sample, limits), key=lambda kv: kv[1])[0]
    return Verdict(True, binding, "", free)


def _fractions(free: Mapping[str, float], sample: Mapping, limits: Limits):
    """Every axis's headroom on one 0..1-ish scale, so the tightest one names `binding` and drives score()."""
    out = [("cpu", free["cpu"])]
    tot = sample.get("mem_total_gb")
    out.append(("mem_gb", free["mem_gb"] / float(tot) if _num(tot) and tot > 0 else free["mem_gb"] / 100.0))
    for axis, lim in (("psi_mem_some10", limits.psi_mem_max), ("psi_io_some10", limits.psi_io_max),
                      ("psi_cpu_some10", limits.psi_cpu_max), ("psi_cpu_some60", limits.psi_cpu60_max)):
        if axis in free and lim > 0:
            out.append((axis, free[axis] / lim))
    if "procs" in free:
        out.append(("procs", free["procs"]))
    if "slice_mem_gb" in free:
        out.append(("slice_mem_gb", free["slice_mem_gb"] / 100.0))
    return out


def score(sample: Mapping, need: Estimate, leases: Iterable[Lease] = (), *, host: Optional[str] = None,
          limits: Limits = Limits(), settle_s: float = DEFAULT_SETTLE_S,
          slice_mem: Optional[SliceMem] = None) -> Optional[float]:
    """Higher = more room left after this job (the binding fraction). None = not admitted. On a saturable host a
    negative CPU fraction stays in the min(), so an oversubscribed box ranks below every host with room."""
    v = admit(sample, need, leases, host=host, limits=limits, settle_s=settle_s, slice_mem=slice_mem)
    if not v.ok:
        return None
    return min(f for _, f in _fractions(v.free, sample, limits))


_GB = float(1 << 30)


def _cg_bytes(path: Path) -> Optional[float]:
    """A cgroup v2 memory file: a byte count, or None for "max" / absent / unreadable."""
    try:
        raw = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return float(int(raw))
    except ValueError:
        return None


def read_slice_memory(name: str = "kanbanw.slice", *, cgroup_root="/sys/fs/cgroup",
                      classes: tuple = ("kanban",)) -> Optional[SliceMem]:
    """The slice's memory cap + use from cgroup v2 (the lower finite one of memory.high / memory.max;
    memory.current; Prism 8081ac5d7bce). None when the
    slice does not exist or has no finite cap (no slice axis: the host axis alone applies). A finite cap with an
    unreadable memory.current keeps the axis with ``current_gb=None``, so admit() refuses ``unmeasured:slice_mem_gb``
    instead of dropping the slice (fail closed, Prism b137e9c52918)."""
    d = Path(cgroup_root) / name
    cur = _cg_bytes(d / "memory.current")
    high, mx = _cg_bytes(d / "memory.high"), _cg_bytes(d / "memory.max")
    finite = [x for x in (high, mx) if x is not None]
    limit = min(finite) if finite else None
    if limit is None:
        return None
    return SliceMem(name=name, limit_gb=limit / _GB, current_gb=None if cur is None else cur / _GB,
                    classes=tuple(classes))


# ── the estimate-vs-actual ledger (the estimator itself is scripts/lib/work_estimate.py, t_9e395789) ──────────────
def record_actual(path, *, job_class: str, host: str, cpus: float, ram_gb: float, gpu_mib: float = 0.0,
                  at: float, job_id: Optional[str] = None, estimated: Optional[Estimate] = None,
                  cpu_s: Optional[float] = None, wall_s: Optional[float] = None) -> dict:
    """Append one measured peak to the history ledger (jsonl). ``estimated`` is kept on the row so the FWA status page
    can show estimate-vs-actual per class (a bad estimate is visible, not silent). ``cpu_s`` / ``wall_s`` (optional:
    the run's cpu-seconds and wall seconds) feed work_estimate's cpu_s / wall_s; absent = not measured, never 0."""
    row = {"schema": SCHEMA, "class": job_class, "host": host, "cpus": round(float(cpus), 3),
           "ram_gb": round(float(ram_gb), 3), "gpu_mib": round(float(gpu_mib), 1), "at": float(at), "job_id": job_id}
    for k, v in (("cpu_s", cpu_s), ("wall_s", wall_s)):
        if v is not None:
            row[k] = round(float(v), 3)
    if estimated is not None:
        row["estimated"] = {"cpus": estimated.cpus, "ram_gb": estimated.ram_gb, "gpu_mib": estimated.gpu_mib,
                            "source": estimated.source}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("ab+") as fh:
        # a torn last line (a writer died mid-row) must not swallow this row: start on a fresh line (Prism bcbf705a6bab)
        lead = b""
        fh.seek(0, 2)
        if fh.tell() > 0:
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                lead = b"\n"
        fh.write(lead + (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    return row


def read_history(path, *, since: Optional[float] = None) -> List[dict]:
    """Rows of the history ledger; a torn last line or a foreign row is skipped (append-only, never fatal)."""
    out: List[dict] = []
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
            if not (isinstance(r, dict) and r.get("schema") == SCHEMA):
                continue
            if since is not None and float(r.get("at") or 0) < since:
                continue
        except (ValueError, TypeError):
            continue                       # torn line or a bad `at` (Prism feea7aeecbda): skip the row, never fatal
        out.append(r)
    return out
