"""Native content slimmer plugin entrypoint.

The plugin is disabled by default. When explicitly enabled, it wires the two
lossless slimming seams required by PRD #2 v2:

- ``transform_terminal_output`` for full terminal output before truncation.
- ``transform_tool_result`` for large non-terminal tool results.

Secret detection is a heuristic guard, not a scanner guarantee. The plugin only
blocks the documented patterns in ``classifier.contains_secret``; a
non-matching secret can still be persisted inside an on-disk artifact. Treat
``active_lossless`` as opt-in only where that limitation is acceptable and the
``expand_artifact`` tool is reachable.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .config import NativeContentSlimmerConfig, load_slimmer_config
from .hook import NativeContentSlimmerHooks, transform_terminal_output, transform_tool_result
from .tools import register_tools

logger = logging.getLogger(__name__)


def register(ctx: Any, config: Mapping[str, Any] | None = None) -> NativeContentSlimmerConfig:
    """Register plugin hooks and the expansion tool when explicitly enabled."""

    cfg = load_slimmer_config(config)
    if not cfg.enabled:
        return cfg

    runtime_cfg = cfg
    try:
        register_tools(ctx)
    except Exception as exc:
        if cfg.mode == "active_lossless":
            logger.error(
                "native_content_slimmer active_lossless cannot register expand_artifact; "
                "falling back to shadow mode: %s",
                exc,
            )
            runtime_cfg = NativeContentSlimmerConfig(
                enabled=True,
                mode="shadow",
                valid=cfg.valid,
                errors=cfg.errors + (f"expand_artifact registration failed: {exc}",),
            )
        else:
            logger.warning("native_content_slimmer could not register expand_artifact: %s", exc)

    runtime = NativeContentSlimmerHooks(runtime_cfg)
    ctx.register_hook("transform_terminal_output", runtime.transform_terminal_output)
    ctx.register_hook("transform_tool_result", runtime.transform_tool_result)
    return runtime_cfg


__all__ = [
    "NativeContentSlimmerHooks",
    "NativeContentSlimmerConfig",
    "load_slimmer_config",
    "register",
    "transform_terminal_output",
    "transform_tool_result",
]
