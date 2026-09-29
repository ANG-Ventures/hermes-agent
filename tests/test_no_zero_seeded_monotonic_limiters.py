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


def _monotonic_aliases(tree: ast.AST) -> frozenset[str]:
    """Names that refer to a monotonic clock function in this module.

    ``from time import monotonic as _mono`` and ``_clock = time.monotonic``
    make ``_mono()`` / ``_clock()`` monotonic calls.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name in _MONOTONIC_FUNCS:
                    aliases.add(alias.asname or alias.name)
        for target, value in _assign_pairs(node):
            if isinstance(target, ast.Name) and isinstance(value, (ast.Name, ast.Attribute)):
                if _call_name(value) in _MONOTONIC_FUNCS:
                    aliases.add(target.id)
    return frozenset(aliases)


def _is_monotonic_call(node: ast.expr, aliases: frozenset[str] = frozenset()) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id in aliases:
        return True
    # ``int(time.monotonic())`` is still an absolute boot-origin timestamp.
    # Only a DIRECT monotonic argument counts: ``int(time.monotonic() - t0)``
    # is a relative elapsed value and must not be treated as absolute.
    if _call_name(func) in _NUMERIC_WRAPPERS and node.args:
        return _is_monotonic_call(node.args[0], aliases)
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


def _is_zero_default(node: ast.expr | None) -> bool:
    """``0`` / ``0.0``, or a missing-key default ``x.get(key, 0)``.

    ``last = stamps.get(chat_id, 0.0)`` seeds a per-key limiter exactly like a
    literal: the first key reads as "attempted at boot".
    """
    if _is_zero_literal(node):
        return True
    # ``stamps.get(k) or 0.0``
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _is_zero_literal(node.values[-1])
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node.func)
    # ``stamps.get(k, 0.0)`` / ``stamps.setdefault(k, 0.0)``
    if isinstance(node.func, ast.Attribute) and name in {"get", "setdefault"}:
        return len(node.args) == 2 and _is_zero_literal(node.args[1])
    # ``getattr(self, "_last", 0.0)``
    if isinstance(node.func, ast.Name) and name == "getattr":
        return len(node.args) == 3 and _is_zero_literal(node.args[2])
    return False


class _Scope:
    def __init__(self, node: ast.AST, parent: "_Scope | None", is_function: bool):
        self.node = node
        self.parent = parent
        self.is_function = is_function
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()
        self.bindings: dict[str, list[ast.expr | None]] = {}
        self.attrs: dict[str, list[ast.expr | None]] = {}  # ClassDef only
        self.item_stores: list[tuple[str, object, ast.expr]] = []

    def class_attrs(self) -> dict[str, list[ast.expr | None]]:
        """Attribute seeds of the nearest enclosing class ({} if none)."""
        scope = self
        while scope is not None:
            if isinstance(scope.node, ast.ClassDef):
                return scope.attrs
            scope = scope.parent
        return {}

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


_SELF_NAMES = {"self", "cls"}
_PRAGMA = "zero-seed-ok:"


def _target_pairs(target: ast.expr, value: ast.expr | None):
    """Yield ``(target, value)`` pairs, pairing unpacked targets element-wise.

    ``last, count = 0.0, 0`` binds ``last`` to ``0.0``. When the value can't be
    paired (``a, b = fn()``), each target is bound to ``None`` (unknown).
    """
    if isinstance(target, (ast.Tuple, ast.List)):
        elts = target.elts
        paired = (
            isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(elts)
            and not any(isinstance(e, ast.Starred) for e in elts)
        )
        for i, elt in enumerate(elts):
            if isinstance(elt, ast.Starred):
                elt = elt.value
            yield from _target_pairs(elt, value.elts[i] if paired else None)
        return
    yield target, value


def _self_attr(node: ast.expr) -> str | None:
    """``self.x`` / ``cls.x`` -> ``"x"``."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in _SELF_NAMES
    ):
        return node.attr
    return None


def _const_key(node: ast.Subscript) -> object:
    key = node.slice
    return key.value if isinstance(key, ast.Constant) else _NO_KEY


_NO_KEY = object()


