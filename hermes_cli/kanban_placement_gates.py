"""Closeout gates for the default-remote flip (Control-Plane Protection spec
v0.3 §3.1 / §3.2.2, Phase 2; Momus pass 3 R1, R2, R4, R10; card t_667f1f6a).

Offline readers only: nothing at runtime calls this module. Each gate fails
CLOSED: missing data, an unknown refusal code, a half-migrated record or a gap
in the tick series is RED, never a vacuous 0.

* :func:`check_i1` (AC-2): local spawns of portable cards under
  ``placement.default: remote`` that the pool did NOT refuse with a known code.
  The SQL is generated from ``kanban_placement_reasons.TAKE_REFUSALS`` (R4), so
  a new take() code cannot be silently allowed or silently forbidden.
* :func:`check_i0` (AC-3 live leg): over ``var/gate-ticks*.jsonl`` the per-board
  key ``portable_waiting_with_room`` must be 0 on every tick (R1); quota the
  board split stranded is reported as ``stranded_allowance`` ticks, outside the
  gate; a gap > 2 ticks, coverage < 95 % or a line without the fields is RED (R2).
* :func:`missing_capability_runs` (R10): remote runs that failed, or completed
  half-done, for a capability their host lacks, per day, against a rollback
  trigger.

    python -m hermes_cli.kanban_placement_gates i1 --since <epoch> [--board-db PATH]
    python -m hermes_cli.kanban_placement_gates i0 --since <epoch> [--until <epoch>] [--interval 60]
    python -m hermes_cli.kanban_placement_gates r10 --days 14 [--board-db PATH]

Exit 0 = GREEN, 1 = RED, 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

from hermes_cli import kanban_db_capability_gap as _gap
from hermes_cli import kanban_placement_reasons as _pr
from hermes_cli import kanban_worker_pool as _kwp

KIND = _pr.PLACEMENT_EVENT
MAX_GAP_TICKS = 2
MIN_COVERAGE = 0.95

# Missing-capability strings (spec §4 0b (iii), the candidate filter for P and
# the R10 detector). A match is a candidate, never a label.
MISSING_CAPABILITY_RE = re.compile(
    r"turns\.db|kanban\.db|gateway\.log|load_gate\.json|\bPAT\b|gh auth"
    r"|docker: command not found|Cannot connect to the Docker daemon"
    r"|Permission denied \(publickey\)|sudo: a password is required"
    r"|[A-Z][A-Z_]+_ABSENT\b",
    re.IGNORECASE,
)
# Rollback trigger (R10): remote runs per day that failed for a missing
# capability. Above this, or above 2x the 14-day pre-flip daily mean (whichever
# is higher), the flip is rolled back (`default: local`) and every hit becomes
# a candidate P entry.
ROLLBACK_TRIGGER_PER_DAY = 3


def _quote(code: str) -> str:
    return "'" + code.replace("'", "''") + "'"


def i1_violation_sql() -> str:
    """The I-1 predicate (spec §3.1), generated from the frozen vocabulary.

    "Local" is ``reason != 'pool'``: the placement record names the
    dispatcher's own host by its claimer id (``mac-studio-m3u``), never the pin
    word ``studio``, so the query keys on the route, not on a host string."""
    codes = ", ".join(_quote(c) for c in sorted(_pr.TAKE_REFUSALS))
    code = ("CASE WHEN instr(json_extract(e.payload,'$.take_refusal'),' (') > 0 "
            "THEN substr(json_extract(e.payload,'$.take_refusal'), 1, "
            "instr(json_extract(e.payload,'$.take_refusal'),' (') - 1) "
            "ELSE json_extract(e.payload,'$.take_refusal') END")
    return (
        "SELECT e.task_id, e.payload FROM task_events e "
        "WHERE e.kind = :kind AND e.created_at >= :since "
        "AND json_extract(e.payload,'$.route_class') = 'portable' "
        f"AND json_extract(e.payload,'$.placement_default') = {_quote(_kwp.PLACEMENT_DEFAULT_REMOTE)} "
        "AND json_extract(e.payload,'$.reason') <> 'pool' "
        f"AND (json_extract(e.payload,'$.take_refusal') IS NULL OR ({code}) NOT IN ({codes}))"
    )


FIELD_GUARD_SQL = (
    "SELECT count(*) FROM task_events WHERE kind = :kind AND created_at >= :since "
    "AND json_extract(payload,'$.placement_default') = 'remote' "
    "AND (json_extract(payload,'$.route_class') IS NULL OR json_extract(payload,'$.host') IS NULL "
    "OR json_extract(payload,'$.reason') IS NULL OR json_extract(payload,'$.pool_free_at_spawn') IS NULL "
    "OR (json_extract(payload,'$.reason') = 'last-resort' AND json_extract(payload,'$.take_refusal') IS NULL))"
)
# A record with no placement_default in the window is half-migrated: it is
# invisible to the remote-only queries above, so it is counted, never excluded.
NO_DEFAULT_SQL = (
    "SELECT count(*) FROM task_events WHERE kind = :kind AND created_at >= :since "
    "AND json_extract(payload,'$.placement_default') IS NULL"
)
KIND_GUARD_SQL = (
    "SELECT count(DISTINCT kind) FROM task_events WHERE kind IN ('placement','spawned') "
    "AND created_at >= :since AND json_extract(payload,'$.route_class') IS NOT NULL"
)
LAST_RESORT_SQL = (
    "SELECT json_extract(payload,'$.take_refusal') FROM task_events WHERE kind = :kind "
    "AND created_at >= :since AND json_extract(payload,'$.reason') = 'last-resort'"
)


def check_i1(conn: sqlite3.Connection, since: float) -> dict:
    """AC-2: I-1 violations, the kind guard, the field guard and the
    last-resort histogram over ``placement`` events since ``since``."""
    args = {"kind": KIND, "since": int(since)}
    violations = [{"task_id": r[0], "payload": json.loads(r[1] or "{}")}
                  for r in conn.execute(i1_violation_sql(), args)]
    kinds = conn.execute(KIND_GUARD_SQL, args).fetchone()[0]
    incomplete = conn.execute(FIELD_GUARD_SQL, args).fetchone()[0]
    no_default = conn.execute(NO_DEFAULT_SQL, args).fetchone()[0]
    hist: dict = {}
    for (refusal,) in conn.execute(LAST_RESORT_SQL, args):
        code = _pr.take_refusal_code(refusal) or "null"
        hist[code] = hist.get(code, 0) + 1
    remote_rows = conn.execute(
        "SELECT count(*) FROM task_events WHERE kind = :kind AND created_at >= :since "
        "AND json_extract(payload,'$.placement_default') = 'remote'", args).fetchone()[0]
    red = []
    if violations:
        red.append(f"i1: {len(violations)} local portable spawn(s) the pool did not refuse")
    if kinds != 1:
        red.append(f"kind guard: {kinds} record kinds carry route_class (must be 1)")
    if incomplete:
        red.append(f"field guard: {incomplete} placement record(s) missing a queried field")
    if no_default:
        red.append(f"field guard: {no_default} record(s) missing placement_default")
    if remote_rows == 0:
        red.append("no placement record under default: remote in the window (nothing proven)")
    return {"green": not red, "red": red, "violations": violations,
            "last_resort": sum(hist.values()), "last_resort_by_refusal": hist,
            "remote_records": remote_rows, "missing_placement_default": no_default}


def series_files(root: Path) -> List[Path]:
    var = Path(root) / "var"
    return sorted(var.glob("gate-ticks.????-??-??.jsonl")) + [var / "gate-ticks.jsonl"]


def read_series(paths: Iterable[Path]) -> List[dict]:
    out: List[dict] = []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                out.append({"_corrupt": line[:80]})
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


I0_FIELDS = ("portable_waiting", "allowance_unused", "portable_waiting_with_room",
             "stranded_allowance", "pool_planned")
_I0_COUNTERS = I0_FIELDS[:4]


def _i0_valid(r: dict) -> bool:
    return (all(isinstance(r[f], (int, float)) and not isinstance(r[f], bool) for f in _I0_COUNTERS)
            and isinstance(r["pool_planned"], bool))


def check_i0(lines: List[dict], *, since: float, until: float, interval: float) -> dict:
    """AC-3 live leg over the tick series, fail-closed on missing data (R2)."""
    window = [r for r in lines if r.get("path") == "gateway"
              and isinstance(r.get("ts"), (int, float)) and since <= r["ts"] < until]
    corrupt = sum(1 for r in lines if "_corrupt" in r)
    window.sort(key=lambda r: r["ts"])
    red: List[str] = []
    # More than MAX_GAP_TICKS ticks missing in a row = a spacing past
    # (MAX_GAP_TICKS + 1) intervals; half an interval of slack absorbs tick jitter.
    max_gap = (MAX_GAP_TICKS + 1.5) * interval
    edges = [since] + [r["ts"] for r in window] + [until]
    gaps = [(a, b) for a, b in zip(edges, edges[1:]) if b - a > max_gap]
    for a, b in gaps:
        red.append(f"gap: {b - a:.0f}s with no gateway tick between "
                   f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(a))} and "
                   f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(b))}")
    missing = [r for r in window if any(f not in r for f in I0_FIELDS)]
    # A record whose invariant inputs are not numbers (or pool_planned not a
    # bool) measured nothing: it is not coverage and never reads as 0.
    bad = {id(r) for r in missing}
    nulls = [r for r in window if id(r) not in bad and not _i0_valid(r)]
    bad |= {id(r) for r in nulls}
    # Coverage is TIME-based: the share of the window not inside a > MAX_GAP_TICKS gap,
    # times the share of lines with non-null inputs. A tick-COUNT ratio against the nominal
    # interval reads ~0.85 on a gap-free live series (ticks land every interval + tick
    # work: median 70 s at interval 60 on 2026-10-15) and could never pass.
    span = max(until - since, 1e-9)
    gap_time = sum(b - a for a, b in gaps)
    good = len(window) - len(missing) - len(nulls)
    coverage = (1 - gap_time / span) * (good / len(window) if window else 0.0)
    expected = max(1, int(span / interval))
    if missing:
        red.append(f"field guard: {len(missing)} tick line(s) lack {I0_FIELDS}")
    if coverage < MIN_COVERAGE:
        red.append(f"coverage: {coverage:.3f} < {MIN_COVERAGE} ({good} usable tick(s), "
                   f"{gap_time:.0f}s of {span:.0f}s in gaps, {len(nulls)} null)")
    if nulls:
        red.append(f"invalid: {len(nulls)} tick line(s) with a null/non-numeric I-0 counter "
                   f"or a non-bool pool_planned")
    if corrupt:
        red.append(f"corrupt: {corrupt} unparseable line(s)")
    valid = [r for r in window if id(r) not in bad]
    violations = [r for r in valid if r["portable_waiting_with_room"] > 0]
    if violations:
        red.append(f"i0: {len(violations)} tick(s) where a pool-refused portable row waited "
                   f"with room on its own board")
    stranded = [r for r in valid if r["stranded_allowance"] > 0]
    return {
        "green": not red, "red": red, "ticks": len(window), "expected": expected,
        "coverage": round(coverage, 4), "gaps": gaps, "violations": len(violations),
        "stranded_ticks": len(stranded),
        "stranded_allowance": sum(int(r["stranded_allowance"]) for r in stranded),
        "null_ticks": len(nulls),
        "unplanned_ticks": sum(1 for r in window if r.get("pool_planned") is False),
        "unplanned_reasons": sorted({str(r.get("pool_reason")) for r in window
                                     if r.get("pool_planned") is False}),
        # The pre-R1 v0.3 query, kept for comparison only: it also counts
        # stranded quota, which is not the I-0 violation.
        "legacy_portable_and_unused": sum(1 for r in window if (r.get("portable_waiting") or 0) > 0
                                          and (r.get("allowance_unused") or 0) > 0),
    }


_R10_SQL = """
SELECT r.id, r.task_id, r.outcome, r.ended_at, coalesce(r.error,''), coalesce(r.summary,''),
       json_extract(p.payload,'$.host')
