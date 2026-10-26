"""Background ``delegate_task`` units on the kanban worker runtime (option C, gateway side).

Spec: docs/dev/delegate-background-on-kanban-runtime.md §5. One unit = one card on the internal
``delegations`` board, spawned at dispatch through the dispatcher's own ``spawn_now`` and run by
``hermes -p <profile> delegation-unit`` (``tools/delegation_unit_worker.py``). The durable
registry (``state/async-delegations.json``) stays the delivery truth; the card carries only the
process lifecycle, and its end is the gateway's signal to drain the record (§5.4).

This module holds:

* §5.1 the eligibility predicate (an allowlist that fails closed);
* §5.2 ``launch_unit``: card + spawn, and the in-process supersede on any failure;
* §5.4 the card-end drain, the lost-card check and the boot observer records;
* §5.5 ``cancel_unit``: archive the unit's card (the only gateway path that stops a unit).
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tools import async_delegation_store as _store

logger = logging.getLogger("tools.async_delegation")

#: Card event that marks a unit's record drained (delivered, settled); the card status alone is not
#: the marker, so a ``/stop`` that archives a card whose runner already wrote ``done`` still drains.
DRAINED_EVENT = "delegation_drained"
#: Card statuses that end a unit: the runner completed it, K2 parked it, or a cancel archived it.
ENDED_STATUSES = ("done", "blocked", "archived")
#: The ``delegations`` card of a unit whose record is terminal or missing is archived after this.
STALE_CARD_SECONDS = 48 * 3600
#: Terminal backends whose filesystem a unit in another process sees as the gateway does (§5.1.4).
ELIGIBLE_BACKENDS = frozenset({"local", "ssh"})
#: Registry state -> live record status (§5.4 step 3); any other state is not settled yet.
SETTLE_STATUS = {"done": "completed", "failed": "error", "cancelled": "cancelled"}
#: argv element that names the runner (never a substring match, §5.4 Boot).
RUNNER_ARG = "delegation-unit"

_dispatcher_lock = threading.Lock()
_dispatcher_active = False


# ── §5.1 eligibility ────────────────────────────────────────────────────────


def set_dispatcher_active(active: bool) -> None:
    """The gateway's embedded dispatcher took (True) or released (False) the singleton lock.

    Without a dispatcher in this process nothing reaps a crashed unit worker (§5.1.5)."""
    global _dispatcher_active
    with _dispatcher_lock:
        _dispatcher_active = bool(active)


def dispatcher_serves_here() -> bool:
    """A dispatcher serves the delegations board in this process and no estop is engaged."""
    with _dispatcher_lock:
        if not _dispatcher_active:
            return False
    try:
        from gateway.kanban_watchers_common import _kanban_dispatch_allowed

        return bool(_kanban_dispatch_allowed())
    except Exception:
        return False


def _terminal_backend() -> str:
    from tools.terminal_tool import _session_scope

    return str(_session_scope().env_type or "")


def ineligible_reason(*, durable_spec: Optional[Dict[str, Any]], children: List[Any],
                      cwd: Optional[str]) -> Optional[str]:
    """None when the unit takes the kanban runtime, else the first §5.1 condition that is not
    positively true. Unknown answers count as false: the unit stays in-process (I6)."""
    if durable_spec is None:
        return "no_durable_record"
    try:
        from tools.delegate_tool_config import _get_background_runtime

        if _get_background_runtime() != "kanban":
            return "knob_in_process"
    except Exception:
        return "knob_unreadable"
    if any(getattr(c, "_inherit_mode", None) == "fork" for c in children):
        return "fork_child"
    try:
        backend = _terminal_backend()
    except Exception:
        return "backend_unreadable"
    if backend not in ELIGIBLE_BACKENDS:
        return f"backend_{backend or 'unknown'}"
    if not dispatcher_serves_here():
        return "no_dispatcher"
    if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return "no_session_cwd"
    try:
        from tools.file_state import cross_process_locks_available

        if not cross_process_locks_available():
            return "no_file_locks"
    except Exception:
        return "no_file_locks"
    return None


def parent_route(parent_agent: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The non-secret route the runner rebuilds the parent from (§5.2.1). ``requested_provider`` is the
    CONFIGURED name (a named custom endpoint), never the resolved generic provider."""
    requested = getattr(parent_agent, "requested_provider", None)
    requested = requested if isinstance(requested, str) and requested else None
    explicit = str(cfg.get("base_url") or "").strip() or None
    route = {"requested_provider": requested, "model": getattr(parent_agent, "model", None) or None,
             "explicit_base_url": explicit}
    if _anonymous_custom(parent_agent, requested):
        # An anonymous custom endpoint (no ``custom:<name>`` to re-resolve by) exists only as the
        # parent's base_url: re-resolving the bare name lands on the profile's default endpoint. The
        # worker keys the parent's own credential on this URL, never on the bare name (1b48ed1b0fa3).
        parent_url = str(getattr(parent_agent, "base_url", None) or "").strip() or None
        route["parent_base_url"] = parent_url
        if explicit is None:
            route["explicit_base_url"] = parent_url
    return route


