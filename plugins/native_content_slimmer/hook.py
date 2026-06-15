"""Hook implementation for the native_content_slimmer plugin."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any, cast

from plugins.blackbox.native_slimmer_schema import (
    RAW_SOURCE_POST_RTK as _RAW_SOURCE_POST_RTK,
    RAW_SOURCE_PRE_TRUNCATION_TERMINAL as _RAW_SOURCE_PRE_TRUNCATION_TERMINAL,
    RAW_SOURCE_TOOL_CONTRACT_BOUNDED as _RAW_SOURCE_TOOL_CONTRACT_BOUNDED,
    RAW_SOURCE_TOOL_RESULT_RETURNED as _RAW_SOURCE_TOOL_RESULT_RETURNED,
    VALID_RAW_SOURCES,
)

from .classifier import Classification, classify_tool_result
from .config import NativeContentSlimmerConfig
from .marker import MarkerLedger, build_authenticated_marker, verify_marker_auth
from .store import ArtifactStore, raw_byte_len, sha256_text
from .telemetry import NativeSlimmerTelemetryBuffer, build_replacement_event, emit_event

logger = logging.getLogger(__name__)

RAW_SOURCE_TERMINAL_PRE_TRUNCATION = _RAW_SOURCE_PRE_TRUNCATION_TERMINAL
RAW_SOURCE_POST_RTK = _RAW_SOURCE_POST_RTK
RAW_SOURCE_TOOL_RESULT_RETURNED = _RAW_SOURCE_TOOL_RESULT_RETURNED
RAW_SOURCE_TOOL_CONTRACT_BOUNDED = _RAW_SOURCE_TOOL_CONTRACT_BOUNDED
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
        telemetry: Any | None = None,
    ) -> None:
        self.config = config or NativeContentSlimmerConfig()
        self.store = store or ArtifactStore()
        self.ledger = ledger or MarkerLedger()
        self.secret = secret if secret is not None else secrets.token_bytes(32)
        self.telemetry = telemetry if telemetry is not None else NativeSlimmerTelemetryBuffer()
        self.telemetry_records = getattr(self.telemetry, "records", [])
        self.shadow_records: list[SlimmerShadowRecord] = []
        self.failures: list[str] = []
        self._blocked_marker_keys: set[tuple[str, str, str]] = set()

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
            raw_source = _raw_source_from_kwargs(
                kwargs,
                default=RAW_SOURCE_TERMINAL_PRE_TRUNCATION,
            )
            return self._process_result(
                tool_name=_TERMINAL_TOOL,
                raw_text=output,
                raw_source=raw_source,
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
            session = _first_nonempty(session_id, task_id, "session")
            call_id = _first_nonempty(tool_call_id, _stable_tool_call_id(name or "tool", result))
            raw_source = (
                RAW_SOURCE_TOOL_CONTRACT_BOUNDED
                if name == _READ_FILE_TOOL
                else _raw_source_from_kwargs(_, default=RAW_SOURCE_TOOL_RESULT_RETURNED)
            )
            return self._process_result(
                tool_name=name,
                raw_text=result,
                raw_source=raw_source,
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

        raw_sha = sha256_text(raw_text)
        marker_key = (session_id, tool_call_id, raw_sha)
        if marker_key in self._blocked_marker_keys:
            return None
        existing = self.ledger.lookup(
            session_id=session_id,
            tool_call_id=tool_call_id,
            raw_sha256=raw_sha,
        )
        if existing is not None:
            marker = self._verified_existing_marker(
                session_id=session_id,
                tool_call_id=tool_call_id,
                raw_sha256=raw_sha,
                artifact_id=existing.artifact_id,
                marker=existing.marker,
            )
            if marker is None:
                self._blocked_marker_keys.add(marker_key)
                return None
            if self.config.mode == "active_lossless":
                return marker
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
        record = self._verify_persisted_artifact(
            record,
            session_id=session_id,
            raw_text=raw_text,
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
        try:
            self._emit_telemetry(
                mode=self.config.mode,
                action="would_replace" if self.config.mode == "shadow" else "replace",
                tool_name=tool_name,
                session_id=session_id,
                tool_call_id=tool_call_id,
                artifact_id=str(record["artifact_id"]),
                raw_sha256=str(record["raw_sha256"]),
                raw_source=raw_source,
                original_bytes=original_bytes,
                emitted_bytes=marker_bytes,
                classification_reason=classification.reason,
                task_id=task_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                tool_status=status,
            )
        except Exception:
            self._blocked_marker_keys.add((session_id, tool_call_id, str(record["raw_sha256"])))
            raise
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

    def _verify_persisted_artifact(
        self,
        record: dict[str, Any],
        *,
        session_id: str,
        raw_text: str,
    ) -> dict[str, Any]:
        """Read the final artifact back before any marker can be emitted."""

        artifact_id = str(record.get("artifact_id") or "")
        if not artifact_id:
            raise RuntimeError("artifact store returned no artifact_id")
        read_record = getattr(self.store, "read_record", None)
        if not callable(read_record):
            raise RuntimeError("artifact store cannot verify persisted artifacts")
        verified_obj = read_record(artifact_id, session_id=session_id)
        if not isinstance(verified_obj, dict):
            raise RuntimeError(f"artifact verification returned non-object for {artifact_id}")
        verified = cast(dict[str, Any], verified_obj)
        if str(verified.get("raw_sha256") or "") != sha256_text(raw_text):
            raise RuntimeError(f"artifact hash verification failed for {artifact_id}")
        if verified.get("raw_text") != raw_text:
            raise RuntimeError(f"artifact raw_text verification failed for {artifact_id}")
        return dict(verified)

    def _verified_existing_marker(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        raw_sha256: str,
        artifact_id: str,
        marker: str,
    ) -> str | None:
        """Return a reusable marker only when artifact + HMAC still verify."""

        try:
            read_record = getattr(self.store, "read_record", None)
            if not callable(read_record):
                return None
            verified_obj = read_record(artifact_id, session_id=session_id)
            if not isinstance(verified_obj, dict):
                return None
            verified_record = cast(dict[str, Any], verified_obj)
            if str(verified_record.get("raw_sha256") or "") != raw_sha256:
                return None
            verification = verify_marker_auth(marker, secret=self.secret, ledger=self.ledger)
            if not verification.ok:
                return None
            if verification.entry is None:
                return None
            if verification.entry.session_id != session_id or verification.entry.tool_call_id != tool_call_id:
                return None
            return marker
        except Exception as exc:
            self._record_failure(exc)
            return None

    def _emit_telemetry(
        self,
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
        task_id: str,
        turn_id: str,
        api_request_id: str,
        tool_status: str,
    ) -> None:
        event = build_replacement_event(
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
            task_id=task_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            tool_status=tool_status,
        )
        emit_event(self.telemetry, event)

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


def _raw_source_from_kwargs(kwargs: dict[str, Any], *, default: str) -> str:
    """Resolve raw_source from trusted hook metadata without guessing from text."""

    explicit = kwargs.get("raw_source")
    if explicit in VALID_RAW_SOURCES:
        return str(explicit)
    metadata = kwargs.get("metadata")
    if isinstance(metadata, dict) and metadata.get("raw_source") in VALID_RAW_SOURCES:
        return str(metadata["raw_source"])

    rtk_status = str(
        kwargs.get("rtk_status")
        or (metadata.get("rtk_status") if isinstance(metadata, dict) else "")
        or ""
    ).strip().lower()
    rtk_applied = bool(
        kwargs.get("rtk_applied")
        or (metadata.get("rtk_applied") if isinstance(metadata, dict) else False)
    )
    if rtk_applied or rtk_status in {"applied", "compressed", "post-rtk", "rewritten"}:
        return RAW_SOURCE_POST_RTK
    return default


def _stable_tool_call_id(tool_name: str, raw_text: str, hint: str | None = None) -> str:
    seed = "\n".join([tool_name or "tool", hint or "", sha256_text(raw_text)])
    return f"{tool_name or 'tool'}-{sha256_text(seed)[:12]}"
