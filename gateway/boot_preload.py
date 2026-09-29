"""Import every first-party module at gateway boot so the process runs ONE code snapshot.

A gateway is long-lived and its ``sys.modules`` is frozen at boot, but a module
imported lazily (inside a function body) is read from DISK at first use.  If the
checkout fast-forwards in between (restarts are batched, so that window is
normal), the lazily-imported module comes from the NEW commit and binds against
OLD dependencies already cached in ``sys.modules`` -> ``ImportError: cannot
import name ...`` on every call through that path (Aegis, 2026-09-28:
``agent.chat_completion_helpers`` new, ``agent.fork_ext.relay_headers`` old).

``preload_first_party_modules()`` runs at the end of gateway startup, while the
tree on disk still matches what booted, and imports every module under the
first-party roots.  Every later lazy import then hits ``sys.modules`` instead of
disk.  It is deliberately a deterministic walk, not a curated list: a new lazy
import site is covered the moment its target lives under a walked root.
``tests/gateway/test_boot_preload.py`` pins that with an AST lint.

Best-effort: a module that fails to import is logged at WARNING and skipped;
boot never aborts because of the preload.  One exception to the WARNING: a
``ModuleNotFoundError`` for a module OUTSIDE the first-party tree (an optional
extra such as ``acp`` for ``acp_adapter``, absent from the runtime venv) is a
reasoned skip, not breakage.  It is counted under ``skipped``, not ``failed``,
and logged once at INFO per (first-party package, missing dependency).
"""

from __future__ import annotations

import importlib
import logging

import sys
import time
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# First-party packages (mirrors [tool.setuptools.packages.find] in pyproject.toml).
PRELOAD_PACKAGES: tuple[str, ...] = (
    "agent",
    "tools",
    "hermes_cli",
    "gateway",
    "tui_gateway",
    "cron",
    "acp_adapter",
    "plugins",
    "providers",
)

# Modules whose import has side effects that are unsafe inside a running
# gateway.  Every entry needs its reason.  Always skipped as well (see
# ``is_excluded``): ``*.__main__`` (importing one runs that package's CLI) and
# in-tree test modules (``tests``/``test_*``/``conftest``), which are not runtime
# code and import their siblings through pytest's sys.path.
EXCLUDED_MODULES: dict[str, str] = {
    "tui_gateway.server": (
        "TUI JSON-RPC backend: at import it rebinds sys.stdout to sys.stderr, installs "
        "signal handlers and starts its idle-reaper threads"
    ),
    "tui_gateway.entry": (
        "TUI process entry point: at import it sets SIGINT to SIG_IGN, installs "
        "SIGTERM/SIGHUP/SIGPIPE handlers and imports tui_gateway.server"
    ),
    "tui_gateway.ws": "imports tui_gateway.server at module level (see above)",
    "plugins.memory.mem0.live_e2e_capture": (
        "ad-hoc live E2E script: inserts a worktree path into sys.path at import, "
        "which makes its flat modules importable under a second name"
    ),
}


def is_excluded(name: str) -> bool:
    if name in EXCLUDED_MODULES:
        return True
    parts = name.split(".")
    leaf = parts[-1]
    return (
        leaf == "__main__"
        or leaf == "conftest"
        or leaf.startswith("test_")
        or any(p in ("tests", "test") for p in parts)
    )


def top_level_modules(project_root: Path = _PROJECT_ROOT) -> list[str]:
    """First-party single-file modules, read from ``[tool.setuptools] py-modules``.

    Sealed/wheel installs have no pyproject next to the code; they return ``[]``
    (their tree is not a git checkout that can fast-forward underneath us).
    """
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        import tomllib

        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
        mods = data.get("tool", {}).get("setuptools", {}).get("py-modules", [])
        return [m for m in mods if isinstance(m, str)]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("boot preload: could not read py-modules from %s: %s", pyproject, exc)
        return []


_SKIP_DIRS = frozenset({"__pycache__", "node_modules"})


def _has_python(directory: Path) -> bool:
    return any(
        p.suffix == ".py" and not _SKIP_DIRS.intersection(p.relative_to(directory).parts)
        for p in directory.rglob("*.py")
    )