def _anonymous_custom(parent_agent: Any, requested: Optional[str]) -> bool:
    from tools.delegate_tool_config import _RUNTIME_PROVIDER_CUSTOM

    if getattr(parent_agent, "provider", None) != _RUNTIME_PROVIDER_CUSTOM:
        return False
    if (requested or "").startswith(f"{_RUNTIME_PROVIDER_CUSTOM}:"):
        return False
    # A configured provider requested by its raw name (``lab``) re-resolves by that name too.
    if requested and requested.strip().lower() != _RUNTIME_PROVIDER_CUSTOM:
        from hermes_cli.runtime_provider_custom import has_named_custom_provider

        return not has_named_custom_provider(requested)
    return True


# ── kanban plumbing ─────────────────────────────────────────────────────────


def _kb():
    from hermes_cli import kanban_db as kb

    return kb


def _board() -> str:
    from hermes_cli.kanban_delegations_board import DELEGATIONS_BOARD

    return DELEGATIONS_BOARD


def _board_exists() -> bool:
    return bool(_board_state())


def _board_state() -> Optional[bool]:
    """True/False: the delegations board positively exists/does not; None: the check itself failed."""
    try:
        return bool(_kb().board_exists(_board()))
    except Exception:
        return None


def card_body(delegation_id: str, attempt_id: str, profile_home: Path) -> str:
    return json.dumps({"delegation_id": delegation_id, "attempt_id": attempt_id,
                       "profile_home": str(profile_home)}, sort_keys=True)


def parse_card_body(body: Optional[str]) -> Dict[str, Any]:
    """The JSON line the dispatch wrote; the home stamp (``origin:`` line) may follow it."""
    for line in str(body or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("delegation_id"):
                return value
    return {}


# ── §5.2 dispatch ───────────────────────────────────────────────────────────


def launch_unit(*, delegation_id: str, attempt_id: str, profile_home: Path, profile: str,
                goal: str, cwd: str, session_id: Optional[str],
                spawn_fn: Optional[Callable] = None, slot_key: Optional[str] = None) -> Dict[str, Any]:
    """Create the unit's card and spawn its runner now (§5.2 steps 2-3).

    Returns ``{"ok": True, "task_id", "pid"}``, or ``{"ok": False, "error"}`` after archiving any
    card it made: the caller then supersedes the attempt and runs the unit in-process (§5.2.4)."""
    kb = _kb()
    from hermes_cli import kanban_db_dispatch as kbd

    board = _board()
    task_id = None
    try:
        kb.create_board(board)
        with kb.connect_closing(board=board) as conn:
            task_id = kb.create_task(
                conn, title=f"deleg {delegation_id}: {str(goal or '')[:60]}",
                body=card_body(delegation_id, attempt_id, profile_home),
                assignee=profile, workspace_kind="dir", workspace_path=cwd, max_retries=1,
                max_runtime_seconds=None, session_id=session_id or None,
                idempotency_key=attempt_id, board=board, created_by=profile,
            )
            # ``slot_key``: the call's shared pool slot, so a boot observer of a multi-unit call
            # holds ONE slot as the dispatching gateway did (not one per unit).
            block = {"board": board, "task_id": task_id,
                     **({"slot_key": slot_key} if slot_key and slot_key != delegation_id else {})}
            if not _store.set_kanban_block(delegation_id, attempt_id, block, profile_home=profile_home):
                raise RuntimeError("attempt no longer current before spawn")
            pid = kbd.spawn_now(conn, task_id, board=board, spawn_fn=spawn_fn)
        return {"ok": True, "task_id": task_id, "pid": pid}
    except Exception as exc:
        # Fenced = no card exists, or it is archived (a started runner's card read stops it). An
        # unfenced card may still have a runner: the caller must not run the unit a second time.
        fenced = not task_id or (_archive_quietly(task_id) and _runner_gone(task_id))
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "task_id": task_id, "fenced": fenced}


