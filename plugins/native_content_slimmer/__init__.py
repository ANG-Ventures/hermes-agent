"""Native content slimmer plugin entrypoint.

The plugin is disabled by default. When explicitly enabled, it wires the two
lossless slimming seams required by PRD #2 v2:

- ``transform_terminal_output`` for full terminal output before truncation.
- ``transform_tool_result`` for large non-terminal tool results.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import NativeContentSlimmerConfig, load_slimmer_config
from .hook import NativeContentSlimmerHooks, transform_terminal_output, transform_tool_result


def register(ctx: Any, config: Mapping[str, Any] | None = None) -> NativeContentSlimmerConfig:
    """Register plugin hooks when explicitly enabled."""

    cfg = load_slimmer_config(config)
    if not cfg.enabled:
        return cfg

    runtime = NativeContentSlimmerHooks(cfg)
    ctx.register_hook("transform_terminal_output", runtime.transform_terminal_output)
    ctx.register_hook("transform_tool_result", runtime.transform_tool_result)
    return cfg


__all__ = [
    "NativeContentSlimmerHooks",
    "NativeContentSlimmerConfig",
    "load_slimmer_config",
    "register",
    "transform_terminal_output",
    "transform_tool_result",
]
