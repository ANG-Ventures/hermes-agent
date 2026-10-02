"""Abstract base class for pluggable context engines.

A context engine decides when/how conversation context is compacted near the token
limit, tracks usage, and may expose tools. ContextCompressor is the default;
``context.engine`` selects a plugin (``plugins/context_engine/<name>/``); one is active.
Lifecycle: on_session_start() -> per API response update_from_response() -> per turn
should_compress() / compress() -> on_session_end() at real session boundaries only
(CLI exit, /reset, gateway expiry), never per-turn.
"""

import copy
import json
from abc import ABC, abstractmethod
import inspect
import logging
from typing import Any, Dict, List, Optional

from agent.compression_marker import elide_middle
from agent.redact import redact_sensitive_text


MEMORY_CONTEXT_MAX_CHARS = 6_000
_MEMORY_CONTEXT_HEAD_CHARS = 4_000
_MEMORY_CONTEXT_TAIL_CHARS = 1_500

# The one compaction phase whose cause is NOT inferable from the context
# percentage: the engine asked for maintenance while the context sat below the
# token threshold. Named once here so the resolver and its tests agree.
ENGINE_PREFLIGHT_MAINTENANCE_PHASE = "engine_preflight_maintenance"
_BELOW_THRESHOLD_ANNOUNCE_KEY = "announce_below_threshold_compaction"

# How far the provider's real prompt_tokens may exceed the local rough estimate
# before it is worth a warning. The skew calibration clamps its ratio to <= 1.0
# (never scale UP), so an under-counting estimate is otherwise recorded as a
# clean ratio=1.000 and leaves no trace. 1.15 keeps ordinary estimator noise
# quiet while catching the structural gaps (a measured session ran 1.38x).
_UNDERCOUNT_WARN_RATIO = 1.15

# Config key for allowing the skew calibration to scale an UNDER-counting
# estimate up toward provider truth (default on).
_SKEW_SCALE_UP_KEY = "skew_scale_up"

# Upper bound on the scale-up correction. The measured under-count range is
# 1.15-1.39x; 1.60 leaves headroom for a worse model/toolset mix while ensuring
# one anomalous (rough, real) pair can never drive a wildly premature
# compaction. The existing skew_floor guards the other direction.
_SKEW_SCALE_UP_MAX = 1.60

# Config key for the minimum number of paired (rough, real) readings a CONTENT
# CLASS must accumulate before its own ratio is trusted over the blended global
# one. See ``_per_class_min_samples`` for the justification of the default.
_SKEW_CLASS_MIN_SAMPLES_KEY = "skew_class_min_samples"
_SKEW_CLASS_MIN_SAMPLES_DEFAULT = 3


def _per_class_min_samples() -> int:
    """Minimum per-class readings before a class ratio overrides the global one.

    Why 3, and why it is a knob rather than a constant:

    The per-class ratio is a MEDIAN of at most ``_SKEW_HISTORY`` (5) readings.
    A median needs >= 3 samples to be a median at all — with 1 it is the single
    reading (no outlier rejection whatsoever, so one anomalous pair fully owns
    the correction) and with 2 it is a mean of two, which an outlier still drags
    half the distance. At 3 a single anomalous reading is out-voted by the other
    two and cannot move the median outside their range. That is the smallest
    sample size at which the smoothing the calibration already relies on
    actually functions, which is why it is the floor rather than a number picked
    for how it scores.

    It is deliberately NOT set higher: the calibration is per-conversation
    (``reset_skew_calibration`` clears it at every session boundary), so a floor
    of, say, 10 would mean most conversations never reach per-class calibration
    at all and the feature would be inert in exactly the sessions that matter.

    Operators who want the historical single-global-ratio behavior can set
    ``compression.skew_class_min_samples`` to a value no class will reach (or
    to ``0``/negative, which disables per-class correction outright); those who
    want more smoothing can raise it toward ``_SKEW_HISTORY``. Read defensively
    — a config failure must never change compaction behavior.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        if not isinstance(cfg, dict):
            return _SKEW_CLASS_MIN_SAMPLES_DEFAULT
        compression_cfg = cfg.get("compression")
        if not isinstance(compression_cfg, dict):
            return _SKEW_CLASS_MIN_SAMPLES_DEFAULT
        raw = compression_cfg.get(
            _SKEW_CLASS_MIN_SAMPLES_KEY, _SKEW_CLASS_MIN_SAMPLES_DEFAULT
        )
        value = int(raw)
        # A ceiling above the retained history would make the class arm
        # unreachable by construction; that is a legitimate way to disable it,
        # so it is allowed, but it must not be negative-indexed anywhere.
        return max(0, value)
    except Exception:
        logger.debug("per-class skew min-samples config read failed", exc_info=True)
        return _SKEW_CLASS_MIN_SAMPLES_DEFAULT


def _scale_up_calibration_enabled() -> bool:
    """Whether skew calibration may scale an under-counting estimate UP.

    Operator kill switch: ``compression.skew_scale_up: false`` in config.yaml
    restores the historical hard clamp at 1.0. Read defensively — a config
    failure must never change compaction behavior, so every error path returns
    the default (enabled).
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        if not isinstance(cfg, dict):
            return True
        compression_cfg = cfg.get("compression")
        if not isinstance(compression_cfg, dict):
            return True
        raw = compression_cfg.get(_SKEW_SCALE_UP_KEY, True)
        return str(raw).strip().lower() not in {"false", "0", "no", "off"}
    except Exception:
        logger.debug("skew scale-up config read failed", exc_info=True)
        return True


