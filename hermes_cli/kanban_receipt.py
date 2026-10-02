"""Completion receipt gate: a worker's ``done`` handoff must carry evidence.

Incident 2026-10-01 (t_e21aa11c): deploy cards closed ``done`` with nothing a
reader could check but prose, and the session overview printed "no receipt on
card" for them. A receipt is one of:

* a PR the handoff owns or names (``owner/repo#N`` / PR URL in summary, result,
  ``metadata.pr_url|pr_urls|pr`` or ``survivor_pr``),
* a survivor claim (``survivor_ref`` / ``survivor_none``),
* an attachment on the card, or declared ``metadata.artifacts``,
* structured handoff metadata: any key outside :data:`BOOKKEEPING_KEYS`
  (what was deployed, a sha table, a read-back).

:func:`missing` is the single predicate. ``complete_task`` refuses a worker's
dispatcher-owned (``expected_run_id``) completion with
:class:`ReceiptRequiredError` (reason code ``no_receipt``; CLI exit
:data:`EXIT_NO_RECEIPT`) when the board knob ``kanban.receipt_gate`` is on, and
``python -m hermes_cli.kanban_receipt --days 7`` lists the receipt-less
``done`` cards already on a board.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from typing import Any, Iterable, Optional

SETTING = "receipt_gate"
REASON_CODE = "no_receipt"
# ``hermes kanban complete`` / ``request-review`` exit status for this refusal
# (0 ok, 1 other refusal, 2 usage error).
EXIT_NO_RECEIPT = 4
EVENT = "completion_blocked_no_receipt"

# Keys the runtime stamps, or that only say "no PR" without saying what was
# done. A handoff whose metadata holds nothing else has no structured receipt.
BOOKKEEPING_KEYS = frozenset({
    "worker_session_id",
    "pool",
    "review_skipped",
    "no_pr",
    "no_pr_reason",
})


class ReceiptRequiredError(ValueError):
    """``complete_task`` refused a worker handoff that carries no receipt.

    ``ValueError`` so tool-error handlers treat it as recoverable: nothing was
    mutated except the ``completion_blocked_no_receipt`` audit event.
    """

    code = REASON_CODE

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"completion blocked ({REASON_CODE}): {task_id} has no receipt. Prose "
            f"alone is not one. Attach the evidence (kanban_attach / artifacts=[...]), "
            f"or put a structured handback in metadata (what was deployed, a "
            f"before/after sha table, the read-back), or name the PR. "
            f"{task_id} is still in-flight (no state change)"
        )


def _has_pr(summary, result, metadata, survivor_pr) -> bool:
    from hermes_cli import kanban_open_pr as _open_pr

    return bool(_open_pr.extract_pr_refs(
        result, summary, metadata=metadata if isinstance(metadata, dict) else None,
        survivor_pr=survivor_pr,
    ))


def structured_keys(metadata: Any) -> list:
    """Metadata keys that count as a structured receipt (sorted)."""
    if not isinstance(metadata, dict):
        return []
    return sorted(
        k for k, v in metadata.items()
        if k not in BOOKKEEPING_KEYS and v not in (None, "", [], {})
    )


def missing(
    *,
    summary: Optional[str] = None,
    result: Optional[str] = None,
    metadata: Any = None,
    survivor_pr=None,
    survivor_ref=None,
    survivor_none: bool = False,
    attachments: int = 0,
) -> bool:
    """True when the handoff carries no receipt (see module docstring)."""
    if attachments or survivor_ref or survivor_none:
        return False
    if structured_keys(metadata):
        return False
    return not _has_pr(summary, result, metadata, survivor_pr)


def attachment_count(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM task_attachments WHERE task_id = ?", (task_id,)
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------- lint
def lint(conn: sqlite3.Connection, *, days: float = 7, now: Optional[int] = None) -> list:
    """Receipt-less ``done`` cards completed in the last ``days`` days.

    Reads each card's latest ENDED run (the closing handoff). Superseded closes
    carry their pointer as evidence and are skipped. Returns dicts sorted by
    completion time.
    """
    now = int(now if now is not None else time.time())
    since = now - int(days * 86400)
    rows = conn.execute(
        """
        SELECT t.id, t.title, t.result, t.completed_at,
               r.profile, r.outcome, r.summary, r.metadata,
               (SELECT count(*) FROM task_attachments a WHERE a.task_id = t.id)
          FROM tasks t
          LEFT JOIN task_runs r ON r.id = (
                SELECT max(id) FROM task_runs
                 WHERE task_id = t.id AND ended_at IS NOT NULL)
         WHERE t.status = 'done' AND t.completed_at >= ?
         ORDER BY t.completed_at, t.id
        """,
        (since,),
    ).fetchall()
    out = []
    for tid, title, result, completed_at, profile, outcome, summary, meta, att in rows:
        if outcome == "superseded":
            continue
        try:
            metadata = json.loads(meta) if meta else None
        except (TypeError, ValueError):
            metadata = None
        if not missing(summary=summary, result=result, metadata=metadata, attachments=att):
            continue
        out.append({
            "id": tid,
            "completed_at": completed_at,
            "closed_by": profile or "-",
            "worker_handoff": isinstance(metadata, dict) and "worker_session_id" in metadata,
            "title": (title or "").strip(),
            "summary": ((summary or result or "").strip().splitlines() or [""])[0],
        })
    return out


def _main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m hermes_cli.kanban_receipt",
        description="List receipt-less done cards (rc 0 none, 1 some found, 2 error).",
    )
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--db", default=None, help="board DB (default: the active board)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        if args.db:
            conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        else:
            from hermes_cli import kanban_db as kb
            conn = kb.connect()
        with conn:
            found = lint(conn, days=args.days)
    except (sqlite3.Error, OSError) as exc:
        print(f"kanban_receipt: cannot read board: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(found, indent=1))
    else:
        for c in found:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["completed_at"]))
            who = c["closed_by"] + (" (worker)" if c["worker_handoff"] else "")
            print(f"{ts}  {c['id']}  {who:<22}  {c['title'][:70]}  | {c['summary'][:80]}")
        print(f"{len(found)} receipt-less done card(s) in the last {args.days:g} day(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(_main())
