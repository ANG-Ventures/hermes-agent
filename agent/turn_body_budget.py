"""Serialized request-body byte ceiling for the turn loop (fork).

Token occupancy does not model base64/JSON bytes. Providers with a declared HTTP body
ceiling get a serialized-size preflight after request middleware (``preflight_request_body``),
a re-check at the terminal edge once execution middleware has replaced the payload
(``terminal_request_body``), and a ``body_too_large`` 413 recovery that remediates retained
images once and otherwise fails the turn actionably (``recover_body_too_large``). Remediation is
request-copy-only and can reach images inside the protected fresh tail; text compaction is
deliberately never involved in byte overflow recovery.

Sibling of ``agent.turn_api_request`` / ``turn_api_call`` / ``turn_api_error``; imports
``agent.request_body_budget`` lazily like the fork loop did.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent.conversation_loop")


def _body_cap(agent: Any) -> Optional[int]:
    from agent.request_body_budget import request_body_limit_for_provider

    return request_body_limit_for_provider(
        getattr(agent, "provider", None), getattr(agent, "model", None),
        api_mode=getattr(agent, "api_mode", None),
    )


def body_budget_failure(
    agent: Any, error_message: str, *, messages: List[Dict[str, Any]], conversation_history: Any,
    api_call_count: int, thinking_spinner: Any = None,
) -> Dict[str, Any]:
    """Terminal turn result for an unsatisfiable byte cap (stops the spinner, persists)."""
    if thinking_spinner:
        thinking_spinner.stop("")
    if agent.thinking_callback:
        agent.thinking_callback("")
    agent._flush_status_buffer()
    agent._vprint(f"{agent.log_prefix}❌ {error_message}", force=True)
    logger.error("%s%s", agent.log_prefix, error_message)
    agent._persist_session(messages, conversation_history)
    return {
        "final_response": error_message, "messages": messages, "completed": False,
        "api_calls": api_call_count, "error": error_message, "partial": True, "failed": True,
        "body_too_large": True,
    }


def preflight_request_body(agent: Any, api_kwargs: Any) -> Tuple[Any, Optional[str]]:
    """``(api_kwargs, error)``: the (possibly remediated) request, or the failure text when
    image remediation cannot satisfy the provider's declared byte cap."""
    try:
        from agent.request_body_budget import body_budget_error, format_byte_count, remediate_request_body

        cap = _body_cap(agent)
        if cap is None:
            return api_kwargs, None
        result = remediate_request_body(api_kwargs, max_body_bytes=cap)
        if result.before_bytes <= cap:
            return api_kwargs, None
        if not result.fits:
            return api_kwargs, body_budget_error(result)
        agent._buffer_status(
            "📐 Request body exceeded provider byte cap — "
            f"resized {result.resized_images} and evicted {result.evicted_images} image(s) "
            f"({format_byte_count(result.before_bytes)} → {format_byte_count(result.after_bytes)})."
        )
        logger.info(
            "%srequest-body preflight remediated %d -> %d bytes (cap=%d, resized=%d, evicted=%d)",
            agent.log_prefix, result.before_bytes, result.after_bytes, cap,
            result.resized_images, result.evicted_images,
        )
        return result.request_kwargs, None
    except (TypeError, ValueError) as exc:
        # A provider profile must never brick a request because a middleware injected a
        # non-JSON SDK object; the provider's own serializer stays authoritative then.
        logger.warning("%srequest-body preflight skipped: %s", agent.log_prefix, exc)
        return api_kwargs, None


