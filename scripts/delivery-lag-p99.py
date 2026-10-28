#!/usr/bin/env python3
# SCHEDULE: manual (operator acceptance read for t_0072cc4d)
"""delivery-lag-p99 -- where a gateway reply spends ``response ready`` -> platform send.

Reads ``PHASE=delivery_stages`` lines (gateway/run_turn.py, logged only for a turn whose
ready->send tail exceeded 2 s) and prints p50/p90/p99/max of ``ready_to_send_s`` plus, per slow
turn, the dominant stage (footer / hooks / compress / persist / branch / deliver).

Lifted from closed ANG-Ventures/hermes-agent#2306 @ 9064fb70 (timestamp parsing, nearest-rank
percentile, CLI/exit shape); the ready/send line pairing was dropped because the gateway now
measures the tail itself.

    python3 scripts/delivery-lag-p99.py --log ~/.hermes/logs/gateway.log
    python3 scripts/delivery-lag-p99.py --log gw.log --since 1792890000 --window 3600 --slow-s 5

Exit: 0 = p99 within --max-p99-s (or no limit given), 1 = over the limit, 2 = no samples in window.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

STAGES = ("footer", "hooks", "compress", "persist", "branch", "deliver")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.](\d+))?(Z|[+-]\d{2}:?\d{2})?")
_PHASE = re.compile(r"PHASE=delivery_stages session=(\S+) ready_to_send_s=([0-9.]+)(.*)$")
_KV = re.compile(r"\b(\w+)=([0-9.]+)\b")


def _epoch(line: str):
    m = _TS.match(line)
    if not m:
        return None
    dt = datetime.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
    tz = m.group(3)
    if tz == "Z":
        dt = dt.replace(tzinfo=timezone.utc)
    elif tz:
        sign = -1 if tz[0] == "-" else 1
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[-2:]))))
    frac = m.group(2)
    return dt.timestamp() + (float("0." + frac) if frac else 0.0)  # naive = host local time


def samples(lines):
    """Yield ``(epoch, session, total_s, {stage: s}, deliver_pre_send_s or None)``."""
    for line in lines:
        m = _PHASE.search(line)
        if m is None:
            continue
        ts = _epoch(line)
        if ts is None:
            continue
        kv = {k: float(v) for k, v in _KV.findall(m.group(3))}
        yield ts, m.group(1), float(m.group(2)), {s: kv.get(s, 0.0) for s in STAGES}, kv.get("deliver_pre_send")


def dominant(stages: dict) -> str:
    return max(STAGES, key=lambda s: stages.get(s, 0.0))


def pct(sorted_vals, q: float) -> float:
    k = max(1, int(-(-q * len(sorted_vals) // 100)))  # nearest rank
    return sorted_vals[k - 1]


def summarize(vals) -> dict:
    v = sorted(vals)
    return {"n": len(v), "p50_s": round(pct(v, 50), 1), "p90_s": round(pct(v, 90), 1),
            "p99_s": round(pct(v, 99), 1), "max_s": round(v[-1], 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", default=os.path.expanduser("~/.hermes/logs/gateway.log"))
    ap.add_argument("--window", type=float, default=3600.0)
    ap.add_argument("--since", type=float, default=None, help="window start (epoch); default now-window")
    ap.add_argument("--slow-s", type=float, default=5.0, help="list the dominant stage of turns above this")
    ap.add_argument("--max-p99-s", type=float, default=None)
    a = ap.parse_args(argv)
    lo = a.since if a.since is not None else time.time() - a.window
    hi = lo + a.window
    try:
        with open(a.log, encoding="utf-8-sig", errors="replace") as fh:
            rows = [r for r in samples(fh) if lo <= r[0] < hi]
    except OSError as exc:
        print(json.dumps({"verdict": "BLIND", "error": str(exc), "log": a.log}))
        return 2
    if not rows:
        print(json.dumps({"verdict": "BLIND", "n": 0, "log": a.log, "from": int(lo), "to": int(hi)}))
        return 2
    for ts, session, total, stages, pre in rows:
        if total > a.slow_s:
            stage = dominant(stages)
            print(json.dumps({"at": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "session": session, "ready_to_send_s": total, "dominant": stage,
                              "dominant_s": stages[stage], "deliver_pre_send_s": pre}, sort_keys=True))
    totals = sorted(r[2] for r in rows)
    counts = {}
    for r in rows:
        counts[dominant(r[3])] = counts.get(dominant(r[3]), 0) + 1
    out = {"from": int(lo), "to": int(hi), "dominant_counts": counts, **summarize(totals)}
    ok = a.max_p99_s is None or pct(totals, 99) <= a.max_p99_s
    out["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
