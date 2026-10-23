#!/usr/bin/env python3
"""Fail when a kanban ROUTE ENTRY is called without the fleet pause hold deciding first (t_133d6efd).

INVARIANT (fleet pause/resume SPEC v0.3 §5.8): a card whose origin host is paused is never handed to a
routing step that can spawn it OR MUTATE it. ``kanban_continuation.origin_route`` can persist
``origin_host=NULL`` (continuation=enforce + an ``empty`` probe -> ``clear_lost_state``), after which the
next tick sees no origin and spawns the card on another host (Prism 76b7233abe29, the third escape of this
class after Momus CB1 and N1). So the hold predicate (``kanban_pause_hold``) must run BEFORE every route
entry, not after it.

ROUTE ENTRIES: ``_route_row``, ``origin_route``, ``clear_lost_state``. Every call to one of them in
``hermes_cli/`` must match a row in ``SITES`` below: ``(file, enclosing function, callee) -> (guard, ...)``,
one guard per call in source order (a second call in the same function needs its own entry).

  * a guard is a NAME that must appear in a test (``if`` / ``elif`` / conditional expression /
    comprehension ``if``) of an ancestor of the call inside the same function, OR in the test of an earlier
    top-level ``if <guard ...>: return ...`` of that function (an early exit for held rows): the call is reached
    only after that held-set or pin-set was consulted.
  * a guard written ``kw=NAME`` instead means the call itself must pass ``kw=NAME`` (the callee decides first:
    ``_route_row(..., held=_pause_held)`` returns the hold before its own ``origin_route``). The ready-lane
    comprehension stays unconditional, as Router Core §4.2 / lint-queue-loops D1 require. ``_pause_held`` is the ready lane's pre-route held set
    (``kanban_pause_hold.held_routes``); ``_review_pause_pins`` proves the review loop is the one fed by
    ``gate_rows`` (which already dropped held review rows).
  * ``guard`` None means the call is internal to another route entry that is itself guarded at its callers
    (``origin_route`` -> ``clear_lost_state``, ``_route_row`` -> ``origin_route``).

A NEW call site (no row) is red: decide the hold before it (``kanban_pause_hold.held_routes`` / ``decide``),
then add the row with its guard. A row whose site is gone is red too (stale), so the table only describes
code that exists. A row whose guard is no longer in an ancestor test is red: the guard moved after the call.

Exit 0 clean (silent), 1 findings (one line each). ``--selftest`` proves red and green on fixtures.
Usage: python3 scripts/check_pause_hold_route_entries.py [--root DIR] [--selftest]
"""
from __future__ import annotations

import argparse
import ast
import sys
import warnings
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = frozenset({"_route_row", "origin_route", "clear_lost_state"})
SCAN = "hermes_cli"

Key = Tuple[str, str, str]  # (repo-relative file, enclosing function, callee)
SITES: Dict[Key, Tuple[Optional[str], ...]] = {
    ("hermes_cli/kanban_db_dispatch.py", "_dispatch_once_locked", "_route_row"): ("held=_pause_held",),
    # ready-lane no-plan continuation branch, then the review loop
    ("hermes_cli/kanban_db_dispatch.py", "_dispatch_once_locked", "origin_route"): ("_pause_held",
                                                                                     "_review_pause_pins"),
    ("hermes_cli/kanban_db_dispatch.py", "_route_row", "origin_route"): ("held",),
    ("hermes_cli/kanban_continuation.py", "origin_route", "clear_lost_state"): (None,),
}


def _callee(node: ast.Call) -> Optional[str]:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _names(node: ast.AST) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _tests_of(ancestor: ast.AST, child: ast.AST) -> List[ast.AST]:
    """The condition nodes of ``ancestor`` that guard ``child`` (never the child's own subtree)."""
    if isinstance(ancestor, (ast.If, ast.IfExp, ast.While)):
        return [ancestor.test] if child is not ancestor.test else []
    if isinstance(ancestor, ast.comprehension):
        return [c for c in ancestor.ifs if c is not child]
    if isinstance(ancestor, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return [c for gen in ancestor.generators for c in gen.ifs if c is not child]
    return []


def _sites(tree: ast.AST) -> Iterator[Tuple[str, ast.Call, List[ast.AST]]]:
    """(enclosing function node, call, ancestor chain inside that function) for every route-entry call."""
    def walk(node, func, chain):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child, [])
                continue
            if isinstance(child, ast.Call) and _callee(child) in ENTRIES and func is not None:
                yield func, child, chain + [node]
            yield from walk(child, func, chain + [node])
    yield from walk(tree, None, [])


def _passes_kw(call: ast.Call, guard: str) -> bool:
    kw, _, name = guard.partition("=")
    return any(k.arg == kw and isinstance(k.value, ast.Name) and k.value.id == name for k in call.keywords)


