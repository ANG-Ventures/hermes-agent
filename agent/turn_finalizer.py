"""Post-loop turn finalization for ``run_conversation``.

Budget summary, trajectory save, persist, diagnostics, response transforms, result
assembly, steer drain, memory/skill review. Synchronous, single return. ``logger`` is
imported lazily from ``agent.conversation_loop`` (no cycle, same logger name)."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import suppress
from typing import Any, Callable, List, Optional, Tuple

from agent.codex_responses_adapter import _summarize_user_message_for_log
from agent.delegation_context import is_dispatcher_owned_worker_context
from agent.interrupt_control import interrupted_during_api_call_reason
from agent.turn_failure_copy import exit_reason_failure, stamp_failure
from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.message_content import flatten_message_text
from agent.message_metadata import append_message, stamp_message_timestamp
from agent.message_sanitization import _sanitize_surrogates
from agent.served_model import result_model_fields

# Verification-continuation nudges (verify-on-stop / pre_verify) must be stripped from
# returned/live history to avoid role-alternation breaks; the assistant response is
# real content and is not flagged. (#65919)
_VERIFICATION_CONTINUATION_FLAGS = ("_verification_stop_synthetic", "_pre_verify_synthetic")

_SENTENCE_END = {".", "!", "?", "。", "！", "？", "`", ")"}

# ``result[key] = agent.session_<key>`` for the per-session usage/cost counters.
_SESSION_TOKEN_KEYS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
)
_SESSION_COST_KEYS = ("estimated_cost_usd", "cost_status", "cost_source")


def _rollup_turn_usage(turn_calls: list[dict]) -> dict:
    """Aggregate physical-call usage into the turn record.

    UNKNOWN flags are absorbing: if any call lacks a measured term, the sum is
    not a measurement. Kept as a callable seam so tests execute the shipped
    behavior rather than extracting an expression from this module's AST.
    """
    return {
        "api_calls": len(turn_calls),
        "input_tokens": sum(c["input_tokens"] for c in turn_calls),
        "output_tokens": sum(c["output_tokens"] for c in turn_calls),
        "output_tokens_unknown": any(
            bool(c.get("output_tokens_unknown")) for c in turn_calls
        ),
        "input_tokens_unknown": any(
            bool(c.get("input_tokens_unknown")) for c in turn_calls
        ),
        "cache_read_tokens_unknown": any(
            bool(c.get("cache_read_tokens_unknown")) for c in turn_calls
        ),
        "cache_write_tokens_unknown": any(
            bool(c.get("cache_write_tokens_unknown")) for c in turn_calls
        ),
        "usage_unknown": any(bool(c.get("usage_unknown")) for c in turn_calls),
        "cache_read_tokens": sum(c["cache_read_tokens"] for c in turn_calls),
        "cache_write_tokens": sum(c["cache_write_tokens"] for c in turn_calls),
        "reasoning_tokens": sum(c["reasoning_tokens"] for c in turn_calls),
        "total_tokens": sum(c["total_tokens"] for c in turn_calls),
    }


def _assistant_row_missing_visible_text(msg: dict) -> bool:
    """True when an assistant row has no visible text (blank final or tool-only)."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    return not flatten_message_text(msg.get("content")).strip()


def _record_kanban_budget_exhausted(
    kanban_task: str, api_call_count: int, max_iterations: int, logger: logging.Logger
) -> None:
    """Record a terminal ``timed_out`` outcome for a kanban worker out of budget.

    Routed via ``_record_task_failure`` (not ``kanban_block``) so it counts toward the
    consecutive-failure circuit breaker. Idempotent via the ``_end_run`` CAS
    (``WHERE ended_at IS NULL``), so safe from multiple exit paths.

    This is a bounded fallback (#87096): the CAS invariant in ``_end_run`` (``WHERE ended_at IS NULL``)
    guarantees idempotence — if another path already closed the run this is a no-op — so it is safe to call
    from multiple exit paths.
    """
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_dispatch as _kbd
        _conn = _kbc.connect()
        try:
            _kbd._record_task_failure(
                _conn,
                kanban_task,
                error=(
                    f"Iteration budget exhausted ({api_call_count}/{max_iterations}) — "
                    "task could not complete within the allowed iterations"
                ),
                outcome="timed_out",
                release_claim=True,
                end_run=True,
                event_payload_extra={"budget_used": api_call_count, "budget_max": max_iterations},
            )
        finally:
            with suppress(Exception):
                _conn.close()
    except Exception:
        logger.warning(
            "Failed to record budget-exhausted failure for task %s", kanban_task, exc_info=True
        )


def _drop_verification_continuation_scaffolding(messages) -> None:
    """Remove verification-continuation nudges in place; only the synthetic nudges carry
    these flags, so the real attempted final answer persisted to state.db survives."""
    messages[:] = [
        m for m in messages
        if not (isinstance(m, dict) and any(m.get(f) for f in _VERIFICATION_CONTINUATION_FLAGS))
    ]


def _clone_background_review_messages(messages):
    """Copy the review input without aliasing the live transcript."""
    # Lazy: conversation_loop imports this module (cycle).
    from agent.conversation_loop import _clone_message_for_send

    return [_clone_message_for_send(message) for message in messages]


def _invoke_hook_safely(name: str, logger: logging.Logger, **kwargs) -> list:
    """Fire a lifecycle plugin hook; a failing hook is logged, never fatal."""
    try:
        from hermes_cli.lifecycle import invoke_hook
        return invoke_hook(name, **kwargs)
    except Exception as exc:
        logger.warning("%s hook failed: %s", name, exc)
        return []


def _guarded_cleanup(label: str, fn: Callable[[], Any], errors: List[str], logger) -> None:
    """Post-loop cleanup must never lose the response: each step is guarded
    independently and errors surface via ``cleanup_errors`` (#8049)."""
    try:
        fn()
    except Exception as err:
        errors.append(f"{label}: {err}")
        logger.error("finalize_turn: _%s failed: %s", label, err, exc_info=True)


