"""Source contract: heavy synchronous work never runs directly on the gateway
event loop.

Incident 2026-09-24 (Apollo): after the LCM freeze-#3 fix (#966) cut the
context-engine load from 13 min to 1-9 s, the Discord websocket STILL dropped
7x in 50 min.  ``PHASE=event_loop_blocked`` named
``plugins/context_engine/__init__.py _load_engine_from_dir`` (20-60 s) and
``gateway/run.py _check_unavailable_skill`` — both reached from ``async def``
bodies: session hygiene built its ``AIAgent`` inline (the engine load waits on
the process-global ``_LOAD_LOCK`` held by concurrent worker turns), ``/compress``
did the same, and an unknown ``/command`` walked 900+ SKILL.md files with
``rglob`` + ``read_text``.  Discord's heartbeat ACK window is ~41 s; any of
these crossing it closes the socket, and every reconnect re-registers 900
skills and re-injects backfill while Ace's turns land as stubs.

The fix hands each of those calls to ``asyncio.to_thread``.  This test locks
the class, not the three sites: no ``async def`` in the gateway package may
call ``AIAgent(...)`` or ``_check_unavailable_skill(...)`` directly.  A call
inside a nested ``def``/``lambda`` (the ``to_thread`` payload) is fine — the
walk stops at nested function scopes.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FILES = [REPO / "gateway" / "run.py", REPO / "gateway" / "slash_commands.py"]
# Constructors / helpers measured blocking the loop.  Extend when a new
# ``PHASE=event_loop_blocked`` site is root-caused to a direct call.
BLOCKING_CALLEES = {"AIAgent", "_check_unavailable_skill"}


def _callee_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _direct_calls_in_async_bodies(tree: ast.AST):
    """Yield (async_fn_name, lineno, callee) for direct calls in async bodies.

    Nested ``def``/``async def``/``lambda`` scopes are not descended: a call
    inside them is executed by whoever invokes that closure (to_thread), not by
    the coroutine itself.
    """
    class Walker(ast.NodeVisitor):
        def __init__(self):
            self.hits = []
            self._async_stack = []

        def visit_AsyncFunctionDef(self, node):
            self._async_stack.append(node.name)
            for stmt in node.body:
                self._scan(stmt)
            self._async_stack.pop()

        def _scan(self, node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if isinstance(node, ast.AsyncFunctionDef):
                    self.visit_AsyncFunctionDef(node)
                return
            if isinstance(node, ast.Call):
                name = _callee_name(node)
                if name in BLOCKING_CALLEES:
                    self.hits.append((self._async_stack[-1], node.lineno, name))
            for child in ast.iter_child_nodes(node):
                self._scan(child)

    w = Walker()
    w.visit(tree)
    return w.hits


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_no_blocking_construction_directly_in_async_def(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = _direct_calls_in_async_bodies(tree)
    assert not hits, (
        f"{path.name}: heavy sync call(s) directly on the event loop — wrap in "
        f"asyncio.to_thread (see module docstring): "
        + ", ".join(f"{fn}:{ln} {callee}(...)" for fn, ln, callee in hits)
    )


def test_walker_catches_a_direct_call_and_ignores_to_thread_payload():
    """Negative control: the contract must actually fire."""
    bad = ast.parse(
        "async def h():\n"
        "    a = AIAgent(model='x')\n"
        "    b = await asyncio.to_thread(lambda: AIAgent(model='y'))\n"
        "    def inner():\n"
        "        return _check_unavailable_skill('z')\n"
        "    return a, b, inner\n"
    )
    hits = _direct_calls_in_async_bodies(bad)
    assert hits == [("h", 2, "AIAgent")]
