"""Kanban pool picks through the ONE placement scorer (card t_d6ce856e).

``SpilloverPlan.take()`` used to pick an unpinned host by the sidecar ``priority``
list (first OK-band host, then least ``projected()`` with priority as the tie).
With ``<root>/config/fleet-hosts.json`` present it now asks
``placement.choose()`` (vendored from hermes-home, #3945): headroom gate ->
Job overrides (affinity / continuity / priority) -> ci-box first while under
its physical cores -> most headroom. The order of ``kanban-pool.json``
``priority`` is no longer read on that path; the list only enumerates hosts.

What stays BEFORE the scorer (hard, unchanged): pins, the reach matrix and
capability ``only`` set, the assignee's profile list, slots, ``state``, and the
pressure band (stale / unreadable / hot-streaked = 0 slots, A1 #3922). Those
hosts reach ``choose()`` as exclusions or closed samples, so the decision rows
still name them.

Samples come from the per-tick probe (``/proc/loadavg`` + the host's
``host-pressure.json`` in one ssh round-trip, age = remote clock - ``at``).
Every take, pinned or scored, is a ledger row for ``RESERVE_TTL_S`` written
BEFORE launch (``placement.admit`` / ``book_pin``), renewed by the tick every
``RENEW_EVERY_S`` while its run is live (``renew_running``), and released after
the run end COMMITS (``_end_run`` -> ``on_commit``); an orphan (holder gone, no
release) is reaped at TTL by the same tick. Every admit, refusal, release and
reap is a row in ``var/placement/admissions.jsonl`` (allocation 2.3). A dry run writes nothing and adds
only its own simulated takes, so N picks see N headrooms there too.

No table (hermes-home not yet deployed) = the previous pick, logged once.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from hermes_cli import placement

_log = logging.getLogger(__name__)

CONSUMER = "kanban"
# Expected run length of a pool worker: measured over 244 ended worker_placed
# runs on the default board 2026-10-12 (p50 11 min, p75 34, p90 73).
RESERVE_TTL_S = 30 * 60
# A live run's reservation is renewed to now + RESERVE_TTL_S at most this often
# (renew_running), so it never lapses while the run is live.
RENEW_EVERY_S = 5 * 60
# Card priority (higher = more urgent; P0 lanes run at >= 100) -> Job.priority
# (placement: 0/1 claim the fastest eligible core class).
JOB_PRIORITY_BANDS = ((100, 0), (90, 1))
RESERVATION_KEY = "placement_reservation"
# The axes the kanban probe reads (/proc/loadavg + host-pressure.json). The
# collector's file carries no %system, so that axis is declared unavailable
# rather than closing every host as unmeasured (placement.REQUIRED_AXES).
PROBE_AXES = tuple(a for a in placement.REQUIRED_AXES if a != "system_pct")


def table_path(root: Path) -> Path:
    return Path(root) / placement.TABLE_REL


def load_table(root: Path) -> Optional[placement.HostTable]:
    """The host table, or None (absent/invalid: the caller keeps the old pick)."""
    p = table_path(root)
    if not p.is_file():
        return None
    try:
        return placement.load_hosts(p)
    except placement.PlacementError as exc:
        _log.warning("kanban pool: placement table refused, old pick kept: %s", exc)
        return None


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def sample_fields(load5: Optional[float], remote_now: Optional[float], text: str, *,
                  stale_after_s: float, local_now: Optional[float] = None) -> Dict[str, Any]:
    """The placement inputs one probe carries, for the plan's ``detail`` row.

    ``sample_at`` is on the PLANNER's clock (``local_now`` - the age the remote
    clock measured), never the remote host's ``at``: admit() re-checks sample
    age against its own clock at decision time."""
    out: Dict[str, Any] = {"load5": load5, "mem_available_gb": None, "psi_cpu_some10": None,
                           "psi_mem_some10": None, "system_pct": None, "sample_at": None,
                           "sample_status": "absent"}
    try:
        doc = json.loads(text) if text else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return out
    for k in ("mem_available_gb", "psi_cpu_some10", "psi_mem_some10", "system_pct"):
        out[k] = _num(doc.get(k))
    at = _num(doc.get("at"))
    if at is None or remote_now is None or not math.isfinite(remote_now):
        out["sample_status"] = "stale"
        return out
    age = remote_now - at
    out["sample_at"] = (local_now - age) if _num(local_now) is not None else at
    out["sample_status"] = "stale" if age > stale_after_s else "fresh"
    return out


def sample_from_detail(d: Mapping) -> Optional[placement.Sample]:
    status = d.get("sample_status")
    if status is None:
        return None
    return placement.Sample(
        mem_available_gb=_num(d.get("mem_available_gb")), load1=_num(d.get("load1")),
        load5=_num(d.get("load5")), psi_cpu_some10=_num(d.get("psi_cpu_some10")),
        psi_mem_some10=_num(d.get("psi_mem_some10")), system_pct=_num(d.get("system_pct")),
        at=_num(d.get("sample_at")), status=str(status),
        note="" if status == "fresh" else str(d.get("pressure") or ""))


def job_priority(card_priority) -> Optional[int]:
    p = _num(card_priority)
    if p is None:
        return None
    for floor, level in JOB_PRIORITY_BANDS:
        if p >= floor:
            return level
    return None


def previous_host(conn, task_id: str) -> Optional[str]:
    """The pool host this card's latest placed run ran on (continuity)."""
    from hermes_cli import kanban_placement_reasons as _pr
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, _pr.WORKER_PLACED_EVENT)).fetchone()
        host = json.loads(row[0] or "{}").get("host") if row else None
    except Exception:
        return None
    return host if isinstance(host, str) and host else None


