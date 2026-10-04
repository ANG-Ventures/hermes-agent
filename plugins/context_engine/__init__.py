"""Context engine plugin discovery: bundled ``plugins/context_engine/<name>/`` then user
``$HERMES_HOME/plugins/<name>/`` (bundled wins on collision) → ``ContextEngine``. Separate from the
general plugin system: ``context.engine`` in config.yaml names the active engine (default
``"compressor"``, the built-in ContextCompressor), so a user-installed engine needs no
``plugins.enabled`` entry to be selectable."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from plugins import plugin_loader as _loader

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
# Engine construction slower than this is logged as PHASE=context_engine_load_slow.
# Healthy init on the fleet's largest DB (10.9 GB) measures 0.14 s; 5 s is 35x that.
ENGINE_LOAD_SLOW_S = float(os.environ.get("HERMES_ENGINE_LOAD_SLOW_S", "5"))

_CONTEXT_ENGINE_PLUGINS_DIR = Path(__file__).parent
# Synthetic parent package for user-installed engines (keeps them out of the bundled namespace).
_USER_NAMESPACE = "_hermes_user_context_engine"


def _is_context_engine_dir(path: Path) -> bool:
    """Cheap text heuristic: ``__init__.py`` mentions the context engine contract."""
    init_file = path / "__init__.py"
    try:
        source = init_file.read_text(errors="replace", encoding="utf-8-sig")[:8192]
    except OSError:
        return False
    return "register_context_engine" in source or "ContextEngine" in source


def _iter_engine_dirs() -> List[Tuple[str, Path]]:
    """``(name, path)`` for bundled then user engines; bundled wins on collisions."""
    dirs = [(child.name, child) for child in _loader.iter_plugin_dirs(_CONTEXT_ENGINE_PLUGINS_DIR)]
    seen = {name for name, _ in dirs}
    user_dir = _loader.user_plugins_dir()
    if user_dir:
        dirs.extend((child.name, child) for child in _loader.iter_plugin_dirs(user_dir)
                    if child.name not in seen and _is_context_engine_dir(child))
    return dirs


def discover_context_engines() -> List[Tuple[str, str, bool]]:
    """Return ``[(name, description, is_available), ...]`` for every bundled and user engine."""
    return [(name, _loader.read_plugin_description(child),
             _loader.probe_availability(lambda c=child: _load_engine_from_dir(c)))
            for name, child in _iter_engine_dirs()]


def find_engine_dir(name: str) -> Optional[Path]:
    """Resolve an engine name to its directory (bundled first, then user-installed)."""
    bundled = _CONTEXT_ENGINE_PLUGINS_DIR / name
    if bundled.is_dir():
        return bundled
    user_dir = _loader.user_plugins_dir()
    user = user_dir / name if user_dir else None
    return user if user and user.is_dir() and _is_context_engine_dir(user) else None


def load_context_engine(name: str) -> Optional["ContextEngine"]:  # noqa: F821
    """Load a ContextEngine instance by name; None if not found or it fails to load."""
    engine_dir = find_engine_dir(name)
    if engine_dir is None:
        logger.debug("Context engine '%s' not found in bundled or user plugins", name)
        return None
    engine = _loader.load_named(
        name, engine_dir, _load_engine_from_dir, kind="Context engine", noun="engine", logger=logger
    )
    if engine:
        _register_host_token_counter(engine_dir, name)
    return engine


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


def _load_engine_from_dir(engine_dir: Path) -> Optional["ContextEngine"]:  # noqa: F821
    """Import an engine module and extract the ContextEngine instance.

    Serialized under _LOAD_LOCK: concurrent child agents share one sys.modules,
    and the inner loader registers the module in sys.modules BEFORE exec_module()
    runs. Without this lock a concurrent caller could grab a half-initialized
    module and silently fall back to the built-in compressor.
    """
    # Everything under this lock is on EVERY turn's critical path: while one
    # thread builds the engine, every other turn's init_agent() waits here.
    # Measured 2026-09-22 (Mac Studio, default gateway): two one-time SQL
    # backfills in the LCM store ran as full-table scans of a 10.9 GB
    # messages table at every boot, ~21-25 min each, and all 8-10 in-flight
    # turns sat in this exact `with` for the duration — the user saw "Apollo
    # is frozen" with no log line anywhere naming the cause. Time the hold and
    # say so out loud when it is slow; a stall behind this lock must never
    # again be silent.
    t0 = time.monotonic()
    waited_for_lock = 0.0
    acquired = _LOAD_LOCK.acquire(blocking=False)
    if not acquired:
        _LOAD_LOCK.acquire()
        waited_for_lock = time.monotonic() - t0
    try:
        t1 = time.monotonic()
        result = _load_engine_from_dir_locked(engine_dir)
        held = time.monotonic() - t1
        if held >= ENGINE_LOAD_SLOW_S or waited_for_lock >= ENGINE_LOAD_SLOW_S:
            logger.warning(
                "PHASE=context_engine_load_slow engine=%s held=%.1fs waited_for_lock=%.1fs "
                "threshold=%.0fs — every concurrent turn was blocked behind this load; "
                "if held is large, something in engine init scales with data size "
                "(see plugins/context_engine/lcm/store.py _init_db and the "
                "init-path lint in tests/context_engine/test_lcm_backfill_cost.py)",
                engine_dir.name, held, waited_for_lock, ENGINE_LOAD_SLOW_S,
            )
        return result
    finally:
        _LOAD_LOCK.release()


def _load_engine_from_dir_locked(engine_dir: Path) -> Optional["ContextEngine"]:  # noqa: F821
    """Import an engine module and extract its ContextEngine (register(ctx) or subclass)."""
    from agent.context_engine import ContextEngine
    name = engine_dir.name
    is_bundled = engine_dir.parent == _CONTEXT_ENGINE_PLUGINS_DIR
    module_name = f"plugins.context_engine.{name}" if is_bundled else f"{_USER_NAMESPACE}.{name}"
    mod = _loader.load_plugin_module(
        module_name, engine_dir, parents=("plugins", "plugins.context_engine"), logger=logger,
        synthetic_namespace=None if is_bundled else _USER_NAMESPACE)
    return mod and _loader.instance_from_module(
        mod, collector=_EngineCollector(engine_name=name), collected_attr="engine",
        base_cls=ContextEngine, name=name, logger=logger)


class _EngineCollector(_loader.NoopPluginContext):
    """Captures register_context_engine; forwards register_command to the global plugin command
    registry so engine slash commands behave like plugin ones."""

    def __init__(self, engine_name: str = ""):
        self.engine = None
        self._engine_name = engine_name or "context_engine"

    def register_context_engine(self, engine):
        self.engine = engine

    def register_command(self, name: str, handler, description: str = "", args_hint: str = "") -> None:
        clean = (name or "").lower().strip().lstrip("/").replace(" ", "-")
        if not clean:
            logger.warning("Context engine '%s' tried to register a command with an empty name.",
                           self._engine_name)
            return
        conflict = "Context engine '%s' tried to register command '/%s' which %s Skipping."
        try:
            from hermes_cli.commands import resolve_command
            if resolve_command(clean) is not None:
                logger.warning(conflict, self._engine_name, clean, "conflicts with a built-in command.")
                return
        except Exception:
            pass
        try:
            from hermes_cli.plugins import get_plugin_manager
            manager = get_plugin_manager()
            if clean in manager._plugin_commands:
                logger.warning(conflict, self._engine_name, clean, "is already registered by a plugin.")
                return
            manager._plugin_commands[clean] = {
                "handler": handler, "description": description or "Context engine command",
                "plugin": f"context-engine:{self._engine_name}", "args_hint": (args_hint or "").strip()}
            logger.debug("Context engine '%s' registered command: /%s", self._engine_name, clean)
        except Exception as exc:
            logger.debug("Context engine '%s' could not register /%s: %s", self._engine_name, clean, exc)