def _resolve_budget_fallback(
    agent, *, final_response, api_call_count, interrupted, failed, messages, _turn_exit_reason,
    _pending_verification_response, _pending_verification_response_previewed, logger,
) -> Tuple[Any, Any, bool, Any]:
    """Iteration-budget exhaustion. Returns ``(final_response, _turn_exit_reason,
    preserved_verification_fallback, interrupted)``."""
    budget_exhausted = (
        api_call_count >= agent.max_iterations or agent.iteration_budget.remaining <= 0
    )
    preserved_verification_fallback = False
    if (
        final_response is None and budget_exhausted and not interrupted and not failed
        and str(_turn_exit_reason) in {"unknown", "budget_exhausted"}
    ):
        _turn_exit_reason = f"max_iterations_reached({api_call_count}/{agent.max_iterations})"
        if _pending_verification_response:
            # A verification gate withheld a composed answer, then the budget ran out:
            # preserve it rather than make another fallible call. The explicit pending
            # value is the provenance guard; unrelated error exits never enter here.
            # Previewed only if the reused candidate was actually streamed as interim.
            final_response = _pending_verification_response
            if _pending_verification_response_previewed:
                agent._response_was_previewed = True
            preserved_verification_fallback = True
        else:
            # _handle_max_iterations makes one extra toolless request for a summary.
            agent._emit_diagnostic_status(
                f"⚠️ Iteration budget exhausted ({api_call_count}/{agent.max_iterations}) "
                "— asking model to summarise"
            )
            if not agent.quiet_mode:
                agent._safe_print(
                    f"\n⚠️  Iteration budget exhausted ({api_call_count}/{agent.max_iterations}) "
                    "— requesting summary...", diagnostic=True,
                )
            _summary_start = time.time()
            try:
                final_response = agent._handle_max_iterations(messages, api_call_count)
            except InterruptedError:
                # The turn ends interrupted, so the pending interrupt message is returned
                # for requeue instead of being cleared behind a fallback summary. A redirect
                # also ends it: the budget is spent, so there is no loop to restart into.
                from agent.conversation_loop import INTERRUPT_WAITING_FOR_MODEL_PREFIX
                interrupted = True
                _turn_exit_reason = interrupted_during_api_call_reason(agent)
                final_response = f"{INTERRUPT_WAITING_FOR_MODEL_PREFIX}{time.time() - _summary_start:.1f}s elapsed)."

    # A kanban worker must record a terminal outcome whether or not a fallback path
    # was eligible, so the dispatcher learns the worker could not complete. Only the
    # dispatcher-owned worker owns the task: an in-process delegate_task child or cron run
    # inherits ``HERMES_KANBAN_TASK`` via os.environ but exhausting ITS budget must not
    # close the parent's run and release its claim (#112817).
    # Background-review forks are separate ``AIAgent`` instances (hardcoded max_iterations=16,
    # ``agent/background_review.py``) sharing this process's env: a fork exhausting ITS budget must
    # not record ``timed_out`` against the parent's card (fork, parity card t_c348a9f0).
    _is_review_fork = getattr(agent, "_memory_write_origin", None) == "background_review"
    _kanban_task = (
        os.environ.get("HERMES_KANBAN_TASK")
        if budget_exhausted and not _is_review_fork and is_dispatcher_owned_worker_context() else None
    )
    # If running as a kanban worker, signal the dispatcher that the worker could not complete (rather than
    # treating it as a protocol violation). This applies whether the user-facing fallback came from the
    # summary call or an explicitly pending continuation; both exhausted the task budget and must advance
    # the failure circuit. We route through ``_record_task_failure(outcome="timed_out")`` rather than
    # ``kanban_block`` so this counts toward the dispatcher's consecutive-failure circuit breaker (#29747
    # gap 2).
    # Bounded fallback (#87096): budget was exhausted but none of the normal fallback paths were eligible
    # (interrupted / failed / anomalous exit_reason). If running as a kanban worker we must still record a
    # terminal outcome so the task does not remain in an ambiguous lifecycle state. The worker's run is
    # closed via ``_record_task_failure`` (compare-and-swap receipt path) which is a no-op if another path
    # closed it — the CAS invariant in ``_end_run`` (``WHERE ended_at IS NULL``) guarantees idempotence.
    if _kanban_task:
        _record_kanban_budget_exhausted(_kanban_task, api_call_count, agent.max_iterations, logger)
    return final_response, _turn_exit_reason, preserved_verification_fallback, interrupted


def _rollback_interrupted_preflight_display(agent, interrupted) -> None:
    """Roll back the preflight-seeded display count only when an interrupt wins before
    any provider response; compaction state (incl. ``-1``) stays with the real-usage
    path. Type-pinned guards keep MagicMock/SimpleNamespace doubles inert."""
    _preflight_snapshot = getattr(agent, "_turn_preflight_display_snapshot", None)
    if (
        interrupted is True
        and isinstance(_preflight_snapshot, int)
        and not isinstance(_preflight_snapshot, bool)
        and getattr(agent, "_turn_received_provider_response", False) is not True
        and getattr(agent, "context_compressor", None) is not None
    ):
        _rollback_fn = getattr(
            agent.context_compressor, "rollback_interrupted_preflight_display_tokens", None
        )
        if callable(_rollback_fn):
            _rollback_fn(_preflight_snapshot)


def _drop_transcript_scaffolding(agent, messages) -> None:
    """Strip private retry scaffolding first, or a later "continue" replays
    assistant("(empty)") / recovery nudges into the same empty-response loop. Only
    the synthetic verification nudges go; the assistant candidate persists (#65919)."""
    agent._drop_trailing_empty_response_scaffolding(messages)
    _drop_verification_continuation_scaffolding(messages)
    # Recovered tool-call notices (fork #942): rows flagged ``_dropped_toolcall_nudge`` were
    # already replayed as the recovery pair; drop them and keep the flushed-prefix index honest.
    _flushed = getattr(agent, "_last_flushed_db_idx", 0)
    if isinstance(_flushed, int):
        agent._last_flushed_db_idx = sum(
            not m.get("_dropped_toolcall_nudge") for m in messages[:_flushed]
        )
    messages[:] = [m for m in messages if not m.get("_dropped_toolcall_nudge")]


def _recover_final_from_stream(agent, final_response, interrupted, failed) -> Tuple[Any, bool]:
    """An empty terminal completion is not authoritative when the stream already
    delivered text; recover before persist so a blank tail isn't frozen (#95514).
    Returns ``(final_response, recovered_from_stream)``. Called by the finalizer BEFORE
    the fallible tail-shaping/persist steps so the recovered text is already bound when
    one of them raises — a persist failure must not lose text the user already saw."""
    if interrupted or failed:
        return final_response, False
    _streamed = getattr(agent, "_current_streamed_assistant_text", "") or ""
    _streamed = _streamed.strip() if isinstance(_streamed, str) else ""
    if not (flatten_message_text(final_response).strip() if final_response else "") and _streamed:
        return _streamed, True
    return final_response, False