def job_for_row(conn, row, *, affinity_targets: Optional[Mapping[str, str]] = None) -> placement.Job:
    """``Job`` for a ready row: affinity = the one remote host a skill manifest
    targets (``host_affinity: <host>``), continuity = the previous placed run's
    host, priority = the card priority band."""
    try:
        prio = row["priority"]
    except (IndexError, KeyError):
        prio = None
    targets = {h.lower() for h in (affinity_targets or {}).values() if h}
    try:
        body = row["body"]
    except (IndexError, KeyError):
        body = None
    # A card may override the kanban class footprint with a body line
    # ``footprint: ram_gb=8 threads=4`` (allocation 2.3); a malformed one is
    # refused by the scorer, never read as the default.
    return placement.Job(affinity=next(iter(targets)) if len(targets) == 1 else None,
                         continuity=previous_host(conn, row["id"]), priority=job_priority(prio),
                         footprint=placement.footprint_from_text(body))


def _add(acc: Dict[str, placement.Footprint], key: str, fp: placement.Footprint) -> None:
    prev = acc.get(key)
    acc[key] = fp if prev is None else placement.Footprint(prev.ram_gb + fp.ram_gb, prev.threads + fp.threads)


def _inputs(plan, names):
    sig = plan.signal
    table = sig.placement_table
    canon = {n: table.resolve(n) or n for n in names}
    samples: Dict[str, Optional[placement.Sample]] = {}
    exclusions: Dict[str, str] = {}
    for n in names:
        d = plan.detail.get(n) or {}
        s = sample_from_detail(d)
        samples[canon[n]] = s
        if plan.slots.get(n, 0) <= 0 and (s is None or s.status == "fresh"):
            exclusions[canon[n]] = str(d.get("pressure") or "no free slot")
    root = Path(sig.placement_reservations).parents[2] if sig.placement_reservations else None
    kw = dict(candidates=[canon[n] for n in names],
              previous=table.resolve(plan.last_pick) if plan.last_pick else None,
              exclusions=exclusions, require_axes=PROBE_AXES, stale_after_s=sig.policy.stale_after_s,
              exclusions_path=None if root is None else root / placement.EXCLUSIONS_REL)
    return table, samples, kw


def choose(plan, names, job: Optional[placement.Job]) -> placement.Decision:
    """``placement.choose`` over ``names`` with NO write (dry run, diagnostics):
    the live ledger plus this plan's SIMULATED takes only. A take that wrote a
    ledger row is already in the ledger read; adding it again would double-count
    it (Prism 08bbc332020f / 32dbcfc15ac3). Keys are the TABLE's canonical ids.
    A corrupt ledger / exclusions file raises PlacementError."""
    table, samples, kw = _inputs(plan, names)
    sig = plan.signal
    now = sig.clock()
    fp = table.footprint(CONSUMER)
    resv: Dict[str, placement.Footprint] = {}
    for h, f in dict(plan.placement_reserved()).items():
        _add(resv, table.resolve(h) or h, f)
    used: Dict[str, float] = {}
    # Slot use is read even when the footprints come from a fixed placement_live
    # snapshot, so --placement never shows eligible where admit() refuses (Prism 404998a66d0e).
    if sig.placement_reservations is not None:
        for h, k in placement.live_slots(CONSUMER, sig.placement_reservations, now=now, table=table).items():
            used[h] = used.get(h, 0.0) + k
    for n, k in plan._simulated.items():
        if k:
            c = table.resolve(n) or n
            _add(resv, c, placement.Footprint(k * fp.ram_gb, k * fp.threads))
            used[c] = used.get(c, 0.0) + k
    return placement.choose(table, CONSUMER, samples, job=job, reservations=resv, slots_used=used,
                            now=now, **kw)


def admit(plan, names, job: Optional[placement.Job], job_id: Optional[str]):
    """ONE locked read-choose-reserve (``placement.admit``) BEFORE launch, so two
    dispatchers on different boards cannot both spend the same headroom (Prism
    e35e13b216d4 / 62f48e3043e0). Every earlier take of this tick, pinned ones
    included, is already a ledger row. Returns ``(Decision, reservation id or
    None)``; a failed claim or spawn refunds through ``SpilloverPlan.release``."""
    sig = plan.signal
    table, samples, kw = _inputs(plan, names)
    rpath = Path(sig.placement_reservations)
    # The production clock is time.time: leave ``now`` unset so placement takes it
    # AFTER the ledger lock (Prism 7859db6445fc). An injected clock is honoured.
    now = None if sig.clock is time.time else sig.clock()
    return placement.admit(table, CONSUMER, samples, ttl_s=RESERVE_TTL_S, job_id=job_id, path=rpath, now=now,
                           admissions_path=rpath.parent / placement.ADMISSIONS_REL.name, job=job, **kw)