def _key_seeds(values: list[ast.expr | None], key: object) -> list[ast.expr | None]:
    """Values stored under ``key`` by dict literals in ``values``."""
    out: list[ast.expr | None] = []
    for v in values:
        if isinstance(v, ast.Dict):
            for k, item in zip(v.keys, v.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    out.append(item)
    return out


def _assign_pairs(child: ast.AST):
    if isinstance(child, ast.Assign):
        for target in child.targets:
            yield from _target_pairs(target, child.value)
    elif isinstance(child, ast.AnnAssign):
        yield child.target, child.value


def _class_nodes(cls: ast.ClassDef):
    """Nodes of ``cls`` incl. its methods, but not nested classes (whose
    ``self`` is a different object)."""
    stack = list(ast.iter_child_nodes(cls))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.ClassDef):
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Parameters are local bindings that shadow module-level names.
            # Their defaults are NOT treated as seeds: a zero parameter default
            # is overwhelmingly a duration/offset (``ack_age=0.0``), not a
            # boot-origin timestamp.
            args = node.args
            for arg in (
                args.posonlyargs + args.args + args.kwonlyargs
                + [a for a in (args.vararg, args.kwarg) if a is not None]
            ):
                scope.bindings.setdefault(arg.arg, []).append(None)
        for child in _own_nodes(node):
            if isinstance(child, ast.Global):
                scope.globals.update(child.names)
            elif isinstance(child, ast.Nonlocal):
                scope.nonlocals.update(child.names)
            else:
                for target, value in _assign_pairs(child):
                    if isinstance(target, ast.Name):
                        scope.bindings.setdefault(target.id, []).append(value)
                    elif (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and _const_key(target) is not _NO_KEY
                        and value is not None
                    ):
                        # ``cache["ts"] = 0.0`` seeds key "ts" of ``cache``
                        # (resolved to its owning scope below, like a load).
                        scope.item_stores.append((target.value.id, _const_key(target), value))
            if isinstance(child, _SCOPE_NODES):
                visit(child, scope)

    visit(tree, None)
    # Instance/class state: ``self.x = ...`` anywhere in the class, plus
    # class-body ``x = ...``, all readable as ``self.x``.
    for scope in scopes:
        if isinstance(scope.node, ast.ClassDef):
            attrs: dict[str, list[ast.expr | None]] = {
                k: list(v) for k, v in scope.bindings.items()
            }
            for sub in _class_nodes(scope.node):
                for target, value in _assign_pairs(sub):
                    attr = _self_attr(target)
                    if attr is not None:
                        attrs.setdefault(attr, []).append(value)
                    elif (
                        isinstance(target, ast.Subscript)
                        and _self_attr(target.value) is not None
                        and _const_key(target) is not _NO_KEY
                        and value is not None
                    ):
                        # ``self.cache["ts"] = 0.0``
                        attrs.setdefault(_self_attr(target.value), []).append(
                            ast.Dict(keys=[ast.Constant(_const_key(target))], values=[value])
                        )
            scope.attrs = attrs
    # Assignments under ``global``/``nonlocal`` bind in the resolved scope.
    for scope in scopes:
        for name in scope.globals | scope.nonlocals:
            values = scope.bindings.pop(name, None)
            if values:
                target = scope.resolve(name)
                if target is not None:
                    target.bindings.setdefault(name, []).extend(values)
    for scope in scopes:
        for name, key, value in scope.item_stores:
            owner = scope.resolve(name) or scope
            owner.bindings.setdefault(name, []).append(
                ast.Dict(keys=[ast.Constant(key)], values=[value])
            )
    return scopes


