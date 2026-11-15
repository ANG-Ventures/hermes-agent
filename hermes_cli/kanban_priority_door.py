"""Per-unit shared-door lifecycle for kanban. No private ordering or admission.

Contract: hermes-home alloc_client @ 01524a8d (P1 #9223). authorize_start answers a wait as
{authorized: False, reason: queued_behind | capacity | not_enqueued} and RAISES AuthorizeRefused for
spent_expired | auth | invalid. A lost reply is reconciled with lookup_unit() before any retry.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager

_log = logging.getLogger(__name__)
EVENT = "door_authorized"
RELEASED = "door_released"
FAULT = "door_fault"


class DoorHeld(RuntimeError):
    """No start was made; the dispatcher must return the claim without penalty."""


class DoorCancelled(DoorHeld):
    """The unit id is spent (or an uncertain reservation was cancelled); the next attempt needs a new ID."""


class DoorFault(DoorHeld):
    """Taxonomy class 2 (config): missing/empty pair token, host table missing or without the host, typed
    auth/invalid. Never retried blind: the card is blocked (capability) and the door latches."""

    def __init__(self, reason, detail):
        super().__init__(f"door {reason}: {detail}")
        self.reason = reason


class DoorUnreachable(DoorHeld):
    """Taxonomy class 3 (transport): allocd unreachable / timeout / 5xx / malformed reply. Same id, no latch,
    retried on a capped backoff."""


def _client():
    from hermes_cli.fleet_placement import alloc_client
    return alloc_client


def enabled():
    """fcfs/classes only when the LOCAL registry exists, parses and says so; absent/unreadable is spawn-only
    (taxonomy class 1, made loud once per tick by kanban_priority_faults.check_registry)."""
    from hermes_cli import kanban_priority_faults as faults
    if faults.registry_state() != "ok":
        return False
    from hermes_cli.fleet_placement import admission_demand
    return getattr(admission_demand.load_priority(), "priority_mode", "spawn-only") in ("fcfs", "classes")


def reservation_owned(conn, task_id, run_id):
    return conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind=?",
                        (task_id, run_id, EVENT)).fetchone() is not None


def unit_id(conn, task_id, *, offset=0):
    """Stable per-attempt id. A held run (door_held) keeps its id, so its arrival survives the wait."""
    import hashlib
    # The board path separates identical ids on different boards; no user prompt content.
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    board = hashlib.sha256(path.encode()).hexdigest()[:16]
    row = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? "
                       "AND COALESCE(outcome,'') != 'door_held'", (task_id,)).fetchone()
    return f"kb-{board}-{task_id}-{int(row[0]) + offset}"


def canonical_host(host=None):
    """A missing/unreadable host table or a host it does not name is a config fault (class 2)."""
    from hermes_cli.fleet_placement import placement
    try:
        table = placement.load_hosts()
    except Exception as exc:
        raise DoorFault("config", f"host table unavailable: {exc}") from exc
    hid = table.resolve(host or "local")
    if not hid:
        raise DoorFault("config", f"host table does not name door host {host or 'local'!r}")
    return hid


def need():
    from hermes_cli.fleet_placement import placement
    n = placement.need_of(placement.load_hosts().footprint("kanban"))
    return n.cpus, n.ram_gb


def mark_asap_if_set(client, conn, hid, uid, task_id):
    """Urgency is the operator capability (alloc_client.ASAP_OPERATOR), never the pair's. Without it the unit keeps
    its arrival; the refusal is logged, never raised: ASAP may reorder a start, never block one."""
    row = conn.execute("SELECT asap FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not (row and row[0]):
        return False
    try:
        client.mark_asap("kanban", hid, uid)
        return True
    except (client.Refused, client.Unreachable) as exc:
        _log.warning("kanban door: ASAP for %s not applied (%s); unit keeps its arrival", task_id, exc)
        return False


# -- one owner record per board: which (host, unit id) this dispatcher has live at the door ----------------------
# Durable (round 5, Prism 39f15c5b2c09): a restarted dispatcher still knows the old host of a card that moved, so it
# withdraws there before enqueueing on the new one. A record lost anyway is recovered by lookup (enqueue_here).

def _published_path(conn):
    import hashlib
    from hermes_cli import kanban_priority_faults as faults
    board = hashlib.sha256(conn.execute("PRAGMA database_list").fetchone()[2].encode()).hexdigest()[:16]
    return faults._root() / "var" / "kanban" / "door-published" / f"{board}.json"


def load_published(conn) -> set:
    try:
        doc = json.loads(_published_path(conn).read_text(encoding="utf-8-sig"))
        return {(str(h), str(u)) for h, u in doc.get("units", [])}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def load_pending_withdrawals(conn) -> list:
    try:
        return json.loads(_published_path(conn).read_text(encoding="utf-8-sig")).get("pending_withdrawals", [])
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def save_published(conn, units, *, pending_withdrawals=None) -> None:
    import os
    import tempfile
    if pending_withdrawals is None:
        pending_withdrawals = load_pending_withdrawals(conn)
    path = _published_path(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".published.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"units": sorted([h, u] for h, u in units),
                   "pending_withdrawals": pending_withdrawals}, fh)
    os.replace(tmp, path)


def _withdraw_elsewhere(client, hid, uid) -> bool:
    """Find ``uid`` live on another host by asking the door (lookup_unit), withdraw it there. True if found."""
    from hermes_cli.fleet_placement import placement
    found = False
    for other in placement.load_hosts().hosts:
        if other == hid:
            continue
        try:
            seen = client.lookup_unit("kanban", other, uid)
        except client.Refused:
            continue                                  # no pair token for that host: we never published there
        if seen.get("state") == "waiting":
            client.withdraw("kanban", other, uid)
            found = True
    return found


def enqueue_here(client, conn, hid, uid):
    """THE enqueue for a kanban unit (publisher and start boundary alike). A unit this board published on another
    host is withdrawn there first; the door refuses one live id on two hosts (409 invalid). If it still answers
    invalid (owner record lost), the unit is found by lookup on the other hosts, withdrawn and enqueued again: our
    own placement disagreement is reconciled, never latched (round 5, Prism a51c53f3bd02 / 23ed3fd0945c)."""
    units = load_published(conn)
    for h, u in sorted(units):
        if u == uid and h != hid:
            client.withdraw("kanban", h, uid)
            units.discard((h, u))
    try:
        receipt = client.enqueue("kanban", hid, uid, *need())
    except client.AuthorizeRefused as exc:
        if exc.reason != "invalid" or not _withdraw_elsewhere(client, hid, uid):
            raise
        receipt = client.enqueue("kanban", hid, uid, *need())
    units.add((hid, uid))
    # This pair is wanted again; an old auth-refused withdrawal must not remove the renewed unit next tick.
    pending = [p for p in load_pending_withdrawals(conn) if (p["host"], p["unit_id"]) != (hid, uid)]
    save_published(conn, units, pending_withdrawals=pending)
    return receipt


def sync_asap(client, conn, hid, uid, task_id, receipt):
    """Make the door unit's urgency equal the card's, both directions (round 5, Prism 2d873aa15a6e / 82278e729600).
    The enqueue receipt carries the door's own record. Urgency on: mark_asap (operator capability, advisory). Urgency
    off while the door still says asap: the door has no un-mark verb and a re-enqueue keeps asap, so the unit is
    withdrawn and enqueued fresh (it loses its old arrival, the cost of clearing urgency)."""
    row = conn.execute("SELECT asap FROM tasks WHERE id=?", (task_id,)).fetchone()
    want = bool(row and row[0])
    door = ((receipt or {}).get("door") or {}) if isinstance(receipt, dict) else {}
    unit = next((u for u in door.get("waiting") or [] if isinstance(u, dict) and u.get("id") == uid), None)
    have = bool(unit and unit.get("asap") is True)
    if want and not have:
        mark_asap_if_set(client, conn, hid, uid, task_id)
    elif have and not want:
        client.withdraw("kanban", hid, uid)
        client.enqueue("kanban", hid, uid, *need())


def _receipt(conn, task, doc):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        if not kb._claim_still_held(conn, task):
            raise DoorHeld("claim lost before door receipt")
        kb._append_event(conn, task.id, EVENT, doc, run_id=task.current_run_id)


def _authorize(client, hid, uid, reservation_id, ttl_s):
    try:
        ans = client.authorize_start("kanban", hid, uid, ttl_s=ttl_s, existing_reservation_id=reservation_id,
                                     existing_consumer="kanban" if reservation_id else None)
    except client.Unreachable:
        # Lost reply: the server may have authorized. Read the unit before anything else.
        try:
            seen = client.lookup_unit("kanban", hid, uid)
        except client.Unreachable as exc:
            raise DoorUnreachable(f"door unreachable; unit {uid} unresolved, kept for retry: {exc}") from exc
        # A lookup answer is NOT authorize health: the authorize transport history stays (per-verb, round 3).
        state = seen.get("state")
        if state == "authorized":
            # No receipt exists for a start this run never made: cancel the lease, a fresh id next run.
            client.release(seen["reservation_id"], consumer=f"kanban@{hid}")
            raise DoorCancelled("lost authorization reply; cancelled, retry with new ID") from None
        if state == "spent_expired":
            raise DoorCancelled(f"unit {uid} spent_expired after a lost reply; retry with new ID") from None
        raise DoorUnreachable(f"door unreachable; unit {uid} {state}") from None
    from hermes_cli import kanban_priority_faults as faults
    faults.note_success("authorize")              # authorize_start answered: a grant or an order refusal (class 4)
    if not ans.get("authorized"):
        if ans.get("reason") == "not_enqueued":
            client.enqueue("kanban", hid, uid, *need())   # same id again; the next tick retries it
        raise DoorHeld(ans.get("reason") or "door held")
    return ans


@contextmanager
def start(conn, task, host=None, *, reservation_id=None, client=None):
    """One authorization per actual start, upgraded in place when capacity was booked.

    Receipt precedes spawn. A repeated authorization never spawns again. A lost reply is
    reconciled by lookup_unit before any retry, so a retry cannot spend twice. Pair-owned
    leases survive restart in task_events, not a process cache.
    """
    if not enabled():
        yield
        return
    if client is None:
        client = _client()
    from hermes_cli import kanban_headroom
    from hermes_cli import kanban_priority_faults as faults
    latch = faults.latched()
    if latch:
        # Class 2: no door call at all. The first card to meet the latch is blocked (capability).
        if faults.claim_block(task.id):
            raise DoorFault("config", latch)
        raise DoorHeld(f"door latched: {latch}")
    held = faults.hold_reason()
    if held:
        raise DoorUnreachable(held)                  # class 3 backoff: no door call, same id
    try:
        hid = canonical_host(host)
        # claim_task opened this run: the waiting id is the one the publisher used before the claim.
        uid = unit_id(conn, task.id, offset=-1)
        if reservation_owned(conn, task.id, task.current_run_id):
            raise DoorHeld("run already authorized; reconcile original start")
        try:
            # A direct spawn_now has not been through a publisher tick; a repeat enqueue keeps the arrival.
            receipt = enqueue_here(client, conn, hid, uid)
            sync_asap(client, conn, hid, uid, task.id, receipt)
            ans = _authorize(client, hid, uid, reservation_id, kanban_headroom.RESERVE_TTL_S)
        except client.AuthorizeRefused as exc:
            if exc.reason == "spent_expired":
                # kanban's existence check is the run row: this run holds no door_authorized receipt, so no start.
                raise DoorCancelled(f"unit {uid} spent_expired; retry with new ID") from exc
            raise DoorFault(exc.reason, exc.detail or str(exc)) from exc
        except client.Refused as exc:
            # missing / empty / unreadable pair token, a malformed request: config, never transport
            raise DoorFault("config", str(exc)) from exc
        except client.Unreachable as exc:
            raise DoorUnreachable(f"door unreachable: {exc}") from exc
    except DoorFault as exc:
        faults.latch(str(exc))
        faults.claim_block(task.id)
        raise
    except DoorUnreachable as exc:
        faults.note_transport(str(exc), "authorize")
        raise
    if ans.get("reused"):
        # spent_live with no receipt on this run: the earlier reply was lost. Cancel, never a second spawn.
        client.release(ans["reservation_id"], consumer=f"kanban@{hid}")
        raise DoorCancelled("reconciled uncertain authorization; retry with new ID")
    import time
    doc = {"reservation_id": ans["reservation_id"], "consumer": f"kanban@{hid}",
           "host": hid, "unit_id": uid, "until": ans["until"],
           "at": time.time(), "at_source": "authorization_receipt_received"}
    try:
        _receipt(conn, task, doc)
        if time.time() >= doc["until"]:
            raise DoorCancelled("authorization expired before launch; retry with new ID")
        yield
    except BaseException:
        # Caller must stop an already-created worker before propagating an error.
        try:
            if client.release(doc["reservation_id"], consumer=doc["consumer"]):
                _mark_released(conn, task.id, task.current_run_id, doc)
        except (client.Refused, client.Unreachable):
            _log.exception("door release failed; durable receipt retained for sweep")
        raise


def spawn(conn, task, workspace, *, host, spawn, spawn_kwargs, state, reservation_id=None):
    from hermes_cli.kanban_db_dispatch import _stop_own_spawn
    with start(conn, task, host, reservation_id=reservation_id):
        try:
            return spawn(task, workspace, **spawn_kwargs)
        except BaseException:
            _stop_own_spawn(state.proc)
            raise


def defer(conn, task, reason=None):
    """A held start is not a failure: the claim returns without penalty and the waiting id is kept (a cancelled
    one moves on). A DoorFault (auth / invalid) is a config fault: the card is blocked for a human, never retried."""
    from hermes_cli import kanban_db as kb
    outcome = ("door_fault" if isinstance(reason, DoorFault) else
               "door_cancelled" if isinstance(reason, DoorCancelled) else "door_held")
    with kb.write_txn(conn):
        if not kb._claim_still_held(conn, task):
            return
        retry = kb._retry_status_for_run(conn, task.id, task.current_run_id)
        conn.execute("UPDATE tasks SET status=?,claim_lock=NULL,claim_expires=NULL,"
                     "worker_pid=NULL WHERE id=?", (retry, task.id))
        kb._end_run(conn, task.id, outcome=outcome, metadata={"retry_status": retry})
        kb._append_event(conn, task.id, FAULT if outcome == "door_fault" else "door_held",
                         {"reason": str(reason or "")[:300]}, run_id=task.current_run_id)
    if isinstance(reason, DoorFault):
        _log.error("kanban door %s for %s: %s (config fault; card blocked, not retried)",
                   reason.reason, task.id, reason)
        kb.block_task(conn, task.id, reason=f"kanban door {reason.reason}: {str(reason)[:200]}; operator: "
                      "check kanban@<host> pair token / door schema", kind="capability",
                      require_unclaimed_ready=True)


def _mark_released(conn, tid, run_id, doc):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        kb._append_event(conn, tid, RELEASED, doc, run_id=run_id)


def _release_finished(client, conn, task_id, run_id, doc):
    """Release a finished run's door lease with the run's use measured on its pool host as ``actual`` (t_dd7a1b7d:
    a door run's lease is released HERE, never by ``_end_run``, so this is where #354's measurement has to ride).
    A failed measurement still releases, unmeasured; an actual allocd refuses still ended the lease (a warning)."""
    from hermes_cli import kanban_headroom as kh
    rid, consumer = doc["reservation_id"], doc["consumer"]
    actual = None
    fp = kh.run_footprint_target(conn, task_id, run_id)
    if fp is not None:
        try:
            actual = kh.measure_run_footprint(fp)
        except Exception as exc:  # never blocks the release: the lease ends unmeasured
            _log.info("kanban door: release of %s carries no actual (%s run %s): %s", rid, fp.get("host"), run_id, exc)
    if actual is None:
        return client.release(rid, consumer=consumer)
    try:
        return client.release(rid, consumer=consumer, actual=actual, actual_source=kh.FOOTPRINT_SOURCE)
    except client.Refused as exc:
        if not str(exc).startswith("actual refused after lease release"):
            raise
        _log.warning("kanban door: release of %s: actual not recorded: %s", rid, exc)
        return True


def maintain(conn, *, client=None):
    """Renew running authorizations and release finished runs, including old modes."""
    if client is None:
        client = _client()
    rows = conn.execute(
        "SELECT e.task_id,e.run_id,e.payload,t.status,t.current_run_id FROM task_events e "
        "JOIN tasks t ON t.id=e.task_id WHERE e.kind=? AND NOT EXISTS "
        "(SELECT 1 FROM task_events r WHERE r.task_id=e.task_id AND r.run_id=e.run_id AND r.kind=?)",
        (EVENT, RELEASED)).fetchall()
    from hermes_cli.kanban_headroom import RESERVE_TTL_S
    for row in rows:
        doc = json.loads(row["payload"])
        try:
            if row["status"] == "running" and row["current_run_id"] == row["run_id"]:
                if not client.renew(doc["reservation_id"], consumer=doc["consumer"], ttl_s=RESERVE_TTL_S):
                    _log.warning("door lease expired: %s run %s", row["task_id"], row["run_id"])
            elif _release_finished(client, conn, row["task_id"], row["run_id"], doc):
                _mark_released(conn, row["task_id"], row["run_id"], doc)
        except (client.Refused, client.Unreachable):
            _log.exception("door lifecycle failed for %s; retry next tick", row["task_id"])