def _close_transcript_tail(agent, messages, final_response, interrupted, _recovered_from_stream) -> None:
    """Shape the transcript tail before the durable snapshot (scaffolding already dropped
    and ``final_response`` already stream-recovered by the caller)."""
    # An interrupt can leave a tool result as the tail; close the sequence so strict
    # providers don't see ``tool → user`` (placeholder: final_response is usually empty).
    if interrupted:
        from agent.message_sanitization import close_interrupted_tool_sequence
        close_interrupted_tool_sequence(messages, final_response, interrupted_assistant_tail=True)

    # Recovery ``break`` sites can return a final_response with no closing assistant
    # row; enforce "delivered final_response ⇒ assistant row" here. Compare content,
    # not role, so a matching verification candidate isn't dup'd.
    if final_response and not interrupted:
        # Some recovery/fallback paths return a real final_response without adding a closing assistant
        # message to the transcript (e.g. the partial-stream and prior-turn-content recovery ``break`` sites
        # in ``conversation_loop``). If persisted as-is, the durable session can end at a tool/user message
        # even though the caller — and the gateway platform — already saw a completed assistant response.
        # The next turn then replays a user-only backlog and the model re-answers every "unanswered"
        # message. Close the durable turn at the source, at the single chokepoint every recovery ``break``
        # flows through, so the invariant "delivered final_response ⇒ assistant row in transcript" holds
        # regardless of which path produced it. (#43849 / #44100) Compare content (not just role) so a
        # verification candidate that matches the final response is not duplicated at budget exhaustion.
        # (#65919 §7)
        _tail = messages[-1] if messages else None
        if not isinstance(_tail, dict) or _tail.get("role") != "assistant":
            append_message(messages, {"role": "assistant", "content": final_response})
        elif (
            _tail.get("content") != final_response
            and _assistant_row_missing_visible_text(_tail)
            and (_tail.get("tool_calls") or _recovered_from_stream)
        ):
            # Pure tool-call turn or stream-recovered blank (#95514): fill the persisted
            # blank row's content rather than append a second row.
            _tail["content"] = final_response
            stamp_message_timestamp(_tail)
            _tail.pop(_DB_PERSISTED_MARKER, None)
            agent._db_flush_scan_prefix = None

    # Request is complete, so replace API-local voice/model/skill guidance with the
    # clean user input before the durable snapshot (earlier flushes still needed them).
    # Earlier turn-start flushes use the DB-only override because their messages are still needed for the
    # API request; this finalizer runs after that request is complete (#48677 / #63766).
    _apply_override = getattr(agent, "_apply_persist_user_message_override", None)
    if callable(_apply_override):
        _apply_override(messages)


def _micro_compact_after_turn(agent, messages, final_response, logger, task_id) -> None:
    """Post-turn micro-compaction: absorb the oldest uncompacted exchange into the
    rolling summary before persist, amortizing compression across turns."""
    try:
        _compressor = getattr(agent, "context_compressor", None)
        # Strict `is True` + callable gates: plugin context engines and MagicMock
        # compressors pass duck checks and would wipe the transcript. Never run while
        # compression.checkpoint_required is armed (no checkpoint hook here), nor for
        # persistence-isolated agents (background review fork): that burns an aux-LLM
        # call on a throwaway transcript and could compact the CANONICAL session rows.
        if (
            _compressor
            and getattr(_compressor, '_micro_compact_enabled', False) is True
            and callable(getattr(_compressor, '_micro_compact', None))
            and final_response
            and getattr(agent, "compression_checkpoint_required", False) is not True
            and not getattr(agent, "_persist_disabled", False)
        ):
            _before = len(messages)
            _compacted = _compressor._micro_compact(messages)
            # Defrag rewrites the newest MICRO marker in place and pops _db_persisted;
            # the compressor flags us to invalidate the flush-scan cursor, else the
            # rewritten row is identity-skipped (stale).
            if getattr(_compressor, "_flush_scan_cursor_invalidated", False):
                _compressor._flush_scan_cursor_invalidated = False
                agent._db_flush_scan_prefix = None
            if isinstance(_compacted, list) and _compacted:
                _spliced = _compacted is not messages  # no-op and defrag passes return the input
                messages[:] = _compacted
                if _spliced:
                    # The splice summarized tool results away: a repeat read must serve them
                    # again, not an "unchanged" stub pointing at a body that is gone (#32106).
                    from agent.conversation_compression import _reset_read_dedup_caches

                    _reset_read_dedup_caches(task_id, session_id=agent.session_id or "")
            if _before != len(messages):
                logger.info("Micro-compaction: %d -> %d messages", _before, len(messages))
    except Exception as _mc_err:
        logger.info("Micro-compaction failed: %s", _mc_err)


def _log_turn_exit(agent, messages, final_response, api_call_count, _turn_exit_reason, interrupted, logger,
                   effective_task_id=None) -> None:
    """Always INFO so agent.log captures WHY every turn ended; WARNING when the last
    message is a tool result (the "just stops" scenario)."""
    _last_msg_role = messages[-1].get("role") if messages else None
    _last_tool_name = None
    if _last_msg_role == "tool":
        # Walk back to the assistant message with the tool call.
        for _m in reversed(messages):
            if _m.get("role") == "assistant" and _m.get("tool_calls"):
                _tcs = _m["tool_calls"]
                if _tcs and isinstance(_tcs[0], dict):
                    _last_tool_name = _tcs[-1].get("function", {}).get("name")
                break

    _turn_tool_count = sum(
        1 for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
    )
    # Fork turns (background review, side questions) carry ``_turn_origin``; tagging the
    # exit line keeps a fork's ``interrupted_during_api_call`` from reading as a killed
    # foreground stream — the fork shares the parent's session_id and often its model (#118693).
    _turn_origin = getattr(agent, "_turn_origin", None)
    _diag_msg = (
        "Turn ended: reason=%s model=%s api_calls=%d/%d budget=%d/%d "
        "tool_turns=%d last_msg_role=%s response_len=%d session=%s task=%s"
        + (" origin=%s" if _turn_origin else "")
    )
    _diag_args = (
        _turn_exit_reason, agent.model, api_call_count, agent.max_iterations,
        agent.iteration_budget.used if agent.iteration_budget else 0,
        agent.iteration_budget.max_total if agent.iteration_budget else 0,
        _turn_tool_count, _last_msg_role, len(final_response) if final_response else 0,
        agent.session_id or "none", effective_task_id or "none",
        *((_turn_origin,) if _turn_origin else ()),
    )
    if _last_msg_role == "tool" and not interrupted:
        logger.warning(
            "Turn ended with pending tool result (agent may appear stuck). "
            + _diag_msg + " last_tool=%s",
            *_diag_args, _last_tool_name,
        )
    else:
        logger.info(_diag_msg, *_diag_args)


def _append_file_mutation_footer(agent, final_response, logger):
    """Append the verifier advisory when ``write_file`` / ``patch`` calls failed and were
    never superseded by a successful write to the same path (surfaces over-claiming)."""
    try:
        # File-mutation verifier footer. This catches the specific case — reported by Ben Eng
        # (#15524-adjacent) — where a model issues a batch of parallel patches, half of them fail with
        # "Could not find old_string", and the model summarises the turn claiming every file was edited. The
        # user then has to manually run ``git status`` to catch the lie. With this footer the truth is
        # surfaced on every turn, so over-claiming is structurally impossible past the model. Gate: only
        # applied when a real text response exists for this turn and the user didn't interrupt.
        # Empty/interrupted turns already have other surface text that shouldn't be augmented.
        _failed = getattr(agent, "_turn_failed_file_mutations", None) or {}
        if _failed and agent._file_mutation_verifier_enabled():
            _failed = agent._file_mutations_still_failed(_failed)
            footer = agent._format_file_mutation_failure_footer(_failed)
            if footer:
                final_response = final_response.rstrip() + "\n\n" + footer
    except Exception as _ver_err:
        logger.debug("file-mutation verifier footer failed: %s", _ver_err)
    return final_response


