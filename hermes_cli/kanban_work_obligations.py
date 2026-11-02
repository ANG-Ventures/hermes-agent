"""Durable work obligations: one row per pending DECISION on a kanban card.

Spec: "Durable Work Ownership" v1.7 §4.1 (record), §4.3 (state machine, binding, lease,
resolve authority), §4.11 (clocks). Contract for other repos: ``docs/kanban-obligations.md``.

A row is keyed by ``(task_id, source_key)``; the key stays unique once resolved, so the same
evidence never opens a second row. Every holder change is ONE ``write_txn`` with a compare-and-set
on ``holder_gen`` and banks the elapsed time of the state it leaves into ``unclaimed_s`` /
``claimed_s``. Wake receipts (``receipt``) never touch ``state`` or ``holder_gen``.

The ``obligation_*`` event kinds are deliberately NOT owner-wake ``SCAN_KINDS``: the reconciler is
the only deliverer (spec §4.1, P1-B3).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb

# --- vocabulary ---------------------------------------------------------------------------------

OPEN_UNCLAIMED = "open_unclaimed"
OPEN_CLAIMED = "open_claimed"
OPEN_EXECUTOR = "open_executor"
RESOLVED = "resolved"
OPEN_STATES = (OPEN_UNCLAIMED, OPEN_CLAIMED, OPEN_EXECUTOR)

SOURCE_PREFIXES = ("detached", "queue", "blocked", "review", "holderless")
EVENT_KINDS = (
    "obligation_opened", "obligation_claimed", "obligation_progress", "obligation_executor",
    "obligation_released", "obligation_lease_expired", "obligation_escalated", "obligation_resolved",
)
# Resolutions an automated observer (drainer / reconciler, no session) may record: each is an
# OBSERVED outcome. ``handed:`` and ``dropped:`` are decisions, so they need operator authority.
OBSERVED_RESOLUTIONS = ("requeued-green", "superseded", "merged", "card-closed")
OPERATOR_RESOLUTIONS = ("handed", "dropped")
WORKER_ENV_MARKERS = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_LINEAGE")

# Exit codes (docs/kanban-obligations.md).
RC_OK, RC_ERROR, RC_REFUSED, RC_CONFLICT = 0, 1, 2, 3

# Compiled fallback of fleet/obligations-policy.json (hermes-home), spec §4.11. The fork never
# imports hermes-home code; it reads the JSON by root path and falls back to these values.
POLICY_DEFAULTS: dict[str, Any] = {
    "classes": {
        "decision": {"first_wake_min": 0, "rewake_min": 10, "escalate_min": 30,
                     "lease_min": 30, "max_claim_min": 120},
        "review": {"first_wake_min": 15, "rewake_min": 15, "escalate_min": 120,
                   "lease_min": 60, "max_claim_min": 240},
    },
    "tick_min": 5, "offhost_min": 15, "idle_min": 60, "enabled": True,
    "review_owner": "deadman", "max_new_cards_per_tick": 5,
}
_CLASS_KEYS = ("first_wake_min", "rewake_min", "escalate_min", "lease_min", "max_claim_min")


class ObligationError(Exception):
    rc = RC_ERROR


class ObligationRefused(ObligationError):
    rc = RC_REFUSED


class ObligationConflict(ObligationError):
    """Lost a compare-and-set, or the row is not in a state this transition starts from."""
    rc = RC_CONFLICT


class ObligationNotFound(ObligationError):
    rc = RC_ERROR


# --- policy -------------------------------------------------------------------------------------

def policy_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root()) / "fleet" / "obligations-policy.json"


def load_policy(path: Optional[Path] = None) -> dict:
    """The registry, else :data:`POLICY_DEFAULTS` when the file is absent or invalid."""
    p = path or policy_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        classes = raw["classes"]
        out = json.loads(json.dumps(POLICY_DEFAULTS))
        for cls in ("decision", "review"):
            for key in _CLASS_KEYS:
                v = classes[cls][key]
                if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1440:
                    raise ValueError(f"{cls}.{key}={v!r}")
                out["classes"][cls][key] = v
        for key in ("tick_min", "offhost_min", "idle_min", "max_new_cards_per_tick"):
            if key in raw:
                out[key] = int(raw[key])
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        return out
    except Exception:
        return json.loads(json.dumps(POLICY_DEFAULTS))


def lane_enabled(policy: Optional[dict] = None) -> bool:
    """Off when ``OBLIGATIONS=0`` OR the registry says ``enabled: false``; neither forces it on."""
    if (os.environ.get("OBLIGATIONS") or "").strip() == "0":
        return False
    return bool((policy or load_policy()).get("enabled", True))


# --- rows ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Obligation:
    id: str
    task_id: str
    source_key: str
    repo: Optional[str]
    pr: Optional[int]
    job_name: Optional[str]
    cls: str
    holder_kind: str
    holder_ref: Optional[str]
    state: str
    claim_session: Optional[str]
    claim_expires: Optional[int]
    lease_expiries: int
    unclaimed_s: int
    claimed_s: int
    state_since: int
    wake_attempts: int
    first_wake_at: Optional[int]
    last_wake_at: Optional[int]
    last_wake_outcome: Optional[str]
    escalated_at: Optional[int]
    summary: Optional[str]
    created_at: int
    updated_at: int
    holder_gen: int
    resolution: Optional[str]

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Obligation":
        d = dict(r)
        d["cls"] = d.pop("class")
        return cls(**d)

    def to_dict(self, now: Optional[int] = None) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["class"] = d.pop("cls")
        if now is not None:
            d["unclaimed_total_s"] = unclaimed_total(self, now)
            d["claimed_total_s"] = claimed_total(self, now)
        return d


def _now(now: Optional[int]) -> int:
    return int(time.time()) if now is None else int(now)


def source_class(source_key: str) -> str:
    prefix = (source_key or "").split(":", 1)[0]
    if prefix not in SOURCE_PREFIXES or ":" not in source_key or not source_key.split(":", 1)[1]:
        raise ObligationRefused(
            f"source key {source_key!r} must be <kind>:<id> with kind in {', '.join(SOURCE_PREFIXES)}")
    return "review" if prefix == "review" else "decision"


def get(conn: sqlite3.Connection, ob_id: str) -> Obligation:
    row = conn.execute("SELECT * FROM obligations WHERE id = ?", (ob_id,)).fetchone()
    if row is None:
        raise ObligationNotFound(f"obligation {ob_id} not found on this board")
    return Obligation.from_row(row)


def get_by_source(conn: sqlite3.Connection, task_id: str, source_key: str) -> Optional[Obligation]:
    row = conn.execute("SELECT * FROM obligations WHERE task_id = ? AND source_key = ?",
                       (task_id, source_key)).fetchone()
    return Obligation.from_row(row) if row is not None else None


def list_obligations(conn: sqlite3.Connection, *, task_id: Optional[str] = None,
                     open_only: bool = False) -> list[Obligation]:
    q, args = "SELECT * FROM obligations WHERE 1=1", []
    if task_id:
        q += " AND task_id = ?"
        args.append(task_id)
    if open_only:
        q += " AND state != 'resolved'"
    q += " ORDER BY created_at, id"
    return [Obligation.from_row(r) for r in conn.execute(q, args).fetchall()]


def unclaimed_total(ob: Obligation, now: int) -> int:
    return ob.unclaimed_s + (max(0, now - ob.state_since) if ob.state == OPEN_UNCLAIMED else 0)


def claimed_total(ob: Obligation, now: int) -> int:
    return ob.claimed_s + (max(0, now - ob.state_since) if ob.state == OPEN_CLAIMED else 0)


def escalation_due(ob: Obligation, now: int, policy: Optional[dict] = None) -> Optional[str]:
    """Why ``ob`` must escalate now, or None (§4.3 clock). Review rows use the deadman's clock."""
    if ob.state == RESOLVED or ob.escalated_at is not None or ob.cls != "decision":
        return None
    knobs = (policy or load_policy())["classes"]["decision"]
    if ob.state == OPEN_EXECUTOR:
        return None  # an executor suspends both lease and escalation
    if ob.lease_expiries >= 2:
        return "second lease expiry"
    if claimed_total(ob, now) >= knobs["max_claim_min"] * 60:
        return f"claimed >= max_claim_min ({knobs['max_claim_min']} min)"
    if unclaimed_total(ob, now) >= knobs["escalate_min"] * 60:
        return f"unclaimed >= escalate_min ({knobs['escalate_min']} min)"
    return None


