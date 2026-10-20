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

Router Core Card B1 (t_4187c8f1): ``kanban.placement.allocd`` (off | shadow |
on, default off, read per tick) routes the UNPINNED scorer take through allocd
(``alloc_client``, vendored). off = the path above, unchanged, allocd never
consulted. shadow = allocd scores the same row first (``shadow: true``, nothing
booked), the old path acts, and one line per decision lands in
``<root>/var/alloc/shadow/kanban.jsonl``. on = ``alloc_client.admit`` decides
and books; release / renew of that reservation go through ``alloc_client``.
allocd is given the candidates AFTER pins, reach / capability ``only``, board
roots and the assignee's profile list, and after the pool's structural limits
(host state, reachability, pool slots); the local pressure band is allocd's to
decide. A dry run never consults allocd.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import time
import uuid
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
# kanban.placement.allocd (t_4187c8f1).
ALLOCD_OFF, ALLOCD_SHADOW, ALLOCD_ON = "off", "shadow", "on"
ALLOCD_MODES = (ALLOCD_OFF, ALLOCD_SHADOW, ALLOCD_ON)
# worker_placed key naming who booked the reservation; absent = the in-process ledger.
RESERVATION_VIA_KEY = "placement_reservation_via"
VIA_ALLOC_CLIENT = "alloc_client"
# Run event: alloc_client renewed a run's reservation by RE-ADMITTING it under a new
# id (``{"from": old, "to": new}``). Its alias map is process-local, so the run's
# reservation id after the replacement lives here, read back by run_reservation /
# running_reservations after a restart (Prism 43f676a1839e).
RESERVATION_REPLACED_EVENT = "placement_reservation_replaced"
SHADOW_REL = Path("var") / "alloc" / "shadow" / f"{CONSUMER}.jsonl"


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


# host-pressure.json fields the scorer reads beyond the v1 axes: D-state netting (#4696) and scorer v2's
# occupancy / PSI avg60 (t_f8a3b6c3). Absent from a probe = None, which the scorer treats as unmeasured.
V2_FIELDS = ("d_threads", "runnable_ewma", "procs_running", "psi_cpu_some60")
# kanban.admission: utilization axes (admission.admit; Prism a864560114d1). Absent = None = ``unmeasured:<axis>``.
# ``ncpu`` is carried only when the pressure row has it: it is the denominator of that row's ``cpu_busy`` and,
# when absent, the probe's own getconf count already in the detail row stands (never overwritten with None).
UTIL_FIELDS = ("cpu_busy", "psi_io_some10")