def _early_exit_before(func: ast.AST, call: ast.Call, guard: str) -> bool:
    """A top-level ``if <test naming guard>: ... return`` in ``func`` that precedes the call's statement."""
    for st in getattr(func, "body", []):
        if any(n is call for n in ast.walk(st)):
            return False
        if isinstance(st, ast.If) and guard in _names(st.test) and st.body and isinstance(st.body[-1], ast.Return):
            return True
    return False


def _guarded(call: ast.Call, chain: List[ast.AST], guard: str, func: ast.AST = None) -> bool:
    if "=" in guard:
        return _passes_kw(call, guard)
    if func is not None and _early_exit_before(func, call, guard):
        return True
    nodes = chain + [call]
    for i, anc in enumerate(chain):
        nxt = nodes[i + 1]
        for test in _tests_of(anc, nxt):
            if guard in _names(test):
                return True
    return False


def check(root: Path) -> List[str]:
    findings: List[str] = []
    seen: Dict[Key, int] = {}
    files = sorted((root / SCAN).rglob("*.py"))
    if not files:
        return [f"{SCAN}: no files scanned under {root} (a lint that scans nothing is red)"]
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            findings.append(f"{rel}: unparseable ({exc})")
            continue
        calls = sorted(_sites(tree), key=lambda t: (t[1].lineno, t[1].col_offset))
        for fnode, call, chain in calls:
            func = fnode.name
            name = _callee(call)
            key = (rel, func, name)
            n = seen.get(key, 0)
            seen[key] = n + 1
            guards = SITES.get(key)
            if guards is None or n >= len(guards):
                findings.append(f"{rel}:{call.lineno}: route entry {name}() in {func}() is not in SITES: decide the "
                                f"fleet pause hold before it (kanban_pause_hold.held_routes/decide), then add it")
                continue
            guard = guards[n]
            if guard is not None and not _guarded(call, chain, guard, fnode):
                findings.append(f"{rel}:{call.lineno}: route entry {name}() in {func}() is reached without "
                                f"`{guard}` guarding it: the pause hold must decide before it")
    for key, guards in SITES.items():
        if not (root / key[0]).exists():
            findings.append(f"{key[0]}: SITES row names a missing file")
        elif seen.get(key, 0) < len(guards):
            findings.append(f"{key[0]}: SITES row {key[1]}() -> {key[2]} expects {len(guards)} call(s), found "
                            f"{seen.get(key, 0)} (stale row: trim it)")
    return findings


_FIX_OK = '''
def _dispatch_once_locked(conn, rows, plan):
    _pause_held = held_routes(rows, {})
    row_route = {r["id"]: _route_row(conn, r, plan, held=_pause_held) for r in rows}
    for r in rows:
        if r["origin_host"] and r["id"] not in _pause_held:
            origin_route(conn, r)
    for row in rows:
        if row["id"] in _review_pause_pins:
            pass
        elif row["origin_host"]:
            origin_route(conn, row)

def _route_row(conn, row, plan, held=None):
    if held and row["id"] in held:
        return held[row["id"]]
    return origin_route(conn, row)
'''
_FIX_CONT = '''
def origin_route(conn, row):
    clear_lost_state(conn, row["id"], "h")
'''


def _selftest() -> int:
    import tempfile

    def run(dispatch: str) -> List[str]:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / SCAN).mkdir()
            (root / SCAN / "kanban_db_dispatch.py").write_text(dispatch, encoding="utf-8")
            (root / SCAN / "kanban_continuation.py").write_text(_FIX_CONT, encoding="utf-8")
            return check(root)

    cases = {
        "green": (_FIX_OK, 0),
        # the 294e6432 shape: _route_row over every row, the hold only after routing
        "route_row without held=": (_FIX_OK.replace("_route_row(conn, r, plan, held=_pause_held)",
                                                     "_route_row(conn, r, plan)"), 1),
        "route_row early exit removed": (_FIX_OK.replace(
            '    if held and row["id"] in held:\n        return held[row["id"]]\n', ""), 1),
        "early exit AFTER the call": (_FIX_OK.replace(
            '    if held and row["id"] in held:\n        return held[row["id"]]\n    return origin_route(conn, row)',
            '    x = origin_route(conn, row)\n    if held and row["id"] in held:\n        return held[row["id"]]\n'
            '    return x'), 1),
        "unguarded no-plan loop": (_FIX_OK.replace(' and r["id"] not in _pause_held', ""), 1),
        "new route entry": (_FIX_OK + "\ndef other(conn, r):\n    origin_route(conn, r)\n", 1),
        "stale row": (_FIX_OK.replace("    return origin_route(conn, row)\n", "    return None\n"), 1),
    }
    bad = 0
    for name, (src, want) in cases.items():
        got = run(src)
        ok = (len(got) == 0) if want == 0 else (len(got) >= 1)
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {len(got)} finding(s)")
        for line in got:
            print(f"       {line}")
        bad += not ok
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    findings = check(args.root)
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