def _walk_package(pkg_dir: Path, prefix: str) -> Iterator[str]:
    """Filesystem walk of one package directory, sorted for determinism.

    Unlike ``pkgutil.walk_packages`` it never imports a package just to list
    it (so an excluded ``tests`` package is never executed), and it descends
    into namespace packages (directories without ``__init__.py``, e.g.
    ``agent/fork_ext`` and ``plugins/platforms``), which ``pkgutil`` skips.
    """
    for entry in sorted(pkg_dir.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        if entry.is_file():
            if entry.suffix != ".py" or entry.name == "__init__.py":
                continue
            name = prefix + entry.stem
            if "." not in entry.stem and not is_excluded(name):
                yield name
        elif entry.is_dir() and "." not in entry.name:
            name = prefix + entry.name
            if is_excluded(name):
                continue
            if (entry / "__init__.py").is_file() or _has_python(entry):
                yield name
                yield from _walk_package(entry, name + ".")


def iter_preload_names(project_root: Path = _PROJECT_ROOT) -> Iterator[str]:
    """Every first-party module name the preload covers, excluded ones skipped."""
    for mod in top_level_modules(project_root):
        if not is_excluded(mod):
            yield mod
    for pkg_name in PRELOAD_PACKAGES:
        pkg_dir = project_root / pkg_name
        if not (pkg_dir / "__init__.py").is_file():
            continue
        yield pkg_name
        yield from _walk_package(pkg_dir, pkg_name + ".")


def _missing_third_party_root(exc: BaseException, root_names: set[str], project_root: Path) -> str | None:
    """Root of the missing module when ``exc`` is an absent THIRD-PARTY dependency, else None.

    A missing first-party module (deleted or renamed in-tree) is real breakage
    and stays a failure.
    """
    if not isinstance(exc, ModuleNotFoundError) or not exc.name:
        return None
    root = exc.name.split(".", 1)[0]
    if root in root_names or (project_root / root).exists() or (project_root / f"{root}.py").exists():
        return None
    return root


def preload_first_party_modules(project_root: Path = _PROJECT_ROOT) -> dict:
    """Import every first-party module not yet in ``sys.modules``.

    Returns ``{"modules": <newly imported first-party>, "failed": <count>,
    "errors": {<module>: <exception type>}, "skipped": {<module>: <missing
    optional dependency>}, "elapsed_ms": <int>}``.
    """
    started = time.perf_counter()
    before = set(sys.modules)
    root_names = set(PRELOAD_PACKAGES) | set(top_level_modules(project_root))
    errors: dict[str, str] = {}
    skipped: dict[str, str] = {}  # module -> missing third-party root
    for name in iter_preload_names(project_root):
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - SystemExit from a stray CLI must not end boot
            if isinstance(exc, KeyboardInterrupt):
                raise
            missing = _missing_third_party_root(exc, root_names, project_root)
            if missing:
                skipped[name] = missing
                continue
            errors[name] = type(exc).__name__
            logger.warning("boot preload: import of %s failed: %s: %s", name, type(exc).__name__, exc)
    by_pkg: dict[tuple[str, str], list[str]] = {}
    for name, missing in skipped.items():
        by_pkg.setdefault((name.split(".", 1)[0], missing), []).append(name)
    for (pkg, missing), names in sorted(by_pkg.items()):
        logger.info(
            "boot preload: skipped %s (%d module(s)): optional dependency %r is not installed",
            pkg, len(names), missing,
        )
    loaded = [m for m in set(sys.modules) - before if m.split(".", 1)[0] in root_names]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "PHASE=boot_preload modules=%d failed=%d skipped=%d elapsed_ms=%d",
        len(loaded), len(errors), len(skipped), elapsed_ms,
    )
    try:
        from gateway.code_skew import record_preload

        record_preload(len(loaded))
    except Exception:
        pass
    return {
        "modules": len(loaded),
        "failed": len(errors),
        "errors": errors,
        "skipped": skipped,
        "elapsed_ms": elapsed_ms,
    }
