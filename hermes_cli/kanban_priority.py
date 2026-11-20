"""Kanban's adapter to the fleet admission door; ordering belongs to admission."""
from __future__ import annotations

import logging
import math
import os

_log = logging.getLogger(__name__)


def set_asap(conn, task_id: str, flag: bool) -> bool:
    """Only an explicit, VERIFIED operator edit may mark (or clear) a card urgent; creation never copies it.

    The operator is resolved here, never taken from the caller (round 5, Prism b37716b1f3e9). Any kanban worker in
    the process ancestry is refused first, whatever profile it runs as (round 6); then the caller needs an operator
    label AND the operator token. A changed flag reaches the shared door unit in the same operation.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_identity import is_operator_label
    from hermes_cli.profiles import current_profile_name
    if os.environ.get("HERMES_KANBAN_TASK") or kb._is_delegated_child():
        raise PermissionError("ASAP is operator-set only")
    _refuse_worker_ancestry()
    # Round 6 (Prism f1dbe6687476): the label alone is not operator-ness; the operator token is required too.
    operator = (current_profile_name("user") or "user").strip()
    if not is_operator_label(operator) or kb._operator_token_state() != "ok":
        raise PermissionError(f"ASAP is operator-set only (caller {operator!r} holds no operator token)")
    from hermes_cli.kanban_db import _home_session_guarded
    # The same ownership guard as operator model edits; a caller-supplied
    # label alone must never authorize a foreign session's mutation.
    ok = _home_session_guarded("set-asap")(_set_asap)(conn, task_id, flag, operator=operator)
    if ok:
        sync_door_asap(conn, task_id)
    return ok


def _refuse_worker_ancestry() -> None:
    """Worker ancestry is checked FIRST, by the process walk, before any operator identity (Prism f1dbe6687476): a
    worker dispatched AS an operator profile still resolves to that profile, so the label cannot be the test.
    An unreadable ancestry or an incomplete board scan cannot prove absence, so it refuses too."""
    from hermes_cli import kanban_identity as ki
    chain = ki._ancestry()
    if not chain:
        raise PermissionError("ASAP is operator-set only (process ancestry unreadable)")
    worker = ki.worker_ancestor_profile()
    if worker:
        raise PermissionError(f"ASAP is operator-set only (caller runs under kanban worker {worker!r})")
    if not ki._worker_rows(chain)[1]:
        raise PermissionError("ASAP is operator-set only (a board was unreadable; worker ancestry unproven)")


def _set_asap(conn, task_id, flag, *, operator):
    from hermes_cli import kanban_db as kb
    if not operator or not operator.strip():
        raise PermissionError("ASAP requires an operator identity")
    with kb.write_txn(conn):
        row = conn.execute("SELECT asap FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return False
        if bool(row["asap"]) != bool(flag):
            conn.execute("UPDATE tasks SET asap=? WHERE id=?", (int(bool(flag)), task_id))
            kb._append_event(conn, task_id, "asap_set", {"asap": bool(flag), "operator": operator})
    kb.notify_task_updated(conn, task_id, ("asap",))
    return True


def sync_door_asap(conn, task_id, *, client=None) -> None:
    """Bring the card's live door unit (if this board published one) to the card's urgency now. Best effort: the
    next publish tick reconciles the same thing, so a door fault here is logged, never raised."""
    from hermes_cli import kanban_priority_door as door
    if not door.enabled():
        return
    uid = door.unit_id(conn, task_id)
    live = [h for h, u in door.load_published(conn) if u == uid]
    if not live:
        return
    client = client or door._client()
    for hid in live:
        try:
            receipt = client.enqueue("kanban", hid, uid, *door.need())
            door.sync_asap(client, conn, hid, uid, task_id, receipt)
        except (client.Refused, client.Unreachable) as exc:
            _log.warning("kanban door: ASAP change on %s not yet at the door (%s); next publish retries", task_id, exc)


def waiting_by_host(cards, plan, *, routes, local_host: str, board=None, default_assignee=None,
                    conn=None, caps=None, cap_only=None, worktree_ports=None, local_budget=None,
                    browser_joins=None, continuity=None):
    """Use the dispatcher's resolved routes, never its pressure-filtered slot budget.

    No age or workload ordering is decided here. The common publisher selects its
    urgency-first visible window; retain explicit urgent cards before the slot cap.
    A card waits on exactly ONE host: the door keeps unit ids unique per workload
    (hermes-home #9223). Local vs pool is the dispatcher's own rule
    (kanban_db_dispatch._portable_goes_remote, round 5): a portable card goes to the pool when the
    local allowance is used up, a capability restricts it, or kanban.placement.default is remote;
    otherwise it waits locally. A pin/origin card waits on its host. The scorer may still pick a
    different pool host at the start; enqueue_here withdraws the old-host unit then, never a latch.
    """
    from hermes_cli.kanban_db_dispatch import _portable_goes_remote
    from hermes_cli.kanban_worker_pool import count_free

    out = {local_host: []}
    if plan is None:
        hosts = {}
    else:
        hosts = plan.hosts
    out.update({name: [] for name in hosts})
    own_only = cap_only or {}
    local_names = {local_host.lower()}
    try:
        from hermes_cli.fleet_placement import placement
        resolved = placement.load_hosts().resolve(local_host)
        if resolved:
            local_names.add(resolved.lower())
    except Exception:
        pass
    # Structural slot capacity per pool host, never the pressure band: the door decides admission.
    free = {} if plan is None else {
        name: max(0, count_free(host, int(plan.detail.get(name, {}).get("running", 0))) - plan.taken.get(name, 0))
        for name, host in hosts.items()}
    local_used = 0
    # Explicit urgent cards claim capacity first; otherwise the dispatcher's own row order.
    for card in [c for c in cards if c["asap"]] + [c for c in cards if not c["asap"]]:
        route, arg = routes.get(card["id"], ("local", None))
        if route == "wait":
            continue
        assignee = card["assignee"] or default_assignee
        if not assignee:
            continue
        only = None
        if route == "portable" and plan is not None and conn is not None:
            from hermes_cli.kanban_db_dispatch import _portable_only
            only, _ = _portable_only(conn, card, assignee, plan, caps,
                                     own_only.get(card["id"]),
                                     (worktree_ports or {}).get(card["id"]))
        # A prose preference is soft (Prism 77c519869eff, as the dispatcher reads it): the preferred hosts are
        # tried first, the hard set (``fallback``, None = any pool host) decides eligibility.
        prefer = ()
        from hermes_cli.kanban_db_dispatch import _ProsePreferred
        if isinstance(only, _ProsePreferred):
            only, prefer = only.fallback, tuple(only)
        allowed = None if only is None else {n.lower() for n in only}
        local_free = local_budget is None or local_used < local_budget
        remote = (route == "portable" and plan is not None
                  and _portable_goes_remote(local_free, own_only.get(card["id"]), plan,
                                            card["id"] in (browser_joins or {}),
                                            continuity=card["id"] in (continuity or {})))
        if route == "local" or (route == "portable" and not remote
                                and (allowed is None or local_names & allowed)):
            out[local_host].append(card)
            local_used += 1
            continue
        if plan is None:
            continue
        want = {n.lower() for n in prefer}
        ordered = sorted(plan.hosts.items(), key=lambda kv: kv[0].lower() not in want)
        for name, host in ordered:
            if route in ("pin", "origin") and name != arg:
                continue
            if allowed is not None and name.lower() not in allowed:
                continue
            if (free[name] <= 0 or not host.enabled or host.state != "active" or name in plan.disabled
                    or assignee not in host.profiles or plan.board_refusal(name, board)
                    or plan.detail.get(name, {}).get("reachable") is False):
                continue
            out[name].append(card)
            free[name] -= 1
            break
    return out


def publish_waiting(conn, waiting, *, verdicts=None, client=None):
    """Enqueue/withdraw only this board's own units through the pair door; never replace a host queue.

    Every destination host is resolved BEFORE any door call; one the host table does not name is the class-2 config
    latch, never a silent skip (round 5, Prism ba4c7620e475). Withdrawals run first, so a card that moved host is gone
    from the old host before the new host's enqueue. The owner record is durable (var/kanban/door-published), so a
    restarted dispatcher still withdraws a moved card (Prism 39f15c5b2c09). Fault taxonomy (2026-11-05): a Refused
    (missing token, typed auth/invalid) latches the door (class 2); an Unreachable starts the transport backoff
    (class 3); either stops this publish. Returns {(host, unit_id): error} for what failed.
    """
    from hermes_cli import kanban_priority_door as door
    from hermes_cli import kanban_priority_faults as faults
    if not door.enabled() or faults.hold_reason():
        return {}                                    # latched (class 2) or backing off (class 3): no door call
    if client is None:
        client = door._client()
    try:
        errors = _publish_waiting(conn, waiting, verdicts, client, door)
    except _Stop as stop:
        errors = stop.errors
    for (hid, uid), why in errors.items():
        _log.warning("kanban door publish %s %s refused: %s", hid, uid, why)
    return errors


class _Stop(Exception):
    def __init__(self, errors):
        super().__init__("door publish stopped")
        self.errors = errors


def _classify(client, faults, exc, errors):
    """Class 2 (missing token, typed auth/invalid) latches and stops; class 3 (transport) backs off and stops."""
    if isinstance(exc, client.Unreachable):
        faults.note_transport(str(exc), "publish")
        raise _Stop(errors)
    if isinstance(exc, client.Refused):
        faults.latch(f"door publish refused: {exc}")
        raise _Stop(errors)


def _table_hosts():
    """Canonical host ids of the host table, or None when it cannot be read (then nothing is dropped)."""
    from hermes_cli.fleet_placement import placement
    try:
        return set(placement.load_hosts().hosts)
    except Exception:
        return None


def _auth_refusal(client, exc) -> bool:
    """Only explicit credential faults are auth; a bare HTTP refusal must still latch."""
    if not isinstance(exc, client.Refused):
        return False
    return (getattr(exc, "reason", None) == "auth"
            or isinstance(exc, getattr(client, "TokenUnreadable", ()))
            or str(exc) == "provisioned demand publisher token required")


def _drop_stale(current, hid, uid, why) -> None:
    """A host removed from the table has no withdrawal destination. Warn once and forget it."""
    current.discard((hid, uid))
    _log.warning("kanban door: dropped published unit %s on %s from the record: %s", uid, hid, why)


def _publish_waiting(conn, waiting, verdicts, client, door):
    from hermes_cli import kanban_priority_faults as faults
    plan = []
    for host, cards in waiting.items():
        if not cards:
            continue                                 # nothing waits there: no destination to validate
        try:
            hid = door.canonical_host(host)
        except door.DoorFault as exc:
            faults.latch(f"door publish: {exc}")
            raise _Stop({(host, None): str(exc)}) from exc
        plan.append((host, hid, [(card, door.unit_id(conn, card["id"])) for card in cards]))
    current = door.load_published(conn)
    pending = door.load_pending_withdrawals(conn)
    wanted = {(hid, uid) for _, hid, rows in plan for _, uid in rows}
    if not plan and not current and not pending:
        return {}                # no allocd call made: proves nothing about publish health (Prism 0056dc604414)
    errors = {}
    known = _table_hosts()
    import time
    now = time.time()
    answered = False
    try:
        # Reconcile desired ownership before destructive retries, even if enqueue later fails.
        for entry in list(pending):
            pair = (entry["host"], entry["unit_id"])
            if pair in wanted:
                current.add(pair)
                pending.remove(entry)
        for entry in list(pending):
            hid, uid = entry["host"], entry["unit_id"]
            if known is not None and hid not in known:
                pending.remove(entry)
                _log.warning("kanban door: dropped pending withdrawal %s on %s: host left table", uid, hid)
                continue
            try:
                client.withdraw("kanban", hid, uid)  # idempotent, including a unit already absent
                pending.remove(entry)
                answered = True
            except (client.Refused, client.Unreachable) as exc:
                entry["last_reason"] = str(exc)
                if _auth_refusal(client, exc):
                    continue
                errors[(hid, uid)] = str(exc)
                _classify(client, faults, exc, errors)
        for hid, uid in sorted(current - wanted):
            if known is not None and hid not in known:
                _drop_stale(current, hid, uid, "host is no longer in the host table")
                continue
            try:
                client.withdraw("kanban", hid, uid)
                current.discard((hid, uid))
                answered = True
            except (client.Refused, client.Unreachable) as exc:
                if _auth_refusal(client, exc):
                    current.discard((hid, uid))
                    if not any(p["host"] == hid and p["unit_id"] == uid for p in pending):
                        pending.append({"host": hid, "unit_id": uid, "first_seen": now, "last_reason": str(exc)})
                        _log.warning("kanban door: pending withdrawal of published unit %s on %s: %s", uid, hid, exc)
                    continue
                errors[(hid, uid)] = str(exc)        # still ours; retried after the fault clears
                _classify(client, faults, exc, errors)
    finally:
        door.save_published(conn, current, pending_withdrawals=pending)
    for host, hid, rows in plan:
        # The capacity fact only (last writer wins); units are managed per id below.
        try:
            client.publish_demand("kanban", hid, [], (verdicts or {}).get(host, {
                "admittable": False, "binding": "unknown", "deficit_cpu": 0.0}))
        except (client.Refused, client.Unreachable) as exc:
            errors[(hid, None)] = str(exc)
            _classify(client, faults, exc, errors)
        for card, uid in rows:
            try:
                receipt = door.enqueue_here(client, conn, hid, uid)
                door.sync_asap(client, conn, hid, uid, card["id"], receipt)
            except (client.Refused, client.Unreachable) as exc:
                errors[(hid, uid)] = str(exc)
                _classify(client, faults, exc, errors)
    if plan or answered:
        faults.note_success("publish")               # only a real answer proves publish health
    return errors


def verdicts_for_plan(plan):
    """Read the same per-host gate as take(); never infer CPU from a hot band."""
    import re
    import time
    from hermes_cli import kanban_headroom
    out = {}
    if plan is None or getattr(plan, "signal", None) is None:
        return out
    if plan.signal.placement_table is None:
        return out
    for host in plan.hosts:
        ok, reason = kanban_headroom._class_verdict(plan, host, None, now=time.time())
        # may_start returns admission.Verdict.reason, including its measured
        # fractional CPU shortfall. PSI and missing measurements have no deficit.
        match = re.search(r"saturated:([a-zA-Z0-9_]+)(?: ([+-]?[0-9.]+))?", reason)
        binding = match[1] if match else ("cpu" if ok else "unknown")
        free = {}
        if match and binding == "cpu" and match[2]:
            free["cpu"] = float(match[2])
        out[host] = door_verdict(ok, binding, free, plan.detail.get(host, {}).get("ncpu", 0))
    return out


def door_verdict(admittable: bool, binding: str, free, ncpu: float) -> dict:
    """Band pressure is not CPU usage; publish only measured headroom deficits."""
    cpu = free.get("cpu")
    measured = (isinstance(cpu, (int, float)) and not isinstance(cpu, bool)
                and math.isfinite(cpu) and isinstance(ncpu, (int, float))
                and not isinstance(ncpu, bool) and math.isfinite(ncpu) and ncpu > 0)
    return {"admittable": bool(admittable), "binding": binding,
            "deficit_cpu": max(0.0, -cpu * ncpu) if measured else 0.0}