def _explain_abnormal_exit(agent, final_response, _turn_exit_reason, preserved_verification_fallback, logger):
    """Turn-completion explainer: on abnormal exits, surface one explanation from
    ``_turn_exit_reason``. Only acts when no usable reply exists (empty, "(empty)",
    or a short unpunctuated fragment); ``text_response(...)`` exits stay silent."""
    try:
        if not agent._turn_completion_explainer_enabled():
            return final_response
        _stripped = (final_response or "").strip()
        _is_empty_terminal = _stripped in ("", "(empty)")
        # A short fragment not from a text_response exit and lacking sentence-ending
        # punctuation is treated as a truncated partial (#34452).
        _is_partial_fragment = (
            not _is_empty_terminal
            and not preserved_verification_fallback
            and not str(_turn_exit_reason).startswith("text_response")
            and len(_stripped) <= 24
            and _stripped[-1:] not in _SENTENCE_END
        )
        if _is_empty_terminal or _is_partial_fragment or str(_turn_exit_reason) == "partial_stream_recovery":
            _explanation = agent._format_turn_completion_explanation(
                _turn_exit_reason, getattr(agent, "_last_persistence_error_cause", None),
                db_path=getattr(getattr(agent, "_session_db", None), "db_path", None),
                model=str(getattr(agent, "model", "") or ""),
            )
            if _explanation:
                # Replace the bare sentinel; keep a partial fragment and append why.
                final_response = _explanation if _is_empty_terminal else _stripped + "\n\n" + _explanation
    except Exception as _exp_err:
        logger.debug("turn-completion explainer failed: %s", _exp_err)
    return final_response