def find_zero_seeded_monotonic_diffs(source: str, filename: str = "<src>") -> list[str]:
    """Return ``file:line: name`` for each ``<monotonic> - <0-seeded name>``."""
    with warnings.catch_warnings():
        # Scanning the whole tree compiles files with invalid escape sequences.
        warnings.simplefilter("ignore")
        tree = ast.parse(source, filename=filename)
    hits: list[str] = []
    lines = source.splitlines()
    aliases = _monotonic_aliases(tree)
    for scope in _build_scopes(tree):
        for node in _own_nodes(scope.node):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)):
                continue
            # Reviewed escape for a guarded 0 sentinel (``if x == 0: x = now``
            # ``elif now - x >= T``): ``# zero-seed-ok: <reason>`` on the line.
            if _PRAGMA in lines[node.lineno - 1]:
                continue
            right = node.right
            right_attr = _self_attr(right)
            right_item = (
                isinstance(right, ast.Subscript)
                and _const_key(right) is not _NO_KEY
                and (isinstance(right.value, ast.Name) or _self_attr(right.value) is not None)
            )
            if (
                not isinstance(right, ast.Name)
                and right_attr is None
                and not right_item
                and not _is_zero_default(right)
            ):
                continue
            left = node.left
            left_attr = _self_attr(left)
            if left_attr is not None:
                left_is_mono = any(
                    v is not None and _is_monotonic_call(v, aliases)
                    for v in scope.class_attrs().get(left_attr, [])
                )
            elif isinstance(left, ast.Name):
                left_scope = scope.resolve(left.id)
                left_is_mono = left_scope is not None and any(
                    v is not None and _is_monotonic_call(v, aliases)
                    for v in left_scope.bindings.get(left.id, [])
                )
            else:
                left_is_mono = _is_monotonic_call(left, aliases)
            if not left_is_mono:
                continue
            if right_item:
                # ``now - cache["ts"]`` where ``cache = {"ts": 0.0, ...}`` or
                # ``cache["ts"] = 0.0`` (also ``self.cache[...]``).
                base = right.value
                if isinstance(base, ast.Name):
                    owner = scope.resolve(base.id)
                    values = owner.bindings.get(base.id, []) if owner else []
                else:
                    values = scope.class_attrs().get(_self_attr(base), [])
                if any(_is_zero_default(v) for v in _key_seeds(values, _const_key(right))):
                    hits.append(f"{filename}:{node.lineno}: {ast.unparse(right)}")
                continue
            if right_attr is not None:
                if any(_is_zero_default(v) for v in scope.class_attrs().get(right_attr, [])):
                    hits.append(f"{filename}:{node.lineno}: {ast.unparse(right)}")
                continue
            if not isinstance(right, ast.Name):
                # inline ``<monotonic> - stamps.get(key, 0.0)`` (or getattr/``or 0``)
                hits.append(f"{filename}:{node.lineno}: {ast.unparse(right)}")
                continue
            right_scope = scope.resolve(right.id)
            if right_scope is None:
                continue
            if any(_is_zero_default(v) for v in right_scope.bindings.get(right.id, [])):
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
            "import time\ndef f(stamps, k):\n    now = time.monotonic()\n"
            "    last = stamps.get(k, 0.0)\n    if now - last < 30:\n        return False\n",
            ["last"],
            id="dict-get-zero-default-bound",
        ),
        pytest.param(
            "import time\ndef f(stamps, k):\n    now = time.monotonic()\n"
            "    return now - stamps.get(k, 0.0) < 3600\n",
            ["stamps.get(k, 0.0)"],
            id="dict-get-zero-default-inline",
        ),
        pytest.param(
            "import time\ndef f(stamps, k):\n    now = time.time()\n"
            "    return now - stamps.get(k, 0.0) < 3600\n",
            [],
            id="dict-get-wall-clock-is-safe",
        ),
        pytest.param(
            "import time\nlast = 0.0\ndef f(last):\n    return time.monotonic() - last\n",
            [],
            id="parameter-shadows-module-zero-seed",
        ),
        pytest.param(
            "import time\ndef f(ack_age=0.0):\n    return time.perf_counter() - ack_age\n",
            [],
            id="parameter-default-is-an-offset-not-a-seed",
        ),
        pytest.param(
            "import time\ndef f(self, stamps, k):\n    now = time.monotonic()\n"
            "    a = getattr(self, '_last', 0.0)\n    b = stamps.setdefault(k, 0)\n"
            "    c = stamps.get(k) or 0.0\n    return now - a, now - b, now - c\n",
            ["a", "b", "c"],
            id="getattr-setdefault-or-zero-defaults",
        ),
        pytest.param(
            "import time\nclass L:\n    def __init__(self):\n        self.last_attempt = 0.0\n"
            "    def tick(self):\n        if time.monotonic() - self.last_attempt < 60:\n"
            "            return False\n        self.last_attempt = time.monotonic()\n",
            ["self.last_attempt"],
            id="instance-attribute-seed",
        ),
        pytest.param(
            "import time\nclass L:\n    last = 0\n"
            "    def tick(self):\n        now = time.monotonic()\n        return now - self.last\n",
            ["self.last"],
            id="class-attribute-seed",
        ),
        pytest.param(
            "import time\nclass L:\n    def __init__(self):\n        self.last = float('-inf')\n"
            "    def tick(self):\n        return time.monotonic() - self.last\n",
            [],
            id="instance-attribute-inf-is-safe",
        ),
        pytest.param(
            "import time\nclass L:\n    def __init__(self):\n        self.last = 0\n"
            "    def tick(self):\n        return time.time() - self.last\n",
            [],
            id="instance-attribute-wall-clock-is-safe",
        ),
        pytest.param(
            "import time\ndef f():\n    last, count = 0.0, 0\n"
            "    if time.monotonic() - last > 5:\n        last = time.monotonic()\n",
            ["last"],
            id="tuple-unpacked-seed",
        ),
        pytest.param(
            "import time\nclass L:\n    def __init__(self):\n        self.a, self.b = 0.0, 1\n"
            "    def tick(self):\n        return time.monotonic() - self.a\n",
            ["self.a"],
            id="tuple-unpacked-attribute-seed",
        ),
        pytest.param(
            "import time\ndef f():\n    start = 0.0\n    now = time.monotonic()\n"
            "    if start == 0.0:\n        start = now\n"
            "    elif now - start >= 1:  # zero-seed-ok: guarded sentinel\n        pass\n",
            [],
            id="pragma-guarded-sentinel",
        ),
        pytest.param(
            "import time\n_cache = {'timestamp': 0.0, 'result': False}\n"
            "def f():\n    now = time.monotonic()\n"
            "    if now - _cache['timestamp'] < 300:\n        return _cache['result']\n",
            ["_cache['timestamp']"],
            id="dict-literal-key-seed",
        ),
        pytest.param(
            "import time\n_cache = {}\n_cache['ts'] = 0\n"
            "def f():\n    return time.monotonic() - _cache['ts']\n",
            ["_cache['ts']"],
            id="subscript-store-seed",
        ),
        pytest.param(
            "import time\nclass C:\n    def __init__(self):\n        self.c = {'ts': 0.0}\n"
            "    def f(self):\n        return time.monotonic() - self.c['ts']\n",
            ["self.c['ts']"],
            id="self-dict-literal-key-seed",
        ),
        pytest.param(
            "import time\n_cache = {'timestamp': float('-inf'), 'n': 0}\n"
            "def f():\n    return time.monotonic() - _cache['timestamp']\n",
            [],
            id="dict-literal-inf-and-other-key-zero-is-safe",
        ),
        pytest.param(
            "from time import monotonic as _mono\nlast = 0.0\n"
            "def f():\n    return _mono() - last\n",
            ["last"],
            id="import-as-alias",
        ),
        pytest.param(
            "import time\n_clock = time.monotonic\n"
            "def f():\n    last = 0\n    now = _clock()\n    return now - last\n",
            ["last"],
            id="assigned-function-alias",
        ),
        pytest.param(
            "import time\nclass C:\n    def __init__(self):\n        self.c = {}\n"
            "        self.c['ts'] = 0.0\n"
            "    def f(self):\n        return time.monotonic() - self.c['ts']\n",
            ["self.c['ts']"],
            id="self-subscript-store-seed",
        ),
        pytest.param(
            "import time\nclass Outer:\n"
            "    class Inner:\n        def __init__(self):\n            self.last = 0.0\n"
            "    def __init__(self):\n        self.last = float('-inf')\n"
            "    def f(self):\n        return time.monotonic() - self.last\n",
            [],
            id="nested-class-self-not-conflated",
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
    assert sorted(h.split(": ", 1)[1] for h in hits) == sorted(expected)
