#!/usr/bin/env python3
"""Fail a test that spawns ``hermes kanban create|promote|claim`` without a stated identity.

The kanban create guard (``kanban_db.create_task(require_home=True)``) refuses a scripted,
sessionless create: ``kanban: refused create (script): no home session``. Whether a spawned
CLI hits that refusal therefore depends on the session identity the CHILD inherits. A test
that leaves it ambient passes in a shell that carries ``HERMES_SESSION_ID`` (an agent or
kanban worker) and fails in CI and off-box: hermes-agent#1825 shipped "5/5 pass" and read
2/5 on the off-box receipt (t_f4c584e2).

A test passes this check when every such spawn either

* runs in a test/fixture/helper that requests the ``kanban_identity`` fixture
  (``tests/conftest.py``), or
* names the identity in the call: ``--parent``, ``--session``, ``--home`` or ``--unhomed``
  (the guard's own explicit choices), or sets ``HERMES_KANBAN_ALLOW_UNHOMED_CREATE`` in the
  env it passes.

A spawn is a call (not ``parse_args``/``run_slash``, which run in-process under the suite's
hermetic env) whose positional arguments carry the literal tokens ``"kanban", "<verb>"``,
a single shell-string argument containing ``kanban <verb>``, or a call to a module helper
that spawns ``"kanban", *argv`` with ``<verb>`` among the call's literals (the #1825 shape).

Suppress a true false positive with ``# kanban-identity: ok — <why>`` on the call's line.

Usage: python3 scripts/check_kanban_test_identity.py [paths...]   (default: tests/)
"""
from __future__ import annotations

import ast
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERBS = frozenset({"create", "promote", "claim"})
# The guard's explicit choices, plus its suite-compat env escape set ON the spawn itself.
IDENTITY_FLAGS = ("--parent", "--session", "--home", "--unhomed", "HERMES_KANBAN_ALLOW_UNHOMED_CREATE")
FIXTURE = "kanban_identity"
IN_PROCESS = frozenset({"parse_args", "run_slash", "parse_known_args"})
# ``/kanban create`` is a slash command run in-process (gateway/CLI), not a spawn.
SHELL_RE = re.compile(r"(?<![/\w])kanban\s+(?:--board\s+\S+\s+)?(create|promote|claim)\b")
SUPPRESS = "# kanban-identity: ok"


def _func_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _flatten(args: list[ast.expr]) -> list[ast.expr]:
    out: list[ast.expr] = []
    for a in args:
        if isinstance(a, (ast.List, ast.Tuple)):
            out.extend(_flatten(list(a.elts)))
        elif isinstance(a, ast.Starred):
            out.extend(_flatten([a.value]))
        else:
            out.append(a)
    return out


def _strings(nodes: list[ast.expr]) -> list[str | None]:
    return [n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None
            for n in nodes]


def _spawn_text(call: ast.Call, helpers: frozenset[str] = frozenset()) -> str | None:
    """The kanban command this call spawns, or None when it spawns none."""
    if _func_name(call.func) in IN_PROCESS:
        return None
    toks = _strings(_flatten(list(call.args)))
    for i, tok in enumerate(toks[:-1]):
        if tok == "kanban" and toks[i + 1] in VERBS:
            return " ".join(t or "<expr>" for t in toks)
    for tok in toks:
        if tok and SHELL_RE.search(tok):
            return tok
    # ``_cli(home, "create", ...)`` where ``_cli`` spawns ``... "kanban", *args``.
    if _func_name(call.func) in helpers and _helper_verb(toks) in VERBS:
        return f"{_func_name(call.func)}(" + ", ".join(t or "<expr>" for t in toks) + ")"
    return None


def _helper_verb(toks: list[str | None]) -> str | None:
    """The kanban subcommand of a helper call's literal argv: the first bare word, past
    ``--board X`` (``boards create`` is a board, not a card)."""
    skip = False
    for tok in toks:
        if skip or tok is None:
            skip = False
            continue
        if tok == "--board":
            skip = True
        elif not tok.startswith("-"):
            return tok
    return None


def _kanban_helpers(tree: ast.AST) -> frozenset[str]:
    """Module functions that spawn ``kanban`` followed by a caller-supplied argv."""
    names: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            if _func_name(call.func) in IN_PROCESS:
                continue
            nodes = _flatten(list(call.args))
            toks = _strings(nodes)
            if any(t == "kanban" and toks[i + 1] is None for i, t in enumerate(toks[:-1])):
                names.add(fn.name)
                break
    return frozenset(names)


def _names_identity(call: ast.Call, src: str) -> bool:
    text = ast.get_source_segment(src, call) or ""
    return any(flag in text for flag in IDENTITY_FLAGS)


def _requests_fixture(fn: ast.AST) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    a = fn.args
    return any(arg.arg == FIXTURE for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs))


def check_source(src: str, filename: str = "<src>") -> list[str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # other files' invalid-escape Syntax/DeprecationWarnings
        tree = ast.parse(src, filename=filename)
    helpers = _kanban_helpers(tree)
    lines = src.splitlines()
    problems: list[str] = []

    def visit(node: ast.AST, scope: list[ast.AST]) -> None:
        if isinstance(node, ast.Call):
            spawned = _spawn_text(node, helpers)
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            if (spawned and SUPPRESS not in line and not _names_identity(node, src)
                    and not any(_requests_fixture(fn) for fn in scope)):
                problems.append(
                    f"{filename}:{node.lineno}: spawns `{spawned}` with no stated identity: "
                    f"request the `{FIXTURE}` fixture or pass --parent/--session/--home/--unhomed")
        child_scope = scope + [node] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
        for child in ast.iter_child_nodes(node):
            visit(child, child_scope)

    visit(tree, [])
    return problems


def main(argv: list[str]) -> int:
    targets = [Path(p) for p in argv] or [ROOT / "tests"]
    files: list[Path] = []
    for t in targets:
        files.extend(sorted(t.rglob("*.py")) if t.is_dir() else [t])
    problems: list[str] = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(f.resolve().relative_to(ROOT))
        except ValueError:
            rel = str(f)
        try:
            problems.extend(check_source(src, rel))
        except SyntaxError as exc:
            problems.append(f"{rel}: unparsable: {exc}")
    for p in problems:
        print(p)
    if problems:
        print(f"\n{len(problems)} kanban spawn(s) without a stated identity (t_f4c584e2).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
