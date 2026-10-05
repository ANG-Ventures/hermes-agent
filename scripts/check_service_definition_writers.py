#!/usr/bin/env python3
"""Fail when a gateway service definition is written or removed outside the ownership chokepoint.

INVARIANT (t_8749a807): a systemd unit / launchd plist for gateway G is written or removed only by a
process whose HERMES_HOME is the home G's definition pins. ``hermes_cli.gateway_service_owner.
assert_may_mutate`` is THE chokepoint. On 2026-10-04 a scratch-home gateway's on-boot refresh rewrote
ACE-AI's real unit because the writer checked the service NAME and nothing checked the file's owner.

Flags, in ``hermes_cli/gateway.py`` and ``hermes_cli/gateway_launchd.py``: a function that calls
``.write_text`` / ``.write_bytes`` / ``.unlink`` on a receiver named like a service definition
(``unit_path``, ``plist_path``, ``unit``, ``plist``, ``dropin``) and neither calls ``assert_may_mutate``
itself, nor calls a same-file function that does, nor is listed in ``_DELEGATES`` (a helper whose only
callers hold the permission).

Suppress a true false positive with ``# service-owner: ok — <why>`` on the call's line.

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
SUPPRESS = "# service-owner: ok"
# Helpers that mutate on behalf of a caller that already passed the chokepoint in the same call.
_DELEGATES = {"gateway.py": {"_remove_units"}}


def _attr(node: ast.AST) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)


def scan_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8-sig")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    rel = path.relative_to(ROOT).as_posix()
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    calls_of = {fn: [n for n in ast.walk(fn) if isinstance(n, ast.Call)] for fn in functions}
    # Holds the permission: calls the chokepoint itself, or calls a same-file function that does (one hop).
    direct = {fn.name for fn in functions if any(_attr(c.func) == CHOKEPOINT for c in calls_of[fn])}
    guarded = direct | {fn.name for fn in functions if any(_attr(c.func) in direct for c in calls_of[fn])}
    problems: list[str] = []
    for fn in functions:
        if fn.name in guarded or fn.name in _DELEGATES.get(path.name, set()):
            continue
        for c in calls_of[fn]:
            if not (isinstance(c.func, ast.Attribute) and c.func.attr in MUTATORS):
                continue
            receiver = ast.get_source_segment(src, c.func.value) or ""
            if not DEFINITION_RE.search(receiver):
                continue
            if SUPPRESS in lines[c.lineno - 1]:
                continue
            problems.append(f"{rel}:{c.lineno}: {fn.name}() mutates {receiver} without {CHOKEPOINT}() "
                            "— every gateway service-definition write/remove goes through the ownership chokepoint")
    return problems


def main(argv: list[str]) -> int:
    files = [ROOT / a for a in argv] or [ROOT / f for f in DEFAULT_FILES]
    problems: list[str] = []
    for f in files:
        if f.exists():
            problems.extend(scan_file(f))
    if problems:
        print("gateway service definitions are mutated only through assert_may_mutate:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"check_service_definition_writers: OK ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
