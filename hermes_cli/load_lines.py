"""load_lines — THE reader for the fleet's CPU admission lines (Ace 2026-10-26 04:40/04:49, Telegram 571820863:
"one central registry that controls all of this for all the devices ... keep things dry").

The registry is the ``cpu_lines`` block of hermes-home ``fleet/placement-policy.json`` (synced to the Linux hosts at
``/usr/local/lib/placement/fleet/placement-policy.json``). Every consumer that decides "is this host too busy to admit
more work" asks ``line(host, lane, kind)`` here; code keeps at most ``FALLBACK`` (== the registry default) and LOGS
when it falls back. Stdlib only, no hermes-home imports: this one file is vendored verbatim into fleet-ops-scripts
(ci-runners/src/load_lines.py) and fleetreview-router (src/prism_router/pool/fwa_vendor/load_lines.py), and read by
hermes-agent (hermes_cli/config_defaults.py) via HERMES_HOME. ``scripts/lint-load-lines.py`` keeps the copies
byte-identical and refuses CPU line literals in the known consumer files.

Shape (fractions of the host's CPU, 0 < x <= 2):
    "cpu_lines": {
      "default": {"admit": 0.90, "spill": 0.90, "resume": 0.85, "hot": 0.90},
      "lanes":   {"<lane>": {<kind>: x}},              # a lane's own line, every host
      "hosts":   {"<host>": {<kind>: x, "lanes": {"<lane>": {<kind>: x}}}}
    }
Resolution, most specific wins: hosts.<h>.lanes.<l>.<k> > hosts.<h>.<k> > lanes.<l>.<k> > default.<k> > FALLBACK.<k>.
Kinds: admit = stop admitting new work at/above; spill = start placing elsewhere; resume = spill clears below;
hot = hard stop (nothing new, any class).

CLI: ``python3 load_lines.py [--policy P] [--host H] [--json]`` prints the effective table.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

KINDS = ("admit", "spill", "resume", "hot")
# The fail-safe used ONLY when the registry is unreadable. Equal to the registry default by lint (lint-load-lines.py).
FALLBACK = {"admit": 0.90, "spill": 0.90, "resume": 0.85, "hot": 0.90}
HOST_POLICY = "/usr/local/lib/placement/fleet/placement-policy.json"
ENV_POLICY = "LOAD_LINES_POLICY"

_cache: dict = {}
_warned: set = set()


def candidates() -> list:
    """Policy paths in the order tried: $LOAD_LINES_POLICY, the hermes-home checkout ($HERMES_HOME or the
    repo this file sits in), the host-synced copy."""
    out = []
    if os.environ.get(ENV_POLICY):
        out.append(Path(os.environ[ENV_POLICY]))
    vh = os.environ.get("HERMES_HOME")
    if vh:
        out.append(Path(vh) / "fleet" / "placement-policy.json")
    here = Path(__file__).resolve()
    for up in here.parents[:4]:
        out.append(up / "fleet" / "placement-policy.json")
    out.append(Path(HOST_POLICY))
    out.append(Path(os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")) / "fleet" / "placement-policy.json")
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def host_id() -> str:
    """This host's policy id: $LOAD_LINES_HOST / $PLACEMENT_HOST_ID, else mac-studio on Darwin, else the short
    hostname lower-cased (ci-box, ace-ai, ace-media). An id the registry does not name resolves to the default."""
    for k in ("LOAD_LINES_HOST", "PLACEMENT_HOST_ID"):
        if os.environ.get(k):
            return os.environ[k]
    if sys.platform == "darwin":
        return "mac-studio"
    import socket
    return socket.gethostname().split(".")[0].lower()


def _ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 2


def validate(block) -> list:
    """Structural errors in a cpu_lines block ([] = valid)."""
    if not isinstance(block, dict):
        return ["cpu_lines must be an object"]
    errs = []

    def kinds(path, d, allow_lanes=False):
        if not isinstance(d, dict):
            errs.append(f"{path} must be an object")
            return
        for k, v in d.items():
            if k.startswith("_"):
                continue
            if allow_lanes and k == "lanes":
                if not isinstance(v, dict):
                    errs.append(f"{path}.lanes must be an object")
                    continue
                for ln, lv in v.items():
                    if not ln.startswith("_"):
                        kinds(f"{path}.lanes.{ln}", lv)
            elif k not in KINDS:
                errs.append(f"{path}.{k}: unknown kind (allowed: {', '.join(KINDS)})")
            elif not _ok(v):
                errs.append(f"{path}.{k} must be a number in (0, 2], not {v!r}")

    d = block.get("default")
    kinds("cpu_lines.default", d)
    if isinstance(d, dict) and set(KINDS) - set(d):
        errs.append(f"cpu_lines.default must name every kind ({', '.join(KINDS)})")
    for sect, allow in (("lanes", False), ("hosts", True)):
        s = block.get(sect, {})
        if not isinstance(s, dict):
            errs.append(f"cpu_lines.{sect} must be an object")
            continue
        for name, row in s.items():
            if not name.startswith("_"):
                kinds(f"cpu_lines.{sect}.{name}", row, allow_lanes=allow)
    for k in block:
        if not k.startswith("_") and k not in ("default", "lanes", "hosts"):
            errs.append(f"cpu_lines.{k}: unknown key (allowed: default, lanes, hosts)")
    return errs


def load(policy=None):
    """(cpu_lines block, source path) or (None, reason). Cached per path+mtime."""
    paths = [Path(policy)] if policy else candidates()
    reasons = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        key = (str(p), st.st_mtime_ns, st.st_size)
        if key in _cache:
            return _cache[key]
        try:
            block = json.loads(p.read_text()).get("cpu_lines")
        except (OSError, ValueError, AttributeError) as exc:
            reasons.append(f"{p}: {exc}")
            continue
        errs = validate(block)
        if errs:
            reasons.append(f"{p}: {errs[0]}")
            continue
        _cache[key] = (block, str(p))
        return _cache[key]
    return None, "; ".join(reasons) or "no placement-policy.json found (" + ", ".join(map(str, paths)) + ")"


def _warn(msg):
    if msg not in _warned:
        _warned.add(msg)
        print(f"load_lines: FALLBACK {msg}", file=sys.stderr)


def resolve(host, lane, kind, policy=None):
    """(value, source) where source names the registry key that decided it, or 'fallback: <why>'."""
    if kind not in KINDS:
        raise KeyError(f"unknown kind {kind!r} (allowed: {', '.join(KINDS)})")
    block, src = load(policy)
    if block is None:
        _warn(f"{host}/{lane}/{kind} = {FALLBACK[kind]}: {src}")
        return FALLBACK[kind], f"fallback: {src}"
    h = (block.get("hosts") or {}).get(host) or {}
    for path, d in ((f"hosts.{host}.lanes.{lane}", (h.get("lanes") or {}).get(lane) or {}),
                    (f"hosts.{host}", h),
                    (f"lanes.{lane}", (block.get("lanes") or {}).get(lane) or {}),
                    ("default", block.get("default") or {})):
        if _ok(d.get(kind)):
            return float(d[kind]), f"cpu_lines.{path}.{kind} ({src})"
    _warn(f"{host}/{lane}/{kind} = {FALLBACK[kind]}: cpu_lines.default.{kind} absent")
    return FALLBACK[kind], "fallback: default missing"


def line(host, lane, kind="admit", policy=None) -> float:
    """The CPU line for (host, lane, kind) as a fraction of the host's CPU."""
    return resolve(host, lane, kind, policy)[0]


