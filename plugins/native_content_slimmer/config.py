"""Configuration helpers for the native_content_slimmer plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .classifier import (
    DEFAULT_ALLOW_TOOLS,
    DEFAULT_DENY_ON_STATUS,
    DEFAULT_DENY_TOOLS,
    DEFAULT_MIN_BYTES,
    DEFAULT_PREVIEW_BYTES,
)

PLUGIN_CONFIG_SECTION = "native_content_slimmer"
DEFAULT_ENABLED = False
DEFAULT_MODE = "shadow"
VALID_MODES = frozenset({"shadow", "active_lossless"})
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


@dataclass(frozen=True)
class NativeContentSlimmerConfig:
    """Validated native content slimmer config.

    Invalid user config fails closed by forcing ``enabled=False`` while
    preserving a diagnostic in ``errors`` for tests and future status surfaces.
    """

    enabled: bool = DEFAULT_ENABLED
    mode: str = DEFAULT_MODE
    valid: bool = True
    errors: tuple[str, ...] = field(default_factory=tuple)
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