FROM task_runs r JOIN task_events p ON p.run_id = r.id AND p.kind = 'worker_placed'
WHERE r.ended_at >= :since AND r.ended_at < :until
"""
_R10_EVENTS = (_gap.EVENT_KIND, "placement_reroute", "placement_custody_hold")


def missing_capability_runs(conn: sqlite3.Connection, since: float, until: float) -> List[dict]:
    """Remote runs ended in [since, until) that failed, or completed half-done,
    for a missing capability (R10): a rail/capability event on the run, or a
    missing-capability string in a failed run's error/summary or a completed
    run's error (never a completed run's summary)."""
    out = []
    marks = ",".join("?" * len(_R10_EVENTS))
    for rid, tid, outcome, ended, err, summary, host in conn.execute(
            _R10_SQL, {"since": int(since), "until": int(until)}):
        ev = conn.execute(f"SELECT kind FROM task_events WHERE run_id = ? AND kind IN ({marks}) LIMIT 1",
                          (rid, *_R10_EVENTS)).fetchone()
        # A COMPLETED run is half-done only on positive evidence: a capability
        # event on the run or its ERROR field. Its summary is never evidence:
        # healthy runs say "Updated kanban.db; all tests passed".
        text = err if outcome == "completed" else f"{err}\n{summary}"
        hit = MISSING_CAPABILITY_RE.search(text)
        if ev is None and hit is None:
            continue
        if ev is None and outcome not in ("blocked", "gave_up", "crashed", "completed"):
            continue
        out.append({"run_id": rid, "task_id": tid, "host": host, "outcome": outcome,
                    "ended_at": ended, "half_done": outcome == "completed",
                    "signal": ev[0] if ev else hit.group(0)})
    return out


def r10_report(conn: sqlite3.Connection, *, days: int, now: Optional[float] = None,
               baseline_daily: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    day0 = now - days * 86400
    per_day = []
    for d in range(days):
        a, b = day0 + d * 86400, day0 + (d + 1) * 86400
        hits = missing_capability_runs(conn, a, b)
        per_day.append({"day": time.strftime("%Y-%m-%d", time.localtime(a)), "n": len(hits),
                        "runs": [h["run_id"] for h in hits]})
    mean = sum(p["n"] for p in per_day) / max(1, days)
    if baseline_daily is None:
        # The trigger must come from the PRE-flip window; deriving it from the
        # window under test lets a steady bad rate set its own bar.
        return {"days": per_day, "mean": round(mean, 3), "trigger": None, "tripped": [],
                "green": False, "red": ["no pre-flip baseline: pass --baseline-daily"]}
    trigger = max(ROLLBACK_TRIGGER_PER_DAY, 2 * baseline_daily)
    tripped = [p for p in per_day if p["n"] > trigger]
    return {"days": per_day, "mean": round(mean, 3), "trigger": trigger,
            "tripped": [p["day"] for p in tripped], "green": not tripped}


def _board_conn(path: Optional[str]) -> sqlite3.Connection:
    if path:
        return sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True)
    from hermes_cli import kanban_db as _kb
    return sqlite3.connect(f"file:{_kb.kanban_db_path()}?mode=ro", uri=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kanban_placement_gates", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("i1")
    p1.add_argument("--since", type=float, required=True)
    p1.add_argument("--board-db")
    p0 = sub.add_parser("i0")
    p0.add_argument("--since", type=float, required=True)
    p0.add_argument("--until", type=float)
    p0.add_argument("--interval", type=float, default=60.0)
    p0.add_argument("--root", help="kanban home holding var/gate-ticks*.jsonl")
    p10 = sub.add_parser("r10")
    p10.add_argument("--days", type=int, default=14)
    p10.add_argument("--baseline-daily", type=float, required=True,
                     help="pre-flip daily mean (r10 over the pre-flip window)")
    p10.add_argument("--board-db")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "i1":
            with _board_conn(args.board_db) as conn:
                out = check_i1(conn, args.since)
        elif args.cmd == "i0":
            if args.root:
                root = Path(args.root).expanduser()
            else:
                from hermes_cli import kanban_db as _kb
                root = _kb.kanban_home()
            out = check_i0(read_series(series_files(root)), since=args.since,
                           until=args.until or time.time(), interval=args.interval)
        else:
            with _board_conn(args.board_db) as conn:
                out = r10_report(conn, days=args.days, baseline_daily=args.baseline_daily)
    except (sqlite3.Error, OSError) as exc:
        print(f"could not run: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1, default=str))
    return 0 if out["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