def lanes_and_hosts(block) -> tuple:
    lanes = set((block.get("lanes") or {}).keys())
    hosts = set((block.get("hosts") or {}).keys())
    for h in (block.get("hosts") or {}).values():
        if isinstance(h, dict):
            lanes |= set((h.get("lanes") or {}).keys())
    return sorted(x for x in hosts if not x.startswith("_")), sorted(x for x in lanes if not x.startswith("_"))


def table(policy=None, hosts=None) -> list:
    block, src = load(policy)
    if block is None:
        return []
    hs, ls = lanes_and_hosts(block)
    rows = []
    for h in hosts or hs:
        for ln in ["*"] + ls:
            rows.append({"host": h, "lane": ln, **{k: resolve(h, ln, k, policy)[0] for k in KINDS}})
    return rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="print the effective CPU admission lines")
    ap.add_argument("--policy")
    ap.add_argument("--host", action="append")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    block, src = load(a.policy)
    if block is None:
        print(f"load_lines: registry unreadable: {src}", file=sys.stderr)
        return 2
    rows = table(a.policy, a.host)
    if a.json:
        print(json.dumps({"source": src, "rows": rows}, indent=1))
        return 0
    print(f"registry: {src}")
    print(f"{'host':12} {'lane':12} " + " ".join(f"{k:>6}" for k in KINDS))
    for r in rows:
        print(f"{r['host']:12} {r['lane']:12} " + " ".join(f"{r[k] * 100:5.0f}%" for k in KINDS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
