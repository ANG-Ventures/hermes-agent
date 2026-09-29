"""No rate limiter may seed a monotonic-clock timestamp with ``0`` / ``0.0``.

``time.monotonic()`` (and ``perf_counter()``, and asyncio's ``loop.time()``,
which is ``time.monotonic()`` by default) has an *arbitrary* origin. On Linux it
is host boot. So a limiter shaped like::

    _last_attempt = 0.0
    ...
    now = time.monotonic()
    if now - _last_attempt < INTERVAL:   # "attempted recently" -> skip
        return

treats "never attempted" as "attempted at boot". On a long-lived host that is
indistinguishable from "long ago", so it works everywhere a developer looks. On
a freshly booted host or a CI microVM, where uptime is still below ``INTERVAL``,
the seed reads as "attempted just now" and the first action is silently dropped
or delayed. That shipped: ``tools/kanban_tools.py`` seeded
``_auto_heartbeat_last_attempt`` and ``_comment_poll_last_attempt`` with
``0.0``; the first worker heartbeat was dropped on a fresh CI VM and ejected
merge groups (``test_kanban_progress_stall``: ``assert None == 1000``,
hermes-agent#1487). A sweep found the same seed in the gateway, TUI gateway,
mem-trim and STT idle-unload paths.

The correct seed is ``float("-inf")``: ``now - (-inf)`` is ``+inf``, which is
"never" on every host regardless of uptime. ``time.time()`` is NOT in scope:
its epoch is 1970, so a 0 seed really is "long ago".

Rule (AST, per scope with Python name resolution incl. ``global`` /
``nonlocal``): flag ``<monotonic-expr> - name`` where ``name`` resolves to a
binding assigned the literal ``0`` or ``0.0`` and ``<monotonic-expr>`` is a
direct monotonic call or a name bound from one. Relative counters such as
``elapsed = int(time.monotonic() - started)`` compared against a 0-seeded
``_last`` are fine (both sides share the same origin) and are not flagged,
because ``elapsed`` is not bound directly from a monotonic call.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".worktrees",
}

_MONOTONIC_FUNCS = {"monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"}
_LOOP_GETTERS = {"get_running_loop", "get_event_loop"}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_NUMERIC_WRAPPERS = {"int", "float", "round", "floor", "ceil", "trunc"}


def _is_monotonic_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # ``int(time.monotonic())`` is still an absolute boot-origin timestamp.
    # Only a DIRECT monotonic argument counts: ``int(time.monotonic() - t0)``
    # is a relative elapsed value and must not be treated as absolute.
    if _call_name(func) in _NUMERIC_WRAPPERS and node.args:
        return _is_monotonic_call(node.args[0])
    if _call_name(func) in _MONOTONIC_FUNCS:
        return True
    # asyncio: ``loop.time()`` / ``asyncio.get_running_loop().time()``
    if isinstance(func, ast.Attribute) and func.attr == "time":
        owner = func.value
        if isinstance(owner, ast.Call) and _call_name(owner.func) in _LOOP_GETTERS:
            return True
        owner_name = _call_name(owner)
        if owner_name is not None and owner_name.lower().endswith("loop"):
            return True
    return False


def _is_zero_literal(node: ast.expr | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and type(node.value) in (int, float)
        and node.value == 0
    )


class _Scope:
    def __init__(self, node: ast.AST, parent: "_Scope | None", is_function: bool):
        self.node = node
        self.parent = parent
        self.is_function = is_function
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()
        self.bindings: dict[str, list[ast.expr | None]] = {}

    @property
    def module(self) -> "_Scope":
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def resolve(self, name: str) -> "_Scope | None":
        """Return the scope whose binding ``name`` refers to from here."""
        if name in self.globals:
            return self.module
        if name in self.nonlocals:
            scope = self.parent
            while scope is not None:
                if scope.is_function and name in scope.bindings:
                    return scope
                scope = scope.parent
            return None
        if name in self.bindings:
            return self
        # Free variable: enclosing *function* scopes, then the module. Class
        # bodies are not visible to nested functions.
        scope = self.parent
        while scope is not None:
            if scope.parent is None or scope.is_function:
                if name in scope.bindings:
                    return scope
            scope = scope.parent
        return None


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _own_nodes(scope_node: ast.AST):
    """Yield nodes belonging to ``scope_node``'s own scope (not nested defs)."""
    stack = list(ast.iter_child_nodes(scope_node))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPE_NODES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _build_scopes(tree: ast.Module) -> list[_Scope]:
    scopes: list[_Scope] = []

    def visit(node: ast.AST, parent: _Scope | None) -> None:
        scope = _Scope(
            node,
            parent,
            is_function=isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)),
        )
        scopes.append(scope)
        for child in _own_nodes(node):
            if isinstance(child, ast.Global):
                scope.globals.update(child.names)
            elif isinstance(child, ast.Nonlocal):
                scope.nonlocals.update(child.names)
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        scope.bindings.setdefault(target.id, []).append(child.value)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                scope.bindings.setdefault(child.target.id, []).append(child.value)
            if isinstance(child, _SCOPE_NODES):
                visit(child, scope)

    visit(tree, None)
    # Assignments under ``global``/``nonlocal`` bind in the resolved scope.
    for scope in scopes:
        for name in scope.globals | scope.nonlocals:
            values = scope.bindings.pop(name, None)
            if values:
                target = scope.resolve(name)
                if target is not None:
                    target.bindings.setdefault(name, []).extend(values)
    return scopes


