#!/usr/bin/env python3
"""Fail when a gateway service definition is written or removed without the ownership chokepoint DOMINATING it.

INVARIANT (t_8749a807): a systemd unit / launchd plist for gateway G is written or removed only by a
process whose HERMES_HOME is the home G's definition pins. ``hermes_cli.gateway_service_owner.
assert_may_mutate`` is THE chokepoint. On 2026-10-04 a scratch-home gateway's on-boot refresh rewrote
ACE-AI's real unit because the writer checked the service NAME and nothing checked the file's owner.

Dominance, not presence. A chokepoint call that merely appears somewhere in the function proves nothing:
``if False: assert_may_mutate(...)``, or the call placed AFTER the write, still "mentions" it and reopens
the hole. For every mutator (``.write_text`` / ``.write_bytes`` / ``.unlink`` on a receiver named like a
service definition: ``unit_path``, ``plist_path``, ``unit``, ``plist``, ``dropin``) the check walks outward
from the statement holding it through every enclosing statement block (``if``/``for``/``try``/``with``
bodies, and a nested ``def``: a closure runs only after its ``def`` statement) and requires a CHOKEPOINT
STATEMENT among the siblings that PRECEDE it at some level. A chokepoint statement is one of:

  * ``assert_may_mutate(...)`` as a bare expression statement or the value of an assignment;
  * a call to a same-file function that is GUARDED FROM ENTRY: its own top-level body reaches a chokepoint
    statement with nothing but docstring / imports / assignments / ``pass`` / plain expressions before it
    (no ``if``/``return``/``try`` that could skip it);
  * ``try:`` whose FIRST statement is a chokepoint statement, whose every ``except`` handler ends in
    ``return`` / ``raise`` / ``continue`` / ``break`` (a refusal leaves the path, never falls through), and
    whose ``finally`` cannot ``return`` / ``break`` / ``continue`` (that would swallow the refusal).

A loop is never a chokepoint for what follows it: it may run zero times, and a refused item that
``continue``s leaves the loop's later items, and every statement after the loop, undominated. Per-item
admission therefore sits in the SAME loop body as the item's mutation. Only a direct call resolves: the
chokepoint is the name ``assert_may_mutate`` imported from ``hermes_cli.gateway_service_owner``, and a guarded
helper is a module-level, plain (not ``async``, not generator) ``def`` called by its bare name; ``obj.name()``
never matches, since the attribute may resolve to anything.

Suppress a true false positive with ``# service-owner: ok — <why>`` on the mutator's line.

Usage: python3 scripts/check_service_definition_writers.py [paths...]
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILES = ("hermes_cli/gateway.py", "hermes_cli/gateway_launchd.py")
MUTATORS = {"write_text", "write_bytes", "unlink"}
DEFINITION_RE = re.compile(r"\b(unit_path|plist_path|dropin|unit|plist)\b")
CHOKEPOINT = "assert_may_mutate"
CHOKEPOINT_MODULE = "hermes_cli.gateway_service_owner"
SUPPRESS = "# service-owner: ok"

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_EXITS = (ast.Return, ast.Raise, ast.Continue, ast.Break)
# Statements that cannot skip what follows them: a callee that runs only these before its chokepoint
# statement is guarded from entry.
_STRAIGHT = (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom, ast.Pass)
_BLOCK_FIELDS = ("body", "orelse", "finalbody")


def _callee_name(node: ast.AST) -> str | None:
    """Bare-name callee only: ``obj.check()`` may resolve to any object's method, so it never matches."""
    if not isinstance(node, ast.Call):
        return None
    return node.func.id if isinstance(node.func, ast.Name) else None


def _own_nodes(stmts: list[ast.stmt]):
    """Every node under *stmts* except the bodies of nested function/class definitions."""
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _stmt_call(stmt: ast.stmt) -> str | None:
    """Name of the call a bare-expression or assignment statement makes, else None."""
    if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign)) and stmt.value is not None:
        return _callee_name(stmt.value)
    return None