def book_pin(plan, name: str, job_id: Optional[str], job: Optional[placement.Job] = None) -> str:
    """Book a PINNED take's footprint in the ledger (no scorer: a pin keeps its
    precedence), so the next unpinned admit() and other boards' dispatchers see
    it (Prism 4609a040ad20 / b8c13f6c2e7a / 9b1349450aff). Returns the id."""
    sig = plan.signal
    table = sig.placement_table
    rpath = Path(sig.placement_reservations)
    fp = table.footprint(CONSUMER, job.footprint if job is not None else None)
    return placement.reserve(table.resolve(name) or name, CONSUMER, fp,
                             ttl_s=RESERVE_TTL_S, job_id=job_id, path=rpath,
                             now=None if sig.clock is time.time else sig.clock(),
                             admissions_path=rpath.parent / placement.ADMISSIONS_REL.name)


def release_id(sig, rid: Optional[str]) -> None:
    if not rid or sig is None or getattr(sig, "placement_reservations", None) is None:
        return
    try:
        placement.release(rid, path=sig.placement_reservations)
    except placement.PlacementError as exc:
        _log.warning("kanban pool: refund of reservation %s failed (TTL bounds it): %s", rid, exc)


def pool_key(table, names, host: Optional[str]) -> Optional[str]:
    """The pool host id behind the scorer's canonical ``host`` (Prism 5c776514232a)."""
    if host is None:
        return None
    for n in names:
        if (table.resolve(n) or n) == host:
            return n
    return None


def reservation_for_run(conn, task_id: str, run_id: Optional[int]) -> Optional[str]:
    """The headroom reservation id the run's ``worker_placed`` event carries."""
    if run_id is None:
        return None
    from hermes_cli import kanban_placement_reasons as _pr
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (task_id, run_id, _pr.WORKER_PLACED_EVENT)).fetchone()
        rid = json.loads(row[0] or "{}").get(RESERVATION_KEY) if row else None
    except Exception as exc:
        _log.warning("kanban pool: placement reservation lookup failed for %s run %s: %s", task_id, run_id, exc)
        return None
    return rid if isinstance(rid, str) and rid else None


def release_reservation(rid: str, root: Optional[Path] = None) -> bool:
    try:
        path = placement.reservations_path(None if root is None else Path(root) / placement.RESERVATIONS_REL)
        return placement.release(rid, path=path)
    except Exception as exc:
        _log.warning("kanban pool: placement release of %s failed (TTL bounds it): %s", rid, exc)
        return False


def release_for_run(conn, task_id: str, run_id: Optional[int], root: Optional[Path] = None) -> bool:
    """Release the reservation the run's ``worker_placed`` event carries, now."""
    rid = reservation_for_run(conn, task_id, run_id)
    return False if rid is None else release_reservation(rid, root)


def running_reservations(conn) -> List[Tuple[str, str]]:
    """``(task_id, reservation id)`` for every RUNNING card whose current run's
    ``worker_placed`` event carries a headroom reservation."""
    from hermes_cli import kanban_placement_reasons as _pr
    out: List[Tuple[str, str]] = []
    for tid, payload in conn.execute(
            "SELECT t.id, e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE t.status = 'running' AND e.kind = ? AND e.run_id IS NOT NULL "
            "AND e.run_id = t.current_run_id", (_pr.WORKER_PLACED_EVENT,)).fetchall():
        try:
            rid = json.loads(payload or "{}").get(RESERVATION_KEY)
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(rid, str) and rid:
            out.append((tid, rid))
    return out


def renew_running(boards, connect, path, *, now: Optional[float] = None) -> Dict[str, int]:
    """Renew the reservation of every live pool run to ``now + RESERVE_TTL_S``
    (Prism 6c6c0c765a1a): a run longer than the admission TTL stays in the
    scorer's headroom and slot count. A live run whose row already lapsed is a
    LEAK (the scorer under-counted it until now): logged, never resurrected.
    Returns ``{"renewed": n, "lapsed": n}``."""
    counts = {"renewed": 0, "lapsed": 0}
    for b in boards or ():
        slug = (b.get("slug") if isinstance(b, dict) else None) or "default"
        conn = None
        try:
            conn = connect(board=slug)
            rows = running_reservations(conn)
        except Exception as exc:
            _log.warning("kanban pool: reservation renewal could not read board %s: %s", slug, exc)
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        for tid, rid in rows:
            try:
                ok = placement.renew(rid, ttl_s=RESERVE_TTL_S, path=path, now=now)
            except placement.PlacementError as exc:
                _log.warning("kanban pool: renewal of reservation %s (%s) failed: %s", rid, tid, exc)
                continue
            if ok:
                counts["renewed"] += 1
            else:
                counts["lapsed"] += 1
                _log.warning("kanban pool: headroom reservation LEAK: %s (%s on board %s) expired while its "
                             "run is live; the scorer did not count it", rid, tid, slug)
    return counts
