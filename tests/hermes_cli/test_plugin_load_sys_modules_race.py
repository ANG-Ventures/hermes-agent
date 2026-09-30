"""Plugin loads must survive a concurrent import (t_99b95957).

daedalus 2026-09-29 18:24:52: ``Failed to load plugin 'blackbox': dictionary
changed size during iteration``. The loader's stale-module eviction iterated
``sys.modules`` live while another thread imported, so ``register()`` never ran
and no ``on_session_end`` hook existed in that worker. Core still wrote the
turn's ``turn_api_calls`` (it imports ``plugins.blackbox`` directly), so the
turn ended normally with 7 calls and no ``turns`` row.
"""
from __future__ import annotations

import ast
import sys
import threading
import types
from pathlib import Path

import pytest

from hermes_cli.plugins import PluginManager, PluginManifest

REPO = Path(__file__).resolve().parents[2]


def test_directory_plugin_load_survives_concurrent_sys_modules_churn(tmp_path):
    plugin_dir = tmp_path / "raceplug"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("def register(ctx):\n    pass\n")
    manifest = PluginManifest(
        name="raceplug", source="user", path=str(plugin_dir), key="raceplug",
    )
    manager = PluginManager()

    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            sys.modules[f"_t99b_churn_{i % 400}"] = types.ModuleType("x")
            sys.modules.pop(f"_t99b_churn_{(i + 200) % 400}", None)
            i += 1

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    worker = threading.Thread(target=churn, daemon=True)
    worker.start()
    errors = []
    try:
        for n in range(200):
            try:
                manager._load_directory_module(
                    manifest, module_name=f"_t99b_plugins.raceplug_{n % 2}",
                )
            except RuntimeError as exc:
                errors.append(str(exc))
    finally:
        stop.set()
        worker.join(timeout=5)
        sys.setswitchinterval(old_interval)
        for name in [n for n in list(sys.modules) if n.startswith(("_t99b_churn_", "_t99b_plugins"))]:
            sys.modules.pop(name, None)

    assert errors == []


def _iterates_sys_modules_live(node: ast.AST) -> bool:
    """``sys.modules`` / ``sys.modules.keys|items|values()`` used as a loop iterable."""
    target = node
    if (
        isinstance(target, ast.Call)
        and isinstance(target.func, ast.Attribute)
        and target.func.attr in {"keys", "items", "values"}
        and not target.args
    ):
        target = target.func.value
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "modules"
        and isinstance(target.value, ast.Name)
        and target.value.id == "sys"
    )


def _sources():
    skip = {"tests", "node_modules", "venv", ".venv", ".git", "website", "web"}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if rel.parts and rel.parts[0] in skip:
            continue
        yield path, rel


def test_no_live_iteration_of_sys_modules_outside_tests():
    """Class guard: iterate a snapshot (``list(sys.modules)``), never the live dict."""
    hits = []
    for path, rel in _sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            iters = []
            if isinstance(node, (ast.For, ast.AsyncFor)):
                iters.append(node.iter)
            elif isinstance(node, ast.comprehension):
                iters.append(node.iter)
            for it in iters:
                if _iterates_sys_modules_live(it):
                    hits.append(f"{rel}:{it.lineno}")
    assert hits == [], "iterate list(sys.modules) instead: " + ", ".join(hits)


@pytest.mark.parametrize("src, flagged", [
    ("[n for n in sys.modules]", True),
    ("{n for n in sys.modules.keys()}", True),
    ("for k, v in sys.modules.items(): pass", True),
    ("[n for n in list(sys.modules)]", False),
    ("'x' in sys.modules", False),
])
def test_guard_detects_the_shape(src, flagged):
    tree = ast.parse(src)
    found = any(
        _iterates_sys_modules_live(n.iter)
        for n in ast.walk(tree)
        if isinstance(n, (ast.For, ast.comprehension))
    )
    assert found is flagged
