"""Overflow recovery for the conversation turn loop: 413 payload-too-large and
context-length errors after ``classify_api_error``.

Each path either compresses and signals a restart, defers softly (compression lock /
transient block), or ends the turn with a typed result. Nothing here imports
``agent.conversation_loop`` at module level (cycle); loop-internal helpers and the token
estimators that tests patch on the loop module are imported lazily inside the handlers.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from agent.conversation_compression import (
    COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE, COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE,
    COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE, compression_blocked_transiently,
    compression_skipped_due_to_lock, context_compression_timed_out,
)
from agent.error_classifier import FailoverReason
from agent.message_sanitization import serialized_messages_bytes
from agent.model_metadata import (
    get_context_length_from_provider_error, is_local_endpoint, is_output_cap_error,
    parse_available_output_tokens_from_error,
)
from agent.turn_failure_copy import site_copy, stamp_failure
from agent.turn_retry_state import TurnRetryState
from utils import base_url_host_matches

logger = logging.getLogger("agent.conversation_loop")

_RETRY_HINT = "   💡 Try /new to start a fresh conversation, or /compress to retry compression."

# A provider "context exceeded" whose request (incl. the output reservation) is under this share
# of the known window is not explained by the transcript; the rough estimator is ±30%, so a real
# overflow (request ≈ window) never lands below half.
_UNEXPLAINED_REJECTION_FRACTION = 0.5
# The provider measured the overflow itself only when it quotes a token count ("233153 tokens")
# or the comparison form ("233153 > 200000"). Any other digit run (request id, timestamp, port)
# is not a measurement and must not turn an unexplained rejection into a session reset.
_PROVIDER_MEASURED_COUNT_RE = re.compile(r"\d[\d,]{3,}\s*(?:input\s+)?tokens?\b|\d{4,}\s*>\s*\d{4,}", re.IGNORECASE)

_GITHUB_MODELS_HINT = (
    "   💡 GitHub Models free tier (models.inference.ai.azure.com) caps every",
    "      request at ~8K tokens. Hermes' system prompt + tool schemas baseline",
    "      exceeds that floor, so this endpoint cannot run an agentic loop.",
    "      Use the `copilot` provider with a Copilot subscription token (`hermes",
    "      setup` → GitHub Copilot), or pick any other provider.",
)

_MINIMAX_ANTHROPIC_URLS = ("https://api.minimax.io/anthropic", "https://api.minimaxi.com/anthropic")


@dataclass
class OverflowVerdict:
    """Outcome of ``recover_from_overflow``.

    ``action`` is one of ``"return"`` (end the turn with ``result``), ``"break"``
    (restart the API call — a ``_retry.restart_with_*`` flag is set), ``"continue"``
    (retry the call immediately) or ``"fallthrough"`` (not an overflow error, or
    overflow recovery declined — continue generic error handling). The remaining
    fields are the loop locals the handler may have rebound."""

    action: str
    result: Optional[Dict[str, Any]]
    messages: List[Dict[str, Any]]
    active_system_prompt: Any
    conversation_history: Any
    approx_tokens: int
    compression_attempts: int
    provider_overflow_recovery_pending: bool
    is_context_length_error: bool


@dataclass(kw_only=True)
class _Recovery(OverflowVerdict):
    """Working state shared by the overflow sub-handlers — the verdict itself, plus the
    read-only call context; handlers mutate the loop-local fields and ``done()`` stamps
    the action."""

    agent: Any
    api_messages: Any
    system_message: Any
    effective_task_id: Any
    api_call_count: int
    max_compression_attempts: int
    action: str = "fallthrough"
    result: Optional[Dict[str, Any]] = None
    provider_overflow_recovery_pending: bool = False
    is_context_length_error: bool = False

    def done(self, action: str, result: Optional[Dict[str, Any]] = None) -> OverflowVerdict:
        self.action, self.result = action, result
        return self

    def fail_turn(
        self, final_response: str, *, notices: tuple = (), log: Optional[tuple] = None,
        compression_exhausted: bool = True, reason: str = "context_overflow",
        retryable: bool = False, **extra: Any,
    ) -> OverflowVerdict:
        """End the turn as failed/partial. ``notices`` flush the buffered retry trace
        first so the user sees what compression attempts were made."""
        agent = self.agent
        if notices:
            agent._flush_status_buffer()
            for line in notices:
                agent._vprint(f"{agent.log_prefix}{line}", force=True, diagnostic=True)
        if log:
            logger.error(*log)
        agent._persist_session(self.messages, self.conversation_history)
        result = stamp_failure({
            "final_response": final_response,
            "messages": self.messages,
            "completed": False,
            "api_calls": self.api_call_count,
            "error": final_response,
            "partial": True,
            "failed": True,
        }, reason, retryable)
        if compression_exhausted:
            # Reuse the gateway's existing context-recovery contract (#98722, salvaged from #98741). The
            # bloated transcript remains intact while future input can move to a clean session instead of
            # replaying the summarize-timeout loop.
            result["compression_exhausted"] = True
        result.update(extra)
        return self.done("return", result)

    def count_attempt(self, *, payload_too_large: bool = False) -> Optional[OverflowVerdict]:
        """Bump ``compression_attempts``; the terminal verdict once the cap is exceeded."""
        self.compression_attempts += 1
        cap = self.max_compression_attempts
        if self.compression_attempts <= cap:
            return None
        if payload_too_large:
            return self.fail_turn(
                site_copy("payload_too_large", model=self.agent.model),
                notices=(
                    f"❌ Max compression attempts ({cap}) reached for payload-too-large error.",
                    _RETRY_HINT,
                ),
                log=("%s413 compression failed after %d attempts.", self.agent.log_prefix, cap),
            )
        return self.fail_turn(
            site_copy("context_overflow", model=self.agent.model),
            notices=(f"❌ Max compression attempts ({cap}) reached.", _RETRY_HINT),
            log=("%sContext compression failed after %d attempts.", self.agent.log_prefix, cap),
        )

    def compress(
        self, request_tokens: int, *, fail_on_timeout: bool = False, trigger_reason: str = "overflow_context",
    ) -> Optional[OverflowVerdict]:
        """One compression pass with the summary-failure cooldown bypassed (the
        provider proved the request doesn't fit). Returns ``None`` when history was
        compressed, or a soft-defer verdict when another path holds the compression
        lock or a timed guard no-oped the pass: the attempt is refunded and the turn
        ends WITHOUT ``compression_exhausted`` so the gateway does not auto-reset. With
        ``fail_on_timeout`` a host timeout (recovery spent its wait budget with no
        committed summary) ends the turn via the typed contract, since re-sending would
        hit the same overflow."""
        from agent.conversation_compression import conversation_history_after_compression
        from agent.conversation_loop import _COMPRESSION_TIMEOUT_FINAL_RESPONSE, _compression_deferred_result

        agent = self.agent
        before = self.messages
        self.messages, self.active_system_prompt = agent._compress_context(
            before, self.system_message, approx_tokens=request_tokens,
            task_id=self.effective_task_id, bypass_cooldown=True, trigger="overflow",
            trigger_reason=trigger_reason,
        )
        if self.messages is before:
            deferred = None
            if compression_skipped_due_to_lock(agent):
                deferred = _compression_deferred_result(agent, self.messages, self.api_call_count)
            elif compression_blocked_transiently(agent):
                deferred = _compression_deferred_result(
                    agent, self.messages, self.api_call_count, reason="transient_block",
                )
            if deferred is not None:
                self.compression_attempts -= 1
                agent._persist_session(self.messages, self.conversation_history)
                return self.done("return", deferred)
        if fail_on_timeout and context_compression_timed_out(agent):
            return self.fail_turn(
                _COMPRESSION_TIMEOUT_FINAL_RESPONSE, turn_exit_reason="context_compression_timeout"
            )
        self.conversation_history = conversation_history_after_compression(
            agent, self.messages, self.conversation_history
        )
        return None

    def compress_scored_by_tokens(
        self, request_tokens: int, *, fail_on_timeout: bool = False, trigger_reason: str = "overflow_context",
    ) -> Tuple[Optional[OverflowVerdict], bool, int]:
        """``compress`` scored in message count / tokens (context-overflow errors ARE
        token-budget errors). Same-message-count compression (tool-result pruning,
        in-place summarization) can shrink the request, so re-estimate rather than trust
        the array length. Returns ``(deferred_verdict, shrank, new_tokens)``."""
        from agent.model_metadata import estimate_messages_tokens_rough

        original_len = len(self.messages)
        original_tokens = estimate_messages_tokens_rough(self.messages)
        deferred = self.compress(request_tokens, fail_on_timeout=fail_on_timeout, trigger_reason=trigger_reason)
        if deferred is not None:
            return deferred, False, original_tokens
        messages = self.messages
        # Re-measure after compression. Same-message-count compression (tool-result pruning, in-place
        # summarization) can materially reduce request size without reducing the message array (#39550), and
        # — the image-dominated case — compaction's historical-media aging (#97160) can free megabytes of
        # base64 that the token estimate never counted. Bytes are the yardstick for a 413; tokens are kept
        # only for status display.
        # Re-estimate tokens after compression. Same-message-count compression (tool-result pruning,
        # in-place summarization) can materially reduce request size without reducing the message array.
        # (#39550)
        new_tokens = estimate_messages_tokens_rough(messages)
        shrank_tokens = new_tokens > 0 and new_tokens < original_tokens * 0.95
        if len(messages) < original_len:
            self.agent._buffer_diagnostic_status(COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE.format(before=original_len, after=len(messages)))
        elif shrank_tokens:
            self.agent._buffer_diagnostic_status(COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE.format(before=original_tokens, after=new_tokens))
        return None, len(messages) < original_len or shrank_tokens, new_tokens

    def request_tokens(self) -> int:
        """Overhead-aware request size (msgs + tools + system) so LCM forced-overflow
        recovery arms on the TRUE request, not the tool-blind message count."""
        from agent.model_metadata import estimate_request_tokens_rough

        return estimate_request_tokens_rough(self.api_messages, tools=self.agent.tools or None)


def _recover_payload_too_large(st: _Recovery, _retry: TurnRetryState) -> OverflowVerdict:
    """413: compress and retry. A 413 is a BYTE-size error, so progress is scored in
    payload bytes — never the token estimate, which is deliberately byte-blind to images
    and wedged sessions on "no progress"."""
    from agent.model_metadata import estimate_messages_tokens_rough

    agent = st.agent
    exhausted = st.count_attempt(payload_too_large=True)
    if exhausted is not None:
        return exhausted
    agent._buffer_diagnostic_status(
        f"⚠️  Request payload too large (413) — compression attempt "
        f"{st.compression_attempts}/{st.max_compression_attempts}..."
    )

    messages = st.messages
    original_len = len(messages)
    # A 413 is a BYTE-size error, so this branch scores progress in BYTES of the serialized messages payload
    # — exact and free — never the token estimate. The estimator prices every image at a flat per-image
    # token cost (see estimate_messages_tokens_rough) so screenshots don't trigger premature compaction;
    # that deliberate byte-blindness means compaction can free megabytes of base64 (real case: two vision
    # results = 96.6% of the request body but ~3.7% of the estimate) while the token delta stays under any
    # threshold. Token-scored progress here burned all attempts on "no progress" and wedged the session
    # permanently. (#88960 / #47339)
    original_bytes = serialized_messages_bytes(messages)
    deferred = st.compress(st.request_tokens(), trigger_reason="overflow_413")
    if deferred is not None:
        return deferred

    # Re-measure: same-count compression and media aging can shrink the request
    # without shrinking the array. Tokens only for status display.
    messages = st.messages
    st.approx_tokens = estimate_messages_tokens_rough(messages)
    new_bytes = serialized_messages_bytes(messages)
    if len(messages) < original_len or (new_bytes > 0 and new_bytes < original_bytes * 0.95):
        if len(messages) < original_len:
            agent._buffer_diagnostic_status(COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE.format(before=original_len, after=len(messages)))
        else:
            agent._buffer_diagnostic_status(
                f"🗜️ Compressed {original_bytes:,} → {new_bytes:,} " f"payload bytes, retrying..."
            )
        time.sleep(2)  # Brief pause between compression retries
        _retry.restart_with_compressed_messages = True
        return st.done("break")

    if agent._try_strip_image_parts_from_tool_messages(st.api_messages, remember_model=False):
        agent._buffer_diagnostic_status(
            "📐 Compression could not reduce the request further — "
            "removed retained vision payloads and retrying..."
        )
        return st.done("continue")

    return st.fail_turn(
        site_copy("payload_too_large", model=agent.model),
        notices=("❌ Payload too large and cannot compress further.", _RETRY_HINT),
        log=("%s413 payload too large. Cannot compress further.", agent.log_prefix),
    )


def _clamp_output_cap(st: _Recovery, _retry: TurnRetryState, available_out: int, old_ctx: int) -> OverflowVerdict:
    """Output-cap error ("max_tokens too large": input fits but input + max_tokens >
    window). The provider's available_tokens is the authoritative bound; also estimate
    the real request shape (API-only content) and use the smaller minus a margin."""
    agent = st.agent
    request_input_estimate = st.request_tokens()
    local_available_out = old_ctx - request_input_estimate
    if local_available_out > 0:
        safe_out = max(1, min(available_out, local_available_out) - 64)
    else:
        # Local estimate can overshoot; fall back to the provider-reported budget.
        safe_out = max(1, available_out - 64)
    agent._ephemeral_max_output_tokens = safe_out
    agent._buffer_vprint(
        f"⚠️  Output cap too large for current prompt — retrying with max_tokens={safe_out:,} "
        f"(provider_available={available_out:,}, estimated_request_tokens={request_input_estimate:,}; "
        f"context_length unchanged at {old_ctx:,})"
    )
    # Still count against compression_attempts so a recurring error can't loop forever.
    exhausted = st.count_attempt()
    if exhausted is not None:
        return exhausted
    # Also compress history so the retry doesn't spin on max_tokens alone; dropping the
    # middle window makes the total fit. Compression must never turn an output-cap error
    # fatal — on error, fall through and retry on max_tokens alone.
    try:
        deferred, _shrank, _new_tokens = st.compress_scored_by_tokens(
            request_input_estimate, trigger_reason="overflow_context")
        if deferred is not None:
            return deferred
    except Exception:
        logger.warning(
            "%sOutput-cap compression hit an error; retrying on max_tokens only.", agent.log_prefix
        )
    _retry.restart_with_compressed_messages = True
    return st.done("break")


def _adopt_provider_context_limit(st: _Recovery, error_msg: str, old_ctx: int) -> Optional[int]:
    """Shrink context_length only when the provider reports the real limit; else keep
    the window and compress. Guessed probe tiers can turn a configured 1M window into
    256K/128K/64K. Returns the provider-reported limit, or ``None``."""
    from agent.model_metadata import save_provider_context_length

    agent = st.agent
    compressor = agent.context_compressor
    new_ctx = get_context_length_from_provider_error(error_msg, old_ctx)
    if new_ctx is not None:
        agent._buffer_vprint(f"Context limit detected from API: {new_ctx:,} tokens (was {old_ctx:,})")
        compressor.update_model(
            model=agent.model, context_length=new_ctx, base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""), provider=agent.provider, api_mode=agent.api_mode,
        )
        # Persist the provider-reported limit BEFORE compression/retry: rate limit,
        # missing usage, or restart must not lose confirmed metadata. Probe flags
        # remain a fallback if this write fails.
        save_provider_context_length(agent.model, agent.base_url, new_ctx, agent.provider)
        # Probe flags only on the built-in compressor (plugin engines manage their
        # own); provider-sourced value, so safe to cache.
        if hasattr(compressor, "_context_probed"):
            compressor._context_probed = True
            compressor._context_probe_persistable = True
        agent._buffer_vprint(f"⚠️  Context length exceeded — using provider limit: {old_ctx:,} → {new_ctx:,} tokens")
        return new_ctx

    is_minimax_provider = (
        (getattr(agent, "provider", "") or "").lower() in {"minimax", "minimax-cn"}
        or (getattr(agent, "base_url", "") or "").rstrip("/").lower().startswith(_MINIMAX_ANTHROPIC_URLS)
    )
    if is_minimax_provider and "context window exceeds limit (" in error_msg:
        agent._buffer_vprint(
            f"Provider reported overflow amount only; "
            f"keeping context_length at {old_ctx:,} tokens."
        )
    else:
        agent._buffer_vprint(
            f"⚠️  Context length exceeded, but provider did not report a max context length; "
            f"keeping context_length at {old_ctx:,} tokens."
        )
    return None


def _endpoint_for_log(base_url: Any) -> str:
    """scheme://host[:port] only: userinfo, path and query can carry credentials."""
    try:
        parts = urlsplit(str(base_url or ""))
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "<unparseable>"
    return f"{parts.scheme}://{host}{port}" if parts.scheme and host else "<unset>"


def _unexplained_rejection_kept(st: _Recovery, request_tokens: int, window: int, basis: str) -> OverflowVerdict:
    """End a turn whose explicit context rejection sits far below the known window, keeping the
    session: no ``compression_exhausted`` (``gateway/run_turn.py::_hmwa_compression_exhaustion_reset``
    would wipe it), retryable. The provider's limit may not live in this transcript at all."""
    agent = st.agent
    return st.fail_turn(
        site_copy("provider_context_rejection", model=agent.model, tokens=request_tokens, window=window,
                  basis=_BASIS_COPY[basis]),
        log=(
            "%sProvider context rejection below the window persisted (~%s %s of %s tokens, %d compression "
            "attempts); keeping the session", agent.log_prefix, f"{request_tokens:,}", basis, f"{window:,}",
            st.compression_attempts,
        ),
        compression_exhausted=False, reason=FailoverReason.server_error.value, retryable=True,
        context_overflow=False,
    )


# How the request size was known (t_5188f4ed): ``measured`` = the provider's last reported
# usage (prompt + completion) plus a rough count of the messages added since; ``estimate`` =
# the local rough estimator alone, which read 244,603 for a request the provider counted at
# 346,615 (2026-11-17 08:06, raw_ratio 1.435).
_BASIS_COPY = {"measured": "counted by the provider", "estimate": "estimate"}


def _rejected_request_tokens(st: _Recovery) -> Tuple[int, str]:
    """``(tokens, basis)`` for the request the provider just rejected, output reservation included."""
    from agent.usage_anchor import anchored_context_tokens

    agent = st.agent
    out = max(0, int(getattr(agent, "max_tokens", 0) or 0))
    try:
        anchored = anchored_context_tokens(st.messages, getattr(agent, "_usage_anchor", None))
    except Exception:  # noqa: BLE001
        anchored = None
    if anchored:
        return int(anchored) + out, "measured"
    return st.request_tokens() + out, "estimate"


def _return_to_pinned_route(agent: Any) -> bool:
    """Put the session back on its pinned route unless that route is benched by a stated rate
    limit or its relay says it cannot serve the model. A sticky hold is not consulted: it keeps
    the session off a primary that failed, but this lane cannot take the request at all, and
    the sticky policy's own ``fallback_failed`` return is the same move (spec §4.3)."""
    from agent import fallback_policy as fp
    from agent.fallback_wiring import _primary_capacity_hold, restore_allowed

    gate = restore_allowed(agent, probe=False)  # no network; the cooldown is read here only
    if not gate.allowed and gate.reason == "cooldown":
        return False
    if _primary_capacity_hold(agent):
        return False
    return bool(agent._restore_primary_runtime(
        _policy_decision=fp.Decision(True, "fallback_failed", "fallback lane context ceiling")))


def _active_entry_base_url(agent: Any) -> Optional[str]:
    """The DECLARED ``base_url`` of the fallback entry now serving (the walk keys ceilings on
    it before it resolves a client). The walk advanced ``_fallback_index`` past that entry."""
    chain = getattr(agent, "_fallback_chain", None) or []
    idx = int(getattr(agent, "_fallback_index", 0) or 0) - 1
    fb = chain[idx] if 0 <= idx < len(chain) and isinstance(chain[idx], dict) else {}
    same = (str(fb.get("provider") or "").strip().lower() == str(agent.provider or "").strip().lower()
            and str(fb.get("model") or "").strip() == str(agent.model or "").strip())
    return fb.get("base_url") if same else None


def _leave_fallback_rung(st: _Recovery, _retry: TurnRetryState, request_tokens: int) -> Optional[OverflowVerdict]:
    """A FALLBACK rung rejected the request below the window (t_5188f4ed): its own ceiling, not
    the transcript. Record the ceiling, then go back to the pinned route if it may serve, else to
    the next rung; never compress for it. ``None`` when neither is available."""
    from agent.context_ceilings import record

    agent = st.agent
    record(agent.provider, agent.model, _active_entry_base_url(agent), request_tokens)
    agent._context_rejected_tokens = max(int(getattr(agent, "_context_rejected_tokens", 0) or 0), request_tokens)
    rung = f"{agent.provider}/{agent.model}"
    moved = _return_to_pinned_route(agent) or bool(
        agent._try_activate_fallback(reason=FailoverReason.context_overflow))
    if not moved:
        return None
    logger.warning("%sFallback %s rejected ~%s tokens as too long; moving to %s/%s without compressing",
                   agent.log_prefix, rung, f"{request_tokens:,}", agent.provider, agent.model)
    from agent.conversation_loop import _arm_fallback_restart

    st.active_system_prompt = _arm_fallback_restart(agent, st.api_messages, st.active_system_prompt, _retry)
    st.compression_attempts = 0
    return st.done("break")


def _recover_context_length(st: _Recovery, _retry: TurnRetryState, error_msg: str) -> OverflowVerdict:
    """Context-length error. Two shapes: "prompt too long" = INPUT overflows the window
    (shrink context_length + compress); "max_tokens too large" = input fits but
    input + max_tokens > window (shrink the OUTPUT cap only)."""
    agent = st.agent
    old_ctx = agent.context_compressor.context_length

    available_out = parse_available_output_tokens_from_error(error_msg)
    if available_out is not None:
        return _clamp_output_cap(st, _retry, available_out, old_ctx)

    # Output-cap error with unparseable budget: compression can't help (input already
    # fits) and would death-loop on the same 400. Fail fast.
    if is_output_cap_error(error_msg):
        return st.fail_turn(
            "The requested output length exceeds the provider's output cap for this model, "
            "and the error did not state the allowed limit.",
            notices=(
                "❌ The provider rejected the request because the requested output length exceeds its "
                "output cap for this model, and the error did not state the allowed limit.",
                "   💡 Hermes has no user setting for the output cap — check the endpoint's default max output "
                "(completion) tokens for this model on the server or proxy. "
                "(This is an output-cap error, not a context overflow — compression cannot fix it.)",
            ),
            log=(
                f"{agent.log_prefix}Output-cap error not routed into compression "
                f"(max_tokens over provider cap): {error_msg[:200]}",
            ),
            compression_exhausted=False,
        )

    new_ctx = _adopt_provider_context_limit(st, error_msg, old_ctx)

    # Ambiguous generic failures on local single-slot servers can mean another
    # request holds their context. An explicit context_length_exceeded is different:
    # loopback may be a relay to a hosted provider, so trust that rejection and
    # compress once rather than classifying it as local contention.
    window = agent.context_compressor.context_length
    request_tokens, basis = _rejected_request_tokens(st)
    explicit_context_rejection = "context_length_exceeded" in error_msg.lower()
    # A provider that quotes its own count ("1200000 tokens > 1000000") measured a real overflow.
    unexplained = (
        new_ctx is None and isinstance(window, int) and window > 0
        and request_tokens < window * _UNEXPLAINED_REJECTION_FRACTION
        and not _PROVIDER_MEASURED_COUNT_RE.search(error_msg)
    )
    if explicit_context_rejection and unexplained:
        # A rejected request is an upper bound, not a measured model window. Keep
        # the observation in telemetry; never poison the authoritative metadata cache.
        logger.warning(
            "%sProvider context ceiling observation provider=%s model=%s endpoint=%s "
            "observed_rejected_request_tokens=%d token_basis=%s believed_window=%d compression_attempts=%d",
            agent.log_prefix, agent.provider, agent.model, _endpoint_for_log(agent.base_url),
            request_tokens, basis, window, st.compression_attempts,
        )
        if getattr(agent, "_fallback_activated", False) is True:
            moved = _leave_fallback_rung(st, _retry, request_tokens)
            if moved is not None:
                return moved
        if st.compression_attempts >= 1:
            return _unexplained_rejection_kept(st, request_tokens, window, basis)
    if (
        is_local_endpoint(agent.base_url)
        and not explicit_context_rejection
        and not _PROVIDER_MEASURED_COUNT_RE.search(error_msg)
        and isinstance(window, int) and window > 0
        and request_tokens < window * _UNEXPLAINED_REJECTION_FRACTION
    ):
        return st.fail_turn(
            site_copy("server_context_rejection", model=agent.model, tokens=request_tokens, window=window,
                      basis=_BASIS_COPY[basis]),
            notices=(
                f"❌ The server rejected the request as too large, but it is only ~{request_tokens:,} "
                f"tokens ({_BASIS_COPY[basis]}) against a {window:,}-token window — not compressing.",
                "   💡 Wait a moment and /retry; another request on the same server (e.g. a background "
                "review) may have been holding its context.",
            ),
            log=(
                "%sProvider context rejection not explained by request size (~%s of %s tokens); "
                "skipping compression: %s", agent.log_prefix, f"{request_tokens:,}", f"{window:,}", error_msg[:200],
            ),
            compression_exhausted=False, reason=FailoverReason.server_error.value, retryable=True,
            context_overflow=False,
        )

    keep_session = explicit_context_rejection and unexplained
    exhausted = st.count_attempt()
    if exhausted is not None:
        return _unexplained_rejection_kept(st, request_tokens, window, basis) if keep_session else exhausted
    agent._buffer_diagnostic_status(COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE.format(
        tokens=st.approx_tokens, attempt=st.compression_attempts, cap=st.max_compression_attempts,
    ))

    # keep_session: a summary timeout falls through to the session-keeping exit below instead of
    # the compression_exhausted timeout contract.
    deferred, shrank, new_tokens = st.compress_scored_by_tokens(
        st.request_tokens(), fail_on_timeout=not keep_session, trigger_reason="overflow_context")
    if deferred is not None:
        return deferred
    st.approx_tokens = new_tokens
    if shrank or (new_ctx and new_ctx < old_ctx):
        time.sleep(2)  # Brief pause between compression retries
        # Rebuild the full request and force normal preflight to honor it; message
        # count alone doesn't prove system/tool-inclusive pressure fell.
        st.provider_overflow_recovery_pending = True
        _retry.restart_with_compressed_messages = True
        return st.done("break")

    if keep_session:
        return _unexplained_rejection_kept(st, request_tokens, window, basis)
    # Can't compress further and already at minimum tier.
    return st.fail_turn(
        site_copy("context_overflow", model=agent.model),
        notices=(
            f"❌ The conversation is too long for the model ({new_tokens:,} tokens) and cannot be shrunk further.",
            _RETRY_HINT,
        ),
        log=("%sContext length exceeded: %s tokens. Cannot compress further.", agent.log_prefix, f"{new_tokens:,}"),
    )


def recover_from_overflow(
    agent: Any, api_error: Exception, classified: Any, _retry: TurnRetryState, *,
    status_code: Optional[int], error_msg: str, wrapped_output_cap_budget: Optional[int],
    messages: List[Dict[str, Any]], api_messages: Any, system_message: Any,
    active_system_prompt: Any, conversation_history: Any, approx_tokens: int,
    compression_attempts: int, max_compression_attempts: int, api_call_count: int,
    effective_task_id: Any,
) -> OverflowVerdict:
    """413 payload-too-large and context-length recovery (compress + retry, output-cap
    clamp, provider-reported context limit, GitHub Models free-tier hint). Order is
    load-bearing: 413 is checked BEFORE the generic 4xx handler, and context-length
    errors (incl. relay-wrapped output-cap 429s) BEFORE non-retryable client errors.
    Compression progress is scored in payload BYTES for 413 (never the byte-blind token
    estimate) and in tokens/message count for context overflow."""
    st = _Recovery(
        agent=agent, api_messages=api_messages, system_message=system_message,
        effective_task_id=effective_task_id, api_call_count=api_call_count,
        max_compression_attempts=max_compression_attempts, messages=messages,
        active_system_prompt=active_system_prompt, conversation_history=conversation_history,
        approx_tokens=approx_tokens, compression_attempts=compression_attempts,
    )

    # GitHub Models free tier caps requests at 8K tokens, under the system prompt +
    # tool schema floor; compression can't help, so say so.
    if (
        status_code == 413
        and isinstance(agent.base_url, str)
        and base_url_host_matches(agent.base_url, "models.inference.ai.azure.com")
    ):
        for line in _GITHUB_MODELS_HINT:
            agent._vprint(f"{agent.log_prefix}{line}", force=True, diagnostic=True)

    if classified.reason == FailoverReason.payload_too_large:
        return _recover_payload_too_large(st, _retry)

    # Relay-wrapped output-cap 429s (parsed by the caller) go to the clamp, not
    # failover or generic retries. The classifier also covers 400/disconnect +
    # large-session heuristics.
    st.is_context_length_error = (
        # Check for context-length errors BEFORE generic 4xx handler. The classifier detects context
        # overflow from: explicit error messages, generic 400 + large session heuristic (#1630), and server
        # disconnect + large session pattern (#2153).
        classified.reason == FailoverReason.context_overflow
        or wrapped_output_cap_budget is not None
    )
    if st.is_context_length_error:
        return _recover_context_length(st, _retry, error_msg)
    return st.done("fallthrough")
