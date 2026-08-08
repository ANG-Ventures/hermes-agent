"""Context engine plugin discovery.

Scans ``plugins/context_engine/<name>/`` directories for context engine
plugins.  Each subdirectory must contain ``__init__.py`` with a class
implementing the ContextEngine ABC.

Context engines are separate from the general plugin system — they live
in the repo and are always available without user installation.  Only ONE
can be active at a time, selected via ``context.engine`` in config.yaml.
The default engine is ``"compressor"`` (the built-in ContextCompressor).

Usage:
    from plugins.context_engine import discover_context_engines, load_context_engine

    available = discover_context_engines()   # [(name, desc, available), ...]
    engine = load_context_engine("lcm")      # ContextEngine instance
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from agent.context_engine import ContextEngine

logger = logging.getLogger(__name__)

# Serializes the import critical-section in _load_engine_from_dir. Child agents
# run concurrently in a shared-process ThreadPoolExecutor (delegate_task,
# max_concurrent_children), so they share one sys.modules. Without this lock a
# second caller can observe a module registered in sys.modules but not yet
# exec_module()'d (the loader sets sys.modules[name]=mod BEFORE executing it),
# grab the half-initialized shell, find no register()/engine class, and fall
# back to the built-in compressor — a silent, intermittent partial-import race.
# RLock (not Lock) guards against any reentrant load during module exec.
_LOAD_LOCK = threading.RLock()

_CONTEXT_ENGINE_PLUGINS_DIR = Path(__file__).parent


def discover_context_engines() -> List[Tuple[str, str, bool]]:
    """Scan plugins/context_engine/ for available engines.

    Returns list of (name, description, is_available) tuples.
    Does NOT import the engines — just reads plugin.yaml for metadata
    and does a lightweight availability check.
    """
    results = []
    if not _CONTEXT_ENGINE_PLUGINS_DIR.is_dir():
        return results

    for child in sorted(_CONTEXT_ENGINE_PLUGINS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        init_file = child / "__init__.py"
        if not init_file.exists():
            continue

        # Read description from plugin.yaml if available
        desc = ""
        yaml_file = child / "plugin.yaml"
        if yaml_file.exists():
            try:
                import yaml
                with open(yaml_file, encoding="utf-8-sig") as f:
                    meta = yaml.safe_load(f) or {}
                desc = meta.get("description", "")
            except Exception:
                pass

        # Quick availability check — try loading and calling is_available()
        available = True
        try:
            engine = _load_engine_from_dir(child)
            if engine is None:
                available = False
            else:
                _is_available = getattr(engine, "is_available", None)
                if callable(_is_available):
                    available = _is_available()
        except Exception:
            available = False

        results.append((child.name, desc, available))

    return results


def load_context_engine(name: str) -> Optional["ContextEngine"]:
    """Load and return a ContextEngine instance by name.

    Returns None if the engine is not found or fails to load.
    """
    engine_dir = _CONTEXT_ENGINE_PLUGINS_DIR / name
    if not engine_dir.is_dir():
        logger.debug("Context engine '%s' not found in %s", name, _CONTEXT_ENGINE_PLUGINS_DIR)
        return None

    try:
        engine = _load_engine_from_dir(engine_dir)
        if engine:
            _register_host_token_counter(engine_dir, name)
            return engine
        logger.warning("Context engine '%s' loaded but no engine instance found", name)
        return None
    except Exception as e:
        logger.warning("Failed to load context engine '%s': %s", name, e)
        return None


def _register_host_token_counter(engine_dir: "Path", name: str) -> None:
    """Give an engine the host's calibrated token counter, if it accepts one.

    An engine that ships its own estimator is maintaining a SECOND, independent
    guess at the same quantity the host already estimates and calibrates against
    real provider usage. When the two diverge, compaction fires on numbers the
    user never sees (2026-08-07: LCM billed a screenshot at 502,182 tokens while
    the host — and the provider — said ~1,900).

    Only registers when the host estimate is genuinely BETTER INFORMED than the
    engine's own — currently the multimodal case, where the host knows provider
    media pricing. For plain text the engine's tokenizer is at least as good
    (it uses tiktoken) and, critically, is INTERNALLY CONSISTENT with the
    per-message counter its own budget arithmetic compares against; swapping in
    a differently-scaled whole-list estimate there makes ``count_messages_tokens``
    disagree with ``sum(count_message_tokens(...))`` and breaks fresh-tail
    budget walks. Purely optional: engines without
    ``set_messages_token_counter`` are untouched, and any failure leaves the
    engine on its built-in estimate.
    """
    try:
        tokens_path = engine_dir / "tokens.py"
        if not tokens_path.is_file():
            return
        import importlib

        tokens_mod = importlib.import_module(
            f"plugins.context_engine.{name}.tokens"
        )
        setter = getattr(tokens_mod, "set_messages_token_counter", None)
        if not callable(setter):
            return
        builtin = getattr(tokens_mod, "count_messages_tokens_builtin", None)
        media_cost = getattr(tokens_mod, "media_part_token_cost", None)
        if not callable(builtin) or not callable(media_cost):
            return
        from agent.model_metadata import estimate_messages_tokens_rough

        def _counter(messages):
            """Host estimate for multimodal lists; engine's own for pure text."""
            for msg in messages or ():
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                parts = content if isinstance(content, list) else None
                if parts is None and isinstance(content, dict) and content.get("_multimodal"):
                    parts = content.get("content")
                if isinstance(parts, list) and any(media_cost(p) for p in parts):
                    return estimate_messages_tokens_rough(messages)
                if isinstance(msg.get("_anthropic_content_blocks"), list):
                    return estimate_messages_tokens_rough(messages)
            return builtin(messages)

        setter(_counter)
        logger.debug("Registered host token counter with context engine '%s'", name)
    except Exception:
        logger.debug(
            "Could not register host token counter with '%s'; engine keeps its own",
            name,
            exc_info=True,
        )