def _bank(ob: Obligation, now: int) -> tuple[int, int]:
    """``(unclaimed_s, claimed_s)`` with the current stint banked (executor time is never banked)."""
    stint = max(0, now - ob.state_since)
    if ob.state == OPEN_UNCLAIMED:
        return ob.unclaimed_s + stint, ob.claimed_s
    if ob.state == OPEN_CLAIMED:
        return ob.unclaimed_s, ob.claimed_s + stint
    return ob.unclaimed_s, ob.claimed_s


def _cas(conn: sqlite3.Connection, ob: Obligation, now: int, **cols: Any) -> None:
    """One holder change: write ``cols``, bump ``holder_gen``, fail unless the gen is unchanged."""
    sets = ", ".join(f"{k} = ?" for k in cols)
    cur = conn.execute(
        f"UPDATE obligations SET {sets}, holder_gen = holder_gen + 1, updated_at = ? "
        f"WHERE id = ? AND holder_gen = ?",
        (*cols.values(), now, ob.id, ob.holder_gen),
    )
    if cur.rowcount != 1:
        raise ObligationConflict(f"obligation {ob.id}: holder_gen moved (expected {ob.holder_gen})")


def _load_for_cas(conn: sqlite3.Connection, ob_id: str, expected_gen: Optional[int]) -> Obligation:
    ob = get(conn, ob_id)
    if expected_gen is not None and ob.holder_gen != expected_gen:
        raise ObligationConflict(
            f"obligation {ob_id}: holder_gen is {ob.holder_gen}, expected {expected_gen}")
    return ob