#: How long a failed launch waits for an archived card's runner to exit (it reads its card every 5 s).
FENCE_WAIT_SECONDS = 12.0


def _runner_gone(task_id: str) -> bool:
    """The archived card's runner is positively not running (the process table proves it), waiting up
    to ``FENCE_WAIT_SECONDS``. An archive alone is not a fence: its kill is best effort and a runner
    may be between card reads. Unknown (unreadable scan) is never gone. Tagged leftovers are reaped."""
    deadline = time.monotonic() + FENCE_WAIT_SECONDS
    while True:
        procs = lineage_processes(task_id)
        if procs is not None and not any(_is_runner(p, task_id) for p in procs):
            _kill_leftovers(procs)
            return True
        if time.monotonic() >= deadline:
            logger.warning("async_delegation_kanban_unfenced task_id=%s scan=%s", task_id,
                           "unknown" if procs is None else "runner alive")
            return False
        time.sleep(0.5)


def _archive_quietly(task_id: str) -> bool:
    kb = _kb()
    try:
        with kb.connect_closing(board=_board()) as conn:
            return bool(kb.archive_task(conn, task_id))
    except Exception:
        logger.warning("async_delegation_kanban_archive_failed task_id=%s", task_id, exc_info=True)
        return False


# ── §5.5 cancel ─────────────────────────────────────────────────────────────


def cancel_unit(delegation_id: str, profile_home: Optional[Path]) -> None:
    """Archive the unit's card (the dispatcher's identity-checked kill); off-thread, never blocks
    the caller (a ``/stop`` on the gateway loop). The runner's own 5 s card read stops it too.

    The thread is only the fast path: the live record is marked ``kanban_cancel`` first, and every
    drain re-archives a marked unit's card until it has ended (a lost thread or a failed archive is
    retried); after a restart, boot re-marks a cancelled record whose card is still live."""
    _mark_cancel(delegation_id)
    try:
        with _store.locked_registry(profile_home, write=False) as registry:
            record = registry["records"].get(delegation_id)
            task_id = ((record or {}).get("kanban") or {}).get("task_id") if isinstance(record, dict) else None
    except Exception:
        logger.warning("async_delegation_kanban_cancel_unreadable delegation_id=%s", delegation_id,
                       exc_info=True)
        return
    if not task_id:
        return
    thread = threading.Thread(target=_archive_quietly, args=(task_id,), daemon=True,
                              name=f"deleg-cancel-{delegation_id}")
    thread.start()


def _mark_cancel(delegation_id: str) -> None:
    from tools import async_delegation as ad

    with ad._records_lock:
        rec = ad._records.get(delegation_id)
        if rec is not None and rec.get("kanban_unit"):
            rec["kanban_cancel"] = True


def _replay_cancels(conn, live: Dict[str, str]) -> None:
    """Archive the card of every cancel-marked live unit whose card has not ended (durable cancel
    replay). Steady state (no marked unit) reads nothing."""
    from tools import async_delegation as ad

    with ad._records_lock:
        marked = [d for d in live if (ad._records.get(d) or {}).get("kanban_cancel")]
    kb = _kb()
    for delegation_id in marked:
        task_id = live.get(delegation_id)
        if not task_id:
            continue
        try:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is not None and row["status"] not in ENDED_STATUSES:
                kb.archive_task(conn, task_id)
        except Exception:
            logger.warning("async_delegation_kanban_cancel_replay_failed task_id=%s", task_id, exc_info=True)


def cancelled_during_launch(delegation_id: str, attempt_id: str, profile_home: Path) -> bool:
    """The record left ``running`` on *attempt_id* (cancelled) while the card was being spawned."""
    try:
        record = _records_of(profile_home).get(delegation_id) or {}
    except Exception:
        return False
    return (record.get("state") == "cancelled"
            and (record.get("attempt") or {}).get("attempt_id") == attempt_id)


def cancel_fn(delegation_id: str, profile_home: Optional[Path]) -> Callable[[], None]:
    return lambda: cancel_unit(delegation_id, profile_home)


# ── §5.4 delivery ───────────────────────────────────────────────────────────


