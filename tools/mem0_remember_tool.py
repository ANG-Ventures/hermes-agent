"""mem0_remember — the background-review deliberate-write tool (registry tool).

Registered in its OWN toolset ``memory_write`` (NOT ``memory``) so it is:
  - present in the PARENT's tools[] (parent enables all toolsets by default) ->
    inherited verbatim into the review fork's tools[] -> byte-stable / cache-safe;
  - DENIED at dispatch in the review fork, whose runtime whitelist is built from
    the ``memory``+``skills`` toolsets only -> denied-not-absent;
  - dispatched MANAGER-FREE through the regular tool registry (handle_function_call),
    NOT the memory-provider path that requires agent._memory_manager (None in the
    skip_memory=True fork). This is the Phase-0 design correction (probe 0.2).

The config flag ``memory.background_review_mem0_write`` adds ``mem0_remember`` to the
fork whitelist; without it the tool stays denied. See
docs/2026-06-27_mem0-in-background-review-spec.md §5A.
"""

import logging
import threading

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# A lazily-built, process-local mem0 provider used ONLY for the manager-free write.
# It is independent of the per-session memory manager (which the fork lacks), and is
# safe to share: the provider's client is thread-safe (its own lock) and stateless
# per call. Built on first use so import stays cheap and side-effect-free.
_provider = None
_provider_lock = threading.Lock()


def _get_write_provider():
    global _provider
    with _provider_lock:
        if _provider is not None:
            return _provider
        from plugins.memory.mem0 import Mem0MemoryProvider
        p = Mem0MemoryProvider()
        # initialize() reads config/env and is idempotent; user_id is pinned to the
        # canonical id by the plugin's own pin logic (pin_user_id).
        p.initialize("background-review-mem0-write")
        _provider = p
        return _provider


def mem0_remember_tool(fact: str, **kwargs) -> str:
    """Dispatch a single deliberate mem0 write through the dedup ladder."""
    if not fact:
        return tool_error("Missing required parameter: fact")
    provider = _get_write_provider()
    return provider.handle_tool_call("mem0_remember", {"fact": fact})


def check_mem0_remember_requirements() -> bool:
    """Available only when mem0 is the configured self-host provider with a host set.

    Keeps the tool out of the registry entirely on profiles that don't run mem0,
    so it never appears in tools[] there (no dead schema cost).
    """
    try:
        from plugins.memory.mem0 import _load_config
        cfg = _load_config()
        return bool(str(cfg.get("host", "")).strip())
    except Exception:
        return False


# Single-source the schema from the plugin. Registered at MODULE TOP LEVEL so the
# registry's AST auto-discovery (tools/registry.py _is_registry_register_call, which
# only matches a top-level ``registry.register(...)`` statement) actually picks this
# file up. The check_fn keeps it out of the catalog on non-mem0 profiles.
from plugins.memory.mem0 import REMEMBER_SCHEMA as _REMEMBER_SCHEMA

registry.register(
    name="mem0_remember",
    toolset="memory_write",
    schema=_REMEMBER_SCHEMA,
    handler=lambda args, **kw: mem0_remember_tool(fact=args.get("fact", "")),
    check_fn=check_mem0_remember_requirements,
    emoji="🧠",
)
