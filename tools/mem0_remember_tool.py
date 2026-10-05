"""mem0_remember: the background-review reviewer's long-term-memory write (opt-in).

Gated by ``memory.background_review_mem0_write`` (default off) AND a configured mem0 provider.
When both hold, the tool is RESIDENT in the parent's tools[] (``_HERMES_CORE_TOOLS``), so the
review fork, which inherits the parent's tools[] byte-for-byte for prompt-cache parity, can see
it; ``agent/background_review.py`` admits it to the fork's dispatch whitelist. A tool that is
whitelisted but absent from tools[] is invisible to the model; that was the 2026-06 bug where this
path was dark from merge until it was reverted.

Foreground turns get a refusal pointing at ``mem0_conclude``: this is the reviewer's surface.
The write itself (dedup/supersede ladder + ledger) lives in the mem0 plugin
(``Mem0MemoryProvider.remember``) and runs through a manager-free provider, because the review fork
is built with ``skip_memory=True`` and has no memory manager.
"""

import json
import logging
import threading

from tools.registry import no_cache_check_fn, registry, tool_error

logger = logging.getLogger(__name__)

CONFIG_KEY = "background_review_mem0_write"

REMEMBER_SCHEMA = {
    "name": "mem0_remember",
    "description": (
        "Background review only. Store ONE durable fact about the user or their stable environment "
        "in long-term memory (mem0), verbatim. Durable = a preference, a standing decision or "
        "correction, an account/device/service pointer (never the secret), topology, a long-lived "
        "plan or constraint, still true next week. Never save work-narration, status, PR/commit "
        "numbers, speculation, one-off requests or transient events. One fact per call, as a "
        "standalone declarative sentence. A fact already stored is skipped and the stored text is "
        "returned; if your fact CHANGES or CORRECTS a stored one, call again with `supersedes` set "
        "to the stored text so the update names what it replaces."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The single durable fact, as a standalone declarative sentence."},
            "supersedes": {
                "type": "string",
                "description": "Optional: the stored fact this one corrects or replaces (its text as returned).",
            },
        },
        "required": ["fact"],
    },
}

# One manager-free provider per Hermes home: a multiplex gateway serves several profiles from one
# process, so a single cached instance would write every profile's facts with the first profile's
# mem0 config.
_providers: dict = {}
_providers_lock = threading.Lock()


def _knob_on() -> bool:
    from hermes_cli.config import cfg_get, load_config_readonly

    value = cfg_get(load_config_readonly(), "memory", CONFIG_KEY, default=False)
    return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})


@no_cache_check_fn
def check_mem0_remember_requirements() -> bool:
    """Opt-in knob on, mem0 is the configured provider, and mem0 has a reachable config."""
    if not _knob_on():
        return False
    if not _mem0_selected():
        return False
    from plugins.memory.mem0 import Mem0MemoryProvider

    return Mem0MemoryProvider().is_available()


def _mem0_selected() -> bool:
    from hermes_cli.config import cfg_get, load_config_readonly

    return str(cfg_get(load_config_readonly(), "memory", "provider", default="") or "").strip() == "mem0"


def _get_provider():
    """Cached per (home, effective mem0 config): a config edit in a running gateway builds a new one."""
    from plugins.memory.mem0 import _load_config
    from hermes_constants import hermes_home_key

    key = (hermes_home_key(), json.dumps(_load_config(), sort_keys=True, default=str))
    with _providers_lock:
        provider = _providers.get(key)
        if provider is None:
            for old in [k for k in _providers if k[0] == key[0]]:
                _providers.pop(old, None)
            from plugins.memory.mem0 import Mem0MemoryProvider

            provider = Mem0MemoryProvider()
            provider.initialize("background-review-mem0-write", platform="background_review")
            _providers[key] = provider
        return provider


def mem0_remember_tool(fact: str, supersedes: str = "") -> str:
    from tools.skill_provenance import is_background_review

    if not is_background_review():
        return tool_error("mem0_remember is the background reviewer's tool; use mem0_conclude in a live turn.")
    if not (fact or "").strip():
        return tool_error("Missing required parameter: fact")
    if not _knob_on():
        return tool_error(f"mem0_remember is off (memory.{CONFIG_KEY} is false).")
    if not _mem0_selected():
        return tool_error("mem0_remember is off (memory.provider is not mem0).")
    try:
        provider = _get_provider()
    except Exception as e:
        return tool_error(f"mem0 unavailable: {e}")
    return json.dumps(provider.remember(fact.strip(), supersedes=(supersedes or "").strip()), ensure_ascii=False)


registry.register(
    name="mem0_remember",
    toolset="memory_write",
    schema=REMEMBER_SCHEMA,
    handler=lambda args, **kw: mem0_remember_tool(args.get("fact", ""), args.get("supersedes", "")),
    check_fn=check_mem0_remember_requirements,
    emoji="🧠",
)
