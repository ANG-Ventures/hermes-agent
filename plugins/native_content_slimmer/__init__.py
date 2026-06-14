"""Native content slimmer plugin skeleton.

The plugin is disabled by default and currently registers inert hook callbacks
only when its own config explicitly enables it. Future phases will add shadow
telemetry and active lossless artifact offload behind this config gate.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import NativeContentSlimmerConfig, load_slimmer_config


def transform_terminal_output(**_: Any) -> None:
    """Placeholder terminal pre-truncation hook.

    Returning ``None`` leaves the terminal output unchanged.
    """

    return None


def transform_tool_result(**_: Any) -> None:
    """Placeholder generic tool-result hook.

    Returning ``None`` leaves the tool result unchanged.
    """

    return None


def register(ctx: Any, config: Mapping[str, Any] | None = None) -> NativeContentSlimmerConfig:
    """Register plugin hooks when explicitly enabled.

    Invalid or absent config fails closed to disabled, so no hooks are wired
    unless ``plugins.native_content_slimmer.enabled`` is a real boolean true
    and ``mode`` is valid.
    """

    cfg = load_slimmer_config(config)
    if not cfg.enabled:
        return cfg

    ctx.register_hook("transform_terminal_output", transform_terminal_output)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    return cfg
