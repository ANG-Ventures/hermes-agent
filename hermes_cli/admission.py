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
import os
import threading
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
    # an unmeasured axis is noted and the rest still computed: a MEASURED saturation names the refusal ahead of it,
    # so the band reads hot, not unknown, when memory refuses and cpu_busy is missing (t_f64c6d34, Prism d60feae165ed)
    unmeasured: list = []
    if slice_mem is not None:
        if not (_num(slice_mem.limit_gb) and _num(slice_mem.current_gb)):
            unmeasured.append(("slice_mem_gb", f"unmeasured:slice_mem_gb ({slice_mem.name})"))
        else:
            inside = [le for le in leases if any(le.job_class.startswith(c) for c in slice_mem.classes)]
            held_slice = live_leases(inside, host, sample.get("at"), settle_s=settle_s)
            free["slice_mem_gb"] = (float(slice_mem.limit_gb) - float(slice_mem.current_gb)
                                    - held_slice.ram_gb - need.ram_gb)

    def missing(axis):
        unmeasured.append((axis, f"unmeasured:{axis}"))

    # memory: available - residents' reserve - live leases - this job
    if not _num(sample.get("mem_available_gb")):
        missing("mem_available_gb")
    else:
        free["mem_gb"] = float(sample["mem_available_gb"]) - limits.mem_reserve_gb - held.ram_gb - need.ram_gb
    # cpu: measured busy fraction + leases + this job, over all CPUs (never load1, never a core count)
    ncpu = sample.get("ncpu")
    if not _num(ncpu) or ncpu <= 0:
        missing("ncpu")
    elif not _num(sample.get("cpu_busy")):
        missing("cpu_busy")
    else:
        busy_after = float(sample["cpu_busy"]) + (held.cpus + need.cpus) / float(ncpu)
        free["cpu"] = limits.cpu_busy_max - busy_after
    for axis, lim in (("psi_mem_some10", limits.psi_mem_max), ("psi_io_some10", limits.psi_io_max),
                      ("psi_cpu_some10", limits.psi_cpu_max)):
        if axis in unmeasurable:
            continue
        if not _num(sample.get(axis)):
            missing(axis)
            continue
        free[axis] = lim - float(sample[axis])
    # R2c avg60 stall closure (t_d456e9cd): read when the sampler pushed it; a pre-avg60 sample is not refused for it
    if not {"psi_cpu_some10", "psi_cpu_some60"} & unmeasurable and _num(sample.get("psi_cpu_some60")):
        free["psi_cpu_some60"] = limits.psi_cpu60_max - float(sample["psi_cpu_some60"])
    if _num(sample.get("procs")) and _num(sample.get("maxproc")) and sample["maxproc"] > 0:
        free["procs"] = limits.procs_frac_max - float(sample["procs"]) / float(sample["maxproc"])
    if need.gpu_mib > 0:
        if not _num(sample.get("gpu_free_mib")):
            missing("gpu_free_mib")
        else:
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
    if unmeasured:                         # nothing measured refuses: fail closed on the first unmeasured axis
        return Verdict(False, unmeasured[0][0], unmeasured[0][1], free)
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


# ── ONE rule for every workload class: one line, priority by demand (t_e5124426; Ace 2026-10-27 02:5x) ───────────
# "It shouldn't just be one admission rule for both. It should be one admission rule for everything ... also combine
# workers as well and any other kind of workers." Every class (CI runner, Prism review, kanban worker, test-gate run,
# batch) is admitted by admit_class() on the SAME measured axes against the SAME host line; no class gets a private
# metric (load1, procs_running x yield) or a cpu_saturable escape. Higher-priority DEMAND, not a reserved lane or a
# floor (Ace 10-25/10-26), is what holds lower classes back: while a higher class has queued work for this host, each
# lower class sees that queue's next few units as a lease, so it stops short of the line and leaves them room.
HOST_LINE = 0.90             # Ace standing ruling: no host above 90 % busy, whatever the mix
DEFAULT_ORDER = ("main", "prism", "interactive", "kanban", "ci", "test-gate", "batch")
DEFAULT_HOLDBACK_UNITS = 2   # "a few slots": per higher class with queued demand, at most this many of its units


@dataclasses.dataclass(frozen=True)
class Priority:
    order: tuple = DEFAULT_ORDER       # highest first; a class not named here ranks below every named one
    line: float = HOST_LINE            # cpu_busy_max for every class
    holdback_units: int = DEFAULT_HOLDBACK_UNITS

    def rank(self, workload: str) -> int:
        try:
            return self.order.index(workload)
        except ValueError:
            return len(self.order)


@dataclasses.dataclass(frozen=True)
class Demand:
    """Queued (not yet started) work of one class that could run on this host, and one unit's estimate there."""
    workload: str
    queued: int
    unit: Estimate


def holdback(workload: str, demand: Iterable[Demand], priority: Priority, host: str) -> List[Lease]:
    """Leases standing in for the queued work of every class that outranks ``workload``. They never settle (at=inf):
    queued work is not in any sample. An idle higher class (queued 0) holds nothing back."""
    mine, out = priority.rank(workload), []
    for d in demand:
        if d.queued <= 0 or priority.rank(d.workload) >= mine or priority.holdback_units <= 0:
            continue
        n = min(int(d.queued), priority.holdback_units)
        u = d.unit
        out.append(Lease(host, f"holdback:{d.workload}",
                         Estimate(u.cpus * n, u.ram_gb * n, u.gpu_mib * n, f"holdback {d.workload} x{n}"),
                         at=math.inf, job_id=f"queued:{d.workload}:{int(d.queued)}"))
    return out


