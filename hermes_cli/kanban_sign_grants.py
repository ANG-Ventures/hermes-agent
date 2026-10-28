"""Scoped board-signing grants for same-model delegate children (t_19c567a8).

A ``delegate_task`` child is fenced off the board: its one write, a comment, is
stamped ``(subagent)`` so no operator-trust reader honours it. That is right by
default (one accountable actor per merge authorization) but it serialises every
review through the parent. A GRANT lets the parent hand a child a narrow,
expiring piece of its signing authority instead of its identity:

* minted by the parent at spawn (``delegate_task(sign_grant={...})``), only when
  the parent can PROVE the ``by`` identity itself (operator token, or the
  operator gateway with no dispatched worker in between), the child runs on the
  parent's model, and the child inherited the parent's context;
* one ``sign_grants`` row per card, on the board holding the cards, carrying the
  verbs, the expiry, the child's session fingerprint and the PRs the grant may
  never cover (the parent run's own PRs);
* a granted comment is written as ``by`` (never ``by (subagent)``) with
  ``task_comments.grant_id`` set, after :func:`validate_comment` passed inside
  the insert's transaction;
* merge-policy readers (hermes-home ``scripts/lib/sign_grant.py``) honour a
  granted row only while its grant is live: a revoked or expired grant voids the
  stamp the way a head move does.

Kill switch: ``kanban.sign_grants`` (board-home config first, then the profile);
anything but ``on`` refuses every mint and every granted write.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from typing import Iterable, Optional

#: Verbs a grant may carry. ``verdict``: a review note / ``prism-verdict:`` line /
#: non-verdict coverage record; ``approve``: an APPROVE coverage record;
#: ``request_changes``: a hold-verdict coverage record; ``release``: a
#: ``close-on:`` line. ``complete`` / ``defer`` are reserved for the lifecycle
#: verbs and are accepted in a grant, but no CLI verb consumes them yet.
VALID_VERBS = frozenset({"verdict", "approve", "request_changes", "release", "complete", "defer"})
DEFAULT_EXPIRES_MIN = 45
MAX_EXPIRES_MIN = 120
GRANT_ID_RE = re.compile(r"^g-[0-9a-f]{16}$")

# The ``sign_grants`` table lives in ``kanban_db.SCHEMA_SQL`` (created on every board init).

_HOLD = ("CHANGES_REQUESTED", "REQUEST_CHANGES", "CHANGES_NEEDED", "BLOCK", "BLOCKED", "REJECT", "REJECTED")
_APPROVE = ("APPROVE", "APPROVED")
_PRISM_VERDICT_RE = re.compile(r"prism-verdict\s*:", re.I)
_CLOSE_ON_RE = re.compile(r"close-on", re.I)


class GrantRefused(PermissionError):
    """A grant mint or a granted write failed validation; nothing was written."""


def enabled() -> bool:
    """``kanban.sign_grants == "on"`` for this board; any read error is off."""
    try:
        from hermes_cli.kanban_db import _kanban_review_setting

        value, _source = _kanban_review_setting("sign_grants", "off")
    except Exception:
        return False
    # YAML 1.1 reads a bare `on` as True: accept it, so the documented `sign_grants: on` works.
    return value is True or str(value).strip().lower() == "on"


def _norm_model(model: Optional[str]) -> str:
    return str(model or "").strip().lower()


def _pr_keys(texts: Iterable[str]) -> set:
    """``owner/repo#N`` (lowercased) for every PR ref (URL or short form) in ``texts``."""
    from hermes_cli.kanban_pr_gate import parse_pr_refs

    return {f"{r.repo.lower()}#{r.number}" for t in texts for r in parse_pr_refs(str(t or ""), default_repo=None)}


def _run_meta_pr_keys(meta_text) -> set:
    from hermes_cli.kanban_open_pr import recorded_pr_refs

    try:
        meta = json.loads(meta_text) if meta_text else None
    except (TypeError, ValueError):
        meta = None
    return _pr_keys(recorded_pr_refs(meta))


