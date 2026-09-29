"""Detector: ``turn_api_calls`` turn_ids that have no parent ``turns`` row.

Every per-call ledger row belongs to a conversation turn, and every turn must
end with a ``turns`` row (``on_session_end`` -> ``store.insert_turn``). A turn
that ended through an early return / raise used to skip that hook, so its
calls were orphaned and failed turns vanished from every turn-level surface
(t_6c09f0c2). This module counts such orphans so a scheduled check can gate
on the invariant instead of relying on someone noticing a missing failure.

CLI (stdout is empty and exit 0 when clean, so a no_agent cron stays silent)::

    python -m plugins.blackbox.orphans [--since-hours 24] [--settle-s 3600] \\
        [--max 0] [--repair [--dry-run]] [DB ...]

With no DB arguments it scans ``<home>/blackbox/turns.db`` plus every
``<home>/profiles/*/blackbox/turns.db`` (home = the Hermes home root). Databases are opened
read-only (``mode=ro``, never ``immutable=1`` -- they are live WAL stores).

``--repair`` writes ONE flagged ``turns`` row per orphan, built from the
turn's own ``turn_api_calls`` aggregates (ts span, calls, tokens, primary
route, cost): ``interrupted=1``, ``terminal_error='orphan_repair'``. The
insert is ``ON CONFLICT DO NOTHING`` and never moves a channel's last-turn
pointer, so a real row (present or written concurrently) always wins and a
repaired row can never pass as a normal one; it is fenced on the ledger it
was aggregated from, so a call landing mid-repair skips the turn for the
next run. Orphans exist because the process that owned the turn was killed
before ``on_session_end`` (SIGKILL, a pre-fix worker); the row's
chat/platform attribution is unknown and left empty. A store whose profile
config is unreadable or has ``record_subagents: false`` is reported and
never written (its orphans may be deliberate subagent drops).
"""

from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time
from typing import Iterable

# A turn whose most recent call is younger than this may still be running
# (its turns row lands at turn end), so it is not yet an orphan.
DEFAULT_SETTLE_S = 3600.0

_ORPHAN_SQL = """
    SELECT c.turn_id,
           MAX(c.ts) AS last_ts,
           SUM(CASE WHEN COALESCE(c.http_status, 200) != 200 THEN 1 ELSE 0 END)
    FROM turn_api_calls c
    WHERE NOT EXISTS (SELECT 1 FROM turns t WHERE t.turn_id = c.turn_id)
    GROUP BY c.turn_id
    HAVING MIN(c.ts) >= ? AND MAX(c.ts) < ?
"""


def orphan_turns(
    conn: sqlite3.Connection, *, since: float, settle_before: float
) -> list[tuple[str, float, int]]:
    """Return ``(turn_id, last_call_ts, error_calls)`` for every orphaned turn.

    Only turns whose FIRST call is at/after ``since`` and whose LAST call is
    before ``settle_before`` are considered.
    """
    return [
        (str(tid), float(last or 0.0), int(errs or 0))
        for tid, last, errs in conn.execute(_ORPHAN_SQL, (since, settle_before))
    ]


def default_db_paths(home: str | None = None) -> list[str]:
    if home is None:
        from plugins.blackbox.store import _db_path

        home = str(_db_path().parent.parent)
    root = home
    # A profile-scoped home (…/profiles/<name>) still scans the fleet root.
    parts = os.path.normpath(root).split(os.sep)
    if len(parts) >= 2 and parts[-2] == "profiles":
        root = os.sep.join(parts[:-2]) or os.sep
    paths = [os.path.join(root, "blackbox", "turns.db")]
    paths += glob.glob(os.path.join(root, "profiles", "*", "blackbox", "turns.db"))
    return sorted(p for p in set(paths) if os.path.exists(p))


def scan(
    paths: Iterable[str], *, since: float, settle_before: float
) -> dict[str, list[tuple[str, float, int]]]:
    out: dict[str, list[tuple[str, float, int]]] = {}
    for path in paths:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if {"turns", "turn_api_calls"} <= tables:
                out[path] = orphan_turns(conn, since=since, settle_before=settle_before)
        finally:
            conn.close()
    return out


REPAIR_MARKER = "orphan_repair"


def profile_for_path(path: str) -> str:
    """``.../profiles/<name>/blackbox/turns.db`` -> name; the root store -> default."""
    parts = os.path.normpath(path).split(os.sep)
    if len(parts) >= 4 and parts[-4] == "profiles" and parts[-2] == "blackbox":
        return parts[-3]
    return "default"