def _load_engine_from_dir(engine_dir: Path) -> Optional["ContextEngine"]:
    """Import an engine module and extract the ContextEngine instance.

    Serialized under _LOAD_LOCK: concurrent child agents share one sys.modules,
    and the inner loader registers the module in sys.modules BEFORE exec_module()
    runs. Without this lock a concurrent caller could grab a half-initialized
    module and silently fall back to the built-in compressor.
    """
    with _LOAD_LOCK:
        return _load_engine_from_dir_locked(engine_dir)


def _load_engine_from_dir_locked(engine_dir: Path) -> Optional["ContextEngine"]:
    """Import an engine module and extract the ContextEngine instance.

    The module must have either:
    - A register(ctx) function (plugin-style) — we simulate a ctx
    - A top-level class that extends ContextEngine — we instantiate it
    """
    name = engine_dir.name
    module_name = f"plugins.context_engine.{name}"
    init_file = engine_dir / "__init__.py"

    if not init_file.exists():
        return None

    # Check if already loaded
    if module_name in sys.modules:
        mod = sys.modules[module_name]
    else:
        # Handle relative imports within the plugin
        # First ensure the parent packages are registered
        for parent in ("plugins", "plugins.context_engine"):
            if parent not in sys.modules:
                parent_path = Path(__file__).parent
                if parent == "plugins":
                    parent_path = parent_path.parent
                parent_init = parent_path / "__init__.py"
                if parent_init.exists():
                    spec = importlib.util.spec_from_file_location(
                        parent, str(parent_init),
                        submodule_search_locations=[str(parent_path)]
                    )
                    if spec:
                        parent_mod = importlib.util.module_from_spec(spec)
                        sys.modules[parent] = parent_mod
                        try:
                            spec.loader.exec_module(parent_mod)
                        except Exception:
                            pass

        # Now load the engine module
        spec = importlib.util.spec_from_file_location(
            module_name, str(init_file),
            submodule_search_locations=[str(engine_dir)]
        )
        if not spec:
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod

        # Register submodules so relative imports work
        for sub_file in engine_dir.glob("*.py"):
            if sub_file.name == "__init__.py":
                continue
            sub_name = sub_file.stem
            full_sub_name = f"{module_name}.{sub_name}"
            if full_sub_name not in sys.modules:
                sub_spec = importlib.util.spec_from_file_location(
                    full_sub_name, str(sub_file)
                )
                if sub_spec:
                    sub_mod = importlib.util.module_from_spec(sub_spec)
                    sys.modules[full_sub_name] = sub_mod
                    try:
                        sub_spec.loader.exec_module(sub_mod)
                    except Exception as e:
                        logger.debug("Failed to load submodule %s: %s", full_sub_name, e)

        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.debug("Failed to exec_module %s: %s", module_name, e)
            sys.modules.pop(module_name, None)
            return None

    # Try register(ctx) pattern first (how plugins are written)
    if hasattr(mod, "register"):
        collector = _EngineCollector(engine_name=name)
        try:
            mod.register(collector)
            if collector.engine:
                return collector.engine
        except Exception as e:
            # WARNING, not debug: a failed engine construction silently
            # downgrades every session to the built-in compressor, and the
            # only breadcrumb is this log line (2026-07-25: an lcm.db FTS
            # schema drift killed construction and the real exception was
            # invisible at debug level).
            logger.warning("register() failed for %s: %s", name, e, exc_info=True)

    # Fallback: find a ContextEngine subclass and instantiate it
    from agent.context_engine import ContextEngine
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name, None)
        if (isinstance(attr, type) and issubclass(attr, ContextEngine)
                and attr is not ContextEngine):
            try:
                return attr()
            except Exception:
                pass

    return None