def sample_fields(load5: Optional[float], remote_now: Optional[float], text: str, *,
                  stale_after_s: float, local_now: Optional[float] = None) -> Dict[str, Any]:
    """The placement inputs one probe carries, for the plan's ``detail`` row.

    ``sample_at`` is on the PLANNER's clock (``local_now`` - the age the remote
    clock measured), never the remote host's ``at``: admit() re-checks sample
    age against its own clock at decision time."""
    out: Dict[str, Any] = {"load5": load5, "mem_available_gb": None, "psi_cpu_some10": None,
                           "psi_mem_some10": None, "system_pct": None, "sample_at": None,
                           "sample_status": "absent"}
    out.update({k: None for k in V2_FIELDS + UTIL_FIELDS})
    try:
        doc = json.loads(text) if text else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return out
    for k in ("mem_available_gb", "psi_cpu_some10", "psi_mem_some10", "system_pct") + V2_FIELDS + UTIL_FIELDS:
        out[k] = _num(doc.get(k))
    if _num(doc.get("ncpu")) is not None and doc["ncpu"] > 0:
        out["ncpu"] = _num(doc["ncpu"])
    if isinstance(doc.get("os"), str) and doc["os"]:
        out["os"] = doc["os"]
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
        note="" if status == "fresh" else str(d.get("pressure") or ""),
        ncpu=_num(d.get("ncpu")), os=d.get("os") if isinstance(d.get("os"), str) else None,
        **{k: _num(d.get(k)) for k in V2_FIELDS + UTIL_FIELDS})


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
    host, priority = the card priority band, profile = the assignee (its
    measured per-profile kanban class when the table has one, t_edc220cc)."""
    try:
        prio = row["priority"]
    except (IndexError, KeyError):
        prio = None
    targets = {h.lower() for h in (affinity_targets or {}).values() if h}
    try:
        body = row["body"]
    except (IndexError, KeyError):
        body = None
    try:
        profile = row["assignee"] or None
    except (IndexError, KeyError):
        profile = None
    # A card may override the kanban class footprint with a body line
    # ``footprint: ram_gb=8 threads=4`` (allocation 2.3); a malformed one is
    # refused by the scorer, never read as the default.
    return placement.Job(affinity=next(iter(targets)) if len(targets) == 1 else None,
                         continuity=previous_host(conn, row["id"]), priority=job_priority(prio),
                         footprint=placement.footprint_from_text(body), profile=profile)


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
    fp = table.footprint(CONSUMER, job.footprint if job is not None else None,
                         profile=job.profile if job is not None else None)
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


# -- kanban.placement.allocd (Router Core Card B1, t_4187c8f1) ---------------

def allocd_mode(kanban_cfg: Any) -> str:
    """``kanban.placement.allocd``: off | shadow | on (default off). YAML's bare
    ``off`` / ``on`` arrive as booleans and mean the same; anything else is off."""
    pl = kanban_cfg.get("placement") if isinstance(kanban_cfg, Mapping) else None
    v = pl.get("allocd", ALLOCD_OFF) if isinstance(pl, Mapping) else ALLOCD_OFF
    if v is False or v is None:
        return ALLOCD_OFF
    if v is True:
        return ALLOCD_ON
    if isinstance(v, str) and v.strip().lower() in ALLOCD_MODES:
        return v.strip().lower()
    _log.warning("kanban pool: kanban.placement.allocd=%r is not off|shadow|on; off", v)
    return ALLOCD_OFF


def plan_allocd_mode(plan) -> str:
    """The mode THIS take runs under: a dry run never consults allocd."""
    if plan.dry_run or plan.signal is None or plan.signal.placement_table is None:
        return ALLOCD_OFF
    return getattr(plan.signal, "allocd", ALLOCD_OFF)


def structural_free(plan, name: str) -> int:
    """Pool slots left on ``name`` from the pool's STRUCTURAL limits only (enabled,
    ``state`` active, the probe answered, slots - running - this tick's takes).
    The local pressure band and memory cap are not applied: on the allocd path
    they are allocd's decision."""
    h = plan.hosts.get(name)
    d = plan.detail.get(name) or {}
    if h is None or not (h.enabled and h.state == "active") or d.get("load1") is None:
        return 0
    return max(0, h.slots - int(d.get("running") or 0) - plan.taken.get(name, 0))


def _job_kw(job: Optional[placement.Job]) -> Dict[str, Any]:
    return {} if job is None else {k: v for k, v in dataclasses.asdict(job).items() if v is not None}


def allocd_request(plan, names, job: Optional[placement.Job]) -> Dict[str, Any]:
    """The admit fields every allocd ask carries (on and shadow alike): the
    filtered candidates as table ids, the Job overrides, the hysteresis pick."""
    table = plan.signal.placement_table
    return {"candidates": [table.resolve(n) or n for n in names], "job": _job_kw(job),
            "previous": table.resolve(plan.last_pick) if plan.last_pick else None}


def allocd_admit(plan, names, job: Optional[placement.Job], job_id: Optional[str]):
    """``alloc_client.admit`` for an unpinned take (mode on). Returns AdmitResult;
    raises alloc_client.Refused on a terminal answer (never bypassed)."""
    from hermes_cli import alloc_client
    return alloc_client.admit(CONSUMER, job_id=admission_key(job_id), ttl_s=RESERVE_TTL_S,
                              **allocd_request(plan, names, job))


def admission_key(job_id: Optional[str]) -> Optional[str]:
    """The idempotency key for ONE admission: ``<card id>#<nonce>`` (Prism 2de4f2d22204).
    alloc_client and allocd ``reuse_job`` on it, so it must be stable across that one
    admission's allocd -> fallback replay (one ``alloc_client.admit`` call) and fresh
    per take: a retried card must never be handed the previous run's live row."""
    return None if job_id is None else f"{job_id}#{uuid.uuid4().hex[:12]}"


def release_alloc(rid: str) -> bool:
    """Release a reservation alloc_client booked (allocd, or its in-process fallback)."""
    from hermes_cli import alloc_client
    try:
        return alloc_client.release(rid, consumer=CONSUMER)
    except Exception as exc:  # Refused / an unreadable ledger: the TTL bounds the row
        _log.warning("kanban pool: alloc_client release of %s failed (TTL bounds it): %s", rid, exc)
        return False


