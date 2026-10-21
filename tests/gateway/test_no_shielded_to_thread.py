"""Class guard for Prism ad88cba956ad.

``asyncio.shield(asyncio.to_thread(...))`` does not protect the off-loop work:
``to_thread`` returns a coroutine, so ``shield`` wraps it in its own Task, and
``asyncio.run()`` shutdown cancels every pending Task. With the default executor
saturated that cancel drops the still-queued job. Shield a bare
``loop.run_in_executor(...)`` future instead (shutdown_default_executor drains it).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "gateway"


def _is_attr_call(node, attr):
    return (isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == attr)


def test_no_shield_wraps_a_to_thread_coroutine():
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _is_attr_call(node, "shield") and node.args and _is_attr_call(node.args[0], "to_thread"):
                hits.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}")
    assert not hits, "shield(to_thread(...)) is cancellable at loop shutdown; use run_in_executor: " + ", ".join(hits)
