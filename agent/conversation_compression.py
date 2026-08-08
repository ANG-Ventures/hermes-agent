"""Context compression — extract the AIAgent methods that drive summarisation.

Three concerns live here:

* :func:`check_compression_model_feasibility` — startup probe of the
  configured auxiliary compression model.  Warns when the aux context
  window can't fit the main model's compression threshold; auto-lowers
  the session threshold when possible; hard-rejects auxes below
  ``MINIMUM_CONTEXT_LENGTH``.

* :func:`replay_compression_warning` — re-emit a stored warning through
  the gateway ``status_callback`` once it's wired up (the callback is
  set after :class:`AIAgent` construction).

* :func:`compress_context` — the actual compression call.  Runs the
  configured compressor, splits the SQLite session, rotates the
  session_id, notifies plugin context engines / memory providers, and
  returns the compressed message list and active system prompt.

* :func:`try_shrink_image_parts_in_messages` — image-too-large recovery
  helper that re-encodes ``data:image/...;base64,...`` parts at a smaller
  size so retries can fit under provider ceilings (Anthropic's 5 MB).

``run_agent`` keeps thin wrappers for each so existing call sites
(``self._compress_context(...)``) keep working.  Tests that exercise
these paths see no behavioural change.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import math
import os
import tempfile
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from agent.fork_ext.compaction_ext import (
    _ANNOUNCE_STATUS_CONDITIONAL,
    _ANNOUNCE_STATUS_UNCONDITIONAL,
    _abbrev_tokens,
    _append_subsplit_lines,
    _compaction_reason_clause,
    _compaction_window_label,
    _fmt_gross_frac,
    _format_compaction_announce,
    _format_granular_announce,
    _inturn_stats_render_eligible,
)
from agent.context_engine import (
    automatic_compaction_status_message,
    sanitize_memory_context,
)
from agent.model_metadata import estimate_request_tokens_rough

logger = logging.getLogger(__name__)

# ── Compaction completion announce (engine-aware) ──────────────────────────
# Spec: ~/.hermes/plans/2026-06-20_compaction-announce-with-context-reference.md
# A persistent, in-chat marker emitted when context is actually compacted, for
# BOTH the built-in ContextCompressor (lossy, session-rotating) and the LCM/DAG
# engine (lossless raw store + lcm_grep/lcm_expand recovery). Additive to the
# fallback announce (never a replacement). Emitted out-of-band via _emit_status,
# never injected into model history.

# Markers stripped from a summary snippet before display.
_COMPACTION_SUMMARY_MARKERS = (
    "[CONTEXT COMPACTION — REFERENCE ONLY]",
    "[CONTEXT COMPACTION - REFERENCE ONLY]",
    "[CONTEXT COMPACTION]",
)

# Stable marker the gateway matches on to re-tag the auto-compaction lifecycle
# status as ``kind="compacting"`` (tui_gateway/server.py::_status_update), so
# drivers like the desktop app can show an explicit "Summarizing…" indicator
# instead of the transcript appearing to silently reset. Keep the marker phrase
# intact if you reword COMPACTION_STATUS.
COMPACTION_STATUS_MARKER = "Compacting context"
COMPACTION_STATUS = (
    f"🗜️ {COMPACTION_STATUS_MARKER} — summarizing earlier conversation so I can continue..."
)

# A-floor approximate-attribution gross-error ceiling (in-turn granular announce).
# The A-floor (fallback single-walk partition in build_inturn_stats) reconciles
# TOTALS by construction but its kept/folded SPLIT is signature-approximate. The
# split error is provably bounded by the kept-tail fraction of pre (the folded bulk
# is a contiguous prefix that always classifies correctly), so when the kept tail
# exceeds this fraction the displayed split could be materially wrong and the render
# degrades to the honest two-line form instead. On real failing sessions the kept
# tail is ≤~7% of pre, so this never trips in practice — it is the honest backstop.
_APPROX_GROSS_MAX_FRAC = 0.10


def _warn_compaction_stats_once(agent, message: str, *, exc_info: bool = False) -> None:
    """Emit a compaction-stats degrade ``warning`` at most once per (cause, session).

    The granular compaction announce silently degrades to a two-line form when
    stats fail to build/reconcile; logging that at ``debug`` is how the PR #95
    regression stayed dark for weeks. This raises it to ``warning`` with a stable,
    greppable ``COMPACTION_STATS_*`` marker — but throttled per cause+session so a
    persistent reconcile bug can't flood the gateway log every turn. The throttle
    state lives on the agent (``_compaction_stats_warned``); if the agent can't
    hold it (no attribute), we still warn (fail-loud over fail-silent).

    Self-identification (spec 2026-07-02, D-4): every marker carries
    ``session=<id>`` so the daily watcher can attribute it without fragile
    proximity joins, plus ``src=test`` when running under pytest
    (``PYTEST_CURRENT_TEST``) so test-suite runs that write through the live
    logging config are excludable from production counts.
    """
    try:
        seen = getattr(agent, "_compaction_stats_warned", None)
        if seen is None:
            seen = set()
            try:
                agent._compaction_stats_warned = seen
            except Exception:
                seen = None
        # Key on the marker + path (first 2 tokens, e.g.
        # "COMPACTION_STATS_RECONCILE_FAILED in-turn"), NOT the full message, so a
        # varying reconcile reason doesn't defeat the throttle.
        cause = " ".join(message.split()[:2])
        key = (cause, getattr(agent, "session_id", None))
        if seen is not None:
            if key in seen:
                return
            seen.add(key)
    except Exception:
        pass  # never let throttle bookkeeping break the reply path
    try:
        _sid = getattr(agent, "session_id", None) or "-"
        message = f"{message} session={_sid}"
        if os.environ.get("PYTEST_CURRENT_TEST"):
            message = f"{message} src=test"
    except Exception:
        pass  # marker suffix is best-effort; never break the warn itself
    logger.warning(message, exc_info=exc_info)


def _msg_text(content: Any) -> str:
    """Flatten a message ``content`` (str or list-of-blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("content")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _extract_compaction_summary_snippet(
    compressed_messages: list, *, max_chars: int = 160
) -> "str | None":
    """Deterministically pull a one-line snippet of what was summarised.

    Scans for the first message carrying a compaction summary marker, strips the
    marker boilerplate, collapses whitespace, and truncates at a word boundary.
    Returns ``None`` when no usable summary text exists (e.g. a placeholder-only
    marker). Not an LLM call.
    """
    if not compressed_messages:
        return None
    for msg in compressed_messages:
        if not isinstance(msg, dict):
            continue
        text = _msg_text(msg.get("content"))
        if not text:
            continue
        if not any(m in text for m in _COMPACTION_SUMMARY_MARKERS):
            continue
        for m in _COMPACTION_SUMMARY_MARKERS:
            text = text.replace(m, " ")
        # collapse all whitespace runs to single spaces
        cleaned = " ".join(text.split())
        if not cleaned:
            return None
        if len(cleaned) <= max_chars:
            return cleaned
        # truncate at a word boundary, then append an ellipsis
        cut = cleaned[:max_chars].rsplit(" ", 1)[0].rstrip()
        if not cut:
            cut = cleaned[:max_chars].rstrip()
        return cut + "…"
    return None


def _resolve_announce_reasoning(agent: Any) -> "str | None":
    """Resolve the reasoning-effort label for the compaction announce.

    Session-truthful source first: the live agent's ``reasoning_config`` is set
    per-message by the gateway from ``_resolve_session_reasoning_config`` (which
    honors ``/reasoning`` session overrides and per-model overrides), so it is
    what the turn actually ran at. The old behavior — re-reading the *global*
    ``agent.reasoning_effort`` from config.yaml — showed the config default
    (e.g. ``r:medium``) while the session genuinely ran at an override
    (``r:xhigh``): the same bug class the runtime footer fixed in
    ``_reasoning_effort_for_footer``. Fall back to the global config value only
    when the agent carries no reasoning_config (CLI/standalone paths).

    Returns the bare effort string (``"xhigh"``), ``"none"`` for an explicit
    disabled config, or ``None`` when nothing is resolvable (announce omits
    the ``r:`` segment; ``_format_compaction_announce`` also drops
    ``default``/``none`` labels).
    """
    try:
        from hermes_constants import reasoning_label
        label = reasoning_label(getattr(agent, "reasoning_config", None))
        if label:
            return label
    except Exception:
        pass
    try:
        from gateway.run import _load_gateway_config as _lgc
        _ac = (_lgc().get("agent") or {})
        return str(_ac.get("reasoning_effort", "") or "").strip() or None
    except Exception:
        return None


def _emit_compaction_announce(agent: Any, *, dedupe_key, **fmt_kwargs) -> None:
    """Emit the compaction announce once per real compaction boundary.

    Dedupe: ``agent._last_compaction_announced`` holds an engine-namespaced
    ``dedupe_key``; a repeat key is skipped. The key is set ONLY after a
    successful emit (D7), so a swallowed emit failure does not suppress the next
    real compaction's announce. The caller holds the compression lock, so the
    read-then-write is serialized per session.

    Loud-fail (§3.5, B3): this site KNOWS the message is a compaction announce
    (so the marker is announce-specific, never tripped by a generic lifecycle
    status). When ``status_callback`` is absent the throwaway/gateway caller is
    expected to deliver → INFO ``PENDING_CALLER_DELIVERY``. When the in-turn
    ``status_callback`` exists but the send fails →
    WARNING ``STATUS_CALLBACK_FAILED`` (the in-turn silent hole, now loud).
    """
    line = _format_compaction_announce(**fmt_kwargs)
    if line is None:
        return  # gating skip — do not advance the key
    if getattr(agent, "_last_compaction_announced", None) == dedupe_key:
        return  # already announced this boundary
    _sid = getattr(agent, "session_id", None) or "?"
    emit = getattr(agent, "_emit_status", None)
    if not callable(emit):
        return
    _had_callback = bool(getattr(agent, "status_callback", None))
    try:
        delivered = emit(line)
    except Exception:
        logger.debug("compaction announce emit failed", exc_info=True)
        return  # key NOT advanced — next real compaction can still announce
    # ``_emit_status`` returns True only when a gateway status_callback existed
    # AND did not raise. Three cases (loud-fail §3.5 / B3):
    if delivered:
        agent._last_compaction_announced = dedupe_key  # in-turn live delivery OK
        return
    if _had_callback:
        # callback existed but the send leg raised → the in-turn announce was
        # LOST. Make it loud (was the one remaining silent-compaction hole).
        logger.warning("COMPACTION_ANNOUNCE_STATUS_CALLBACK_FAILED session=%s", _sid)
        return  # key NOT advanced — a retry can still announce
    # No gateway callback → either a CLI-only agent (the _vprint leg already
    # showed it) or a throwaway agent (hygiene/compress) whose CALLER delivers
    # from real facts. Either way it is NOT a silent failure. Mark pending so a
    # throwaway-path delivery gap is auditable, and advance the key (CLI delivery
    # via _vprint is complete; the throwaway caller dedupes structurally).
    logger.info(
        "COMPACTION_ANNOUNCE_PENDING_CALLER_DELIVERY reason=%s session=%s",
        fmt_kwargs.get("trigger_reason"), _sid,
    )
    agent._last_compaction_announced = dedupe_key


# Tight wall-clock window (seconds) used ONLY when no turn id is available to
# link a fallback to a following compaction. Real chained fallback→compaction
# happens within one turn (seconds), not minutes — keep this tight.
_POST_FALLBACK_WALLCLOCK_SECS = 75.0


def _compaction_after_fallback(
    agent: Any, *, now_monotonic: float, current_turn_id: "str | None"
) -> "Tuple[bool, Optional[int], Optional[int]]":
    """Decide whether this compaction follows a model fallback, turn-scoped.

    Returns ``(after_fallback, window_from, window_to)``. The causal signal is
    *same logical turn AND fallback-before-compaction* — NOT wall-clock
    proximity (the §0 incident had the fallback AFTER the compaction, which must
    NOT be labeled). Only when no turn id exists on either side does it fall back
    to a tight wall-clock window, still requiring fallback-before-compaction.
    """
    ev = getattr(agent, "_last_fallback_event", None)
    if not isinstance(ev, dict):
        return (False, None, None)
    fb_mono = ev.get("monotonic_time")
    if fb_mono is None or fb_mono > now_monotonic:
        # fallback happened AFTER this compaction (or unknown time) → not causal
        return (False, None, None)
    fb_turn = ev.get("turn_id")
    if fb_turn is not None and current_turn_id is not None:
        if fb_turn != current_turn_id:
            return (False, None, None)
    else:
        # no turn linkage available → tight wall-clock fallback
        if (now_monotonic - fb_mono) > _POST_FALLBACK_WALLCLOCK_SECS:
            return (False, None, None)
    return (True, ev.get("old_window"), ev.get("new_window"))

COMPACTION_DONE_STATUS = "✓ Context compaction complete — continuing turn..."


def _emit_compaction_done(agent: Any) -> None:
    """Emit the structured terminal edge for a started compaction."""
    status_callback = getattr(agent, "status_callback", None)
    if not status_callback:
        return
    try:
        status_callback("compacted", COMPACTION_DONE_STATUS)
    except Exception:
        logger.debug("status_callback error in compaction completion", exc_info=True)


# ── Routine compression status templates ────────────────────────────────────
# Every ROUTINE (non-failure, non-manual-/compress) compression status line the
# agent emits lives here so the gateway noise filter and its tests can couple
# to the real emitted wording instead of hand-copied literals. These are
# suppressed on human-facing chat platforms by _TELEGRAM_NOISY_STATUS_RE
# (gateway/run.py) — when rewording ANY of them, update that regex and the
# pinned data in tests/gateway/test_telegram_noise_filter.py in the same PR.
# Failure notices (⚠ Compression aborted / empty transcript / codex compaction
# failed) and manual /compress feedback (manual_compression_feedback.py) are
# deliberate carve-outs from silence and must NOT be added here.
PRE_API_COMPRESSION_STATUS_TEMPLATE = (
    "📦 Pre-API compression: ~{tokens:,} tokens "
    "near the context/output limit. Compacting before the next model call."
)
PREFLIGHT_COMPRESSION_STATUS_TEMPLATE = (
    "📦 Preflight compression: ~{tokens:,} tokens "
    ">= {threshold:,} threshold. This may take a moment."
)
ENGINE_PREFLIGHT_MAINTENANCE_STATUS_TEMPLATE = (
    "📦 {engine} maintenance compaction: ~{tokens:,} tokens "
    "(BELOW the {threshold:,} threshold) — the context engine requested this, "
    "not token pressure. This may take a moment."
)
# Same arm, but the engine told us WHY. A below-threshold compaction with no
# cause reads as unprovoked ("we were nowhere near the threshold"), so prefer
# this form whenever the engine exposes a reason.
ENGINE_PREFLIGHT_MAINTENANCE_REASON_STATUS_TEMPLATE = (
    "📦 {engine} maintenance compaction: ~{tokens:,} tokens "
    "(BELOW the {threshold:,} threshold) — triggered because {reason}, "
    "not token pressure. This may take a moment."
)
IDLE_COMPACTION_STATUS_TEMPLATE = (
    "💤 Resumed after {idle_seconds}s idle — compacting "
    "~{tokens:,} tokens before continuing."
)
COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE = (
    "🗜️ Context too large (~{tokens:,} tokens) — compressing ({attempt}/{cap})..."
)
COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE = (
    "🗜️ Compressed {before} → {after} messages, retrying..."
)
COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE = (
    "🗜️ Compressed ~{before:,} → ~{after:,} tokens, retrying..."
)
COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE = (
    "🗜️ Context reduced to {new_ctx:,} tokens (was {old_ctx:,}), retrying..."
)

# FAILURE-CLASS notice — a deliberate carve-out from routine-compression
# silence (#16775 class): the context is over the compression threshold but
# compression is blocked (summary-LLM cooldown / anti-thrash breaker), so the
# session will keep growing until the hard provider token limit kills it.
# This MUST stay visible on chat gateways. Do NOT add it to
# ROUTINE_COMPRESSION_STATUS_SAMPLES or the gateway noise regex
# (_TELEGRAM_NOISY_STATUS_RE); it is pinned un-swallowed in
# tests/gateway/test_telegram_noise_filter.py::VISIBLE_COMPRESSION_MESSAGES.
CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE = (
    "⚠ Context is over the compression threshold "
    "(~{tokens:,} tokens >= {threshold:,}) "
    "but compression is currently blocked ({reason}). "
    "The model may stop responding. Run /new to start a fresh "
    "session or /compress to retry immediately."
)

# Sample-formatted instances of every routine compression status line, for
# behavioral tests that iterate the ACTUAL emitted wording (formatted from the
# same constants the emission sites use) through the gateway noise filter.
ROUTINE_COMPRESSION_STATUS_SAMPLES = (
    COMPACTION_STATUS,
    PRE_API_COMPRESSION_STATUS_TEMPLATE.format(tokens=123456),
    PREFLIGHT_COMPRESSION_STATUS_TEMPLATE.format(tokens=120000, threshold=100000),
    IDLE_COMPACTION_STATUS_TEMPLATE.format(idle_seconds=3600, tokens=120000),
    COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE.format(tokens=250000, attempt=1, cap=3),
    COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE.format(before=30, after=12),
    COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE.format(before=250000, after=120000),
    COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE.format(
        new_ctx=120000, old_ctx=250000
    ),
)


def _builtin_memory_prompt_snapshot(agent: Any) -> Optional[Tuple[str, str]]:
    """Return the built-in memory text that can affect a system prompt.

    ``MemoryStore`` freezes this text until ``load_from_disk()``.  Rendering
    the frozen blocks after that reload lets compression retain the exact
    cached system prompt when it already embeds the current memory (see
    :func:`_cached_prompt_reflects_builtin_memory`).  An unreadable snapshot
    returns ``None`` so callers take the conservative rebuild path.
    """
    store = getattr(agent, "_memory_store", None)
    if store is None:
        return "", ""
    try:
        memory = (
            store.format_for_system_prompt("memory") or ""
            if getattr(agent, "_memory_enabled", False)
            else ""
        )
        user = (
            store.format_for_system_prompt("user") or ""
            if getattr(agent, "_user_profile_enabled", False)
            else ""
        )
    except Exception:
        return None
    return memory, user


def _cached_prompt_reflects_builtin_memory(agent: Any, cached_prompt: str) -> bool:
    """Whether the cached system prompt already embeds current built-in memory.

    The retention fast path must NOT compare the memory snapshot before vs
    after the disk reload: on fresh-agent surfaces (gateway, TUI) the cached
    prompt is restored from the session DB and can predate mid-session memory
    writes that the fresh ``MemoryStore`` already picked up at init — the
    snapshot is then identical on both sides of the reload while the prompt
    itself is stale, and retaining it would latch old memory for the life of
    the session (and re-persist it via ``update_system_prompt``).

    Instead, verify the CURRENT (post-reload) rendered blocks appear verbatim
    in the cached prompt, and that no leftover block header remains for a
    target whose entries have since been emptied or disabled.
    """
    snapshot = _builtin_memory_prompt_snapshot(agent)
    if snapshot is None:
        return False
    try:
        from tools.memory_tool import MEMORY_BLOCK_HEADERS
    except Exception:
        return False
    for target, block in zip(("memory", "user"), snapshot):
        block = block.strip()
        if block:
            # build_system_prompt_parts embeds the stripped block verbatim;
            # the rendered text includes the usage header, so any entry
            # change (or char-count change) breaks containment → rebuild.
            if block not in cached_prompt:
                return False
        elif MEMORY_BLOCK_HEADERS[target] in cached_prompt:
            # The prompt still carries a block for a target that is now
            # empty/disabled — stale; rebuild.
            return False
    return True


class CompressionCommitFence:
    """Fence timeout cancellation against post-summary session mutation.

    Compression itself is synchronous and may be running in an executor thread.
    A caller can stop waiting for the summary, but it cannot kill that thread.
    This fence makes the commit boundary deterministic: cancellation either wins
    before session mutation starts, or waits until an already-started commit is
    fully complete before the caller proceeds.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._commit_started = False

    def cancel_before_commit(self) -> bool:
        """Cancel a pending commit, or wait for an active commit to finish.

        Returns ``True`` when cancellation won before the commit boundary.
        Returns ``False`` when the worker had already entered the boundary; in
        that case acquiring this lock waits until all session mutation finishes.
        """
        with self._lock:
            if self._commit_started:
                return False
            self._cancelled = True
            return True

    def try_cancel_before_commit(self) -> Optional[bool]:
        """Non-blocking form of :meth:`cancel_before_commit`.

        Returns ``None`` while an active commit owns the fence, allowing an
        async caller to yield instead of blocking its event loop.
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._commit_started:
                return False
            self._cancelled = True
            return True
        finally:
            self._lock.release()

    def begin_commit(self) -> bool:
        """Enter the commit boundary unless cancellation already won."""
        self._lock.acquire()
        if self._cancelled:
            self._lock.release()
            return False
        self._commit_started = True
        return True

    def finish_commit(self) -> None:
        """Leave a commit boundary entered by :meth:`begin_commit`."""
        self._lock.release()


def _lock_api_is_absent_on_session_db(lock_db: Any) -> bool:
    """Whether the live in-memory SessionDB class structurally predates locks.

    In the supported hot-reload skew, this module is new while the already
    imported ``hermes_state.SessionDB`` class (and its live instances) is old.
    Only that exact class identity may fail open. Proxies, nominal lookalikes,
    non-callables, and descriptor failures must fail closed. Static lookup
    avoids invoking a present-but-broken descriptor.
    """
    try:
        from hermes_state import SessionDB

        missing = object()
        return (
            type(lock_db) is SessionDB
            and inspect.getattr_static(
                SessionDB, "try_acquire_compression_lock", missing
            ) is missing
        )
    except Exception:
        return False


def _refresh_persisted_compression_guards(compressor: Any) -> None:
    """Refresh durable automatic-compression guards on a built-in compressor."""
    method_calls = (
        ("get_active_compression_failure_cooldown", {"refresh": True}),
        ("_load_fallback_compression_streak", {}),
        ("_load_ineffective_compression_count", {}),
    )
    for method_name, kwargs in method_calls:
        method = getattr(type(compressor), method_name, None)
        if not callable(method):
            continue
        try:
            method(compressor, **kwargs)
        except Exception as exc:
            logger.debug("compression guard refresh failed (%s): %s", method_name, exc)


def _session_was_rotated_by_compression(session_db: Any, session_id: str) -> bool:
    """Return whether another path already rotated this compression parent."""
    getter = getattr(type(session_db), "get_session", None)
    if not callable(getter):
        return False
    session = getter(session_db, session_id)
    return bool(
        session
        and session.get("ended_at") is not None
        and session.get("end_reason") == "compression"
    )


def _emit_compression_attempt_telemetry(
    agent: Any,
    *,
    started_at: float,
    commit_status: str,
    split_status: str,
    failure_class: str | None = None,
) -> None:
    """Emit one content-free JSON log line for a compression attempt."""
    try:
        telemetry = getattr(agent.context_compressor, "_last_compression_telemetry", None)
        if not isinstance(telemetry, dict):
            telemetry = {}
        payload = dict(telemetry)
        payload.setdefault("event", "compression_attempt")
        payload.setdefault("attempt_id", getattr(agent, "_compression_attempt_id", "") or uuid.uuid4().hex)
        payload.setdefault("session_id", getattr(agent, "session_id", "") or "")
        payload["total_duration_ms"] = int((time.monotonic() - started_at) * 1000)
        payload["commit_status"] = commit_status
        payload["split_status"] = split_status
        if failure_class:
            payload["failure_class"] = failure_class
        payload.setdefault("chunking", False)
        payload.setdefault("chunk_count", 0)
        payload["fallback_used"] = bool(
            payload.get("fallback_used")
            or getattr(agent.context_compressor, "_last_summary_fallback_used", False)
            or getattr(agent.context_compressor, "_last_aux_model_failure_model", None)
        )
        logger.info(
            "context compression attempt telemetry: %s",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
    except Exception as exc:
        logger.debug("failed to emit compression attempt telemetry: %s", exc)


def compression_skipped_due_to_lock(agent: Any) -> bool:
    """Type-pinned read of the #69870 lock-skip signal.

    ``agent._compression_skipped_due_to_lock`` is set by ``compress_context``
    when a compression pass no-ops because another path holds the per-session
    compression lock (holder string when the holder was confirmed, ``True``
    otherwise) and cleared to ``None`` at the entry of every call.

    The read MUST be type-pinned (``is True or isinstance(x, str)``), never
    bare truthiness: MagicMock test-double agents auto-create truthy
    attributes, and a bare ``if getattr(agent, ...)`` would hijack every
    mocked agent in sibling suites into the lock-skip branch (the
    #69870 × #69840 type-ahead incident).
    """
    _sig = getattr(agent, "_compression_skipped_due_to_lock", None)
    return _sig is True or isinstance(_sig, str)


def _compression_lock_holder(agent: Any) -> str:
    """Build a unique holder id for the lock: pid:tid:agent-instance:uuid.

    The pid+tid prefix lets ops tell crashed/abandoned holders apart from
    live ones (expiry-based recovery uses the timestamp, but ``holder``
    is what shows up in diagnostics + log lines). The agent instance id
    and a per-acquire uuid disambiguate two co-resident agents on the
    same thread (background_review forks run on a worker thread, but
    on machines where compression itself dispatches to a thread pool
    we want each acquire to be unique).
    """
    import threading
    return (
        f"pid={os.getpid()}"
        f":tid={threading.get_ident()}"
        f":agent={id(agent):x}"
        f":nonce={uuid.uuid4().hex[:8]}"
    )


def _compression_kwargs_are_signature_proven(compress_fn: Any) -> bool:
    """True when signature inspection PROVED which kwargs the engine accepts.

    ``_supported_compression_kwargs`` filters to names the callable declares. That
    filter is only *predictive* when the signature is (a) inspectable and (b) has
    no ``**kwargs`` catch-all — then a kwarg we pass is guaranteed to bind, so a
    ``TypeError`` escaping ``compress()`` is an INTERNAL ENGINE BUG and must
    propagate untouched (retrying would execute a stateful compressor twice —
    the exact hazard ``_supported_compression_kwargs``' docstring names).

    When the callable is opaque (C-backed, no signature) or declares
    ``**kwargs`` (wrappers, Mocks, decorated engines), inspection CANNOT predict
    a strict-signature rejection, so the fork's runtime-``TypeError`` fallback to
    the oldest documented call shape stays in force (#69870).
    """
    try:
        parameters = inspect.signature(compress_fn).parameters
    except (TypeError, ValueError):
        return False
    return not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _supported_compression_kwargs(
    compress_fn: Any,
    *,
    current_tokens: Optional[int],
    focus_topic: Optional[str],
    force: bool,
    memory_context: str,
) -> dict:
    """Return only compression kwargs accepted by an engine callable.

    Context-engine plugins can outlive additions to the optional host contract.
    Inspecting the callable before invoking it keeps those older signatures
    compatible without catching an internal ``TypeError`` and executing a
    stateful compressor twice.
    """
    candidates = {
        "current_tokens": current_tokens,
        "focus_topic": focus_topic,
        "force": force,
    }
    if memory_context:
        candidates["memory_context"] = memory_context
    try:
        parameters = inspect.signature(compress_fn).parameters
    except (TypeError, ValueError):
        # ``current_tokens`` has been part of the ContextEngine ABC since its
        # introduction. Keep the oldest documented call shape when a C-backed
        # or otherwise opaque callable has no inspectable signature.
        return {"current_tokens": current_tokens}

    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_kwargs:
        return candidates
    return {name: value for name, value in candidates.items() if name in parameters}


class _CompressionActivityHeartbeat:
    """Refresh the agent inactivity tracker while compression blocks in an aux call."""

    def __init__(self, agent: Any, interval_seconds: float | None = None) -> None:
        self._agent = agent
        if interval_seconds is None:
            interval_seconds = getattr(agent, "_compression_activity_heartbeat_interval", 60.0)
        try:
            interval_seconds = float(interval_seconds or 60.0)
        except (TypeError, ValueError):
            interval_seconds = 60.0
        if not math.isfinite(interval_seconds):
            interval_seconds = 60.0
        self._interval_seconds = max(0.1, interval_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="compression-activity-heartbeat",
            daemon=True,
        )

    def start(self) -> "_CompressionActivityHeartbeat":
        self._touch("context compression started")
        self._thread.start()
        return self

    def stop(self, desc: str = "context compression completed") -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)
        self._touch(desc)

    def _touch(self, desc: str) -> None:
        try:
            touch = getattr(self._agent, "_touch_activity", None)
            if callable(touch):
                touch(desc)
        except Exception:
            logger.debug("compression activity heartbeat touch failed", exc_info=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._touch("context compression in progress")


class _CompressionLockLeaseRefresher:
    def __init__(
        self,
        db: Any,
        session_id: str,
        holder: str,
        ttl_seconds: float,
        refresh_interval_seconds: float | None = None,
    ) -> None:
        self._db = db
        self._session_id = session_id
        self._holder = holder
        self._ttl_seconds = ttl_seconds
        if refresh_interval_seconds is None:
            refresh_interval_seconds = max(1.0, min(60.0, ttl_seconds / 2.0))
        self._refresh_interval_seconds = max(0.1, float(refresh_interval_seconds))
        # Tolerate transient refresh failures for at most one lease's worth of
        # time, so the give-up window is genuinely bounded by the TTL the
        # acquirer set (a single blip recovers on the next tick; a persistent
        # failure stops before the lease could outlive its TTL). Floor of 1 so a
        # degenerate interval >= ttl still tolerates one blip.
        self._max_consecutive_failures = max(
            1, int(self._ttl_seconds / self._refresh_interval_seconds)
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="compression-lock-refresh",
            daemon=True,
        )

    def start(self) -> "_CompressionLockLeaseRefresher":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        # join() may time out while the refresher is mid-UPDATE; that's safe —
        # it's a daemon thread, and a late refresh on an already-released lock
        # matches rowcount 0 (a no-op). stop() returning does not guarantee the
        # thread has fully quiesced, only that we've signalled it and waited
        # briefly.
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        # A single falsy refresh must NOT permanently kill the lease: a
        # transient DB blip (write contention escaping _execute_write's retry
        # budget, a momentary "database is locked") returns False just like a
        # genuine lost-ownership, but only the latter should stop the loop.
        # Tolerate consecutive failures for at most one lease's worth of time
        # (_max_consecutive_failures = ttl / interval), so a one-off blip
        # recovers on the next tick while the total give-up window stays bounded
        # by the TTL the acquirer set — the lock can never be held past its TTL
        # by a stuck refresher.
        consecutive_failures = 0
        while not self._stop.wait(self._refresh_interval_seconds):
            try:
                refreshed = self._db.refresh_compression_lock(
                    self._session_id,
                    self._holder,
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception as exc:
                logger.debug("compression lock refresh raised: %s", exc)
                refreshed = False
            if refreshed:
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            if consecutive_failures >= self._max_consecutive_failures:
                logger.debug(
                    "compression lock refresh failed %d times in a row; "
                    "stopping lease refresher for session %s",
                    consecutive_failures, self._session_id,
                )
                break


def check_compression_model_feasibility(agent: Any) -> None:
    """Warn at session start if the auxiliary compression model's context
    window is smaller than the main model's compression threshold.

    When the auxiliary model cannot fit the content that needs summarising,
    compression will either fail outright (the LLM call errors) or produce
    a severely truncated summary.

    Called during ``AIAgent.__init__`` so CLI users see the warning
    immediately (via ``_vprint``).  The gateway sets ``status_callback``
    *after* construction, so :func:`replay_compression_warning` re-sends
    the stored warning through the callback on the first
    ``run_conversation()`` call.
    """
    if not agent.compression_enabled:
        return
    try:
        from agent.auxiliary_client import (
            _resolve_task_provider_model,
            _try_configured_fallback_for_unavailable_client,
            get_text_auxiliary_client,
        )
        from agent.model_metadata import (
            MINIMUM_CONTEXT_LENGTH,
            get_model_context_length,
        )

        # Best-effort aux provider label for the warning message. The
        # configured provider may be "auto", in which case we fall back
        # to the client's base_url hostname so the user can still tell
        # where the compression model is actually being called.
        try:
            _aux_cfg_provider, _, _, _, _ = _resolve_task_provider_model("compression")
        except Exception:
            _aux_cfg_provider = ""
        client, aux_model = get_text_auxiliary_client(
            "compression",
            main_runtime=agent._current_main_runtime(),
        )
        if client is None or not aux_model:
            fb_client, fb_model, fb_label = _try_configured_fallback_for_unavailable_client(
                "compression",
                _aux_cfg_provider,
            )
            if fb_client is not None and fb_model:
                client, aux_model = fb_client, fb_model
                if "(" in fb_label and fb_label.endswith(")"):
                    _aux_cfg_provider = fb_label.rsplit("(", 1)[1][:-1]
        if client is None or not aux_model:
            if _aux_cfg_provider and _aux_cfg_provider != "auto":
                msg = (
                    "⚠ Configured auxiliary compression provider "
                    f"'{_aux_cfg_provider}' is unavailable — context "
                    "compression will drop middle turns without a summary. "
                    "Check auxiliary.compression in config.yaml and "
                    "reauthenticate that provider."
                )
            else:
                msg = (
                    "⚠ No auxiliary LLM provider configured — context "
                    "compression will drop middle turns without a summary. "
                    "Run `hermes setup` or set OPENROUTER_API_KEY."
                )
            agent._compression_warning = msg
            agent._emit_status(msg)
            logger.warning(
                "No auxiliary LLM provider for compression — "
                "summaries will be unavailable."
            )
            return

        aux_base_url = str(getattr(client, "base_url", ""))
        # ``client.api_key`` may be a callable (Azure Foundry Entra ID
        # bearer provider). The context-length resolver chain expects a
        # string, but it only needs a key for live catalogue probes
        # (provider model lists). For Entra clients the model-metadata
        # chain still resolves via models.dev + hardcoded family
        # fallbacks, which don't require auth — pass empty string rather
        # than minting a bearer JWT just to look up a context length.
        _raw_aux_key = getattr(client, "api_key", "")
        aux_api_key = "" if (callable(_raw_aux_key) and not isinstance(_raw_aux_key, str)) else str(_raw_aux_key or "")

        aux_context = get_model_context_length(
            aux_model,
            base_url=aux_base_url,
            api_key=aux_api_key,
            config_context_length=getattr(agent, "_aux_compression_context_length_config", None),
            # Each model must be resolved with its own provider so that
            # provider-specific paths (e.g. Bedrock static table, OpenRouter API)
            # are invoked for the correct client, not inherited from the main model.
            provider=(_aux_cfg_provider if _aux_cfg_provider and _aux_cfg_provider != "auto" else getattr(agent, "provider", "")),
            custom_providers=agent._custom_providers,
        )

        # Hard floor: the auxiliary compression model must have at least
        # MINIMUM_CONTEXT_LENGTH (64K) tokens of context.  The main model
        # is already required to meet this floor (checked earlier in
        # __init__), so the compression model must too — otherwise it
        # cannot summarise a full threshold-sized window of main-model
        # content.  Mirrors the main-model rejection pattern.
        if aux_context and aux_context < MINIMUM_CONTEXT_LENGTH:
            raise ValueError(
                f"Auxiliary compression model {aux_model} has a context "
                f"window of {aux_context:,} tokens, which is below the "
                f"minimum {MINIMUM_CONTEXT_LENGTH:,} required by Hermes "
                f"Agent.  Choose a compression model with at least "
                f"{MINIMUM_CONTEXT_LENGTH // 1000}K context (set "
                f"auxiliary.compression.model in config.yaml), or set "
                f"auxiliary.compression.context_length to override the "
                f"detected value if it is wrong."
            )

        threshold = agent.context_compressor.threshold_tokens
        if aux_context < threshold:
            # Auto-correct: lower the live session threshold so
            # compression actually works this session.  The hard floor
            # above guarantees aux_context >= MINIMUM_CONTEXT_LENGTH,
            # so the new threshold is always >= 64K.
            #
            # The compression summariser sends a single user-role
            # prompt (no system prompt, no tools) to the aux model, so
            # new_threshold == aux_context is safe: the request is
            # the raw messages plus a small summarisation instruction.
            old_threshold = threshold
            new_threshold = aux_context
            agent.context_compressor.threshold_tokens = new_threshold
            # ``tail_token_budget`` is derived from the trigger threshold, not
            # directly from the model window. Keep it in lockstep with this
            # just-in-time correction exactly as ContextCompressor.update_model()
            # does. Leaving the old budget behind can make the tail's 1.5x soft
            # ceiling wider than the lowered trigger, so compression preserves
            # nearly the entire request and repeatedly re-fires.
            summary_target_ratio = getattr(
                agent.context_compressor, "summary_target_ratio", None
            )
            if isinstance(summary_target_ratio, (int, float)):
                agent.context_compressor.tail_token_budget = int(
                    new_threshold * summary_target_ratio
                )
            # Keep threshold_percent in sync so future main-model
            # context_length changes (update_model) re-derive from a
            # sensible number rather than the original too-high value.
            main_ctx = agent.context_compressor.context_length
            if main_ctx:
                agent.context_compressor.threshold_percent = (
                    new_threshold / main_ctx
                )
            safe_pct = int((aux_context / main_ctx) * 100) if main_ctx else 50
            # The "lower the threshold" suggestion must survive the built-in
            # trigger recomputation (#67422): _effective_threshold_percent()
            # raises sub-75% values back up for main windows under 512K, and
            # _compute_threshold_tokens() further applies the output-token
            # reservation, the 64K floor, and the degenerate-window guard.
            # Recommending a value those would override is silently ignored
            # and this warning would reappear every session — so mirror the
            # compressor's own math and only offer the option when the
            # recomputed trigger actually fits the auxiliary model's context.
            # External engines own compaction policy (#44439); the built-in
            # floor doesn't apply to them, so keep the plain suggestion.
            from agent.context_compressor import ContextCompressor as _CC

            recomputed_threshold = None
            if main_ctx and isinstance(agent.context_compressor, _CC):
                recomputed_threshold = _CC._compute_threshold_tokens(
                    main_ctx,
                    _CC._effective_threshold_percent(main_ctx, safe_pct / 100),
                    getattr(agent.context_compressor, "max_tokens", None),
                )
            threshold_suggestion_viable = (
                recomputed_threshold is None or recomputed_threshold <= aux_context
            )
            # Build human-readable "model (provider)" labels for both
            # the main model and the compression model so users can
            # tell at a glance which provider each side is actually
            # using. When the configured provider is empty or "auto",
            # fall back to the client's base_url hostname.
            _main_model = getattr(agent, "model", "") or "?"
            _main_provider = getattr(agent, "provider", "") or ""
            _aux_provider_label = (
                _aux_cfg_provider
                if _aux_cfg_provider and _aux_cfg_provider != "auto"
                else ""
            )
            if not _aux_provider_label:
                try:
                    from urllib.parse import urlparse
                    _aux_provider_label = (
                        urlparse(aux_base_url).hostname or aux_base_url
                    )
                except Exception:
                    _aux_provider_label = aux_base_url or "auto"
            _main_label = (
                f"{_main_model} ({_main_provider})"
                if _main_provider
                else _main_model
            )
            _aux_label = f"{aux_model} ({_aux_provider_label})"
            msg = (
                f"⚠ Compression model {_aux_label} context is "
                f"{aux_context:,} tokens, but the main model "
                f"{_main_label}'s compression threshold was "
                f"{old_threshold:,} tokens. "
                f"Auto-lowered this session's threshold to "
                f"{new_threshold:,} tokens so compression can run.\n"
            )
            if threshold_suggestion_viable:
                msg += (
                    f"  To make this permanent, edit config.yaml — either:\n"
                    f"  1. Use a larger compression model:\n"
                    f"       auxiliary:\n"
                    f"         compression:\n"
                    f"           model: <model-with-{old_threshold:,}+-context>\n"
                    f"  2. Lower the compression threshold:\n"
                    f"       compression:\n"
                    f"         threshold: 0.{safe_pct:02d}"
                )
            else:
                msg += (
                    f"  To make this permanent, use a larger compression "
                    f"model in config.yaml:\n"
                    f"       auxiliary:\n"
                    f"         compression:\n"
                    f"           model: <model-with-{old_threshold:,}+-context>\n"
                    f"  (Lowering compression.threshold cannot help here — "
                    f"with {_main_label}'s {main_ctx:,}-token window, "
                    f"Hermes's small-context floor and output reservation "
                    f"would recompute the trigger to "
                    f"{recomputed_threshold:,} tokens, still above the "
                    f"compression model's {aux_context:,}.)"
                )
            agent._compression_warning = msg
            agent._emit_status(msg)
            logger.warning(
                "Auxiliary compression model %s has %d token context, "
                "below the main model's compression threshold of %d "
                "tokens — auto-lowered session threshold to %d to "
                "keep compression working.",
                aux_model,
                aux_context,
                old_threshold,
                new_threshold,
            )
    except ValueError:
        # Hard rejections (aux below minimum context) must propagate
        # so the session refuses to start.
        raise
    except Exception as exc:
        logger.debug(
            "Compression feasibility check failed (non-fatal): %s", exc
        )


def replay_compression_warning(agent: Any) -> None:
    """Re-send the compression warning through ``status_callback``.

    During ``__init__`` the gateway's ``status_callback`` is not yet
    wired, so ``_emit_status`` only reaches ``_vprint`` (CLI).  This
    method is called once at the start of the first
    ``run_conversation()`` — by then the gateway has set the callback,
    so every platform (Telegram, Discord, Slack, etc.) receives the
    warning.
    """
    msg = getattr(agent, "_compression_warning", None)
    if msg and agent.status_callback:
        try:
            agent.status_callback("lifecycle", msg)
        except Exception:
            pass


def conversation_history_after_compression(
    agent: Any,
    messages: list,
    previous_history: Optional[list] = None,
) -> Optional[list]:
    """Return the correct flush baseline after a compression boundary.

    Legacy compression rotates to a fresh child session. That child has not
    seen the compacted transcript through the normal same-turn flush path yet,
    so callers must clear ``conversation_history`` to ``None`` and let the next
    persistence call write the whole compacted list.

    In-place compaction is different: ``archive_and_compact()`` has already
    soft-archived the previous active rows and inserted ``messages`` as the new
    active live transcript under the same session id. If the same agent turn
    continues with ``conversation_history=None``, the identity-based flush path
    treats those already-persisted compacted dicts as new and appends them a
    second time, doubling the active context and retriggering compression.

    A shallow copy is intentional: it captures the current compacted dict
    identities as history while allowing later same-turn appends to remain new.

    An aborted or no-op attempt after an earlier in-place compaction must retain
    the pre-attempt baseline.  Treating all current messages as persisted would
    drop any later, unflushed turns on restart; clearing the baseline would
    append the already-persisted compacted rows a second time.
    """
    if bool(getattr(agent, "_last_compression_attempt_recorded", False)):
        attempt_in_place = getattr(agent, "_last_compression_attempt_in_place", None)
        if attempt_in_place is True:
            return list(messages)
        if attempt_in_place is False:
            return None
        return previous_history
    if bool(getattr(agent, "_last_compaction_in_place", False)):
        return list(messages)
    return None


_SYNTHETIC_USER_PREFIXES = (
    "[System: Your previous response was truncated",
    "[System: The previous response was cut off",
    "[System: Your previous tool call",
    "[Your active task list was preserved across context compression]",
    "[IMPORTANT: Background process ",
)


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict)
        )
    return ""


_SYNTHETIC_USER_FLAGS = (
    "_todo_snapshot_synthetic",
    "_empty_recovery_synthetic",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
)


def _is_real_user_message(message: Any) -> bool:
    """Distinguish human intent from user-role runtime scaffolding.

    A compaction summary pinned to ``role="user"`` (the compressor flips the
    summary role to preserve alternation when the tail starts with an
    assistant message) is scaffolding too: treating it as human intent would
    short-circuit anchor restoration with a message the model is explicitly
    told NOT to act on.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if any(message.get(flag) for flag in _SYNTHETIC_USER_FLAGS):
        return False
    text = _message_text(message).strip()
    if not text:
        return False
    if text.startswith(_SYNTHETIC_USER_PREFIXES):
        return False
    from agent.context_compressor import ContextCompressor

    return not ContextCompressor._is_synthetic_compression_user_turn(message)


def _strip_stale_todo_snapshot(content: Any) -> Any:
    """Remove a previously merged todo-snapshot block from message content.

    Snapshot merges (see the injection site in ``compress_context``) always
    append the block at the end of the trailing user turn, so a surviving
    header marks stale todo state from an earlier compaction boundary.
    Stripping before re-injection keeps repeated boundaries from
    accumulating outdated snapshots (#26981).
    """
    from tools.todo_tool import TODO_INJECTION_HEADER

    if isinstance(content, str):
        idx = content.find(TODO_INJECTION_HEADER)
        if idx == -1:
            return content
        return content[:idx].rstrip()
    if isinstance(content, list):
        return [
            part
            for part in content
            if not (
                isinstance(part, dict)
                and part.get("type") == "text"
                and str(part.get("text") or "")
                .lstrip()
                .startswith(TODO_INJECTION_HEADER)
            )
        ]
    return content


def _merge_anchor_into_user_message(target: dict, anchor: dict) -> None:
    """Fold the human anchor into an existing user-role scaffolding turn.

    Used only when every insertion slot would create two consecutive
    user-role messages. The anchor text leads (it is the active task), the
    scaffolding content is preserved after it, and the synthetic flags are
    cleared because the merged turn now carries real human intent.
    """
    anchor_content = anchor.get("content")
    target_content = target.get("content")
    if isinstance(anchor_content, list) or isinstance(target_content, list):
        anchor_parts = (
            list(anchor_content)
            if isinstance(anchor_content, list)
            else [{"type": "text", "text": str(anchor_content or "")}]
        )
        target_parts = (
            list(target_content)
            if isinstance(target_content, list)
            else [{"type": "text", "text": str(target_content or "")}]
        )
        target["content"] = anchor_parts + target_parts
    else:
        merged = f"{anchor_content or ''}\n\n{target_content or ''}".strip()
        target["content"] = merged
    for flag in _SYNTHETIC_USER_FLAGS:
        target.pop(flag, None)


def _insert_real_user_anchor(messages: list, anchor: dict) -> None:
    """Insert the latest human turn without breaking role alternation."""

    def _role(msg: Any) -> Optional[str]:
        return msg.get("role") if isinstance(msg, dict) else None

    # Preferred: the summary boundary — before the first assistant message
    # not already preceded by a user turn. The left neighbour is then
    # non-user by construction and the right neighbour is an assistant.
    for index, message in enumerate(messages):
        if _role(message) != "assistant":
            continue
        previous_role = _role(messages[index - 1]) if index > 0 else None
        if previous_role != "user":
            messages.insert(index, anchor)
            return
    # Every assistant is user-preceded (or there are none). Appending is
    # safe whenever the transcript does not already end with a user turn.
    if not messages or _role(messages[-1]) != "user":
        messages.append(anchor)
        return
    # The transcript ends with a user-role message and no slot avoids
    # user/user adjacency.
    from agent.context_compressor import ContextCompressor

    if ContextCompressor._is_context_summary_content(
        _message_text(messages[-1])
    ):
        # Never merge into a compaction summary: the summary prefix must
        # stay at the start of its message for downstream summary detection.
        # Appending after it makes the anchor "the latest user message after
        # the summary" — exactly what the handoff prefix instructs — and the
        # adjacent user turns are merged summary-first by
        # repair_message_sequence before the next API call.
        messages.append(anchor)
        return
    # Trailing user-role scaffolding (e.g. the todo snapshot): merge instead
    # of inserting a consecutive same-role message (#55677 strict templates).
    _merge_anchor_into_user_message(messages[-1], anchor)


def _ensure_compressed_has_user_turn(original_messages: list, compressed: list) -> None:
    """Preserve human intent, not merely a synthetic user-role placeholder."""
    if any(_is_real_user_message(message) for message in compressed):
        return
    from agent.context_compressor import (
        COMPRESSION_CONTINUATION_USER_CONTENT,
        _fresh_compaction_message_copy,
    )

    for message in reversed(original_messages):
        if _is_real_user_message(message):
            _insert_real_user_anchor(
                compressed,
                _fresh_compaction_message_copy(message),
            )
            return
    compressed.append({
        "role": "user",
        "content": COMPRESSION_CONTINUATION_USER_CONTENT,
    })


_PENDING_CONTEXT_ENGINE_NOTIFICATION = (
    "_pending_context_engine_compression_notification"
)


def _notify_context_engine_compression_complete(
    agent: Any,
    *,
    new_session_id: str,
    old_session_id: str,
) -> bool:
    """Notify the active context engine after a durable compression commit."""
    callback = getattr(agent.context_compressor, "on_session_start", None)
    if not callable(callback):
        return False
    try:
        callback(
            new_session_id,
            boundary_reason="compression",
            old_session_id=old_session_id,
            platform=getattr(agent, "platform", None) or "cli",
            conversation_id=getattr(agent, "_gateway_session_key", None),
        )
    except Exception:
        # Context-engine hooks are observers. A callback failure must not undo
        # history that the core or an outer host transaction already committed.
        logger.debug(
            "context engine on_session_start (compression) failed",
            exc_info=True,
        )
        return False
    return True


def _queue_context_engine_compression_notification(
    agent: Any,
    *,
    new_session_id: str,
    old_session_id: str,
) -> None:
    """Stage exactly one existing hook call for an outer host transaction."""
    if callable(getattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, None)):
        raise RuntimeError("a compression notification is already pending")

    def _notify() -> bool:
        return _notify_context_engine_compression_complete(
            agent,
            new_session_id=new_session_id,
            old_session_id=old_session_id,
        )

    setattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, _notify)


def finalize_context_engine_compression_notification(
    agent: Any,
    *,
    committed: bool,
) -> bool:
    """Emit or discard a deferred notification; repeated calls are no-ops."""
    pending = getattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, None)
    setattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, None)
    if not committed or not callable(pending):
        return False
    return bool(pending())


def compress_context(
    agent: Any,
    messages: list,
    system_message: str,
    *,
    approx_tokens: Optional[int] = None,
    task_id: str = "default",
    focus_topic: Optional[str] = None,
    force: bool = False,
    trigger_reason: Optional[str] = None,
    defer_context_engine_notification: bool = False,
    commit_fence: Optional[CompressionCommitFence] = None,
) -> Tuple[list, str]:
    """Compress conversation context and split the session in SQLite.

    Args:
        agent: The owning :class:`AIAgent`.
        messages: Current message history (will be summarised).
        system_message: Current system prompt; used when compression needs a
            rebuilt cached prompt.
        approx_tokens: Pre-compression token estimate, logged for ops.
        task_id: Tool task scope (used for clearing file-read dedup state).
        focus_topic: Optional focus string for guided compression — the
            summariser will prioritise preserving information related to
            this topic.  Inspired by Claude Code's ``/compact <focus>``.
        force: If True, bypass any active summary-failure cooldown.  Set
            by the manual ``/compress`` slash command so users can retry
            immediately after an auto-compress abort.  Auto-compress
            callers use the default ``False``.
        defer_context_engine_notification: Delay the existing context-engine
            hook until a manual host commits its outer history transaction.
        commit_fence: Optional cooperative fence for executor callers that
            may time out. It prevents a late worker from mutating session state
            after its caller has moved on.

    Returns:
        ``(compressed_messages, new_system_prompt)`` tuple.  When
        compression aborts (aux LLM failed to produce a usable summary),
        returns the original messages unchanged and the existing system
        prompt — the session is NOT rotated.  Callers should detect the
        no-op via ``len(returned) == len(input)`` and stop the retry loop.
    """
    if (
        defer_context_engine_notification
        and callable(getattr(agent, _PENDING_CONTEXT_ENGINE_NOTIFICATION, None))
    ):
        raise RuntimeError("a compression notification is already pending")

    # ``conversation_history_after_compression()`` needs the latest attempt's
    # outcome, while ``_last_compaction_in_place`` remains the run-level signal
    # read by gateway callers. ``None`` means this attempt aborted or made no
    # boundary, so the previous flush baseline remains authoritative.
    agent._last_compression_attempt_recorded = True
    agent._last_compression_attempt_in_place = None
    # Clear the lock-skip signal at the VERY TOP, before the codex route and
    # the breaker gates below can early-return (per-attempt state rule,
    # #58630/#69853). A stale ``True``/holder value from a prior lock-skip
    # must never make a later breaker/codex no-op look like lock contention
    # to the automatic-path consumers (compression_deferred, #49874) — the
    # second clear before lock acquisition below stays for the same reason
    # it was added in #69870 and is simply idempotent now.
    agent._compression_skipped_due_to_lock = None

    _attempt_started_at = time.monotonic()
    _attempt_id = uuid.uuid4().hex
    _trigger_source = "manual" if force else "auto"
    try:
        agent._compression_attempt_id = _attempt_id
        setattr(agent.context_compressor, "_compression_telemetry_seed", {
            "attempt_id": _attempt_id,
            "session_id": agent.session_id or "",
            "trigger_source": _trigger_source,
        })
    except Exception:
        pass

    # Codex app-server sessions: the codex agent owns the real thread context;
    # Hermes' summarizer would only rewrite a local mirror without shrinking
    # the actual thread (#36801). Route compaction to the app server's own
    # thread/compact mechanism. Behavior is controlled by
    # ``compression.codex_app_server_auto`` (native|hermes|off).
    # The memory-provider context handoff below is intentionally Hermes-only:
    # the app server does not expose its native summary prompt, so there is no
    # truthful injection point for ``on_pre_compress()`` return text here.
    if getattr(agent, "api_mode", None) == "codex_app_server":
        _codex_fence_entered = False
        if commit_fence is not None:
            _codex_fence_entered = commit_fence.begin_commit()
            if not _codex_fence_entered:
                existing_prompt = getattr(agent, "_cached_system_prompt", None)
                if not existing_prompt:
                    existing_prompt = agent._build_system_prompt(system_message)
                return messages, existing_prompt
        try:
            return _compress_context_via_codex_app_server(
                agent,
                messages,
                system_message,
                approx_tokens=approx_tokens,
                task_id=task_id,
                force=force,
            )
        finally:
            if _codex_fence_entered:
                commit_fence.finish_commit()

    # Every automatic entrypoint must honor compressor-owned cooldown and
    # breaker state. Gateway hygiene constructs a fresh AIAgent, so the
    # persisted fallback streak is loaded by bind_session_state() before this.
    if not force:
        _refresh_persisted_compression_guards(agent.context_compressor)
        blocked = getattr(
            type(agent.context_compressor),
            "_automatic_compression_blocked",
            None,
        )
        if callable(blocked) and blocked(agent.context_compressor):
            existing_prompt = getattr(agent, "_cached_system_prompt", None)
            if not existing_prompt:
                existing_prompt = agent._build_system_prompt(system_message)
            return messages, existing_prompt

    # Lazy feasibility check — run the auxiliary-provider probe + context
    # length lookup just-in-time on the first compression attempt instead of
    # at AIAgent.__init__. Saves ~400ms cold off every short session that
    # never reaches the threshold (the vast majority of ``chat -q`` runs).
    # The check itself sets ``agent._compression_warning`` so the
    # status-callback replay machinery still emits the warning to the user
    # the first time it would matter.
    if not getattr(agent, "_compression_feasibility_checked", False):
        # Mark as checked only after the probe completes. If the check
        # raises (e.g. a fatal aux-context ValueError that aborts the
        # session), leaving the flag unset is harmless; a non-fatal
        # transient failure is swallowed inside the function so the flag
        # is set normally on the next successful pass.
        check_compression_model_feasibility(agent)
        agent._compression_feasibility_checked = True

    _pre_msg_count = len(messages)
    # Capture the provider's REAL prompt_tokens BEFORE compression runs. The
    # compaction path sets last_prompt_tokens = -1 (the "await real usage"
    # sentinel) well before the announce is rendered, so reading it at announce
    # time yields the sentinel, not the measurement. Prefer
    # last_real_prompt_tokens (the compressor's durable copy of the last real
    # reading); fall back to last_prompt_tokens for engines that do not keep one.
    # Used only for the display-side counter-divergence line — never for a gate.
    try:
        _cc_pre = getattr(agent, "context_compressor", None)
        _real_prompt_tokens_pre = int(
            getattr(_cc_pre, "last_real_prompt_tokens", 0)
            or getattr(_cc_pre, "last_prompt_tokens", 0)
            or 0
        )
        if _real_prompt_tokens_pre < 0:
            _real_prompt_tokens_pre = 0
    except Exception:
        _real_prompt_tokens_pre = 0
    # In-place compaction (config: compression.in_place, see #38763). When True,
    # this compaction rewrites the message list and refreshes the system prompt
    # when necessary, but keeps the SAME session_id — no end_session, no
    # parent_session_id child, no
    # `name #N` renumber, no contextvar/env/logging re-sync, no memory/context-
    # engine session-switch. The conversation keeps one durable id for life,
    # eliminating the session-rotation bug cluster. Default False during rollout.
    in_place = bool(getattr(agent, "compression_in_place", False))
    # Set True once the in-place DB write actually completes (the DB block can
    # raise and skip it). Surfaced to the gateway via agent._last_compaction_in_place.
    compacted_in_place = False
    # Set True when the compressor DID produce a compacted list but the DB write
    # that would persist it (rotation's child-session create, or the in-place
    # archive_and_compact) failed and was rolled back — a locked / contended
    # state.db, an FK error, ENOSPC. In that case the returned list is compacted
    # in memory but the STORE is untouched, so the next request resends the
    # original context. The gateway reads agent._last_compaction_persist_failed
    # to tell this TRANSIENT, retryable failure apart from a genuine
    # nothing-to-compress no-op (both leave session_id unchanged). See #44794.
    persist_failed = False
    # Attribution is load-bearing: a compaction whose cause is not recorded is a
    # compaction nobody can explain later, which is exactly the class of bug that
    # produced the 2026-08-07 "why did it compact at 46%?" incident. Every caller
    # passes ``trigger_reason``; a missing one is a WIRING DEFECT in a new call
    # site, so name it loudly in the log rather than letting it read as normal.
    _trigger_label = (trigger_reason or "").strip() or "UNATTRIBUTED"
    if _trigger_label == "UNATTRIBUTED":
        logger.warning(
            "context compression has no trigger_reason (session=%s) — a caller "
            "is not passing one; every compaction must name its arm",
            agent.session_id or "none",
        )
    logger.info(
        "context compression started: session=%s trigger=%s messages=%d "
        "tokens=~%s model=%s focus=%r",
        agent.session_id or "none", _trigger_label, _pre_msg_count,
        f"{approx_tokens:,}" if approx_tokens else "unknown", agent.model,
        focus_topic,
    )
    _compaction_status = COMPACTION_STATUS
    if not force:
        _compaction_status = automatic_compaction_status_message(
            agent.context_compressor,
            phase="compress",
            default_message=_compaction_status,
            approx_tokens=approx_tokens,
            message_count=_pre_msg_count,
            model=agent.model,
            focus_topic=focus_topic,
        )
    _compaction_status_emitted = bool(_compaction_status)
    if _compaction_status:
        agent._emit_status(_compaction_status)
    _compaction_done_emitted = False

    def _complete_compaction_lifecycle() -> None:
        nonlocal _compaction_done_emitted
        if _compaction_done_emitted:
            return
        _compaction_done_emitted = True
        # A suppressed start (quiet context engine) opened no visible
        # compaction phase — emit no terminal edge either. Failure warnings
        # go through agent._emit_warning and are never suppressed here.
        if _compaction_status_emitted:
            _emit_compaction_done(agent)

    # ── Compression lock ────────────────────────────────────────────────
    # Atomic, state.db-backed lock per session_id.  Without this, two
    # AIAgent instances that share the same session_id (most commonly the
    # parent-turn agent and its background-review fork — see
    # ``agent/background_review.py``: ``review_agent.session_id =
    # agent.session_id``) can each call compress() on overlapping
    # snapshots of the same conversation.  Both succeed, both rotate
    # ``agent.session_id`` to a fresh id, both create child sessions in
    # state.db parented to the same old id.  The gateway's SessionEntry
    # only catches one rotation, so the other child becomes an orphan
    # that silently accumulates writes — Damien's repro shape.
    #
    # Acquire keyed on the OLD session_id (the rotation target's parent),
    # because that's the id that competing paths see and read from
    # SessionEntry at the start of their own compression attempt.
    #
    # If we can't acquire the lock, another path is mid-compression on
    # this session.  Aborting is correct: the messages are unchanged, the
    # other path's rotation will produce the canonical new session_id,
    # and our caller's auto-compress loop sees ``len(returned) == len(input)``
    # and stops retrying for this cycle. The session is NOT corrupted —
    # we just sit out this round and let the winner finish.
    _lock_db = getattr(agent, "_session_db", None)
    _lock_sid = agent.session_id or ""
    _lock_holder: Optional[str] = None
    # Probe whether the lock subsystem is actually available on this
    # SessionDB instance. A process running mismatched module versions can have
    # this call site while its long-lived SessionDB instance predates the lock
    # API. Only that structural absence is safe to fail open for: compression
    # must make progress rather than spin forever after an update. Once the
    # method has been resolved, every exception from its implementation fails
    # closed because proceeding without a lock can fork the session lineage.
    _try_acquire_lock = None
    _lock_lookup_error: Optional[Exception] = None
    _legacy_session_db_without_lock_api = False
    # Clear any stale lock-skip signal from a prior call so this call's
    # outcome alone determines what callers see.  Without this an
    # auto-compress lock-skip followed by a successful manual /compress
    # would falsely report "Compression already in progress" and discard
    # the compression results.
    agent._compression_skipped_due_to_lock = None
    if _lock_db is not None:
        try:
            _legacy_session_db_without_lock_api = _lock_api_is_absent_on_session_db(
                _lock_db
            )
        except Exception as exc:
            _lock_lookup_error = exc
        if _lock_lookup_error is None and not _legacy_session_db_without_lock_api:
            try:
                _try_acquire_lock = _lock_db.try_acquire_compression_lock
                if not callable(_try_acquire_lock):
                    _lock_lookup_error = TypeError(
                        "compression lock API is present but not callable"
                    )
            except Exception as exc:
                _lock_lookup_error = exc
    try:
        _lock_ttl = float(getattr(agent, "_compression_lock_ttl_seconds", 300.0) or 300.0)
    except (TypeError, ValueError):
        _lock_ttl = 300.0
    _lock_refresh_interval = getattr(agent, "_compression_lock_refresh_interval", None)
    _lock_refresher: Optional[_CompressionLockLeaseRefresher] = None
    if _lock_db is not None and _lock_sid:
        _lock_holder = _compression_lock_holder(agent)
        if _lock_lookup_error is not None:
            # Attribute lookup itself failed for a reason other than a missing
            # lock API. It is unsafe to proceed without a lock in that case.
            _lock_holder = None
            logger.warning(
                "compression lock lookup raised unexpectedly for session=%s "
                "(%s: %s) — skipping compression this cycle",
                _lock_sid, type(_lock_lookup_error).__name__, _lock_lookup_error,
            )
            _lock_acquired = False
        elif _try_acquire_lock is None:
            # The lock API itself is absent on this in-memory instance. Log once
            # and proceed unlocked so an update-version skew cannot leave the
            # outer auto-compression loop making no progress forever.
            _lock_holder = None
            if getattr(agent, "_last_compression_lock_error_sid", None) != _lock_sid:
                agent._last_compression_lock_error_sid = _lock_sid
                logger.warning(
                    "compression lock subsystem unavailable for session=%s "
                    "— proceeding without lock. This usually means a stale "
                    "in-memory module after an update; restart the process "
                    "(or `hermes update`) to resync.",
                    _lock_sid,
                )
            _lock_acquired = True  # acquired-but-unlocked compatibility path
        else:
            try:
                _lock_acquired = _try_acquire_lock(
                    _lock_sid, _lock_holder, ttl_seconds=_lock_ttl
                )
            except Exception as _lock_err:
                # The method exists and entered its implementation but failed.
                # Do not mistake an internal AttributeError or TypeError for
                # version skew: fail closed and preserve session lineage. A
                # failure after SQLite committed the acquire can leave our
                # holder row behind, so release it best-effort before returning
                # unchanged messages; release is holder-qualified and safe when
                # acquisition never succeeded.
                try:
                    _lock_db.release_compression_lock(_lock_sid, _lock_holder)
                except Exception as _release_err:
                    logger.debug(
                        "compression lock cleanup after failed acquire failed: %s",
                        _release_err,
                    )
                _lock_holder = None
                logger.warning(
                    "compression lock acquisition raised unexpectedly for "
                    "session=%s (%s: %s) — skipping compression this cycle",
                    _lock_sid, type(_lock_err).__name__, _lock_err,
                )
                _lock_acquired = False
        if not _lock_acquired:
            try:
                existing = _lock_db.get_compression_lock_holder(_lock_sid)
            except Exception:
                existing = None
            logger.warning(
                "compression skipped: another path is compressing session=%s "
                "(holder=%s) — returning messages unchanged to avoid session fork",
                _lock_sid, existing,
            )
            _lock_holder = None  # don't release a lock we don't own
            # Signal to callers that this no-op is due to a concurrent lock,
            # not a genuine "nothing to compress" or aux-model failure.
            # Manual /compress callers can surface a clear status message
            # instead of the misleading "No changes from compression" text.
            agent._compression_skipped_due_to_lock = existing or True
            # Surface to the user once — quiet for downstream auto-compress loops
            if getattr(agent, "_last_compression_lock_warning_sid", None) != _lock_sid:
                agent._last_compression_lock_warning_sid = _lock_sid
                try:
                    agent._emit_warning(
                        "⚠ Skipping concurrent compression — another path "
                        "is already compressing this session. Will retry "
                        "after it finishes."
                    )
                except Exception:
                    pass
            _existing_sp = getattr(agent, "_cached_system_prompt", None)
            if not _existing_sp:
                _existing_sp = agent._build_system_prompt(system_message)
            try:
                if hasattr(agent.context_compressor, "_begin_compression_telemetry"):
                    agent.context_compressor._begin_compression_telemetry(current_tokens=approx_tokens)
            except Exception:
                pass
            _emit_compression_attempt_telemetry(
                agent,
                started_at=_attempt_started_at,
                commit_status="aborted",
                split_status="aborted",
                failure_class="lock_contended",
            )
            _complete_compaction_lifecycle()
            return messages, _existing_sp
    _lock_released = False

    def _release_lock() -> None:
        """Release the lock keyed on the OLD session_id (before rotation)."""
        nonlocal _lock_released
        _complete_compaction_lifecycle()
        if _lock_released:
            return
        _lock_released = True
        if _lock_refresher is not None:
            try:
                _lock_refresher.stop()
            except Exception as _stop_err:
                logger.debug("compression lock refresher stop failed: %s", _stop_err)
        if _lock_db is not None and _lock_sid and _lock_holder:
            try:
                _lock_db.release_compression_lock(_lock_sid, _lock_holder)
            except Exception as _rel_err:
                logger.debug("compression lock release failed: %s", _rel_err)

    # A delayed contender can acquire the parent lock after the winning path
    # has released it and completed rotation. The lock serializes work but does
    # not by itself prove that this stale agent still owns a live parent.
    if _lock_db is not None and _lock_sid:
        try:
            _parent_already_rotated = _session_was_rotated_by_compression(
                _lock_db, _lock_sid
            )
        except Exception as _session_err:
            logger.warning(
                "compression session ownership lookup failed for session=%s "
                "(%s: %s) - skipping compression this cycle",
                _lock_sid,
                type(_session_err).__name__,
                _session_err,
            )
            _release_lock()
            _existing_sp = getattr(agent, "_cached_system_prompt", None)
            if not _existing_sp:
                _existing_sp = agent._build_system_prompt(system_message)
            return messages, _existing_sp
        if _parent_already_rotated:
            logger.info(
                "compression skipped: session=%s was already rotated by "
                "another compression path",
                _lock_sid,
            )
            _release_lock()
            _existing_sp = getattr(agent, "_cached_system_prompt", None)
            if not _existing_sp:
                _existing_sp = agent._build_system_prompt(system_message)
            return messages, _existing_sp

    # The agent may have been constructed before another path completed an
    # in-place compaction on the same session. Re-read durable breaker state
    # after acquiring the session lock so this final gate cannot act on the
    # stale snapshot loaded by bind_session_state().
    if not force:
        compressor = agent.context_compressor
        _refresh_persisted_compression_guards(compressor)
        blocked = getattr(
            type(compressor),
            "_automatic_compression_blocked",
            None,
        )
        if callable(blocked) and blocked(compressor):
            _release_lock()
            existing_prompt = getattr(agent, "_cached_system_prompt", None)
            if not existing_prompt:
                existing_prompt = agent._build_system_prompt(system_message)
            return messages, existing_prompt

    _activity_heartbeat: Optional[_CompressionActivityHeartbeat] = None
    try:
        if _lock_holder is not None:
            _lock_refresher = _CompressionLockLeaseRefresher(
                _lock_db,
                _lock_sid,
                _lock_holder,
                _lock_ttl,
                _lock_refresh_interval,
            )
            _lock_refresher.start()

        # Notify external memory provider before compression discards context.
        # The provider's on_pre_compress() may return a string of insights it
        # wants surfaced inside the compression summary; capture and forward it
        # instead of silently discarding the provider's return value.
        memory_context = ""
        if agent._memory_manager:
            try:
                _maybe_ctx = agent._memory_manager.on_pre_compress(messages)
                if isinstance(_maybe_ctx, str):
                    memory_context = sanitize_memory_context(_maybe_ctx)
            except Exception:
                pass

        compress_fn = agent.context_compressor.compress
        compress_kwargs = _supported_compression_kwargs(
            compress_fn,
            current_tokens=approx_tokens,
            focus_topic=focus_topic,
            force=force,
            memory_context=memory_context,
        )
        if memory_context.strip() and "memory_context" not in compress_kwargs:
            engine_name = getattr(
                agent.context_compressor,
                "name",
                type(agent.context_compressor).__name__,
            )
            if (
                getattr(agent, "_last_memory_context_unsupported_engine", None)
                != engine_name
            ):
                agent._last_memory_context_unsupported_engine = engine_name
                logger.warning(
                    "context engine %s does not accept memory_context; continuing "
                    "without provider-supplied summary context",
                    engine_name,
                )

        messages_before_compression = copy.deepcopy(messages)
        _activity_heartbeat = _CompressionActivityHeartbeat(agent).start()
        try:
            compressed = compress_fn(messages, **compress_kwargs)
        except TypeError:
            # Strict-signature context engine (or a wrapper/mock whose
            # signature inspection could not predict the rejection) that does
            # NOT accept focus_topic / force / memory_context — fall back to
            # the oldest documented call shape (current_tokens only). Fork
            # behavior preserved on top of upstream's signature-filtering:
            # the filter handles inspectable plugins, this catches the rest
            # (#69870 lock refresher must still stop on the fallback path).
            #
            # BUT: when the signature was fully inspectable AND declared no
            # **kwargs catch-all, the filter PROVED every kwarg we passed
            # binds — so this TypeError came from INSIDE the engine, not from
            # argument binding. Retrying would execute a stateful compressor a
            # second time (the hazard _supported_compression_kwargs exists to
            # prevent). Propagate it untouched; the outer BaseException handler
            # still releases the lock and stops the refresher.
            if _compression_kwargs_are_signature_proven(compress_fn):
                raise
            compressed = compress_fn(messages, current_tokens=approx_tokens)
    except BaseException as _compress_exc:
        # ANY exception after lock acquisition — memory hook, capability
        # inspection, engine lookup, or compress() — must release the lock so
        # the session isn't permanently blocked from future compression.
        if _activity_heartbeat is not None:
            _activity_heartbeat.stop("context compression failed")
            _activity_heartbeat = None
        _release_lock()
        _emit_compression_attempt_telemetry(
            agent,
            started_at=_attempt_started_at,
            commit_status="aborted",
            split_status="aborted",
            failure_class=f"exception:{type(_compress_exc).__name__}",
        )
        raise
    finally:
        if _activity_heartbeat is not None:
            _activity_heartbeat.stop("context compression completed")

    _commit_fence_entered = False
    try:
        # Capture boundary quality before session-rotation callbacks run. Built-in
        # and plugin lifecycle hooks may reset per-session compressor fields while
        # rebinding to the child id; the completed attempt's verdict must survive
        # that rebind and be recorded only after the full boundary commits.
        _compression_made_progress = bool(
            getattr(agent.context_compressor, "_last_compression_made_progress", False)
        )
        _compression_used_fallback = bool(
            getattr(agent.context_compressor, "_last_summary_fallback_used", False)
        )

        # If compression aborted (aux LLM failed to produce a usable summary)
        # the compressor returns the input messages unchanged.  Surface the
        # error to the user, skip the session-rotation work entirely (no
        # session has logically ended), and let auto-compress callers detect
        # the no-op via len(returned) == len(input).
        if getattr(agent.context_compressor, "_last_compress_aborted", False):
            try:
                _err = getattr(agent.context_compressor, "_last_summary_error", None) or "unknown error"
                if getattr(agent, "_last_compression_summary_warning", None) != _err:
                    agent._last_compression_summary_warning = _err
                    agent._emit_warning(
                        f"⚠ Compression aborted: {_err}. "
                        "No messages were dropped — conversation continues unchanged. "
                        "Run /compress to retry, or /new to start a fresh session."
                    )
                _existing_sp = getattr(agent, "_cached_system_prompt", None)
                if not _existing_sp:
                    _existing_sp = agent._build_system_prompt(system_message)
                _emit_compression_attempt_telemetry(
                    agent,
                    started_at=_attempt_started_at,
                    commit_status="aborted",
                    split_status="aborted",
                    failure_class=(
                        getattr(agent.context_compressor, "_last_summary_error", None)
                        and "summary_generation_aborted"
                    ),
                )
                return messages, _existing_sp
            finally:
                _release_lock()

        # Compare against the pre-dispatch semantic state, not object identity:
        # legacy/plugin engines may return an equal copy for a no-op, or mutate
        # the live list while returning an unchanged snapshot. Neither case may
        # rotate or rewrite the session.
        if compressed == messages_before_compression:
            if messages != messages_before_compression:
                messages[:] = copy.deepcopy(messages_before_compression)
            logger.info(
                "Compression made no progress (session=%s) — skipping boundary rewrite.",
                agent.session_id or "none",
            )
            _existing_sp = getattr(agent, "_cached_system_prompt", None)
            if not _existing_sp:
                _existing_sp = agent._build_system_prompt(system_message)
            _emit_compression_attempt_telemetry(
                agent,
                started_at=_attempt_started_at,
                commit_status="aborted",
                split_status="aborted",
                failure_class="no_progress",
            )
            _release_lock()
            return messages, _existing_sp

        if not compressed:
            logger.error(
                "context compression returned an empty transcript; refusing to "
                "rotate session=%s so the parent remains resumable",
                agent.session_id or "none",
            )
            try:
                agent._emit_warning(
                    "⚠ Compression returned an empty transcript. "
                    "No session split was performed; conversation continues unchanged."
                )
            except Exception:
                pass
            _existing_sp = getattr(agent, "_cached_system_prompt", None)
            if not _existing_sp:
                _existing_sp = agent._build_system_prompt(system_message)
            _release_lock()
            return messages, _existing_sp

        if commit_fence is not None:
            _commit_fence_entered = commit_fence.begin_commit()
            if not _commit_fence_entered:
                logger.info(
                    "Compression commit cancelled before session mutation "
                    "(session=%s).",
                    agent.session_id or "none",
                )
                agent._last_compaction_in_place = False
                _existing_sp = getattr(agent, "_cached_system_prompt", None)
                if not _existing_sp:
                    _existing_sp = agent._build_system_prompt(system_message)
                _emit_compression_attempt_telemetry(
                    agent,
                    started_at=_attempt_started_at,
                    commit_status="aborted",
                    split_status="aborted",
                    failure_class="commit_fence_cancelled",
                )
                _release_lock()
                return messages, _existing_sp

        summary_error = getattr(agent.context_compressor, "_last_summary_error", None)
        if summary_error:
            if getattr(agent, "_last_compression_summary_warning", None) != summary_error:
                agent._last_compression_summary_warning = summary_error
                agent._emit_warning(
                    f"⚠ Compression summary failed: {summary_error}. "
                    "Inserted a fallback context marker."
                )
        else:
            # No hard failure — but did the configured aux model error out
            # and get recovered by retrying on main?  Surface that so users
            # know their auxiliary.compression.model setting is broken even
            # though compression succeeded.
            _aux_fail_model = getattr(agent.context_compressor, "_last_aux_model_failure_model", None)
            _aux_fail_err = getattr(agent.context_compressor, "_last_aux_model_failure_error", None)
            if _aux_fail_model:
                # Dedup on (model, error) so we don't spam on every compaction
                _aux_key = (_aux_fail_model, _aux_fail_err)
                if getattr(agent, "_last_aux_fallback_warning_key", None) != _aux_key:
                    agent._last_aux_fallback_warning_key = _aux_key
                    agent._emit_warning(
                        f"ℹ Configured compression model '{_aux_fail_model}' failed "
                        f"({_aux_fail_err or 'unknown error'}). Recovered using main model — "
                        "check auxiliary.compression.model in config.yaml."
                    )

        todo_snapshot = agent._todo_store.format_for_injection()
        if todo_snapshot:
            # Fold the snapshot into a trailing REAL user message so
            # compression never introduces a synthetic user/user pair. Any
            # snapshot merged at an earlier boundary is stripped first so
            # repeated compactions refresh rather than accumulate todo state
            # (#26981). Scaffolding tails (continuation marker, summary
            # handoff, a bare stale snapshot row) must never absorb the
            # snapshot: merging would upgrade them to "real user" evidence
            # and break zero-user provenance (#69292), so those keep the
            # flagged standalone append and the real-user preservation pass
            # continues to see todo scaffolding, not human intent.
            from agent.context_compressor import _append_text_to_content

            merged = False
            _tail = (
                compressed[-1]
                if compressed and isinstance(compressed[-1], dict)
                else None
            )
            if _tail is not None and _tail.get("role") == "user":
                _stripped = _strip_stale_todo_snapshot(_tail.get("content"))
                _probe = {
                    key: value for key, value in _tail.items() if key != "content"
                }
                _probe["content"] = _stripped
                if _is_real_user_message(_probe):
                    _snapshot_text = (
                        f"\n\n{todo_snapshot}"
                        if isinstance(_stripped, str) and _stripped
                        else todo_snapshot
                    )
                    _tail["content"] = _append_text_to_content(
                        _stripped, _snapshot_text
                    )
                    merged = True
                elif _stripped != _tail.get("content") and not _message_text(
                    {"role": "user", "content": _stripped}
                ).strip():
                    # The tail was nothing but an earlier snapshot row —
                    # refresh it in place instead of stacking a duplicate.
                    _tail["content"] = todo_snapshot
                    _tail["_todo_snapshot_synthetic"] = True
                    merged = True
            if not merged:
                compressed.append({
                    "role": "user",
                    "content": todo_snapshot,
                    "_todo_snapshot_synthetic": True,
                })
        _ensure_compressed_has_user_turn(messages, compressed)

        cached_system_prompt = agent._cached_system_prompt
        agent._invalidate_system_prompt()

        # Built-in memory is the only system-prompt input that a normal
        # compaction reloads. When the cached prompt already embeds the
        # freshly-reloaded memory blocks verbatim, keep the exact cached
        # prompt so local backends retain their KV-cache prefix. Containment
        # (not before/after snapshot equality) is required: fresh-agent
        # surfaces restore the cached prompt from the session DB, where it
        # can predate mid-session memory writes the in-memory snapshot has
        # already absorbed. External providers can change their own prompt
        # block during on_pre_compress(), so they retain the rebuild path.
        if (
            cached_system_prompt is not None
            and getattr(agent, "_memory_manager", None) is None
            and _cached_prompt_reflects_builtin_memory(agent, cached_system_prompt)
        ):
            new_system_prompt = cached_system_prompt
            agent._cached_system_prompt = cached_system_prompt
        else:
            new_system_prompt = agent._build_system_prompt(system_message)
            agent._cached_system_prompt = new_system_prompt

        _session_commit_succeeded = False
        split_status = "not_applicable"
        if agent._session_db:
            split_status = "pending"
            try:
                # Trigger memory extraction on the current session before the
                # transcript is rewritten (runs in BOTH modes — the logical
                # conversation's pre-compaction turns are about to be summarized
                # away regardless of whether the id rotates).
                agent.commit_memory_session(messages)

                if in_place:
                    # ── In-place compaction: keep the same session_id ──────────
                    # No end_session, no new row, no parent_session_id, no title
                    # renumber, no contextvar/env/logging re-sync. The session's
                    # id, title, cwd, /goal, and gateway routing all stay put.
                    #
                    # Durable, NON-DESTRUCTIVE replace: soft-archive the
                    # pre-compaction turns (active=0, kept on disk + FTS-searchable +
                    # recoverable) and insert `compressed` as the new live (active=1)
                    # set, atomically. `compressed` already carries the surviving
                    # tail (current-turn messages the compressor kept via
                    # protect_last_n), so we DON'T pre-flush here — a flush would
                    # INSERT current-turn rows that archive_and_compact would then
                    # archive alongside the rest (harmless but wasted writes). The
                    # live-context load filters active=1, so a resume reloads ONLY
                    # the compacted set; the original turns remain under the SAME id
                    # for search/recovery (Teknium review — keep one durable id
                    # WITHOUT destroying history, unlike a hard replace_messages).
                    # See #38763.
                    agent._session_db.archive_and_compact(agent.session_id, compressed)
                    split_status = "in_place_committed"
                    # Reset the flush identity set so the next turn's appends are
                    # diffed against the COMPACTED transcript: the compacted dicts
                    # are passed as conversation_history next turn and skipped by
                    # identity, so only genuinely new turn messages get appended
                    # (no dup of the summary, no resurrection of dropped turns).
                    agent._flushed_db_message_ids = set()
                    # Rotation-independent signal: the conversation was compacted in
                    # place (id unchanged). The gateway reads this (NOT an id-change
                    # diff) to re-baseline transcript handling.
                    compacted_in_place = True
                else:
                    # ── Rotation (legacy): end this session, fork a continuation ─
                    # Flush any un-persisted current-turn messages to the OLD
                    # session before ending it, so they survive in the preserved
                    # parent transcript (#47202). (In-place skips this — see above.)
                    #
                    # Pass the already-durable prefix as conversation_history so
                    # the flush skips it by identity (#68196). Preflight
                    # compression runs BEFORE the normal turn flush has stamped
                    # the cold-resumed history dicts with _DB_PERSISTED_MARKER, so
                    # without a boundary _flush_messages_to_session_db treats every
                    # restored row as new and re-appends the whole transcript to
                    # the parent. turn_context anchors _persist_user_message_idx at
                    # the current-turn user message before preflight runs, so
                    # messages[:idx] is exactly the persisted prefix; only the
                    # current turn's new messages get written.
                    current_idx = getattr(agent, "_persist_user_message_idx", None)
                    persisted_history = (
                        messages[:current_idx]
                        if isinstance(current_idx, int)
                        and 0 <= current_idx <= len(messages)
                        else None
                    )
                    try:
                        agent._flush_messages_to_session_db(
                            messages,
                            conversation_history=persisted_history,
                        )
                    except Exception:
                        pass  # best-effort — don't block compression on a flush error
                    # Propagate title to the new session with auto-numbering
                    old_title = agent._session_db.get_session_title(agent.session_id)
                    agent._session_db.end_session(agent.session_id, "compression")
                    old_session_id = agent.session_id
                    agent.session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                    # Ordering contract: the agent thread updates the contextvar here;
                    # the gateway propagates to SessionEntry after run_in_executor returns.
                    try:
                        from gateway.session_context import set_current_session_id

                        set_current_session_id(agent.session_id)
                    except Exception:
                        os.environ["HERMES_SESSION_ID"] = agent.session_id
                    # The gateway/tools session context (ContextVar + env) and the
                    # logging session context are SEPARATE mechanisms. The call above
                    # moves the former; the ``[session_id]`` tag on log lines comes
                    # from ``hermes_logging._session_context`` (set once per turn in
                    # conversation_loop.py). Without this, post-rotation log lines in
                    # the same turn keep the STALE old id while the message/DB/gateway
                    # state carry the new one — breaking log correlation exactly at the
                    # compaction boundary (see #34089). Guarded separately so a logging
                    # failure can never regress the routing update above.
                    try:
                        from hermes_logging import set_session_context

                        set_session_context(agent.session_id)
                    except Exception:
                        pass
                    agent._session_db_created = False
                    try:
                        agent._session_db.create_session(
                            session_id=agent.session_id,
                            source=agent.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
                            model=agent.model,
                            model_config=agent._session_init_model_config,
                            parent_session_id=old_session_id,
                        )
                    except Exception as _cs_err:
                        # The child row could not be created (e.g. FK constraint,
                        # contended write). Previously the outer handler simply
                        # warned and let the agent continue on the NEW id — which
                        # has no row in state.db, producing an orphan: the parent
                        # is ended, the child is never indexed, and every
                        # subsequent message is attributed to a session that
                        # doesn't exist (#33906/#33907). Roll the live id back to
                        # the parent so the conversation stays attached to a real,
                        # indexed session instead of a phantom.
                        logger.warning(
                            "Compression child session create failed (%s) — "
                            "rolling back to parent session %s to avoid an orphan.",
                            _cs_err, old_session_id,
                        )
                        agent.session_id = old_session_id
                        # The compacted list exists in memory but was NOT
                        # persisted (we rolled back to the parent). Mark it so
                        # the gateway reports a retryable save failure instead
                        # of a benign "No changes" no-op (#44794).
                        persist_failed = True
                        try:
                            from gateway.session_context import set_current_session_id
                            set_current_session_id(agent.session_id)
                        except Exception:
                            os.environ["HERMES_SESSION_ID"] = agent.session_id
                        try:
                            from hermes_logging import set_session_context
                            set_session_context(agent.session_id)
                        except Exception:
                            pass
                        # Re-open the parent: it was ended above, but we're
                        # continuing on it, so it must not stay closed.
                        try:
                            agent._session_db.reopen_session(old_session_id)
                        except Exception:
                            pass
                        old_session_id = None  # no rotation happened
                        # The parent row already exists in state.db, so mark the
                        # session as created — _ensure_db_session would otherwise
                        # retry a (harmless INSERT OR IGNORE) create next turn.
                        agent._session_db_created = True
                        raise
                    agent._session_db_created = True
                    split_status = "rotated_committed"
                    # Carry a persistent /goal onto the continuation session.
                    # Compression mints a fresh child id; load_goal does a flat
                    # per-session lookup with no parent walk, so without this an
                    # active goal silently dies at the boundary (#33618).
                    try:
                        from hermes_cli.goals import migrate_goal_to_session
                        migrate_goal_to_session(old_session_id, agent.session_id, reason="compression")
                    except Exception as _goal_err:
                        logger.debug("Could not migrate goal on compression: %s", _goal_err)
                    # Auto-number the title for the continuation session
                    if old_title:
                        try:
                            new_title = agent._session_db.get_next_title_in_lineage(old_title)
                            agent._session_db.set_session_title(agent.session_id, new_title)
                        except (ValueError, Exception) as e:
                            logger.debug("Could not propagate title on compression: %s", e)

                # Shared post-write steps (both modes target agent.session_id, which
                # in-place keeps and rotation has already reassigned to the new id):
                # refresh the stored system prompt and reset the flush cursor so the
                # next turn re-bases its append diff.
                agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
                if in_place:
                    agent._last_flushed_db_idx = 0
                else:
                    # A headless turn can be killed before its finalizer. Persist
                    # the rotated child's compacted handoff at the boundary so
                    # the new session is immediately resumable.
                    agent._session_db.replace_messages(agent.session_id, compressed)
                    agent._last_flushed_db_idx = len(compressed)
                    agent._flushed_db_message_session_id = agent.session_id
                    agent._flushed_db_message_ids = {
                        id(message)
                        for message in compressed
                        if isinstance(message, dict)
                    }
                _session_commit_succeeded = True
            except Exception as e:
                split_status = "aborted" if locals().get("old_session_id") is None and not in_place else "failed_not_indexed"
                # If the rotation rolled back to the parent (orphan-avoidance
                # above), agent.session_id is the still-indexed parent and
                # old_session_id was cleared — so this is recovery, not an
                # un-indexed orphan. Otherwise an earlier step failed before the
                # child was created and the warning's original meaning holds.
                # Either way the DB persist did not complete cleanly: the
                # in-memory `compressed` list was not written to the store, so
                # flag it for the gateway's retryable-failure message (#44794).
                # Exception: a genuine in-place success sets compacted_in_place
                # BEFORE any post-write step that could raise here — don't
                # override that (the archive already landed; a later
                # bookkeeping failure is non-fatal to persistence).
                if not compacted_in_place:
                    persist_failed = True
                if locals().get("old_session_id") is None and not in_place:
                    logger.warning(
                        "Compression rotation aborted and rolled back to the "
                        "parent session (%s): %s", agent.session_id or "?", e,
                    )
                else:
                    logger.warning("Session DB compression split failed — new session will NOT be indexed: %s", e)

        # Compaction-boundary bookkeeping, computed once. `old_session_id` is only
        # bound in the rotation branch; in-place leaves it unset. `_boundary_parent`
        # is the id the boundary notifications attribute the prior state to: the old
        # id on rotation, the (unchanged) current id in-place.
        _old_sid = locals().get("old_session_id")
        _is_boundary = bool(_old_sid) or in_place
        _context_engine_boundary_committed = _session_commit_succeeded and (
            bool(_old_sid) or compacted_in_place
        )
        _boundary_parent = _old_sid or agent.session_id or ""

        # Notify the context engine that a compaction boundary occurred. Plugin
        # engines (e.g. hermes-lcm) use boundary_reason="compression" to preserve
        # DAG lineage / checkpoint per-session state across the boundary instead of
        # re-initializing fresh. See hermes-lcm#68. Built-in ContextCompressor
        # ignores kwargs. Fires in BOTH modes: rotation passes old→new ids; in-place
        # passes the SAME id (the boundary is real even though the id didn't move).
        if _context_engine_boundary_committed:
            if defer_context_engine_notification:
                _queue_context_engine_compression_notification(
                    agent,
                    new_session_id=agent.session_id or "",
                    old_session_id=_boundary_parent,
                )
            else:
                _notify_context_engine_compression_complete(
                    agent,
                    new_session_id=agent.session_id or "",
                    old_session_id=_boundary_parent,
                )

        # Notify memory providers of the compaction boundary so provider-cached
        # per-session state (Hindsight's _document_id, accumulated turn buffers,
        # counters) refreshes. reset=False because the logical conversation
        # continues. See #6672. Fires in BOTH modes: in-place uses the same id as
        # parent (the conversation didn't fork, but the buffer must still be told
        # the transcript was compacted so it doesn't double-count dropped turns).
        try:
            if _is_boundary and agent._memory_manager:
                agent._memory_manager.on_session_switch(
                    agent.session_id or "",
                    parent_session_id=_boundary_parent,
                    reset=False,
                    reason="compression",
                )
        except Exception as _me_err:
            logger.debug("memory manager on_session_switch (compression): %s", _me_err)

        # Keep the post-compression rough estimate for diagnostics, but do not
        # treat it as provider-reported prompt usage. Schema-heavy rough estimates
        # can remain above threshold even after the next real API request fits.
        _compressed_est = estimate_request_tokens_rough(
            compressed,
            system_prompt=new_system_prompt or "",
            tools=agent.tools or None,
        )

        # Record the anti-thrash effectiveness verdict at the REQUEST level (the
        # same level should_compress uses), apples-to-apples: pre = the request-level
        # estimate of the messages that triggered this compaction, post =
        # _compressed_est. This is the SINGLE owner of the counter for the normal
        # compaction path and fixes the 2026-06-19 thrash where compress()'s
        # messages-only verdict reset the counter on every pass (the
        # 205,072 -> 297,723 "tokens went UP" case). Use the pre-compaction request
        # estimate of the ORIGINAL messages so a failed/placeholder summary that
        # leaves the request over threshold is correctly counted as ineffective.
        try:
            _pre_request_est = estimate_request_tokens_rough(
                messages,
                system_prompt=system_message or "",
                tools=agent.tools or None,
            )
            if hasattr(agent.context_compressor, "record_compaction_effectiveness"):
                agent.context_compressor.record_compaction_effectiveness(
                    pre_request_tokens=_pre_request_est,
                    post_request_tokens=_compressed_est,
                )
        except Exception as _eff_err:
            logger.debug("record_compaction_effectiveness failed: %s", _eff_err)

        agent.context_compressor.last_compression_rough_tokens = _compressed_est
        agent.context_compressor.last_prompt_tokens = -1
        agent.context_compressor.last_completion_tokens = 0
        agent.context_compressor.awaiting_real_usage_after_compression = True

        # Warn on repeated compressions (quality degrades with each pass).
        # Route through _emit_status (like the other compression warnings above)
        # so the warning reaches the TUI / Telegram / Discord via status_callback,
        # not just CLI stdout. _emit_status still _vprints for the CLI, and
        # storing it on _compression_warning lets replay_compression_warning
        # re-deliver it once a late-bound gateway status_callback is wired (#36908).
        _cc = agent.context_compressor.compression_count
        if _cc >= 2:
            _cc_msg = (
                f"{agent.log_prefix}⚠️  Session compressed {_cc} times — "
                f"accuracy may degrade. Consider /new to start fresh."
            )
            agent._compression_warning = _cc_msg
            agent._emit_status(_cc_msg)

        # Emit session:compress event so hooks (e.g. MemPalace sync) can ingest
        # the completed old session before its details are lost. In in-place mode
        # there is no old id (same session); ``in_place=True`` tells hooks the
        # transcript was compacted on the same id rather than rotated.
        if getattr(agent, "event_callback", None):
            try:
                agent.event_callback("session:compress", {
                    "platform": agent.platform or "",
                    "session_id": agent.session_id,
                    "old_session_id": _old_sid or "",
                    "in_place": in_place,
                    "compression_count": agent.context_compressor.compression_count,
                })
            except Exception as e:
                logger.debug("event_callback error on session:compress: %s", e)

        logger.info(
            "context compression done: session=%s messages=%d->%d rough_tokens=~%s awaiting_real_usage=true",
            agent.session_id or "none", _pre_msg_count, len(compressed),
            f"{_compressed_est:,}",
        )

        # ── In-chat compaction announce (engine-aware; additive to fallback) ──────
        # Emitted out-of-band via _emit_status (persistent chat line, never injected
        # into model history). Engine-correct recovery reference: built-in → session
        # pointer; LCM → lossless-store + lcm_grep/lcm_expand guidance. Gating is the
        # allow-list in _format_compaction_announce. Runs INSIDE the lock hold (the
        # _release_lock() below) so the dedupe read-then-write is serialized.
        try:
            _cc = agent.context_compressor
            _engine_name = getattr(_cc, "name", None)
            _status = getattr(_cc, "_last_compression_status", None)
            _old_sid = locals().get("old_session_id")
            _new_sid = agent.session_id
            if _engine_name == "lcm":
                _dedupe_key = ("lcm", getattr(_cc, "compression_count", None))
            else:
                _dedupe_key = ("builtin", (_old_sid, _new_sid))
            _now_mono = time.monotonic()
            _after_fb, _win_from, _win_to = _compaction_after_fallback(
                agent,
                now_monotonic=_now_mono,
                current_turn_id=getattr(agent, "_current_turn_id", None),
            )
            # Granular stats (in-turn population = the whole messages list; cleared=0).
            # Built inside try/except; validate()+degrade so a reconcile bug never
            # ships wrong math or breaks the turn. Guarded by hasattr so built-in /
            # overflow / manual paths (no LCM marker shape) simply degrade.
            #
            # P1 render gate (spec 2026-07-02, D-1/§5A): for the LCM path, only build
            # stats — and only emit COMPACTION_STATS_* degrade markers — when the
            # announce will actually RENDER. The formatter default-denies noop/idle/
            # running/bypassed and conditional statuses whose post<pre check fails;
            # building stats for those emitted ~100%-kept_tail APPROX_ATTRIBUTION noise
            # on every LCM no-op. The NON-LCM (built-in compressor) path is UNCHANGED:
            # it always attempted the build before this PR and its announce gating is
            # the sid-rotation logic in _format_compaction_announce, not a status
            # allow-list; suppressing its stats here would silently degrade every
            # built-in announce to two-line (Greptile #177). The gate consumes the
            # EXACT variables the announce call passes as pre_tokens/post_tokens
            # (_pre_request_est / _compressed_est) so gate and render can't straddle
            # an estimate boundary.
            if _engine_name == "lcm":
                _inturn_stats_eligible = _inturn_stats_render_eligible(
                    _status,
                    locals().get("_pre_request_est"),
                    _compressed_est,
                )
            else:
                _inturn_stats_eligible = True  # built-in path: unchanged (always attempt)
            _inturn_stats = None
            if _inturn_stats_eligible:
                try:
                    from agent.compaction_stats import build_inturn_stats
                    from agent.model_metadata import estimate_messages_tokens_rough as _est
                    _why2 = "build raised"  # bound before build so the warning %s can't be unbound
                    # Multi-pass provenance is SHADOW-ONLY until the PR-C trust-flip
                    # (spec 2026-07-02, D-3): the engine now stamps every pass, but
                    # only single-pass stamps are trusted as the exact partition.
                    _leaf_passes = getattr(_cc, "last_leaf_passes", 0) or 0
                    _trust = "single-pass" if _leaf_passes <= 1 else "shadow"

                    def _on_shadow_compare(_b_idx, _cur_idx):
                        _sid = getattr(agent, "session_id", None) or "-"
                        _src = " src=test" if os.environ.get("PYTEST_CURRENT_TEST") else ""
                        if _b_idx == _cur_idx:
                            logger.info(
                                "COMPACTION_STATS_B_MULTIPASS_SHADOW agree "
                                "(kept_pre B=%d cur=%d) session=%s%s",
                                len(_b_idx), len(_cur_idx), _sid, _src,
                            )
                        else:
                            # Direct (UN-throttled) warning: the soak gate needs EVERY
                            # diverge event independently observable to measure within-
                            # session frequency — _warn_compaction_stats_once would drop
                            # all but the first per session (Greptile #178). Carries the
                            # same session/src fields so the watcher attributes it.
                            logger.warning(
                                "COMPACTION_STATS_B_MULTIPASS_SHADOW diverge "
                                "(kept_pre B=%d cur=%d) session=%s%s",
                                len(_b_idx), len(_cur_idx), _sid, _src,
                            )

                    _cand = build_inturn_stats(
                        messages=messages,
                        compressed=compressed,
                        estimator=_est,
                        engine_is_lcm=(_engine_name == "lcm"),
                        sanitize=getattr(_cc, "_sanitize_active_context_messages", None),
                        fresh_tail_count=getattr(_cc, "protect_last_n", None),
                        provenance_trust=_trust,
                        on_shadow_compare=_on_shadow_compare,
                        on_tag_missing=lambda: _warn_compaction_stats_once(
                            agent, "COMPACTION_STATS_TAG_MISSING in-turn"
                        ),
                    )
                    _ok2, _why2 = _cand.validate()
                    if _ok2:
                        # A-floor (approx_attribution) reconciles by construction but its
                        # kept/folded SPLIT is signature-approximate. The split error is
                        # bounded by the kept-tail fraction (the folded bulk is a contiguous
                        # prefix and always classifies correctly), so a kept-tail that is a
                        # large fraction of pre is the only case where the displayed split
                        # could be materially wrong. Degrade THAT render to two-line when the
                        # kept tail exceeds the gross-error threshold; otherwise show the
                        # granular split LABELED approximate + emit the observability marker.
                        if getattr(_cand, "approx_attribution", False):
                            # Gross-error magnitude = the RAW kept-tail size
                            # (estimator(messages[-fresh_tail_count:]) — match- AND
                            # sanitize-independent). kept_tokens (comp-side) is stripped small
                            # on a heavily-sanitized tail and _kept_pre_tokens is 0 when the
                            # signature match fails, so BOTH can under-report the true raw tail
                            # (Greptile P1 ×2, PR #109). Use raw_tail_tokens as the primary
                            # bound, with the other two as a floor in case it's unavailable.
                            _gross_tok = max(
                                _cand.raw_tail_tokens or 0,
                                _cand.kept_tokens or 0,
                                _cand._kept_pre_tokens or 0,
                            )
                            _pre_tok = _cand.pre_tokens or 0
                            _gross_frac = (_gross_tok / _pre_tok) if _pre_tok > 0 else 0.0
                            if _gross_frac > _APPROX_GROSS_MAX_FRAC:
                                # split could be materially wrong → honest two-line degrade
                                _warn_compaction_stats_once(
                                    agent,
                                    f"COMPACTION_STATS_APPROX_ATTRIBUTION in-turn "
                                    f"degraded (kept_tail {_gross_tok} / pre {_pre_tok} "
                                    f"= {_fmt_gross_frac(_gross_tok, _pre_tok)} "
                                    f"> {_APPROX_GROSS_MAX_FRAC:.0%}); two-line",
                                )
                                _inturn_stats = None
                            else:
                                _inturn_stats = _cand
                                # observability: the floor produced the numbers (not exact
                                # alignment / engine record). A heavy LCM session running the
                                # floor is now visible (watcher rate-alerts), never silent.
                                _warn_compaction_stats_once(
                                    agent,
                                    f"COMPACTION_STATS_APPROX_ATTRIBUTION in-turn "
                                    f"(engine={_engine_name}; kept_tail {_gross_tok} / "
                                    f"pre {_pre_tok} = {_fmt_gross_frac(_gross_tok, _pre_tok)})",
                                )
                        else:
                            _inturn_stats = _cand
                    else:
                        _warn_compaction_stats_once(
                            agent, f"COMPACTION_STATS_RECONCILE_FAILED in-turn {_why2}"
                        )
                except Exception:
                    _warn_compaction_stats_once(
                        agent, "COMPACTION_STATS_BUILD_FAILED in-turn", exc_info=True
                    )
            _reasoning_inturn = _resolve_announce_reasoning(agent)
            # Automatic (non-force) compaction honors the engine's announce
            # opt-out. The announce rail is DISTINCT from the lifecycle-status
            # rail: ``emit_automatic_compaction_announce`` (tri-state) decides,
            # and ``None`` INHERITS ``emit_automatic_compaction_status`` so a
            # plain quiet engine stays quiet on both rails (the default-engine
            # opt-out contract). LCM sets the announce flag True explicitly —
            # it silences lifecycle phases but its announce carries the
            # load-bearing lcm_grep/lcm_expand recovery guidance for the raw
            # turns it moved out of context. A manual /compress (force=True)
            # always announces.
            _announce_opt = getattr(
                agent.context_compressor,
                "emit_automatic_compaction_announce",
                None,
            )
            if _announce_opt is None:
                _announce_opt = getattr(
                    agent.context_compressor,
                    "emit_automatic_compaction_status",
                    True,
                )
            _announce_suppressed = not force and not _announce_opt
            if not _announce_suppressed:
                _emit_compaction_announce(
                    agent,
                    dedupe_key=_dedupe_key,
                    engine_name=_engine_name,
                    status=_status,
                    old_session_id=_old_sid,
                    new_session_id=_new_sid,
                    old_messages=_pre_msg_count,
                    new_messages=len(compressed),
                    pre_tokens=locals().get("_pre_request_est"),
                    post_tokens=_compressed_est,
                    model=getattr(agent, "model", None),
                    provider=getattr(agent, "provider", None),
                    window_from=_win_from,
                    window_to=_win_to,
                    summary_snippet=_extract_compaction_summary_snippet(compressed),
                    raw_store_count=None,  # session-scoped count not cheap here; omit (N-NEW-3)
                    after_fallback=_after_fb,
                    trigger_reason=trigger_reason,
                    trigger_value=(
                        getattr(_cc, "threshold_tokens", None)
                        if trigger_reason == "threshold" else None
                    ),
                    reasoning=_reasoning_inturn,
                    stats=_inturn_stats,
                    in_place=in_place,
                    real_prompt_tokens=_real_prompt_tokens_pre or None,
                )
        except Exception:
            logger.debug("compaction announce skipped (non-fatal)", exc_info=True)

        # ── Option B provenance strip (load-bearing, MUST NOT be skipped) ──────────
        # The engine stamps ``_src_idx`` on kept rows so build_inturn_stats (above) can
        # read the EXACT pre-side partition. It MUST NOT reach the wire / prompt cache /
        # transcript (``compressed`` becomes the new session transcript), so strip it
        # here — the single point on the only path where ``compressed`` carries it (the
        # early abort/noop returns return the original ``messages``, never stamped).
        # Done inline (no import that could fail and silently leave the key — Greptile
        # #110); idempotent; the transport sanitizer also drops ``_``-prefixed keys as a
        # defense-in-depth backstop.
        for _m in compressed:
            if isinstance(_m, dict) and "_src_idx" in _m:
                try:
                    del _m["_src_idx"]
                except Exception:
                    _m.pop("_src_idx", None)

        # Surface the compaction mode to the caller (run_conversation / gateway)
        # via a rotation-independent flag. The gateway uses this — NOT an
        # id-change diff — to re-baseline transcript handling (history_offset=0 +
        # rewrite on the same id) when compaction happened in place. See #38763.
        agent._last_compression_attempt_in_place = compacted_in_place
        agent._last_compaction_in_place = compacted_in_place

        # Surface the persist-failure signal (rotation-independent). True when a
        # compacted list was produced but the DB write to persist it was rolled
        # back (locked/contended state.db, FK error, ENOSPC). The gateway reads
        # this to distinguish a TRANSIENT, retryable save failure from a genuine
        # nothing-to-compress no-op — both leave session_id unchanged, so the
        # id-diff alone can't tell them apart. See #44794.
        agent._last_compaction_persist_failed = persist_failed

        # Keep the post-compression rough estimate for diagnostics, but do not
        # treat it as provider-reported prompt usage. Schema-heavy rough estimates
        # can remain above threshold even after the next real API request fits.
        _compressed_est = estimate_request_tokens_rough(
            compressed,
            system_prompt=new_system_prompt or "",
            tools=agent.tools or None,
        )
        agent.context_compressor.last_compression_rough_tokens = _compressed_est
        agent.context_compressor.last_prompt_tokens = -1
        agent.context_compressor.last_completion_tokens = 0
        agent.context_compressor.awaiting_real_usage_after_compression = True
        # Arm the effectiveness verdict only after a completed rewrite crosses
        # the full compaction boundary. Exceptions, aborts, and no-op attempts
        # leave this false, so unrelated later usage cannot be charged to an
        # attempt that never changed the transcript.
        if _compression_made_progress:
            record_boundary = getattr(
                type(agent.context_compressor),
                "record_completed_compaction",
                None,
            )
            if callable(record_boundary):
                record_boundary(
                    agent.context_compressor,
                    used_fallback=_compression_used_fallback,
                )
            else:
                agent.context_compressor._verify_compaction_cleared_threshold = True

        # Clear the file-read dedup cache.  After compression the original
        # read content is summarised away — if the model re-reads the same
        # file it needs the full content, not a "file unchanged" stub.
        try:
            from tools.file_tools import reset_file_dedup
            reset_file_dedup(task_id)
        except Exception:
            pass

        logger.info(
            "context compression done: session=%s messages=%d->%d rough_tokens=~%s awaiting_real_usage=true",
            agent.session_id or "none", _pre_msg_count, len(compressed),
            f"{_compressed_est:,}",
        )
        _commit_status = "committed" if split_status in {"not_applicable", "in_place_committed", "rotated_committed"} else "aborted"
        _emit_compression_attempt_telemetry(
            agent,
            started_at=_attempt_started_at,
            commit_status=_commit_status,
            split_status=split_status,
            failure_class=(
                "session_split_failed"
                if split_status in {"failed_not_indexed", "aborted"}
                else None
            ),
        )
        return compressed, new_system_prompt
    finally:
        # Release the lock on the OLD session_id only AFTER rotation completed
        # and all post-rotation bookkeeping (memory manager, context engine,
        # file dedup) ran. A concurrent path that wakes up the moment we
        # release will see the NEW session_id in state.db / SessionEntry and
        # acquire on that — no race against our just-finished work.
        try:
            _release_lock()
        finally:
            if _commit_fence_entered:
                commit_fence.finish_commit()


def _compress_context_via_codex_app_server(
    agent: Any,
    messages: list,
    system_message: Optional[str],
    *,
    approx_tokens: Optional[int] = None,
    task_id: str = "default",
    force: bool = False,
) -> Tuple[list, str]:
    """Route compaction to Codex app-server for Codex-owned threads.

    Hermes' normal compressor rewrites the local OpenAI-style transcript.
    That does not shrink the actual Codex app-server thread context. For this
    runtime, ask Codex to compact its own thread and keep Hermes' transcript
    unchanged.
    """
    auto_mode = str(
        getattr(agent, "codex_app_server_auto_compaction", "native") or "native"
    ).lower()
    if auto_mode not in {"native", "hermes", "off"}:
        auto_mode = "native"
    if not force and auto_mode != "hermes":
        logger.info(
            "codex app-server compaction skipped: mode=%s force=false "
            "(session=%s messages=%d tokens=~%s)",
            auto_mode,
            getattr(agent, "session_id", None) or "none",
            len(messages),
            f"{approx_tokens:,}" if approx_tokens else "unknown",
        )
        existing_prompt = getattr(agent, "_cached_system_prompt", None)
        if not existing_prompt:
            existing_prompt = agent._build_system_prompt(system_message)
        return messages, existing_prompt

    codex_session = getattr(agent, "_codex_session", None)
    if codex_session is None:
        logger.info(
            "codex app-server compaction skipped: no active codex thread "
            "(session=%s messages=%d tokens=~%s)",
            getattr(agent, "session_id", None) or "none",
            len(messages),
            f"{approx_tokens:,}" if approx_tokens else "unknown",
        )
        existing_prompt = getattr(agent, "_cached_system_prompt", None)
        if not existing_prompt:
            existing_prompt = agent._build_system_prompt(system_message)
        return messages, existing_prompt

    logger.info(
        "codex app-server compaction started: session=%s messages=%d tokens=~%s",
        getattr(agent, "session_id", None) or "none",
        len(messages),
        f"{approx_tokens:,}" if approx_tokens else "unknown",
    )
    try:
        agent._emit_status(COMPACTION_STATUS)
    except Exception:
        pass

    _compaction_done_emitted = False

    def _complete_compaction_lifecycle() -> None:
        nonlocal _compaction_done_emitted
        if _compaction_done_emitted:
            return
        _compaction_done_emitted = True
        _emit_compaction_done(agent)

    _activity_heartbeat: Optional[_CompressionActivityHeartbeat] = None
    try:
        _activity_heartbeat = _CompressionActivityHeartbeat(agent).start()
        result = codex_session.compact_thread()
    except BaseException:
        if _activity_heartbeat is not None:
            _activity_heartbeat.stop("context compression failed")
        _complete_compaction_lifecycle()
        raise

    if getattr(result, "interrupted", False) or getattr(result, "error", None):
        _activity_heartbeat.stop("context compression failed")
    else:
        _activity_heartbeat.stop("context compression completed")

    if getattr(result, "should_retire", False):
        try:
            codex_session.close()
        except Exception:
            pass
        agent._codex_session = None

    if getattr(result, "interrupted", False) or getattr(result, "error", None):
        try:
            agent._emit_warning(
                f"⚠ Codex app-server compaction failed: {result.error}"
            )
        except Exception:
            pass
        existing_prompt = getattr(agent, "_cached_system_prompt", None)
        if not existing_prompt:
            existing_prompt = agent._build_system_prompt(system_message)
        _complete_compaction_lifecycle()
        return messages, existing_prompt

    try:
        from agent.codex_runtime import (
            _record_codex_app_server_compaction,
            _record_codex_app_server_usage,
        )

        _record_codex_app_server_compaction(
            agent,
            result,
            approx_tokens=approx_tokens,
            force=True,
        )
        # An empty usage report must consume the pending post-compaction verdict
        # rather than leaving preflight deferral armed until some unrelated later
        # Codex turn supplies usage. Minimal external test engines may not expose
        # the ContextEngine update hook; preserve their existing bookkeeping.
        if hasattr(agent.context_compressor, "update_from_response"):
            _record_codex_app_server_usage(agent, result)
    except Exception:
        logger.debug("codex compaction bookkeeping failed", exc_info=True)

    try:
        from tools.file_tools import reset_file_dedup

        reset_file_dedup(task_id)
    except Exception:
        pass

    logger.info(
        "codex app-server compaction done: session=%s thread=%s turn=%s",
        getattr(agent, "session_id", None) or "none",
        getattr(result, "thread_id", None) or "",
        getattr(result, "turn_id", None) or "",
    )
    existing_prompt = getattr(agent, "_cached_system_prompt", None)
    if not existing_prompt:
        existing_prompt = agent._build_system_prompt(system_message)
    _complete_compaction_lifecycle()
    return messages, existing_prompt


def try_shrink_image_parts_in_messages(
    api_messages: list,
    *,
    max_dimension: int = 8000,
) -> bool:
    """Re-encode all native image parts at a smaller size to recover from
    image-too-large errors (Anthropic 5 MB, unknown other providers).

    Mutates ``api_messages`` in place. Returns True if any image part was
    actually replaced, False if there were no image parts to shrink or
    Pillow couldn't help (caller should surface the original error).

    Strategy: look for ``image_url`` / ``input_image`` parts carrying a
    ``data:image/...;base64,...`` payload, plus Anthropic-native
    ``{"type": "image", "source": {"type": "base64", ...}}`` blocks.
    For each one whose encoded size exceeds 4 MB (a safe target that slides
    under Anthropic's 5 MB ceiling with header overhead) or whose longest side
    exceeds ``max_dimension``, write the base64 to a tempfile, call
    ``vision_tools._resize_image_for_vision`` to produce a smaller data
    URL, and substitute it in place.

    Non-data-URL images (http/https URLs) are not touched — the provider
    fetches those itself and the size limit is different.
    """
    if not api_messages:
        return False

    try:
        from tools.vision_tools import _resize_image_for_vision
    except Exception as exc:
        logger.warning("image-shrink recovery: vision_tools unavailable — %s", exc)
        return False

    # 4 MB target leaves comfortable headroom under Anthropic's 5 MB.
    # Non-Anthropic providers we haven't observed rejecting are fine with
    # much larger; shrinking to 4 MB here loses quality but only fires
    # after a confirmed provider rejection, so the alternative is failure.
    target_bytes = 4 * 1024 * 1024
    # Anthropic enforces an 8000px per-side dimension cap independently of
    # the 5 MB byte cap.  In many-image requests, the provider can report a
    # lower cap (observed: 2000px).  The caller passes that parsed ceiling
    # when the rejection includes it.
    changed_count = 0
    # Track parts that are over the target but could NOT be shrunk under it.
    # If any survive, retrying is pointless — the same oversized payload will
    # be re-sent and rejected again, wasting the single retry budget.  We only
    # report success (caller retries) when every over-threshold image was
    # actually brought under the target.
    unshrinkable_oversized = 0

    def _decode_pixels(data_url: str) -> Optional[tuple]:
        """Return ``(width, height)`` of a base64 data URL, or None on failure.

        Soft-depends on Pillow; returns None (caller falls back to a
        bytes-only check) if Pillow is missing or the payload is corrupt.
        """
        try:
            import base64 as _b64_dim
            import io as _io_dim
            header_d, _, data_d = data_url.partition(",")
            if not data_d or not data_url.startswith("data:"):
                return None
            from PIL import Image as _PILImage
            with _PILImage.open(_io_dim.BytesIO(_b64_dim.b64decode(data_d))) as _img:
                return _img.size
        except Exception:
            return None

    def _shrink_data_url(url: str) -> tuple:
        """Return ``(resized_url, unshrinkable)`` for a data URL.

        ``resized_url`` is a smaller/dimension-correct data URL, or None when
        no rewrite was applied.  ``unshrinkable`` is True only when the image
        exceeded a constraint (byte-size or dimensions) and the resize failed
        to satisfy *that same* constraint — so the caller knows retrying is
        pointless even if a different image in the request shrank.
        """
        if not isinstance(url, str) or not url.startswith("data:"):
            return None, False

        # Determine which constraint is binding.  The accept/reject gate below
        # MUST be checked against the same axis that triggered the shrink: a
        # downscaled screenshot PNG routinely re-encodes to *more* bytes than
        # the original (PNG compression is non-monotonic in image size — a
        # smaller raster with LANCZOS resampling noise compresses worse than a
        # larger smooth one).  Rejecting a pixel-correct downscale purely
        # because its bytes grew permanently wedges sessions on the Anthropic
        # many-image 2000px path (#48013).
        needs_shrink = len(url) > target_bytes  # over byte budget
        triggered_by = "bytes" if needs_shrink else None
        if not needs_shrink:
            # Bytes are fine — check pixel dimensions against the provider's
            # reported per-side cap.  A screenshot can be tiny in bytes yet
            # too large in pixels.
            dims = _decode_pixels(url)
            if dims is None:
                # Pillow missing or corrupt data — fall back to byte-only.
                return None, False
            if max(dims) <= max_dimension:
                return None, False  # both bytes and pixels are within limits
            needs_shrink = True
            triggered_by = "dimension"

        try:
            header, _, data = url.partition(",")
            mime = "image/jpeg"
            if header.startswith("data:"):
                mime_part = header[len("data:"):].split(";", 1)[0].strip()
                if mime_part.startswith("image/"):
                    mime = mime_part
            import base64 as _b64
            raw = _b64.b64decode(data)
            suffix = {
                "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
                "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/bmp": ".bmp",
            }.get(mime, ".jpg")
            tmp = tempfile.NamedTemporaryFile(
                prefix="hermes_shrink_", suffix=suffix, delete=False,
            )
            try:
                tmp.write(raw)
                tmp.close()
                resized = _resize_image_for_vision(
                    Path(tmp.name),
                    mime_type=mime,
                    max_base64_bytes=target_bytes,
                    max_dimension=max_dimension,
                )
            finally:
                try:
                    Path(tmp.name).unlink(missing_ok=True)
                except Exception:
                    pass
            if not resized:
                # Resize returned nothing — Pillow couldn't help.
                return None, True
            if triggered_by == "bytes":
                # Byte budget is the binding constraint — bytes must shrink.
                if len(resized) >= len(url):
                    return None, True  # re-encode made it bigger
                # The per-side dimension cap is ALSO an active provider
                # constraint on this request (the caller passes the parsed cap
                # to both this helper and the resizer).  _resize_image_for_vision
                # returns a best-effort, possibly-over-cap blob when it
                # exhausts its halving budget — it freezes the long side once
                # the short side hits its 64px floor, so a very-high-aspect
                # image can stay over the cap even after bytes shrank.  If the
                # output is still over the cap, retrying would re-400 on
                # dimensions; treat it as unshrinkable.  (Skip when dims can't
                # be decoded — preserves historical byte-only behaviour.)
                new_dims = _decode_pixels(resized)
                if new_dims is not None and max(new_dims) > max_dimension:
                    return None, True
                return resized, False
            # triggered_by == "dimension": the per-side cap is binding.  The
            # re-encode may have grown in bytes; accept it as long as it is now
            # within the dimension cap.  Verify the new dimensions when we can.
            new_dims = _decode_pixels(resized)
            if new_dims is not None:
                if max(new_dims) <= max_dimension:
                    return resized, False
                # Still over the per-side cap — the resize didn't satisfy it.
                return None, True
            # Couldn't verify the re-encode's dimensions (corrupt output or
            # Pillow gone mid-call).  Fall back to the historical "bytes must
            # shrink" gate so we never accept an unverifiable, byte-larger blob.
            if len(resized) >= len(url):
                return None, True
            return resized, False
        except Exception as exc:
            logger.warning("image-shrink recovery: re-encode failed — %s", exc)
            return None, triggered_by is not None

    def _source_to_data_url(source: Any) -> Optional[str]:
        if not isinstance(source, dict) or source.get("type") != "base64":
            return None
        data = source.get("data")
        if not isinstance(data, str) or not data:
            return None
        media_type = str(source.get("media_type") or "image/jpeg").strip()
        if not media_type.startswith("image/"):
            media_type = "image/jpeg"
        return f"data:{media_type};base64,{data}"

    def _write_data_url_to_source(source: dict, data_url: str) -> None:
        header, _, data = data_url.partition(",")
        media_type = "image/jpeg"
        if header.startswith("data:"):
            candidate = header[len("data:"):].split(";", 1)[0].strip()
            if candidate.startswith("image/"):
                media_type = candidate
        source["type"] = "base64"
        source["media_type"] = media_type
        source["data"] = data

    for msg in api_messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "image":
                source = part.get("source")
                url = _source_to_data_url(source)
                resized, unshrinkable = _shrink_data_url(url or "")
                if resized and isinstance(source, dict):
                    _write_data_url_to_source(source, resized)
                    changed_count += 1
                elif unshrinkable:
                    unshrinkable_oversized += 1
                continue
            if ptype not in {"image_url", "input_image"}:
                continue
            image_value = part.get("image_url")
            # OpenAI chat.completions: {"image_url": {"url": "data:..."}}
            # OpenAI Responses: {"image_url": "data:..."}
            if isinstance(image_value, dict):
                url = image_value.get("url", "")
                resized, unshrinkable = _shrink_data_url(url)
                if resized:
                    image_value["url"] = resized
                    changed_count += 1
                elif unshrinkable:
                    unshrinkable_oversized += 1
            elif isinstance(image_value, str):
                resized, unshrinkable = _shrink_data_url(image_value)
                if resized:
                    part["image_url"] = resized
                    changed_count += 1
                elif unshrinkable:
                    unshrinkable_oversized += 1

    if changed_count:
        logger.info(
            "image-shrink recovery: re-encoded %d image part(s) to fit under %.0f MB",
            changed_count, target_bytes / (1024 * 1024),
        )
    if unshrinkable_oversized:
        # At least one oversized image could not be shrunk under the target.
        # Retrying would re-send it and fail identically, so signal "no
        # progress" even if other parts shrank — the caller will surface the
        # original error rather than burning its single retry on a no-op.
        logger.warning(
            "image-shrink recovery: %d oversized image part(s) could not be "
            "shrunk under %.0f MB — not retrying (would re-send rejected payload)",
            unshrinkable_oversized, target_bytes / (1024 * 1024),
        )
        return False
    return changed_count > 0


__all__ = [
    "COMPACTION_STATUS",
    "COMPACTION_DONE_STATUS",
    "COMPACTION_STATUS_MARKER",
    "check_compression_model_feasibility",
    "replay_compression_warning",
    "compress_context",
    "try_shrink_image_parts_in_messages",
]
