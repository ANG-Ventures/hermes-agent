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
percentile (p90, doctrine rule 3: "p90 of the last ~50 per class per host"; t_d0c8e8f0) over the LAST ``window`` (50)
rows of the class on that host, raised to the level a corroborated STEP CHANGE reached (trailing-window constraint 1:
>= 2 of the last 3 runs above the p90, so a class that just got heavier is charged its new level at once, while ONE
outlier run never is, Prism 351931a4ea67); never reduced to fit a host. Oversized measured demand is refused by
admission with a typed resource-fit reason; rows age out by TIME (CLASS_USAGE_WINDOW_S, the
read_history cut; constraint 3), never by count alone. Too few rows there: the class across the fleet; too few
anywhere: the seed (the consumer's declared number is only a cold-start prior). Never below FLOOR (a class that
measures ~0 still reserves something).

The table: footprint_table() is that same estimate for every (class, host) the ledger holds, the ONE measured
footprint per (class, host) every consumer books (allocd GET /footprints/<host>; scripts/class-footprint-honesty.py
reads reserved vs measured back nightly).

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
P = 0.90                     # the estimate is the p90 of a class's recorded peaks (doctrine rule 3; t_d0c8e8f0)
WINDOW = 50                  # over its last N runs on that host (doctrine rule 3: "the last ~50")
MIN_ROWS = 10                # nearest-rank p90 must not equal one isolated maximum during cold start
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


STEP_LAST = 3                # the newest-run floor reads the last 3 runs ...
STEP_MIN = 2                 # ... and engages only when >= 2 of them sit above the p90 (a corroborated step change)
AXES = ("cpus", "ram_gb", "gpu_mib")


def _step_floor(rows: Sequence[Mapping], ax: str, pv: Optional[float]) -> Optional[float]:
    """Trailing-window constraint 1, self-healing (Prism 351931a4ea67): the level a class STEPPED to, i.e. the
    STEP_MIN-th highest of its last STEP_LAST runs when at least STEP_MIN of them sit above the p90 ``pv`` (the level
    two recent runs both reached); None otherwise.
    ONE anomalous run (runaway test, leaked child) is never the class's need: it would pin admission on every host
    the fleet fallback reaches until it aged out, and a class priced out of every host records no run to displace it."""
    xs = [float(r[ax]) for r in rows[-STEP_LAST:] if _num(r.get(ax)) and r[ax] >= 0]
    if pv is None:
        return None
    above = sorted((x for x in xs if x > pv), reverse=True)
    return above[STEP_MIN - 1] if len(above) >= STEP_MIN else None


def estimate(klass: str, host: str, history: Iterable[Mapping], seed: Optional[Estimate] = None, *,
             p: float = P, window: int = WINDOW, min_rows: int = MIN_ROWS) -> Estimate:
    """p90 per axis over the last ``window`` recorded runs of ``klass`` on ``host`` (raised to the level a
    corroborated step change reached, _step_floor); too few rows there -> the class
    across every host; then the same two for the base class (``prism-review`` for ``prism-review:pr-m``: the ledger kept
    before sub-classes existed keeps counting); too few anywhere -> ``seed``. Every axis is floored at FLOOR; a measured
    need or a declared seed is NEVER lowered to fit a host (ruling t_d0c8e8f0 23:1x: placement.choose() refuses a need
    no host can admit with a named reason instead). ``history`` rows: {class, host, cpus, ram_gb, gpu_mib, at,
    [cpu_s, wall_s]} (admission.read_history())."""
    return _estimate(klass, host, history, seed, p=p, window=window, min_rows=min_rows)[0]


def _estimate(klass, host, history, seed, *, p, window, min_rows):
    """estimate() plus the axes it booked from the seed (no measurement in the window it read)."""
    history = list(history)
    seed = seed or Estimate()
    base = klass.split(":", 1)[0]
    v = dict.fromkeys((*AXES, "cpu_s", "wall_s"))
    source = "seed"
    for k in ((klass, base) if base != klass else (klass,)):
        rows = sorted((r for r in history if r.get("class") == k), key=lambda r: float(r.get("at") or 0.0))
        here = [r for r in rows if r.get("host") == host]
        for pick, where in ((here, host), (rows, "fleet")):
            if len(pick) < min_rows:
                continue
            last = pick[-window:]
            # Resolve each axis independently; a sparse host axis still gets the
            # fleet/base-class measurements before falling back to its seed.
            if source == "seed":
                source = f"p{int(p * 100)}:{k}@{where} n={len(last)}"
            for ax in v:
                if v[ax] is not None:
                    continue
                if sum(_num(r.get(ax)) and r[ax] >= 0 for r in last) < min_rows:
                    continue
                v[ax] = _axis(last, ax, p)
                step = _step_floor(last, ax, v[ax])
                if step is not None:
                    v[ax] = step
    if source == "seed":
        return (dataclasses.replace(seed, cpus=max(FLOOR["cpus"], seed.cpus), ram_gb=max(FLOOR["ram_gb"], seed.ram_gb),
                                    gpu_mib=max(FLOOR["gpu_mib"], seed.gpu_mib), source="seed"), list(AXES))
    seeded = [ax for ax in AXES if v[ax] is None]
    return Estimate(**{ax: max(FLOOR[ax], v[ax] if v[ax] is not None else getattr(seed, ax)) for ax in AXES},
                    cpu_s=v["cpu_s"], wall_s=v["wall_s"], source=source), seeded