def _orphan_route(conn: sqlite3.Connection, turn_id: str) -> tuple[str, str, float, float]:
    """(provider, model, first_ts, last_ts) from the orphan's own calls.

    The primary route is the first main-lane call's (what a live turn would
    have recorded); an orphan with only aux calls falls back to its first
    call so the row still names the route that produced it.
    """
    first, last = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM turn_api_calls WHERE turn_id = ?", (turn_id,)
    ).fetchone()
    row = conn.execute(
        "SELECT provider, model FROM turn_api_calls WHERE turn_id = ? "
        "ORDER BY (COALESCE(lane_family, '') = 'aux'), (parent_call_id IS NOT NULL), seq "
        "LIMIT 1",
        (turn_id,),
    ).fetchone()
    provider, model = (row or ("", ""))
    return str(provider or ""), str(model or ""), float(first or 0.0), float(last or 0.0)


def store_config(path: str) -> dict | None:
    """The EFFECTIVE ``blackbox:`` block of the profile that OWNS ``path``.

    A repair process runs under one profile but writes every profile's store;
    ``record_subagents`` / ``store_text`` are per profile, so read the target
    home's own config.yaml (``<home>/blackbox/turns.db`` -> ``<home>/config.yaml``)
    the way the live recorder sees it: user file + the administrator's
    managed-scope overlay + ``${ENV}`` expansion. None when the file cannot be
    read or parsed -- an unknown config is not permission to write.
    """
    from plugins import blackbox

    try:
        from hermes_cli.config import _expand_env_vars, read_user_config_raw
        from hermes_cli.managed_scope import apply_managed_overlay

        home = os.path.dirname(os.path.dirname(os.path.abspath(path)))
        data = apply_managed_overlay(read_user_config_raw(os.path.join(home, "config.yaml")))
        block = _expand_env_vars(data).get("blackbox")
    except Exception:
        return None
    cfg = dict(blackbox._DEFAULTS)
    if isinstance(block, dict):
        cfg.update(block)
    return cfg


def repairable(path: str) -> tuple[bool, str]:
    """Whether every orphan in ``path`` is a defect the repair may close.

    The ledger carries no subagent identity. A profile with
    ``record_subagents: false`` drops subagent turns on purpose, so its
    orphans mix deliberate drops with real losses and a synthesized row would
    book excluded subagent spend as a top-level turn: such a store is
    reported, never repaired. An unreadable profile config is treated the
    same way (fail closed).
    """
    cfg = store_config(path)
    if cfg is None:
        return False, "profile config unreadable: cannot tell deliberate subagent drops from losses"
    if not bool(cfg.get("record_subagents", True)):
        return False, "record_subagents is false for this profile: orphans may be deliberate subagent drops"
    return True, ""


