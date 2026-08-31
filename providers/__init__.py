"""Provider module registry.

Provider profiles can live in three places:

1. Bundled plugins: ``plugins/model-providers/<name>/`` (shipped with hermes-agent)
2. User plugins: ``$HERMES_HOME/plugins/model-providers/<name>/``
3. Pip-installed plugins: distributions exposing a ``hermes_agent.plugins``
   entry point (``module:func`` callable or a self-registering ``module``)

Each plugin directory contains:
  - ``__init__.py`` — calls ``register_provider(profile)`` at import
  - ``plugin.yaml`` — manifest (name, kind: model-provider, version, description)

Discovery is lazy: the first call to ``get_provider_profile()`` or
``list_providers()`` scans both locations and imports every plugin. User
plugins override bundled plugins on name collision (last-writer-wins), so
third parties can monkey-patch or replace any built-in profile without
editing the repo.

For backward compatibility, ``providers/*.py`` files (other than ``base.py``
and ``__init__.py``) are still discovered via ``pkgutil.iter_modules``.
This lets out-of-tree users drop a single-file profile into an editable
install without the plugin dir structure. New profiles should prefer the
plugin layout.

Usage::

    from providers import get_provider_profile
    profile = get_provider_profile("nvidia")   # ProviderProfile or None
    profile = get_provider_profile("kimi")     # checks name + aliases
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from providers.base import OMIT_TEMPERATURE, ProviderProfile  # noqa: F401

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ProviderProfile] = {}
_ALIASES: dict[str, str] = {}
_PROVIDER_LIST_CACHE: list[ProviderProfile] | None = None
_discovered = False

# Repo-root ``plugins/model-providers/`` — populated at discovery time.
_BUNDLED_PLUGINS_DIR = (
    Path(__file__).resolve().parent.parent / "plugins" / "model-providers"
)


def register_provider(profile: ProviderProfile) -> None:
    """Register a provider profile by name and aliases.

    Later registrations with the same name replace earlier ones — so user
    plugins under ``$HERMES_HOME/plugins/model-providers/`` can override
    bundled profiles without editing repo code.
    """
    global _PROVIDER_LIST_CACHE
    _REGISTRY[profile.name] = profile
    for alias in profile.aliases:
        _ALIASES[alias] = profile.name
    _PROVIDER_LIST_CACHE = None


def _lint_provider_collisions() -> list[str]:
    """Detect accidental fN provider-alias/name/base_url/env-key collisions.

    Lint/observability ONLY — does NOT change registration semantics
    (last-writer-wins stays, so user plugins can still override bundled
    profiles). It surfaces the class of drift that a copy-paste fN plugin
    introduces silently: a duplicated ``name``/``alias`` shadowing a real
    failover lane, or two profiles pointing at the same ``base_url`` / env-key.

    Edge-triggered: returns one warning line per real collision and an empty
    list when the registry is clean (no per-boot spam). The proxy-vs-bridge
    case (same host, different port/path) is NOT a collision because the full
    ``base_url`` incl. port + path differs — the compare is on the full URL,
    never just the host.
    """
    warnings: list[str] = []
    profiles = list_providers()

    # name/alias → the profile.name that claims it. A second, DIFFERENT profile
    # claiming the same key is a collision (the shadowing that hides a lane).
    claim: dict[str, str] = {}
    for prof in profiles:
        keys = [("name", prof.name)] + [("alias", a) for a in (prof.aliases or ())]
        for kind, key in keys:
            k = str(key or "").strip().lower()
            if not k:
                continue
            prior = claim.get(k)
            if prior is not None and prior != prof.name:
                warnings.append(
                    f"provider {kind} collision: '{key}' is claimed by both "
                    f"'{prior}' and '{prof.name}'"
                )
            else:
                claim.setdefault(k, prof.name)

    # Full base_url collision (incl. port + path). Two DIFFERENT profiles on the
    # exact same endpoint is a copy-paste smell; same host + different port
    # (fN proxy :18801/anthropic vs fN bridge :3556/v1) is NOT flagged. A
    # deliberate api_key-vs-oauth split on the same upstream (e.g. minimax vs
    # minimax-oauth) is also NOT a collision — the auth_type differs, so they
    # are distinct lanes by design. Likewise (parity 2026-08-30, new upstream
    # provider shapes): a dual-API-MODE pair on one endpoint (commandcode
    # chat_completions vs commandcode-anthropic anthropic_messages) and a
    # free-vs-keyed pair (opencode-free with NO credential vs opencode-zen
    # keyed) are distinct lanes by design. Key the owner map on
    # (url, auth_type, api_mode, has-credential) — NOT the env value: two fN
    # lanes with per-lane keys pointed at one proxy URL are still the classic
    # copy-paste and must flag.
    url_owner: dict[str, str] = {}
    for prof in profiles:
        url = str(getattr(prof, "base_url", "") or "").strip().rstrip("/").lower()
        if not url:
            continue
        auth = str(getattr(prof, "auth_type", "") or "").strip().lower()
        mode = str(getattr(prof, "api_mode", "") or "").strip().lower()
        _envs0 = (getattr(prof, "env_vars", ()) or ())
        keyed = "keyed" if str(_envs0[0] if _envs0 else "").strip() else "free"
        okey = f"{url}\x00{auth}\x00{mode}\x00{keyed}"
        prior = url_owner.get(okey)
        if prior is not None and prior != prof.name:
            warnings.append(
                f"provider base_url collision: '{url}' is shared by "
                f"'{prior}' and '{prof.name}'"
            )
        else:
            url_owner.setdefault(okey, prof.name)

    # Env-key collision: two profiles whose PRIMARY credential env var
    # (``env_vars[0]``) is the same var. Only the primary is compared — a
    # provider that lists another lane's var as a SECONDARY fallback (e.g.
    # alibaba-coding-plan listing DASHSCOPE_API_KEY after its own
    # ALIBABA_CODING_PLAN_API_KEY) is deliberate credential reuse, not a
    # copy-paste collision, and must not warn on a clean fleet.
    # REGIONAL/MODE TWINS (parity 2026-08-30): upstream ships lane FAMILIES
    # that share ONE vendor credential by design — regional variants
    # (alibaba/alibaba-cn, alibaba-token-plan/-cn: same var, different
    # region endpoint) and dual-API-mode variants (commandcode/
    # commandcode-anthropic: same var, same endpoint, different wire mode).
    # A name that extends the other with a dashed suffix is one lane family,
    # not a copy-paste.
    def _same_lane_family(a: str, b: str) -> bool:
        a = str(a or "").strip().lower()
        b = str(b or "").strip().lower()
        if not a or not b:
            return False
        return a.startswith(b + "-") or b.startswith(a + "-")

    env_owner: dict[str, str] = {}
    for prof in profiles:
        _envs = getattr(prof, "env_vars", ()) or ()
        if not _envs:
            continue
        e = str(_envs[0] or "").strip().upper()
        if not e:
            continue
        prior = env_owner.get(e)
        if prior is not None and prior != prof.name:
            if _same_lane_family(prior, prof.name):
                continue  # deliberate family twin — one credential, N lanes
            warnings.append(
                f"provider env-key collision: '{e}' is the primary "
                f"credential for both '{prior}' and '{prof.name}'"
            )
        else:
            env_owner.setdefault(e, prof.name)

    return warnings


def lint_provider_collisions(emit_log: bool = True) -> list[str]:
    """Public entry: return (and optionally log) fN provider collision warnings.

    Ensures discovery has run, then runs the edge-triggered collision lint.
    ``hermes doctor`` and the fleet cli-parity sweep call this to surface an
    accidental fN name/alias/base_url/env-key collision on demand. Loud on a
    real collision; silent (empty list, no log) when clean.
    """
    if not _discovered:
        _discover_providers()
    warnings = _lint_provider_collisions()
    if emit_log:
        for w in warnings:
            logger.warning("Provider collision lint: %s", w)
    return warnings



def get_provider_profile(name: str) -> ProviderProfile | None:
    """Look up a provider profile by name or alias.

    Returns None if the provider has no profile (falls back to generic).
    """
    if not _discovered:
        _discover_providers()
    canonical = _ALIASES.get(name, name)
    return _REGISTRY.get(canonical)


def list_providers() -> list[ProviderProfile]:
    """Return all registered provider profiles (one per canonical name)."""
    global _PROVIDER_LIST_CACHE
    if not _discovered:
        _discover_providers()
    if _PROVIDER_LIST_CACHE is not None:
        return list(_PROVIDER_LIST_CACHE)
    # Deduplicate: _REGISTRY has canonical names; _ALIASES points to same objects
    seen: set[int] = set()
    result: list[ProviderProfile] = []
    for profile in _REGISTRY.values():
        pid = id(profile)
        if pid not in seen:
            seen.add(pid)
            result.append(profile)
    _PROVIDER_LIST_CACHE = result
    return list(result)


def _user_plugins_dir() -> Path | None:
    """Return ``$HERMES_HOME/plugins/model-providers/`` if it exists."""
    try:
        from hermes_constants import get_hermes_home

        d = get_hermes_home() / "plugins" / "model-providers"
        return d if d.is_dir() else None
    except Exception:
        return None


def _import_plugin_dir(plugin_dir: Path, source: str) -> None:
    """Import a single plugin directory so it self-registers.

    ``source`` is "bundled" or "user", used only for log messages.
    """
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return

    # Give bundled plugins a stable import path (``plugins.model_providers.<name>``)
    # so relative imports within the plugin work. User plugins load via
    # ``importlib.util.spec_from_file_location`` with a unique module name so
    # multiple HERMES_HOME profiles don't alias each other.
    safe_name = plugin_dir.name.replace("-", "_")
    if source == "bundled":
        module_name = f"plugins.model_providers.{safe_name}"
    else:
        module_name = f"_hermes_user_provider_{safe_name}"

    if module_name in sys.modules:
        return  # already imported

    try:
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(plugin_dir)]
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning(
            "Failed to load %s provider plugin %s: %s", source, plugin_dir.name, exc
        )
        sys.modules.pop(module_name, None)


def _discover_entry_point_providers() -> None:
    """Import pip-installed provider plugins via the ``hermes_agent.plugins``
    entry-point group so they self-register.

    A distribution ships::

        [project.entry-points."hermes_agent.plugins"]
        acme-inference = "acme_hermes_plugin:register"

    The target may be either a **callable** (``module:func`` — invoked with no
    args; typically calls ``register_provider(profile)``) or a **module**
    (``module`` — imported for its module-level ``register_provider`` side
    effect, mirroring the directory-plugin ``__init__.py`` contract).

    Gating and safety:

    * **Opt-in.** Entry-point plugins are subject to the same
      ``plugins.enabled`` allow-list (and ``plugins.disabled`` deny-list) the
      general PluginManager enforces — a pip package is never imported just
      because it is installed. An entry point whose name is not enabled is
      skipped without loading.
    * **Provider targets only.** The ``hermes_agent.plugins`` group is shared
      with general plugins whose target is ``register(ctx)``. Callables that
      require arguments are skipped here (the PluginManager owns them);
      provider registration hooks take no arguments by contract.

    Failures are swallowed per-entry (a broken third-party package must not
    break provider discovery) and logged at warning level. This scan runs
    first, so filesystem plugins (bundled + ``$HERMES_HOME``) keep their
    documented override precedence via last-writer-wins in
    ``register_provider()`` — a pip package cannot hijack a first-party
    provider name.
    """
    try:
        import importlib.metadata as _md
    except Exception:  # pragma: no cover — importlib.metadata always present ≥3.8
        return

    # Same opt-in gate as the general PluginManager: only entry points named
    # in ``plugins.enabled`` load, and ``plugins.disabled`` always wins.
    try:
        from hermes_cli.plugins import _get_disabled_plugins, _get_enabled_plugins

        enabled = _get_enabled_plugins()  # None = nothing enabled yet (opt-in default)
        disabled = _get_disabled_plugins()
    except Exception:  # pragma: no cover — config layer unavailable
        enabled, disabled = None, set()
    if not enabled:
        return

    group = "hermes_agent.plugins"
    try:
        eps = _md.entry_points()
        # Python 3.10+ exposes .select(); older returns a dict-like mapping.
        if hasattr(eps, "select"):
            group_eps = list(eps.select(group=group))
        else:  # pragma: no cover — legacy interpreters
            group_eps = list(eps.get(group, []))  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("entry-point provider scan skipped: %s", exc)
        return

    for ep in group_eps:
        if ep.name not in enabled or ep.name in disabled:
            logger.debug(
                "entry-point provider %r skipped: not enabled in config", ep.name
            )
            continue
        try:
            loaded = ep.load()
        except Exception as exc:
            logger.warning(
                "Failed to load entry-point provider plugin %r: %s", ep.name, exc
            )
            continue
        # ``module:func`` → callable we invoke; bare ``module`` → import side
        # effect already happened during load(). Only call when it's callable
        # AND zero-arg: general plugins in this shared group expose
        # ``register(ctx)`` (requires an argument) and belong to the
        # PluginManager, not the provider registry.
        if callable(loaded):
            if _requires_arguments(loaded):
                logger.debug(
                    "entry-point %r skipped by provider scan: target requires "
                    "arguments (general plugin owned by PluginManager)",
                    ep.name,
                )
                continue
            try:
                loaded()
            except Exception as exc:
                logger.warning(
                    "Entry-point provider plugin %r raised on invocation: %s",
                    ep.name,
                    exc,
                )


def _requires_arguments(fn) -> bool:
    """True when ``fn`` cannot be called with zero arguments.

    Used to distinguish provider registration hooks (zero-arg by contract)
    from general plugin hooks (``register(ctx)``) sharing the same entry-point
    group. Unintrospectable callables (C extensions) are treated as zero-arg
    and left to the per-entry exception guard.
    """
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover — builtins/C callables
        return False
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ) and param.default is inspect.Parameter.empty:
            return True
    return False


def _discover_providers() -> None:
    """Populate the registry by importing every provider plugin.

    Order:
      1. Bundled plugins at ``<repo>/plugins/model-providers/<name>/``
      2. User plugins at ``$HERMES_HOME/plugins/model-providers/<name>/``
      3. Legacy per-file modules at ``providers/<name>.py`` (back-compat)

    Each step imports its plugins, which call ``register_provider()`` at
    module-level. Later steps win on name collision.
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    # 0. Pip-installed plugins — entry points in the ``hermes_agent.plugins``
    #    group (the same group the general PluginManager uses). The manager
    #    records model-provider manifests for introspection but deliberately
    #    does NOT import them — provider lifecycle is owned here — so without
    #    this step a ``pip install``ed provider never calls
    #    ``register_provider()`` and is never selectable.
    #
    #    Discovered FIRST, i.e. lowest precedence: because
    #    ``register_provider()`` is last-writer-wins, running this before the
    #    filesystem steps means a bundled or ``$HERMES_HOME`` profile of the
    #    same name always overrides a pip-installed one. That prevents a
    #    third-party package from silently hijacking a first-party provider
    #    name (e.g. ``openrouter``) while still letting pip packages add
    #    genuinely new providers.
    _discover_entry_point_providers()

    # 1. Bundled plugins — shipped with hermes-agent.
    if _BUNDLED_PLUGINS_DIR.is_dir():
        for child in sorted(_BUNDLED_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "bundled")

    # 2. User plugins — under $HERMES_HOME/plugins/model-providers/<name>/.
    #    These can override any bundled profile of the same name (last-writer-wins
    #    in register_provider()).
    user_dir = _user_plugins_dir()
    if user_dir is not None:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "user")

    # 3. Legacy single-file profiles at providers/<name>.py. Kept for
    #    back-compat — if someone drops a ``providers/foo.py`` into an
    #    editable install, it still works without the plugin layout.
    try:
        import pkgutil

        import providers as _pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(_pkg.__path__):
            if modname.startswith("_") or modname == "base":
                continue
            try:
                importlib.import_module(f"providers.{modname}")
            except ImportError as exc:
                logger.warning(
                    "Failed to import legacy provider module %s: %s", modname, exc
                )
    except Exception:
        pass

    # (Pip entry-point providers are discovered in step 0, before the
    # filesystem plugins, so first-party profiles always win on name
    # collision — see _discover_entry_point_providers.)