def _put(payloads: List[Dict[str, Any]], profile_home: Path) -> None:
    if not payloads:
        return
    from tools.process_registry import process_registry

    for payload in payloads:
        payload["_registry_profile_home"] = str(profile_home)
        process_registry.completion_queue.put(payload)
        logger.info("async_delegation_kanban_drained delegation_id=%s event_id=%s",
                    payload.get("delegation_id"), payload.get("event_id"))


def _log_tail(task_id: str, limit: int = 2048) -> str:
    try:
        path = _kb().worker_logs_dir(board=_board()) / f"{task_id}.log"
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _terminal_settled(record: Dict[str, Any]) -> bool:
    """The record is terminal and its terminal event left ``pending`` (or it never had one)."""
    if record.get("state") not in SETTLE_STATUS:
        return False
    return not any(isinstance(e, dict) and e.get("type") == "async_delegation" and e.get("state") == "pending"
                   for e in record.get("outbox") or [])


def _mark_drained(conn, task_id: str, delegation_id: str, status: str) -> int:
    """Archive the card, THEN record the drained marker (1). An archive error leaves the card unmarked
    (0), so the next drain (or boot cleanup) retries it; the marker only ever names an archived card."""
    kb = _kb()
    if status != "archived":
        try:
            if not kb.archive_task(conn, task_id):
                row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row is None or row["status"] != "archived":
                    return 0
        except Exception:
            logger.warning("async_delegation_kanban_archive_failed task_id=%s", task_id, exc_info=True)
            return 0
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, DRAINED_EVENT, {"delegation_id": delegation_id})
    return 1


def _records_of(profile_home: Path) -> Dict[str, Dict[str, Any]]:
    with _store.locked_registry(profile_home, write=False) as registry:
        return {k: v for k, v in registry["records"].items() if isinstance(v, dict)}


def _ended_cards(conn) -> List[Any]:
    # Filtered by the body's ``profile_home`` by the caller: the home, not a profile NAME (which
    # differs between launch spellings), is what the registry is keyed on.
    marks = ",".join("?" for _ in ENDED_STATUSES)
    return conn.execute(
        f"SELECT id, status, body, created_at, "
        "(SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = tasks.id) AS ended_at "
        f"FROM tasks WHERE status IN ({marks}) "
        "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = tasks.id AND e.kind = ?)",
        (*ENDED_STATUSES, DRAINED_EVENT),
    ).fetchall()


def _live_kanban_units(profile_home: Path) -> Dict[str, str]:
    """``{delegation_id: task_id}`` of this profile's live kanban records in memory (dispatched here
    or observed at boot): the set the lost-card check re-reads every tick."""
    from tools import async_delegation as ad

    home = str(Path(profile_home).resolve())
    with ad._records_lock:
        return {rid: str(r.get("kanban_task_id") or "") for rid, r in ad._records.items()
                if r.get("kanban_unit") and r.get("status") in ad._LIVE_STATES
                and str(Path(str(r.get("registry_profile_home") or "")).resolve()) == home}


