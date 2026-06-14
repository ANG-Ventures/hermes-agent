"""Configuration helpers for the native_content_slimmer plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

PLUGIN_CONFIG_SECTION = "native_content_slimmer"
DEFAULT_ENABLED = False
DEFAULT_MODE = "shadow"
VALID_MODES = frozenset({"shadow", "active_lossless"})


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

    Defaults are disabled + shadow. If the plugin block exists but has an
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

    enabled = block.get("enabled", DEFAULT_ENABLED)
    if not isinstance(enabled, bool):
        errors.append("enabled must be a boolean")

    mode = block.get("mode", DEFAULT_MODE)
    if not isinstance(mode, str) or mode not in VALID_MODES:
        errors.append("mode must be one of: active_lossless, shadow")

    if errors:
        return NativeContentSlimmerConfig(
            enabled=False,
            mode=DEFAULT_MODE,
            valid=False,
            errors=tuple(errors),
        )

    return NativeContentSlimmerConfig(enabled=enabled, mode=mode)
