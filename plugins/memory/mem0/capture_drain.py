"""Drain worker for mem0 salient auto-capture (Track A-lite).

Runs PLUGIN-SIDE (in the Hermes gateway process, alongside the mem0 plugin — the plugin reaches the
store over HTTP, so the queue + worker live here, not in the mem0 container). A single background
loop that pulls queued turns and does the slow/failable work off the critical path:

    lease a due row  ->  client.add(messages, prompt=GATE, model=MODEL)   [server-side extraction+gate]
                     ->  record model_verdict (D-10)
                     ->  reconcile by capture_idem: if rows already exist, mark done (exactly-once, D-8)
                     ->  else scrub the just-written facts (INV-4 defense-in-depth) — A-lite has no
                         server redaction seam yet, so the drainer scrubs post-write and FORGETS any
                         row that carries a secret
                     ->  mark done  |  on transient failure: record fault + mark_retry (bounded, D-10)

INV-1 (no silent loss): a lease that dies mid-flight is recovered by the queue reaper; a model fault
requeues with backoff up to MAX then dead-letters + alerts. INV-3 (never blocks the turn): this runs
in its own thread; sync_turn only does the tiny enqueue.

The extraction+gate happen SERVER-SIDE (mem0 runs ADDITIVE_EXTRACTION_PROMPT + the gate as
custom_instructions) — same as mem0 cloud. The worker is the client-side reliability wrapper only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class CaptureDrainWorker:
    def __init__(
        self,
        queue,                                   # CaptureQueue
        *,
        add_fn: Callable[[List[Dict[str, str]], Dict[str, Any]], Any],
        recall_idem_fn: Callable[[str], int],    # -> count of existing rows with this capture_idem
        scrub_fn: Callable[[List[str]], "tuple[List[str], List[dict]]"],
        forget_fn: Optional[Callable[[str], None]] = None,   # forget a secret-bearing memory by id
        get_written_fn: Optional[Callable[[str], List[Dict[str, Any]]]] = None,  # rows for a capture_idem
        gate: str = "",
        model: str = "",
        write_filters: Optional[Dict[str, Any]] = None,
        poll_interval_s: float = 2.0,
        lease_s: float = 120.0,
        backoff_base_s: float = 30.0,
        max_attempts: int = 5,
        breaker_open_fn: Optional[Callable[[], bool]] = None,
        alert_fn: Optional[Callable[[str], None]] = None,
    ):
        self._q = queue
        self._add = add_fn
        self._recall_idem = recall_idem_fn
        self._scrub = scrub_fn
        self._forget = forget_fn
        self._get_written = get_written_fn
        self._gate = gate
        self._model = model
        self._write_filters = dict(write_filters or {})
        self._poll = poll_interval_s
        self._lease_s = lease_s
        self._backoff = backoff_base_s
        self._max_attempts = max_attempts
        self._breaker_open = breaker_open_fn or (lambda: False)
        self._alert = alert_fn or (lambda m: logger.error("mem0 capture alert: %s", m))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # observability counters (read by the digest). scrub_dead = a secret MAY be live in the store.
        self.stats = {"drained": 0, "dead": 0, "retried": 0, "reaped": 0, "scrubbed": 0, "scrub_dead": 0}

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="mem0-capture-drain")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # ---- the loop ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.drain_once()
            except Exception as e:  # a loop iteration must never kill the worker
                logger.warning("capture drain iteration error: %s", e)
                worked = False
            # reaper sweep each idle pass (cheap)
            try:
                r = self._q.reap(backoff_s=self._backoff, max_attempts=self._max_attempts)
                self.stats["reaped"] += r.get("requeued_env", 0) + r.get("requeued_fault", 0)
                self.stats["dead"] += r.get("dead", 0)
            except Exception as e:
                logger.debug("reaper error: %s", e)
            if not worked:
                self._stop.wait(self._poll)

    def drain_once(self) -> bool:
        """Process at most one due row. Returns True if a row was handled."""
        if self._breaker_open():
            return False
        row = self._q.lease_one(lease_s=self._lease_s)
        if row is None:
            return False
        key = row["idem_key"]
        payload = row["payload"]
        messages = [
            {"role": "user", "content": payload.get("user", "")},
            {"role": "assistant", "content": payload.get("assistant", "")},
        ]
        # EXACTLY-ONCE (D-8): if the add already ran on a prior lease (crash before mark_done),
        # rows exist -> mark done WITHOUT re-adding.
        # FAIL-CLOSED (Greptile P1): if the idem check itself FAILS (transient search fault), we
        # cannot tell "new" from "already-written". Re-adding on an unknown = duplicate rows the
        # SQLite queue can't prevent. So on an idem-check error, REQUEUE (bounded) instead of adding.
        try:
            if self._recall_idem(key) > 0:
                # rows already exist. Still run the scrub before completing — a prior lease may have
                # added but crashed/failed BEFORE the scrub ran, so the shortcut must not skip it.
                if self._scrub_written_or_requeue(key, row):
                    return True
                self._q.mark_done(key)
                self.stats["drained"] += 1
                return True
        except Exception as e:
            attempts = int(row.get("attempts", 0)) + 1
            status = self._q.mark_retry(key, backoff_s=self._backoff * (2 ** (attempts - 1)),
                                        error=f"idem-check-failed: {str(e)[:280]}",
                                        max_attempts=self._max_attempts)
            if status == "dead":
                self.stats["dead"] += 1
                logger.error("capture idem-check unresolved after %d attempts; dead-lettered "
                             "(did NOT re-add to avoid duplicates): %s", attempts, e)
            else:
                self.stats["retried"] += 1
                logger.warning("capture idem pre-check failed; requeued (fail-closed, no re-add): %s", e)
            return True

        # SERVER-SIDE extraction + gate. Stamp capture_idem so the reconcile can find the rows.
        kwargs = dict(self._write_filters)
        md = dict(kwargs.get("metadata") or {})
        md["capture_idem"] = key
        kwargs["metadata"] = md
        if self._gate:
            kwargs["prompt"] = self._gate
        if self._model:
            kwargs["model"] = self._model
        try:
            self._add(messages, kwargs)
            self._q.record_verdict(key, "ok")
        except Exception as e:
            # transient model/network fault -> requeue with backoff (bounded), or dead-letter
            self._q.record_verdict(key, "fault")
            attempts = int(row.get("attempts", 0)) + 1
            status = self._q.mark_retry(key, backoff_s=self._backoff * (2 ** (attempts - 1)),
                                        error=str(e)[:300], max_attempts=self._max_attempts)
            if status == "dead":
                self.stats["dead"] += 1
                logger.warning("capture turn dead-lettered after %d attempts: %s", attempts, e)
            else:
                self.stats["retried"] += 1
            return True

        # POST-WRITE SCRUB (INV-4). See _scrub_written_or_requeue — fail-closed: never complete a row
        # whose scrub boundary we could not prove clean.
        if self._scrub_written_or_requeue(key, row):
            return True

        self._q.mark_done(key)
        self.stats["drained"] += 1
        return True

    def _scrub_written_or_requeue(self, key, row) -> bool:
        """Deterministically scrub the rows written for `key` and FORGET any secret-bearing one.
        The salience gate is NOT a reliable secret boundary (it leaked a bot token in the eval), so
        this is defense-in-depth (INV-4). FAIL-CLOSED (Greptile P1): if the rows can't be READ or a
        FORGET fails, requeue (bounded) instead of completing — a secret must never be left
        recallable behind a done row. The scrub is idempotent (scanning a clean row is a no-op), so
        it's safe to re-run on the exactly-once shortcut path too.

        Returns True if the row was requeued/dead-lettered (caller must stop); False if clean.
        """
        if not (self._get_written and self._forget):
            return False
        try:
            written = self._get_written(key)   # [{id, memory}] — may raise on transient search fault
            for r in written:
                txt = r.get("memory", "") or ""
                _, dropped = self._scrub([txt])
                if dropped:
                    self._forget(r.get("id", ""))   # may raise; requeue below if so
                    self.stats["scrubbed"] += 1
                    logger.warning("capture scrubbed a secret-bearing memory (reason=%s)",
                                   dropped[0].get("reason"))
            return False
        except Exception as e:
            attempts = int(row.get("attempts", 0)) + 1
            status = self._q.mark_retry(key, backoff_s=self._backoff * (2 ** (attempts - 1)),
                                        error=f"scrub-failed: {str(e)[:280]}",
                                        max_attempts=self._max_attempts)
            if status == "dead":
                self.stats["dead"] += 1
                # A scrub dead-letter is NOT a normal fault: a secret-bearing memory may still be
                # LIVE and recallable in the store, and the queue will stop retrying. Escalate LOUDLY
                # (Greptile P1) so an operator can scrub it out of band; also record it distinctly.
                self.stats["scrub_dead"] += 1
                self._alert(
                    f"SECRET SCRUB DEAD-LETTERED for capture row {key!r} after {attempts} attempts — "
                    f"a secret-bearing memory may remain recallable in mem0; manual scrub required: {e}")
                logger.error("capture SCRUB dead-lettered after %d attempts (secret may be live — "
                             "escalated): %s", attempts, e)
            else:
                self.stats["retried"] += 1
                logger.warning("capture post-write scrub failed; requeued to retry the scrub: %s", e)
            return True