def _last_turn_reasoning(messages) -> Optional[Any]:
    """Reasoning from the CURRENT turn only: stop at this turn's user message (#17055),
    but take the most recent non-empty reasoning since many providers emit it on the
    tool-call step and leave the final step with reasoning=None."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return None  # turn boundary — don't cross into prior turns
        if msg.get("role") == "assistant" and msg.get("reasoning"):
            return msg["reasoning"]
    return None


def _apply_output_hooks(
    agent, final_response, logger, *, platform, effective_task_id, turn_id, original_user_message,
    messages,
) -> Tuple[Any, bool, Optional[Any]]:
    """Resolve the turn's ``transform_llm_output`` outcome, then fire ``post_llm_call`` once per
    turn after the tool loop. Returns ``(final_response, transformed, pre_transform_response)``.

    The transform itself normally already ran before the assistant row was first persisted
    (``apply_llm_output_transform`` from ``finish_text_response`` / ``_persist_step``); this
    call returns that recorded outcome, and only fires the hook here when no earlier seam saw a
    response (e.g. text that only appeared through ``_explain_abnormal_exit``)."""
    final_response, transformed, pre_transform = apply_llm_output_transform(
        agent, final_response, turn_id=turn_id, platform=platform, logger=logger,
    )
    # Detached forks are internal work and must not publish turns under the parent's session ID.
    if not getattr(agent, "_persist_disabled", False):
        _invoke_hook_safely(
            "post_llm_call", logger,
            session_id=agent.session_id,
            task_id=effective_task_id,
            turn_id=turn_id,
            user_message=original_user_message,
            assistant_response=final_response,
            conversation_history=list(messages),
            model=agent.model,
            platform=platform,
        )
    return final_response, transformed, pre_transform


def apply_llm_output_transform(
    agent, final_response, *, turn_id, platform=None, logger=None,
) -> Tuple[Any, bool, Optional[Any]]:
    """Fire ``transform_llm_output`` once per turn and return
    ``(final_response, transformed, pre_transform_response)``.

    Called BEFORE the final assistant row is first persisted — from ``finish_text_response``
    ahead of its durable flush, and from ``finalize_turn._persist_step`` ahead of the
    recovery-path tail close — so the text the user sees is the text stored in SQLite/JSON and
    replayed next turn (#44239). SQLite treats a non-blank assistant row as settled (a re-flush
    adopts the stored content rather than overwriting it), so transforming after that first
    write can never reach the durable store. Idempotent per ``turn_id``: later callers in the
    same turn get the recorded outcome instead of a second hook firing. Only the current
    turn's not-yet-written text is touched — earlier turns and the system prompt are never
    rewritten (prompt-cache invariant)."""
    if logger is None:
        from agent.conversation_loop import logger
    recorded = getattr(agent, "_llm_output_transform", None)
    if isinstance(recorded, tuple) and len(recorded) == 3 and recorded[0] == turn_id:
        _, transformed, pre_transform = recorded
        return final_response, transformed, pre_transform
    if not final_response:
        return final_response, False, None
    if platform is None:
        platform = getattr(agent, "platform", None) or ""
    transformed, pre_transform = False, None
    # First hook to return a string wins; None/empty leaves the text unchanged.
    for _hook_result in _invoke_hook_safely(
        "transform_llm_output", logger,
        response_text=final_response,
        session_id=agent.session_id or "",
        model=agent.model,
        platform=platform,
        turn_id=turn_id,  # per-turn identity for the hook callback gate
    ):
        if isinstance(_hook_result, str) and _hook_result:
            pre_transform, final_response, transformed = final_response, _hook_result, True
            break
    agent._llm_output_transform = (turn_id, transformed, pre_transform)
    return final_response, transformed, pre_transform


def finalize_turn(
    agent, *, final_response, api_call_count, interrupted, failed, messages, conversation_history,
    effective_task_id, turn_id, user_message, original_user_message, _should_review_memory,
    _turn_exit_reason, _turn_calls=None, _pending_verification_response=None,
    _pending_verification_response_previewed=False,
):
    """Run the post-loop finalization and return the turn ``result`` dict."""
    from agent.conversation_loop import logger

    final_response, _turn_exit_reason, preserved_verification_fallback, interrupted = _resolve_budget_fallback(
        agent, final_response=final_response, api_call_count=api_call_count,
        interrupted=interrupted, failed=failed, messages=messages,
        _turn_exit_reason=_turn_exit_reason,
        _pending_verification_response=_pending_verification_response,
        _pending_verification_response_previewed=_pending_verification_response_previewed,
        logger=logger,
    )

    # A non-interrupted turn that fell out of the loop after a tool result, with no
    # follow-up assistant text, is the Desktop/TUI "silent stop" (#55316, #54756): the
    # composer returns to ready (or keeps spinning) while the durable transcript ends
    # at a raw ``tool`` row — the user never learns the turn stopped, and the next user
    # message lands as ``tool → user``. Interrupted tails keep
    # ``close_interrupted_tool_sequence``; this is the non-interrupt sibling. Mint the
    # exit reason, fail the turn, and synthesize the visible close so the tail close in
    # ``_persist_step`` persists an assistant row. A turn that already streamed text is
    # left alone: ``_recover_final_from_stream`` owns that recovery (#95514).
    if (
        not final_response
        and not interrupted
        and messages
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "tool"
        and not (getattr(agent, "_current_streamed_assistant_text", "") or "").strip()
    ):
        _turn_exit_reason = "pending_tool_result"
        failed = True
        final_response = ""
        try:
            if agent._turn_completion_explainer_enabled():
                final_response = (
                    agent._format_turn_completion_explanation("pending_tool_result", None) or ""
                )
        except Exception:
            final_response = ""
        if not final_response:
            # The turn-completion explainer opt-out must not reintroduce the silent stop.
            final_response = (
                "No reply: the turn stopped while a tool result was still pending. "
                "Send `continue` to let the model summarize."
            )

    # Loop exits that are failures in their own right (outer-loop error cap, shutdown, context
    # that could not be shrunk) carry the verdict the UI descriptor needs; a bare
    # ``turn_exit_reason`` collapsed to code="unknown", retryable=True on every surface.
    # Advisory verdicts (``fails_turn=False``) only add the code: ``failed``/``completed`` keep
    # the loop's values so cron, kanban and transcript persistence behave as before.
    _exit_failure = None if interrupted else exit_reason_failure(_turn_exit_reason)
    if _exit_failure is not None and _exit_failure.fails_turn:
        failed = True

    # Sibling producers (``turn_recovery``, ``codex_runtime``) return ``completed=False`` for an
    # interrupted turn; the gateway stream gate and the API run status rely on that contract.
    completed = (
        final_response is not None
        and not failed
        and not interrupted
        and (api_call_count < agent.max_iterations or str(_turn_exit_reason).startswith("text_response("))
    )

    _rollback_interrupted_preflight_display(agent, interrupted)

    from hermes_cli.observability.shared_metrics_harness import finish_turn
    finish_turn(agent, _turn_exit_reason, final_response, interrupted=interrupted, failed=failed)

    _cleanup_errors: List[str] = []
    # The model has answered (or the loop gave up): a title upgrade held back because it shares a
    # self-hosted endpoint with the main request (#117296) may go out now.
    from agent.turn_context import start_deferred_title_upgrade
    _guarded_cleanup("start_deferred_title_upgrade", lambda: start_deferred_title_upgrade(agent), _cleanup_errors, logger)
    # ``user_message`` may be a multimodal list of parts; the trajectory format wants a string.
    _guarded_cleanup(
        "save_trajectory",
        lambda: agent._save_trajectory(messages, _summarize_user_message_for_log(user_message), completed),
        _cleanup_errors, logger,
    )
    _guarded_cleanup(
        "cleanup_task_resources", lambda: agent._cleanup_task_resources(effective_task_id),
        _cleanup_errors, logger,
    )
    # Persist only after the transcript tail is shaped and scaffolding removed. Each
    # sub-step runs in the same order as the original inline block, and the
    # stream-recovered ``final_response`` is rebound the moment it is computed — BEFORE
    # the fallible tail-shaping / override / micro-compaction / persist calls — so a
    # raise in any of them can't drop text the user already saw (#95514, #8049).
    def _persist_step():
        nonlocal final_response
        _drop_transcript_scaffolding(agent, messages)
        final_response, _recovered_from_stream = _recover_final_from_stream(
            agent, final_response, interrupted, failed
        )
        # Recovery paths (stream-recovered / prior-turn text) reach here with a response no
        # earlier seam transformed; the normal text turn already did this before its flush and
        # gets the recorded outcome back. Either way the tail close below writes the text the
        # user will see, never the raw model text (#44239).
        if final_response and not interrupted:
            final_response, _, _ = apply_llm_output_transform(agent, final_response, turn_id=turn_id, logger=logger)
        _close_transcript_tail(agent, messages, final_response, interrupted, _recovered_from_stream)
        if not interrupted and not failed:
            _micro_compact_after_turn(agent, messages, final_response, logger, effective_task_id)
        agent._persist_session(messages, conversation_history)

    _guarded_cleanup("persist_session", _persist_step, _cleanup_errors, logger)

    # Keep the gateway's separate in-memory history snapshot current even on
    # cleanup error, so a later prompt isn't sent with a pre-turn snapshot.
    with suppress(Exception):
        agent._session_messages = messages

    _log_turn_exit(agent, messages, final_response, api_call_count, _turn_exit_reason, interrupted, logger,
                   effective_task_id=effective_task_id)

    # Response transforms apply only to real, uninterrupted responses.
    if final_response and not interrupted:
        final_response = _append_file_mutation_footer(agent, final_response, logger)
    if not interrupted:
        final_response = _explain_abnormal_exit(
            agent, final_response, _turn_exit_reason, preserved_verification_fallback, logger,
        )

    _platform = getattr(agent, "platform", None) or ""
    _response_transformed = False
    _pre_transform_response = None
    if final_response and not interrupted:
        final_response, _response_transformed, _pre_transform_response = _apply_output_hooks(
            agent, final_response, logger, platform=_platform, effective_task_id=effective_task_id,
            turn_id=turn_id, original_user_message=original_user_message, messages=messages,
        )

    # Context engine observation hook: the turn finished with the finalized transcript.
    # Fail-open. ``_last_turn_usage`` is the last response's canonical usage dict, or
    # ``None`` on turns that never reached a provider response — by contract.
    try:
        from agent.conversation_loop import _notify_context_engine_turn_complete
        _notify_context_engine_turn_complete(
            agent, messages, usage=getattr(agent, "_last_turn_usage", None), logger=logger,
            turn_id=turn_id, task_id=effective_task_id, api_call_count=api_call_count,
            interrupted=interrupted, failed=failed, turn_exit_reason=_turn_exit_reason,
        )
    except Exception as exc:
        logger.warning("on_turn_complete notification failed: %s", exc)

    # Surrogate chokepoint: RAW SDK text with a lone UTF-16 surrogate crashes downstream
    # consumers (stdout, Telegram ``utf16_len``, JSON); scrub once where it leaves the loop.
    # Class-level surrogate chokepoint (#80366, #55143, #55309, #19819): ``final_response`` is often the RAW
    # SDK content (``assistant_message.content``), not the sanitized copy stored in history by
    # ``build_assistant_message``. Any lone UTF-16 surrogate (U+D800–U+DFFF) in it crashes downstream
    # consumers — oneshot stdout writes, Telegram's ``utf16_len`` length check, Signal formatting, JSON
    # envelope encodes — on every provider (Ollama, NVIDIA NIM, …). Scrub once here, where model text leaves
    # the conversation loop, so every delivery surface receives valid Unicode.
    if isinstance(final_response, str):
        final_response = _sanitize_surrogates(final_response)

    result = {
        "final_response": final_response,
        "last_reasoning": _last_turn_reasoning(messages),
        "messages": messages,
        "api_calls": api_call_count,
        "completed": completed,
        "turn_exit_reason": _turn_exit_reason,
        "failed": failed,
        "partial": False,  # True only when stopped due to invalid tool calls
        "interrupted": interrupted,
        "response_transformed": _response_transformed,
        "pre_transform_response": _pre_transform_response,
        "response_previewed": getattr(agent, "_response_was_previewed", False),
        "model": agent.model,
        # requested_model / served_model: proxy-reported deployment or Hermes' own fallback route.
        **result_model_fields(agent),
        "provider": agent.provider,
        "base_url": agent.base_url,
        **{key: getattr(agent, f"session_{key}") for key in _SESSION_TOKEN_KEYS},
        # Gateway SessionEntry persists an API reading, never the preflight display seed.
        "last_prompt_tokens": (
            getattr(agent.context_compressor, "last_real_prompt_tokens", agent.context_compressor.last_prompt_tokens)
            if getattr(agent.context_compressor, "last_prompt_tokens", 0) > 0
            else getattr(agent.context_compressor, "last_prompt_tokens", 0)
        ) or 0,
        **{key: getattr(agent, f"session_{key}") for key in _SESSION_COST_KEYS},
        # Requested service tier, for billing audits (`hermes -z --usage-file`).
        "service_tier": (
            (getattr(agent, "request_overrides", {}) or {}).get("service_tier")
            or (
                (getattr(agent, "request_overrides", {}) or {}).get("extra_body") or {}
            ).get("service_tier")
        ),
        # Tier the provider reports it actually SERVED (may differ from the
        # request: `default` for an ultrafast request = silent downgrade).
        "service_tier_served": getattr(agent, "_served_service_tier", None),
        "session_id": agent.session_id,
    }
    if agent._tool_guardrail_halt_decision is not None:
        result["guardrail"] = agent._tool_guardrail_halt_decision.to_metadata()
    # Persistence failures already set failed=True; also stamp `error` so the gateway
    # surfaces status="error" (desktop can toast) instead of a quiet complete frame, plus
    # the machine-readable cause 'session_persistence_failed:<locked|compression|...>'.
    if failed and str(_turn_exit_reason) == "session_persistence_failed":
        from hermes_constants import profile_cli_selector

        # Never rebind final_response here: the memory sync and the background-review gate
        # below must still see an empty response on a persistence-failed turn.
        result["error"] = final_response or (
            "session storage could not be written — check the state database "
            f"health (`hermes {profile_cli_selector()}doctor`), then send your message again"
        )
        _cause = getattr(agent, "_last_persistence_error_cause", None)
        result["failure_reason"] = "session_persistence_failed:" + (_cause or "unknown")
    elif _exit_failure is not None:
        if failed:
            result["error"] = final_response or str(_turn_exit_reason)
        stamp_failure(result, _exit_failure.reason, _exit_failure.retryable)
    # Cleanup failures are surfaced, but the response is returned either way (#8049).
    if _cleanup_errors:
        result["cleanup_errors"] = _cleanup_errors
    # A /steer landing after the final assistant turn has no tool batch to drain into;
    # hand it back so it becomes the next user turn instead of being lost.
    _leftover_steer = agent._drain_pending_steer()
    if _leftover_steer:
        result["pending_steer"] = _leftover_steer
    agent._response_was_previewed = False
    if interrupted and agent._interrupt_message:
        result["interrupt_message"] = agent._interrupt_message
    agent.clear_interrupt()
    agent._stream_callback = None  # don't leak into future calls

    # Skill trigger is checked NOW — based on how many tool iterations THIS turn used.
    _should_review_skills = (
        agent._skill_nudge_interval > 0
        and agent._iters_since_skill >= agent._skill_nudge_interval
        and "skill_manage" in agent.valid_tool_names
    )
    if _should_review_skills:
        agent._iters_since_skill = 0

    # External memory provider: sync the completed turn + queue next prefetch.
    agent._sync_external_memory_for_turn(
        original_user_message=original_user_message, final_response=final_response,
        interrupted=interrupted, messages=messages,
    )

    # Background memory/skill review runs AFTER delivery so it never competes with the
    # user's task. Suppressed by skip_background_review (e.g. cron): the fork costs
    # ~30K tokens / event with no human-in-the-loop benefit. Best-effort; the review
    # clones the snapshot structurally so its sanitizers can't reach the live transcript.
    if (
        final_response
        and not interrupted
        and not getattr(agent, "skip_background_review", False)
        and (_should_review_memory or _should_review_skills)
    ):
        with suppress(Exception):
            agent._spawn_background_review(
                messages_snapshot=list(messages), review_memory=_should_review_memory,
                review_skills=_should_review_skills,
            )

    # Memory provider on_session_end()/shutdown_all() are NOT called here:
    # run_conversation() runs once per message; CLI/gateway own session-end cleanup.
    # Fork: every turn (detached review forks included) emits through emit_session_end so each
    # Blackbox call-ledger row gets its turns row (#1056); the fork's turn carries
    # ``_blackbox_is_subagent``/parent ids instead of being suppressed.
    emit_session_end(
        agent,
        turn_id=turn_id,
        effective_task_id=effective_task_id,
        completed=completed,
        failed=failed,
        interrupted=interrupted,
        turn_exit_reason=_turn_exit_reason,
        original_user_message=original_user_message,
        final_response=final_response,
        turn_calls=_turn_calls,
    )

    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    return result


def emit_session_end(
    agent,
    *,
    turn_id,
    effective_task_id,
    completed,
    failed,
    interrupted,
    turn_exit_reason,
    original_user_message,
    final_response,
    turn_calls=None,
    provisional=False,
):
    """Fire the per-turn ``on_session_end`` plugin hook exactly once.

    ``provisional=True`` (host abandoning an in-flight turn) fires
    ``on_turn_abandoned`` instead and does NOT set the per-turn emitted
    marker, so if the turn later unwinds its real finalize or backstop emit
    still fires. Returns False when a provisional emit stood down.

    Shared by ``finalize_turn`` (the normal loop exit) and
    ``emit_unfinalized_session_end`` (the backstop for the conversation loop's
    early ``return``s and raised exceptions, which never reach the finalizer).
    """
    from agent.conversation_loop import logger

    # Plugin hook: on_session_end
    # Fired at the very end of every run_conversation call (i.e. once per TURN,
    # despite the name). Plugins can use this for cleanup, flushing buffers,
    # and per-turn telemetry. The `turn_usage` kwarg is an ADDITIVE, optional
    # payload (Blackbox plugin) — existing consumers take **kwargs and ignore
    # it; invoke_hook wraps each callback in try/except so a strict-signature
    # callback cannot break the loop.
    try:
        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
        # Fold the per-turn accumulator into a compact summary. Telemetry must
        # never break the turn, so guard the fold.
        _turn_calls = turn_calls or []
        _turn_usage = None
        _blackbox_compaction = getattr(agent, "_blackbox_compaction", None)
        if not isinstance(_blackbox_compaction, dict):
            _blackbox_compaction = {}
        try:
            if _turn_calls:
                # Last-call cache split — the FINAL provider call's own
                # cache_read/cache_write/uncached. Distinct from the summed
                # cache_* totals above (which are whole-turn billing and
                # double-count re-sent context). These three sum to the final
                # call's prompt_tokens == context_used, so they decompose the
                # context WINDOW (occupancy) rather than the turn's SPEND. The
                # /context "Context window" line renders this split so the
                # window numbers are visibly different from the billed sums.
                _last_call = _turn_calls[-1]
                _last_cache_read = int(_last_call.get("cache_read_tokens", 0) or 0)
                _last_cache_write = int(_last_call.get("cache_write_tokens", 0) or 0)
                _last_uncached = int(_last_call.get("input_tokens", 0) or 0)
                # Discriminator for the four FINAL-call figures (the split above
                # plus context_used). The turn-level flags in _rollup_turn_usage
                # are absorbing across every call, so they answer "did ANY call
                # go unmeasured", not "is this split real" — a turn whose call
                # #2 returned no usage would otherwise blank a fully measured
                # final call's window numbers (r6 finding 9).
                _last_call_prompt_unknown = any(
                    bool(_last_call.get(k))
                    for k in ("input_tokens_unknown", "cache_read_tokens_unknown",
                              "cache_write_tokens_unknown", "usage_unknown")
                )
                # Request composition of the FINAL call — the char/4 fixed vs
                # non-fixed breakdown of the exact payload that produced the
                # window occupancy (context_used). This is the authoritative
                # "what was in the last request" record /context + /usage read.
                _last_composition = _last_call.get("composition") or None
                # Per-call composition history (small ints, ~60B/call) so
                # /cost <turn_id> can show how the request grew call-by-call.
                _comp_calls = [
                    {
                        "composition": c.get("composition"),
                        "output_tokens": int(c.get("output_tokens", 0) or 0),
                        "reasoning_tokens": int(c.get("reasoning_tokens", 0) or 0),
                    }
                    for c in _turn_calls
                ]
                _turn_usage = {
                    **_rollup_turn_usage(_turn_calls),
                    "latency_s": sum(c.get("latency_s", 0.0) for c in _turn_calls),
                    # Per-call breakdown so the plugin can price each call
                    # against tiered pricing and reconcile cost_status worst-of.
                    "calls": list(_turn_calls),
                    "context_used": getattr(agent.context_compressor, "last_prompt_tokens", 0)
                        if getattr(agent, "context_compressor", None) else 0,
                    "context_length": getattr(agent.context_compressor, "context_length", 0)
                        if getattr(agent, "context_compressor", None) else 0,
                    # Last-call cache split (decomposes the context window).
                    "last_cache_read_tokens": _last_cache_read,
                    "last_cache_write_tokens": _last_cache_write,
                    "last_uncached_tokens": _last_uncached,
                    "last_call_prompt_unknown": _last_call_prompt_unknown,
                    # Real request composition (fixed vs non-fixed, char/4) of
                    # the final call + per-call history. See compose_request_breakdown.
                    "last_composition": _last_composition,
                    "composition_calls": _comp_calls,
                    # Subagent attribution (set by delegate_tool before run; absent at top level).
                    "parent_turn_id": getattr(agent, "_blackbox_parent_turn_id", None),
                    "parent_platform": getattr(agent, "_blackbox_parent_platform", None),
                    "parent_chat_id": getattr(agent, "_blackbox_parent_chat_id", None),
                    "parent_chat_name": getattr(agent, "_blackbox_parent_chat_name", None),
                    "is_subagent": bool(getattr(agent, "_blackbox_is_subagent", False)),
                    # Depth tracking (B1): read from the agent attribute set at
                    # construction time (0 for parents, parent+1 for children).
                    "depth": getattr(agent, "_blackbox_depth", None),
                    **_blackbox_compaction,
                }
            elif any(k != "idle_compaction_fired" or v for k, v in _blackbox_compaction.items()):
                # The prologue seeds {"idle_compaction_fired": False} on every
                # turn; only a compaction that actually recorded something makes
                # a call-less turn carry usage. Otherwise stay None so the store
                # does not persist fabricated known zeros (C7 k84).
                _turn_usage = dict(_blackbox_compaction)
        except Exception:
            _turn_usage = None
        # Mark BEFORE invoking: the run_agent forwarder's backstop
        # (emit_unfinalized_session_end) must never fire a second hook for a
        # turn whose finalizer already attempted one.
        # The marker check and the hook write are one critical section per
        # agent: a provisional (shutdown) emit racing the turn's own finalize
        # must either land first (and be upserted over) or see the real
        # marker and stand down -- never write interrupted AFTER the real row.
        with _session_end_lock(agent):
            if provisional:
                # Re-read under the lock: the turn may have finished (marker
                # set) or the agent moved on to a newer turn while we waited.
                if getattr(agent, "_session_end_emitted_turn_id", None) == turn_id:
                    return False
                if getattr(agent, "_current_turn_id", None) != turn_id:
                    return False
                # NOT on_session_end: its consumers do end-of-turn teardown
                # (file cleanup, call hangup) that must not run on a turn that
                # is still live. Usage is left to the consumer's own per-call
                # ledger; only identity + attribution travel here.
                _invoke_hook(
                    "on_turn_abandoned",
                    session_id=agent.session_id,
                    task_id=effective_task_id,
                    turn_id=turn_id,
                    reason=turn_exit_reason,
                    model=agent.model,
                    platform=getattr(agent, "platform", None) or "",
                    provider=getattr(agent, "provider", None) or "",
                    chat_id=getattr(agent, "_chat_id", None) or "",
                    chat_name=getattr(agent, "_chat_name", None) or "",
                    user_message=original_user_message,
                    parent_turn_id=getattr(agent, "_blackbox_parent_turn_id", None),
                    parent_platform=getattr(agent, "_blackbox_parent_platform", None),
                    parent_chat_id=getattr(agent, "_blackbox_parent_chat_id", None),
                    parent_chat_name=getattr(agent, "_blackbox_parent_chat_name", None),
                    is_subagent=bool(getattr(agent, "_blackbox_is_subagent", False)),
                    depth=getattr(agent, "_blackbox_depth", None),
                )
                return True
            agent._session_end_emitted_turn_id = turn_id
            # (turn_id, marker) stamped by the CLI's signal handler before it
            # interrupts the loop: names an external termination
            # (``signal_15``) on THIS turn's row. Additive kwarg; None otherwise.
            _stamp = getattr(agent, "_turn_terminal_error", None)
            _terminal_error = (
                _stamp[1]
                if isinstance(_stamp, tuple) and len(_stamp) == 2 and _stamp[0] == turn_id
                else None
            )
            _invoke_hook(
                "on_session_end",
                session_id=agent.session_id,
                task_id=effective_task_id,
                turn_id=turn_id,
                completed=completed,
                failed=failed,
                interrupted=interrupted,
                turn_exit_reason=turn_exit_reason,
                terminal_error=_terminal_error,
                model=agent.model,
                platform=getattr(agent, "platform", None) or "",
                provider=getattr(agent, "provider", None) or "",
                chat_id=getattr(agent, "_chat_id", None) or "",
                chat_name=getattr(agent, "_chat_name", None) or "",
                user_message=original_user_message,
                final_response=final_response,
                turn_usage=_turn_usage,
                cli_invocation_id=getattr(agent, "_cli_invocation_id", None),
            )
    except Exception as exc:
        logger.warning("on_session_end hook failed: %s", exc)
    return True


_SESSION_END_LOCKS_GUARD = threading.Lock()
_SESSION_END_FALLBACK_LOCK = threading.RLock()


def _session_end_lock(agent):
    """Per-agent RLock serializing on_session_end emits for that agent.

    Read from the instance ``__dict__``, never ``getattr``: agent doubles with
    a catch-all ``__getattr__`` would hand back a non-lock.
    """
    slots = getattr(agent, "__dict__", None)
    if not isinstance(slots, dict):
        return _SESSION_END_FALLBACK_LOCK
    lock = slots.get("_session_end_emit_lock")
    if lock is not None:
        return lock
    with _SESSION_END_LOCKS_GUARD:
        return slots.setdefault("_session_end_emit_lock", threading.RLock())


def emit_unfinalized_session_end(agent, turn_id, *, result=None, exc=None, abandoned_reason=None):
    """Backstop: fire ``on_session_end`` for a turn that bypassed ``finalize_turn``.

    ``run_conversation`` has dozens of early ``return``s (fallback chain
    exhausted, interrupt unwind, truncated tool args, billing/content-policy
    blocks, ...) and can raise; none of them reach ``finalize_turn``, so the
    hook documented as "fired once per turn" never fired and Blackbox wrote
    ``turn_api_calls`` rows with no parent ``turns`` row. The ``run_agent``
    forwarder calls this after every turn, successful or not; it is a no-op
    when the finalizer already emitted for ``turn_id`` or the turn never
    started. Returns True when it emitted. Never raises.

    ``abandoned_reason`` marks a turn the HOST is abandoning while it is still
    in flight (gateway shutdown drain timed out, the process is about to
    exit). That emit is provisional: it fires ``on_turn_abandoned`` (not
    ``on_session_end``), leaves the per-turn emitted marker unset so a turn
    that does unwind later still emits its real row (which supersedes the
    provisional one), and does not settle ``_billed_unaccounted``, which the
    turn's own thread still owns.
    """
    try:
        if not turn_id or getattr(agent, "_current_turn_id", None) != turn_id:
            return False
        if getattr(agent, "_session_end_emitted_turn_id", None) == turn_id:
            return False
        if abandoned_reason and getattr(agent, "_session_end_abandoned_turn_id", None) == turn_id:
            return False
        # Both are (turn_id, value) pairs so a raise before the loop published
        # them for THIS turn can never attribute the previous turn's data.
        published = getattr(agent, "_blackbox_turn_calls", None)
        turn_calls = []
        if isinstance(published, tuple) and len(published) == 2 and published[0] == turn_id:
            turn_calls = published[1] if isinstance(published[1], list) else []
        published_msg = getattr(agent, "_turn_original_user_message", None)
        user_message = None
        if isinstance(published_msg, tuple) and len(published_msg) == 2 and published_msg[0] == turn_id:
            user_message = published_msg[1]
        try:
            # Billed responses the loop rejected before bailing out belong to
            # THIS turn; finalize_turn settles them the same way. Not for an
            # abandoned turn: its thread is still live and owns that list.
            if not abandoned_reason:
                from agent.conversation_loop import _settle_unaccepted_billed_responses

                _settle_unaccepted_billed_responses(agent, turn_calls, turn_id)
        except Exception:
            pass
        # Copy: an abandoned turn's own thread may still be appending.
        turn_calls = list(turn_calls)
        res = result if isinstance(result, dict) else {}
        final_response = ""
        if abandoned_reason:
            interrupted = True
            failed = False
            reason = str(abandoned_reason)[:200]
        elif exc is not None:
            interrupted = isinstance(exc, (KeyboardInterrupt, InterruptedError)) or (
                type(exc).__name__ == "CancelledError"
            )
            failed = not interrupted
            reason = f"exception:{type(exc).__name__}"
        else:
            interrupted = res.get("interrupted") is True
            failed = not interrupted and bool(res.get("failed") or res.get("error"))
            if interrupted:
                reason = "early_return:interrupted"
            elif failed:
                detail = str(res.get("error") or "failed").strip().splitlines()
                reason = "early_return:" + (detail[0] if detail else "failed")[:200]
            else:
                reason = "early_return"
                final_response = res.get("final_response") or ""
        emitted = emit_session_end(
            agent,
            turn_id=turn_id,
            effective_task_id=getattr(agent, "_current_task_id", None),
            completed=bool(res.get("completed")) and not failed and not interrupted,
            failed=failed,
            interrupted=interrupted,
            turn_exit_reason=reason,
            original_user_message=user_message,
            final_response=final_response,
            turn_calls=turn_calls,
            provisional=bool(abandoned_reason),
        )
        if abandoned_reason and emitted:
            agent._session_end_abandoned_turn_id = turn_id
        return bool(emitted)
    except Exception:
        logging.getLogger(__name__).warning(
            "unfinalized on_session_end backstop failed", exc_info=True
        )
        return False


def emit_abandoned_session_ends(agents, reason):
    """Record every still-in-flight turn of ``agents`` (and their live
    subagents) as interrupted before the host process abandons them.

    A host that exits while a turn is still running (gateway shutdown after
    the drain + interrupt-settle window expired; the turn is blocked in a
    provider stream or a tool) never returns from ``run_conversation``, so
    neither ``finalize_turn`` nor the ``run_agent`` backstop fires and
    Blackbox is left with ``turn_api_calls`` rows and no ``turns`` row. This is
    the gateway counterpart of cli ``_emit_interrupted_session_end``. Turns
    that already emitted (finished, early-returned) are skipped by the
    per-turn marker, so it is safe to call on every agent. Returns the number
    of turns it emitted for. Never raises.
    """
    emitted = 0
    seen = set()
    stack = list(agents or ())
    while stack:
        agent = stack.pop()
        if agent is None or id(agent) in seen:
            continue
        seen.add(id(agent))
        try:
            lock = getattr(agent, "_active_children_lock", None)
            children = getattr(agent, "_active_children", None) or ()
            if lock is not None:
                with lock:
                    children = list(children)
            stack.extend(list(children))
        except Exception:
            pass
        turn_id = getattr(agent, "_current_turn_id", None)
        if not turn_id:
            continue
        # Write under the profile the turn ran in (multiplex gateway), not the
        # shutdown thread's: its turn_api_calls live in that profile's ledger.
        home = getattr(agent, "_turn_home", None)
        token = None
        if isinstance(home, tuple) and len(home) == 2 and home[0] == turn_id and home[1]:
            try:
                from hermes_constants import set_hermes_home_override

                token = set_hermes_home_override(home[1])
            except Exception:
                token = None
        try:
            if emit_unfinalized_session_end(agent, turn_id, abandoned_reason=reason):
                emitted += 1
        finally:
            if token is not None:
                try:
                    from hermes_constants import reset_hermes_home_override

                    reset_hermes_home_override(token)
                except Exception:
                    pass
    return emitted
