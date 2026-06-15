"""Telemetry helpers for native_content_slimmer hooks."""

from __future__ import annotations

from collections.abc import Callable, MutableSequence
from typing import Any, Mapping

from plugins.blackbox.native_slimmer_schema import build_native_slimmer_event


class NativeSlimmerTelemetryBuffer:
    """Small in-memory sink used by tests and fail-open hook runtime.

    The real Blackbox persistence layer can consume the same event dicts later;
    this sink keeps the hook side-effect free unless a caller injects a concrete
    emitter.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        self.records.append(dict(event))


def build_replacement_event(
    *,
    mode: str,
    action: str,
    tool_name: str,
    session_id: str,
    tool_call_id: str,
    artifact_id: str,
    raw_sha256: str,
    raw_source: str,
    original_bytes: int,
    emitted_bytes: int,
    classification_reason: str,
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    tool_status: str = "success",
    status_quo_baseline_bytes: int | None = None,
) -> dict[str, Any]:
    """Build the Blackbox-native telemetry row for one replacement decision."""

    return build_native_slimmer_event(
        mode=mode,
        action=action,
        tool_name=tool_name,
        session_id=session_id,
        tool_call_id=tool_call_id,
        artifact_id=artifact_id,
        raw_sha256=raw_sha256,
        raw_source=raw_source,
        original_bytes=original_bytes,
        emitted_bytes=emitted_bytes,
        classification_reason=classification_reason,
        lossy=False,
        task_id=task_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
        tool_status=tool_status,
        status_quo_baseline_bytes=status_quo_baseline_bytes,
    )


def emit_event(sink: Any, event: Mapping[str, Any]) -> None:
    """Emit an event to a sink with a narrow, testable protocol.

    Supported sinks:
    - object with ``emit(event)``;
    - callable taking one event;
    - mutable sequence, which receives an appended dict copy.

    A sink error is intentionally allowed to propagate. The hook catches it and
    fails open to the original tool result, matching the PRD's "Blackbox emit
    failure must not produce an unverified marker" invariant.
    """

    if sink is None:
        return
    emit = getattr(sink, "emit", None)
    if callable(emit):
        emit(event)
        return
    if isinstance(sink, MutableSequence):
        sink.append(dict(event))
        return
    if isinstance(sink, Callable):
        sink(event)
        return
    raise TypeError("native slimmer telemetry sink must be callable, appendable, or expose emit(event)")
