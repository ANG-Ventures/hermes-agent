"""Abstract base class for pluggable context engines.

A context engine controls how conversation context is managed when
approaching the model's token limit. The built-in ContextCompressor
is the default implementation. Third-party engines (e.g. LCM) can
replace it via the plugin system or by being placed in the
``plugins/context_engine/<name>/`` directory.

Selection is config-driven: ``context.engine`` in config.yaml.
Default is ``"compressor"`` (the built-in). Only one engine is active.

The engine is responsible for:
  - Deciding when compaction should fire
  - Performing compaction (summarization, DAG construction, etc.)
  - Optionally exposing tools the agent can call (e.g. lcm_grep)
  - Tracking token usage from API responses

Lifecycle:
  1. Engine is instantiated and registered (plugin register() or default)
  2. on_session_start() called when a conversation begins
  3. update_from_response() called after each API response with usage data
  4. should_compress() checked after each turn
  5. compress() called when should_compress() returns True
  6. on_session_end() called at real session boundaries (CLI exit, /reset,
     gateway session expiry) — NOT per-turn
"""

from abc import ABC, abstractmethod
import logging
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text


MEMORY_CONTEXT_MAX_CHARS = 6_000
_MEMORY_CONTEXT_HEAD_CHARS = 4_000
_MEMORY_CONTEXT_TAIL_CHARS = 1_500
_MEMORY_CONTEXT_TRUNCATION_MARKER = "\n...[memory provider context truncated]...\n"

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


def sanitize_memory_context(memory_context: str) -> str:
    """Prepare provider context for a context-engine/LLM egress boundary."""
    sanitized = redact_sensitive_text(
        memory_context.strip(),
        force=True,
        redact_url_credentials=True,
    )
    if len(sanitized) <= MEMORY_CONTEXT_MAX_CHARS:
        return sanitized
    return (
        sanitized[:_MEMORY_CONTEXT_HEAD_CHARS]
        + _MEMORY_CONTEXT_TRUNCATION_MARKER
        + sanitized[-_MEMORY_CONTEXT_TAIL_CHARS:]
    )


