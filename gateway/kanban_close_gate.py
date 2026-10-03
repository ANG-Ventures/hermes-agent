"""Is a ``changes_requested`` event a real send-back, or a landed card with a close gate?

t_dcc4ed08 (2026-10-02 17:30, Apollo scope add): the lifecycle feed rendered every
request-changes as ``🛑 ... review requested changes/BLOCK: <text>``. At 17:18 eight of them
went out for cards whose PR had MERGED and which were handed back only to prove a native-tick
close gate ("2 quiet ticks + mdutil -s", "p99 < 200ms one hour", "ACE-AI failed=0 + doctor
rc0"). That is not a block, and the stop sign was false.

A send-back is a LANDED CLOSE GATE when either of these holds:
  * the card's own recorded PR merged at the review coverage's ``head_sha`` (or, if the
    coverage has no head, every own PR merged). Merged means landed, whatever the reviewer
    typed (Apollo's mutant 4).
  * the reason or a coverage item carries the close-on marker (``close-on:``, ``Close on``,
    ``Closes on``, ``close when``, ``Done when``) and no own PR is still OPEN.
Either way the line renders ``⏳ [board] Kanban t_x landed · close on: <gate> — implementer @who``.
Anything else (PR open, code findings) keeps the 🛑 shape.

``classify`` runs in the notifier's collect thread (never on the event loop). PR state comes
from one REST read per own PR (``kanban_open_pr.query_pr_state``), memoised per tick. With no
oracle (pytest, or a ``gh`` failure) the merged rule is skipped and only the marker rule runs.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Same marker family as hermes-home scripts/close_contract.py (t_dcc4ed08).
CLOSE_ON_RE = re.compile(
    r"(?:^|(?<=[\s.;:(\u2014]))(?:close-on|close[-_ ]on|closes\s+on|closes?\s+when|done\s+when)\b"
    r"[*_`]*\s*:?[*_`]*[ \t]*(?P<gate>[^\n]+)",
    re.I,
)
_MERGE_ONLY_RE = re.compile(r"^\s*(?:merge|merged(?:=true)?|land|landed)\b[\s.]*$", re.I)
MAX_REFS = 3


def gate_marker(text: Any) -> Optional[str]:
    """The gate text after a close-on marker, else None. A gate that is only the merge is no gate."""
    m = CLOSE_ON_RE.search(str(text or ""))
    if not m:
        return None
    gate = m.group("gate").strip(" \t*_`)(.")
    if not gate or _MERGE_ONLY_RE.match(gate):
        return None
    return gate


def _coverage_for(kb: Any, conn: Any, task_id: str, ev: Any) -> dict:
    """The review_coverage the send-back recorded: a comment on the closed review run, else the
    newest one written at or before the event."""
    run_id = getattr(ev, "run_id", None)
    if run_id is not None:
        rows = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND run_id = ? ORDER BY id DESC",
            (task_id, int(run_id)),
        ).fetchall()
    else:
        rows = []
    if not rows:
        rows = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND created_at <= ? "
            "AND body LIKE '%review_coverage:%' ORDER BY created_at DESC, id DESC LIMIT 5",
            (task_id, int(getattr(ev, "created_at", 0) or 0)),
        ).fetchall()
    rows = [{"body": r[0] or ""} for r in rows]
    cov, _err = kb._latest_review_coverage(rows) if rows else (None, None)
    return cov if isinstance(cov, dict) else {}


def _own_pr_states(kb: Any, conn: Any, task_id: str, query_fn: Callable) -> list:
    """``[(state, head_sha)]`` for the card's own recorded PRs (bounded to MAX_REFS)."""
    from hermes_cli import kanban_open_pr as _open_pr

    refs = _open_pr.extract_pr_refs(*kb._card_recorded_pr_refs(conn, task_id))[:MAX_REFS]
    out = []
    for ref in refs:
        try:
            st = query_fn(ref.repo, ref.number)
        except Exception:
            st = None
        if not isinstance(st, dict):
            out.append((None, ""))
            continue
        out.append((str(st.get("state") or "").upper() or None, str(st.get("head_sha") or "").lower()))
    return out


def _head_match(cov_head: str, pr_head: str) -> bool:
    a, b = (cov_head or "").lower(), (pr_head or "").lower()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def classify(kb: Any, conn: Any, task_id: str, ev: Any, query_fn: Optional[Callable]) -> Optional[dict]:
    """``{"gate", "batch", "why"}`` when the send-back is a landed close gate, else None."""
    payload = getattr(ev, "payload", None) or {}
    reason = str(payload.get("reason") or "")
    cov = _coverage_for(kb, conn, task_id, ev)
    items = [str(i) for i in (cov.get("items") or []) if isinstance(i, (str, int, float))]
    marker = gate_marker(reason) or next((g for g in map(gate_marker, items) if g), None)
    states = _own_pr_states(kb, conn, task_id, query_fn) if query_fn is not None else []
    merged = [h for s, h in states if s == "MERGED"]
    any_open = any(s == "OPEN" for s, _ in states)
    cov_head = str(cov.get("head_sha") or "")
    if merged and not any_open and (
        not cov_head or any(_head_match(cov_head, h) for h in merged)
    ):
        why = "merged"
    elif marker and not any_open:
        why = "marker"
    else:
        return None
    batch = cov.get("batch_id")
    batch = str(batch).strip() if isinstance(batch, (str, int)) and not str(batch).startswith("n/a") else ""
    return {"gate": marker or reason or (items[0] if items else ""), "batch": batch, "why": why}
