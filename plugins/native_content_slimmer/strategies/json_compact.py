from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from .base import CompressedView, Compressor
from .registry import register_compressor

STRATEGY_NAME = "json_compact"
EVAL_RUN_ID = "prd5-json-compact-phase1a"
THRESHOLD = "GO"
DEFAULT_PARAMS: dict[str, int] = {
    "max_depth": 128,
    "max_input_bytes": 2_000_000,
    "long_string_chars": 160,
    "long_array_items": 40,
    "long_array_chars": 1_000,
    "anomaly_keep_chars": 512,
}
JSON_LANES = ("web_extract", "terminal", "terminal-json")
_ANOMALY_WORDS = ("unhealthy", "fatal", "error", "panic", "failed", "failure", "critical")
_ALLOWED_RAW_CONTROLS = {"\t", "\n", "\r"}


class JsonCompactCompressor:
    """Deterministic structure-aware JSON compressor.

    The compressor preserves every object key and recursively compacts only long
    values. It returns ``None`` on invalid/pathological input so callers can
    fail open to the lossless offload preview.
    """

    strategy_name = STRATEGY_NAME

    def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView | None:
        merged_params: dict[str, object] = dict(DEFAULT_PARAMS)
        merged_params.update(dict(params or {}))
        if not isinstance(raw, str) or not raw.strip():
            return None
        if _has_disallowed_control(raw):
            return None
        if _raw_byte_len(raw) > _int_param(merged_params, "max_input_bytes"):
            return None
        max_depth = _int_param(merged_params, "max_depth")
        if _exceeds_json_depth(raw, max_depth):
            return None
        try:
            parsed = json.loads(raw)
        except (RecursionError, MemoryError, ValueError, TypeError):
            return None
        try:
            compacted = _compact_value(parsed, merged_params, depth=0)
            view_text = json.dumps(compacted, ensure_ascii=False, indent=2, sort_keys=True)
        except (RecursionError, MemoryError, ValueError, TypeError):
            return None
        return CompressedView(
            view_text=view_text,
            view_bytes=_raw_byte_len(view_text),
            lossy_view=True,
            recoverable=True,
            strategy_name=STRATEGY_NAME,
        )


def register() -> None:
    """Register json_compact for evaluated JSON lanes."""

    compressor = JsonCompactCompressor()
    for tool_name in JSON_LANES:
        register_compressor(
            tool_name=tool_name,
            content_class="json",
            compressor=cast(Compressor, compressor),
            eval_run_id=EVAL_RUN_ID,
            threshold=THRESHOLD,
            strategy_name=STRATEGY_NAME,
            params=DEFAULT_PARAMS,
        )


def _compact_value(value: Any, params: Mapping[str, object], *, depth: int) -> Any:
    if depth > _int_param(params, "max_depth"):
        raise ValueError("json depth exceeded")
    if isinstance(value, dict):
        return {str(key): _compact_value(child, params, depth=depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        if _should_summarize_array(value, params):
            return _array_summary(value)
        return [_compact_value(child, params, depth=depth + 1) for child in value]
    if isinstance(value, str):
        return _compact_string(value, params)
    return value


def _compact_string(value: str, params: Mapping[str, object]) -> str:
    if len(value) <= _int_param(params, "long_string_chars"):
        return value
    if _contains_anomaly(value) and len(value) <= _int_param(params, "anomaly_keep_chars"):
        return value
    return f"…{len(value)} chars…"


def _should_summarize_array(value: list[Any], params: Mapping[str, object]) -> bool:
    if len(value) < _int_param(params, "long_array_items"):
        return False
    if any(isinstance(item, (dict, list)) for item in value):
        return False
    if any(isinstance(item, str) and _contains_anomaly(item) for item in value):
        return False
    return len(_canonical_json(value)) >= _int_param(params, "long_array_chars")


def _array_summary(value: list[Any]) -> str:
    return f"…{len(_canonical_json(value))} chars, {len(value)} items…"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _contains_anomaly(value: str) -> bool:
    lowered = value.lower()
    return any(word in lowered for word in _ANOMALY_WORDS)


def _has_disallowed_control(raw: str) -> bool:
    return any(ord(char) < 32 and char not in _ALLOWED_RAW_CONTROLS for char in raw)


def _exceeds_json_depth(raw: str, max_depth: int) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                return True
        elif char in "]}":
            depth -= 1
            if depth < 0:
                return False
    return False


def _int_param(params: Mapping[str, object], name: str) -> int:
    value = params.get(name, DEFAULT_PARAMS[name])
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(DEFAULT_PARAMS[name])


def _raw_byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


register()
