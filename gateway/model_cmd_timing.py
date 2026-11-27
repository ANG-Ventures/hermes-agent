"""Per-stage wall time of one typed ``/model`` (``/m``) command: one INFO line per command (t_7fe48f0b).

An idle-session ``/m opus-d`` took ~79 s on 2026-11-09 while ``switch_model()`` alone took 0.09 s warm
and 3.6 s cold; nothing said where the rest went. The handler marks each stage boundary with
:func:`phase` (one line per boundary, so the long handler body is not re-indented); the time from one
boundary to the next belongs to the earlier phase; ``confirm`` includes adapter delivery::

    PHASE=model_cmd session=<key> path=background outcome=ok save=ok lock_wait_s=0.000 config_s=0.002 ...
        record_s=4.210 record_get_session_s=0.020 record_update_model_s=4.100 ... reply_s=0.090 total_s=4.300

The reply goes out before the save (t_2ab07928), so the line is logged when the save settles, not
at delivery: ``reply_s`` is the time to the confirmation, ``total_s`` the time to the settled save,
``save`` its outcome (ok / failed / interrupted / not_run, ``-`` when nothing was switched).
Phases are exclusive and sum to ``total_s - other_s`` (``other_s`` = time outside any phase: before
the first one and between delivery and the first save stage). ``record_s`` is derived: the sum of
the ``record_*`` phases, one per DB/store call. ``durable_*`` fields are nested spans inside
``record_durable`` (config write, override clear, override write-through) and are not added again.
A phase the command never reached prints ``-``.
``path`` names how the command reached the handler (t_2bf22375): ``background`` (the adapter's own
session task), ``inline_bypass`` (a busy session, dispatched inline), ``callback`` (a picker or
confirm callback that renders its own reply) or ``direct`` (any other caller).
Telemetry only: every entry point swallows its own errors, so it can never change the reply.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from typing import Dict, Iterator, Optional

logger = logging.getLogger("gateway.run")  # same record stream as the /model handler

# Exclusive phases, in handler order (gateway/slash_commands.py ``_handle_model_command_locked``).
PHASES = ("lock_wait", "config", "normalize", "prepare", "listing", "resolve", "enrich", "guard",
          "fallback_clear", "agent_switch", "record_get_session", "record_update_model", "record_override",
          "announce", "record_durable", "confirm")
RECORD_PHASES = ("record_get_session", "record_update_model", "record_override", "record_durable")
# Nested spans inside ``record_durable`` (_commit_model_switch_durable).
DURABLE_SPANS = ("durable_config", "durable_clear_override", "durable_set_override")

_current: contextvars.ContextVar[Optional["ModelCmdTiming"]] = contextvars.ContextVar(
    "model_cmd_timing", default=None)


class ModelCmdTiming:
    def __init__(self) -> None:
        self.session = "-"
        self.path = "-"
        self.t0 = time.monotonic()
        self.phase: Optional[str] = None
        self.phase_t0 = self.t0
        self.phases: Dict[str, float] = {}
        self.spans: Dict[str, float] = {}
        self.finished = False
        self.save_pending = False
        self.reply_s: Optional[float] = None
        self.delivery_outcome: Optional[str] = None
        self.save_outcome: Optional[str] = None

    def enter(self, name: Optional[str], now: float) -> None:
        if self.phase is not None:
            self.phases[self.phase] = self.phases.get(self.phase, 0.0) + max(now - self.phase_t0, 0.0)
        self.phase, self.phase_t0 = name, now

    def line(self, outcome: str, total_s: float) -> str:
        def _fmt(name: str, table: Dict[str, float]) -> str:
            value = table.get(name)
            return f"{name}_s=" + ("-" if value is None else f"{value:.3f}")

        fields = [f"PHASE=model_cmd session={self.session} path={self.path} outcome={outcome} save={self.save_outcome or '-'}"]
        fields += [_fmt(name, self.phases) for name in PHASES]
        reached = [self.phases[n] for n in RECORD_PHASES if n in self.phases]
        fields.append("record_s=" + (f"{sum(reached):.3f}" if reached else "-"))
        fields += [_fmt(name, self.spans) for name in DURABLE_SPANS]
        other = total_s - sum(self.phases.values())
        fields.append("reply_s=" + ("-" if self.reply_s is None else f"{self.reply_s:.3f}"))
        fields.append(f"other_s={max(other, 0.0):.3f} total_s={total_s:.3f}")
        return " ".join(fields)


def begin() -> Optional[ModelCmdTiming]:
    """Start the timer for this command in phase ``lock_wait``. Never raises."""
    try:
        timing = ModelCmdTiming()
        timing.enter("lock_wait", timing.t0)
        _current.set(timing)
        return timing
    except Exception:
        return None


def phase(name: str) -> None:
    """Close the open phase and open *name* (no-op outside a timed command). Never raises."""
    try:
        timing = _current.get()
        if timing is not None and not timing.finished:
            timing.enter(name, time.monotonic())
    except Exception:
        pass


def finish(timing: Optional[ModelCmdTiming], outcome: str = "ok") -> None:
    """Close the open phase and log the line (no-op for None, once per timer). Never raises."""
    if timing is None:
        return
    try:
        if timing.finished:
            return
        now = time.monotonic()
        timing.enter(None, now)
        timing.finished = True
        logger.info("%s", timing.line(outcome, now - timing.t0))
    except Exception:
        pass


def annotate(*, session: Optional[str] = None, path: Optional[str] = None) -> None:
    """Name the session key / dispatch path on the active timer. Never raises."""
    try:
        timing = _current.get()
        if timing is not None and not timing.finished:
            if session:
                timing.session = str(session)
            if path:
                timing.path = str(path)
    except Exception:
        pass


@contextlib.contextmanager
def span(name: str) -> Iterator[None]:
    """Record the wall time of the ``with`` body (awaits included) as nested span *name*. Exceptions
    from the body propagate unchanged; the timing itself never raises."""
    try:
        t0 = time.monotonic()
    except Exception:
        t0 = None
    try:
        yield
    finally:
        try:
            timing = _current.get()
            if t0 is not None and timing is not None and not timing.finished:
                timing.spans[name] = timing.spans.get(name, 0.0) + max(time.monotonic() - t0, 0.0)
        except Exception:
            pass


def current() -> Optional[ModelCmdTiming]:
    """The timer of the /model command running in this context, if any. Never raises."""
    try:
        return _current.get()
    except Exception:
        return None


def bind(timing: Optional[ModelCmdTiming]) -> None:
    """Make *timing* current in this context (the save may run in another task). Never raises."""
    try:
        if timing is not None:
            _current.set(timing)
    except Exception:
        pass


def mark_save_pending(timing: Optional[ModelCmdTiming]) -> None:
    """The command switched the model and its save is still to run: the line waits for it."""
    try:
        if timing is not None:
            timing.save_pending = True
    except Exception:
        pass


def reply_delivered(timing: Optional[ModelCmdTiming]) -> None:
    """Stamp ``reply_s`` (once) and close the ``confirm`` phase. Never raises."""
    try:
        if timing is None or timing.finished or timing.reply_s is not None:
            return
        now = time.monotonic()
        timing.reply_s = max(now - timing.t0, 0.0)
        timing.enter(None, now)
    except Exception:
        pass


def save_finished(timing: Optional[ModelCmdTiming], outcome: str) -> None:
    """The save settled: log the one line with the delivery outcome (if any). Never raises."""
    try:
        if timing is None:
            return
        timing.save_outcome = outcome
        timing.save_pending = False
        if timing.reply_s is not None:  # else the delivery (or handler return) logs it
            finish(timing, timing.delivery_outcome or "ok")
    except Exception:
        pass


def handler_finished(event, timing, exc: Optional[BaseException]) -> None:
    """Keep the command clock open through the adapter's owned delivery callback, and through
    the save when the command switched the model.

    Direct callers have no adapter delivery and finish at handler return (or when the save
    settles). The event carries the timer rather than a task-local lookup: routed handlers may
    run in another context.
    """
    try:
        if exc is not None:
            finish(timing, outcome_for(exc))
            return
        if not getattr(event, "_model_cmd_delivery", None):
            if timing is not None and not timing.save_pending:
                reply_delivered(timing)  # a direct caller gets the reply now
                finish(timing, "ok")
            return
        event._model_cmd_timing = timing
        adapter, key = event._model_cmd_delivery
        adapter.register_post_delivery_callback(key, lambda: delivery_finished(event))
    except Exception:
        finish(timing, outcome_for(exc))


def delivery_finished(event, outcome: str = "ok") -> None:
    """Record the delivery outcome (first call wins); log now unless a save is still to run.
    Idempotent across the ordinary callback and exceptional adapter cleanup."""
    try:
        timing = getattr(event, "_model_cmd_timing", None)
        if timing is None or timing.finished:
            return
        if timing.delivery_outcome is None:
            timing.delivery_outcome = outcome
        reply_delivered(timing)
        if not timing.save_pending:
            finish(timing, timing.delivery_outcome)
    except Exception:
        pass


def outcome_for(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "ok"
    if type(exc).__name__ == "CancelledError":
        return "cancelled"
    return "error"
