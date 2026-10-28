"""work_estimate: the ONE work-footprint estimator every admission consumer uses (t_9e395789; Ace 2026-10-20 05:0x:
"makes an estimate of what our work is going to be like and how much resources it is going to need, then allocates
accordingly"). admission.admit() takes the Estimate this module returns; no consumer carries its own footprint number.

    est = work_estimate.estimate(job_class, host, history, seed)

Classes (one name per work kind, a ``:<discriminator>`` suffix where the consumer knows one):
    kanban worker   kanban-worker-llm-wait[:<profile>]        (consumer kanban / athena; Job.profile)
    CI job          ci-suite-shard[:<suite or label>]         (consumer ci; Job.job_class)
    Prism review    prism-review[:pr-xs|s|m|l|xl]            (consumer prism; prism_class(<changed lines>))
    test-gate run   test-gate-run[:files-1|2-10|11-50|51+]    (consumer test-gate; test_gate_class(<test files>))

Seed: the declared class footprint in config/fleet-hosts.json (``footprints.<consumer>`` / ``.profiles.<profile>``,
the constants the consumers carried until now), read through placement.HostTable. It is the estimate until the
class has MIN_ROWS recorded actuals.

Learned: the actuals are the class-usage ledger (var/placement/class-usage.jsonl), the estimate-vs-actual rows
placement.release(actual=) / allocd /release write through admission.record_actual() (FWA instrumentation spec
plans/2026-10-19_fwa-instrumentation-SPEC.md is the producer side; this module only reads). Per axis: the P-th
percentile (p75) over the LAST ``window`` rows of the class on that host; too few there, the class across the fleet;
too few anywhere, the seed. Never below FLOOR (a class that measures ~0 still reserves something).

Axes: cpus (CPUs kept busy at peak, the lease), ram_gb (peak), gpu_mib, and the two the body asks for that admit()
does not gate on: cpu_s (cpu-seconds per run) and wall_s (run wall seconds); None until a row carries them.

    fleet-host estimate <class|consumer> [--host H] [--profile P] [--json]   (operators; read-only)
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

try:
    from . import admission
except ImportError:  # scripts/lib on sys.path
    import admission

Estimate = admission.Estimate
P = 0.75                     # the estimate is the p75 of a class's recorded peaks (t_9e395789)
WINDOW = 20                  # over its last N runs
MIN_ROWS = 5                 # fewer rows than this: the next fallback (class@fleet, then the seed)
FLOOR = {"cpus": 0.1, "ram_gb": 0.25, "gpu_mib": 0.0}   # never below (a measured ~0 still reserves something)
CLASS_USAGE_REL = Path("var") / "placement" / "class-usage.jsonl"
CLASS_USAGE_WINDOW_S = 14 * 86400.0

# consumer -> its base class (the ledger key the consumers have written since t_da6f84f7; unchanged so history stays).
# prism is prism-review (t_e3260e9f): one allocd lease = one REVIEW on one member host, and prism-router's fwa gate
# estimates and /release records under that same key (the Studio had no class-usage ledger yet on 10-20, so no
# prism-seat history is orphaned).
CONSUMER_CLASS = {"kanban": "kanban-worker-llm-wait", "athena": "kanban-worker-llm-wait", "test-gate": "test-gate-run",
                  "prism": "prism-review", "ci": "ci-suite-shard"}
PROFILE_CONSUMERS = ("kanban", "athena")   # a Job.profile names a sub-class here (footprints.kanban.profiles)


# ── class names ────────────────────────────────────────────────────────────────────────────────────────────────
def job_class(consumer: str, *, explicit: Optional[str] = None, profile: Optional[str] = None) -> str:
    """The class a job's estimate is keyed on: ``explicit`` (Job.job_class) if given, else the consumer's base class
    with ``:<profile>`` for a kanban-family consumer that knows its profile."""
    if explicit:
        return str(explicit)
    base = CONSUMER_CLASS.get(consumer, consumer)
    if profile and consumer in PROFILE_CONSUMERS:
        return f"{base}:{profile}"
    return base


def _band(n: float, edges: Sequence[tuple]) -> str:
    for hi, name in edges:
        if n <= hi:
            return name
    return edges[-1][1]


def prism_class(changed_lines: Optional[float]) -> str:
    """Prism review class by PR size band (additions + deletions); unknown size = the base class."""
    if not isinstance(changed_lines, (int, float)) or isinstance(changed_lines, bool) or changed_lines < 0:
        return CONSUMER_CLASS["prism"]
    return CONSUMER_CLASS["prism"] + ":" + _band(changed_lines, ((50, "pr-xs"), (200, "pr-s"), (800, "pr-m"),
                                                                 (3000, "pr-l"), (math.inf, "pr-xl")))


def test_gate_class(test_files: Optional[float]) -> str:
    """test-gate run class by the number of test files it runs; unknown = the base class."""
    if not isinstance(test_files, (int, float)) or isinstance(test_files, bool) or test_files < 1:
        return CONSUMER_CLASS["test-gate"]
    return CONSUMER_CLASS["test-gate"] + ":" + _band(test_files, ((1, "files-1"), (10, "files-2-10"),
                                                                  (50, "files-11-50"), (math.inf, "files-51+")))


def ci_class(label: Optional[str]) -> str:
    """CI job class by suite / runner label; none = the base class."""
    return CONSUMER_CLASS["ci"] + (f":{label}" if label else "")


def consumer_of(klass: str) -> Optional[str]:
    """The consumer a class name belongs to (for its seed), or None."""
    base = klass.split(":", 1)[0]
    for c, b in CONSUMER_CLASS.items():
        if b == base or c == base:
            return c
    return None


# ── the estimate ───────────────────────────────────────────────────────────────────────────────────────────────
def seed_of(fp) -> Estimate:
    """A declared class footprint (placement.Footprint or a mapping) as the seed estimate."""
    get = (lambda k: fp.get(k)) if isinstance(fp, Mapping) else (lambda k: getattr(fp, k, None))
    return Estimate(cpus=float(get("threads") or 0.0), ram_gb=float(get("ram_gb") or 0.0),
                    gpu_mib=float(get("gpu_mb") or 0.0), source="seed")


def _pctl(xs: Sequence[float], p: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, math.ceil(p * len(s)) - 1))
    return float(s[k])


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _axis(rows: Sequence[Mapping], key: str, p: float) -> Optional[float]:
    xs = [float(r[key]) for r in rows if _num(r.get(key)) and r[key] >= 0]
    return _pctl(xs, p) if xs else None


def estimate(klass: str, host: str, history: Iterable[Mapping], seed: Optional[Estimate] = None, *,
             p: float = P, window: int = WINDOW, min_rows: int = MIN_ROWS) -> Estimate:
    """p75 per axis over the last ``window`` recorded runs of ``klass`` on ``host``; too few rows there -> the class
    across every host; then the same two for the base class (``prism-review`` for ``prism-review:pr-m``: the ledger kept
    before sub-classes existed keeps counting); too few anywhere -> ``seed``. Every axis is floored at FLOOR.
    ``history`` rows: {class, host, cpus, ram_gb, gpu_mib, at, [cpu_s, wall_s]} (admission.read_history())."""
    history = list(history)
    seed = seed or Estimate()
    base = klass.split(":", 1)[0]
    for k in ((klass, base) if base != klass else (klass,)):
        rows = sorted((r for r in history if r.get("class") == k), key=lambda r: float(r.get("at") or 0.0))
        here = [r for r in rows if r.get("host") == host]
        for pick, where in ((here, host), (rows, "fleet")):
            if len(pick) < min_rows:
                continue
            last = pick[-window:]
            v = {ax: _axis(last, ax, p) for ax in ("cpus", "ram_gb", "gpu_mib", "cpu_s", "wall_s")}
            return Estimate(cpus=max(FLOOR["cpus"], v["cpus"] if v["cpus"] is not None else seed.cpus),
                            ram_gb=max(FLOOR["ram_gb"], v["ram_gb"] if v["ram_gb"] is not None else seed.ram_gb),
                            gpu_mib=max(FLOOR["gpu_mib"], v["gpu_mib"] if v["gpu_mib"] is not None else seed.gpu_mib),
                            cpu_s=v["cpu_s"], wall_s=v["wall_s"],
                            source=f"p{int(p * 100)}:{k}@{where} n={len(last)}")
    return dataclasses.replace(seed, cpus=max(FLOOR["cpus"], seed.cpus), ram_gb=max(FLOOR["ram_gb"], seed.ram_gb),
                               gpu_mib=max(FLOOR["gpu_mib"], seed.gpu_mib), source="seed")


def read_history(root: Optional[Path] = None, *, now: Optional[float] = None, path=None) -> List[dict]:
    """The class-usage ledger rows inside CLASS_USAGE_WINDOW_S (``path`` wins over ``root``)."""
    import time
    p = Path(path) if path is not None else Path(root or ".") / CLASS_USAGE_REL
    now = time.time() if now is None else now
    return admission.read_history(p, since=now - CLASS_USAGE_WINDOW_S)


# ── operator view: fleet-host estimate <class> ─────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet-host estimate",
                                 description="What admission will reserve for one job of a class (read-only).")
    ap.add_argument("klass", help="a class (kanban-worker-llm-wait:daedalus, prism-review:pr-m, ...) or a consumer")
    ap.add_argument("--host", action="append", default=None, help="host id (repeatable; default: every table host)")
    ap.add_argument("--profile", default=None, help="kanban profile (same as <base>:<profile>)")
    ap.add_argument("--table", default=None, help="config/fleet-hosts.json (default: the fleet root's)")
    ap.add_argument("--usage", default=None, help="class-usage.jsonl (default: the fleet root's)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        from . import placement
    except ImportError:
        import placement
    table = placement.load_hosts(a.table) if a.table else placement.load_hosts()
    consumer = a.klass if a.klass in CONSUMER_CLASS else consumer_of(a.klass)
    klass = job_class(consumer, profile=a.profile) if a.klass in CONSUMER_CLASS else a.klass
    profile = a.profile or (klass.split(":", 1)[1] if consumer in PROFILE_CONSUMERS and ":" in klass else None)
    if consumer is None or consumer not in table.footprints:
        print(f"fleet-host estimate: REFUSED: no consumer for class {a.klass!r} "
              f"(known: {', '.join(sorted(CONSUMER_CLASS))})", file=sys.stderr)
        return 2
    seed = seed_of(table.footprint(consumer, profile=profile))
    hist = read_history(path=a.usage if a.usage else placement.class_usage_path())
    hosts = [table.resolve(h) or h for h in (a.host or list(table.hosts))] or ["fleet"]   # no host: the fleet row
    out = []
    for h in hosts:
        e = estimate(klass, h, hist, seed)
        out.append({"class": klass, "host": h, "cpus": round(e.cpus, 3), "ram_gb": round(e.ram_gb, 3),
                    "gpu_mib": round(e.gpu_mib, 1), "cpu_s": e.cpu_s, "wall_s": e.wall_s, "source": e.source})
    if a.json:
        print(json.dumps({"class": klass, "consumer": consumer, "seed": dataclasses.asdict(seed), "hosts": out},
                         sort_keys=True))
        return 0
    print(f"{klass}  (consumer {consumer}; seed {seed.cpus:g} cpu / {seed.ram_gb:g} GB from config/fleet-hosts.json;"
          f" p{int(P * 100)} of last {WINDOW}, >= {MIN_ROWS} rows, floor {FLOOR['cpus']:g} cpu / {FLOOR['ram_gb']:g} GB)")
    for r in out:
        extra = "".join(f"  {k} {r[k]:g}" for k in ("cpu_s", "wall_s") if r[k] is not None)
        print(f"  {r['host']:<16} {r['cpus']:>6g} cpu  {r['ram_gb']:>6g} GB{extra}  [{r['source']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