def _require_state(ob: Obligation, *states: str, verb: str) -> None:
    if ob.state not in states:
        raise ObligationConflict(f"obligation {ob.id} is {ob.state}; {verb} needs {' or '.join(states)}")


# --- transitions (each one write_txn) -----------------------------------------------------------

def _insert(conn: sqlite3.Connection, task_id: str, source_key: str, *, created_at: int, now: int,
            summary: Optional[str], repo: Optional[str], pr: Optional[int],
            job_name: Optional[str]) -> tuple[Obligation, bool]:
    cls = source_class(source_key)
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        raise ObligationNotFound(f"card {task_id} is not on this board")
    ob_id = f"ob_{secrets.token_hex(6)}"
    cur = conn.execute(
        "INSERT INTO obligations (id, task_id, source_key, repo, pr, job_name, class, holder_kind, "
        "state, state_since, summary, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'operator', ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, source_key) DO NOTHING",
        (ob_id, task_id, source_key, repo, pr, job_name, cls, OPEN_UNCLAIMED, created_at,
         summary, created_at, now),
    )
    created = cur.rowcount == 1
    ob = get_by_source(conn, task_id, source_key)
    assert ob is not None
    if created:
        kb._append_event(conn, task_id, "obligation_opened", {
            "id": ob.id, "source_key": source_key, "class": cls, "created_at": created_at})
    return ob, created


def open_obligation(conn: sqlite3.Connection, task_id: str, source_key: str, *,
                    created_at: Optional[int] = None, summary: Optional[str] = None,
                    repo: Optional[str] = None, pr: Optional[int] = None,
                    job_name: Optional[str] = None, now: Optional[int] = None) -> tuple[Obligation, bool]:
    """Open (idempotent): a second open of the same source, even after resolve, writes nothing."""
    t = _now(now)
    with kb.write_txn(conn):
        return _insert(conn, task_id, source_key, created_at=t if created_at is None else int(created_at),
                       now=t, summary=summary, repo=repo or None, pr=pr, job_name=job_name or None)


def claim(conn: sqlite3.Connection, ob_id: str, *, session: str, now: Optional[int] = None,
          lease_s: Optional[int] = None, expected_gen: Optional[int] = None,
          non_home: bool = False) -> Obligation:
    t = _now(now)
    with kb.write_txn(conn):
        ob = _load_for_cas(conn, ob_id, expected_gen)
        _require_state(ob, OPEN_UNCLAIMED, verb="claim")
        lease = lease_s if lease_s is not None else load_policy()["classes"][ob.cls]["lease_min"] * 60
        un, cl = _bank(ob, t)
        _cas(conn, ob, t, state=OPEN_CLAIMED, holder_kind="operator", holder_ref=session,
             claim_session=session, claim_expires=t + lease, unclaimed_s=un, claimed_s=cl,
             state_since=t)
        kb._append_event(conn, ob.task_id, "obligation_claimed", {
            "id": ob.id, "session": session, "claim_expires": t + lease, "non_home": non_home})
        return get(conn, ob_id)


def progress(conn: sqlite3.Connection, ob_id: str, line: str, *, now: Optional[int] = None,
             lease_s: Optional[int] = None) -> Obligation:
    """Renew the lease of a CLAIMED row. Not a holder change: no gen bump, no banking."""
    t = _now(now)
    line = (line or "").strip()
    if not line:
        raise ObligationRefused("progress needs a line saying what moved")
    with kb.write_txn(conn):
        ob = get(conn, ob_id)
        if ob.state == OPEN_UNCLAIMED:
            raise ObligationRefused(f"obligation {ob_id} is unclaimed: claim first")
        _require_state(ob, OPEN_CLAIMED, verb="progress")
        lease = lease_s if lease_s is not None else load_policy()["classes"][ob.cls]["lease_min"] * 60
        conn.execute("UPDATE obligations SET claim_expires = ?, updated_at = ? WHERE id = ?",
                     (t + lease, t, ob_id))
        kb._append_event(conn, ob.task_id, "obligation_progress", {
            "id": ob.id, "line": line[:500], "claim_expires": t + lease})
        return get(conn, ob_id)


def set_executor(conn: sqlite3.Connection, ob_id: str, holder_ref: str, *, rule: str,
                 now: Optional[int] = None, expected_gen: Optional[int] = None) -> Obligation:
    """A bound operator job / re-enqueued queue row holds the row (implicit claim, D-9).

    The binding is written ONCE: a ``holder_ref`` already bound to another row, or a row already
    held by another ``holder_ref``, is refused (P6-RC4: never retargeted)."""
    t = _now(now)
    ref = (holder_ref or "").strip()
    if not ref:
        raise ObligationRefused("executor needs a holder ref (run id or queue row)")
    with kb.write_txn(conn):
        ob = _load_for_cas(conn, ob_id, expected_gen)
        _check_binding_free(conn, ob, ref)
        if ob.state == OPEN_EXECUTOR and ob.holder_ref == ref:
            return ob
        _require_state(ob, OPEN_UNCLAIMED, OPEN_CLAIMED, verb="executor")
        un, cl = _bank(ob, t)
        _cas(conn, ob, t, state=OPEN_EXECUTOR, holder_kind="executor", holder_ref=ref,
             claim_session=None, claim_expires=None, unclaimed_s=un, claimed_s=cl, state_since=t)
        kb._append_event(conn, ob.task_id, "obligation_executor",
                         {"id": ob.id, "holder_ref": ref, "rule": rule})
        return get(conn, ob_id)


