#!/usr/bin/env python3
"""Replay synthetic tool-call payloads through two shell hook dispatchers and compare per-call wall time (t_492356b3).

Each dispatcher runs warm (``--serve``), the way the gateway runs it, and gets the SAME hook set and
the SAME payload sequence. Requests alternate between the two (before, after, before, ...) so host load
drifts hit both equally. Per call: wall from writing the request to reading ``{"end": true}``.

    python3 scripts/bench_shell_hooks.py --hooks-dir ~/.hermes/hooks --hook a.py --hook b.py --n 200
    python3 scripts/bench_shell_hooks.py --synthetic --n 200           # generated 15-hook fixture
    python3 scripts/bench_shell_hooks.py --before origin/main ...      # git ref or a dispatcher path

The hooks run with a scratch home (``--home``, default a new temp dir) and a scratch cwd, so real
policy hooks read and write nothing of the host's. Output: one JSON object (mean / p50 / p90 / max ms
per dispatcher, speedup, prefilter skips), and a short table on stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AFTER = REPO / "agent" / "shell_hook_dispatcher.py"
COMMANDS = ["ls -la", "cat README.md", "python3 -c 'print(1)'", "grep -rn foo src", "pwd", "echo hello",
            "make -n", "head -5 setup.py", "wc -l *.py", "date"]


def _resolve_before(spec: str, tmp: Path) -> Path:
    path = Path(spec).expanduser()
    if path.is_file():
        return path.resolve()
    out = subprocess.run(["git", "-C", str(REPO), "show", f"{spec}:agent/shell_hook_dispatcher.py"],
                         capture_output=True, check=True).stdout
    dest = tmp / "dispatcher_before.py"
    dest.write_bytes(out)
    return dest


def _synthetic(dirpath: Path) -> list:
    """15 hooks shaped like the live pre_tool_call set: 3 large (~140-260 KB) and 12 small, each
    parsing its payload and allowing."""
    dirpath.mkdir(parents=True, exist_ok=True)
    names = []
    for i, size_kb in enumerate([260, 140, 140] + [20] * 12):
        body = ["import json, re, sys", "payload = json.load(sys.stdin)"]
        n = 0
        while sum(len(line) + 1 for line in body) < size_kb * 1024:
            body.append(f"def rule_{n}(cmd):\n    m = re.search(r'\\bgit\\s+push\\s+{n}\\b', cmd)\n"
                        f"    return {{'rule': {n}, 'hit': bool(m), 'cmd': cmd[:{n % 40 + 1}]}}\n")
            n += 1
        body.append("cmd = (payload.get('tool_input') or {}).get('command') or ''")
        body.append("print(json.dumps({}))")
        name = f"synthetic_{i:02d}.py"
        (dirpath / name).write_text("\n".join(body) + "\n", encoding="utf-8")
        names.append(name)
    return names


class _Serve:
    def __init__(self, python: str, dispatcher: Path, cwd: Path, env: dict) -> None:
        self.proc = subprocess.Popen([python, str(dispatcher), "--serve"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, cwd=str(cwd), env=env)
        self.ms: list = []
        self.skipped = 0
        self.nonzero = 0
        self.hook_ms: dict = {}
        self.cpu_s = 0.0

    def _cpu(self):
        """utime+stime+cutime+cstime (s) of the dispatcher: its own work plus every reaped hook child.
        Linux /proc only; None elsewhere."""
        try:
            with open(f"/proc/{self.proc.pid}/stat", encoding="ascii", errors="replace") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            return sum(int(x) for x in fields[11:15]) / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            return None

    def call(self, request: bytes, record: bool) -> None:
        cpu0 = self._cpu()
        t0 = time.monotonic()
        self.proc.stdin.write(request)
        self.proc.stdin.flush()
        rows = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("dispatcher exited mid-request")
            row = json.loads(line)
            if row.get("end"):
                break
            rows.append(row)
        if record:
            self.ms.append((time.monotonic() - t0) * 1000.0)
            cpu1 = self._cpu()
            if cpu0 is not None and cpu1 is not None and self.cpu_s is not None:
                self.cpu_s += cpu1 - cpu0
            else:
                self.cpu_s = None
            self.skipped += sum(1 for r in rows if r.get("skipped"))
            self.nonzero += sum(1 for r in rows if r.get("rc") not in (0, None) or r.get("timed_out"))
            for r in rows:
                if "elapsed" in r:
                    self.hook_ms.setdefault(r["i"], []).append(r["elapsed"] * 1000.0)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def _pct(sorted_ms: list, q: float) -> float:
    k = max(1, -(-len(sorted_ms) * q // 100))
    return sorted_ms[int(k) - 1]


def _stats(s: _Serve, names: list) -> dict:
    ms = sorted(s.ms)
    return {"calls": len(ms), "mean_ms": round(statistics.fmean(ms), 1), "p50_ms": round(_pct(ms, 50), 1),
            "p90_ms": round(_pct(ms, 90), 1), "max_ms": round(ms[-1], 1), "hooks_skipped": s.skipped,
            "hook_rows_nonzero_rc": s.nonzero,
            "cpu_ms_per_call": round(s.cpu_s * 1000.0 / len(ms), 1) if s.cpu_s is not None else None,
            "hook_mean_ms": {names[i]: round(statistics.fmean(v), 1) for i, v in sorted(s.hook_ms.items())}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hooks-dir", type=Path)
    ap.add_argument("--hook", action="append", default=[], help="hook file name in --hooks-dir (repeat); default all *.py")
    ap.add_argument("--synthetic", action="store_true", help="generate a 15-hook fixture instead of --hooks-dir")
    ap.add_argument("--n", type=int, default=200, help="measured calls per dispatcher")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--before", default="origin/main", help="git ref or dispatcher path (default origin/main)")
    ap.add_argument("--after", type=Path, default=AFTER)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--timeout", type=float, default=30.0, help="per-hook timeout in the request")
    ap.add_argument("--home", default=None, help="home dir for the hooks (default: a new temp dir; 'inherit' keeps yours)")
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="bench-shell-hooks-"))
    try:
        if a.synthetic:
            hooks_dir = tmp / "hooks"
            names = _synthetic(hooks_dir)
        else:
            if a.hooks_dir is None:
                ap.error("--hooks-dir or --synthetic is required")
            hooks_dir = a.hooks_dir.expanduser().resolve()
            names = a.hook or sorted(p.name for p in hooks_dir.glob("*.py") if not p.name.startswith("test_"))
        scripts = [str(hooks_dir / n) for n in names]
        missing = [s for s in scripts if not os.path.isfile(s)]
        if missing:
            ap.error(f"hook files not found: {missing}")
        before = _resolve_before(a.before, tmp)
        work = tmp / "cwd"
        work.mkdir()
        env = dict(os.environ)
        if a.home != "inherit":
            home = (Path(a.home).expanduser() if a.home else tmp / "home").resolve()
            home.mkdir(parents=True, exist_ok=True)
            env["HERMES_HOME"] = str(home)
            env["HOME"] = str(home.resolve())  # hooks using Path.home() / ~ stay in the scratch home too
        hooks = [{"script": s, "args": [], "timeout": a.timeout} for s in scripts]
        requests = []
        for i in range(a.warmup + a.n):
            payload = json.dumps({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                                  "tool_input": {"command": COMMANDS[i % len(COMMANDS)]}, "session_id": "bench",
                                  "cwd": str(work), "extra": {}})
            requests.append((json.dumps({"hooks": hooks, "stdin": payload}) + "\n").encode("utf-8"))
        sides = {"before": _Serve(a.python, before, work, env),
                 "after": _Serve(a.python, a.after.expanduser().resolve(), work, env)}
        try:
            for i, req in enumerate(requests):
                for side in (("before", "after") if i % 2 == 0 else ("after", "before")):
                    sides[side].call(req, record=i >= a.warmup)
        finally:
            for s in sides.values():
                s.close()
        result = {"hooks": len(scripts), "hook_bytes": sum(os.path.getsize(s) for s in scripts),
                  "python": a.python, "before": a.before, "after": str(a.after),
                  "load1_end": round(os.getloadavg()[0], 1), "ncpu": os.cpu_count()}  # admission-lint: not-a-decider bench telemetry only
        for side, s in sides.items():
            result[side] = _stats(s, names)
        b, f = result["before"], result["after"]
        result["speedup_mean"] = round(b["mean_ms"] / f["mean_ms"], 2) if f["mean_ms"] else None
        result["speedup_p90"] = round(b["p90_ms"] / f["p90_ms"], 2) if f["p90_ms"] else None
        print(json.dumps(result, sort_keys=True))
        sys.stderr.write("%-7s %8s %8s %8s %8s %9s\n" % ("", "mean", "p50", "p90", "max", "cpu"))
        for side in ("before", "after"):
            r = result[side]
            cpu = r["cpu_ms_per_call"]
            sys.stderr.write("%-7s %8.1f %8.1f %8.1f %8.1f %9s  ms/call\n"
                             % (side, r["mean_ms"], r["p50_ms"], r["p90_ms"], r["max_ms"],
                                "-" if cpu is None else "%.1f" % cpu))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
