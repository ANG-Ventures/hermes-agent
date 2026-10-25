#!/usr/bin/env python3
# SCHEDULE: manual (operator acceptance read for t_aeae3fe7 / t_a1ec1342)
"""delivery-lag-p99 -- gateway reply delivery lag (``response ready`` -> ``Sending response``) per hour.

2026-10-24: Discord replies reached users 1-16 min after the agent finished (p50 64-119 s, max 963 s)
because the reply-path transcript persist queued behind a SessionDB._lock held for 16-28 s
(t_aeae3fe7). This reads a gateway log and reports the lag that users see.

Two sources, per line:
  * ``Sending response (N chars) to <chat> delivery_lag_s=X.X`` -- the gauge (preferred, exact: it is
    measured on the turn's own event).
  * otherwise a ``response ready: platform=<p> chat=<c> ...`` line paired with a later
    ``[<p>] Sending response (...) to <c>`` line, lag = timestamp difference. When both lines carry a
    ``turn=<id>`` token they pair by turn id. Otherwise (at least one side has no turn id; a ready
    with a different turn id is never taken) a send pairs only with the LAST ready in the same chat, and only if no other ready for that chat came in between: a ready superseded by a
    newer one (a cancelled or interrupted reply) is dropped, never paired with the next turn's send
    (Prism b0dbbe468ced). Dropped readies and sends with nothing to pair are counted as ``unpaired``.

Timestamps: ``YYYY-MM-DD HH:MM:SS[,.frac]`` in the host's local time, or with a ``Z`` / ``+HH:MM`` /
``-HHMM`` suffix honoured (Prism d756c5c3a7d8).

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
from datetime import datetime, timedelta, timezone

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.](\d{1,6}))?(Z|[+-]\d{2}:?\d{2})?")
_READY = re.compile(r"response ready: platform=(\S+) chat=(\S+)")
_SEND = re.compile(r"\[([^\]]+)\] Sending response \(\d+ chars\) to (\S+)")
_LAG = re.compile(r"\bdelivery_lag_s=([0-9.]+)")
_TURN = re.compile(r"\bturn=(\S+)")


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
        hh, mm = int(tz[1:3]), int(tz[-2:])
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
    frac = m.group(2)
    return dt.timestamp() + (int(frac) / 10 ** len(frac) if frac else 0.0)  # naive = host local time


def samples(lines, platform: str, unpaired=None):
    """Yield ``(send_epoch, lag_s)`` for *platform* (``all`` = every platform). Epochs of readies and
    sends that could not be paired are appended to *unpaired* when given."""
    unpaired = [] if unpaired is None else unpaired
    pending = {}  # (platform, chat) -> (ready epoch, turn id or None): the LAST ready only
    by_turn = {}  # turn id -> ((platform, chat), ready epoch)

    def wanted(key):
        return platform == "all" or key[0] == platform

    for line in lines:
        ts = _epoch(line)
        if ts is None:
            continue
        turn = _TURN.search(line)
        turn = turn.group(1) if turn else None
        m = _READY.search(line)
        if m:
            key = (m.group(1).lower(), m.group(2))
            if turn is not None:
                by_turn[turn] = (key, ts)
            prev = pending.get(key)
            if prev is not None and prev[1] is None and wanted(key):
                unpaired.append(prev[0])  # superseded with no turn id: never paired with a later send
            pending[key] = (ts, turn)  # a superseded ready WITH a turn id can still pair by turn
            continue
        m = _SEND.search(line)
        if not m:
            continue
        key = (m.group(1).lower(), m.group(2))
        ready = None
        if turn is not None and turn in by_turn:
            rkey, ready = by_turn.pop(turn)
            if pending.get(rkey, (None, None))[1] == turn:
                pending.pop(rkey)
        else:
            entry = pending.get(key)
            # Chat fallback only when at least one side has no turn id: a ready carrying a DIFFERENT
            # turn id is another turn's, never this send's (Prism 35b6a882d9fe); it stays pending.
            if entry is not None and (turn is None or entry[1] is None):
                pending.pop(key)
                ready = entry[0]
                if entry[1] is not None:
                    by_turn.pop(entry[1], None)
        if not wanted(key):
            continue
        lag = _LAG.search(line)
        if lag is not None:
            yield ts, float(lag.group(1))
        elif ready is not None:
            yield ts, max(0.0, ts - ready)
        else:
            unpaired.append(ts)
    unpaired.extend(ts for key, (ts, t) in pending.items() if t is None and wanted(key))
    unpaired.extend(ts for key, ts in by_turn.values() if wanted(key))


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
        unpaired = []
        with open(a.log, encoding="utf-8", errors="replace") as fh:
            rows = [(ts, lag) for ts, lag in samples(fh, a.platform.lower(), unpaired) if lo <= ts < hi]
        n_unpaired = sum(1 for ts in unpaired if lo <= ts < hi)
    except OSError as exc:
        print(json.dumps({"verdict": "BLIND", "error": str(exc), "log": a.log}))
        return 2
    if not rows:
        print(json.dumps({"verdict": "BLIND", "n": 0, "unpaired": n_unpaired, "log": a.log,
                          "from": int(lo), "to": int(hi)}))
        return 2
    hours = {}
    for ts, lag in rows:
        hours.setdefault(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00"), []).append(lag)
    for hour in sorted(hours):
        print(json.dumps({"hour": hour, **summarize(hours[hour])}, sort_keys=True))
    lags = sorted(lag for _, lag in rows)
    out = {"from": int(lo), "to": int(hi), "platform": a.platform, "unpaired": n_unpaired, **summarize(lags)}
    ok = a.max_p99_s is None or pct(lags, 99) <= a.max_p99_s  # at the limit passes (Prism 71fb90530d15); unrounded: rounding is display only
    out["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