def _check_binding_free(conn: sqlite3.Connection, ob: Obligation, ref: str) -> None:
    other = conn.execute(
        "SELECT id FROM obligations WHERE holder_kind = 'executor' AND holder_ref = ? AND id != ?",
        (ref, ob.id)).fetchone()
    if other is not None:
        raise ObligationRefused(f"{ref} is already bound to {other[0]}; a binding is never retargeted")
    if ob.holder_kind == "executor" and ob.holder_ref and ob.holder_ref != ref:
        raise ObligationRefused(f"obligation {ob.id} is bound to {ob.holder_ref}; never retargeted")


def release_executor(conn: sqlite3.Connection, ob_id: str, *, now: Optional[int] = None,
                     expected_gen: Optional[int] = None, why: str = "") -> Obligation:
    """The bound job ended with an outcome that opens no row: the decision is pending again.
    ``holder_ref`` stays as the binding record; ``holder_kind`` returns to operator."""
    t = _now(now)
    with kb.write_txn(conn):
        ob = _load_for_cas(conn, ob_id, expected_gen)
        _require_state(ob, OPEN_EXECUTOR, verb="release")
        _cas(conn, ob, t, state=OPEN_UNCLAIMED, holder_kind="operator", state_since=t)
        kb._append_event(conn, ob.task_id, "obligation_released",
                         {"id": ob.id, "holder_ref": ob.holder_ref, "why": why[:300]})
        return get(conn, ob_id)