def find_zero_seeded_monotonic_diffs(source: str, filename: str = "<src>") -> list[str]:
    """Return ``file:line: name`` for each ``<monotonic> - <0-seeded name>``."""
    with warnings.catch_warnings():
        # Scanning the whole tree compiles files with invalid escape sequences.
        warnings.simplefilter("ignore")
        tree = ast.parse(source, filename=filename)
    hits: list[str] = []
    for scope in _build_scopes(tree):
        for node in _own_nodes(scope.node):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)):
                continue
            right = node.right
            if not isinstance(right, ast.Name):
                continue
            left = node.left
            if isinstance(left, ast.Name):
                left_scope = scope.resolve(left.id)
                left_is_mono = left_scope is not None and any(
                    v is not None and _is_monotonic_call(v)
                    for v in left_scope.bindings.get(left.id, [])
                )
            else:
                left_is_mono = _is_monotonic_call(left)
            if not left_is_mono:
                continue
            right_scope = scope.resolve(right.id)
            if right_scope is None:
                continue
            if any(_is_zero_literal(v) for v in right_scope.bindings.get(right.id, [])):
                hits.append(f"{filename}:{node.lineno}: {right.id}")
    return hits


def _iter_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        yield path, rel


def test_no_zero_seeded_monotonic_limiters_in_tree():
    hits: list[str] = []
    for path, rel in _iter_python_files():
        try:
            source = path.read_text(encoding="utf-8")
            hits.extend(find_zero_seeded_monotonic_diffs(source, str(rel)))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
    assert not hits, (
        "monotonic-clock timestamps seeded with 0/0.0 read as 'just now' on a "
        "freshly booted host (uptime < interval) and drop the first action; "
        "seed with float('-inf') instead (see module docstring):\n  "
        + "\n  ".join(sorted(hits))
    )


# The pre-#1487 tools/kanban_tools.py shape, reduced. RED fixture.
_PRE_FIX_KANBAN_TOOLS = '''
import time as _time
_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0

def heartbeat_current_worker_from_env(progress_at=None):
    global _auto_heartbeat_last_attempt
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    return True
'''


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(_PRE_FIX_KANBAN_TOOLS, ["_auto_heartbeat_last_attempt"], id="pre-fix-kanban-tools"),
        pytest.param(
            _PRE_FIX_KANBAN_TOOLS.replace(": float = 0.0", ': float = float("-inf")'), [], id="post-fix-kanban-tools"
        ),
        pytest.param(
            "import time\nlast = 0\n"
            "def f():\n    global last\n    if time.monotonic() - last > 5:\n        last = time.monotonic()\n",
            ["last"],
            id="direct-monotonic-call-global",
        ),
        pytest.param(
            "import asyncio\n"
            "async def drain():\n    last = 0.0\n"
            "    def tick():\n        nonlocal last\n"
            "        now = asyncio.get_running_loop().time()\n"
            "        if now - last >= 1.0:\n            last = now\n",
            ["last"],
            id="nonlocal-loop-time",
        ),
        pytest.param(
            "import time\ndef f():\n    last = 0\n    now = int(time.time())\n"
            "    if now - last >= 300:\n        last = now\n",
            [],
            id="wall-clock-is-safe",
        ),
        pytest.param(
            "import time\ndef f():\n    last = 0\n    now = int(time.monotonic())\n"
            "    if now - last >= 30:\n        last = now\n",
            ["last"],
            id="int-wrapped-monotonic",
        ),
        pytest.param(
            "import math, time\ndef f():\n    last = 0.0\n"
            "    if round(time.monotonic(), 1) - last >= 30:\n        last = 1\n"
            "    if math.floor(time.monotonic()) - last >= 30:\n        last = 1\n",
            ["last", "last"],
            id="round-and-floor-wrapped-monotonic",
        ),
        pytest.param(
            "import time\ndef f(started):\n    last = 0\n"
            "    elapsed = int(time.monotonic() - started)\n"
            "    if elapsed - last >= 30:\n        last = elapsed\n",
            [],
            id="relative-elapsed-is-safe",
        ),
        pytest.param(
            "import time\nnow = 0\ndef g():\n    now = time.time()\n    return now\n"
            "def f():\n    last = 0.0\n    t = time.monotonic()\n    return now - last\n",
            [],
            id="shadowed-name-in-other-function-not-conflated",
        ),
    ],
)
def test_detector_shapes(source, expected):
    hits = find_zero_seeded_monotonic_diffs(source)
    assert [h.rsplit(": ", 1)[1] for h in hits] == expected
