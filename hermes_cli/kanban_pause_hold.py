"""Fleet pause hold: a card of a paused host spawns nowhere (fleet pause/resume SPEC v0.3 §5.8, t_133d6efd).

The manifest ``<kanban_home()>/state/fleet-pause/<host>/manifest.json`` is written by ``scripts/fleet-pause``
(hermes-home). ONE predicate, :func:`decide`, evaluated in two layers:

* layer 1, :func:`gate_rows`: a post-route pass in ``_dispatch_once_locked`` after every route is known and
  before any spawn (ready lane before ``_browser_gate``, review lane after ``_browser_hold_review_rows``), so the
  plan branch, the no-plan branch and every way a row became ready pass through it;
* layer 2, :func:`refuse_if_held`: the first statement of the two run openers ``_claim_and_open_run`` and
  ``_open_review_run``, the only functions that turn a ready/review row into a running row + ``task_runs`` row.
  A held card raises :class:`PauseHeld` before the CAS; the caller's ``write_txn`` rolls back.

Path root is ``kanban_home()``, never a profile ``HERMES_HOME``. A missing manifest = no pause; an unreadable one
holds every card of that host (fail closed). Kill switch ``kanban.placement.pause_hold: on|off`` (default on),
read by both layers through :func:`enabled`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

CODE = "pause_hold"
REFUSED_EVENT = "claim_refused_pause_hold"
UNREADABLE = "unreadable"
HOLD_PHASES = frozenset({"draining", "paused", "paused-forced", "paused-partial"})
PIN_PHASES = frozenset({"resuming", "resumed"})
STATUS_FILE = "hold-status.json"
_UNREADABLE = object()


class PauseHeld(Exception):
    """A run opener refused a card whose origin host is paused."""

    def __init__(self, host: str, manifest_id: str, task_id: str = ""):
        super().__init__(f"held by fleet-pause {manifest_id} (host {host})")
        self.host, self.manifest_id, self.task_id = host, manifest_id, task_id

    @property
    def reason(self) -> str:
        return f"{CODE} ({self.host}:{self.manifest_id} stage=claim)"


def root() -> Path:
    from hermes_cli import kanban_db as _kb
    return _kb.kanban_home() / "state" / "fleet-pause"


def enabled() -> bool:
    """``kanban.placement.pause_hold`` from the ROOT config; anything but an explicit off is on."""
    try:
        from hermes_cli import kanban_db as _kb
        placement = (_kb._board_home_kanban_cfg() or {}).get("placement") or {}
        value = placement.get("pause_hold", "on") if isinstance(placement, dict) else "on"
    except Exception:
        _log.warning("kanban pause_hold: root config unreadable; hold stays on", exc_info=True)
        return True
    return value is not False and str(value).strip().lower() not in ("off", "false", "0", "no")


def _load(host: str, cache: dict):
    if host in cache:
        return cache[host]
    path = root() / host / "manifest.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        cache[host] = None
        return None
    except OSError:
        cache[host] = _UNREADABLE
        return _UNREADABLE
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or not isinstance(doc.get("phase"), str) or not doc.get("host"):
            raise ValueError("manifest lacks host/phase")
    except ValueError:
        _log.warning("kanban pause_hold: %s unreadable; every %s card is HELD", path, host)
        doc = _UNREADABLE
    cache[host] = doc
    return doc


def decide(origin_host: Optional[str], task_id: str, cache: dict):
    """None | ("wait", "pause_hold (<host>:<id>)") | ("pin", host). Pure over the manifest contents."""
    if not origin_host:
        return None
    doc = _load(str(origin_host), cache)
    if doc is None:
        return None
    if doc is _UNREADABLE:
        return ("wait", f"{CODE} ({origin_host}:{UNREADABLE})")
    phase = doc.get("phase")
    if phase in HOLD_PHASES:
        return ("wait", f"{CODE} ({origin_host}:{doc.get('id') or '?'})")
    if phase in PIN_PHASES:
        for unit in doc.get("units") or ():
            if isinstance(unit, dict) and unit.get("lane") == "kanban" and unit.get("id") == task_id:
                if ((unit.get("resume") or {}).get("new_run_id")) is None:
                    return ("pin", str(origin_host))
                return None
    return None


def _write_status(mode: str, now: float) -> None:
    from hermes_cli import kanban_db_dispatch as _kbd
    try:
        interval = float((_kbd._root_kanban_cfg() or {}).get("dispatch_interval_seconds", 60) or 60)
    except (TypeError, ValueError):
        interval = 60.0
    path = root() / STATUS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{STATUS_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"mode": mode, "pid": os.getpid(), "updated_at": int(now),
                                   "interval_s": interval}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _log.warning("kanban pause_hold: could not write %s", path, exc_info=True)


def gate_rows(conn, rows, result, row_route: dict, dry_run: bool, cache: dict, *, lane: str,
              planned: bool = True, entry: str = "gateway"):
    """Layer 1. Ready lane: writes ``row_route`` and returns ``rows``. Review lane: records the wait in
    ``result.placement_waits`` and returns the rows that may proceed.

    ``hold-status.json`` is published on the ready lane only by a live GATEWAY tick (RC-P3-2: a one-shot
    ``hermes kanban dispatch`` runs the same code from disk before the gateway restarts and must not vouch for it).
    """
    on = enabled()
    if lane == "ready" and not dry_run and entry == "gateway":
        _write_status("on" if on else "off", time.time())
    if not on or not rows:
        return rows
    kept = []
    for r in rows:
        try:
            origin = r["origin_host"]
        except (IndexError, KeyError):
            origin = None
        verdict = decide(origin, r["id"], cache)
        if verdict is None:
            kept.append(r)
            continue
        if verdict[0] == "wait":
            if lane == "review":
                result.placement_waits[r["id"]] = verdict[1]
                continue
            row_route[r["id"]] = verdict
            kept.append(r)
            continue
        kept.append(r)
        if lane != "ready":
            continue
        current = row_route.get(r["id"])
        if current and current[0] in ("pin", "origin", "wait"):
            continue  # never widen an existing pin, never touch another gate's wait
        # RC-P3-1: with no pool plan a pin route reaches spillover.take(None); wait like a body pin does (I-11).
        row_route[r["id"]] = verdict if planned else ("wait", "pool_unavailable")
    return kept if lane == "review" else rows


def refuse_if_held(conn, task_id: str) -> None:
    """Layer 2: raise :class:`PauseHeld` for a card whose origin host is in a hold phase. Caller holds the txn;
    the raise rolls it back, so nothing is written here (RC-P3-3: the caller records the refusal)."""
    if not enabled():
        return
    row = conn.execute("SELECT origin_host FROM tasks WHERE id = ?", (task_id,)).fetchone()
    origin = row[0] if row is not None else None
    if not origin:
        return
    verdict = decide(origin, task_id, {})
    if verdict is not None and verdict[0] == "wait":
        manifest_id = verdict[1].split(":", 1)[1].rstrip(")")
        raise PauseHeld(str(origin), manifest_id, task_id)


def record_refusal(conn, exc: PauseHeld, *, stage: str = "claim") -> None:
    """The ``claim_refused_pause_hold`` event, in its OWN txn after the opener's txn rolled back. Best-effort."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    try:
        with _kbc.write_txn(conn):
            _kb._append_event(conn, exc.task_id, REFUSED_EVENT,
                              {"host": exc.host, "manifest_id": exc.manifest_id, "stage": stage})
    except Exception:
        _log.warning("kanban pause_hold: could not record the refusal for %s", exc.task_id, exc_info=True)