def expire_leases(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[Obligation]:
    """Every claimed row past ``claim_expires`` back to unclaimed, banked AT ``claim_expires``."""
    t = _now(now)
    out: list[Obligation] = []
    with kb.write_txn(conn):
        rows = conn.execute("SELECT * FROM obligations WHERE state = ? AND claim_expires < ?",
                            (OPEN_CLAIMED, t)).fetchall()
        for r in rows:
            ob = Obligation.from_row(r)
            exp = int(ob.claim_expires or t)
            _cas(conn, ob, t, state=OPEN_UNCLAIMED, holder_ref=None, claim_expires=None,
                 claimed_s=ob.claimed_s + max(0, exp - ob.state_since), state_since=exp,
                 lease_expiries=ob.lease_expiries + 1)
            kb._append_event(conn, ob.task_id, "obligation_lease_expired", {
                "id": ob.id, "session": ob.claim_session, "lease_expiries": ob.lease_expiries + 1})
            out.append(get(conn, ob.id))
    return out


def mark_escalated(conn: sqlite3.Connection, ob_id: str, *, now: Optional[int] = None,
                   why: str = "") -> Obligation:
    t = _now(now)
    with kb.write_txn(conn):
        ob = get(conn, ob_id)
        if ob.escalated_at is None:
            conn.execute("UPDATE obligations SET escalated_at = ?, updated_at = ? WHERE id = ?",
                         (t, t, ob_id))
            kb._append_event(conn, ob.task_id, "obligation_escalated", {"id": ob.id, "why": why[:300]})
        return get(conn, ob_id)


def receipt(conn: sqlite3.Connection, ob_id: str, wake_outcome: str, *,
            now: Optional[int] = None) -> Obligation:
    """Record a wake attempt. Writes ONLY the wake columns: never ``state``/``holder_gen``."""
    t = _now(now)
    outcome = (wake_outcome or "").strip()
    if not outcome:
        raise ObligationRefused("receipt needs --wake-outcome")
    with kb.write_txn(conn):
        get(conn, ob_id)
        conn.execute(
            "UPDATE obligations SET wake_attempts = wake_attempts + 1, "
            "first_wake_at = COALESCE(first_wake_at, ?), last_wake_at = ?, last_wake_outcome = ? "
            "WHERE id = ?", (t, t, outcome[:200], ob_id))
        return get(conn, ob_id)


def validate_resolution(resolution: str) -> str:
    res = (resolution or "").strip()
    if res == "card-closed":
        return res
    kind, _, rest = res.partition(":")
    if kind not in OBSERVED_RESOLUTIONS + OPERATOR_RESOLUTIONS or kind == "card-closed" or not rest.strip():
        raise ObligationRefused(
            "resolution must be requeued-green:<run_id> | superseded:<source_key> | merged:<sha> | "
            f"card-closed | handed:ace | dropped:<why>, got {resolution!r}")
    return res


def resolution_kind(resolution: str) -> str:
    return resolution.partition(":")[0]


def resolve(conn: sqlite3.Connection, ob_id: str, resolution: str, *, now: Optional[int] = None,
            expected_gen: Optional[int] = None, actor_session: Optional[str] = None,
            non_home: bool = False) -> Obligation:
    t = _now(now)
    res = validate_resolution(resolution)
    with kb.write_txn(conn):
        ob = _load_for_cas(conn, ob_id, expected_gen)
        _require_state(ob, *OPEN_STATES, verb="resolve")
        if resolution_kind(res) == "requeued-green":
            run = res.partition(":")[2].strip()
            if ob.holder_kind != "executor" or ob.holder_ref != run:
                # Only a BOUND lander resolves green; a watcher's green is a receipt (P6-B1).
                raise ObligationRefused(
                    f"{res}: {ob.id} is not bound to {run} (holder {ob.holder_kind}:{ob.holder_ref}); "
                    "only the bound lander's green resolves it")
        un, cl = _bank(ob, t)
        _cas(conn, ob, t, state=RESOLVED, resolution=res, unclaimed_s=un, claimed_s=cl,
             claim_expires=None, state_since=t)
        kb._append_event(conn, ob.task_id, "obligation_resolved", {
            "id": ob.id, "resolution": res, "actor_session": actor_session, "non_home": non_home})
        return get(conn, ob_id)


def supersede(conn: sqlite3.Connection, ob_id: str, successor_source_key: str, *,
              successor_created_at: Optional[int] = None, successor_summary: Optional[str] = None,
              successor_repo: Optional[str] = None, successor_pr: Optional[int] = None,
              successor_job_name: Optional[str] = None, now: Optional[int] = None,
              expected_gen: Optional[int] = None) -> tuple[Obligation, Obligation]:
    """Open the successor (idempotent) AND resolve ``ob_id`` ``superseded:<key>`` in ONE txn.

    If the CAS on ``ob_id`` fails, nothing commits (successor included); the caller retries next
    tick. It never falls back to ``open_unclaimed`` (P6-RC8)."""
    t = _now(now)
    with kb.write_txn(conn):
        ob = _load_for_cas(conn, ob_id, expected_gen)
        _require_state(ob, *OPEN_STATES, verb="supersede")
        succ, _created = _insert(
            conn, ob.task_id, successor_source_key,
            created_at=t if successor_created_at is None else int(successor_created_at), now=t,
            summary=successor_summary, repo=successor_repo or None, pr=successor_pr,
            job_name=successor_job_name or None)
        if succ.id == ob.id:
            raise ObligationRefused("a row cannot supersede itself")
        res = f"superseded:{successor_source_key}"
        un, cl = _bank(ob, t)
        _cas(conn, ob, t, state=RESOLVED, resolution=res, unclaimed_s=un, claimed_s=cl,
             claim_expires=None, state_since=t)
        kb._append_event(conn, ob.task_id, "obligation_resolved",
                         {"id": ob.id, "resolution": res, "successor": succ.id})
        return get(conn, ob_id), succ


# --- binding (§4.3, P6-B1 / P6-RC4 / P6-RC5) ----------------------------------------------------

# Binding/resolving eligibility is ACTUAL landing code, never the job name and never
# ``--allow-lander`` (P6-B1). The wide D-7 gating test (name ``^land``) lives in detached-run.
_LANDING_CODE_RES = (
    re.compile(r"(?<![\w-])(?:fleet-merge(?:\.sh)?|apollo-land(?:\.py)?|land-handback(?:\.py)?)(?![\w.-])"),
    re.compile(r"\bgh\s+pr\s+merge\b"),
    re.compile(r"/pulls/[^/\s'\"]+/merge\b"),
    re.compile(r"(?<![\w-])(?:landing_queue|land_request)\.py\b"),
)


def is_landing_code(code: Optional[str]) -> bool:
    """True when ``code`` (the job's script/argv text) actually lands a PR."""
    text = code or ""
    return any(rx.search(text) for rx in _LANDING_CODE_RES)


@dataclass(frozen=True)
class Binding:
    status: str                    # "bound" | "unbound" | "refused"
    obligation_id: Optional[str]
    rule: Optional[str]            # "explicit" | "same-pr" | "same-name" | "persisted" | None
    reason: str

    def to_dict(self) -> dict:
        return {"status": self.status, "obligation": self.obligation_id, "rule": self.rule,
                "reason": self.reason}


def _identity(repo: Optional[str], pr: Optional[int]) -> Optional[tuple[str, int]]:
    """A PR identity, or None. NULL never equals anything, NULL included (P6-RC5)."""
    if not repo or pr is None:
        return None
    return (str(repo).strip().lower(), int(pr))


def decide_binding(conn: sqlite3.Connection, task_id: str, *, job_name: Optional[str],
                   code: Optional[str], repo: Optional[str] = None, pr: Optional[int] = None,
                   obligation_id: Optional[str] = None, operator_owned: bool = True,
                   holder_ref: Optional[str] = None) -> Binding:
    """Which open row (if any) a job queued with ``--card task_id`` binds to. Read-only.

    Decided ONCE at enqueue; the caller persists it (job.json ``obligation``, or
    :func:`set_executor` for a queue row). A ``holder_ref`` that is already bound returns that
    binding unchanged, so a row opening later cannot retarget it."""
    if holder_ref:
        row = conn.execute(
            "SELECT id FROM obligations WHERE holder_kind = 'executor' AND holder_ref = ?",
            (holder_ref,)).fetchone()
        if row is not None:
            return Binding("bound", row[0], "persisted", f"{holder_ref} was bound to {row[0]} at first sight")
    lander = is_landing_code(code)
    if not lander:
        if obligation_id:
            return Binding("refused", None, None,
                           "a job that does not land (watcher, CI wait, readback) cannot bind a "
                           "decision; drop --obligation (it runs as an unbound receipt)")
        return Binding("unbound", None, None, "not landing code: receipt only")
    if not operator_owned:
        return Binding("unbound", None, None, "worker-owned re-queue: receipt only")
    rows = [o for o in list_obligations(conn, task_id=task_id, open_only=True)]
    ident = _identity(repo, pr)
    if obligation_id:
        ob = next((o for o in rows if o.id == obligation_id), None)
        if ob is None:
            return Binding("refused", None, None, f"{obligation_id} is not an open row on {task_id}")
        rid = _identity(ob.repo, ob.pr)
        if rid is not None and ident is not None and rid != ident:
            return Binding("refused", None, None,
                           f"{obligation_id} names {ob.repo}#{ob.pr}, this job lands {repo}#{pr}")
        return Binding("bound", ob.id, "explicit", "--obligation")
    if not rows:
        return Binding("unbound", None, None, f"{task_id} has no open obligation")
    detached = [o for o in rows if o.source_key.startswith("detached:")]
    if len(detached) == 1 and ident is not None and _identity(detached[0].repo, detached[0].pr) == ident:
        return Binding("bound", detached[0].id, "same-pr", f"only open detached row, same {repo}#{pr}")
    named = [o for o in rows if job_name and o.job_name == job_name
             and not (_identity(o.repo, o.pr) and ident and _identity(o.repo, o.pr) != ident)]
    if len(named) == 1:
        return Binding("bound", named[0].id, "same-name", f"job name {job_name}")
    listing = ", ".join(f"{o.id} ({o.source_key})" for o in rows)
    return Binding("refused", None, None, f"name the decision: --obligation <id> (open: {listing})")


# --- authority (§4.1, P6-RC9) -------------------------------------------------------------------

@dataclass(frozen=True)
class Actor:
    profile: Optional[str]
    session: Optional[str]
    worker_profile: Optional[str]      # worker ancestry, read from process identity
    worker_env: tuple[str, ...]        # worker markers present in env


def current_actor() -> Actor:
    from hermes_cli.kanban_identity import worker_ancestor_profile

    profile, session = kb._event_actor()
    markers = tuple(m for m in WORKER_ENV_MARKERS if (os.environ.get(m) or "").strip())
    try:
        worker = worker_ancestor_profile()
    except Exception:
        worker = None
    return Actor(profile, session, worker, markers)


def _operator_state_dbs() -> list[Path]:
    from hermes_constants import get_default_hermes_root

    root = Path(get_default_hermes_root())
    dbs = [root / "state.db"]
    for name in sorted(kb.OPERATOR_PROFILES - {"default"}):
        dbs.append(root / "profiles" / name / "state.db")
    return dbs


def session_in_operator_state(session: str) -> bool:
    for path in _operator_state_dbs():
        if not path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
            try:
                if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session,)).fetchone():
                    return True
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return False