def authored_pr_keys(conn: sqlite3.Connection, profiles: Iterable[str], *, task_id: Optional[str] = None,
                     candidates: Iterable[str] = ()) -> set:
    """PRs a run of one of ``profiles`` recorded as its own: every such run on ``task_id``, plus any run on the
    board whose metadata names one of ``candidates`` (``owner/repo#N``). That is "the child or its parent run
    authored it": the child runs in its parent's profile, so the parent profile's runs are both."""
    profiles = sorted({str(p).strip() for p in profiles or () if str(p).strip()})
    if not profiles:
        return set()
    marks = ",".join("?" * len(profiles))
    out: set = set()
    if task_id:
        for (meta,) in conn.execute(
                f"SELECT metadata FROM task_runs WHERE task_id = ? AND profile IN ({marks})", (task_id, *profiles)):
            out |= _run_meta_pr_keys(meta)
    for key in sorted(set(candidates or ())):
        number = key.rsplit("#", 1)[-1]
        for (meta,) in conn.execute(
                f"SELECT metadata FROM task_runs WHERE profile IN ({marks}) AND metadata LIKE ?",
                (*profiles, f"%{number}%")):
            out |= _run_meta_pr_keys(meta) & {key}
    return out


def mint(
    conn: sqlite3.Connection,
    *,
    cards: Iterable[str],
    verbs: Iterable[str],
    by: str,
    child_session_ref: str,
    parent_model: str,
    child_model: str,
    parent_session_ref: Optional[str] = None,
    child_id: Optional[str] = None,
    expires_in_min: Optional[float] = None,
    parent_profiles: Iterable[str] = (),
    parent_task: Optional[str] = None,
    now: Optional[int] = None,
) -> dict:
    """Validate and write one grant (one row per card). Raises :class:`GrantRefused`.

    The caller (``delegate_task``) has already proven ``by`` and the inherited
    context; this checks everything the board can see.
    """
    if not enabled():
        raise GrantRefused("sign grants are off (kanban.sign_grants is not 'on')")
    cards = list(dict.fromkeys(str(c).strip() for c in cards or () if str(c).strip()))
    verbs = sorted({str(v).strip().lower() for v in verbs or () if str(v).strip()})
    if not cards:
        raise GrantRefused("a grant needs at least one card")
    if not verbs or set(verbs) - VALID_VERBS:
        raise GrantRefused(f"grant verbs must be a non-empty subset of {sorted(VALID_VERBS)}, got {verbs}")
    by = str(by or "").strip()
    if not by:
        raise GrantRefused("a grant needs a 'by' identity")
    if not child_session_ref:
        raise GrantRefused("a grant needs the child's session")
    if not _norm_model(parent_model) or _norm_model(parent_model) != _norm_model(child_model):
        raise GrantRefused(f"child model {child_model!r} != parent model {parent_model!r}; "
                           "a grant never goes to a different (downgraded) reviewer")
    minutes = DEFAULT_EXPIRES_MIN if expires_in_min in (None, "") else float(expires_in_min)
    if not (0 < minutes <= MAX_EXPIRES_MIN):
        raise GrantRefused(f"expires_in_min must be in (0, {MAX_EXPIRES_MIN}], got {expires_in_min!r}")
    parent_profiles = sorted({str(p).strip() for p in parent_profiles or () if str(p).strip()})
    if not parent_profiles:
        raise GrantRefused("a grant needs the parent's profile (its runs' PRs are never covered)")
    for card in cards:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (card,)).fetchone():
            raise GrantRefused(f"card {card} is not on this board")
        if parent_task and card == parent_task:
            raise GrantRefused(f"card {card} is the parent's own kanban card; a grant never covers it")
        own = authored_pr_keys(conn, parent_profiles, task_id=card)
        if own:
            raise GrantRefused(f"card {card} names PR(s) {sorted(own)} the parent run authored; "
                               "a grant never covers an own PR")
    now = int(time.time()) if now is None else int(now)
    gid = "g-" + secrets.token_hex(8)
    expires_at = now + int(minutes * 60)
    from hermes_cli.kanban_db import write_txn

    with write_txn(conn):
        for card in cards:
            conn.execute(
                "INSERT INTO sign_grants (grant_id, task_id, verbs, by_author, parent_session_ref, child_id, "
                "child_session_ref, parent_model, child_model, parent_profiles, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, card, json.dumps(verbs), by, parent_session_ref, child_id, child_session_ref,
                 str(parent_model), str(child_model), json.dumps(parent_profiles), now, expires_at),
            )
    return {"grant_id": gid, "cards": cards, "verbs": verbs, "by": by, "expires_at": expires_at,
            "child_id": child_id, "model": str(child_model)}


