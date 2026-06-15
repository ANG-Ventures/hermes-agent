"""Agent-facing tools for the native content slimmer plugin."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from plugins.native_content_slimmer.classifier import contains_secret
from plugins.native_content_slimmer.store import (
    ArtifactGoneError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)

TOOLSET_NAME = "native_content_slimmer"
EXPAND_ARTIFACT_NAME = "expand_artifact"
DEFAULT_MAX_BYTES = 200_000

EXPAND_ARTIFACT_SCHEMA: dict[str, Any] = {
    "name": EXPAND_ARTIFACT_NAME,
    "description": (
        "Return exact raw content for a native-content-slimmer artifact owned by "
        "the current session. Cross-session and subagent reads are denied."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Artifact id from a HERMES_ARTIFACT_COMPACTED marker.",
            },
            "max_bytes": {
                "type": "integer",
                "description": (
                    "Maximum UTF-8 bytes to return. Defaults to 200000 to avoid "
                    "recursive context blow-up."
                ),
                "minimum": 0,
            },
            "range": {
                "type": ["object", "null"],
                "description": "Optional UTF-8 byte range, end-exclusive: {start, end}.",
                "properties": {
                    "start": {"type": "integer", "minimum": 0},
                    "end": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}


def register_tools(ctx: Any) -> None:
    """Register native content slimmer tools with a Hermes plugin context."""

    ctx.register_tool(
        name=EXPAND_ARTIFACT_NAME,
        toolset=TOOLSET_NAME,
        schema=EXPAND_ARTIFACT_SCHEMA,
        handler=handle_expand_artifact,
        emoji="📦",
    )


def handle_expand_artifact(args: Mapping[str, Any] | None, **kwargs: Any) -> str:
    """Registry handler for ``expand_artifact``.

    ``session_id`` is deliberately read only from trusted handler kwargs. A
    model-supplied ``session_id`` argument is ignored so a caller cannot spoof
    ownership in the JSON payload.
    """

    result = expand_artifact_tool(args or {}, **kwargs)
    return json.dumps(result, ensure_ascii=False)


def expand_artifact_tool(args: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Expand a stored artifact with same-session auth and sensitive gating."""

    artifact_id = str(args.get("id") or "")
    if not artifact_id:
        return _error(artifact_id, "invalid_request", message="id is required")

    session_id = _trusted_session_id(kwargs)
    if not session_id:
        return _error(artifact_id, "not_authorized")

    max_bytes_result = _parse_non_negative_int(args.get("max_bytes"), default=DEFAULT_MAX_BYTES)
    if isinstance(max_bytes_result, dict):
        return _error(artifact_id, "invalid_request", message=max_bytes_result["message"])
    max_bytes = max_bytes_result

    parsed_range = _parse_range(args.get("range"))
    if isinstance(parsed_range, dict):
        return _error(artifact_id, "invalid_range", message=parsed_range["message"])
    range_start, range_end = parsed_range

    store = kwargs.get("store")
    if store is None:
        store = ArtifactStore()
    if not isinstance(store, ArtifactStore):
        # Tests may pass an ArtifactStore subclass, but arbitrary objects make
        # the auth check too easy to bypass accidentally.
        return _error(artifact_id, "store_error", message="invalid artifact store")

    auth_error = _authorize_same_session(store, artifact_id, session_id)
    if auth_error is not None:
        return auth_error

    try:
        record = store.read_record(artifact_id, session_id=session_id)
    except ArtifactGoneError as exc:
        return _gone_error(artifact_id, exc)
    except ArtifactNotFoundError:
        return _error(artifact_id, "not_found")
    except ArtifactIntegrityError as exc:
        return _error(artifact_id, "hash_mismatch", message=str(exc))
    except Exception as exc:
        return _error(artifact_id, "store_error", message=str(exc))

    raw_text = str(record.get("raw_text", ""))
    if contains_secret(raw_text) is not None:
        return _error(artifact_id, "sensitive_content_blocked")

    expanded = store.expand_artifact(
        artifact_id,
        session_id=session_id,
        max_bytes=max_bytes,
        range_start=range_start,
        range_end=range_end,
    )
    if expanded.get("ok") is False and expanded.get("error") == "store_error":
        # Keep the tool contract small and loud. Store-level unexpected errors
        # remain errors; they never get converted into partial content.
        return expanded
    return expanded


def _trusted_session_id(kwargs: Mapping[str, Any]) -> str:
    value = kwargs.get("session_id") or kwargs.get("current_session_id")
    return str(value or "").strip()


def _authorize_same_session(
    store: ArtifactStore,
    artifact_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Return an auth/not-found/gone error, or None when same-session access is allowed."""

    try:
        store.find_artifact_path(artifact_id, session_id=session_id)
    except ArtifactGoneError as exc:
        if str(exc.tombstone.get("session_id") or "") == session_id:
            return _gone_error(artifact_id, exc)
        return _error(artifact_id, "not_found")
    except ArtifactNotFoundError:
        return _error(artifact_id, "not_found")
    except Exception as exc:
        return _error(artifact_id, "store_error", message=str(exc))

    try:
        record = store.read_record(artifact_id, session_id=session_id)
    except ArtifactIntegrityError:
        return None
    except ArtifactGoneError as exc:
        if str(exc.tombstone.get("session_id") or "") == session_id:
            return _gone_error(artifact_id, exc)
        return _error(artifact_id, "not_found")
    except ArtifactNotFoundError:
        return _error(artifact_id, "not_found")
    except Exception as exc:
        return _error(artifact_id, "store_error", message=str(exc))

    if str(record.get("session_id") or "") != session_id:
        return _error(artifact_id, "not_found")
    return None


def _parse_non_negative_int(value: Any, *, default: int) -> int | dict[str, str]:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return {"message": "max_bytes must be a non-negative integer"}
    if parsed < 0:
        return {"message": "max_bytes must be a non-negative integer"}
    return parsed


def _parse_range(value: Any) -> tuple[int | None, int | None] | dict[str, str]:
    if value is None:
        return None, None
    if not isinstance(value, Mapping):
        return {"message": "range must be null or an object with start/end byte offsets"}

    start_result = _parse_optional_offset(value.get("start"), "range.start")
    if isinstance(start_result, dict):
        return start_result
    end_result = _parse_optional_offset(value.get("end"), "range.end")
    if isinstance(end_result, dict):
        return end_result

    start = start_result
    end = end_result
    if start is not None and end is not None and end < start:
        return {"message": "range.end must be greater than or equal to range.start"}
    return start, end


def _parse_optional_offset(value: Any, field: str) -> int | None | dict[str, str]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return {"message": f"{field} must be a non-negative integer"}
    if parsed < 0:
        return {"message": f"{field} must be a non-negative integer"}
    return parsed


def _gone_error(artifact_id: str, exc: ArtifactGoneError) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "ok": False,
        "error": "gone",
        "deleted_at": exc.tombstone.get("deleted_at"),
        "reason": exc.tombstone.get("reason"),
    }


def _error(artifact_id: str, error: str, *, message: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": artifact_id, "ok": False, "error": error}
    if message:
        payload["message"] = message
    return payload
