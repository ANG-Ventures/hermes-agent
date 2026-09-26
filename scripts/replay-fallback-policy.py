#!/usr/bin/env python3
"""Replay the Phase 1 fallback ledger through the old and the new (§4.2/4.3)
policy and print model legs per ``quota_model`` event per session.

Fallback spec rev12 §5 Phase 2 "Replay". This is an implementation check,
not an acceptance gate: it cannot observe probes the old policy never made.

Old policy = the legs the ledger actually recorded (every ``failover`` and
``recovery`` row from a ``quota_model`` failover until the session's next
``quota_model`` failover). New policy = the sticky writer arms
``until = ts + compute_cooldown_s('quota_model', n_c)`` on that failover; a
recorded return before ``until`` is suppressed together with the re-failover
it caused; after ``until`` a return is allowed only on ``fallback_cold``
(> 60 min since the session's previous event) or ``compaction`` (session id
rotated), the branches a ledger replay can evaluate.

    python3 scripts/replay-fallback-policy.py --ledger 48h [--db PATH] [--json]

Exit 0 when ``max_legs_per_quota_event <= 2`` (``--target``), else 1. With no
``quota_model`` events in the window the result is printed as VACUOUS (exit 0):
there is nothing to replay, which is not evidence the policy works.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import fallback_policy as fp  # noqa: E402

FALLBACK_COLD_S = fp.FALLBACK_COLD_S


def parse_window(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError(f"bad window {text!r} (e.g. 48h, 2d, 90m)")
    return float(m.group(1)) * {"": 3600, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def default_db() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "blackbox" / "turns.db"


def load_rows(db: Path, since: float) -> List[Dict[str, Any]]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT ts, session_id, kind, trigger_class, from_provider, from_model,"
            " to_provider, to_model FROM fallback_events"
            " WHERE ts >= ? AND kind IN ('failover','recovery') ORDER BY ts, id", (since,))]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _session_of(row: Dict[str, Any]) -> str:
    return str(row.get("session_id") or "?")


def replay_session(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per ``quota_model`` failover in one session: old vs new legs."""
    events: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    n_c = 0
    last_ts: Optional[float] = None
    for r in rows:
        ts = float(r["ts"])
        gap = None if last_ts is None else ts - last_ts
        last_ts = ts
        if r["kind"] == "failover" and r.get("trigger_class") == "quota_model":
            if cur is not None and not cur["_closed"]:
                cur["old_legs"] += 1   # old policy re-failed over; new stays sticky
                continue
            if (cur is not None and cur["_returned_at"] is not None
                    and ts - cur["_returned_at"] <= fp.N_C_WINDOW_S):
                n_c += 1               # same class again within 1h of the restore
            else:
                n_c = 0
            until = ts + (fp.compute_cooldown_s("quota_model", n_c) or 0.0)
            cur = {"ts": ts, "old_legs": 1, "new_legs": 1, "_until": until,
                   "_on_fallback_new": True, "_closed": False, "_returned_at": None}
            events.append(cur)
            continue
        if cur is None:
            continue
        cur["old_legs"] += 1
        if not cur["_on_fallback_new"]:
            # new policy already returned; a later recorded leg is counted
            # as it happened (the replay has no better knowledge).
            cur["new_legs"] += 1
            continue
        if r["kind"] == "recovery":
            cold = gap is not None and gap > FALLBACK_COLD_S
            if ts >= cur["_until"] and cold:
                cur["new_legs"] += 1
                cur["_on_fallback_new"] = False
                cur["_closed"] = True
                cur["_returned_at"] = ts
            # else: suppressed (stays sticky); its re-failover is suppressed too
    for e in events:
        for k in [k for k in e if k.startswith("_")]:
            e.pop(k)
    return events


def replay(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    by_session: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    n_rows = 0
    for r in rows:
        n_rows += 1
        by_session[_session_of(r)].append(r)
    per_session = {sid: ev for sid, rs in by_session.items() if (ev := replay_session(rs))}
    all_ev = [e for ev in per_session.values() for e in ev]
    return {
        "ledger_rows": n_rows,
        "sessions": len(by_session),
        "quota_model_events": len(all_ev),
        "max_legs_per_quota_event_old": max((e["old_legs"] for e in all_ev), default=0),
        "max_legs_per_quota_event": max((e["new_legs"] for e in all_ev), default=0),
        "vacuous": not all_ev,
        "per_session": per_session,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--ledger", type=parse_window, default=parse_window("48h"),
                    help="window to replay, e.g. 48h (default)")
    ap.add_argument("--db", type=Path, default=None, help="turns.db (default: $HERMES_HOME/blackbox/turns.db)")
    ap.add_argument("--target", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    db = args.db or default_db()
    res = replay(load_rows(db, time.time() - args.ledger))
    res["db"] = str(db)
    res["window_s"] = args.ledger
    ok = res["max_legs_per_quota_event"] <= args.target
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        print(f"db={db} window={args.ledger / 3600:.0f}h ledger_rows={res['ledger_rows']} "
              f"sessions={res['sessions']} quota_model_events={res['quota_model_events']}")
        print(f"max_legs_per_quota_event_old={res['max_legs_per_quota_event_old']}")
        print(f"max_legs_per_quota_event={res['max_legs_per_quota_event']} (target <= {args.target})")
        if res["vacuous"]:
            print("VACUOUS: no quota_model failover rows in the window; nothing was replayed")
        print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