def revoke(conn: sqlite3.Connection, grant_id: str, *, now: Optional[int] = None) -> int:
    """Revoke every row of ``grant_id``; returns the rows changed. Voids its stamps."""
    from hermes_cli.kanban_db import write_txn

    now = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE sign_grants SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL", (now, grant_id))
    return cur.rowcount


def get(conn: sqlite3.Connection, grant_id: str, task_id: str) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM sign_grants WHERE grant_id = ? AND task_id = ?", (grant_id, task_id)).fetchone()
    except sqlite3.OperationalError:   # no table yet: no grant exists
        return None


def _coverage_verdicts(body: str) -> list:
    # The kernel's one coverage parser: a record the board's readers honour (bare or
    # backtick-wrapped) is the same record the grant is checked against (Prism 9e7e6653e13e).
    from hermes_cli.kanban_db import _review_coverage_payload

    out = []
    for line in str(body or "").splitlines():
        payload = _review_coverage_payload(line)
        if payload is None:
            continue
        try:
            rec = json.loads(payload)
        except ValueError:
            rec = None
        out.append(rec if isinstance(rec, dict) else {})
    return out


def body_verbs(body: str) -> set:
    """The verbs a comment body exercises (see :data:`VALID_VERBS`)."""
    verbs = set()
    for rec in _coverage_verdicts(body):
        v = str(rec.get("verdict") or "").strip().upper().replace("-", "_").replace(" ", "_")
        verbs.add("approve" if v in _APPROVE else "request_changes" if v in _HOLD else "verdict")
    if _PRISM_VERDICT_RE.search(body or ""):
        verbs.add("verdict")
    if _CLOSE_ON_RE.search(body or ""):
        verbs.add("release")
    return verbs or {"verdict"}


def body_pr_keys(body: str) -> set:
    """PRs a coverage record in the body names (its ``pr`` field)."""
    return _pr_keys(str(rec.get("pr") or "") for rec in _coverage_verdicts(body))


def validate_comment(
    conn: sqlite3.Connection,
    grant_id: str,
    task_id: str,
    author: str,
    body: str,
    *,
    caller_session_ref: Optional[str],
    now: Optional[int] = None,
) -> sqlite3.Row:
    """The grant row that authorizes this comment, or :class:`GrantRefused`."""
    if not enabled():
        raise GrantRefused("sign grants are off (kanban.sign_grants is not 'on'); granted write refused")
    if not GRANT_ID_RE.fullmatch(str(grant_id or "")):
        raise GrantRefused(f"malformed grant id {grant_id!r}")
    row = get(conn, grant_id, task_id)
    if row is None:
        raise GrantRefused(f"grant {grant_id} does not cover card {task_id}")
    now = int(time.time()) if now is None else int(now)
    if row["revoked_at"] is not None:
        raise GrantRefused(f"grant {grant_id} was revoked")
    if int(row["expires_at"]) <= now:
        raise GrantRefused(f"grant {grant_id} expired")
    if _norm_model(row["parent_model"]) != _norm_model(row["child_model"]):
        raise GrantRefused(f"grant {grant_id}: child model != parent model")
    if not caller_session_ref or caller_session_ref != row["child_session_ref"]:
        raise GrantRefused(f"grant {grant_id} belongs to another session")
    if str(author or "").strip().lower() != str(row["by_author"]).strip().lower():
        raise GrantRefused(f"grant {grant_id} signs as {row['by_author']!r}, not {author!r}")
    allowed = set(json.loads(row["verbs"] or "[]"))
    missing = body_verbs(body) - allowed
    if missing:
        raise GrantRefused(f"grant {grant_id} does not carry verb(s) {sorted(missing)} (has {sorted(allowed)})")
    own = authored_pr_keys(conn, json.loads(row["parent_profiles"] or "[]"), task_id=task_id,
                           candidates=body_pr_keys(body))
    if own:
        raise GrantRefused(f"grant {grant_id} never covers PR(s) {sorted(own)} its parent run authored")
    return row
