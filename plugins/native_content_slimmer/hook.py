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

from .classifier import Classification, classify_tool_result, deterministic_preview
from .config import NativeContentSlimmerConfig
from .gc import collect_garbage
from .health import check_artifact_store_health
from .marker import MarkerLedger, build_authenticated_marker, verify_marker_auth
from .store import ArtifactStore, raw_byte_len, safe_component, sha256_text
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
                build_authenticated_marker(
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
            marker = self._verified_existing_marker(
                session_id=session_id,
                tool_call_id=tool_call_id,
                raw_sha256=raw_sha,
                artifact_id=existing.artifact_id,
                marker=existing.marker,
            )
            if marker is not None:
                if self.config.mode == "active_lossless":
                    return marker
                return None
            self.ledger.discard(session_id=session_id, tool_call_id=tool_call_id, raw_sha256=raw_sha)
            self._blocked_marker_keys[marker_key] = 1
            self._record_skip("marker_reuse_verification_failed")
            return None

        classification = classify_tool_result(
            tool_name=tool_name,
            result=raw_text,
            status=status,
            min_bytes=self.config.min_bytes,
            preview_bytes=self.config.preview_bytes,
            allow_tools=self.config.allow_tools,
            deny_tools=self.config.deny_tools,
            deny_on_status=self.config.deny_on_status,
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
    ) -> str | None:
        skip_reason = self._active_artifact_store_skip_reason(session_id=session_id)
        if skip_reason is not None:
            self._record_skip(skip_reason)
            return None

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
            preview_strategy=_PREVIEW_STRATEGY,
            preview_bytes=preview_bytes,
            omitted_bytes=omitted_bytes,
            lossy=False,
            classification_reason=classification.reason,
            redaction_applied=False,
            metadata=artifact_metadata,
            marker_preview=preview,
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
        self._maybe_gc_after_write(active_session_id=session_id)
        return marker

    def _active_artifact_store_skip_reason(self, *, session_id: str) -> str | None:
        if self.config.mode != "active_lossless":
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
