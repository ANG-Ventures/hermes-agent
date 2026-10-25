"""Consumption receipt for async-delegation wakes (t_97a879af).

Adapter admission only QUEUES (or steers) a completion behind a busy session, so the JSON outbox
event sits ``admitted`` until a turn consumes it. On 2026-10-24 deleg_3b87f5fe's batch-complete was
acked ``delivered`` at admission and then lost in memory at the interrupt-depth cap; nothing could
tell. This sweep, run at boot and every ``SWEEP_INTERVAL_S`` by the async-delegation watcher:

* confirms ``delivered`` once the transcript carries the wake (its generation-specific ``Event:
  <event_id>`` line in a user row since admission, in the parent's compression chain or in any
  session of its routing key open or ended since);
* leaves it alone while the wake is still parked in memory or the session is running a turn, and
  skips (never re-injects) a candidate whose transcript cannot be read;
* otherwise, after two consecutive misses and ``MIN_AGE_S``, re-opens it ``pending`` and re-queues
  it ONCE through the normal delivery path (``REINJECT_CAP`` per event, then ``dropped``).

At-least-once: a turn that persisted its user row between the transcript read and the re-open can
see the wake twice. It is never lost silently.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Iterable, List, Tuple

logger = logging.getLogger("gateway.run")

SWEEP_INTERVAL_S = 60.0
MIN_AGE_S = 120.0
REINJECT_CAP = 2


def _profile_homes(runner: Any) -> List[Path]:
    from hermes_constants import get_hermes_home
    homes = {str(get_hermes_home()): get_hermes_home()}
    for home in (getattr(runner, "_served_profile_homes", None) or {}).values():
        homes.setdefault(str(home), Path(home))
    return list(homes.values())


def _load_admitted(homes: Iterable[Path]) -> List[Tuple[Path, dict]]:
    from tools.async_delegation import admitted_outbox_events
    out: List[Tuple[Path, dict]] = []
    for home in homes:
        try:
            out += [(home, event) for event in admitted_outbox_events(home)]
        except Exception:
            logger.debug("admitted-completion sweep: registry read failed for %s", home, exc_info=True)
    return out


def _in_transcript(home: Path, event: dict) -> bool:
    """True when a user row since admission carries this wake; raises when it cannot be read."""
    from tools.async_delegation import admission_receipt_needle
    from hermes_state import SessionDB
    payload = event.get("payload") or {}
    needle = admission_receipt_needle(payload)
    db_path = home / "state.db"
    if not needle:
        return False
    if not db_path.exists():
        # Unverifiable is not "lost": the caller skips this candidate (Prism a54f28d4d21a).
        raise FileNotFoundError(f"transcript store missing: {db_path}")
    db = SessionDB(db_path, read_only=True)
    try:
        session_id = str(event.get("admitted_session_id") or payload.get("parent_session_id") or "")
        chain = db.get_compression_chain(session_id) if session_id else []
        return db.user_message_contains(
            chain, needle, since=float(event.get("admitted_at") or 0.0) - 5.0,
            session_key=str(event.get("admitted_session_key") or payload.get("session_key") or ""))
    finally:
        db.close()


def _parked_in_memory(runner: Any, session_key: str, needle: str) -> bool:
    """The wake is still queued somewhere this process will drain, or its session is mid-turn."""
    if not session_key:
        return False
    if runner._is_session_running(session_key):
        return True
    state = runner._peek_session_state(session_key)
    queued = list(getattr(getattr(state, "conversation", None), "queued_events", None) or [])
    queued += list(getattr(getattr(state, "conversation", None), "held_queue", None) or [])
    maps = (getattr(runner, "adapters", {}) or {}, *(getattr(runner, "_profile_adapters", {}) or {}).values())
    for adapter in {id(a): a for m in maps for a in m.values() if a is not None}.values():
        slot = getattr(adapter, "_pending_messages", None)
        if isinstance(slot, dict) and slot.get(session_key) is not None:
            queued.append(slot[session_key])
        if session_key in (getattr(adapter, "_active_sessions", None) or {}):
            return True
    # Terminated like the transcript probe (newline, CR, space or end): ``Event: X:g1`` must not match
    # ``Event: X:g10``, and a batch wake's ``Event: X   Total duration: Ns`` line must match.
    return any(_needle_terminated_in(getattr(event, "text", "") or "", needle) for event in queued)


def _needle_terminated_in(text: str, needle: str) -> bool:
    return f"{needle}\n" in f"{text}\n" or f"{needle} " in text or f"{needle}\r" in text


async def sweep_admitted_completions(runner: Any, *, now: float | None = None) -> int:
    """One sweep; returns the number of wakes re-queued."""
    from tools import async_delegation as ad
    from tools.process_registry import process_registry
    now = time.time() if now is None else now
    suspects: set = runner.__dict__.setdefault("_admitted_sweep_suspects", set())
    started = float(getattr(runner, "_gateway_started_at", 0.0) or 0.0)
    homes = _profile_homes(runner)
    candidates = await asyncio.to_thread(_load_admitted, homes)
    # Event ids are only unique per profile registry: every suspect is keyed by (home, event_id).
    suspects.intersection_update({(str(home), event.get("event_id")) for home, event in candidates})
    requeued = 0
    for home, event in candidates:
        try:
            requeued += await _sweep_one(runner, ad, process_registry, suspects, started, now, home, event)
        except Exception:
            # One unreadable profile/transcript must not starve the rest; unknown is not "lost".
            logger.warning("admitted-completion sweep: skipped %s in %s", event.get("event_id"), home,
                           exc_info=True)
    return requeued


async def _drop_past_ceiling(ad, home, event, now) -> bool:
    """Admitted past ``ADMITTED_MAX_AGE_SECONDS`` and still unconfirmed (state.db gone, transcript
    never carried it): drop it as lost instead of holding it to the 30-day retention (t_506536db)."""
    from tools.async_delegation_store import admitted_past_ceiling
    if not admitted_past_ceiling(event, now):
        return False
    event_id = str(event.get("event_id") or "")
    await asyncio.to_thread(ad.drop_admitted_event, event_id, "admitted_age_ceiling", home)
    logger.warning("async_delegation_wake_lost event_id=%s session_key=%s age_hours=%.1f: admitted, never "
                   "confirmed; dropped at the age ceiling", event_id, event.get("admitted_session_key") or "-",
                   (now - float(event["admitted_at"])) / 3600.0)
    return True


async def _sweep_one(runner, ad, process_registry, suspects, started, now, home, event) -> int:
    event_id = str(event.get("event_id") or "")
    suspect = (str(home), event_id)
    payload = event.get("payload") or {}
    try:
        consumed = await asyncio.to_thread(_in_transcript, home, event)
    except Exception:
        if not await _drop_past_ceiling(ad, home, event, now):
            raise  # unknown is not "lost" until the age ceiling says so
        suspects.discard(suspect)
        return 0
    if consumed:
        await asyncio.to_thread(ad.confirm_admitted_event, event_id, home)
        suspects.discard(suspect)
        return 0
    if await _drop_past_ceiling(ad, home, event, now):
        suspects.discard(suspect)
        return 0
    admitted_at = float(event.get("admitted_at") or 0.0)
    if now - max(admitted_at, started) < MIN_AGE_S:
        return 0
    key = str(event.get("admitted_session_key") or payload.get("session_key") or "")
    if _parked_in_memory(runner, key, ad.admission_receipt_needle(payload)):
        suspects.discard(suspect)
        return 0
    if suspect not in suspects:
        suspects.add(suspect)  # one more look before acting: a drain may be mid-flight
        return 0
    suspects.discard(suspect)
    if int(event.get("reinjects") or 0) >= REINJECT_CAP:
        await asyncio.to_thread(ad.drop_admitted_event, event_id, "reinject_cap", home)
        logger.warning("async_delegation_wake_lost event_id=%s session_key=%s reinjects=%s: giving up",
                       event_id, key, event.get("reinjects"))
        return 0
    reopened = await asyncio.to_thread(
        ad.reopen_admitted_event, event_id, home, reason="admitted_not_in_transcript")
    if reopened is None:
        return 0
    identity = runner._completion_delivery_identity(reopened)
    if identity is not None:
        with runner._completion_delivery_lock:
            runner._completion_deliveries_delivered.pop(identity, None)
            runner._completion_deliveries_inflight.discard(identity)
    logger.warning(
        "async_delegation_wake_reinjected event_id=%s session_key=%s admitted_at=%.0f: admitted but never "
        "reached the transcript", event_id, key, admitted_at)
    process_registry.completion_queue.put(reopened)
    return 1
