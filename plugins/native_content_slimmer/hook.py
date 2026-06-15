"""Hook implementation for the native_content_slimmer plugin."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from .classifier import Classification, classify_tool_result
from .config import NativeContentSlimmerConfig
from .marker import MarkerLedger, build_authenticated_marker
from .store import ArtifactStore, raw_byte_len, sha256_text

logger = logging.getLogger(__name__)

RAW_SOURCE_TERMINAL_PRE_TRUNCATION = "pre-truncation-terminal"
RAW_SOURCE_TOOL_RESULT_RETURNED = "tool-result-returned"
_READ_FILE_TOOL = "read_file"
_TERMINAL_TOOL = "terminal"
_PREVIEW_STRATEGY = "head-tail-lines"


@dataclass(frozen=True)
class SlimmerShadowRecord:
    """A would-replace record emitted by shadow mode."""

    mode: str
    action: str
    tool_name: str
    session_id: str
    tool_call_id: str
    artifact_id: str
    raw_source: str
    original_bytes: int
    emitted_bytes: int
    would_save_bytes: int
    classification_reason: str


class NativeContentSlimmerHooks:
    """Fail-open transform hook handlers for native content slimming."""

    def __init__(
        self,
        config: NativeContentSlimmerConfig | None = None,
        *,
        store: ArtifactStore | None = None,
        ledger: MarkerLedger | None = None,
        secret: bytes | str | None = None,
    ) -> None:
        self.config = config or NativeContentSlimmerConfig()
        self.store = store or ArtifactStore()
        self.ledger = ledger or MarkerLedger()
        self.secret = secret if secret is not None else secrets.token_bytes(32)
        self.shadow_records: list[SlimmerShadowRecord] = []
        self.failures: list[str] = []

    def transform_terminal_output(
        self,
        *,
        command: str | None = None,
        output: str | None = None,
        returncode: int | None = None,
        task_id: str | None = None,
        env_type: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Handle terminal stdout/stderr before terminal_tool.py truncates it."""

        try:
            if output is None:
                return None
            status = "success" if int(returncode or 0) == 0 else "error"
            session_id = _first_nonempty(kwargs.get("session_id"), task_id, "terminal")
            tool_call_id = _first_nonempty(
                kwargs.get("tool_call_id"),
                _stable_tool_call_id(_TERMINAL_TOOL, output, command),
            )
            return self._process_result(
                tool_name=_TERMINAL_TOOL,
                raw_text=output,
                raw_source=RAW_SOURCE_TERMINAL_PRE_TRUNCATION,
                status=status,
                session_id=session_id,
                tool_call_id=tool_call_id,
                task_id=str(task_id or ""),
                turn_id=str(kwargs.get("turn_id") or ""),
                api_request_id=str(kwargs.get("api_request_id") or ""),
                duration_ms=kwargs.get("duration_ms"),
                metadata={"command": command or "", "env_type": env_type or ""},
            )
        except Exception as exc:  # pragma: no cover - covered through public tests
            self._record_failure(exc)
            return None

    def transform_tool_result(
        self,
        *,
        tool_name: str | None = None,
        result: str | None = None,
        status: str | None = "success",
        session_id: str | None = None,
        tool_call_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
        api_request_id: str | None = None,
        duration_ms: float | int | None = None,
        **_: Any,
    ) -> str | None:
        """Handle non-terminal tool results after tool return, before context append."""

        try:
            if result is None:
                return None
            name = str(tool_name or "")
            if name == _TERMINAL_TOOL:
                # Terminal raw belongs to transform_terminal_output; this seam sees
                # post-truncation terminal_tool output and must not double-count it.
                return None
            if name == _READ_FILE_TOOL:
                # read_file is pagination-bounded by contract; no artifact can recover
                # bytes outside the requested page, so do not emit an artifact marker.
                return None
            session = _first_nonempty(session_id, task_id, "session")
            call_id = _first_nonempty(tool_call_id, _stable_tool_call_id(name or "tool", result))
            return self._process_result(
                tool_name=name,
                raw_text=result,
                raw_source=RAW_SOURCE_TOOL_RESULT_RETURNED,
                status=status or "success",
                session_id=session,
                tool_call_id=call_id,
                task_id=str(task_id or ""),
                turn_id=str(turn_id or ""),
                api_request_id=str(api_request_id or ""),
                duration_ms=duration_ms,
                metadata={},
            )
        except Exception as exc:  # pragma: no cover - covered through public tests
            self._record_failure(exc)
            return None

    def _process_result(
        self,
        *,
        tool_name: str,
        raw_text: str,
        raw_source: str,
        status: str,
        session_id: str,
        tool_call_id: str,
        task_id: str,
        turn_id: str,
        api_request_id: str,
        duration_ms: float | int | None,
        metadata: dict[str, Any],
    ) -> str | None:
        if not self.config.enabled:
            return None
        if not isinstance(raw_text, str) or raw_text == "":
            return None

        classification = classify_tool_result(
            tool_name=tool_name,
            result=raw_text,
            status=status,
        )
        if not classification.eligible:
            return None

        marker = self._persist_and_build_marker(
            tool_name=tool_name,
            raw_text=raw_text,
            raw_source=raw_source,
            status=status,
            session_id=session_id,
            tool_call_id=tool_call_id,
            task_id=task_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            duration_ms=duration_ms,
            metadata=metadata,
            classification=classification,
        )

        if self.config.mode == "shadow":
            return None
        if self.config.mode == "active_lossless":
            return marker
        return None

    def _persist_and_build_marker(
        self,
        *,
        tool_name: str,
        raw_text: str,
        raw_source: str,
        status: str,
        session_id: str,
        tool_call_id: str,
        task_id: str,
        turn_id: str,
        api_request_id: str,
        duration_ms: float | int | None,
        metadata: dict[str, Any],
        classification: Classification,
    ) -> str:
        preview = classification.preview or ""
        preview_bytes = raw_byte_len(preview)
        original_bytes = raw_byte_len(raw_text)
        omitted_bytes = max(0, original_bytes - preview_bytes)
        artifact_metadata: dict[str, Any] = {
            "mode": self.config.mode,
            "would_replace": self.config.mode == "shadow",
        }
        if duration_ms is not None:
            artifact_metadata["duration_ms"] = duration_ms
        artifact_metadata.update({key: value for key, value in metadata.items() if value not in (None, "")})

        record = self.store.write_artifact(
            session_id=session_id,
            tool_call_id=tool_call_id,
            raw_text=raw_text,
            task_id=task_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            tool_name=tool_name,
            tool_status=status,
            raw_source=raw_source,
            preview_strategy=_PREVIEW_STRATEGY,
            preview_bytes=preview_bytes,
            omitted_bytes=omitted_bytes,
            lossy=False,
            classification_reason=classification.reason,
            redaction_applied=False,
            metadata=artifact_metadata,
        )
        marker = build_authenticated_marker(
            session_id=session_id,
            tool_call_id=tool_call_id,
            artifact_id=str(record["artifact_id"]),
            tool_name=tool_name,
            raw_sha256=str(record["raw_sha256"]),
            original_bytes=original_bytes,
            shown_bytes=preview_bytes,
            omitted_bytes=omitted_bytes,
            preview=preview,
            secret=self.secret,
            ledger=self.ledger,
        )
        marker_bytes = raw_byte_len(marker)
        if self.config.mode == "shadow":
            self.shadow_records.append(
                SlimmerShadowRecord(
                    mode="shadow",
                    action="would_replace",
                    tool_name=tool_name,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    artifact_id=str(record["artifact_id"]),
                    raw_source=raw_source,
                    original_bytes=original_bytes,
                    emitted_bytes=marker_bytes,
                    would_save_bytes=max(0, original_bytes - marker_bytes),
                    classification_reason=classification.reason,
                )
            )
        return marker

    def _record_failure(self, exc: Exception) -> None:
        message = str(exc) or exc.__class__.__name__
        self.failures.append(message)
        logger.debug("native_content_slimmer hook failed open: %s", message)


_DEFAULT_RUNTIME = NativeContentSlimmerHooks()


def transform_terminal_output(**kwargs: Any) -> str | None:
    """Module-level disabled-by-default terminal hook."""

    return _DEFAULT_RUNTIME.transform_terminal_output(**kwargs)


def transform_tool_result(**kwargs: Any) -> str | None:
    """Module-level disabled-by-default generic tool-result hook."""

    return _DEFAULT_RUNTIME.transform_tool_result(**kwargs)


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return "none"


def _stable_tool_call_id(tool_name: str, raw_text: str, hint: str | None = None) -> str:
    seed = "\n".join([tool_name or "tool", hint or "", sha256_text(raw_text)])
    return f"{tool_name or 'tool'}-{sha256_text(seed)[:12]}"