def drain_profile(profile_home: Path, boot_id: str, *,
                  scan: Optional[Callable[[str], Optional[List[Any]]]] = None) -> Dict[str, int]:
    """One card-end-watcher pass for one served profile (§5.4). Call under that profile's scope.

    For each ended, not yet drained card: (1) fail a record still running on the card's attempt
    (the runner died before its terminal write), (2) queue that ONE record's pending events,
    (3) settle the live record, (4) once the terminal event left ``pending``, mark the card
    drained and archive it. Then the lost-card check for live units whose card did not show.
    Steady state (nothing ended, every live card present) is SQLite reads only: no registry lock."""
    out = {"drained": 0, "queued": 0, "failed": 0, "lost": 0}
    profile_home = Path(profile_home)
    home = profile_home.resolve()
    if str(home) in _cancel_scan_pending and _board_state():
        observe_running(profile_home, boot_id, cancelled_only=True)  # retry the inconclusive boot read
    live = _live_kanban_units(profile_home)
    if any(not task_id for task_id in live.values()):
        _refresh_cardless(profile_home, boot_id, live)
    board = _board_state()
    if board is None:
        return out  # unknown is never absent: retried next tick
    if board is False:
        # No board, so no card of any live unit exists: each one is a lost card (the process table decides).
        _drain_lost(profile_home, boot_id, [(d, t) for d, t in live.items() if t], out, scan=scan)
        return out
    kb = _kb()
    with kb.connect_closing(board=_board()) as conn:
        _replay_cancels(conn, live)
        ended = []
        for row in _ended_cards(conn):
            body = parse_card_body(row["body"])
            if body.get("delegation_id") and Path(str(body.get("profile_home") or "")).resolve() == home:
                ended.append((row, body))
        lost = []
        seen = {body["delegation_id"] for _, body in ended}
        for delegation_id, task_id in live.items():
            if delegation_id in seen or not task_id:
                continue
            try:
                present = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            except Exception:
                continue  # read error: unknown is never dead; retried next tick
            if present is None:
                lost.append((delegation_id, task_id))
        if not ended and not lost:
            return out
        try:
            records = _records_of(profile_home)
        except Exception:
            logger.debug("async_delegation_kanban_registry_unreadable home=%s", profile_home, exc_info=True)
            return out
        for row, body in ended:
            delegation_id, attempt_id = body["delegation_id"], body.get("attempt_id")
            record = records.get(delegation_id)
            if record is None:
                out["drained"] += _mark_drained(conn, row["id"], delegation_id, row["status"])
                continue
            if (record.get("attempt") or {}).get("attempt_id") != attempt_id:
                # Superseded (spawn failure, §5.2.4): the record belongs to its in-process attempt now.
                if record.get("state") != "running":
                    out["drained"] += _mark_drained(conn, row["id"], delegation_id, row["status"])
                continue
            if record.get("state") == "running":
                # The runner died without a result: what its children left (setsid'd tools carry the
                # K6 lineage tag) goes now, create-time checked; never a live runner process.
                procs = (scan or lineage_processes)(row["id"])
                waited = _inconclusive_expired(row["id"], row["ended_at"]) if procs is None else None
                if procs is None and waited is None:
                    continue  # scan unknown: a tool may still be writing; retry next tick (bounded)
                _inconclusive_since.pop(row["id"], None)
                _kill_leftovers([p for p in (procs or []) if not _is_runner(p, row["id"])])
                tail = _log_tail(row["id"])
                why = "unit worker ended without a result"
                if waited is not None:
                    why += (f" (dead runner; lineage scan inconclusive for {waited:.0f}s, "
                            "leftover tool processes could not be checked or reaped)")
                if _store.fail_attempt(delegation_id, str(attempt_id), f"{why}: {tail}",
                                       profile_home=profile_home, queued_boot_id=None):
                    out["failed"] += 1
            elif not _needs_enqueue(record, boot_id):
                # Its completion is already queued this boot (waiting on its ack): no registry write.
                settle(delegation_id, record.get("state"))
                if _terminal_settled(record):
                    out["drained"] += _mark_drained(conn, row["id"], delegation_id, row["status"])
                continue
            queued = _store.enqueue_record_outbox(delegation_id, boot_id, profile_home=profile_home)
            _put(queued, profile_home)
            out["queued"] += len(queued)
            fresh = _records_of(profile_home).get(delegation_id) or {}
            settle(delegation_id, fresh.get("state"))
            if _terminal_settled(fresh):
                out["drained"] += _mark_drained(conn, row["id"], delegation_id, row["status"])
        _drain_lost(profile_home, boot_id, lost, out, scan=scan, records=records)
    return out


#: An ended card whose runner died is finalized after this long of inconclusive lineage scans: the card
#: ended, so its runner is gone, and an always-unreadable process table must not hold the result forever
#: (Prism 42bc969f2f92). Leftovers cannot be reaped then; the error is still delivered.
INCONCLUSIVE_SCAN_LIMIT_SECONDS = 600.0
_inconclusive_since: Dict[str, float] = {}


def _inconclusive_expired(task_id: str, ended_at: Any = None) -> Optional[float]:
    """Seconds the deferral has lasted once past the limit, else None. Measured from the card's last
    event (durable: a gateway restart does not restart the clock) or, without one, from this
    process's first inconclusive scan of it."""
    first = _inconclusive_since.setdefault(task_id, time.monotonic())
    waited = time.monotonic() - first
    try:
        waited = max(waited, time.time() - float(ended_at)) if ended_at is not None else waited
    except (TypeError, ValueError):
        pass
    if waited < INCONCLUSIVE_SCAN_LIMIT_SECONDS:
        return None
    logger.warning("async_delegation_kanban_scan_inconclusive_finalized task_id=%s after=%.0fs", task_id, waited)
    return waited