def admit_class(sample: Mapping, workload: str, need: Estimate, leases: Iterable[Lease] = (), *,
                demand: Iterable[Demand] = (), priority: Priority = Priority(), limits: Limits = Limits(),
                host: Optional[str] = None, settle_s: float = DEFAULT_SETTLE_S,
                slice_mem: Optional[SliceMem] = None) -> Verdict:
    """THE admission decision for any workload class on any host. ``limits`` supplies the host's own facts
    (mem_reserve_gb, unmeasurable axes); the CPU line is ``priority.line`` and the CPU / PSI cpu axes always refuse
    (cpu_saturable is ignored: a class that can run past the line is how one decider starves the others).
    A refusal caused only by higher-priority demand reads ``held_back:<class> ...`` with binding ``priority``."""
    host = host or str(sample.get("host") or "")
    lim = dataclasses.replace(limits, cpu_busy_max=priority.line, cpu_saturable=False)
    leases = list(leases)
    demand = list(demand)
    held = holdback(workload, demand, priority, host)
    v = admit(sample, need, leases + held, host=host, limits=lim, settle_s=settle_s, slice_mem=slice_mem)
    if v.ok or not held:
        return v
    bare = admit(sample, need, leases, host=host, limits=lim, settle_s=settle_s, slice_mem=slice_mem)
    if not bare.ok:
        return v
    who = ", ".join(f"{d.workload} {d.queued} queued" for d in demand
                    if d.queued > 0 and priority.rank(d.workload) < priority.rank(workload))
    return Verdict(False, "priority", f"held_back:{workload} for {who} ({v.reason})", v.free)


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
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()   # a BOM-prefixed ledger keeps its 1st row
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


def _history_row(ln) -> Optional[dict]:
    try:
        r = json.loads(ln)
    except (ValueError, TypeError):
        return None
    return r if isinstance(r, dict) and r.get("schema") == SCHEMA else None


# path -> [open binary file, bytes consumed, rows parsed so far, checkpoint]   (read_history_cached; t_c9fd9c5e)
# checkpoint = the last CHECKPOINT_BYTES consumed bytes: re-read before reusing the rows, so a truncate + refill to the
# old size or past it (same inode, never smaller at a read) resets the cache instead of replaying the old generation.
# The file stays OPEN while its rows are cached, so its inode cannot be recycled by an unlink + recreate (Prism e2ace455c12a).
_HISTORY_CACHE: dict = {}
_HISTORY_LOCK = threading.Lock()
CHECKPOINT_BYTES = 256


def _checkpoint_ok(fh, consumed: int, want: bytes) -> bool:
    if consumed <= 0 or not want:
        return True
    fh.seek(consumed - len(want))
    return fh.read(len(want)) == want


def read_history_cached(path, *, since: Optional[float] = None) -> List[dict]:
    """read_history() for a long-lived reader of an APPEND-ONLY ledger: each call parses only the bytes appended since
    the previous call (t_c9fd9c5e). allocd ran read_history() on the whole 14-day class-usage.jsonl inside every
    utilization admit, i.e. under the reservation-ledger flock; at 100k rows that is ~0.7 s of flock per admit, so
    renews queued behind admits past alloc_client's 2 s timeout (allocd_unreachable) and the fallback then lost the
    flock race (allocd_wedged_lock). Same rows and filters as read_history(); an incomplete last line waits for its
    newline. A replaced (new inode) or shrunk file is re-read from byte 0. The cached descriptor is held open, so the
    inode it names cannot be reused while cached; each call compares stat(path) with fstat() of that descriptor, and
    a mismatch closes it and reads the path afresh from byte 0 (Prism e2ace455c12a). Reads always come from the held
    descriptor, so a rotation after the stat() returns the old file's rows once, never the old offset in a new file."""
    p = Path(path)
    key = str(p)
    with _HISTORY_LOCK:
        hit = _HISTORY_CACHE.get(key)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            st = None
        if hit is not None:
            held = os.fstat(hit[0].fileno())
            if st is None or (st.st_dev, st.st_ino) != (held.st_dev, held.st_ino):
                hit[0].close()
                _HISTORY_CACHE.pop(key, None)
                hit = None
            elif held.st_size < hit[1] or not _checkpoint_ok(hit[0], hit[1], hit[3]):
                hit[1], hit[2], hit[3] = 0, [], b""   # shrunk or rewritten in place: start over on the same descriptor
        if hit is None:
            try:
                hit = [p.open("rb"), 0, [], b""]
            except FileNotFoundError:
                return []
        fh = hit[0]
        fh.seek(hit[1])
        chunk = fh.read()
        end = chunk.rfind(b"\n") + 1               # only whole lines; a torn tail is read again next call
        # utf-8-sig strips a BOM at byte 0 only: a BOM-prefixed ledger keeps its first row (Prism 5799d4eb7009)
        text = chunk[:end].decode("utf-8-sig" if hit[1] == 0 else "utf-8", errors="replace")
        for ln in text.splitlines():
            r = _history_row(ln)
            if r is not None:
                hit[2].append(r)
        hit[1] += end
        if end:
            hit[3] = (hit[3] + chunk[:end])[-CHECKPOINT_BYTES:]
        _HISTORY_CACHE[str(p)] = hit
        rows = list(hit[2])
    if since is None:
        return rows
    out: List[dict] = []
    for r in rows:
        try:
            if float(r.get("at") or 0) < since:
                continue
        except (ValueError, TypeError):
            continue
        out.append(r)
    return out