def terminal_request_body(agent: Any, next_api_kwargs: Any) -> Any:
    """Re-check at the terminal edge (execution middleware may have replaced the payload) so the
    measured object is exactly what the SDK receives. Raises ``RequestBodyBudgetExceeded`` when
    remediation cannot satisfy the cap."""
    try:
        from agent.request_body_budget import (
            RequestBodyBudgetExceeded, format_byte_count, remediate_request_body,
        )

        cap = _body_cap(agent)
        result = remediate_request_body(next_api_kwargs, max_body_bytes=cap) if cap is not None else None
    except (TypeError, ValueError) as exc:
        logger.warning("%srequest-body terminal preflight skipped: %s", agent.log_prefix, exc)
        return next_api_kwargs
    if result is None or result.before_bytes <= result.max_body_bytes:
        return next_api_kwargs
    if not result.fits:
        raise RequestBodyBudgetExceeded(result)
    agent._buffer_status(
        "📐 Execution middleware exceeded the provider byte cap — "
        f"resized {result.resized_images} and evicted {result.evicted_images} image(s) "
        f"({format_byte_count(result.before_bytes)} → {format_byte_count(result.after_bytes)})."
    )
    return result.request_kwargs


def recover_body_too_large(
    agent: Any, api_error: BaseException, *, api_kwargs: Any, api_messages: Any, _retry: Any,
) -> Tuple[Optional[Any], Optional[str]]:
    """Whole-body byte overflow is distinct from token/context overflow: a structured
    ``request_too_large`` 413 must never enter text compaction (retained images can live in the
    protected fresh tail, where summarization is a no-op). Returns ``(api_messages, None)`` when
    retained images were remediated and the request may be retried once, else
    ``(None, error_message)`` for the terminal failure."""
    from agent.conversation_compression import emit_image_eviction_attempt_telemetry
    from agent.request_body_budget import (
        RequestBodyBudgetExceeded, body_budget_error, remediate_request_body,
        request_body_limit_from_error, serialized_request_body_size,
    )

    started_at = time.monotonic()
    made_progress = False
    if isinstance(api_error, RequestBodyBudgetExceeded):
        return None, str(api_error)
    caps = [cap for cap in (_body_cap(agent), request_body_limit_from_error(api_error)) if cap is not None]
    recovery_cap = min(caps) if caps else None
    if recovery_cap is not None:
        rejected = remediate_request_body(api_kwargs, max_body_bytes=recovery_cap)
        made_progress = rejected.after_bytes < rejected.before_bytes
        if not _retry.body_byte_retry_attempted and rejected.fits and rejected.image_count > 0:
            _retry.body_byte_retry_attempted = True
            # ``api_kwargs`` may already be Anthropic-native (images nested under
            # tool_result.content). Apply the same byte reduction to the pre-transport message
            # copy so the normal retry rebuild keeps it.
            required_reduction = max(1, rejected.before_bytes - rejected.after_bytes)
            wrapper = {"messages": api_messages}
            target = max(1, serialized_request_body_size(wrapper) - required_reduction - 1024)
            message_result = remediate_request_body(wrapper, max_body_bytes=target)
            made_progress = bool(made_progress or message_result.after_bytes < message_result.before_bytes)
            if message_result.resized_images or message_result.evicted_images:
                agent._buffer_status(
                    "📐 Provider rejected the serialized request body — "
                    "remediated retained images and retrying once..."
                )
                emit_image_eviction_attempt_telemetry(agent, started_at=started_at)
                return message_result.request_kwargs["messages"], None
        error_message = body_budget_error(rejected).replace(
            "before provider dispatch", "after provider rejection"
        )
    else:
        actual_bytes = serialized_request_body_size(api_kwargs)
        probe = remediate_request_body(api_kwargs, max_body_bytes=max(1, actual_bytes))
        made_progress = probe.after_bytes < probe.before_bytes
        error_message = (
            "Request body too large after provider rejection: "
            f"body={actual_bytes} bytes, cap=unknown (provider/profile did not report one), "
            f"images={probe.image_bytes} bytes across {probe.image_count} images. "
            "Reduce image attachments or run /new to start a new session."
        )
    emit_image_eviction_attempt_telemetry(
        agent, started_at=started_at,
        failure_class="insufficient_progress" if made_progress else "no_progress",
    )
    return None, error_message