def _renew_alloc(rid: str) -> bool:
    """Renew a reservation alloc_client booked; a terminal refusal is a failed renewal."""
    from hermes_cli import alloc_client
    try:
        return alloc_client.renew(rid, ttl_s=RESERVE_TTL_S, consumer=CONSUMER)
    except Exception as exc:  # Refused / an unreadable ledger
        raise placement.PlacementError(f"alloc_client renew: {exc}") from exc


def _alloc_current(rid: str) -> str:
    """The id alloc_client now tracks for ``rid`` (its process-local re-admit alias)."""
    from hermes_cli import alloc_client
    return alloc_client._current(rid)


def _persist_replacement(connect, slug: str, task_id: str, old: str, new: str) -> None:
    conn = None
    try:
        conn = connect(board=slug)
        record_replacement(conn, task_id, old, new)
    except Exception as exc:
        _log.warning("kanban pool: reservation replacement %s -> %s (%s on board %s) not recorded: %s",
                     old, new, task_id, slug, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def shadow_ask(plan, names, job: Optional[placement.Job], job_id: Optional[str]) -> Dict[str, Any]:
    """What allocd WOULD answer for this take (mode shadow), asked BEFORE the old
    path books: ``/admit`` with ``shadow: true`` (scored, nothing reserved) over the
    candidates mode on would send. Never the in-process fallback, never a write."""
    from hermes_cli import alloc_client
    cands = [n for n in names if structural_free(plan, n) > 0]
    req = allocd_request(plan, cands, job)
    out: Dict[str, Any] = {"candidates": req["candidates"], "answered": False, "new_host": None,
                           "new_rule": None, "new_reason": ""}
    if not cands:
        out.update(answered=True, new_reason="no_candidates")
        return out
    body = {"consumer": CONSUMER, "job_id": job_id or "shadow", "ttl_s": RESERVE_TTL_S, "class": None,
            "footprint": None, "shadow": True, **req}
    try:
        if (placement.fleet_root() / alloc_client.ROUTER_OFF_REL).exists():
            raise alloc_client.Unreachable("router-off file present")
        code, ans = alloc_client._call("/admit", body, timeout=alloc_client.TIMEOUT_S)
    except alloc_client.Unreachable as exc:
        out["new_reason"] = f"allocd_unreachable: {exc}"
        return out
    except alloc_client.Refused as exc:
        out["new_reason"] = f"allocd_refused: {exc}"
        return out
    if code != 200 or not isinstance(ans, dict) or ans.get("shadow") is not True:
        out["new_reason"] = f"allocd_unexpected_reply: {code}"
        return out
    out.update(answered=True, new_host=ans.get("host") or None, new_rule=ans.get("rule") or None,
               new_reason=ans.get("reason") or "")
    return out


def shadow_record(plan, ask: Mapping[str, Any], host, job_id: Optional[str]) -> Dict[str, Any]:
    """Append one decision to ``<root>/var/alloc/shadow/kanban.jsonl``:
    ``{at, consumer, job_id, old_host, new_host, agree, reason, old_rule, new_rule, candidates}``.
    ``agree`` is null when allocd gave no answer (not a comparison: never counted
    toward the n >= 50 promotion gate). Hosts are table ids."""
    sig = plan.signal
    table = sig.placement_table
    old = (table.resolve(host.name) or host.name) if host is not None else None
    new = ask.get("new_host")
    d = plan.last_decision
    old_rule = getattr(d, "rule", None) if host is not None else None
    if not ask.get("answered"):
        agree, reason = None, ask.get("new_reason") or "allocd gave no answer"
    else:
        agree = old == new
        reason = "agree" if agree else (
            f"old {old or 'refused: ' + (plan.refusal or '?')} vs new {new or 'refused: ' + (ask.get('new_reason') or '?')}")
    line = {"at": time.time(), "consumer": CONSUMER, "job_id": job_id, "old_host": old, "new_host": new,
            "agree": agree, "reason": reason, "old_rule": old_rule or None, "new_rule": ask.get("new_rule"),
            "candidates": list(ask.get("candidates") or ())}
    p = Path(sig.placement_reservations).parents[2] / SHADOW_REL
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError as exc:
        _log.warning("kanban pool: shadow line not written (%s): %s", p, exc)
    return line


def pool_key(table, names, host: Optional[str]) -> Optional[str]:
    """The pool host id behind the scorer's canonical ``host`` (Prism 5c776514232a)."""
    if host is None:
        return None
    for n in names:
        if (table.resolve(n) or n) == host:
            return n
    return None


def run_reservation(conn, task_id: str, run_id: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    """``(reservation id, via)`` the run's ``worker_placed`` event carries; ``via``
    is ``alloc_client`` for an allocd-path take, else None (the in-process ledger)."""
    if run_id is None:
        return None, None
    from hermes_cli import kanban_placement_reasons as _pr
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (task_id, run_id, _pr.WORKER_PLACED_EVENT)).fetchone()
        doc = json.loads(row[0] or "{}") if row else {}
    except Exception as exc:
        _log.warning("kanban pool: placement reservation lookup failed for %s run %s: %s", task_id, run_id, exc)
        return None, None
    rid = doc.get(RESERVATION_KEY)
    if not (isinstance(rid, str) and rid):
        return None, None
    via = VIA_ALLOC_CLIENT if doc.get(RESERVATION_VIA_KEY) == VIA_ALLOC_CLIENT else None
    return (_replaced_rid(conn, task_id, run_id, rid) if via else rid), via


def _replaced_rid(conn, task_id: str, run_id: int, rid: str) -> str:
    """``rid`` after the run's persisted :data:`RESERVATION_REPLACED_EVENT` hops (oldest
    first), so a reservation alloc_client re-admitted under a new id is still the
    run's after a gateway restart (Prism 43f676a1839e)."""
    try:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ? ORDER BY id",
            (task_id, run_id, RESERVATION_REPLACED_EVENT)).fetchall()
    except Exception as exc:
        _log.warning("kanban pool: reservation replacement lookup failed for %s run %s: %s", task_id, run_id, exc)
        return rid
    for (payload,) in rows:
        try:
            doc = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        to = doc.get("to") if isinstance(doc, dict) else None
        if doc.get("from") == rid and isinstance(to, str) and to:
            rid = to
    return rid


