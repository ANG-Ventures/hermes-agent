"""A review card that is approved at its PR head and has a land in flight is TERMINAL-PENDING (t_26ebe71a).

Apollo, 2026-10-13 07:5x / 08:1x: a review claim has a fixed 15-min lease and no legal way to extend it from
outside the claiming session (``heartbeat_claim`` requires ``status='running'`` AND the caller's own lock). On a
slow-CI morning every approved card outlived its lease; the TTL sweep scored each expiry as a worker failure
(``gave_up``: failures 2-3 vs effective_limit 2, ``retry_status=review``) and the owner got ``stuck`` and
``gave_up`` wakes for cards that were finished. Seven cards that morning.

The rule here: a card whose newest ``review_coverage`` record is an APPROVE naming a PR head, with an OPEN land
intent for that card at that same head, is waiting for the lander, not for a reviewer. Then

* :func:`extend_claim` keeps its review claim alive (``release_stale_claims`` extends instead of reclaiming);
* the owner wake and the notifier write a ``notify_suppressed`` receipt with :data:`REASON` instead of a
  ``stuck`` / ``gave_up`` turn.

An open land intent is one of (all under ``kanban_home()/var``):

* ``land-requests/<ulid>.json`` / ``.inflight`` / ``.parked.json`` with no ``.done.json`` and no
  ``.cancel.json``: a request the lander executor has not finished (``scripts/land_request.py``);
* ``land-requests/<ulid>.result.json`` with status ARMED or QUEUED (auto-merge armed / in the merge queue) inside
  :data:`ARMED_HORIZON_S`, with no newer result for the same PR;
* ``land-watch/<repo>-<pr>.pin.json`` for the card whose ``.lock`` a live ``scripts/land-watch.py`` holds;
* a pending ``state/apollo-land-queue/<id>.json`` row for the card (the durable CI-wait queue).

The request's ``review_head_sha`` (or the pin's ``head``) must name the APPROVE's head. Anything unreadable is NOT
pending: the predicate fails open to today's behaviour, never to a claim that cannot expire.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

REASON = "terminal-pending"
# The owner wake's receipt for a pid-less review claim whose lease ran out (no worker existed to be stuck).
REASON_LEASE_LAPSE = "review-lease-lapse"
# A lander result that left the PR armed on GitHub (auto-merge / merge queue) stays a land in flight for this long;
# land_request.py's own PARK_MAX_SECONDS bound for a parked request.
ARMED_HORIZON_S = 6 * 3600
ARMED_STATUSES = frozenset({"ARMED", "QUEUED"})
_OPEN_SUFFIXES = (".parked.json", ".inflight", ".json")
_CLOSED_SUFFIXES = (".done.json", ".result.json", ".unparked.json", ".cancel.json", ".superseded.json")
_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _same_head(a: object, b: object) -> bool:
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    if not (_HEX_RE.match(a) and _HEX_RE.match(b)):
        return False
    return a.startswith(b) or b.startswith(a)


def approve_head(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The PR head of the card's newest ``review_coverage`` record when that record is an APPROVE, else None.

    The NEWEST record decides: an APPROVE followed by a send-back is not an approval any more."""
    from hermes_cli import kanban_db as kb

    # Plain tuples -> dicts: a read-only board connection carries no row factory.
    rows = [{"body": r[0] or ""} for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND body LIKE '%review_coverage:%' "
        "ORDER BY id DESC LIMIT 20", (task_id,),
    ).fetchall()]
    record, _err = kb._latest_review_coverage(rows)  # noqa: SLF001 -- the one review_coverage parser
    if not isinstance(record, dict) or str(record.get("verdict") or "").upper() != "APPROVE":
        return None
    head = str(record.get("head_sha") or "").strip().lower()
    return head if _HEX_RE.match(head) else None


