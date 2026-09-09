"""Explicit repair of historical quota crashes. Original events remain immutable."""
from __future__ import annotations

from contextlib import nullcontext
import json
import re
import sqlite3

from hermes_cli import kanban_db as kb
from agent.error_classifier import _POOL_EXHAUSTED_PATTERNS


_QUOTA_RE = re.compile(r"\b(?:429|rate[\s_-]?limit\w*|quota|insufficient[_ ](?:quota|credits)|billing)\b", re.I)


def _object(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _quota_evidence(payload: dict) -> bool:
    if payload.get("exit_kind") == "signaled" or payload.get("protocol_violation"):
        return False
    text = payload.get("stderr_tail", "")
    return isinstance(text, str) and bool(
        _QUOTA_RE.search(text) or any(p in text.lower() for p in _POOL_EXHAUSTED_PATTERNS)
    )


def _counter_repair(conn, task_id, candidate_ids):
    task = kb.get_task(conn, task_id)
    if (task is None or task.status != "blocked" or task.block_kind
            or task.claim_lock or task.current_run_id is not None):
        return None
    # Only undo the automatic breaker, never a later operator/lifecycle block.
    transition = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? AND kind IN "
        "('gave_up','blocked','unblocked','completed','archived','assigned','status_changed',"
        "'reclaimed','review_requested','changes_requested','quota_crash_repaired') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if transition is None or transition["kind"] != "gave_up":
        return None
    breaker = _object(transition["payload"])
    if breaker.get("trigger_outcome") != "crashed":
        return None
    remaining = task.consecutive_failures
    removed = 0
    # Walk only the CURRENT streak. A completion/block/reassignment must not let
    # an ancient quota crash discount a real failure in today's streak.
    for run in conn.execute("SELECT id,outcome,metadata FROM task_runs WHERE task_id=? ORDER BY id DESC", (task_id,)):
        if remaining <= 0:
            break
        if run["outcome"] == "rate_limited":
            continue
        if run["outcome"] not in ("crashed", "timed_out", "spawn_failed", "gave_up"):
            break
        if _object(run["metadata"]).get("protocol_violation"):
            break  # separate violation budget; do not guess at its accounting
        remaining -= 1
        if run["id"] in candidate_ids:
            removed += 1
    if not removed:
        return None
    after = max(0, task.consecutive_failures - removed)
    limit = task.max_retries if task.max_retries is not None else breaker.get("effective_limit", kb.DEFAULT_FAILURE_LIMIT)
    status = "blocked"
    if after < int(limit):
        status = "review" if breaker.get("retry_status") == "review" else "ready"
        if not kb._parents_satisfied(conn, task_id):
            status = "todo"
    return {"task_id": task_id, "before": task.consecutive_failures, "after": after, "status": status}


def reclassify_quota_crashes(conn: sqlite3.Connection, *, dry_run: bool = True) -> dict:
    """Plan with SELECTs only, or atomically rewrite outcomes + audit each change.

    Dry-run accepts connect_readonly(), performs no initialization/migration and
    reports WOULD-change counts. Apply is idempotent: only still-crashed runs
    with recorded quota evidence are eligible. Live claims are never modified.
    """
    report = {"dry_run": dry_run, "reclassified": 0, "unblocked": [], "adjusted": [], "runs": []}
    with nullcontext() if dry_run else kb.write_txn(conn):
        candidates = {}
        for row in conn.execute(
            "SELECT e.id AS event_id, e.task_id, e.run_id, e.payload, r.metadata "
            "FROM task_events e JOIN task_runs r ON r.id=e.run_id AND r.task_id=e.task_id "
            "WHERE e.kind='crashed' AND r.outcome='crashed' ORDER BY e.id"
        ):
            if _quota_evidence(_object(row["payload"])):
                candidates.setdefault(row["run_id"], dict(row))
        for tid in sorted({r["task_id"] for r in candidates.values()}):
            adjustment = _counter_repair(conn, tid, candidates)
            if adjustment:
                report["adjusted"].append(adjustment)
                if adjustment["status"] != "blocked":
                    report["unblocked"].append(tid)
        for rid, row in candidates.items():
            report["runs"].append({"task_id": row["task_id"], "run_id": rid, "source_event_id": row["event_id"]})
            if not dry_run:
                metadata = _object(row["metadata"])
                metadata["quota_reclassification"] = {"original_outcome": "crashed", "source_event_id": row["event_id"]}
                conn.execute(
                    "UPDATE task_runs SET outcome='rate_limited',status='rate_limited',metadata=? WHERE id=?",
                    (json.dumps(metadata), rid),
                )
                kb._append_event(conn, row["task_id"], "quota_crash_reclassified",
                                 metadata["quota_reclassification"], run_id=rid)
        if not dry_run:
            for adjustment in report["adjusted"]:
                conn.execute("UPDATE tasks SET consecutive_failures=?,status=? WHERE id=?",
                             (adjustment["after"], adjustment["status"], adjustment["task_id"]))
                kb._append_event(conn, adjustment["task_id"], "quota_crash_repaired", adjustment)
        report["reclassified"] = len(candidates)
    return report