def _below_threshold_announce_enabled() -> bool:
    """Whether below-threshold compactions announce themselves (default True).

    Operator kill switch: ``compression.announce_below_threshold_compaction:
    false`` in config.yaml. Read defensively — a config failure must never
    suppress the explanation, so every error path defaults to announcing.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        if not isinstance(cfg, dict):
            return True
        compression_cfg = cfg.get("compression")
        if not isinstance(compression_cfg, dict):
            return True
        raw = compression_cfg.get(_BELOW_THRESHOLD_ANNOUNCE_KEY, True)
        return str(raw).strip().lower() not in {"false", "0", "no", "off"}
    except Exception:
        logger.debug("below-threshold announce config read failed", exc_info=True)
        return True


def classify_request(messages) -> "str | None":
    """Dominant content class of an outgoing request, or None. Never raises.

    Module-level (not a method) so every engine — including duck-typed test
    doubles and third-party plugins that do not subclass ContextEngine — gets
    the same behavior without needing to grow a member.
    """
    if not messages:
        return None
    try:
        from agent.content_class import dominant_content_class

        return dominant_content_class(messages)
    except Exception:
        # Classification is an optimization: any failure falls back to the
        # global calibration rather than perturbing the live turn.
        logger.debug("content classification failed", exc_info=True)
        return None


def class_skew_ratio(by_class, content_class: "str | None") -> "float | None":
    """Median skew for one content class, or ``None`` to defer to global.

    ``None`` is returned whenever the class arm must not be trusted: no class,
    no bucket, or fewer than ``compression.skew_class_min_samples`` readings in
    it. That is the clean fallback to the pre-existing global behavior — a class
    that has not been measured enough contributes nothing, it does not
    contribute a bad number.
    """
    if not content_class or not isinstance(by_class, dict):
        return None
    bucket = by_class.get(content_class)
    if not bucket:
        return None
    min_samples = _per_class_min_samples()
    # A floor of 0 disables the per-class arm entirely (documented operator
    # kill switch); it must not be read as "any single sample qualifies".
    if min_samples <= 0 or len(bucket) < min_samples:
        return None
    ordered = sorted(bucket)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def call_with_messages(fn, rough_tokens, messages):
    """Call a calibration entry point with ``messages``, tolerating old signatures.

    The per-content-class calibration needs the request being estimated so it
    can classify it. Third-party context-engine plugins ship their own
    ``note_rough_sent`` / ``calibrated_tokens`` / ``should_compress_calibrated``
    overrides that predate the parameter, so the host degrades to the
    single-argument call rather than breaking them — those engines keep the
    global-only behavior, which is exactly the documented fallback.

    Only a signature mismatch falls back; any other exception propagates, so a
    real bug inside the engine is not silently swallowed and retried.
    """
    if messages is None:
        return fn(rough_tokens)
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Un-introspectable callable (C function, some mocks): try the new
        # signature and fall back on the specific TypeError it would raise.
        try:
            return fn(rough_tokens, messages)
        except TypeError:
            return fn(rough_tokens)
    try:
        sig.bind(rough_tokens, messages)
    except TypeError:
        return fn(rough_tokens)
    return fn(rough_tokens, messages)


def _accepts_kwarg(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def should_compress_request(
    compressor, rough_tokens, messages, anchored_tokens=None
):
    """Host-side compaction trigger for a request estimate.

    ``rough_tokens`` is the rough (char-based) estimate; ``anchored_tokens`` is
    the usage-anchored real figure when the provider anchor is valid, else
    ``None``. The rough-estimator skew applies ONLY to rough input — an anchored
    figure is already in the provider's accounting and is compared unscaled.

    Engines whose ``should_compress_calibrated`` predates ``anchored_tokens``
    (third-party plugins) get the plain ``should_compress(anchored_tokens)`` on
    the anchored path — no skew, which is the correct semantics — and the
    pre-existing calibrated call otherwise.
    """
    gate = getattr(compressor, "should_compress_calibrated", None)
    if anchored_tokens is not None:
        if callable(gate) and _accepts_kwarg(gate, "anchored_tokens"):
            try:
                verdict = gate(
                    rough_tokens, messages, anchored_tokens=anchored_tokens
                )
            except TypeError:
                # Signature over-claims (``**kwargs`` wrapper/double) but the
                # implementation rejects the kwarg: treat as legacy.
                verdict = None
            if isinstance(verdict, bool):
                return verdict
        # Legacy engine (or a non-bool double): plain threshold on the real
        # figure, plus the skew-independent raw-rough hard-frac backstop.
        ctx_len = getattr(compressor, "context_length", 0)
        hard_frac = getattr(compressor, "_hard_frac", ContextEngine._HARD_FRAC_DEFAULT)
        ceiling = 0
        if (
            isinstance(ctx_len, (int, float))
            and isinstance(hard_frac, (int, float))
            and ctx_len > 0
        ):
            ceiling = int(ctx_len * hard_frac)
        if ceiling and rough_tokens >= ceiling:
            return compressor.should_compress(rough_tokens)
        return compressor.should_compress(anchored_tokens)
    if callable(gate):
        return call_with_messages(gate, rough_tokens, messages)
    return compressor.should_compress(rough_tokens)


def trigger_compare_tokens_for(
    compressor, rough_tokens, messages, anchored_tokens=None
) -> int:
    """The figure ``should_compress_request`` compared, for honest logging."""
    fn = getattr(compressor, "trigger_compare_tokens", None)
    if callable(fn) and _accepts_kwarg(fn, "anchored_tokens"):
        try:
            value = fn(rough_tokens, messages, anchored_tokens=anchored_tokens)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        except Exception:
            pass
    return int(anchored_tokens if anchored_tokens is not None else rough_tokens)


def sanitize_memory_context(memory_context: str) -> str:
    """Prepare provider context for a context-engine/LLM egress boundary."""
    sanitized = redact_sensitive_text(memory_context.strip(), force=True, redact_url_credentials=True)
    if len(sanitized) <= MEMORY_CONTEXT_MAX_CHARS:
        return sanitized
    return elide_middle(sanitized, _MEMORY_CONTEXT_HEAD_CHARS, _MEMORY_CONTEXT_TAIL_CHARS)


def automatic_compaction_status_message(engine: Any, *, phase: str, default_message: str, **context: Any) -> str | None:
    """Host-visible status for an automatic compaction event; ``None`` = emit nothing.

    Engines can suppress routine automatic status with
    ``emit_automatic_compaction_status = False`` or customize it by defining
    ``get_automatic_compaction_status_message(...)``. Empty strings and
    ``None`` mean "do not emit a lifecycle status".

    One phase overrides that opt-out: ``engine_preflight_maintenance``. Engines
    silence routine chatter because the user can infer the cause from the
    context percentage — but a compaction that fires while the context is BELOW
    the threshold has no such tell, so silencing it produces a compaction the
    user cannot explain. That phase is therefore always announced unless the
    operator explicitly opts out via
    ``compression.announce_below_threshold_compaction: false``.
    """
    if not getattr(engine, "emit_automatic_compaction_status", True):
        if not (
            phase == ENGINE_PREFLIGHT_MAINTENANCE_PHASE
            and _below_threshold_announce_enabled()
        ):
            return None

    formatter = getattr(engine, "get_automatic_compaction_status_message", None)
    if callable(formatter):
        message = formatter(
            phase=phase,
            default_message=default_message,
            **context,
        )
        # An engine that opts out of routine status returns None from the base
        # formatter regardless of phase. For the below-threshold arm the host's
        # default IS the message the user needs, so fall back to it rather than
        # letting the engine's blanket opt-out re-suppress what we just allowed.
        if message is None and phase == ENGINE_PREFLIGHT_MAINTENANCE_PHASE:
            message = default_message
    else:
        message = default_message

    if message is None:
        return None
    return str(message).strip() or None

logger = logging.getLogger(__name__)


class ContextEngine(ABC):
    """Base class all context engines must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'compressor', 'lcm')."""

    # Token state: engines MUST maintain these; run_agent.py reads them directly.
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0
    # Compaction parameters (read by run_agent.py for preflight). protect_first_n counts
    # non-system head messages kept verbatim IN ADDITION to the always-protected system
    # prompt (3 keeps the historical head shape).
    # These control the preflight compression check. Subclasses may override via __init__ or property;
    # defaults are sensible for most engines. See #13754.
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6
    # False keeps successful automatic compaction passes silent (routine background
    # maintenance); warnings, errors and manual /compress still surface.
    emit_automatic_compaction_status: bool = True

    # The fork's persistent in-chat compaction ANNOUNCE (the "🗜️ Context
    # compacted … ↩ recover with …" line) is a DIFFERENT rail from the transient
    # lifecycle status above: the status narrates work-in-progress, the announce
    # is the durable record telling the user what happened and how to recover
    # the elided turns. An engine that silences routine lifecycle chatter
    # usually wants the announce silenced too, so the default is to INHERIT
    # ``emit_automatic_compaction_status`` (``None`` = inherit). An engine whose
    # compaction is genuinely lossy-looking but recoverable (LCM: raw turns stay
    # in lcm.db, reachable via lcm_grep/lcm_expand) sets this True explicitly to
    # keep the recovery guidance while staying quiet about lifecycle phases.
    emit_automatic_compaction_announce: "bool | None" = None

    # -- Core interface ----------------------------------------------------

    @abstractmethod
    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Update tracked token usage after every LLM call.

        ``prompt_tokens``/``completion_tokens``/``total_tokens`` are always present; the
        canonical buckets (``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
        ``cache_write_tokens``, ``reasoning_tokens``) are optional on older hosts.
        """

    @abstractmethod
    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Return True if compaction should fire this turn."""

    def should_compress_info(self, prompt_tokens: int = None) -> "tuple[bool, str | None]":
        """Return ``(should_compress, reason)``.

        Engines with block reasons (summary-LLM cooldown, anti-thrashing guard) override
        this so callers can warn instead of silently skipping; the default keeps plugin
        engines from raising AttributeError.
        """
        return self.should_compress(prompt_tokens), None

    @abstractmethod
    def compress(
        self, messages: List[Dict[str, Any]], current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Compact ``messages`` into a valid OpenAI-format list that fits the budget.

        ``focus_topic`` comes from manual ``/compress <focus>`` (prioritise that topic);
        ``force`` asks to bypass an engine-owned cooldown; ``memory_context`` is provider
        text for the handoff prompt. Older engines may omit optional parameters — the
        host filters them by signature.
        """

    def prune_tool_results_only(
        self, messages: List[Dict[str, Any]], current_tokens: int | None = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Deterministically trim old tool-result payloads without an LLM call.

        Runs on a low, cost-oriented trigger independent of ``should_compress`` so
        large-window engines reclaim re-sent tool output long before full compaction.
        Returns ``(messages, n_pruned)``; the default no-op keeps older engines safe.
        """
        return messages, 0

    def select_context(
        self, request_messages: List[Dict[str, Any]], *, conversation_messages: List[Dict[str, Any]] = None,
        incoming_message: Dict[str, Any] = None, budget_tokens: int = 0,
    ) -> List[Dict[str, Any]]:
        """Optionally *select* (replace) the context for THIS request, pre-generation.

        Runs on every provider request (also retries), independent of
        ``should_compress()``: ``compress()`` shrinks over-long context, this swaps in a
        different one (retrieval, topic routing, branch switching). Return ``None`` to
        leave the request unchanged. The returned list is request-only — it MUST NOT be
        treated as persisted transcript state (session DB history is untouched); unlike
        ``pre_llm_call`` it may replace the list. The host runs it before prompt
        cache-control and every request sanitizer, so a malformed replacement never
        reaches the provider and the default no-op keeps the request byte-identical;
        an engine that replaces the list changes its own cache prefix (breakpoints are
        re-derived on the selected list). ``request_messages`` is the assembled request
        (system prompt + history + ephemeral prefill); ``conversation_messages`` is the
        persisted history for reference only (do not mutate); ``budget_tokens`` is the
        model's context length or 0 if unknown.
        """
        return None

    def on_turn_complete(self, messages: List[Dict[str, Any]], usage: Dict[str, Any] = None, **kwargs: Any) -> None:
        """Observe a finished turn (complement of ``select_context()``) to index/update
        routing state for the next request.

        Best-effort, not guaranteed: fires from the normal finalization seam only; some
        abnormal early returns (content-policy block, provider terminal failure) skip it.
        ``messages`` is a read-only shallow copy (return value ignored; never rely on
        transcript mutation). ``usage`` has the ``update_from_response`` shape and is
        ``None`` when no provider response was reached (interrupt). ``kwargs`` may include
        ``turn_id``, ``task_id``, ``api_call_count``, ``interrupted``, ``failed``, ``turn_exit_reason``.
        """
        return None

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        """Cheap rough check before the API call (no real token count yet); default skips."""
        return False

    # -- "Compact on the truth" calibration (P2) ---------------------------
    # Shared concrete implementation for ALL engines (ContextCompressor, LCMEngine,
    # third-party). The rough estimate (estimate_request_tokens_rough, char/3.5)
    # over-counts schema-heavy requests; the provider's real prompt_tokens measures
    # the skew. We scale the rough by the clamped median skew before comparing to
    # threshold, so compaction fires on the provider's real accounting, not the
    # ~21%-inflated guess. State is lazy (engines whose __init__ doesn't set it still
    # work). Pure functions of recorded scalars → no defer-baseline to ratchet.

    _SKEW_FLOOR_DEFAULT = 0.7
    _HARD_FRAC_DEFAULT = 0.95
    _SKEW_HISTORY = 5
    # Positive lower bound for the COLD-START trigger prior (``_trigger_skew`` on an
    # empty history). Guards against a misconfigured near-zero ``skew_floor`` (e.g.
    # 0.01) shrinking the calibrated estimate so far that the soft threshold becomes
    # unreachable and only the 95% hard-frac ceiling ever fires. 0.5 keeps the soft
    # threshold reachable while still deferring the raw-rough over-count false-fire.
    _TRIGGER_SKEW_MIN = 0.5

    def reset_skew_calibration(self) -> None:
        """Clear per-conversation skew state at a session boundary. The engine is a
        process-global singleton, so a skew learned in one conversation must not leak
        into the next session's first preflight (Greptile #111)."""
        self._recent_skews = []
        self._class_skews = {}
        self._last_rough_sent = 0
        self._last_rough_class = None
        self.rough_at_last_real = 0

    def seed_skew_calibration(self, ratios: "list[float]") -> None:
        """Seed the skew history from persisted per-session state (restart resume).

        Only applies when the in-memory history is empty (a live history is
        fresher than any persisted snapshot) and only accepts sane ratios.
        Invalid input is ignored — seeding is an optimization, never a
        correctness requirement.

        The accepted band MUST match what ``_current_skew`` is willing to apply,
        or persisted calibration is silently thrown away on every restart. When
        scale-up is enabled the recorder emits ratios ABOVE 1.0 (the estimate
        read LOW), so an upper bound of 1.0 here would discard exactly the
        readings that matter most — an under-count means threshold compaction
        fires LATE, toward provider overflow. Bound by the same
        ``_SKEW_SCALE_UP_MAX`` ceiling ``_current_skew`` clamps to, so a corrupt
        or absurd persisted value still can't be seeded.
        """
        if getattr(self, "_recent_skews", None):
            return
        upper = _SKEW_SCALE_UP_MAX if _scale_up_calibration_enabled() else 1.0
        clean = []
        for r in ratios or []:
            try:
                f = float(r)
            except (TypeError, ValueError):
                continue
            if 0.0 < f <= upper:
                clean.append(f)
        if clean:
            self._recent_skews = clean[-self._SKEW_HISTORY:]

    def skew_calibration_key(self) -> "tuple[str, str] | None":
        """The ``(provider, model)`` this engine's skew readings belong to.

        ``None`` when the model is unknown — a ratio with no model attached is
        unattributable, and guessing would be worse than staying uncalibrated.
        """
        model = (getattr(self, "model", "") or "").strip()
        if not model:
            return None
        provider = (getattr(self, "provider", "") or "").strip()
        return (provider, model)

    def bind_session_state(self, session_db: "Any" = None, session_id: str = "") -> None:
        """Hand the engine the session store its persistence layer needs.

        ``ContextCompressor`` overrides this with a much larger version that
        also rehydrates cooldowns and failure streaks. The ABC needs its own
        minimal default because ``agent_init`` binds via
        ``getattr(engine, "bind_session_state", None)``: an engine that does
        NOT define it (every plugin engine — LCM included) silently skips the
        bind, leaves ``_session_db`` unset, and makes
        ``_persist_skew_history()`` a permanent no-op.

        That is not hypothetical. Measured 2026-08-09 on the live tree: the
        calibration loop was running correctly (COMPACTION_SKEW lines with
        ratios of 1.33-1.41 and per-class labels), while
        ``compression_skew_calibration`` held ZERO rows — so every restart
        threw the learned ratio away and started from raw-rough 1.0, which is
        exactly the case the persistence was built for. Defining the default
        here fixes the whole class rather than one engine: any future engine
        inherits a working bind instead of failing silently the same way.

        Deliberately minimal and side-effect-free beyond the two attributes:
        seeding is the caller's business, and a plugin engine must not
        inherit ContextCompressor's cooldown rehydration.
        """
        self._session_db = session_db
        self._session_id = session_id or ""

    def _persist_skew_history(self) -> None:
        """Persist the current skew history (best-effort, never raises).

        Writes to BOTH stores, for two different jobs:

        * the ``(provider, model)`` calibration table — the DURABLE one. The
          skew ratio is a property of the model's tokenizer, so it is valid for
          every future session on that same pair and invalid for any other. A
          fresh session on a model we have measured before therefore starts
          calibrated instead of at raw-rough 1.0, which is precisely when it
          has no readings of its own.
        * the session row — kept for same-session restart resume, which is
          strictly narrower (it also survives a model switch mid-session, where
          the model-keyed row correctly would not apply).

        Fail-safe by contract: calibration is an optimization, so a failure in
        either writer must not prevent the other and must never touch the turn.
        """
        session_db = getattr(self, "_session_db", None)
        if not session_db:
            return
        history = list(getattr(self, "_recent_skews", []) or [])

        key = None
        try:
            key = self.skew_calibration_key()
        except Exception:
            logger.debug("skew calibration key resolution failed", exc_info=True)
        if key is not None:
            model_writer = getattr(session_db, "record_model_skew_history", None)
            if model_writer is not None:
                try:
                    model_writer(key[0], key[1], history)
                except Exception:
                    logger.debug("model-keyed skew persist failed", exc_info=True)

        session_id = getattr(self, "_session_id", "")
        if not session_id:
            return
        writer = getattr(session_db, "record_compression_skew_history", None)
        if writer is None:
            return
        try:
            writer(session_id, history)
        except Exception:
            logger.debug("session-keyed skew persist failed", exc_info=True)

    def seed_skew_calibration_for_model(self, session_db: "Any" = None) -> bool:
        """Seed from this engine's ``(provider, model)`` learned calibration.

        Returns True iff a persisted prior was found AND accepted. An UNSEEN
        pair returns False and leaves the history empty, so the engine starts
        uncalibrated (skew 1.0) rather than inheriting another model's
        tokenizer ratio. Honors the same live-history-wins and accept-band
        rules as ``seed_skew_calibration`` — it delegates to it.
        """
        if getattr(self, "_recent_skews", None):
            return False
        db = session_db if session_db is not None else getattr(self, "_session_db", None)
        if not db:
            return False
        getter = getattr(db, "get_model_skew_history", None)
        if getter is None:
            return False
        try:
            key = self.skew_calibration_key()
            if key is None:
                return False
            persisted = getter(key[0], key[1])
        except Exception:
            logger.debug("model-keyed skew seed read failed", exc_info=True)
            return False
        if not persisted:
            return False
        self.seed_skew_calibration(persisted)
        return bool(getattr(self, "_recent_skews", None))

    def note_rough_sent(
        self,
        rough_tokens: int,
        messages: "list | None" = None,
    ) -> None:
        """Stash the rough estimate of the request about to be sent so the next
        ``record_skew_from_real``/``update_from_response`` pairs it with the real
        prompt_tokens (the skew denominator). Same message set ⇒ correct ratio.

        When ``messages`` is supplied the request is also CLASSIFIED (see
        ``agent.content_class``) and the resulting ratio is filed under the
        dominant content class as well as the global history — so a tool-output
        heavy turn learns and applies the tool-class correction instead of one
        blended across content that skews differently. ``messages`` is optional
        so every existing caller (and every third-party engine host) keeps
        working unchanged; without it the calibration is exactly the pre-existing
        global-only behavior.
        """
        if rough_tokens and rough_tokens > 0:
            self._last_rough_sent = int(rough_tokens)
            self._last_rough_class = classify_request(messages)

    @staticmethod
    def _classify_request(messages: "list | None") -> "str | None":
        """Dominant content class of an outgoing request, or None. Never raises."""
        return classify_request(messages)

    def record_skew_from_real(self, real_prompt_tokens: int) -> None:
        """Pair a real provider ``prompt_tokens`` with the stashed rough (atomically,
        from the engine's ``update_from_response``). Records ratio ≤ 1.0 (rough
        over-counts; never scale UP), keeps the last-k for median smoothing.

        T0 (2026-06-27): the stashed rough is CONSUMED (reset to 0) after use, so a
        single ``note_rough_sent`` pairs with exactly ONE real reading. Without this,
        a multi-call turn (one preflight ``note_rough_sent`` + N ``update_from_response``)
        divided the SAME stale rough into N growing reals, polluting the skew median
        the trigger calibrates on. See spec
        ~/.hermes/plans/2026-06-27_skew-telemetry-and-render-harness-SPEC.md.
        """
        last_rough = getattr(self, "_last_rough_sent", 0)
        if real_prompt_tokens and real_prompt_tokens > 0 and last_rough > 0:
            self.rough_at_last_real = last_rough
            raw_ratio = real_prompt_tokens / last_rough
            allow_up = _scale_up_calibration_enabled()
            ratio = raw_ratio if allow_up else min(1.0, raw_ratio)
            # Historically this was hard-clamped to <= 1.0 ("rough over-counts;
            # never scale UP"). That holds only while the estimate over-counts.
            # Measured 2026-08-08 across live sessions it UNDER-counts by
            # 1.15-1.39x, and the clamp recorded every one of those as a clean
            # ratio=1.000 — so the threshold gate compared against a number well
            # below the real prompt and fired LATE, toward a provider overflow.
            # Scaling up is now opt-in via compression.skew_scale_up (default
            # ON): the correction is bounded by _SKEW_SCALE_UP_MAX so a single
            # anomalous pair cannot drive premature compaction.
            if allow_up and raw_ratio > 1.0:
                ratio = min(ratio, _SKEW_SCALE_UP_MAX)
            if raw_ratio > _UNDERCOUNT_WARN_RATIO:
                logger.warning(
                    "COMPACTION_ESTIMATE_UNDERCOUNT rough=%d real=%d raw_ratio=%.3f "
                    "(recorded=%.3f, scale_up=%s) — the local estimate reads "
                    "%.2fx LOW",
                    last_rough, int(real_prompt_tokens), raw_ratio, ratio,
                    "on" if allow_up else "off", raw_ratio,
                )
            hist = getattr(self, "_recent_skews", None)
            if hist is None:
                hist = []
                self._recent_skews = hist
            hist.append(ratio)
            if len(hist) > self._SKEW_HISTORY:
                self._recent_skews = hist[-self._SKEW_HISTORY:]
            # Per-class arm: file the SAME (already-bounded) ratio under the
            # dominant content class of the request it was measured on, so a
            # tool-output-heavy turn learns the tool-class rate instead of one
            # blended with prose. The global history above is unconditional —
            # per-class is strictly additive and the global remains the fallback
            # for any class below the sample floor.
            cls = getattr(self, "_last_rough_class", None)
            if cls:
                by_class = getattr(self, "_class_skews", None)
                if not isinstance(by_class, dict):
                    by_class = {}
                    self._class_skews = by_class
                bucket = by_class.setdefault(cls, [])
                bucket.append(ratio)
                if len(bucket) > self._SKEW_HISTORY:
                    by_class[cls] = bucket[-self._SKEW_HISTORY:]
            # T0: consume the stashed rough so the next real reading without a fresh
            # note_rough_sent records nothing (bounds cross-turn/session mispairing
            # to ≤1 on the process-global singleton). The class label is consumed
            # with it — a stale label must not tag the next turn's reading.
            self._last_rough_sent = 0
            self._last_rough_class = None
            # Persist the updated history so a process restart can seed the
            # calibration instead of reverting to skew=1.0 (raw rough) on the
            # first post-restart preflight (2026-07-10 false-fire incident).
            # Best-effort: persistence failure must never touch the live turn.
            try:
                self._persist_skew_history()
            except Exception:
                pass
            # T1: skew telemetry — one COMPACTION_SKEW line per FRESH pair, so a skew
            # distribution is buildable from logs. Best-effort: a logging failure or a
            # missing attribute must NEVER propagate into the live turn (INV-2).
            try:
                self._emit_skew_telemetry(
                    last_rough, int(real_prompt_tokens), ratio, cls
                )
            except TypeError:
                # An engine/test double overriding the pre-class 3-arg
                # signature must not break on the added label.
                self._emit_skew_telemetry(
                    last_rough, int(real_prompt_tokens), ratio
                )

    def _emit_skew_telemetry(
        self,
        rough: int,
        real: int,
        ratio: float,
        content_class: "str | None" = None,
    ) -> None:
        """Best-effort COMPACTION_SKEW telemetry (T1). Never raises into the hot path.

        Emits one ``info`` line per fresh (rough, real) pair and appends the same
        ``task=main`` line to a dedicated append-only sink for the v0.2 floor tune
        (the rotating gateway logs can rotate skew lines out before N accrues).
        """
        try:
            task = getattr(self, "_aux_task", None) or "main"
            model = getattr(self, "model", "") or ""
            provider = getattr(self, "provider", "") or ""
            ctx = getattr(self, "context_length", 0) or 0
            model_str = f"{provider}/{model}" if provider else model
            line = (
                f"COMPACTION_SKEW rough={rough} real={real} ratio={ratio:.3f} "
                f"task={task} model={model_str} ctx={ctx} "
                f"class={content_class or 'mixed'}"
            )
            logger.info(line)
            # Dedicated non-rotating sample sink (main-turn distribution only — the
            # task the trigger uses). Aux tasks don't reach here without their own
            # note_rough_sent (consumed), so this is naturally main-dominated.
            if task == "main":
                self._append_skew_sample(line)
        except Exception:
            # Telemetry must never break a live turn (INV-2).
            pass

    def _append_skew_sample(self, line: str) -> None:
        """Append one skew sample to ~/.hermes/state/skew-samples.log (append-only,
        non-rotating). Best-effort; any failure is swallowed by the caller's guard."""
        import os

        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.path.expanduser("~"), ".hermes"
        )
        # HERMES_HOME may already be a profile dir; the sink is per-process-home,
        # which is the correct scope for a per-process skew distribution.
        state_dir = os.path.join(home, "state")
        os.makedirs(state_dir, exist_ok=True)
        import time as _time

        stamp = _time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(os.path.join(state_dir, "skew-samples.log"), "a", encoding="utf-8-sig") as fh:
            fh.write(f"{stamp} {line}\n")

    def _resolve_content_class(
        self, messages: "list | None" = None
    ) -> "str | None":
        """Content class to calibrate THIS estimate against.

        Prefers an explicitly supplied message list (classified live). Falls
        back to the class stashed by the matching ``note_rough_sent`` for the
        same request, so a host that wires only the recording side still gets
        per-class reads. ``None`` ⇒ use the global blended calibration.
        """
        if messages:
            cls = classify_request(messages)
            if cls:
                return cls
        return getattr(self, "_last_rough_class", None)

    def _class_skew_ratio(self, content_class: "str | None") -> "float | None":
        """Median skew for this engine's ``content_class`` bucket, or None.

        Delegates to the module-level :func:`class_skew_ratio` so an engine
        (or test double) that never grew ``_class_skews`` degrades to the
        global calibration instead of raising.
        """
        return class_skew_ratio(getattr(self, "_class_skews", None), content_class)

    def _current_skew(self, content_class: "str | None" = None) -> float:
        """Median of the last-k real/rough ratios, clamped to [floor, ceiling].
        Returns 1.0 (no scaling = pre-P2 behavior) when no real reading has
        paired yet.

        When ``content_class`` is supplied AND that class has at least
        ``compression.skew_class_min_samples`` readings, the median is taken
        over that class's readings only — the estimator's error is a RATE error
        that differs by content (prose vs structured tool output vs media), so a
        tool-output-heavy request is corrected by the tool-class ratio instead
        of one blended across content that skews differently. Below the sample
        floor (or with no class) the global history is used exactly as before.

        The upper clamp is 1.0 when scale-up is disabled (historical behavior:
        the estimate is only ever corrected DOWN). When ``compression.
        skew_scale_up`` is on (default) it rises to ``_SKEW_SCALE_UP_MAX`` so a
        measured UNDER-count can actually reach the trigger — otherwise
        record_skew_from_real's un-clamped ratio is silently re-clamped here and
        the correction never takes effect. The per-class arm rides the SAME
        clamp band: a class median may correct an under-count upward, bounded by
        the same ceiling.
        """
        med = class_skew_ratio(getattr(self, "_class_skews", None), content_class)
        if med is None:
            hist = getattr(self, "_recent_skews", None)
            if not hist:
                return 1.0
            ordered = sorted(hist)
            mid = len(ordered) // 2
            med = (
                ordered[mid]
                if len(ordered) % 2
                else (ordered[mid - 1] + ordered[mid]) / 2.0
            )
        floor = getattr(self, "_skew_floor", self._SKEW_FLOOR_DEFAULT)
        ceiling = _SKEW_SCALE_UP_MAX if _scale_up_calibration_enabled() else 1.0
        return max(floor, min(ceiling, med))

    def _trigger_skew(self, content_class: "str | None" = None) -> float:
        """Skew used for the compaction TRIGGER decision only (never for display).

        Identical to ``_current_skew`` once at least one real reading has paired.
        On an EMPTY history it returns the conservative cold-start prior
        (``_skew_floor``) instead of 1.0, so the FIRST uncalibrated preflight on a
        large resumed/fresh session does not FALSE-FIRE a premature lossy
        compaction off the raw over-counting rough estimate (2026-07-18 incident:
        raw 316,953 at skew 1.000 >= 279,000 threshold fired while real usage was
        ~48% of the window; empirically the rough estimator's p10 skew is 0.67 and
        min 0.10 across 5,268 samples, so an uncalibrated estimate can over-count
        badly on dense/schema-heavy sessions).

        A class that has cleared the sample floor is itself a real reading, so
        it satisfies the cold-start condition even if the global history were
        somehow empty.

        This is trigger-ONLY: ``_current_skew`` deliberately stays identity (1.0)
        on empty history so the displayed/logged estimate remains an honest 'not
        yet measured' value (Greptile #111 display contract). Deferring here can
        never cause an overflow because ``should_compress_calibrated`` keeps the
        window hard-frac ceiling as a skew-independent 413 backstop.
        """
        hist = getattr(self, "_recent_skews", None)
        if hist or class_skew_ratio(
            getattr(self, "_class_skews", None), content_class
        ) is not None:
            return self._current_skew(content_class)
        floor = getattr(self, "_skew_floor", self._SKEW_FLOOR_DEFAULT)
        # Clamp defensively to a sane band; a misconfigured floor must not scale the
        # estimate UP (rough never under-counts) or so low it silently disables the
        # SOFT threshold path. A near-zero floor (e.g. 0.01) is not just the exact
        # 0.0 case Python truthiness would rescue — it would shrink the calibrated
        # estimate to ~1% of raw, so the threshold target (e.g. 75% of window) is
        # unreachable and NOTHING compacts until the hard-frac ceiling (95%) fires —
        # the very late-compaction hazard this cold-start prior exists to avoid. So
        # enforce a positive lower bound (``_TRIGGER_SKEW_MIN``): overflow is always
        # backstopped by the ceiling, but the soft threshold must stay reachable.
        try:
            floor = float(floor)
        except (TypeError, ValueError):
            floor = self._SKEW_FLOOR_DEFAULT
        return max(self._TRIGGER_SKEW_MIN, min(1.0, floor))

    def calibrated_tokens(
        self, rough_tokens: int, messages: "list | None" = None
    ) -> int:
        """``round(rough × skew)`` — the rough estimate scaled to the provider's
        measured accounting. Safe default (skew 1.0) ⇒ identical to raw rough.

        Uses the DISPLAY skew (``_current_skew``): this value is shown to the user
        in the preflight status line and logged, so it must stay identity until a
        real reading pairs. The TRIGGER decision applies the cold-start prior
        separately via ``_trigger_calibrated_tokens`` inside
        ``should_compress_calibrated``.

        ``messages`` (optional) is the request being estimated; when supplied it
        is classified so the correction applied is the one measured on this
        request's dominant content class."""
        if rough_tokens <= 0:
            return rough_tokens
        cls = self._resolve_content_class(messages)
        return int(round(rough_tokens * self._current_skew(cls)))

    def _trigger_calibrated_tokens(
        self, rough_tokens: int, messages: "list | None" = None
    ) -> int:
        """``round(rough × trigger_skew)`` — the trigger-decision calibration.
        Identical to ``calibrated_tokens`` once history exists; applies the
        cold-start prior on empty history."""
        if rough_tokens <= 0:
            return rough_tokens
        cls = self._resolve_content_class(messages)
        return int(round(rough_tokens * self._trigger_skew(cls)))

    def trigger_compare_tokens(
        self,
        rough_tokens: int,
        messages: "list | None" = None,
        *,
        anchored_tokens: "int | None" = None,
    ) -> int:
        """The token figure ``should_compress_calibrated`` compares to threshold.

        Single source for the trigger's compared value so a caller can log the
        number the gate actually tested (the printed inequality is then true by
        construction):

        * RAW rough at/over the window hard-frac ceiling -> raw rough
          (skew-independent 413 / dense-paste backstop, regardless of anchor);
        * ``anchored_tokens`` given -> that value UNSCALED. It is the provider's
          real prompt_tokens (+ completion + rough delta of messages appended
          since, see ``anchored_context_tokens``), already in the provider's
          accounting. Multiplying it by the rough-estimator skew double-scales
          it (2026-09-25: real 488,061 x 1.547 = 755,030 >= 750,000 fired the
          pre-API arm at 49% of the window, 11/11 fires below threshold);
        * otherwise -> ``rough x trigger_skew`` (the calibrated rough estimate).
        """
        ctx_len = getattr(self, "context_length", 0) or 0
        hard_frac = getattr(self, "_hard_frac", self._HARD_FRAC_DEFAULT)
        if ctx_len > 0 and rough_tokens >= int(ctx_len * hard_frac):
            return rough_tokens
        if anchored_tokens is not None:
            return int(anchored_tokens)
        return self._trigger_calibrated_tokens(rough_tokens, messages)

    def should_compress_calibrated(
        self,
        rough_tokens: int,
        messages: "list | None" = None,
        *,
        anchored_tokens: "int | None" = None,
    ) -> bool:
        """P2 trigger: compact when CALIBRATED rough ≥ threshold, OR when RAW rough
        reaches the window ceiling (skew-independent 413 / dense-paste guard — a
        dense in-turn paste raises raw rough so the ceiling fires even if a stale
        skew would defer). Delegates the actual threshold + anti-thrash to the
        engine's ``should_compress``.

        The calibrated compare uses the TRIGGER skew (cold-start prior on empty
        history) so a fresh/resumed large session does not false-fire; the window
        hard-frac ceiling below is skew-independent and remains the overflow
        backstop, so the cold-start deferral can never cause a 413.

        ``messages`` (optional) is classified so a tool-output-heavy request is
        compared against the tool-class correction rather than a blended one.

        ``anchored_tokens`` (optional) is a usage-anchored REAL figure; when
        given, the soft threshold compares it unscaled (see
        ``trigger_compare_tokens``) while the raw-rough hard-frac backstop still
        applies to ``rough_tokens``."""
        return self.should_compress(
            self.trigger_compare_tokens(
                rough_tokens, messages, anchored_tokens=anchored_tokens
            )
        )

    def get_automatic_compaction_status_message(
        self, *, phase: str, default_message: str, **context: Any,
    ) -> str | None:
        """User-visible status for automatic compaction, or ``None`` to suppress it.

        ``phase`` is the host call site (``"preflight"`` / ``"compress"``); ``context``
        carries best-effort ``approx_tokens`` / ``threshold_tokens``. Warnings, errors
        and manual ``/compress`` are not governed by this hook.
        """
        return default_message if self.emit_automatic_compaction_status else None

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        """Preflight guard for gateway ``/compress``: False reports "nothing to
        compress yet" without an LLM call (e.g. transcript entirely protected)."""
        return True

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Session begins: load persisted state. kwargs may include hermes_home, platform, model."""

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Real session boundary (CLI exit, /reset, gateway expiry) — never per-turn."""

    def on_session_reset(self) -> None:
        """/new or /reset: reset per-session state (default: counters and token tracking)."""
        # Reset cross-call calibration state captured under the PREVIOUS model. These fields encode "the
        # provider proved this prompt fit" / "preflight can be deferred" decisions that are only valid for
        # the model that produced them. Carrying them across a switch to a smaller-context model would let
        # should_defer_preflight_to_real_usage() suppress a preflight compression the new model actually
        # needs — the exact oversized-send-after-switch failure in #23767. The new model's first response
        # repopulates them via update_from_response(). Setting last_prompt_tokens to 0 (NOT -1) is
        # deliberate: 0 is the documented "no real usage yet -> use the rough estimate" state, so the post-
        # response should_compress path falls back to estimate_request_tokens_rough rather than skipping
        # compression. -1 is a different sentinel (#36718, "compression just ran, await real usage") and
        # must not be set here.
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas this engine provides to the agent.

        Engines may return bare OpenAI function schemas or full
        {"type": "function", "function": ...} tool definitions; the host
        normalizes both. LCM returns schemas for lcm_grep, lcm_describe,
        lcm_expand here.
        """
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a call to one of this engine's tools; must return a JSON string.
        kwargs may include ``messages`` (live in-memory list)."""
        return json.dumps({"error": f"Unknown context engine tool: {name}"})

    def get_status(self) -> Dict[str, Any]:
        """Status dict with the standard fields run_agent.py expects."""
        # Clamp the -1 "compression just ran, awaiting real usage" sentinel to 0 so no
        # reader sees a negative usage_percent on the transitional turn.
        last_prompt = max(self.last_prompt_tokens, 0)
        return {
            "last_prompt_tokens": last_prompt,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": min(100, last_prompt / self.context_length * 100) if self.context_length else 0,
            "compression_count": self.compression_count,
        }

    def clone_for_agent(self) -> "ContextEngine":
        """Per-agent instance of a plugin-registered engine (the plugin system holds ONE shared
        instance; every AIAgent gets its own so a child's update_model() cannot mutate the parent's).
        Override when the engine holds uncopyable state (locks, DB connections): return a fresh
        engine sharing the durable backend and copying only mutable budget state."""
        return copy.deepcopy(self)

    def update_model(
        self, model: str, context_length: int, base_url: str = "", api_key: str = "",
        provider: str = "", api_mode: str = "",
    ) -> None:
        """Model switch / fallback: recompute threshold_tokens (override for more).

        Per-model threshold override (longest substring match), else the raw config
        percent — snapshotted ONCE so repeated switches fall back to the configured
        value, not the previous model's override.
        """
        self.context_length = context_length
        from agent.context_compressor import resolve_model_threshold
        if not hasattr(self, "_config_threshold_percent"):
            self._config_threshold_percent = self.threshold_percent
        self._base_threshold_percent = resolve_model_threshold(
            model, getattr(self, "model_thresholds", {}), self._config_threshold_percent, provider)
        self.threshold_percent = self._base_threshold_percent
        self.threshold_tokens = int(context_length * self.threshold_percent)