def table_seed_for(table):
    """``seed_for`` for footprint_table from a placement HostTable: the class's consumer's declared footprint (the
    profile row for <base>:<profile> classes), i.e. the seed admit_context books; None for an unknown class."""
    def seed_for(klass: str) -> Optional[Estimate]:
        consumer = consumer_of(klass)
        if consumer is None or consumer not in table.footprints:
            return None
        profile = klass.split(":", 1)[1] if consumer in PROFILE_CONSUMERS and ":" in klass else None
        try:
            return seed_of(table.footprint(consumer, profile=profile))
        except Exception:     # placement.PlacementError and table shape errors: no seed, the row names its seed axes
            return None
    return seed_for


def footprint_table(history: Iterable[Mapping], *, host: Optional[str] = None, p: float = P, window: int = WINDOW,
                    min_rows: int = MIN_ROWS, seed_for=None) -> List[dict]:
    """THE measured footprint table (t_d0c8e8f0): one row per (class, host) an admit could book (``host`` given:
    EVERY class the ledger holds, priced for that host, so a class with no rows there still reads the fleet-backed
    row estimate() books, Prism 72f07fab965a), each the estimate() an admit books for that pair (same p, window,
    floors, fallbacks; the seed ``seed_for(class)`` returns, table_seed_for: the consumer's declared footprint, as admit books it). An axis
    with no measurement books that seed and is listed in ``seed_axes`` (Prism 7911e118b8db: never presented as a
    measured p90; without seed_for it is the floor and still listed). ``basis``: ``host`` | ``fleet`` | ``seed`` (where the booked number came from); ``n`` = the
    rows that number is built from (the same trailing window; 0 for seed). Rows sorted by (host, class)."""
    history = list(history)
    classes = {str(r.get("class")) for r in history if r.get("class") and r.get("host")}
    if host is None:
        pairs = sorted({(str(r.get("host")), str(r.get("class"))) for r in history if r.get("class") and r.get("host")})
    else:
        pairs = sorted((host, k) for k in classes)
    out = []
    for h, k in pairs:
        seed = seed_for(k) if seed_for is not None else None
        e, seeded = _estimate(k, h, history, seed, p=p, window=window, min_rows=min_rows)
        # A custom class has no inferred consumer. Its caller owns the trusted
        # cold-start seed; never advertise generic floors as that reservation.
        unresolved_seed = seed_for is not None and seed is None
        if e.source == "seed":
            basis, n = "seed", 0
        else:
            basis = "fleet" if e.source.split(" n=", 1)[0].endswith("@fleet") else "host"
            n = int(e.source.split(" n=", 1)[1].split()[0])          # the estimate's own trailing window
        # Full precision: consumers book these through resolve_footprint, so a rounded value would book a different
        # (possibly smaller) reservation than estimate() does locally. Round only where it is displayed.
        out.append({"class": k, "host": h, "n": n, "basis": basis,
                    "cpus": None if unresolved_seed and "cpus" in seeded else e.cpus,
                    "ram_gb": None if unresolved_seed and "ram_gb" in seeded else e.ram_gb,
                    "gpu_mib": None if unresolved_seed and "gpu_mib" in seeded else e.gpu_mib,
                    "cpu_s": e.cpu_s, "wall_s": e.wall_s, "source": e.source, "seed_axes": seeded})
    return out


def resolve_footprint(rows: Iterable[Mapping], klass: str, seed: Estimate) -> Estimate:
    """Resolve an API table using admission's exact-class, base-class, seed order.

    Rows with seed-backed CPU/RAM are not transferable to a new subclass: that
    subclass may have its own declared seed. Missing axes keep the caller seed.
    """
    seed = dataclasses.replace(seed, **{ax: max(FLOOR[ax], getattr(seed, ax)) for ax in AXES})
    by_class = {row.get("class"): row for row in rows}
    base = klass.split(":", 1)[0]
    for key in dict.fromkeys((klass, base)):
        row = by_class.get(key)
        if row is None:
            continue
        seeded = set(row.get("seed_axes") or ())
        if key != klass and row.get("basis") == "seed":
            continue
        # a seed-basis row booked the seed: its runtime fields are the caller's, as estimate() keeps them (Prism 3d7235fd4ec3)
        runtime = ({} if row.get("basis") == "seed" else
                   {"cpu_s": row.get("cpu_s", seed.cpu_s), "wall_s": row.get("wall_s", seed.wall_s)})
        return dataclasses.replace(seed, **{
            ax: (getattr(seed, ax) if ax in seeded else row.get(ax, getattr(seed, ax)))
            for ax in AXES}, **runtime, source=str(row.get("source") or "seed"))
    return seed


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
        # The sibling by LITERAL path (t_58b4cbcc): a bare `import placement` under the daemon closure walker's blind
        # fallback also matched scripts/lint_rules/placement.py -> placement_lint.py's computed loader, which sent
        # every daemon that reaches this module wide (gh-webhook-consumer, lander; Studio self-test red since #9038).
        placement = sys.modules.get("placement")
        if placement is None:
            import importlib.util
            _spec = importlib.util.spec_from_file_location("placement", Path(__file__).resolve().parent / "placement.py")
            placement = importlib.util.module_from_spec(_spec)
            sys.modules["placement"] = placement
            try:
                _spec.loader.exec_module(placement)
            except BaseException:
                sys.modules.pop("placement", None)
                raise
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
    for h in hosts:     # what admit_context books: the estimate, never lowered (Prism da618cd0207e)
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
