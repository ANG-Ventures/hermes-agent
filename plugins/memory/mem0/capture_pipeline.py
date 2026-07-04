"""Capture pipeline orchestration for mem0 salient auto-capture (Track A-lite, A3 wiring).

Owns the queue + drain-worker lifecycle and the two guards the review demanded, so the change to
the plugin __init__ is minimal (compose, don't inline):
  - the DURABLE QUEUE (capture_queue.CaptureQueue) + the DRAIN WORKER (capture_drain.CaptureDrainWorker)
  - the SALIENCE GATE string + its VERSION (D-11 gate-version guard): capture PAUSES if the live gate
    version != the eval-certified version, so we never write through an uncertified gate
  - the CROSS-PROCESS bg-review INTERLOCK (D-7): a single resolver both the plugin and the bgr writer
    read at DECISION TIME, so both-ON is impossible across processes

sync_turn() becomes: if capture on AND gate certified -> enqueue (tiny, non-blocking) and let the
worker do the slow server-side extraction. Everything here is degrade-safe: any failure disables
capture (falls back to today's off state) rather than breaking a turn (INV-3).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")
_DEFAULT_QUEUE_PATH = "~/.hermes/state/mem0-capture/capture_queue.db"


def load_certified_gate() -> tuple[str, str]:
    """Return (gate_string, gate_version). Empty gate ('','') if assets missing -> capture stays off
    (fail-safe: no certified gate => don't auto-capture)."""
    gate_path = os.path.join(_ASSET_DIR, "capture_gate_v3.txt")
    ver_path = os.path.join(_ASSET_DIR, "gate_version.txt")
    try:
        gate = open(gate_path, encoding="utf-8").read()
        raw = open(ver_path, encoding="utf-8").read().strip()
        # gate_version.txt is "GATE_VERSION=v3:<hash>"
        version = raw.split("=", 1)[1] if "=" in raw else raw
        if gate.strip() and version:
            return gate, version
    except Exception as e:
        logger.warning("mem0 capture: certified gate assets not loadable (%s) — capture disabled", e)
    return "", ""


def bgr_write_allowed(capture_is_on: bool) -> bool:
    """Cross-process interlock (D-7): the bg-review mem0 writer calls this at DECISION TIME (each
    write attempt). If foreground auto-capture is ON, the bgr writer must NOT also write (both-ON
    impossible). Read fresh each call so a live capture flip is honored without a restart."""
    return not capture_is_on


class CapturePipeline:
    def __init__(
        self,
        *,
        capture_on_fn: Callable[[], bool],       # reads the LIVE capture state each call
        add_fn: Callable[[List[Dict[str, str]], Dict[str, Any]], Any],
        recall_idem_fn: Callable[[str], int],
        scrub_fn: Callable[[List[str]], "tuple[List[str], List[dict]]"],
        forget_fn: Optional[Callable[[str], None]],
        get_written_fn: Optional[Callable[[str], List[Dict[str, Any]]]],
        write_filters: Dict[str, Any],
        model: str,
        breaker_open_fn: Optional[Callable[[], bool]] = None,
        alert_fn: Optional[Callable[[str], None]] = None,
        queue_path: Optional[str] = None,
        expected_gate_version: Optional[str] = None,  # None => whatever the assets certify
    ):
        try:
            from .capture_queue import CaptureQueue
            from .capture_drain import CaptureDrainWorker
        except ImportError:  # flat import (unit tests run with PYTHONPATH=<dir>)
            from capture_queue import CaptureQueue
            from capture_drain import CaptureDrainWorker

        self._capture_on = capture_on_fn
        self._alert = alert_fn or (lambda m: logger.warning("mem0 capture alert: %s", m))
        self._gate, self._gate_version = load_certified_gate()
        self._expected_version = expected_gate_version or self._gate_version
        self._certified = bool(self._gate) and self._gate_version == self._expected_version
        if not self._certified:
            self._alert(
                f"mem0 auto-capture DISABLED: gate version mismatch/absent "
                f"(live={self._gate_version!r} expected={self._expected_version!r})")
        self._model = model
        qp = os.path.expanduser(queue_path or _DEFAULT_QUEUE_PATH)
        self._queue = CaptureQueue(qp)
        self._worker = CaptureDrainWorker(
            self._queue,
            add_fn=add_fn,
            recall_idem_fn=recall_idem_fn,
            scrub_fn=scrub_fn,
            forget_fn=forget_fn,
            get_written_fn=get_written_fn,
            gate=self._gate,
            model=model,
            write_filters=write_filters,
            breaker_open_fn=breaker_open_fn,
            alert_fn=self._alert,
        )
        self._started = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        """Capture is active only if it's on AND the gate is certified (D-11)."""
        return self._certified and self._capture_on()

    def start(self) -> None:
        with self._lock:
            if self._started or not self._certified:
                return
            self._worker.start()
            self._started = True
            logger.info("mem0 capture pipeline started (gate %s, model %s, queue depth %s)",
                        self._gate_version, self._model, self._queue.counts())

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self._worker.stop()
                self._started = False

    def enqueue_turn(self, user_content: str, assistant_content: str, *,
                     session_id: str = "", turn_ordinal: int = 0) -> bool:
        """The ONLY synchronous step (INV-3): a tiny durable INSERT. Returns True if enqueued.
        Degrade-safe: any failure is swallowed (never breaks the turn)."""
        if not self.active:
            return False
        try:
            try:
                from .capture_queue import idem_key
            except ImportError:
                from capture_queue import idem_key
            key = idem_key(session_id, turn_ordinal, user_content, assistant_content)
            enq = self._queue.enqueue(key, {"user": user_content, "assistant": assistant_content,
                                            "session_id": session_id})
            # Start the worker whenever capture is active and it isn't running yet — NOT only on a
            # brand-new insert (Greptile P1). After a restart with pending/expired rows already in
            # SQLite, a duplicate enqueue returns False; gating start on `enq` would leave those
            # durable rows (and the reaper) idle until some later unique turn. Reaching an active
            # enqueue means there is work to drain, so ensure the drain+reaper loop is up.
            if not self._started:
                self.start()
            return enq
        except Exception as e:
            logger.warning("mem0 capture enqueue failed (turn not captured, not broken): %s", e)
            return False

    def stats(self) -> Dict[str, Any]:
        out = {"certified": self._certified, "gate_version": self._gate_version,
               "queue": self._queue.counts()}
        out.update(self._worker.stats)
        return out
