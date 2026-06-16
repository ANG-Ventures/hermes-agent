"""Configuration helpers for the native_content_slimmer plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .classifier import (
    COMPRESS_OFFLOAD,
    DEFAULT_ALLOW_TOOLS,
    DEFAULT_DENY_ON_STATUS,
    DEFAULT_DENY_TOOLS,
    DEFAULT_MIN_BYTES,
    DEFAULT_PREVIEW_BYTES,
    LOSSLESS_OFFLOAD,
    PASS_THROUGH,
)

PLUGIN_CONFIG_SECTION = "native_content_slimmer"
DEFAULT_ENABLED = False
DEFAULT_MODE = "shadow"
VALID_MODES = frozenset({"shadow", "active_lossless"})
SLIMMER_MODE_OFF = "off"
SLIMMER_MODE_SHADOW = "shadow"
SLIMMER_MODE_ACTIVE = "active"
DEFAULT_SLIMMER_MODE = SLIMMER_MODE_OFF
VALID_SLIMMER_MODES = frozenset(
    {SLIMMER_MODE_OFF, SLIMMER_MODE_SHADOW, SLIMMER_MODE_ACTIVE}
)
COMPRESSION_MODE_OFF = "off"
COMPRESSION_MODE_SHADOW = "shadow"
COMPRESSION_MODE_ACTIVE = "active"
COMPRESSION_MODE_CANARY = "canary"
DEFAULT_COMPRESSION_MODE = COMPRESSION_MODE_OFF
VALID_COMPRESSION_MODES = frozenset(
    {
        COMPRESSION_MODE_OFF,
        COMPRESSION_MODE_SHADOW,
        COMPRESSION_MODE_ACTIVE,
        COMPRESSION_MODE_CANARY,
    }
)
ACTIVE_COMPRESSION_MODES = frozenset({COMPRESSION_MODE_ACTIVE, COMPRESSION_MODE_CANARY})
DEFAULT_COMPRESSION_CANARY_PERCENT = 0.0
DEFAULT_COMPRESSION_BREAKER_CEILING = 0.25
DEFAULT_ARTIFACT_TTL_DAYS = 14
DEFAULT_ARTIFACT_MAX_BYTES_PER_PROFILE = 2_147_483_648
DEFAULT_ARTIFACT_GC_ON_START = False
DEFAULT_ARTIFACT_GC_ON_SESSION_END = False
DEFAULT_ARTIFACT_GC_ON_SESSION_RESET = False
DEFAULT_ARTIFACT_GC_AFTER_WRITE_EVERY = 25
DEFAULT_SAVINGS_RETENTION_DAYS = 30
DEFAULT_ARTIFACT_GC_MODE = "async_best_effort"
VALID_ARTIFACT_GC_MODES = frozenset({DEFAULT_ARTIFACT_GC_MODE})
DEFAULT_SECRET_POLICY = "no_store_pass_through"
VALID_SECRET_POLICIES = frozenset({DEFAULT_SECRET_POLICY})


class NativeContentSlimmerConfigError(ValueError):
    """Raised for unsafe native_content_slimmer mode combinations."""


@dataclass(frozen=True)
class ModePrecedenceDecision:
    """Resolved runtime action for one offload/compression mode tuple."""

    slimmer_mode: str
    compression_mode: str
    eval_passed: bool
    outcome: str
    passes_through: bool
    emits_lossless_marker: bool
    emits_compressed_marker: bool


@dataclass(frozen=True)
class NativeContentSlimmerConfig:
    """Validated native content slimmer config.

    Invalid user config fails closed by forcing ``enabled=False`` while
    preserving a diagnostic in ``errors`` for tests and future status surfaces.
    """

    enabled: bool = DEFAULT_ENABLED
    mode: str = DEFAULT_MODE
    slimmer_mode: str = DEFAULT_SLIMMER_MODE
    compression_mode: str = DEFAULT_COMPRESSION_MODE
    valid: bool = True
    errors: tuple[str, ...] = field(default_factory=tuple)
    compression_strategies: Mapping[str, bool] = field(default_factory=dict)
    compression_lane_params: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    compression_canary_percent: float = DEFAULT_COMPRESSION_CANARY_PERCENT
    compression_breaker_ceiling: float = DEFAULT_COMPRESSION_BREAKER_CEILING
    artifact_ttl_days: int = DEFAULT_ARTIFACT_TTL_DAYS
    artifact_max_bytes_per_profile: int = DEFAULT_ARTIFACT_MAX_BYTES_PER_PROFILE
    artifact_gc_on_start: bool = DEFAULT_ARTIFACT_GC_ON_START
    artifact_gc_on_session_end: bool = DEFAULT_ARTIFACT_GC_ON_SESSION_END
    artifact_gc_on_session_reset: bool = DEFAULT_ARTIFACT_GC_ON_SESSION_RESET
    artifact_gc_after_write_every: int = DEFAULT_ARTIFACT_GC_AFTER_WRITE_EVERY
    savings_retention_days: int = DEFAULT_SAVINGS_RETENTION_DAYS
    artifact_gc_mode: str = DEFAULT_ARTIFACT_GC_MODE
    min_bytes: int = DEFAULT_MIN_BYTES
    preview_bytes: int = DEFAULT_PREVIEW_BYTES
    allow_tools: frozenset[str] | None = DEFAULT_ALLOW_TOOLS
    deny_tools: frozenset[str] = DEFAULT_DENY_TOOLS
    deny_on_status: frozenset[str] = DEFAULT_DENY_ON_STATUS
    secret_policy: str = DEFAULT_SECRET_POLICY


def resolve_mode_precedence(
    *,
    slimmer_mode: str,
    compression_mode: str,
    eval_passed: bool,
) -> ModePrecedenceDecision:
    """Resolve PRD-5 Invariant 12 into exactly one runtime outcome."""

    if slimmer_mode not in VALID_SLIMMER_MODES:
        raise NativeContentSlimmerConfigError("slimmer_mode must be one of: active, off, shadow")
    if compression_mode not in VALID_COMPRESSION_MODES:
        raise NativeContentSlimmerConfigError(
            "compression_mode must be one of: active, canary, off, shadow"
        )
    if slimmer_mode == SLIMMER_MODE_OFF and compression_mode in ACTIVE_COMPRESSION_MODES:
        raise NativeContentSlimmerConfigError(
            f"compression_mode={compression_mode} requires slimmer_mode=shadow or slimmer_mode=active"
        )

    outcome = PASS_THROUGH
    if compression_mode in ACTIVE_COMPRESSION_MODES and eval_passed:
        outcome = COMPRESS_OFFLOAD
    elif slimmer_mode == SLIMMER_MODE_ACTIVE:
        outcome = LOSSLESS_OFFLOAD

    return ModePrecedenceDecision(
        slimmer_mode=slimmer_mode,
        compression_mode=compression_mode,
        eval_passed=bool(eval_passed),
        outcome=outcome,
        passes_through=outcome == PASS_THROUGH,
        emits_lossless_marker=outcome == LOSSLESS_OFFLOAD,
        emits_compressed_marker=outcome == COMPRESS_OFFLOAD,
    )


def _plugin_block(config: Mapping[str, Any] | None) -> Any:
    if not isinstance(config, Mapping):
        return None
    plugins = config.get("plugins")
    if not isinstance(plugins, Mapping):
        return None
    return plugins.get(PLUGIN_CONFIG_SECTION)


def load_slimmer_config(config: Mapping[str, Any] | None = None) -> NativeContentSlimmerConfig:
    """Return validated plugin config.

    Expected shape::

        plugins:
          native_content_slimmer:
            enabled: false
            mode: shadow
            slimmer_mode: off
            compression_mode: off
            compression_strategies: {json_compact: true}
            compression_lane_params: {terminal:json: {max_items: 20}}
            compression_canary_percent: 0.0
            compression_breaker_ceiling: 0.25
            artifact_ttl_days: 14
            artifact_max_bytes_per_profile: 2147483648
            artifact_gc_on_start: false
            artifact_gc_on_session_end: false
            artifact_gc_on_session_reset: false
            artifact_gc_after_write_every: 25
            artifact_gc_mode: async_best_effort
            min_bytes: 12000
            preview_bytes: 2500
            allow_tools: [terminal, web_extract, browser_console]
            deny_tools: [discord_admin, ha_call_service, memory, mem0_conclude, send_message]
            deny_on_status: [error]
            secret_policy: no_store_pass_through

    Defaults are disabled + shadow, and the code defaults here are authoritative
    even where PRD prose shows broader lifecycle examples. If the plugin block exists but has an
    invalid type/value, return a disabled config so the plugin fails closed.
    Unknown keys are ignored to keep future config additions forward-compatible.
    """

    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception as exc:
            return NativeContentSlimmerConfig(
                enabled=False,
                mode=DEFAULT_MODE,
                valid=False,
                errors=(f"config_load_error: {exc}",),
            )

    block = _plugin_block(config)
    if block is None:
        return NativeContentSlimmerConfig()
    if not isinstance(block, Mapping):
        return NativeContentSlimmerConfig(
            enabled=False,
            mode=DEFAULT_MODE,
            valid=False,
            errors=("plugins.native_content_slimmer must be a mapping",),
        )

    errors: list[str] = []

    enabled = _parse_bool(block, "enabled", DEFAULT_ENABLED, errors)
    mode = block.get("mode", DEFAULT_MODE)
    if not isinstance(mode, str) or mode not in VALID_MODES:
        errors.append("mode must be one of: active_lossless, shadow")
        mode = DEFAULT_MODE
    explicit_slimmer_mode = "slimmer_mode" in block
    slimmer_mode = (
        block.get("slimmer_mode")
        if explicit_slimmer_mode
        else _slimmer_mode_from_legacy(enabled, mode)
    )
    if not isinstance(slimmer_mode, str) or slimmer_mode not in VALID_SLIMMER_MODES:
        errors.append("slimmer_mode must be one of: active, off, shadow")
        slimmer_mode = DEFAULT_SLIMMER_MODE
    elif explicit_slimmer_mode:
        enabled = slimmer_mode != SLIMMER_MODE_OFF
        mode = _legacy_mode_from_slimmer(slimmer_mode)
    compression_mode = block.get("compression_mode", DEFAULT_COMPRESSION_MODE)
    if not isinstance(compression_mode, str) or compression_mode not in VALID_COMPRESSION_MODES:
        errors.append("compression_mode must be one of: active, canary, off, shadow")
        compression_mode = DEFAULT_COMPRESSION_MODE
    resolve_mode_precedence(
        slimmer_mode=slimmer_mode,
        compression_mode=compression_mode,
        eval_passed=False,
    )

    artifact_ttl_days = _parse_nonnegative_int(
        block,
        "artifact_ttl_days",
        DEFAULT_ARTIFACT_TTL_DAYS,
        errors,
    )
    artifact_max_bytes_per_profile = _parse_nonnegative_int(
        block,
        "artifact_max_bytes_per_profile",
        DEFAULT_ARTIFACT_MAX_BYTES_PER_PROFILE,
        errors,
    )
    artifact_gc_on_start = _parse_bool(
        block,
        "artifact_gc_on_start",
        DEFAULT_ARTIFACT_GC_ON_START,
        errors,
    )
    artifact_gc_on_session_end = _parse_bool(
        block,
        "artifact_gc_on_session_end",
        DEFAULT_ARTIFACT_GC_ON_SESSION_END,
        errors,
    )
    artifact_gc_on_session_reset = _parse_bool(
        block,
        "artifact_gc_on_session_reset",
        DEFAULT_ARTIFACT_GC_ON_SESSION_RESET,
        errors,
    )
    artifact_gc_after_write_every = _parse_nonnegative_int(
        block,
        "artifact_gc_after_write_every",
        DEFAULT_ARTIFACT_GC_AFTER_WRITE_EVERY,
        errors,
    )
    savings_retention_days = _parse_nonnegative_int(
        block,
        "savings_retention_days",
        DEFAULT_SAVINGS_RETENTION_DAYS,
        errors,
    )
    artifact_gc_mode = block.get("artifact_gc_mode", DEFAULT_ARTIFACT_GC_MODE)
    if not isinstance(artifact_gc_mode, str) or artifact_gc_mode not in VALID_ARTIFACT_GC_MODES:
        errors.append("artifact_gc_mode must be async_best_effort")
        artifact_gc_mode = DEFAULT_ARTIFACT_GC_MODE

    min_bytes = _parse_nonnegative_int(block, "min_bytes", DEFAULT_MIN_BYTES, errors)
    preview_bytes = _parse_nonnegative_int(block, "preview_bytes", DEFAULT_PREVIEW_BYTES, errors)
    allow_tools = _parse_optional_string_set(block, "allow_tools", DEFAULT_ALLOW_TOOLS, errors)
    deny_tools = _parse_string_set(block, "deny_tools", DEFAULT_DENY_TOOLS, errors)
    deny_on_status = frozenset(
        value.lower() for value in _parse_string_set(block, "deny_on_status", DEFAULT_DENY_ON_STATUS, errors)
    )
    secret_policy = block.get("secret_policy", DEFAULT_SECRET_POLICY)
    if not isinstance(secret_policy, str) or secret_policy not in VALID_SECRET_POLICIES:
        errors.append("secret_policy must be no_store_pass_through")
        secret_policy = DEFAULT_SECRET_POLICY
    compression_strategies = _parse_compression_strategies(block, errors)
    compression_lane_params = _parse_compression_lane_params(block, errors)
    compression_canary_percent = _parse_float_range(
        block,
        "compression_canary_percent",
        DEFAULT_COMPRESSION_CANARY_PERCENT,
        0.0,
        100.0,
        errors,
    )
    compression_breaker_ceiling = _parse_float_range(
        block,
        "compression_breaker_ceiling",
        DEFAULT_COMPRESSION_BREAKER_CEILING,
        0.0,
        1.0,
        errors,
    )

    if errors:
        return NativeContentSlimmerConfig(
            enabled=False,
            mode=DEFAULT_MODE,
            valid=False,
            errors=tuple(errors),
        )

    return NativeContentSlimmerConfig(
        enabled=enabled,
        mode=mode,
        slimmer_mode=slimmer_mode,
        compression_mode=compression_mode,
        compression_strategies=compression_strategies,
        compression_lane_params=compression_lane_params,
        compression_canary_percent=compression_canary_percent,
        compression_breaker_ceiling=compression_breaker_ceiling,
        artifact_ttl_days=artifact_ttl_days,
        artifact_max_bytes_per_profile=artifact_max_bytes_per_profile,
        artifact_gc_on_start=artifact_gc_on_start,
        artifact_gc_on_session_end=artifact_gc_on_session_end,
        artifact_gc_on_session_reset=artifact_gc_on_session_reset,
        artifact_gc_after_write_every=artifact_gc_after_write_every,
        savings_retention_days=savings_retention_days,
        artifact_gc_mode=artifact_gc_mode,
        min_bytes=min_bytes,
        preview_bytes=preview_bytes,
        allow_tools=allow_tools,
        deny_tools=deny_tools,
        deny_on_status=deny_on_status,
        secret_policy=secret_policy,
    )


def _parse_bool(block: Mapping[str, Any], key: str, default: bool, errors: list[str]) -> bool:
    value = block.get(key, default)
    if not isinstance(value, bool):
        errors.append(f"{key} must be a boolean")
        return default
    return value


def _parse_nonnegative_int(
    block: Mapping[str, Any],
    key: str,
    default: int,
    errors: list[str],
) -> int:
    value = block.get(key, default)
    if isinstance(value, bool):
        errors.append(f"{key} must be a non-negative integer")
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"{key} must be a non-negative integer")
        return default
    if parsed < 0:
        errors.append(f"{key} must be a non-negative integer")
        return default
    return parsed


def _slimmer_mode_from_legacy(enabled: bool, mode: str) -> str:
    if not enabled:
        return SLIMMER_MODE_OFF
    if mode == "active_lossless":
        return SLIMMER_MODE_ACTIVE
    return SLIMMER_MODE_SHADOW


def _legacy_mode_from_slimmer(slimmer_mode: str) -> str:
    if slimmer_mode == SLIMMER_MODE_ACTIVE:
        return "active_lossless"
    return DEFAULT_MODE


def _parse_compression_strategies(
    block: Mapping[str, Any],
    errors: list[str],
) -> dict[str, bool]:
    value = block.get("compression_strategies", {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        errors.append("compression_strategies must be a mapping")
        return {}
    parsed: dict[str, bool] = {}
    for raw_name, raw_setting in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            errors.append("compression_strategies keys must be non-empty strings")
            return {}
        name = raw_name.strip()
        if isinstance(raw_setting, bool):
            parsed[name] = raw_setting
            continue
        if isinstance(raw_setting, Mapping):
            enabled = raw_setting.get("enabled", True)
            if not isinstance(enabled, bool):
                errors.append("compression_strategies enabled values must be booleans")
                return {}
            parsed[name] = enabled
            continue
        errors.append("compression_strategies values must be booleans or mappings")
        return {}
    return parsed


def _parse_compression_lane_params(
    block: Mapping[str, Any],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    value = block.get("compression_lane_params", {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        errors.append("compression_lane_params must be a mapping")
        return {}
    parsed: dict[str, dict[str, Any]] = {}
    for raw_lane, raw_params in value.items():
        if not isinstance(raw_lane, str) or not raw_lane.strip():
            errors.append("compression_lane_params keys must be non-empty strings")
            return {}
        if not isinstance(raw_params, Mapping):
            errors.append("compression_lane_params values must be mappings")
            return {}
        parsed[raw_lane.strip()] = dict(raw_params)
    return parsed


def _parse_float_range(
    block: Mapping[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float:
    value = block.get(key, default)
    if isinstance(value, bool):
        errors.append(f"{key} must be a number between {minimum:g} and {maximum:g}")
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        errors.append(f"{key} must be a number between {minimum:g} and {maximum:g}")
        return default
    if parsed < minimum or parsed > maximum:
        errors.append(f"{key} must be a number between {minimum:g} and {maximum:g}")
        return default
    return parsed


def _parse_string_set(
    block: Mapping[str, Any],
    key: str,
    default: frozenset[str],
    errors: list[str],
) -> frozenset[str]:
    parsed = _parse_optional_string_set(block, key, default, errors)
    if parsed is None:
        errors.append(f"{key} must be a list of strings")
        return default
    return parsed


def _parse_optional_string_set(
    block: Mapping[str, Any],
    key: str,
    default: frozenset[str] | None,
    errors: list[str],
) -> frozenset[str] | None:
    value = block.get(key, default)
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        errors.append(f"{key} must be a list of strings")
        return default
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{key} must be a list of strings")
            return default
        result.append(item.strip())
    return frozenset(result)
