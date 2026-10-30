# SCHEDULE: manual (library imported by each class's own scheduler; nothing runs this file on a cadence)
"""admission_demand: the I/O half of the ONE admission rule (t_e5124426). admission.admit_class() is pure; this module
reads its two inputs from disk and gives every caller the same kill switch.

  load_priority(path=None) -> admission.Priority     fleet/admission-priority.json (order, line, holdback_units)
  publish(workload, queued, unit, hosts=None, root=None, now=None)
                                                     a class publishes its queue: var/placement/demand/<workload>.json
  read_demand(host, root=None, now=None, resolve=None) -> [admission.Demand]
                                                     every class's FRESH queue that may run on ``host``
  unified(workload, env=None) -> bool                the per-caller kill switch: <WORKLOAD>_UNIFIED_ADMIT=0 = the
                                                     caller's old local rule (one release, card step 4)
  published_unified(workload, root=None, now=None) -> bool | None
                                                     that switch as the class's admitter published it (demand file
                                                     ``unified``), for a reader in another process

Demand file (one per class, atomic replace; the class's own scheduler writes it every tick):
  {"schema": 1, "workload": "prism", "at": <epoch>, "queued": 64, "hosts": ["ci-box", "ace-ai"] | null,
   "unit": {"cpus": 1.0, "ram_gb": 4.0, "gpu_mib": 0}, "unified": true}
``unified`` = the publisher's <WORKLOAD>_UNIFIED_ADMIT switch (absent in a file from an older publisher).
``hosts`` null = may run anywhere. A file older than demand_max_age_s, unreadable, or malformed holds nothing back
(said once on stderr): a dead publisher must never starve the other classes, and the host line still protects the box.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

try:
    from . import admission
except ImportError:  # scripts/lib on sys.path
    import admission

SCHEMA = 1
PRIORITY_REL = Path("fleet") / "admission-priority.json"
DEMAND_REL = Path("var") / "placement" / "demand"
DEFAULT_MAX_AGE_S = 120.0
_SAID: set = set()


def _root(root=None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("PLACEMENT_HOME")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def _say(key: str, msg: str) -> None:
    if key not in _SAID:
        _SAID.add(key)
        sys.stderr.write(f"admission_demand: {msg}\n")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _read_policy(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _say(f"policy:{path}", f"{path} unreadable ({exc}); built-in priority defaults")
        return {}
    return doc if isinstance(doc, dict) else {}


def validate_priority(doc: Mapping) -> List[str]:
    errs = []
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    order = doc.get("order")
    if not (isinstance(order, list) and order and all(isinstance(x, str) and x for x in order)
            and len(set(order)) == len(order)):
        errs.append("order must be a non-empty list of distinct class names")
    line = doc.get("line")
    if not (_num(line) and 0 < line <= admission.HOST_LINE):
        errs.append(f"line must be a number in (0, {admission.HOST_LINE}] (Ace: no host above 90 % busy)")
    hb = doc.get("holdback_units")
    if not (isinstance(hb, int) and not isinstance(hb, bool) and 0 <= hb <= 64):
        errs.append("holdback_units must be an int in [0, 64]")
    age = doc.get("demand_max_age_s")
    if not (_num(age) and age > 0):
        errs.append("demand_max_age_s must be a number > 0")
    return errs


def load_priority(path=None, root=None) -> admission.Priority:
    p = Path(path) if path is not None else _root(root) / PRIORITY_REL
    doc = _read_policy(p)
    if not doc:
        return admission.Priority()
    errs = validate_priority(doc)
    if errs:
        _say(f"invalid:{p}", f"{p} invalid ({'; '.join(errs)}); built-in priority defaults")
        return admission.Priority()
    return admission.Priority(order=tuple(doc["order"]), line=float(doc["line"]), holdback_units=int(doc["holdback_units"]))


def max_age_s(path=None, root=None) -> float:
    p = Path(path) if path is not None else _root(root) / PRIORITY_REL
    doc = _read_policy(p)
    v = doc.get("demand_max_age_s") if doc else None
    return float(v) if _num(v) and v > 0 else DEFAULT_MAX_AGE_S


def publish(workload: str, queued: int, unit: admission.Estimate, *, hosts=None, root=None,
            now: Optional[float] = None, env: Optional[Mapping[str, str]] = None) -> Path:
    """``unified`` records the PUBLISHER's kill switch (its own process env, or ``env``): the class's admitter is
    the process that publishes, so a reader in another process (the placement collector's band) takes the rule
    mode from here, never from its own env (Prism 59574d3a240b)."""
    d = _root(root) / DEMAND_REL
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schema": SCHEMA, "workload": workload, "at": float(time.time() if now is None else now),
           "queued": max(0, int(queued)), "hosts": None if hosts is None else sorted(str(h) for h in hosts),
           "unit": {"cpus": float(unit.cpus), "ram_gb": float(unit.ram_gb), "gpu_mib": float(unit.gpu_mib)},
           "unified": unified(workload, env)}
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=f".{workload}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
        os.replace(tmp, d / f"{workload}.json")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return d / f"{workload}.json"


def _demand_of(doc, now: float, max_age: float, host: str,
               resolve: Optional[Callable[[str], Optional[str]]] = None) -> Optional[admission.Demand]:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    wl, at, q, unit = doc.get("workload"), doc.get("at"), doc.get("queued"), doc.get("unit")
    if not (isinstance(wl, str) and wl and _num(at) and isinstance(q, int) and not isinstance(q, bool)
            and isinstance(unit, dict)):
        return None
    if now - float(at) > max_age or float(at) - now > max_age:
        return None
    hosts = doc.get("hosts")
    if hosts is not None:
        if not isinstance(hosts, list):
            return None
        # with a resolver (placement's HostTable.resolve), an alias names its canonical host (Prism 364936434c3d)
        want = (resolve(host) if resolve else None) or host
        names = {((resolve(str(h)) if resolve else None) or str(h)).lower() for h in hosts}
        if want.lower() not in names:
            return None
    cpus, ram, gpu = unit.get("cpus"), unit.get("ram_gb"), unit.get("gpu_mib", 0.0)
    if not (_num(cpus) and _num(ram) and _num(gpu) and cpus >= 0 and ram >= 0 and gpu >= 0):
        return None
    return admission.Demand(wl, q, admission.Estimate(float(cpus), float(ram), float(gpu), f"demand:{wl}"))


def read_demand(host: str, *, root=None, now: Optional[float] = None, max_age: Optional[float] = None,
                resolve: Optional[Callable[[str], Optional[str]]] = None) -> List[admission.Demand]:
    """Every FRESH class queue that may run on ``host``. ``resolve`` (name -> canonical host id or None) folds aliases
    on both sides of a ``hosts`` restriction; without it the match is case-insensitive string equality."""
    now = float(time.time() if now is None else now)
    max_age = max_age_s(root=root) if max_age is None else float(max_age)
    out: List[admission.Demand] = []
    d = _root(root) / DEMAND_REL
    try:
        names = sorted(p for p in d.glob("*.json"))
    except OSError:
        return out
    for p in names:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _say(f"demand:{p}", f"{p} unreadable: holds nothing back")
            continue
        dm = _demand_of(doc, now, max_age, host, resolve)
        if dm is not None:
            out.append(dm)
    return out


def unified(workload: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """<WORKLOAD>_UNIFIED_ADMIT (e.g. PRISM_UNIFIED_ADMIT, TEST_GATE_UNIFIED_ADMIT): "0" = the caller's old rule."""
    env = os.environ if env is None else env
    key = workload.upper().replace("-", "_") + "_UNIFIED_ADMIT"
    return str(env.get(key, "1")).strip() != "0"


def published_unified(workload: str, *, root=None, now: Optional[float] = None,
                      max_age: Optional[float] = None) -> Optional[bool]:
    """The kill switch as ``workload``'s admitter last published it (var/placement/demand/<workload>.json ``unified``),
    or None: no file, unreadable, malformed, no ``unified`` key (an older publisher) or older than demand_max_age_s.
    A reader in ANOTHER process than the admitter reads the mode here, never its own env (Prism 59574d3a240b)."""
    now = float(time.time() if now is None else now)
    max_age = max_age_s(root=root) if max_age is None else float(max_age)
    try:
        doc = json.loads((_root(root) / DEMAND_REL / f"{workload}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("workload") != workload:
        return None
    at, mode = doc.get("at"), doc.get("unified")
    if not _num(at) or abs(now - float(at)) > max_age or not isinstance(mode, bool):
        return None
    return mode


def workload_of(consumer: str) -> str:
    """placement consumer -> admission workload class (the names in fleet/admission-priority.json)."""
    return {"athena": "kanban"}.get(consumer, consumer)
