"""Hook implementation for the native_content_slimmer plugin."""

from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from plugins.blackbox.native_slimmer_schema import (
    RAW_SOURCE_POST_RTK as _RAW_SOURCE_POST_RTK,
    RAW_SOURCE_PRE_TRUNCATION_TERMINAL as _RAW_SOURCE_PRE_TRUNCATION_TERMINAL,
    RAW_SOURCE_TOOL_CONTRACT_BOUNDED as _RAW_SOURCE_TOOL_CONTRACT_BOUNDED,
    RAW_SOURCE_TOOL_RESULT_RETURNED as _RAW_SOURCE_TOOL_RESULT_RETURNED,
    VALID_RAW_SOURCES,
)

from .classifier import COMPRESS_OFFLOAD, Classification, classify_tool_result, deterministic_preview
from .config import (
    ACTIVE_COMPRESSION_MODES,
    COMPRESSION_MODE_ACTIVE,
    COMPRESSION_MODE_CANARY,
    COMPRESSION_MODE_OFF,
    COMPRESSION_MODE_SHADOW,
    NativeContentSlimmerConfig,
)
from .breaker import ExpansionRateCircuitBreaker
from .gc import collect_garbage
from .health import check_artifact_store_health
from .marker import (
    MarkerLedger,
    build_authenticated_marker,
    make_marker_signature,
    parse_marker,
    verify_marker_auth,
)
from .store import ArtifactStore, raw_byte_len, safe_component, sha256_text
from .strategies import registry as strategy_registry
from .strategies.base import CompressedView, run_with_timeout_guard
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
        breaker: ExpansionRateCircuitBreaker | None = None,
    ) -> None:
        self.config = config or NativeContentSlimmerConfig()
        self.store = store or ArtifactStore()
        self.ledger = ledger or MarkerLedger()
        if secret is not None:
            self.secret = secret
        elif self.config.enabled and hasattr(self.store, "root"):
            self.secret = _load_or_create_signing_key(self.store.root)
        else:
            # Disabled module-level hooks and test doubles without a durable
            # artifact root should stay inert/fail-open without touching profile
            # storage at import/construction time.
            self.secret = secrets.token_bytes(32)
        self.telemetry = telemetry if telemetry is not None else NativeSlimmerTelemetryBuffer()
        self.breaker = breaker or ExpansionRateCircuitBreaker(
            trip_threshold=float(self.config.compression_breaker_ceiling or 0.0)
        )
        self.telemetry_records = getattr(self.telemetry, "records", [])
        self.shadow_records: list[SlimmerShadowRecord] = []
        self.failures: list[str] = []
        self.skip_reasons: list[str] = []
        self.gc_records: list[dict[str, Any]] = []
        self._write_count = 0
        self._blocked_marker_keys: dict[tuple[str, str, str], int] = {}
        self._gc_threads: list[threading.Thread] = []
        if ledger is None and self.config.enabled:
            self._rehydrate_ledger_from_artifacts()
        if self.config.enabled and self.config.artifact_gc_on_start:
            self._run_gc(active_session_id=None)

    def _rehydrate_ledger_from_artifacts(self) -> None:
        """Rebuild marker auth entries from durable artifact records."""

        try:
            paths = list(self.store.iter_artifact_paths())
        except Exception as exc:
            self._record_failure(exc)
            return
        for path in paths:
            try:
                record = self.store.read_record(path.stem, session_id=path.parent.name)
                raw_text = str(record.get("raw_text") or "")
                raw_sha256 = str(record.get("raw_sha256") or "")
                artifact_id = str(record.get("artifact_id") or path.stem)
                session_id = str(record.get("session_id") or path.parent.name)
                tool_call_id = str(record.get("tool_call_id") or "")
                if not raw_text or not raw_sha256 or not artifact_id or not session_id or not tool_call_id:
                    continue
                original_bytes = int(record.get("raw_bytes") or raw_byte_len(raw_text))
                shown_bytes = int(record.get("preview_bytes") or 0)
                omitted_bytes = int(record.get("omitted_bytes") or max(0, original_bytes - shown_bytes))
                preview = str(record.get("marker_preview") or "")
                if not preview and shown_bytes > 0:
                    preview = deterministic_preview(raw_text, preview_bytes=shown_bytes)
                marker = build_authenticated_marker(
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    artifact_id=artifact_id,
                    tool_name=str(record.get("tool_name") or ""),
                    raw_sha256=raw_sha256,
                    original_bytes=original_bytes,
                    shown_bytes=shown_bytes,
                    omitted_bytes=omitted_bytes,
                    preview=preview,
                    secret=self.secret,
                    ledger=self.ledger,
                )
                strategy_name = str(record.get("strategy") or "")
                if strategy_name:
                    marker = _annotate_compressed_marker(
                        marker,
                        strategy_name=strategy_name,
                        view_bytes=int(record.get("view_bytes") or shown_bytes),
                        lossy_view=bool(record.get("lossy_view", True)),
                        recoverable=bool(record.get("recoverable", True)),
                    )
                    self._record_marker_variant(
                        session_id=session_id,
                        tool_call_id=tool_call_id,
                        raw_sha256=raw_sha256,
                        artifact_id=artifact_id,
                        original_bytes=original_bytes,
                        marker=marker,
                    )
            except Exception as exc:
                self._record_failure(exc)

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
            session_id = _optional_nonempty(kwargs.get("session_id"))
            if session_id is None:
                self._record_skip("no_session_scope")
                return None
            tool_call_id = _optional_nonempty(kwargs.get("tool_call_id"))
            if tool_call_id is None:
                self._record_skip("no_tool_call_id")
                return None
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
            if name == _READ_FILE_TOOL:
                self._record_skip("read_file_contract_bounded")
                return None
            session = _optional_nonempty(session_id)
            if session is None:
                self._record_skip("no_session_scope")
                return None
            call_id = _optional_nonempty(tool_call_id)
            if call_id is None:
                self._record_skip("no_tool_call_id")
                return None
            raw_source = _raw_source_from_kwargs(_, default=RAW_SOURCE_TOOL_RESULT_RETURNED)
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
        if not str(session_id or "").strip():
            self._record_skip("no_session_scope")
            return None
        if not str(tool_call_id or "").strip():
            self._record_skip("no_tool_call_id")
            return None
        if tool_name == _READ_FILE_TOOL:
            self._record_skip("read_file_contract_bounded")
            return None

        raw_sha = sha256_text(raw_text)
        marker_key = (session_id, tool_call_id, raw_sha)
        if self._consume_blocked_marker_key(marker_key):
            return None
        existing = self.ledger.lookup(
            session_id=session_id,
            tool_call_id=tool_call_id,
            raw_sha256=raw_sha,
        )
        if existing is not None:
            recompute_after_compressed_reuse = False
            marker = self._verified_existing_marker(
                session_id=session_id,
                tool_call_id=tool_call_id,
                raw_sha256=raw_sha,
                artifact_id=existing.artifact_id,
                marker=existing.marker,
            )
            if marker is not None:
                parsed_existing = parse_marker(marker)
                existing_is_compressed = bool(parsed_existing and parsed_existing.fields.get("strategy"))
                if existing_is_compressed:
                    # A compressed marker must not bypass current config/breaker/canary
                    # gates. Recompute the path for this turn instead of blindly
                    # reusing a ledger entry created under older controls.
                    self.ledger.discard(session_id=session_id, tool_call_id=tool_call_id, raw_sha256=raw_sha)
                    recompute_after_compressed_reuse = True
                elif self.config.mode == "active_lossless":
                    return marker
                else:
                    return None
            if not recompute_after_compressed_reuse:
                self.ledger.discard(session_id=session_id, tool_call_id=tool_call_id, raw_sha256=raw_sha)
                self._blocked_marker_keys[marker_key] = 1
                self._record_skip("marker_reuse_verification_failed")
                return None

        compression_requested = self._compression_requested_for_turn(metadata=metadata)
        classification = classify_tool_result(
            tool_name=tool_name,
            result=raw_text,
            status=status,
            min_bytes=self.config.min_bytes,
            preview_bytes=self.config.preview_bytes,
            allow_tools=self.config.allow_tools,
            deny_tools=self.config.deny_tools,
            deny_on_status=self.config.deny_on_status,
            command=str(metadata.get("command") or ""),
            compression_enabled=compression_requested,
            compression_strategies=dict(self.config.compression_strategies or {}),
        )
        classification = self._apply_runtime_compression_gates(
            classification=classification,
            tool_name=tool_name,
            raw_text=raw_text,
            marker_key=marker_key,
        )
        if not classification.eligible:
            self._record_skip(classification.reason)
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
        if marker is None:
            return None

        parsed_marker = parse_marker(marker)
        marker_is_compressed = bool(parsed_marker and parsed_marker.fields.get("strategy"))
        if marker_is_compressed:
            if self.config.compression_mode in ACTIVE_COMPRESSION_MODES:
                return marker
            return None
        if self.config.mode == "shadow":
            return None
        if self.config.mode == "active_lossless":
            return marker
        return None

    def _compression_requested_for_turn(self, *, metadata: dict[str, Any]) -> bool:
        mode = self.config.compression_mode
        if mode == COMPRESSION_MODE_OFF:
            return False
        # Shadow compression is a no-live-risk screening path. Generate compressed
        # shadow rows only when the legacy slimmer path is also shadow; if lossless
        # offload is active, keep returning the shipped lossless marker.
        if mode == COMPRESSION_MODE_SHADOW:
            return self.config.mode == "shadow"
        return mode in ACTIVE_COMPRESSION_MODES

    def _apply_runtime_compression_gates(
        self,
        *,
        classification: Classification,
        tool_name: str,
        raw_text: str,
        marker_key: tuple[str, str, str],
    ) -> Classification:
        if classification.outcome != COMPRESS_OFFLOAD:
            return classification
        lane = self._compression_lane_id(tool_name=tool_name, classification=classification)
        mode = self.config.compression_mode
        if mode == COMPRESSION_MODE_SHADOW:
            return classification
        if mode == COMPRESSION_MODE_CANARY and not self._canary_allows(marker_key):
            self._record_skip("compression_canary_not_selected")
            return self._lossless_classification_from(classification, raw_text, "compression_canary_not_selected")
        if mode in ACTIVE_COMPRESSION_MODES:
            state = self.breaker.evaluate(lane)
            if not state.allow_compression:
                self._record_skip(f"compression_breaker_{state.reason}")
                return self._lossless_classification_from(
                    classification,
                    raw_text,
                    f"compression_breaker_{state.reason}",
                )
        return classification

    def _lossless_classification_from(
        self,
        classification: Classification,
        raw_text: str,
        reason: str,
    ) -> Classification:
        return Classification(
            eligible=True,
            reason=reason,
            raw_bytes=classification.raw_bytes,
            content_class=classification.content_class,
            preview=deterministic_preview(raw_text, preview_bytes=self.config.preview_bytes),
            secret_match=classification.secret_match,
            outcome="lossless_offload",
            recommended_strategy=None,
        )

    def _compression_lane_id(self, *, tool_name: str, classification: Classification) -> tuple[str, str, str]:
        return (
            str(tool_name or ""),
            str(classification.content_class or "unknown"),
            str(classification.recommended_strategy or "unknown"),
        )

    def _lane_params(self, *, tool_name: str, classification: Classification) -> dict[str, Any]:
        params: dict[str, Any] = {}
        configured = self.config.compression_lane_params or {}
        keys = (
            f"{tool_name}:{classification.content_class}",
            f"{tool_name}:{classification.content_class}:{classification.recommended_strategy}",
            str(classification.recommended_strategy or ""),
        )
        for key in keys:
            value = configured.get(key)
            if isinstance(value, dict):
                params.update(value)
        return params

    def _canary_allows(self, marker_key: tuple[str, str, str]) -> bool:
        percent = max(0.0, min(100.0, float(self.config.compression_canary_percent or 0.0)))
        if percent <= 0.0:
            return False
        if percent >= 100.0:
            return True
        # Stable per-result bucketing; no randomness in tests or replay.
        digest = sha256_text("|".join(marker_key))
        bucket = int(digest[:8], 16) % 10_000
        return bucket < int(percent * 100)

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
    ) -> str | None:
        skip_reason = self._active_artifact_store_skip_reason(session_id=session_id)
        if skip_reason is not None:
            self._record_skip(skip_reason)
            return None

        compressed_view = self._compressed_view_for(
            tool_name=tool_name,
            raw_text=raw_text,
            classification=classification,
        )
        if compressed_view is None:
            preview = classification.preview or deterministic_preview(raw_text, preview_bytes=self.config.preview_bytes)
            preview_strategy = _PREVIEW_STRATEGY
            lossy = False
            strategy_name = ""
            recoverable = True
        else:
            preview = str(compressed_view.view_text or "")
            preview_strategy = compressed_view.strategy_name or str(classification.recommended_strategy or "")
            lossy = bool(compressed_view.lossy_view)
            strategy_name = preview_strategy
            recoverable = bool(compressed_view.recoverable)
        preview_bytes = raw_byte_len(preview)
        original_bytes = raw_byte_len(raw_text)
        omitted_bytes = max(0, original_bytes - preview_bytes)
        telemetry_action = "replace" if (
            strategy_name and self.config.compression_mode in ACTIVE_COMPRESSION_MODES
        ) else ("would_replace" if self.config.mode == "shadow" else "replace")
        telemetry_mode = "active_lossless" if telemetry_action == "replace" else "shadow"
        artifact_metadata: dict[str, Any] = {
            "mode": telemetry_mode,
            "would_replace": telemetry_action == "would_replace",
        }
        artifact_extra: dict[str, Any] = {}
        if strategy_name:
            artifact_metadata.update(
                {
                    "strategy": strategy_name,
                    "view_bytes": preview_bytes,
                    "lossy_view": lossy,
                    "recoverable": recoverable,
                }
            )
            artifact_extra.update(
                {
                    "strategy": strategy_name,
                    "view_bytes": preview_bytes,
                    "lossy_view": lossy,
                    "recoverable": recoverable,
                }
            )
        if duration_ms is not None:
            artifact_metadata["duration_ms"] = duration_ms
        artifact_metadata.update({key: value for key, value in metadata.items() if value not in (None, "")})
        base_artifact_id = _call_identity_artifact_id(session_id=session_id, tool_call_id=tool_call_id)

        record = self.store.write_artifact(
            session_id=session_id,
            tool_call_id=tool_call_id,
            raw_text=raw_text,
            artifact_id=base_artifact_id,
            task_id=task_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            tool_name=tool_name,
            tool_status=status,
            raw_source=raw_source,
            preview_strategy=preview_strategy,
            preview_bytes=preview_bytes,
            omitted_bytes=omitted_bytes,
            lossy=lossy,
            classification_reason=classification.reason,
            redaction_applied=False,
            metadata=artifact_metadata,
            marker_preview=preview,
            **artifact_extra,
        )
        record = self._verify_persisted_artifact(
            record,
            session_id=session_id,
            raw_text=raw_text,
        )
        if str(record.get("artifact_id") or "") != base_artifact_id:
            logger.warning(
                "native_content_slimmer tool_call_id reused with different raw_sha256; "
                "stored suffixed artifact_id=%s base_artifact_id=%s",
                record.get("artifact_id"),
                base_artifact_id,
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
        if strategy_name:
            marker = _annotate_compressed_marker(
                marker,
                strategy_name=strategy_name,
                view_bytes=preview_bytes,
                lossy_view=lossy,
                recoverable=recoverable,
            )
            self._record_marker_variant(
                session_id=session_id,
                tool_call_id=tool_call_id,
                raw_sha256=str(record["raw_sha256"]),
                artifact_id=str(record["artifact_id"]),
                original_bytes=original_bytes,
                marker=marker,
            )
        marker_bytes = raw_byte_len(marker)
        try:
            self._emit_telemetry(
                mode=telemetry_mode,
                action=telemetry_action,
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
                strategy=strategy_name or None,
                view_bytes=preview_bytes if strategy_name else None,
                lossy_view=lossy if strategy_name else None,
                expansions_triggered=0 if strategy_name else None,
                status_quo_baseline_bytes=_status_quo_baseline_bytes(
                    raw_source=raw_source,
                    original_bytes=original_bytes,
                ),
            )
        except Exception:
            raw_sha256 = str(record["raw_sha256"])
            self.ledger.discard(session_id=session_id, tool_call_id=tool_call_id, raw_sha256=raw_sha256)
            self._delete_untelemetried_artifact(
                artifact_id=str(record["artifact_id"]),
                session_id=session_id,
            )
            self._record_skip("telemetry_emit_failed")
            raise
        if telemetry_action == "would_replace":
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
        self._maybe_gc_after_write(active_session_id=session_id)
        return marker

    def _active_artifact_store_skip_reason(self, *, session_id: str) -> str | None:
        active_marker_path = self.config.mode == "active_lossless" or self.config.compression_mode in ACTIVE_COMPRESSION_MODES
        if not active_marker_path:
            return None
        root = getattr(self.store, "root", None)
        if root is None:
            return None
        max_bytes = self.config.artifact_max_bytes_per_profile
        try:
            health = check_artifact_store_health(
                root,
                max_bytes=max_bytes,
                active_session_id=session_id,
            )
            if not bool(health.get("writable")):
                return "artifact_store_unwritable"
            if not bool(health.get("over_cap")):
                return None
            if int(health.get("ended_session_bytes") or 0) > 0:
                self._run_gc(active_session_id=session_id)
                health = check_artifact_store_health(
                    root,
                    max_bytes=max_bytes,
                    active_session_id=session_id,
                )
                if not bool(health.get("writable")):
                    return "artifact_store_unwritable"
            if bool(health.get("over_cap")):
                return "artifact_store_over_cap"
            return None
        except Exception as exc:
            self._record_failure(exc)
            return "artifact_store_unwritable"

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

    def _compressed_view_for(
        self,
        *,
        tool_name: str,
        raw_text: str,
        classification: Classification,
    ) -> CompressedView | None:
        if classification.outcome != COMPRESS_OFFLOAD:
            return None
        selection = strategy_registry.select_compressor(
            tool_name=tool_name,
            content_class=classification.content_class,
        )
        if selection is None:
            return None
        params = dict(selection.params or {})
        params.update(self._lane_params(tool_name=tool_name, classification=classification))
        view = run_with_timeout_guard(selection.compressor, raw_text, params=params)
        if view is None:
            return None
        view_text = str(view.view_text or "")
        strategy_name = str(view.strategy_name or selection.strategy_name or classification.recommended_strategy or "")
        return CompressedView(
            view_text=view_text,
            view_bytes=raw_byte_len(view_text),
            lossy_view=bool(view.lossy_view),
            recoverable=bool(view.recoverable),
            strategy_name=strategy_name,
        )

    def _record_marker_variant(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        raw_sha256: str,
        artifact_id: str,
        original_bytes: int,
        marker: str,
    ) -> None:
        signature = make_marker_signature(
            session_id=session_id,
            tool_call_id=tool_call_id,
            raw_sha256=raw_sha256,
            artifact_id=artifact_id,
            original_bytes=original_bytes,
            secret=self.secret,
        )
        self.ledger.record(
            session_id=session_id,
            tool_call_id=tool_call_id,
            raw_sha256=raw_sha256,
            artifact_id=artifact_id,
            original_bytes=original_bytes,
            signature=signature,
            marker=marker,
        )

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
        strategy: str | None = None,
        view_bytes: int | None = None,
        lossy_view: bool | None = None,
        expansions_triggered: int | None = None,
        status_quo_baseline_bytes: int | None = None,
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
            status_quo_baseline_bytes=status_quo_baseline_bytes,
        )
        if strategy is not None:
            event.update(
                {
                    "strategy": strategy,
                    "view_bytes": int(view_bytes or 0),
                    "lossy_view": bool(lossy_view),
                    "lossy": bool(lossy_view),
                    "expansions_triggered": int(expansions_triggered or 0),
                }
            )
        emit_event(self.telemetry, event)

    def _record_skip(self, reason: str) -> None:
        self.skip_reasons.append(str(reason or "unknown"))

    def _consume_blocked_marker_key(self, marker_key: tuple[str, str, str]) -> bool:
        remaining = self._blocked_marker_keys.get(marker_key)
        if remaining is None:
            return False
        if remaining <= 1:
            self._blocked_marker_keys.pop(marker_key, None)
        else:
            self._blocked_marker_keys[marker_key] = remaining - 1
        self._record_skip("marker_reuse_suppressed")
        logger.info(
            "native_content_slimmer suppressed marker reuse for transiently blocked key session=%s tool_call=%s",
            marker_key[0],
            marker_key[1],
        )
        return True

    def _delete_untelemetried_artifact(self, *, artifact_id: str, session_id: str) -> None:
        """Best-effort rollback for artifacts whose telemetry emit failed."""

        try:
            path_for = getattr(self.store, "path_for", None)
            if callable(path_for):
                path = path_for(artifact_id, session_id=session_id)
            else:
                find_path = getattr(self.store, "find_artifact_path", None)
                if not callable(find_path):
                    return
                path = find_path(artifact_id, session_id=session_id)
            artifact_path = path if isinstance(path, Path) else Path(str(path))
            artifact_path.unlink(missing_ok=True)
            try:
                _fsync_directory(artifact_path.parent)
            except Exception:
                pass
        except FileNotFoundError:
            return
        except Exception as exc:
            self._record_failure(exc)

    def _maybe_gc_after_write(self, *, active_session_id: str) -> None:
        every = int(self.config.artifact_gc_after_write_every or 0)
        if every <= 0:
            return
        self._write_count += 1
        if self._write_count % every == 0:
            self._run_gc_async(active_session_id=active_session_id)
            self._maybe_prune_savings()

    def _maybe_prune_savings(self) -> None:
        """Best-effort TTL prune of the native_slimmer_savings table (PRD #1.5 D-8).

        Rides the same write cadence as the artifact GC. Fully fail-open — a prune
        error must never break a write. No-op unless the savings retention is set.
        """

        days = int(getattr(self.config, "savings_retention_days", 0) or 0)
        if days <= 0:
            return
        try:
            import time as _time

            from plugins.blackbox import native_slimmer_store as nss

            cutoff = _time.time() - days * 86400
            nss.prune_older_than(cutoff)
        except Exception as exc:  # pragma: no cover - defensive, fail-open
            logger.debug("native_content_slimmer savings prune failed open: %s", exc)

    def _run_gc_async(self, *, active_session_id: str | None) -> None:
        # Single-flight: prune finished threads, then skip spawning if a GC pass
        # is already in flight. Prevents daemon-thread fan-out when GC runs slower
        # than writes arrive on a large/slow-disk profile (RC#3).
        self._gc_threads = [thread for thread in self._gc_threads if thread.is_alive()]
        if self._gc_threads:
            return
        thread = threading.Thread(
            target=self._run_gc,
            kwargs={"active_session_id": active_session_id},
            name="native-content-slimmer-gc",
            daemon=True,
        )
        self._gc_threads.append(thread)
        thread.start()

    def _run_gc(self, *, active_session_id: str | None) -> None:
        root = getattr(self.store, "root", None)
        try:
            result = collect_garbage(
                root=root,
                ttl_days=self.config.artifact_ttl_days,
                max_bytes=self.config.artifact_max_bytes_per_profile,
                active_session_id=active_session_id,
            )
            if isinstance(result, dict):
                self.gc_records.append(result)
        except Exception as exc:
            self._record_failure(exc)

    def on_session_end(self, *, session_id: str | None = None, **_: Any) -> None:
        if self.config.artifact_gc_on_session_end:
            self._run_gc(active_session_id=_optional_nonempty(session_id))

    def on_session_reset(self, *, session_id: str | None = None, **_: Any) -> None:
        if self.config.artifact_gc_on_session_reset:
            self._run_gc(active_session_id=_optional_nonempty(session_id))

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