class _Scanner:
    def __init__(self, src: str, rel: str) -> None:
        self.src, self.rel = src, rel
        self.lines = src.splitlines()
        tree = ast.parse(src)
        # Calling an async or generator function runs none of its body, so neither can guard its caller.
        self.functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)
                          and not any(isinstance(x, (ast.Yield, ast.YieldFrom)) for x in _own_nodes(n.body))}
        self.chokepoint_bound = CHOKEPOINT not in self.functions and any(
            isinstance(n, ast.ImportFrom) and n.module == CHOKEPOINT_MODULE
            and any(a.name == CHOKEPOINT and a.asname in (None, CHOKEPOINT) for a in n.names)
            for n in ast.walk(tree))
        self._guarded: dict[str, bool] = {}
        # statement -> (its block, index in that block, statement owning the block or None at module level)
        self.place: dict[ast.stmt, tuple[list[ast.stmt], int, ast.stmt | None]] = {}
        # mutator call -> (innermost statement, innermost enclosing function or None)
        self.calls: dict[ast.Call, tuple[ast.stmt, ast.FunctionDef | ast.AsyncFunctionDef | None]] = {}
        self._index(tree, None, None)

    def _index(self, owner: ast.AST, owner_stmt: ast.stmt | None,
               fn: ast.FunctionDef | ast.AsyncFunctionDef | None) -> None:
        blocks = [getattr(owner, f) for f in _BLOCK_FIELDS if isinstance(getattr(owner, f, None), list)]
        blocks += [h.body for h in getattr(owner, "handlers", None) or []]
        for block in blocks:
            for i, stmt in enumerate(block):
                self.place[stmt] = (block, i, owner_stmt)
                inner_fn = stmt if isinstance(stmt, _FUNCS) else fn
                self._index(stmt, stmt, inner_fn)
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Call) and sub not in self.calls:
                        self.calls[sub] = (stmt, inner_fn)

    # -- chokepoint statement forms --------------------------------------------------------------
    def guarded_from_entry(self, name: str, seen: frozenset[str] = frozenset()) -> bool:
        """A same-file function whose top-level body cannot return before a chokepoint statement."""
        if name in self._guarded:
            return self._guarded[name]
        fn = self.functions.get(name)
        if fn is None or name in seen:
            return False
        ok = False
        for stmt in fn.body:
            if self.is_chokepoint(stmt, seen | {name}):
                ok = True
                break
            if not isinstance(stmt, _STRAIGHT):
                break
        self._guarded[name] = ok
        return ok

    def is_chokepoint(self, stmt: ast.stmt, seen: frozenset[str] = frozenset()) -> bool:
        call = _stmt_call(stmt)
        if call is not None:
            return (call == CHOKEPOINT and self.chokepoint_bound) or self.guarded_from_entry(call, seen)
        if isinstance(stmt, ast.Try):
            return bool(stmt.body) and self.is_chokepoint(stmt.body[0], seen) and all(
                h.body and isinstance(h.body[-1], _EXITS) for h in stmt.handlers) and not any(
                isinstance(n, (ast.Return, ast.Break, ast.Continue)) for n in _own_nodes(stmt.finalbody))
        return False

    # -- dominance ---------------------------------------------------------------------------------
    def dominated(self, stmt: ast.stmt | None) -> bool:
        """Some preceding sibling, at the mutator's level or any enclosing level, is a chokepoint statement.
        A module-level mutator is never dominated."""
        while stmt is not None and stmt in self.place:
            block, idx, owner = self.place[stmt]
            if any(self.is_chokepoint(prev) for prev in block[:idx]):
                return True
            stmt = owner
        return False

    def problems(self) -> list[str]:
        out: list[str] = []
        for call, (stmt, fn) in self.calls.items():
            if not (isinstance(call.func, ast.Attribute) and call.func.attr in MUTATORS):
                continue
            receiver = ast.get_source_segment(self.src, call.func.value) or ""
            if not DEFINITION_RE.search(receiver) or SUPPRESS in self.lines[call.lineno - 1]:
                continue
            if self.dominated(stmt):
                continue
            where = f"{fn.name}()" if fn is not None else "module level"
            out.append(f"{self.rel}:{call.lineno}: {where} mutates {receiver} without {CHOKEPOINT}() dominating it "
                       "— every gateway service-definition write/remove runs the ownership chokepoint FIRST, "
                       "unconditionally, on the same path")
        return out


def scan_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8-sig")
    try:
        scanner = _Scanner(src, path.relative_to(ROOT).as_posix())
    except SyntaxError:
        return []
    return scanner.problems()


def main(argv: list[str]) -> int:
    files = [ROOT / a for a in argv] or [ROOT / f for f in DEFAULT_FILES]
    problems: list[str] = []
    for f in files:
        if f.exists():
            problems.extend(scan_file(f))
    if problems:
        print("gateway service definitions are mutated only under assert_may_mutate's dominance:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"check_service_definition_writers: OK ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