def _needs_enqueue(record: Dict[str, Any], boot_id: str) -> bool:
    """The record has a pending outbox event not yet queued by this boot."""
    return any(isinstance(e, dict) and e.get("state") == "pending" and e.get("queued_boot_id") != boot_id
               for e in record.get("outbox") or [])


def _drain_lost(profile_home: Path, boot_id: str, lost: List[Any], out: Dict[str, int], *,
                scan=None, records: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Live units whose card is positively absent. A record the runner already finished is delivered
    (its pending events queued) before it is settled: no other path would find it again."""
    if not lost:
        return
    if records is None:
        try:
            records = _records_of(profile_home)
        except Exception:
            logger.debug("async_delegation_kanban_registry_unreadable home=%s", profile_home, exc_info=True)
            return
    for delegation_id, task_id in lost:
        record = records.get(delegation_id)
        if record is None or record.get("state") != "running":
            if record is not None:
                queued = _store.enqueue_record_outbox(delegation_id, boot_id, profile_home=profile_home)
                _put(queued, profile_home)
                out["queued"] += len(queued)
            settle(delegation_id, (record or {}).get("state") or "failed")
            continue
        if _lost_card(delegation_id, record, task_id, profile_home, boot_id, scan=scan):
            out["lost"] += 1


def _refresh_cardless(profile_home: Path, boot_id: str, live: Dict[str, str]) -> None:
    """A live unit observed before its card was recorded: adopt the card id once the record has one;
    fail the unit when its launching gateway died without recording one (the boot rule, every tick)."""
    from tools import async_delegation as ad

    try:
        records = _records_of(profile_home)
    except Exception:
        return
    for delegation_id, task_id in list(live.items()):
        if task_id:
            continue
        durable = records.get(delegation_id) or {}
        recorded = str((durable.get("kanban") or {}).get("task_id") or "")
        if recorded:
            with ad._records_lock:
                rec = ad._records.get(delegation_id)
                if rec is not None:
                    rec["kanban_task_id"] = recorded
            live[delegation_id] = recorded
            continue
        if durable.get("state") != "running":
            _put(_store.enqueue_record_outbox(delegation_id, boot_id, profile_home=profile_home), profile_home)
            settle(delegation_id, durable.get("state") or "failed")
            continue
        if _fail_never_started(delegation_id, durable, profile_home, boot_id):
            settle(delegation_id, "failed")


def _fail_never_started(delegation_id: str, durable: Dict[str, Any], profile_home: Path, boot_id: str) -> bool:
    """A running kanban record with no card whose dispatching gateway is gone: no card and no runner
    exist, and none ever will. Deliver the error; never re-run (I3). False while the owner may live."""
    attempt = durable.get("attempt") or {}
    owner = str(attempt.get("owner_boot_id") or "")
    if (durable.get("kanban") or {}).get("task_id") or owner == boot_id or _boot_alive(owner):
        return False
    if _store.fail_attempt(delegation_id, str(attempt.get("attempt_id") or ""),
                           "background unit never started (gateway stopped while launching it)",
                           profile_home=profile_home, queued_boot_id=None):
        _put(_store.enqueue_record_outbox(delegation_id, boot_id, profile_home=profile_home), profile_home)
    return True


def _lost_card(delegation_id: str, record: Dict[str, Any], task_id: str, profile_home: Path,
               boot_id: str, *, scan=None) -> bool:
    """The card row is positively absent: fail the unit only when the process table proves no
    runner is alive; kill tagged leftovers first (create-time checked, I8)."""
    procs = (scan or lineage_processes)(task_id)
    if procs is None:
        return False  # scan unknown: retry next tick
    if any(_is_runner(p, task_id) for p in procs):
        return False
    _kill_leftovers(procs)
    attempt_id = str((record.get("attempt") or {}).get("attempt_id") or "")
    if not _store.fail_attempt(delegation_id, attempt_id, "unit card lost",
                               profile_home=profile_home, queued_boot_id=None):
        return False
    _put(_store.enqueue_record_outbox(delegation_id, boot_id, profile_home=profile_home), profile_home)
    settle(delegation_id, "failed")
    return True


def _is_runner(proc, task_id: Optional[str] = None) -> bool:
    """The unit's runner: its argv, or (``setproctitle`` rewrote argv) the canonical worker title."""
    try:
        cmdline = proc.cmdline() or []
    except Exception:
        return True  # unreadable: assume a live runner (unknown is never dead)
    if RUNNER_ARG in cmdline:
        return True
    if task_id:
        from hermes_cli.kanban_db import _proc_is_titled_worker

        return _proc_is_titled_worker(cmdline, task_id)
    return False


def lineage_processes(task_id: str) -> Optional[List[Any]]:
    """Same-uid processes carrying this card's lineage tag (K6), as ``psutil.Process`` handles.
    None (unknown) when the process table or any same-uid environment cannot be read."""
    import psutil
    from hermes_cli.kanban_db import KANBAN_LINEAGE_ENV

    me, uid = os.getpid(), os.getuid() if hasattr(os, "getuid") else None
    found = []
    try:
        procs = list(psutil.process_iter(["pid", "uids"]))
    except Exception:
        return None
    prefix = f"{task_id}:"
    for proc in procs:
        if proc.pid in (me, 0, 1):
            continue
        try:
            uids = proc.info.get("uids")
            # Same uid = real AND effective uid ours: a setuid process (sudo, login) we can neither
            # read nor signal is not ours, so its unreadable env does not make the scan unknown.
            if uid is not None and uids is not None and (uids.real != uid or uids.effective != uid):
                continue
            if _titled_worker(proc, task_id):
                found.append(proc)  # setproctitle overwrote argv and env: the title names the card
                continue
            env = proc.environ()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # Same uid but non-dumpable (Linux: systemd --user, sd-pam, ssh-agent, sshd-session): the
            # kernel hides its environ. Like the setuid skip above, it is not ours; counting it unknown
            # made every scan on a Linux host with a user session unknown, forever.
            continue
        except Exception:
            return None
        tag = env.get(KANBAN_LINEAGE_ENV) or ""  # lineage-reader: ok — the lost-card reaper (spec §5.4)
        if tag.startswith(prefix):
            found.append(proc)
    return found


def _titled_worker(proc, task_id: str) -> bool:
    from hermes_cli.kanban_db import _proc_is_titled_worker

    try:
        return _proc_is_titled_worker(proc.cmdline() or [], task_id)
    except Exception:
        return False


def _kill_leftovers(procs: List[Any], grace: float = 5.0) -> None:
    import psutil

    live = []
    for proc in procs:
        try:
            if proc.is_running():  # psutil re-checks the create time: a reused pid is not ours
                proc.terminate()
                live.append(proc)
        except psutil.Error:
            continue
    _, alive = psutil.wait_procs(live, timeout=grace)
    for proc in alive:
        try:
            if proc.is_running():
                proc.kill()
        except psutil.Error:
            continue


def settle(delegation_id: str, state: Optional[str]) -> bool:
    """§5.4 step 3 (``async_delegation._settle_kanban_record``)."""
    from tools import async_delegation as ad

    return ad._settle_kanban_record(delegation_id, state)


# ── §5.4 boot ───────────────────────────────────────────────────────────────


def _boot_alive(boot_id: str) -> bool:
    if not boot_id:
        return False
    try:
        return bool(_store.is_boot_id_alive(boot_id))
    except Exception:
        return True  # unknown is never dead


#: Profile homes whose boot read of cancelled cards was inconclusive: each conclusive drain retries it.
_cancel_scan_pending: set = set()


def observe_running(profile_home: Path, boot_id: str, *, cancelled_only: bool = False) -> int:
    """Boot: one observer live record per running kanban record of this profile, so
    ``active_count``, ``has_live_for_session`` and ``/stop`` see the unit; and archive this
    profile's delegations cards older than 48 h whose record is terminal or missing.
    ``cancelled_only``: the drain's retry of an inconclusive boot read (cancelled records only)."""
    from tools import async_delegation as ad

    profile_home = Path(profile_home)
    try:
        records = _records_of(profile_home)
    except Exception:
        return 0
    added = 0
    home_key = str(profile_home.resolve())
    cancelled = _cancelled_with_live_card(records, profile_home)
    if cancelled is None:
        # Unknown is not live: observing every cancelled record would resurrect long-settled ones as
        # observers that never settle (Prism 68f494996a96). A later conclusive drain retries the read.
        _cancel_scan_pending.add(home_key)
        cancelled = set()
    else:
        _cancel_scan_pending.discard(home_key)
    for delegation_id, durable in records.items():
        if not _store.is_kanban_record(durable):
            continue
        if cancelled_only and delegation_id not in cancelled:
            continue
        if durable.get("state") != "running" and delegation_id not in cancelled:
            continue
        if durable.get("state") == "running" and _fail_never_started(delegation_id, durable, profile_home, boot_id):
            continue
        source = durable.get("source") or {}
        route = durable.get("route") or {}
        goals = [str(t.get("goal") or "") for t in source.get("tasks") or [] if isinstance(t, dict)]
        with ad._records_lock:
            if delegation_id in ad._records:
                continue
            ad._records[delegation_id] = {
                "delegation_id": delegation_id,
                "goal": goals[0] if len(goals) == 1 else f"{len(goals)} parallel subagents",
                "goals": goals, "context": source.get("shared_context"), "role": "leaf",
                "model": (durable.get("execution") or {}).get("model"),
                "session_key": route.get("session_key", ""),
                "origin_ui_session_id": route.get("origin_ui_session_id", ""),
                "parent_session_id": route.get("parent_session_id"), "status": "running",
                "dispatched_at": durable.get("created_at"), "completed_at": None,
                "is_batch": source.get("kind") == "batch" or len(goals) > 1,
                "attempt_id": (durable.get("attempt") or {}).get("attempt_id"),
                "owner_boot_id": (durable.get("attempt") or {}).get("owner_boot_id"),
                "registry_profile_home": str(profile_home),
                "slot_key": (durable.get("kanban") or {}).get("slot_key") or delegation_id,
                "interrupt_fn": cancel_fn(delegation_id, profile_home), "progress_fn": None,
                "kanban_unit": True, "kanban_task_id": (durable.get("kanban") or {}).get("task_id"),
                **({"kanban_cancel": True} if delegation_id in cancelled else {}),
                "_context": contextvars.copy_context()}
        added += 1
    if not cancelled_only:
        _archive_stale_cards(records, profile_home)
    return added


def _cancelled_with_live_card(records: Dict[str, Dict[str, Any]], profile_home: Path) -> Optional[set]:
    """Cancelled kanban records whose card has not ended: the cancel's archive never landed (the
    gateway exited before its thread ran). Boot observes them so the drain replays the archive.
    None: the read was inconclusive (board check, connect or a card SELECT failed); the caller
    observes nothing and the drain retries the whole read on a later tick (Prism 3b8946d15b24,
    68f494996a96)."""
    candidates = {d: str((r.get("kanban") or {}).get("task_id") or "") for d, r in records.items()
                  if r.get("state") == "cancelled" and _store.is_kanban_record(r)
                  and (r.get("kanban") or {}).get("task_id")}
    if not candidates:
        return set()
    board = _board_state()
    if board is False:
        return set()
    if board is None:
        logger.warning("async_delegation_kanban_cancel_scan_inconclusive home=%s (board check failed)", profile_home)
        return None
    kb = _kb()
    out = set()
    try:
        with kb.connect_closing(board=_board()) as conn:
            for delegation_id, task_id in candidates.items():
                row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row is not None and row["status"] not in ENDED_STATUSES:
                    out.add(delegation_id)
    except Exception:
        logger.warning("async_delegation_kanban_cancel_scan_inconclusive home=%s", profile_home, exc_info=True)
        return None
    return out


def _archive_stale_cards(records: Dict[str, Dict[str, Any]], profile_home: Path) -> None:
    if not _board_exists():
        return
    kb = _kb()
    cutoff = time.time() - STALE_CARD_SECONDS
    try:
        with kb.connect_closing(board=_board()) as conn:
            rows = conn.execute(
                "SELECT id, status, body FROM tasks WHERE status != 'archived' AND created_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = tasks.id AND e.kind = ?)",
                (int(cutoff), DRAINED_EVENT)).fetchall()
            for row in rows:
                body = parse_card_body(row["body"])
                if Path(str(body.get("profile_home") or "")).resolve() != profile_home.resolve():
                    continue
                record = records.get(body.get("delegation_id") or "")
                # A terminal record whose completion is still pending is left to drain_profile.
                if record is None or _terminal_settled(record):
                    _mark_drained(conn, row["id"], str(body.get("delegation_id")), row["status"])
    except Exception:
        logger.warning("async_delegation_kanban_stale_archive_failed home=%s", profile_home, exc_info=True)