def worker_refusal(actor: Actor) -> Optional[str]:
    if actor.worker_env:
        return f"worker-run marker present ({', '.join(actor.worker_env)})"
    if actor.worker_profile:
        return f"runs under dispatched worker {actor.worker_profile} (process ancestry)"
    return None


def operator_refusal(actor: Actor) -> Optional[str]:
    """None when ``actor`` holds operator authority, else why not."""
    why = worker_refusal(actor)
    if why:
        return why
    if (actor.profile or "").strip().lower() not in kb.OPERATOR_PROFILES:
        return f"profile {actor.profile!r} is not an operator profile"
    if not actor.session:
        return "no session id (HERMES_SESSION_ID)"
    if not session_in_operator_state(actor.session):
        return f"session {actor.session} is not in any operator profile's state.db"
    return None


def is_home_session(conn: sqlite3.Connection, task_id: str, session: Optional[str]) -> bool:
    row = conn.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    home = row[0] if row is not None else None
    if not home or not session:
        return False
    if kb.is_operator_home(home):
        return True
    return session in kb.home_ids(home)


# --- receipts (P6-RC9) --------------------------------------------------------------------------

RECEIPT_AUTHOR = "obligations"
RECEIPT_MARK = "[obligations]"


def post_logs(text: str) -> None:
    """One #logs line. Fire-and-forget; a hermetic home (no notify.py) is a no-op."""
    try:
        from hermes_cli import kanban_budget as _kbud

        script = _kbud._notify_script_path()
        if script is None:
            return
        subprocess.Popen(
            [sys.executable, script, "--channel", "discord", "--target", _kbud.RECOVERY_TARGET,
             "--send", text],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception as exc:  # best-effort by contract
        print(f"obligation: #logs receipt failed: {exc}", file=sys.stderr)


def emit_resolve_receipt(conn: sqlite3.Connection, ob: Obligation, actor: Actor, *, non_home: bool,
                         board: Optional[str]) -> Optional[str]:
    """Machine card comment + #logs actor receipt for a ``dropped:*`` or non-home resolve."""
    dropped = resolution_kind(ob.resolution or "") == "dropped"
    if not (dropped or non_home):
        return None
    why = "dropped" if dropped else "non-home session"
    text = (f"{RECEIPT_MARK} {ob.id} ({ob.source_key}) resolved {ob.resolution} by "
            f"{actor.profile or '?'} session {actor.session or '?'} ({why})")
    with kb.write_txn(conn, allow_nested=True):
        kb.add_comment(conn, ob.task_id, RECEIPT_AUTHOR, text)
    post_logs(f"🧾 Kanban '{board or kb.DEFAULT_BOARD}': {ob.task_id} {text}")
    return text


# --- CLI ----------------------------------------------------------------------------------------

def setup_parser(p) -> None:
    """``obligation`` sub-verbs, attached by :mod:`hermes_cli.kanban_parser`."""
    sub = p.add_subparsers(dest="obligation_action")
    o = sub.add_parser("open", help="Open a decision row (idempotent per (card, source key))")
    o.add_argument("task_id")
    o.add_argument("--source-key", required=True,
                   help="detached:<run_id> | queue:<repo>#<pr>@<head>#<gen> | blocked:<event id> | "
                        "review:<event id> | holderless:<...>")
    o.add_argument("--created-at", type=int, help="EVENT time (epoch s); default now")
    o.add_argument("--summary")
    o.add_argument("--repo")
    o.add_argument("--pr", type=int)
    o.add_argument("--job-name")
    c = sub.add_parser("claim", help="Operator claim (lease)")
    c.add_argument("obligation_id")
    c.add_argument("--expected-gen", type=int)
    g = sub.add_parser("progress", help="Renew a claimed row's lease")
    g.add_argument("obligation_id")
    g.add_argument("line", nargs="+")
    r = sub.add_parser("resolve", help="Resolve a row")
    r.add_argument("obligation_id")
    r.add_argument("resolution")
    r.add_argument("--expected-gen", type=int)
    rc = sub.add_parser("receipt", help="Record a wake attempt (never changes state)")
    rc.add_argument("obligation_id")
    rc.add_argument("--wake-outcome", required=True)
    b = sub.add_parser("bind", help="Decide (read-only) which row a job queued on a card binds to")
    b.add_argument("task_id")
    b.add_argument("--job-name")
    b.add_argument("--code-file", help="the job's script; '-' reads stdin")
    b.add_argument("--repo")
    b.add_argument("--pr", type=int)
    b.add_argument("--obligation", dest="bind_obligation")
    b.add_argument("--worker-owned", action="store_true")
    b.add_argument("--holder-ref")
    e = sub.add_parser("executor", help="A bound job / queue row holds the row (binding persisted once)")
    e.add_argument("obligation_id")
    e.add_argument("--holder-ref", required=True)
    e.add_argument("--rule", default="explicit")
    e.add_argument("--expected-gen", type=int)
    rl = sub.add_parser("release", help="Bound job ended opening no row: back to unclaimed")
    rl.add_argument("obligation_id")
    rl.add_argument("--why", default="")
    rl.add_argument("--expected-gen", type=int)
    s = sub.add_parser("supersede", help="Open successor + resolve superseded in one transaction")
    s.add_argument("obligation_id")
    s.add_argument("--successor-key", required=True)
    s.add_argument("--created-at", type=int)
    s.add_argument("--summary")
    s.add_argument("--repo")
    s.add_argument("--pr", type=int)
    s.add_argument("--job-name")
    s.add_argument("--expected-gen", type=int)
    sub.add_parser("expire", help="Return every expired claim to unclaimed")
    ls = sub.add_parser("list", help="List rows (JSON)")
    ls.add_argument("--card", dest="task_id")
    ls.add_argument("--open", dest="open_only", action="store_true")
    a = sub.add_parser("audit", help="Open rows with clocks; exit 1 when one is past its bound")
    a.add_argument("--card", dest="task_id")


def _print(obj: Any) -> None:
    print(json.dumps(obj, sort_keys=True))


def _refuse(msg: str, rc: int = RC_REFUSED) -> int:
    print(f"obligation: {msg}", file=sys.stderr)
    return rc


def _resolve_authority(conn: sqlite3.Connection, ob: Obligation, resolution: str,
                       actor: Actor) -> Optional[str]:
    kind = resolution_kind(resolution)
    if kind in OPERATOR_RESOLUTIONS:
        return operator_refusal(actor)
    # Observed outcomes: an automated observer may record them, a worker never may.
    return worker_refusal(actor)


def cmd_obligation(args, *, now: Optional[int] = None) -> int:
    from hermes_cli import kanban_db_connect as kbc

    action = getattr(args, "obligation_action", None)
    if not action:
        return _refuse("usage: hermes kanban --board <slug> obligation "
                       "{open,claim,progress,resolve,receipt,bind,executor,release,supersede,expire,list,audit}")
    t = _now(now)
    board = kb.get_current_board()
    try:
        with kbc.connect_closing() as conn:
            return _dispatch(conn, action, args, t, board)
    except ObligationError as exc:
        return _refuse(str(exc), exc.rc)
    except PermissionError as exc:  # delegated child / fenced board
        return _refuse(str(exc))


def _dispatch(conn: sqlite3.Connection, action: str, args, t: int, board: Optional[str]) -> int:
    if action == "open":
        if not lane_enabled():
            _print({"disabled": True, "opened": False})
            return RC_OK
        ob, created = open_obligation(conn, args.task_id, args.source_key, created_at=args.created_at,
                                      summary=args.summary, repo=args.repo, pr=args.pr,
                                      job_name=args.job_name, now=t)
        _print({"opened": created, "obligation": ob.to_dict(t)})
        return RC_OK
    if action == "list":
        _print({"obligations": [o.to_dict(t) for o in
                                list_obligations(conn, task_id=args.task_id, open_only=args.open_only)]})
        return RC_OK
    if action == "audit":
        policy = load_policy()
        rows = []
        for o in list_obligations(conn, task_id=args.task_id, open_only=True):
            d = o.to_dict(t)
            d["escalation_due"] = escalation_due(o, t, policy)
            d["lease_expired"] = o.state == OPEN_CLAIMED and (o.claim_expires or 0) < t
            rows.append(d)
        overdue = [d["id"] for d in rows if d["escalation_due"]]
        _print({"open": len(rows), "overdue": overdue, "obligations": rows})
        return RC_ERROR if overdue else RC_OK
    if action == "receipt":
        _print({"obligation": receipt(conn, args.obligation_id, args.wake_outcome, now=t).to_dict(t)})
        return RC_OK
    if action == "bind":
        code = None
        if args.code_file == "-":
            code = sys.stdin.read()
        elif args.code_file:
            code = Path(args.code_file).read_text(encoding="utf-8", errors="replace")
        b = decide_binding(conn, args.task_id, job_name=args.job_name, code=code, repo=args.repo,
                           pr=args.pr, obligation_id=args.bind_obligation,
                           operator_owned=not args.worker_owned, holder_ref=args.holder_ref)
        _print(b.to_dict())
        return RC_REFUSED if b.status == "refused" else RC_OK

    actor = current_actor()
    if action == "claim":
        why = operator_refusal(actor)
        if why:
            return _refuse(f"claim refused: {why}")
        ob = get(conn, args.obligation_id)
        non_home = not is_home_session(conn, ob.task_id, actor.session)
        ob = claim(conn, ob.id, session=actor.session or "", now=t, expected_gen=args.expected_gen,
                   non_home=non_home)
        _print({"obligation": ob.to_dict(t), "non_home": non_home})
        return RC_OK
    if action == "progress":
        ob = get(conn, args.obligation_id)
        if operator_refusal(actor) is not None:
            from hermes_cli.kanban_identity import _owns_dispatcher_run

            own_card = (os.environ.get("HERMES_KANBAN_TASK") or "").strip() == ob.task_id
            if not (own_card and _owns_dispatcher_run(None)):
                return _refuse("progress refused: needs operator authority, or the worker of "
                               f"{ob.task_id} itself")
        ob = progress(conn, ob.id, " ".join(args.line), now=t)
        _print({"obligation": ob.to_dict(t)})
        return RC_OK
    if action == "resolve":
        res = validate_resolution(args.resolution)
        ob = get(conn, args.obligation_id)
        why = _resolve_authority(conn, ob, res, actor)
        if why:
            return _refuse(f"resolve refused: {why}")
        non_home = bool(actor.session) and not is_home_session(conn, ob.task_id, actor.session)
        ob = resolve(conn, ob.id, res, now=t, expected_gen=args.expected_gen,
                     actor_session=actor.session, non_home=non_home)
        receipt_text = emit_resolve_receipt(conn, ob, actor, non_home=non_home, board=board)
        _print({"obligation": ob.to_dict(t), "receipt": receipt_text})
        return RC_OK

    # Automation transitions: never from a worker.
    why = worker_refusal(actor)
    if why:
        return _refuse(f"{action} refused: {why}")
    if action == "executor":
        ob = set_executor(conn, args.obligation_id, args.holder_ref, rule=args.rule, now=t,
                          expected_gen=args.expected_gen)
        _print({"obligation": ob.to_dict(t)})
        return RC_OK
    if action == "release":
        ob = release_executor(conn, args.obligation_id, now=t, expected_gen=args.expected_gen, why=args.why)
        _print({"obligation": ob.to_dict(t)})
        return RC_OK
    if action == "supersede":
        old, succ = supersede(conn, args.obligation_id, args.successor_key,
                              successor_created_at=args.created_at, successor_summary=args.summary,
                              successor_repo=args.repo, successor_pr=args.pr,
                              successor_job_name=args.job_name, now=t, expected_gen=args.expected_gen)
        _print({"superseded": old.to_dict(t), "successor": succ.to_dict(t)})
        return RC_OK
    if action == "expire":
        _print({"expired": [o.to_dict(t) for o in expire_leases(conn, now=t)]})
        return RC_OK
    return _refuse(f"unknown obligation verb {action!r}")