def record_replacement(conn, task_id: str, old: str, new: str) -> bool:
    """Persist alloc_client's ``old -> new`` re-admission on the card's CURRENT run
    (still running), so run_reservation / running_reservations follow it across a
    restart. False (logged) when the run is gone or the write fails."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    try:
        with _kbc.write_txn(conn):
            row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ? AND status = 'running'",
                               (task_id,)).fetchone()
            if not row or row[0] is None:
                return False
            _kb._append_event(conn, task_id, RESERVATION_REPLACED_EVENT, {"from": old, "to": new}, run_id=row[0])
        return True
    except Exception as exc:
        _log.warning("kanban pool: reservation replacement %s -> %s (%s) not recorded: %s", old, new, task_id, exc)
        return False


def reservation_for_run(conn, task_id: str, run_id: Optional[int]) -> Optional[str]:
    """The headroom reservation id the run's ``worker_placed`` event carries."""
    return run_reservation(conn, task_id, run_id)[0]


def release_run_reservation(rid: str, via: Optional[str]) -> bool:
    """End a run's reservation through whoever booked it."""
    return release_alloc(rid) if via == VIA_ALLOC_CLIENT else release_reservation(rid)


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


def running_reservations(conn) -> List[Tuple[str, str, Optional[str]]]:
    """``(task_id, reservation id, via)`` for every RUNNING card whose current run's
    ``worker_placed`` event carries a headroom reservation (``via`` as in
    :func:`run_reservation`)."""
    from hermes_cli import kanban_placement_reasons as _pr
    out: List[Tuple[str, str, Optional[str]]] = []
    for tid, run_id, payload in conn.execute(
            "SELECT t.id, e.run_id, e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE t.status = 'running' AND e.kind = ? AND e.run_id IS NOT NULL "
            "AND e.run_id = t.current_run_id", (_pr.WORKER_PLACED_EVENT,)).fetchall():
        try:
            doc = json.loads(payload or "{}")
            rid = doc.get(RESERVATION_KEY)
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(rid, str) and rid:
            if doc.get(RESERVATION_VIA_KEY) == VIA_ALLOC_CLIENT:
                out.append((tid, _replaced_rid(conn, tid, run_id, rid), VIA_ALLOC_CLIENT))
            else:
                out.append((tid, rid, None))
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
        for tid, rid, via in rows:
            try:
                if via == VIA_ALLOC_CLIENT:
                    try:
                        ok = _renew_alloc(rid)
                    finally:
                        # alloc_client._readmit aliases a LIVE replacement before it can still fail
                        # (renew_not_extended, a Refused out of _extend): persist it on every outcome
                        # (Prism 82b5db46c257), else a restart loses the row the run-end release needs.
                        new = _alloc_current(rid)
                        if new != rid:
                            _persist_replacement(connect, slug, tid, rid, new)
                else:
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
