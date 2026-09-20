"""No statement may be a bare tuple whose first element is an assertion.

Dropping the `assert` keyword in front of an assertion that carries a message
turns the whole line into a tuple *expression*:

    sync.assert_not_awaited(), "a reconnect must not sync"   # ← evaluates, discards

Python builds a 2-tuple and throws it away. For a `Mock.assert_*()` call the
call still runs, so an actual mismatch would still raise — but for the far more
common `assert_not_*` / "must not have happened" family the mock call returns
`None` on success and the line is *inert as a contract*: it asserts nothing the
reader thinks it asserts, and nothing at all when the expression is a bare
comparison (`x == y, "msg"`). The test goes green while its primary contract is
unchecked.

This actually shipped: `tests/gateway/test_discord_command_sync_recovery.py:339`
was `sync.assert_not_awaited(), "..."` — caught by a human reviewer
(@Enough1122 on #117299), not by CI. An AST sweep then found three more in the
same shape, in cron and gateway tests.

Why this guard and not a linter: ruff/flake8 **B018 (useless-expression) does
NOT catch it.** B018 deliberately exempts any expression containing a `Call`,
because calls may have side effects — and every one of these four sites is a
call. Measured on this tree: B018 flags `x, 2` but is silent on
`m.assert_called_once(), "msg"`. So the whole bug class is invisible to the
lint config, and a regex over `assert_` both misses the non-mock shapes and
over-matches the 5000+ legitimate bare `m.assert_called()` statements that are
NOT tuples. The discriminator is the tuple, and that needs an AST.

The rule is narrow on purpose: only a *statement-level* `ast.Expr` whose value
is an `ast.Tuple`. A bare `m.assert_called()` on its own line is the correct
idiom and is untouched.
"""

from __future__ import annotations

import ast
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
}


def _assertion_shape(node: ast.expr) -> str | None:
    """Name the assertion-looking shape of ``node``, or None.

    These are the expression shapes that are meaningful as the subject of an
    `assert` and meaningless as a discarded tuple element.
    """
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr.startswith("assert"):
            return f"mock assertion {func.attr}()"
        if isinstance(func, ast.Name) and func.id.startswith("assert"):
            return f"assertion helper {func.id}()"
        return None
    if isinstance(node, ast.Attribute) and node.attr.startswith("assert"):
        return f"uncalled mock assertion {node.attr}"
    if isinstance(node, ast.Compare):
        return "comparison"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return "`not` expression"
    return None


def inert_tuple_assertions(tree: ast.AST, rel: str) -> list[str]:
    """Statement-level tuples whose first element looks like an assertion."""
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Tuple):
            continue
        elts = node.value.elts
        if not elts:
            continue
        shape = _assertion_shape(elts[0])
        if shape is not None:
            problems.append(
                f"{rel}:{node.lineno} discards a {shape} inside a tuple expression "
                f"— the `assert` keyword is missing, so nothing is checked"
            )
    return problems


def _python_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
    )


def test_no_inert_tuple_assertions_repo_wide():
    problems: list[str] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        problems += inert_tuple_assertions(tree, path.relative_to(REPO_ROOT).as_posix())

    assert not problems, (
        "A tuple expression is not an assert. Add the missing `assert` keyword "
        "(or drop the message and leave the mock call on its own line):\n  "
        + "\n  ".join(problems)
    )


@pytest.mark.parametrize(
    "src",
    [
        'sync.assert_not_awaited(), "a reconnect must not sync"',
        'store._save.assert_called_once_with(), (\n    "msg"\n)',
        'clear.assert_called(), "msg"',
        'holder[0].close.assert_called_once(), ("a", "b")',
        'assertSomething(x), "msg"',
        'm.assert_called_once, "msg"',
        'x == 1, "msg"',
        'not x, "msg"',
    ],
)
def test_guard_fires_on_each_inert_shape(src):
    """The guard must actually fire, not vacuously pass."""
    assert inert_tuple_assertions(ast.parse(src), "fake.py")


@pytest.mark.parametrize(
    "src",
    [
        # The correct idioms — must NOT be flagged.
        "m.assert_called_once()",
        'assert m.called, "msg"',
        'assert x == 1, "msg"',
        "m.assert_called_once_with(1, 2)",
        # A genuine tuple statement that is not an assertion shape.
        'foo(), "msg"',
        "a, b",
        # A tuple *assigned* or *returned* is fine.
        'pair = (m.assert_called(), "msg")',
        'def f():\n    return m.assert_called(), "msg"',
    ],
)
def test_guard_allows_correct_shapes(src):
    assert not inert_tuple_assertions(ast.parse(src), "fake.py")