def automatic_compaction_status_message(
    engine: Any,
    *,
    phase: str,
    default_message: str,
    **context: Any,
) -> str | None:
    """Resolve host-visible status for an automatic compaction event.

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
    message = str(message).strip()
    return message or None

logger = logging.getLogger(__name__)


class ContextEngine(ABC):
    """Base class all context engines must implement."""

    # -- Identity ----------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'compressor', 'lcm')."""

    # -- Token state (read by run_agent.py for display/logging) ------------
    #
    # Engines MUST maintain these. run_agent.py reads them directly.

    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0

    # -- Compaction parameters (read by run_agent.py for preflight) --------
    #
    # These control the preflight compression check.  Subclasses may
    # override via __init__ or property; defaults are sensible for most
    # engines.
    #
    # protect_first_n semantics (since PR #13754): count of non-system head
    # messages always preserved verbatim, IN ADDITION to the system prompt
    # which is always implicitly protected.  Default 3 keeps the
    # historical "system + first 3 non-system messages" head shape.

    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6

    # User-visible lifecycle status for automatic host-triggered compaction.
    # Alternative engines that treat compaction as routine background
    # maintenance can set this false to keep successful automatic passes silent;
    # warnings, errors, and explicit manual commands should still surface.
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
        """Update tracked token usage from an API response.

        Called after every LLM call with a normalized usage dict. The legacy
        keys ``prompt_tokens``, ``completion_tokens``, and ``total_tokens``
        are always present. Newer hosts also include canonical buckets:
        ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
        ``cache_write_tokens``, and ``reasoning_tokens``. Engines should
        treat those fields as optional for compatibility with older hosts.
        """

    @abstractmethod
    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Return True if compaction should fire this turn."""

    def should_compress_info(self, prompt_tokens: int = None) -> "tuple[bool, str | None]":
        """Return ``(should_compress, reason)``.

        The base implementation is backward-compatible: engines that only
        implement ``should_compress`` get ``(should_compress(prompt_tokens),
        None)``. Concrete engines with richer block reasons (e.g. a
        summary-LLM cooldown or an anti-thrashing guard) override this to
        surface a human-readable reason so callers can warn the user instead
        of silently skipping compression. Added for the silent-overflow
        warning fix (#62625) so plugin engines don't raise AttributeError.
        """
        return self.should_compress(prompt_tokens), None

    @abstractmethod
    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        """Compact the message list and return the new message list.

        This is the main entry point. The engine receives the full message
        list and returns a (possibly shorter) list that fits within the
        context budget. The implementation is free to summarize, build a
        DAG, or do anything else — as long as the returned list is a valid
        OpenAI-format message sequence.

        Args:
            focus_topic: Optional topic string from manual ``/compress <focus>``.
                Engines that support guided compression should prioritise
                preserving information related to this topic.  Engines that
                don't support it may simply ignore this argument.
            force: Whether a user-requested compression should bypass an
                engine-owned cooldown. Engines without cooldowns may ignore it.
            memory_context: Text returned by memory providers immediately before
                compaction. Summarizing engines should include non-empty text in
                their handoff prompt. Older engines may omit this parameter; the
                host filters unsupported optional arguments by signature.
        """

    # -- Optional: proactive tool-result prune -----------------------------

    def prune_tool_results_only(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int | None = None,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Deterministically trim old tool-result payloads without an LLM call.

        Runs on a low, cost-oriented trigger independent of ``should_compress``
        so large-window engines can reclaim re-sent tool output long before full
        compaction would fire. Returns ``(messages, n_pruned)``.

        Default is a safe no-op: the list is returned unchanged with ``0``
        pruned. Engines that don't implement a cheap prune — and any engine that
        predates this hook — inherit this default, so the agent loop's
        post-tool-call prune path never raises ``AttributeError`` on them. The
        built-in ContextCompressor overrides this with the real implementation.
        """
        return messages, 0

    # -- Optional: per-turn context selection (distinct from compression) --

    def select_context(
        self,
        request_messages: List[Dict[str, Any]],
        *,
        conversation_messages: List[Dict[str, Any]] = None,
        incoming_message: Dict[str, Any] = None,
        budget_tokens: int = 0,
    ) -> List[Dict[str, Any]]:
        """Optionally choose/replace the context for THIS request, pre-generation.

        Called every turn after the request message list is assembled and
        before it is dispatched to the provider — independent of
        ``should_compress()``. This lets an engine *select* which context
        enters the prompt (retrieval, topic routing, role/branch switching)
        rather than *shrink* context that is already there. The two verbs are
        orthogonal:

          - ``compress()``      : context is too long  -> make it shorter.
          - ``select_context()``: this turn belongs to a different context
                                  -> use that one instead.

        Without this hook, engines that need per-turn access to the message
        list have to force ``should_compress()`` to return ``True`` so that
        ``compress()`` is invoked every turn purely as a callback — which
        conflates selection with compression and degrades behaviour when the
        engine's backend is unavailable. ``select_context()`` removes the need
        for that workaround.

        The returned list is request-only: it replaces the messages sent to
        the provider for this single call and MUST NOT be treated as persisted
        transcript state. The conversation history in the session DB is left
        untouched, so nothing leaks across turns. Return ``None`` to leave the
        request unchanged.

        Unlike the ``pre_llm_call`` plugin hook (which appends to the user
        message and intentionally never rewrites the list, to preserve the
        cache prefix), ``select_context()`` may *replace* the message list.

        Ordering / cache contract: the host runs this hook **before** prompt
        cache-control and **before** every request sanitizer (orphaned-tool
        cleanup, thinking-only/role normalization, whitespace/JSON
        normalization). So (a) whatever the hook returns still passes through
        the same validation as any request — a malformed replacement cannot
        reach the provider — and (b) prompt-cache stability (an AGENTS.md
        invariant) is preserved: the default no-op leaves the request
        byte-identical, so cache behaviour is unchanged for the built-in
        compressor and any non-implementing engine. An engine that *does*
        replace the list changes its own cache prefix by definition; that is
        the engine's concern, and cache-control breakpoints are re-derived on
        the selected list. The hook is evaluated per provider request (so it
        re-runs on retries within a turn), consistent with "select the context
        for THIS request".

        Args:
            request_messages: The assembled request message list (system
                prompt + history + any ephemeral prefill), in OpenAI format.
            conversation_messages: The unmodified persisted conversation
                history, for reference only (do not mutate).
            incoming_message: The current turn's user message, if available.
            budget_tokens: The active model's context length, or 0 if unknown.

        Default returns ``None`` (no-op) — zero impact on the built-in
        compressor or any existing engine.
        """
        return None

    def on_turn_complete(
        self,
        messages: List[Dict[str, Any]],
        usage: Dict[str, Any] = None,
        **kwargs: Any,
    ) -> None:
        """Observe a finished user turn (post-turn ingestion / observation).

        Called from the standard turn-finalization path once the assistant/tool
        loop completes, with the finalized in-memory transcript snapshot. This
        is the complement to ``select_context()``: selection happens *before*
        the request, while observation happens *after* the turn. It lets an
        engine ingest, index, summarize, or update routing / topic / session
        state from what actually happened — so the next ``select_context()``
        can act on it.

        Coverage: this fires from the normal finalization seam. Some abnormal
        early-return paths in the loop (e.g. a content-policy block or a
        provider terminal failure) persist and return without routing through
        finalization, and therefore do not currently emit this hook. Treat it
        as a best-effort post-turn observation for completed turns, not a
        guaranteed callback for every possible early exit; unifying all
        terminal paths behind one finalization seam is a separate follow-up.

        Together the two hooks remove the need to abuse ``should_compress()`` /
        ``compress()`` as a generic per-turn callback just to observe history,
        and they cover the case where a turn finishes and there may be no next
        request from which to infer the previous turn.

        ``messages`` is a shallow copy and should be treated as read-only:
        return values are ignored and this hook must not rely on transcript
        mutation for persistence. ``kwargs`` may include ``turn_id``,
        ``task_id``, ``api_call_count``, ``interrupted``, ``failed``, and
        ``turn_exit_reason``.

        ``usage`` carries the completed turn's canonical token usage (the same
        dict shape passed to ``update_from_response`` — ``prompt_tokens`` /
        ``completion_tokens`` / ``total_tokens`` plus the canonical
        ``input_tokens`` / ``output_tokens`` / ``cache_read_tokens`` /
        ``cache_write_tokens`` / ``reasoning_tokens`` buckets) so an engine can
        weigh how large/expensive the selected context actually was when
        deciding the next ``select_context()``. It is ``None`` on finalized
        turns that never reached a provider response (e.g. interrupt); engines
        must treat it as optional.

        Default is a no-op.
        """
        return None

    # -- Optional: pre-flight check ----------------------------------------

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        """Quick rough check before the API call (no real token count yet).

        Default returns False (skip pre-flight). Override if your engine
        can do a cheap estimate.
        """
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
        self._last_rough_sent = 0
        self.rough_at_last_real = 0

    def seed_skew_calibration(self, ratios: "list[float]") -> None:
        """Seed the skew history from persisted per-session state (restart resume).

        Only applies when the in-memory history is empty (a live history is
        fresher than any persisted snapshot) and only accepts sane ratios
        (0 < r <= 1.0; rough never under-counts). Invalid input is ignored —
        seeding is an optimization, never a correctness requirement.
        """
        if getattr(self, "_recent_skews", None):
            return
        clean = []
        for r in ratios or []:
            try:
                f = float(r)
            except (TypeError, ValueError):
                continue
            if 0.0 < f <= 1.0:
                clean.append(f)
        if clean:
            self._recent_skews = clean[-self._SKEW_HISTORY:]

    def _persist_skew_history(self) -> None:
        """Write the current skew history to the bound session row (best-effort).

        Uses the same session binding as the durable compression-failure
        cooldown (``bind_session_state``). No binding → silently skip.
        """
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        if not session_db or not session_id:
            return
        writer = getattr(session_db, "record_compression_skew_history", None)
        if writer is None:
            return
        writer(session_id, list(getattr(self, "_recent_skews", []) or []))

    def note_rough_sent(self, rough_tokens: int) -> None:
        """Stash the rough estimate of the request about to be sent so the next
        ``record_skew_from_real``/``update_from_response`` pairs it with the real
        prompt_tokens (the skew denominator). Same message set ⇒ correct ratio."""
        if rough_tokens and rough_tokens > 0:
            self._last_rough_sent = int(rough_tokens)

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
            # T0: consume the stashed rough so the next real reading without a fresh
            # note_rough_sent records nothing (bounds cross-turn/session mispairing
            # to ≤1 on the process-global singleton).
            self._last_rough_sent = 0
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
            self._emit_skew_telemetry(last_rough, int(real_prompt_tokens), ratio)

    def _emit_skew_telemetry(self, rough: int, real: int, ratio: float) -> None:
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
                f"task={task} model={model_str} ctx={ctx}"
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
        with open(os.path.join(state_dir, "skew-samples.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")

    def _current_skew(self) -> float:
        """Median of the last-k real/rough ratios, clamped to [floor, ceiling].
        Returns 1.0 (no scaling = pre-P2 behavior) when no real reading has
        paired yet.

        The upper clamp is 1.0 when scale-up is disabled (historical behavior:
        the estimate is only ever corrected DOWN). When ``compression.
        skew_scale_up`` is on (default) it rises to ``_SKEW_SCALE_UP_MAX`` so a
        measured UNDER-count can actually reach the trigger — otherwise
        record_skew_from_real's un-clamped ratio is silently re-clamped here and
        the correction never takes effect.
        """
        hist = getattr(self, "_recent_skews", None)
        if not hist:
            return 1.0
        ordered = sorted(hist)
        mid = len(ordered) // 2
        med = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
        floor = getattr(self, "_skew_floor", self._SKEW_FLOOR_DEFAULT)
        ceiling = _SKEW_SCALE_UP_MAX if _scale_up_calibration_enabled() else 1.0
        return max(floor, min(ceiling, med))

    def _trigger_skew(self) -> float:
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

        This is trigger-ONLY: ``_current_skew`` deliberately stays identity (1.0)
        on empty history so the displayed/logged estimate remains an honest 'not
        yet measured' value (Greptile #111 display contract). Deferring here can
        never cause an overflow because ``should_compress_calibrated`` keeps the
        window hard-frac ceiling as a skew-independent 413 backstop.
        """
        hist = getattr(self, "_recent_skews", None)
        if hist:
            return self._current_skew()
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

    def calibrated_tokens(self, rough_tokens: int) -> int:
        """``round(rough × skew)`` — the rough estimate scaled to the provider's
        measured accounting. Safe default (skew 1.0) ⇒ identical to raw rough.

        Uses the DISPLAY skew (``_current_skew``): this value is shown to the user
        in the preflight status line and logged, so it must stay identity until a
        real reading pairs. The TRIGGER decision applies the cold-start prior
        separately via ``_trigger_calibrated_tokens`` inside
        ``should_compress_calibrated``."""
        if rough_tokens <= 0:
            return rough_tokens
        return int(round(rough_tokens * self._current_skew()))

    def _trigger_calibrated_tokens(self, rough_tokens: int) -> int:
        """``round(rough × trigger_skew)`` — the trigger-decision calibration.
        Identical to ``calibrated_tokens`` once history exists; applies the
        cold-start prior on empty history."""
        if rough_tokens <= 0:
            return rough_tokens
        return int(round(rough_tokens * self._trigger_skew()))

    def should_compress_calibrated(self, rough_tokens: int) -> bool:
        """P2 trigger: compact when CALIBRATED rough ≥ threshold, OR when RAW rough
        reaches the window ceiling (skew-independent 413 / dense-paste guard — a
        dense in-turn paste raises raw rough so the ceiling fires even if a stale
        skew would defer). Delegates the actual threshold + anti-thrash to the
        engine's ``should_compress``.

        The calibrated compare uses the TRIGGER skew (cold-start prior on empty
        history) so a fresh/resumed large session does not false-fire; the window
        hard-frac ceiling below is skew-independent and remains the overflow
        backstop, so the cold-start deferral can never cause a 413."""
        ctx_len = getattr(self, "context_length", 0) or 0
        hard_frac = getattr(self, "_hard_frac", self._HARD_FRAC_DEFAULT)
        if ctx_len > 0 and rough_tokens >= int(ctx_len * hard_frac):
            return self.should_compress(rough_tokens)
        return self.should_compress(self._trigger_calibrated_tokens(rough_tokens))

    def get_automatic_compaction_status_message(
        self,
        *,
        phase: str,
        default_message: str,
        **context: Any,
    ) -> str | None:
        """Return user-visible status for automatic host-triggered compaction.

        Return ``None`` to suppress successful automatic lifecycle status for
        this compaction event. ``phase`` identifies the host call site (for
        example ``"preflight"`` or ``"compress"``). ``context`` contains
        best-effort fields such as ``approx_tokens`` and ``threshold_tokens``.

        This hook does not control warning/error messages or explicit manual
        commands such as ``/compress``.
        """
        if not self.emit_automatic_compaction_status:
            return None
        return default_message

    # -- Optional: manual /compress preflight ------------------------------

    def has_content_to_compress(self, messages: List[Dict[str, Any]]) -> bool:
        """Quick check: is there anything in ``messages`` that can be compacted?

        Used by the gateway ``/compress`` command as a preflight guard —
        returning False lets the gateway report "nothing to compress yet"
        without making an LLM call.

        Default returns True (always attempt).  Engines with a cheap way
        to introspect their own head/tail boundaries should override this
        to return False when the transcript is still entirely protected.
        """
        return True

    # -- Optional: session lifecycle ---------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Called when a new conversation session begins.

        Use this to load persisted state (DAG, store) for the session.
        kwargs may include hermes_home, platform, model, etc.
        """

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Called at real session boundaries (CLI exit, /reset, gateway expiry).

        Use this to flush state, close DB connections, etc.
        NOT called per-turn — only when the session truly ends.
        """

    def on_session_reset(self) -> None:
        """Called on /new or /reset. Reset per-session state.

        Default resets compression_count and token tracking.
        """
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    # -- Optional: tools ---------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas this engine provides to the agent.

        Engines may return bare OpenAI function schemas or full
        {"type": "function", "function": ...} tool definitions; the host
        normalizes both. LCM returns schemas for lcm_grep, lcm_describe,
        lcm_expand here.
        """
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a tool call from the agent.

        Only called for tool names returned by get_tool_schemas().
        Must return a JSON string.

        kwargs may include:
          messages: the current in-memory message list (for live ingestion)
        """
        import json
        return json.dumps({"error": f"Unknown context engine tool: {name}"})

    # -- Optional: status / display ----------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return status dict for display/logging.

        Default returns the standard fields run_agent.py expects.
        """
        # Clamp the -1 "compression just ran, awaiting real usage" sentinel
        # (set by conversation_compression) to 0 so status readers don't see a
        # raw -1 or a negative usage_percent on the transitional turn. Mirrors
        # the CLI/gateway status-bar paths (cli.py, tui_gateway/server.py).
        last_prompt = self.last_prompt_tokens if self.last_prompt_tokens > 0 else 0
        return {
            "last_prompt_tokens": last_prompt,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, last_prompt / self.context_length * 100)
                if self.context_length else 0
            ),
            "compression_count": self.compression_count,
        }

    # -- Optional: model switch support ------------------------------------

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        """Called when the user switches models or on fallback activation.

        Default updates context_length and recalculates threshold_tokens
        from threshold_percent. Override if your engine needs more
        (e.g. recalculate DAG budgets, switch summary models).
        """
        self.context_length = context_length
        # Apply per-model threshold overrides if set (longest substring match).
        # Falls back to _config_threshold_percent (the raw config value) when
        # no override matches. Plugin engines that override update_model() can
        # call resolve_model_threshold() for the same logic.
        from agent.context_compressor import resolve_model_threshold
        if not hasattr(self, "_config_threshold_percent"):
            # Snapshot the pre-override percent ONCE so repeated model
            # switches fall back to the engine's configured value, not the
            # previous model's override.
            self._config_threshold_percent = self.threshold_percent
        self._base_threshold_percent = resolve_model_threshold(
            model, getattr(self, "model_thresholds", {}),
            self._config_threshold_percent,
        )
        self.threshold_percent = self._base_threshold_percent
        self.threshold_tokens = int(context_length * self.threshold_percent)