def _optional_nonempty(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _call_identity_artifact_id(*, session_id: str, tool_call_id: str) -> str:
    return (
        f"art_{safe_component(session_id, fallback='session')}_"
        f"{safe_component(tool_call_id, fallback='tool_call')}"
    )


def _status_quo_baseline_bytes(*, raw_source: str, original_bytes: int) -> int | None:
    if raw_source != RAW_SOURCE_TERMINAL_PRE_TRUNCATION:
        return None
    try:
        from tools.tool_output_limits import get_max_bytes

        cap = int(get_max_bytes())
    except Exception:
        return None
    if cap <= 0:
        return None
    return min(max(0, int(original_bytes or 0)), cap)


def _annotate_compressed_marker(
    marker: str,
    *,
    strategy_name: str,
    view_bytes: int,
    lossy_view: bool,
    recoverable: bool,
) -> str:
    """Add compression metadata to an already-authenticated marker header."""

    if not strategy_name:
        return marker
    lines = marker.splitlines()
    if not lines or not lines[0].endswith("]"):
        return marker
    fields = [
        _quoted_marker_field("strategy", strategy_name),
        f"view_bytes={max(0, int(view_bytes or 0))}",
        f"lossy_view={_bool_marker_value(lossy_view)}",
        f"recoverable={_bool_marker_value(recoverable)}",
    ]
    lines[0] = f"{lines[0][:-1]} {' '.join(fields)}]"
    if len(lines) > 1:
        lines[1] = (
            "This is a semantically compressed preview, not a literal excerpt or the full tool result. "
            "Call expand_artifact(id) for exact original bytes."
        )
    return "\n".join(lines)


def _quoted_marker_field(key: str, value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def _bool_marker_value(value: bool) -> str:
    return "true" if bool(value) else "false"


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


_SIGNING_KEY_NAME = ".signing_key"


def _load_or_create_signing_key(root: str | Path) -> bytes:
    """Load the durable profile-scoped marker signing key, creating it once."""

    root_path = Path(root)
    key_path = root_path / _SIGNING_KEY_NAME
    try:
        return _read_signing_key(key_path)
    except FileNotFoundError:
        pass

    root_path.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    tmp_path = key_path.with_name(f"{key_path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}")
    fd: int | None = None
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            fd = None
            handle.write(key.hex() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(tmp_path), str(key_path))
        except FileExistsError:
            tmp_path.unlink()
            return _read_signing_key(key_path)
        tmp_path.unlink()
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        _fsync_directory(root_path)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return _read_signing_key(key_path)


def _read_signing_key(path: Path) -> bytes:
    text = path.read_text(encoding="ascii").strip()
    key = bytes.fromhex(text)
    if len(key) != 32:
        raise RuntimeError(f"invalid native_content_slimmer signing key at {path}")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
