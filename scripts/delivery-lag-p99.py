#!/usr/bin/env python3
# SCHEDULE: manual (operator acceptance read for t_aeae3fe7 / t_a1ec1342)
"""delivery-lag-p99 -- gateway reply delivery lag (``response ready`` -> ``Sending response``) per hour.

2026-10-24: Discord replies reached users 1-16 min after the agent finished (p50 64-119 s, max 963 s)
because the reply-path transcript persist queued behind a SessionDB._lock held for 16-28 s
(t_aeae3fe7). This reads a gateway log and reports the lag that users see.

Two sources, per line:
  * ``Sending response (N chars) to <chat> delivery_lag_s=X.X`` -- the gauge (preferred, exact).
  * otherwise a ``response ready: platform=<p> chat=<c> ...`` line paired with the next
    ``[<p>] Sending response (...) to <c>`` line (no gauge), lag = timestamp difference.

    python3 scripts/delivery-lag-p99.py --log ~/.hermes/logs/gateway.log
    python3 scripts/delivery-lag-p99.py --log gw.log --since 1792890000 --window 3600 --max-p99-s 5

Exit: 0 = p99 within --max-p99-s (or no limit given), 1 = over the limit, 2 = no samples in window.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.](\d{1,6}))?")
_READY = re.compile(r"response ready: platform=(\S+) chat=(\S+)")
_SEND = re.compile(r"\[([^\]]+)\] Sending response \(\d+ chars\) to (\S+)(?: delivery_lag_s=([0-9.]+))?")


def _epoch(line: str):
    m = _TS.match(line)
    if not m:
        return None
    base = datetime.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()
    frac = m.group(2)
    return base + (int(frac) / 10 ** len(frac) if frac else 0.0)


def samples(lines, platform: str):
    """Yield ``(send_epoch, lag_s)`` for *platform* (``all`` = every platform)."""
    pending = {}  # (platform, chat) -> ready epoch, oldest first
    for line in lines:
        ts = _epoch(line)
        if ts is None:
            continue
        m = _READY.search(line)
        if m:
            key = (m.group(1).lower(), m.group(2))
            pending.setdefault(key, []).append(ts)
            continue
        m = _SEND.search(line)
        if not m:
            continue
        key = (m.group(1).lower(), m.group(2))
        queue = pending.get(key) or []
        ready = queue.pop(0) if queue else None
        if platform != "all" and key[0] != platform:
            continue
        if m.group(3) is not None:
            yield ts, float(m.group(3))
        elif ready is not None:
            yield ts, max(0.0, ts - ready)


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
    ap.add_argument("--platform", default="discord", help="platform name, or 'all'")
    ap.add_argument("--window", type=float, default=3600.0)
    ap.add_argument("--since", type=float, default=None, help="window start (epoch); default now-window")
    ap.add_argument("--max-p99-s", type=float, default=None)
    a = ap.parse_args(argv)
    lo = a.since if a.since is not None else time.time() - a.window
    hi = lo + a.window
    try:
        with open(a.log, encoding="utf-8", errors="replace") as fh:
            rows = [(ts, lag) for ts, lag in samples(fh, a.platform.lower()) if lo <= ts < hi]
    except OSError as exc:
        print(json.dumps({"verdict": "BLIND", "error": str(exc), "log": a.log}))
        return 2
    if not rows:
        print(json.dumps({"verdict": "BLIND", "n": 0, "log": a.log, "from": int(lo), "to": int(hi)}))
        return 2
    hours = {}
    for ts, lag in rows:
        hours.setdefault(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00"), []).append(lag)
    for hour in sorted(hours):
        print(json.dumps({"hour": hour, **summarize(hours[hour])}, sort_keys=True))
    lags = sorted(lag for _, lag in rows)
    out = {"from": int(lo), "to": int(hi), "platform": a.platform, **summarize(lags)}
    ok = a.max_p99_s is None or pct(lags, 99) <= a.max_p99_s  # at the limit passes (Prism 71fb90530d15); unrounded: rounding is display only
    out["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
