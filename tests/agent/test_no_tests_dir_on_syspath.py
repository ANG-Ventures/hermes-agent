"""Lint: no test may put ``tests/`` itself on ``sys.path``.

``tests/`` contains packages whose names shadow real top-level ones — measured
2026-08-09: agent, cron, docker, gateway, hermes_cli, plugins, providers, tools,
tui_gateway, website. Ten collisions.

If a test inserts ``tests/`` onto ``sys.path`` (the classic
``os.path.join(os.path.dirname(__file__), "..")`` from a file in
``tests/<sub>/``), then any later ``from hermes_cli import ...`` resolves to
``tests/hermes_cli`` instead of the real package. The failure does NOT look like
a path bug: ``tests/agent/test_endpoint_blackhole.py`` surfaced it as 22
collection ERRORs raised from deep inside ``agent/auxiliary_client.py``, and the
per-file CI runner reported it only as "1 file where no tests ran".

The correct idiom is the repo ROOT, resolved from ``__file__`` so it survives
git worktrees and second clones::

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

This lint asserts the property (does the computed path equal ``tests/``?) rather
than pattern-matching one spelling, so a novel spelling of the same mistake is
still caught.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"


def _syspath_insert_targets(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, resolved_dir) for every literal sys.path insert/append."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"insert", "append"}:
            continue
        # sys.path.insert(...) / sys.path.append(...)
        owner = node.func.value
        if not (
            isinstance(owner, ast.Attribute)
            and owner.attr == "path"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "sys"
        ):
            continue

        arg = node.args[-1] if node.args else None
        resolved = _eval_path_expr(arg, path)
        if resolved is not None:
            out.append((node.lineno, resolved))
    return out


def _eval_path_expr(node: ast.AST | None, source_file: Path) -> str | None:
    """Statically evaluate the os.path.* expressions tests actually use.

    Returns a normalised absolute path, or None when the expression is dynamic
    enough that we cannot judge it (we do not guess — an unjudgeable expression
    is not reported).
    """
    if node is None:
        return None

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return os.path.normpath(node.value)

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        fn = node.func.attr
        parts = [_eval_path_expr(a, source_file) for a in node.args]

        if fn == "dirname" and len(parts) == 1 and parts[0] is not None:
            return os.path.dirname(parts[0])
        if fn == "abspath" and len(parts) == 1 and parts[0] is not None:
            return os.path.normpath(parts[0])
        if fn == "join" and parts and all(p is not None for p in parts):
            return os.path.normpath(os.path.join(*parts))  # type: ignore[arg-type]

    # Bare __file__ -> this test module's own path.
    if isinstance(node, ast.Name) and node.id == "__file__":
        return str(source_file)

    return None


def test_no_test_puts_tests_dir_on_syspath() -> None:
    offenders: list[str] = []

    for path in sorted(TESTS_DIR.rglob("*.py")):
        for lineno, target in _syspath_insert_targets(path):
            if os.path.normpath(target) == os.path.normpath(str(TESTS_DIR)):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno} inserts tests/ onto sys.path")

    assert not offenders, (
        "These files put tests/ on sys.path, which shadows real top-level "
        "packages (hermes_cli, gateway, agent, ...):\n  "
        + "\n  ".join(offenders)
        + "\n\nUse the repo root instead:\n"
        "  sys.path.insert(0, os.path.dirname(os.path.dirname(\n"
        "      os.path.dirname(os.path.abspath(__file__)))))"
    )


def test_lint_is_not_vacuous() -> None:
    """Positive control: the evaluator must actually resolve the bad idiom.

    A lint that silently evaluates nothing would pass forever. Feed it the exact
    expression that caused the incident and assert it resolves to tests/.
    """
    src = 'import sys, os\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))\n'
    fake = TESTS_DIR / "agent" / "_lint_probe.py"

    tree = ast.parse(src)
    resolved: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "insert":
                got = _eval_path_expr(node.args[-1], fake)
                if got:
                    resolved.append(os.path.normpath(got))

    assert resolved == [os.path.normpath(str(TESTS_DIR))], (
        f"evaluator failed to resolve the known-bad idiom; got {resolved!r}"
    )


def test_tests_dir_really_does_shadow_real_packages() -> None:
    """Document WHY the lint exists, and fail if the premise ever stops holding."""
    shadowed = {
        d.name
        for d in TESTS_DIR.iterdir()
        if d.is_dir() and (d / "__init__.py").exists() and (REPO_ROOT / d.name).is_dir()
    }
    assert "hermes_cli" in shadowed, (
        "tests/hermes_cli no longer shadows the real package — re-check whether "
        f"this lint is still needed. Currently shadowed: {sorted(shadowed)}"
    )