class _EngineCollector:
    """Fake plugin context that captures register_context_engine calls.

    Plugin context engines using the standard ``register(ctx)`` pattern may
    also call ``ctx.register_command(...)`` to expose slash commands (e.g.
    ``/lcm``). Forward those to the global plugin command registry so they
    behave identically to commands registered by normal plugins.
    """

    def __init__(self, engine_name: str = ""):
        self.engine = None
        self._engine_name = engine_name or "context_engine"
        self._registered_commands: list[str] = []

    def register_context_engine(self, engine):
        self.engine = engine

    def register_command(
        self,
        name: str,
        handler,
        description: str = "",
        args_hint: str = "",
    ) -> None:
        """Forward to the global plugin command registry."""
        clean = (name or "").lower().strip().lstrip("/").replace(" ", "-")
        if not clean:
            logger.warning(
                "Context engine '%s' tried to register a command with an empty name.",
                self._engine_name,
            )
            return

        # Reject conflicts with built-in commands.
        try:
            from hermes_cli.commands import resolve_command
            if resolve_command(clean) is not None:
                logger.warning(
                    "Context engine '%s' tried to register command '/%s' which conflicts "
                    "with a built-in command. Skipping.",
                    self._engine_name, clean,
                )
                return
        except Exception:
            pass

        try:
            from hermes_cli.plugins import get_plugin_manager
            manager = get_plugin_manager()
            if clean in manager._plugin_commands:
                # Don't clobber a regular plugin's command — same conflict
                # policy the plugin system uses for plugin-vs-plugin collisions.
                logger.warning(
                    "Context engine '%s' tried to register command '/%s' which "
                    "is already registered by a plugin. Skipping.",
                    self._engine_name, clean,
                )
                return
            manager._plugin_commands[clean] = {
                "handler": handler,
                "description": description or "Context engine command",
                "plugin": f"context-engine:{self._engine_name}",
                "args_hint": (args_hint or "").strip(),
            }
            self._registered_commands.append(clean)
            logger.debug(
                "Context engine '%s' registered command: /%s",
                self._engine_name, clean,
            )
        except Exception as exc:
            logger.debug(
                "Context engine '%s' could not register /%s: %s",
                self._engine_name, clean, exc,
            )

    # No-op for other registration methods
    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass

    def register_memory_provider(self, *args, **kwargs):
        pass