def repair_record(path: str, turn_id: str):
    """Build the flagged ``TurnRecord`` for one orphan from its ledger rows.

    Returns ``(record, fence)``: ``fence`` is the ``(last_call_ts, call_count)``
    the record was aggregated from, which ``insert_turn(ledger_fence=...)``
    re-checks inside its write transaction so a call that lands after this
    read can never be summarised away. ``(None, None)`` when the ledger could
    not be read (a failed read must not become a zero-usage row) or the
    profile records no such turn.
    """
    from plugins import blackbox
    from plugins.blackbox import store

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        provider, model, first_ts, last_ts = _orphan_route(conn, turn_id)
        n_calls, n_main = conn.execute(
            "SELECT COUNT(*), SUM(COALESCE(lane_family, '') != 'aux' "
            "AND parent_call_id IS NULL) FROM turn_api_calls WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
    finally:
        conn.close()
    usage = store.ledger_turn_usage(turn_id, db_path=path)
    if usage is None:
        if int(n_main or 0) > 0:
            return None, None  # main-lane rows exist but the read failed
        usage = {}  # verified aux-only: a flagged 0-call row still closes the orphan
    cfg = store_config(path)
    if cfg is None:
        return None, None
    record = blackbox._build_record(
        session_id=turn_id.split(":", 1)[0],
        interrupted=True,
        model=model,
        platform="",
        provider=provider,
        user_message="",
        final_response="",
        turn_usage=usage,
        cfg=cfg,
        kwargs={
            "turn_id": turn_id,
            "profile": profile_for_path(path),
            "provisional": True,
            "terminal_error": REPAIR_MARKER,
        },
    )
    if record is None:
        return None, None
    record.ts_start = first_ts
    record.ts_end = last_ts or first_ts
    return record, (last_ts, int(n_calls or 0))


def repair(
    results: dict[str, list[tuple[str, float, int]]], *, apply: bool = True
) -> dict[str, list[tuple[str, bool]]]:
    """Write one flagged row per orphan; ``(turn_id, written)`` per store.

    A row is only ever written where none exists (``insert_turn(provisional=
    True)`` is ``ON CONFLICT DO NOTHING``), so a turn whose real row lands
    between the scan and the write keeps the real row, and only while the
    ledger still matches what was aggregated (``ledger_fence``), so a call
    that lands in between is never summarised away. A store that is not
    ``repairable()`` is skipped whole (every entry ``False``). With
    ``apply=False`` nothing is written and every entry reports ``False``.

    Liveness is the caller's ``settle_before`` (``--settle-s``): a turn whose
    last call is older than that is taken as dead. A turn that is merely
    parked on a longer tool call gets a flagged row too; if it later
    finalizes, its real row upserts over the flagged one, and if it dies
    unfinalized the flagged row keeps the usage as of the repair -- partial
    by construction, which is what the ``orphan_repair`` marker says.
    """
    from plugins.blackbox import store

    out: dict[str, list[tuple[str, bool]]] = {}
    for path, rows in sorted(results.items()):
        ok, _why = repairable(path)
        done: list[tuple[str, bool]] = []
        for turn_id, _last_ts, _errs in rows:
            written = False
            if apply and ok:
                record, fence = repair_record(path, turn_id)
                written = record is not None and store.insert_turn(
                    record, provisional=True, db_path=path, ledger_fence=fence
                )
            done.append((turn_id, bool(written)))
        out[path] = done
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Count turn_api_calls turn_ids with no parent turns row."
    )
    ap.add_argument("dbs", nargs="*", help="turns.db paths (default: every profile)")
    ap.add_argument("--since-hours", type=float, default=24.0)
    ap.add_argument("--since", type=float, default=None, help="epoch lower bound (overrides --since-hours)")
    ap.add_argument("--settle-s", type=float, default=DEFAULT_SETTLE_S)
    ap.add_argument("--max", type=int, default=0, help="orphans tolerated before failing")
    ap.add_argument(
        "--repair", action="store_true",
        help=f"write a flagged turns row (interrupted=1, terminal_error={REPAIR_MARKER}) "
             "for every orphan in the window from its turn_api_calls aggregates; "
             "never touches an existing row",
    )
    ap.add_argument("--dry-run", action="store_true", help="with --repair: list, write nothing")
    args = ap.parse_args(argv)

    now = time.time()
    since = args.since if args.since is not None else now - args.since_hours * 3600.0
    results = scan(args.dbs or default_db_paths(), since=since, settle_before=now - args.settle_s)
    total = sum(len(v) for v in results.values())
    if args.repair:
        written = repair(results, apply=not args.dry_run)
        n_written = 0
        for path, rows in sorted(written.items()):
            if not rows:
                continue
            can, why = repairable(path)
            if not can:
                print(f"skipped {len(rows)} orphan turn(s) in {path}: {why}")
                continue
            ok_n = sum(1 for _tid, ok in rows if ok)
            n_written += ok_n
            head = f"would repair {len(rows)}" if args.dry_run else f"repaired {ok_n}/{len(rows)}"
            print(f"{head} orphan turn(s) in {path}")
            for tid, ok in rows:
                note = "" if ok or args.dry_run else "  (not written: row exists now or insert failed)"
                print(f"    {tid}{note}")
        if args.dry_run:
            return 0 if total <= args.max else 1
        return 0 if n_written == total else 1
    if total <= args.max:
        return 0
    print(
        f"blackbox: {total} turn(s) have turn_api_calls rows but no turns row "
        f"(window since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}, "
        f"tolerance {args.max})"
    )
    for path, rows in sorted(results.items()):
        if not rows:
            continue
        with_err = sum(1 for _tid, _ts, errs in rows if errs)
        print(f"  {path}: {len(rows)} orphan turn(s), {with_err} with a non-200 call")
        for tid, _ts, errs in rows[:5]:
            print(f"    {tid} error_calls={errs}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