def _read(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _land_requests(root: Path, task_id: str, head: str, now: float) -> Optional[dict]:
    d = root / "var" / "land-requests"
    try:
        names = os.listdir(d)
    except OSError:
        return None
    present = set(names)
    for name in names:
        if name.endswith(_CLOSED_SUFFIXES) or not name.endswith(_OPEN_SUFFIXES):
            continue
        uid = next(name[: -len(s)] for s in _OPEN_SUFFIXES if name.endswith(s))
        if uid.startswith(".") or f"{uid}.done.json" in present or f"{uid}.cancel.json" in present:
            continue
        req = _read(d / name)
        if req and req.get("card") == task_id and _same_head(req.get("review_head_sha"), head):
            return {"source": "land-request", "id": uid, "repo": req.get("repo"), "pr": req.get("pr"),
                    "head": req.get("review_head_sha"), "state": "parked" if name.endswith(".parked.json")
                    else "inflight" if name.endswith(".inflight") else "pending"}
    # ARMED / QUEUED results: only files touched inside the horizon are read.
    newest: dict = {}
    for name in names:
        if not name.endswith(".result.json"):
            continue
        path = d / name
        try:
            if now - path.stat().st_mtime > ARMED_HORIZON_S:
                continue
        except OSError:
            continue
        res = _read(path)
        req = _read(d / name.replace(".result.json", ".done.json"))
        if not res or not req:
            continue
        key = (str(req.get("repo") or "").lower(), str(req.get("pr")))
        at = float(res.get("finished_at") or 0)
        if key not in newest or at > newest[key][0]:
            newest[key] = (at, res, req)
    for at, res, req in newest.values():
        if (str(res.get("status") or "").upper() in ARMED_STATUSES and now - at <= ARMED_HORIZON_S
                and req.get("card") == task_id and _same_head(req.get("review_head_sha"), head)):
            return {"source": "land-request", "id": res.get("ulid"), "repo": req.get("repo"), "pr": req.get("pr"),
                    "head": req.get("review_head_sha"), "state": str(res.get("status")).lower(),
                    "until": int(at + ARMED_HORIZON_S)}
    return None


def _lock_held(path: Path) -> bool:
    """A live land-watch holds an exclusive flock on its lock file for its whole life."""
    try:
        fh = open(path, "rb")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        fh.close()
    return False


def _land_watch(root: Path, task_id: str, head: str) -> Optional[dict]:
    d = root / "var" / "land-watch"
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".pin.json"):
            continue
        pin = _read(d / name)
        stem = name[: -len(".pin.json")]
        if (pin and pin.get("card") == task_id and _same_head(pin.get("head"), head)
                and _lock_held(d / f"{stem}.lock")):
            return {"source": "land-watch", "id": stem, "head": pin.get("head"), "state": "watching"}
    return None


def _land_queue(root: Path, task_id: str, head: str) -> Optional[dict]:
    """A pending ``state/apollo-land-queue/<id>.json`` row for the card (``scripts/apollo_land_queue.py``). The
    row waits on CI for whatever head the PR has; one that already names a sha must name the APPROVE's head."""
    d = root / "state" / "apollo-land-queue"
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        row = _read(d / name)
        if not row or row.get("card") != task_id or row.get("status") not in (None, "pending"):
            continue
        sha = row.get("merging_sha") or row.get("rc_posted_sha")
        if sha and not _same_head(sha, head):
            continue
        return {"source": "apollo-land-queue", "id": row.get("id"), "repo": row.get("repo"), "pr": row.get("pr"),
                "head": sha or None, "state": "queued"}
    return None


def land_intent(task_id: str, head: str, *, root: Optional[Path] = None,
                now: Optional[float] = None) -> Optional[dict]:
    """The open land intent for ``task_id`` at ``head``, or None."""
    if root is None:
        from hermes_cli import kanban_db as kb

        root = kb.kanban_home()
    root = Path(root)
    now = time.time() if now is None else now
    return (_land_requests(root, task_id, head, now) or _land_watch(root, task_id, head)
            or _land_queue(root, task_id, head))


def terminal_pending(conn: sqlite3.Connection, task_id: str, *, root: Optional[Path] = None,
                     now: Optional[float] = None) -> Optional[dict]:
    """``{"approve_head", "land"}`` when the card is approved at a head with a land in flight there, else None.
    Never raises: an unreadable board or tree is NOT terminal-pending."""
    try:
        head = approve_head(conn, task_id)
        if head is None:
            return None
        land = land_intent(task_id, head, root=root, now=now)
    except Exception:
        return None
    return {"approve_head": head, "land": land} if land else None


def extend_claim(conn: sqlite3.Connection, row: sqlite3.Row, now: int, pending: dict) -> bool:
    """Extend an EXPIRED review claim of a terminal-pending card by one claim TTL (``claim_extended``,
    reason ``terminal_pending``). CAS on the expired lock and run, as ``_extend_live_stale_claim``."""
    from hermes_cli import kanban_db as kb

    new_expires = now + kb._resolve_claim_ttl_seconds()  # noqa: SLF001
    until = (pending.get("land") or {}).get("until")
    if until:
        new_expires = max(now + 60, min(new_expires, int(until)))
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ? AND status = 'running' AND claim_lock IS ? "
            "AND claim_expires IS NOT NULL AND claim_expires < ? AND current_run_id IS ?",
            (new_expires, row["id"], row["claim_lock"], now, row["current_run_id"]),
        )
        if cur.rowcount != 1:
            return False
        run_id = kb._extend_run_claim(conn, row["id"], new_expires)  # noqa: SLF001
        kb._append_event(  # noqa: SLF001
            conn, row["id"], "claim_extended",
            {"reason": "terminal_pending", "claim_lock": row["claim_lock"],
             "claim_expires_was": int(row["claim_expires"]), "claim_expires_now": new_expires,
             "approve_head": pending.get("approve_head"), "land": pending.get("land")},
            run_id=run_id,
        )
    return True
