#!/usr/bin/env python3
# SCHEDULE: manual (CI lint: run by the spawn-site CI job in hermes-agent fork and fleet-ops-scripts; no cron row)
"""lint-queue-loops: the two queue loops present EVERY ready item each tick (Router Core spec v5 §4.2 / §7.1 [v4 RC-e]).

WHY: the router keeps the waiting set as "every (consumer, job_id) whose last /admit was a 409 within 2 x
retry_after_s". A queue loop that stops at the first refusal never re-presents the items behind it, so they age out of
the waiting set, holdback cannot see them, and the router's ordering silently degrades to the consumer's own FIFO.
The two loops the spec names:

  dispatcher  hermes_cli/kanban_db_dispatch.py (hermes-agent fork)
              D1 `_route_row` is called for every ready row: a comprehension/loop over `ready_rows` with no filter.
              D2 the spawn loop `for ... in enumerate(ready_rows)` / `for row in ready_rows` inside
                 `_dispatch_once_locked` has no `break` / `return` that can end it early, EXCEPT a `break` that sits
                 after an `if route_first: ... continue` in the same block (the kanban.placement.route_first=false
                 operator kill switch; default true walks every row in record-only mode).
  pool        ci-runners/src/ci-runner-pool.sh (fleet-ops-scripts)
              P1 the slot loop (`while ... "$i" -le "$effective_pool" ...; do ... done`) holds no `break`, `return`
                 or `exit`: a refused slot (mem_refuse, a future 409) is `i=$((i + 1)); continue`.

A check whose target file or loop is missing is RED, never skipped (S6: a lint that inspects nothing is not clean).
Exit 0 clean (silent), 1 findings (one line each), 2 usage.
  --root DIR       tree to lint (default: cwd)
  --check NAME     dispatcher | pool (repeatable; required: each repo names the loops it owns)
  --selftest       prove red and green on fixtures
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
import tempfile
from pathlib import Path

TARGETS = {
    "dispatcher": "hermes_cli/kanban_db_dispatch.py",
    "pool": "ci-runners/src/ci-runner-pool.sh",
}
SPAWN_FN = "_dispatch_once_locked"
ROUTE_FN = "_route_row"
READY = "ready_rows"
ROUTE_MAP = "row_route"


# ---------------------------------------------------------------- dispatcher (python AST)
def _iter_names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _is_route_first_continue(stmt) -> bool:
    """`if route_first: ... continue` (the body ends in continue)."""
    return (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Name) and stmt.test.id == "route_first"
            and bool(stmt.body) and isinstance(stmt.body[-1], ast.Continue))


def _early_exits(body: list, path: str, guarded: bool = False, raises: bool = False,
                 caught: bool = False) -> list[str]:
    """Break/return statements in a loop body that end the loop early (nested loops/functions are their own).
    `raises`: a `raise <exc>` ends it too (the routing loop: the rows after it are never presented), unless it sits
    in the body of a try whose handlers may catch it (`caught`). A bare `raise` re-raises an error that was leaving
    anyway (_reraises), so it is not a new exit."""
    out = []
    seen_guard = guarded
    for st in body:
        guard_after = _is_route_first_continue(st)   # exempts only the statements AFTER this one
        if isinstance(st, ast.Break) and not seen_guard:
            out.append(f"{path}:{st.lineno}: break in the ready loop ends the tick early (not behind "
                       f"`if route_first: continue`)")
        elif isinstance(st, ast.Return):
            out.append(f"{path}:{st.lineno}: return inside the ready loop ends the tick early")
        elif isinstance(st, ast.Raise) and st.exc is not None and raises and not caught:
            out.append(f"{path}:{st.lineno}: raise inside the ready loop ends the tick early")
        elif isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            # a nested loop's break is its own; only a return (or raise) escapes its body. Its else clause runs
            # in the outer iteration, so a break there ends the outer loop (t_f87e058c X3).
            out += _early_exits_returns_only(st.body, path, raises, caught)
            out += _early_exits(st.orelse, path, seen_guard, raises, caught)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        else:
            may_catch = caught or (isinstance(st, TRY) and bool(st.handlers) and not _reraises(st))
            for fld in ("body", "orelse", "finalbody"):
                sub = getattr(st, fld, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    out += _early_exits(sub, path, seen_guard, raises, may_catch if fld == "body" else caught)
            for h in getattr(st, "handlers", []) or []:
                out += _early_exits(h.body, path, seen_guard, raises, caught)
            for c in getattr(st, "cases", []) or []:
                out += _early_exits(c.body, path, seen_guard, raises, caught)
        if guard_after:
            seen_guard = True
    return out


def _early_exits_returns_only(nodes, path: str, raises: bool = False, caught: bool = False) -> list[str]:
    """Return (and, with `raises`, an uncaught raise) anywhere in `nodes`, skipping nested defs / lambdas."""
    out = []
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Return):
            out.append(f"{path}:{n.lineno}: return inside the ready loop ends the tick early")
        elif isinstance(n, ast.Raise) and n.exc is not None and raises and not caught:
            out.append(f"{path}:{n.lineno}: raise inside the ready loop ends the tick early")
        if isinstance(n, TRY) and n.handlers and not _reraises(n):
            out += _early_exits_returns_only(n.body, path, raises, True)
            out += _early_exits_returns_only([*n.handlers, *n.orelse, *n.finalbody], path, raises, caught)
        else:
            out += _early_exits_returns_only(list(ast.iter_child_nodes(n)), path, raises, caught)
    return out


def _over_ready(it) -> bool:
    """`ready_rows` or `enumerate(ready_rows)` (not a slice / filtered view)."""
    if isinstance(it, ast.Name) and it.id == READY:
        return True
    return (isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate"
            and len(it.args) == 1 and isinstance(it.args[0], ast.Name) and it.args[0].id == READY)


EAGER = frozenset(("list", "dict", "set", "tuple", "sorted", "frozenset"))


def _eager_parent(fn, gen) -> bool:
    """A GeneratorExp runs only when consumed: accept it as the direct sole argument of list/dict/set/... ."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in EAGER
                and len(n.args) == 1 and n.args[0] is gen):
            return True
    return False


DEFERRED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_eager(node):
    """ast.walk that does not descend into a nested def / lambda / class: code there runs only if something calls
    it, so a routing comprehension in an uncalled helper (or one called only under `if cond:`) routes nothing."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in ast.iter_child_nodes(n) if not isinstance(c, DEFERRED))


TRY = tuple(t for t in (ast.Try, getattr(ast, "TryStar", None)) if t is not None)


def _finally_exits(st) -> bool:
    """The finally clause of `st` holds a return, or a break / continue of the try's own loop: it then discards the
    handlers' re-raise and the tick goes on (Prism 34a70a667291). A break / continue in an inner loop is that loop's."""
    def walk(n, own: bool) -> bool:
        if isinstance(n, DEFERRED):
            return False
        if isinstance(n, ast.Return) or (own and isinstance(n, (ast.Break, ast.Continue))):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            return any(walk(c, False) for c in n.body) or any(walk(c, own) for c in n.orelse)
        return any(walk(c, own) for c in ast.iter_child_nodes(n))
    return any(walk(c, True) for c in st.finalbody)


def _reraises(st) -> bool:
    """A try / try* whose every handler ends in a bare `raise` and cannot leave any other way, and whose finally
    cannot swallow the re-raise: an exception in its body aborts the tick, so the body runs to the end on every
    tick that goes on (t_f87e058c (a))."""
    return isinstance(st, TRY) and not _finally_exits(st) and all(
        h.body and isinstance(h.body[-1], ast.Raise) and h.body[-1].exc is None
        and not any(isinstance(n, (ast.Break, ast.Continue, ast.Return)) for n in _walk_eager(h))
        for h in st.handlers)


def _plain_with(st) -> bool:
    """A with / async with whose EVERY context manager is a bare name or attribute (`with lk:`, `with self._lock:`).
    A call such as `contextlib.suppress(...)` may swallow an exception and cut the body short (Prism 67a939c59339)."""
    return isinstance(st, (ast.With, ast.AsyncWith)) and all(
        isinstance(i.context_expr, (ast.Name, ast.Attribute)) for i in st.items)


def _always_runs(st):
    """The statements of a compound `st` that run every time `st` completes: a plain with / async with body
    (_plain_with, t_f87e058c (b)), or the body, else and finally of a re-raising try (_reraises). None otherwise."""
    if _plain_with(st):
        return st.body
    if _reraises(st):
        return st.body + st.orelse + st.finalbody
    return None


def _route_calls(node) -> list:
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == ROUTE_FN]


def _writes_row_route(node) -> bool:
    """`row_route[...] = ...` (plain, augmented or annotated assignment, incl. inside a tuple target)."""
    for n in ast.walk(node):
        targets = (n.targets if isinstance(n, ast.Assign) else
                   [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else [])
        for t in targets:
            if any(isinstance(s, ast.Subscript) and isinstance(s.value, ast.Name) and s.value.id == ROUTE_MAP
                   for s in ast.walk(t)):
                return True
    return False


def _conditional_inside(node, call) -> bool:
    """True when `call` sits under an IfExp / BoolOp / nested comprehension within `node` (it may not run)."""
    for n in ast.walk(node):
        if isinstance(n, (ast.IfExp, ast.BoolOp, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
                          ast.Lambda)) and n is not node and any(c is call for c in ast.walk(n)):
            return True
    return False


def _unconditional_in_loop(loop) -> bool:
    """_route_row runs on every iteration: it is in a TOP-LEVEL simple statement of the loop body (or of a with /
    re-raising try body there), not under a condition, and no earlier statement can skip it (continue / break /
    return anywhere in it)."""
    return _route_first(loop.body) is True


def _route_first(stmts):
    """True: an unconditional _route_row comes first; False: a conditional one or a possible skip comes first;
    None: neither (keep looking after these statements)."""
    for st in stmts:
        calls = _route_calls(st)
        inner = _always_runs(st)
        if inner is not None:
            got = _route_first(inner)
            if got is not None:
                return got
        elif calls and not isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With,
                                           ast.Match if hasattr(ast, "Match") else ast.If)):
            return not all(_conditional_inside(st, c) for c in calls)
        if any(isinstance(n, (ast.Continue, ast.Break, ast.Return)) for n in ast.walk(st)) or calls:
            return False
    return None


COMPOUND = tuple(t for t in (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, getattr(ast, "TryStar", None),
                             ast.With, ast.AsyncWith, getattr(ast, "Match", None),
                             ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef) if t is not None)


# compound statements whose body runs on some ticks only; a plain `if` is handled with its both-arms / plan-guard
# exemptions, and a nested def / lambda / class is skipped by _walk_eager (Prism 84d48881bad3). A plain with body
# always runs (a non-plain with is a guard, Prism 67a939c59339), and a re-raising try guards only its handlers
# (_always_runs, t_f87e058c).
GUARDS = tuple(t for t in (ast.While, ast.Try, getattr(ast, "TryStar", None), ast.For,
                           ast.AsyncFor, getattr(ast, "Match", None)) if t is not None)


def _ready_route_comp(n) -> bool:
    """A comprehension with one generator over `ready_rows` that calls _route_row."""
    return (isinstance(n, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)) and bool(_route_calls(n))
            and len(n.generators) == 1 and _over_ready(n.generators[0].iter))


def _leaves_outer_loop(st) -> bool:
    """`st` can skip the rest of the enclosing loop's iteration: a continue / break that belongs to that loop,
    or a return anywhere. A continue / break inside a nested loop is that loop's own."""
    def walk(n, own: bool, caught: bool = False) -> bool:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return False
        if (isinstance(n, ast.Return) or (own and isinstance(n, (ast.Continue, ast.Break)))
                or (isinstance(n, ast.Raise) and n.exc is not None and not caught)):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            # the nested loop's continue/break are its own; its else clause runs in the outer iteration
            return (any(walk(c, False, caught) for c in n.body)
                    or any(walk(c, own, caught) for c in n.orelse))
        if isinstance(n, TRY) and n.handlers and not _reraises(n):
            # a raise in the body may be caught by a handler (t_f87e058c X4)
            return (any(walk(c, own, True) for c in n.body)
                    or any(walk(c, own, caught) for c in [*n.handlers, *n.orelse, *n.finalbody]))
        return any(walk(c, own, caught) for c in ast.iter_child_nodes(n))
    return walk(st, True)


def _ends_outer_loop(st) -> bool:
    """`st` can END the enclosing loop: a break that belongs to it, or a return anywhere (a continue only ends
    the iteration, so it is harmless once the row is routed)."""
    def walk(n, own: bool, caught: bool = False) -> bool:
        if isinstance(n, DEFERRED):
            return False
        if (isinstance(n, ast.Return) or (own and isinstance(n, ast.Break))
                or (isinstance(n, ast.Raise) and n.exc is not None and not caught)):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            return any(walk(c, False, caught) for c in n.body) or any(walk(c, own, caught) for c in n.orelse)
        if isinstance(n, TRY) and n.handlers and not _reraises(n):
            return (any(walk(c, own, True) for c in n.body)
                    or any(walk(c, own, caught) for c in [*n.handlers, *n.orelse, *n.finalbody]))
        return any(walk(c, own, caught) for c in ast.iter_child_nodes(n))
    return walk(st, True)


def _block_routes(stmts) -> bool:
    """The statement list routes its row on every pass: a TOP-LEVEL simple statement writes row_route[...] or
    calls _route_row unconditionally, or a top-level if/else routes in BOTH arms; no earlier statement can leave
    the loop's iteration first, and no LATER statement can end the loop (a trailing break / return routes only
    the first row: Prism 19377e67cb32 / b89a0617821b)."""
    routed = False
    for st in stmts:
        if routed:
            if _ends_outer_loop(st):
                return False
            continue
        if not isinstance(st, COMPOUND):
            calls = _route_calls(st)
            if _writes_row_route(st) or (calls and not all(_conditional_inside(st, c) for c in calls)):
                routed = True
                continue
        elif isinstance(st, ast.If) and _block_routes(st.body) and _block_routes(st.orelse):
            routed = True
            continue
        elif _always_runs(st) is not None and _block_routes(_always_runs(st)):
            routed = True
            continue
        if _leaves_outer_loop(st):
            return False
    return routed


def _loop_routes_every_row(loop) -> bool:
    """A loop over the ready rows routes every row only if its body routes on every iteration (_block_routes).
    Prism 2e9888a37c28 (`for r in ready_rows: pass`) and 1d133dcecb68 (`if r["ok"]: row_route[...] = ...`)."""
    return _block_routes(loop.body)


def _bindings(fn, name: str) -> int:
    """How many times `name` is bound anywhere under `fn` (nested scopes included: conservative)."""
    n = 0
    for x in ast.walk(fn):
        if x is fn:
            continue
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            n += x.name == name
        elif isinstance(x, ast.Name):
            n += x.id == name and isinstance(x.ctx, (ast.Store, ast.Del))
        elif isinstance(x, ast.alias):
            n += (x.asname or x.name.split(".")[0]) == name
        elif isinstance(x, (ast.Global, ast.Nonlocal)):
            n += name in x.names
        elif isinstance(x, ast.ExceptHandler):
            n += x.name == name
    return n


def _called_helpers(fn, guarded: set) -> list:
    """Eager nodes of each nested def in `fn` that `fn` calls with no arguments, unconditionally: the call is in
    eager code, not under a guard, an IfExp / BoolOp or a comprehension. Only the helper's statements up to the
    first one that can return or raise count (the code after it may not run)."""
    # Prism 75ebb57d741f: calling an async def or a generator runs none of its body, and a name bound more than
    # once in fn (assign, loop target, walrus, with-as, import, global/nonlocal, except-as, another def) may no
    # longer be the def at the call
    # Prism a8f5e5d182f8: _walk_eager yields its root, so a nested def / class / lambda statement in the helper
    # body is skipped before the walk (its body runs only if called)
    def eager(body):
        return [b for b in body if not isinstance(b, DEFERRED)
                and not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Lambda))]
    defs = {st.name: st for st in fn.body if isinstance(st, ast.FunctionDef)
            and _bindings(fn, st.name) == 1
            and not any(isinstance(x, (ast.Yield, ast.YieldFrom)) for b in eager(st.body) for x in _walk_eager(b))}
    out = []
    for c in _walk_eager(fn):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in defs and not c.args
                and not c.keywords and id(c) not in guarded and not _conditional_inside(fn, c)):
            d = defs.pop(c.func.id)
            for st in eager(d.body):
                out += list(_walk_eager(st))
                if any(isinstance(n, (ast.Return, ast.Raise)) for n in _walk_eager(st)):
                    break
    return out


def check_dispatcher(root: Path) -> list[str]:
    rel = TARGETS["dispatcher"]
    p = root / rel
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as e:
        return [f"{rel}: dispatcher check cannot read its target ({e.__class__.__name__}): red, never skipped (S6)"]
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == SPAWN_FN), None)
    if fn is None:
        return [f"{rel}: {SPAWN_FN} not found: the dispatcher loop check inspects nothing (S6)"]
    out = []
    # the statement-level guards above every node: a routing comprehension nested under an `if`/`try`/`with`
    # that is not the function body runs only when that guard holds (Prism 26752da165d9: `if should_route:`
    # around the whole comprehension read clean while the spawn loop below it still ran every tick)
    # An `if` whose EVERY branch routes the ready rows (the live dispatcher: a planned-pool comprehension in the
    # body, a `for r in ready_rows: row_route[...] = ...` fallback in the else) is fine; an `if` with a routing
    # comprehension in one arm and no routing in the other (or no other arm) skips routing on some ticks.
    def _routes_ready(stmts) -> bool:
        # Only an UNCONDITIONAL route at the arm's own statement level counts (Prism 1d133dcecb68): a nested
        # `if cond: for r in ready_rows: ...` in the arm, or a loop body of `if r["ok"]: row_route[...] = ...`,
        # routes only some ticks / some rows. A nested if/else counts when BOTH of its arms route.
        for st in stmts:
            if isinstance(st, (ast.For, ast.AsyncFor)):
                if _over_ready(st.iter) and _loop_routes_every_row(st):
                    return True
            elif isinstance(st, ast.If):
                if _routes_ready(st.body) and _routes_ready(st.orelse):
                    return True
            elif _match_routes_every_case(st):
                return True
            elif _always_runs(st) is not None:
                if _routes_ready(_always_runs(st)):
                    return True
            elif not isinstance(st, COMPOUND) and any(_ready_route_comp(n) for n in _walk_eager(st)):
                return True
        return False
    def _match_routes_every_case(st) -> bool:
        """A `match` that ends in an irrefutable, unguarded `case _:` and whose EVERY case routes the ready rows
        runs one routing arm each tick, like an if/else whose both arms route (desk send-back on #7265). No
        wildcard, a guarded wildcard, or one case that does not route keeps it a guard."""
        if not (hasattr(ast, "Match") and isinstance(st, ast.Match) and st.cases):
            return False
        last = st.cases[-1]
        if not (isinstance(last.pattern, ast.MatchAs) and last.pattern.pattern is None and last.guard is None):
            return False
        return all(_routes_ready(c.body) for c in st.cases)
    def _passes_plan(c, plan: str) -> bool:
        """`_route_row(conn, row, <plan>, ...)` or `_route_row(conn, row, plan=<plan>, ...)`."""
        if len(c.args) >= 3:
            return isinstance(c.args[2], ast.Name) and c.args[2].id == plan
        return len(c.args) == 2 and any(k.arg == "plan" and isinstance(k.value, ast.Name) and k.value.id == plan
                                        for k in c.keywords)
    def _rebinds(n, name: str) -> bool:
        """A comprehension target in `n` binds `name`: the `plan` inside is that loop variable, not the guard's."""
        return any(isinstance(g, ast.comprehension) and any(isinstance(t, ast.Name) and t.id == name
                                                            for t in ast.walk(g.target))
                   for g in ast.walk(n))
    def _plan_routes(stmts, plan: str) -> bool:
        """The guard body routes every ready row with `plan`: a top-level ready_rows routing comprehension (eager
        code only, never inside a nested def / lambda) whose every _route_row passes the guard's own `plan`, or a
        nested if/else whose BOTH arms do."""
        for b in stmts:
            if isinstance(b, ast.If):
                if _plan_routes(b.body, plan) and _plan_routes(b.orelse, plan):
                    return True
            elif not isinstance(b, COMPOUND) and any(
                    _ready_route_comp(n) and not _rebinds(n, plan)
                    and all(_passes_plan(c, plan) for c in _route_calls(n))
                    for n in _walk_eager(b)):
                return True
        return False
    def _plan_guard(st) -> bool:
        """The live dispatcher's `if spillover is not None:` shape, and only that: `if <plan> is not None:` whose
        body routes every ready row with `<plan>` as the PLAN argument of _route_row (the third positional,
        `_route_row(conn, row, plan, ...)`, or `plan=<plan>`), AND whose else arm is the no-plan path: a top-level
        loop over the same ready rows that writes row_route[...] (pins wait; every other row takes the spawn
        loop's `row_route.get(id, ("local", None))` default). No else, `else: pass`, a guard on any other name
        (`if conn is not None:`), or a body that does not pass the guard name as the plan still marks the arms
        guarded (desk send-back on hermes-home#7231)."""
        t = st.test
        if not (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and len(t.ops) == 1
                and isinstance(t.ops[0], ast.IsNot) and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value is None):
            return False
        plan = t.left.id
        body_ok = _plan_routes(st.body, plan)
        else_ok = any(isinstance(b, (ast.For, ast.AsyncFor)) and _over_ready(b.iter) and _writes_row_route(b)
                      for b in st.orelse)
        return body_ok and else_ok
    guarded: set = set()
    for st in ast.walk(fn):
        if isinstance(st, ast.If) and not (_routes_ready(st.body) and _routes_ready(st.orelse)) \
                and not _plan_guard(st):
            # every node under the `if`, the else arm's own statements included (Prism d75c20691309:
            # `if skip: pass / else: for r in ready_rows: ...` routes only on the ticks that take the else)
            for inner in ast.walk(st):
                if inner is not st:
                    guarded.add(id(inner))
        elif ((isinstance(st, GUARDS) or (isinstance(st, (ast.With, ast.AsyncWith)) and not _plain_with(st)))
              and not _match_routes_every_case(st)):
            # a loop over anything but the ready rows may run 0 times, a for/while else clause, an except /
            # except* handler and a match case run only on some ticks (Prism 84d48881bad3). A for's iterable and
            # a match subject are evaluated every time the statement runs, so they are not under its guard.
            head = {id(getattr(st, f)) for f in ("iter", "subject") if getattr(st, f, None) is not None}
            if _reraises(st):
                # a re-raising try: an exception in the body aborts the whole tick, so only the handlers are
                # conditional (t_f87e058c (a))
                head |= {id(c) for c in st.body + st.orelse + st.finalbody}
            for c in ast.iter_child_nodes(st):
                if id(c) not in head:
                    for inner in ast.walk(c):
                        guarded.add(id(inner))
    # D1: _route_row presented for every ready row (eager code only: a comprehension inside a nested def or
    # lambda runs only if something calls it, so a helper def counts only where the function calls it
    # unconditionally, `x = helper()` at an unguarded spot: t_f87e058c X5)
    d1 = False
    for n in [*_walk_eager(fn), *_called_helpers(fn, guarded)]:
        if isinstance(n, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            calls = _route_calls(n)
            gens = n.generators
            if calls and len(gens) == 1 and _over_ready(gens[0].iter):
                if id(n) in guarded:
                    out.append(f"{rel}:{n.lineno}: {ROUTE_FN} comprehension over {READY} sits under a guard "
                               f"(if/try/with/loop/match): every ready row must be routed each tick, unconditionally")
                elif gens[0].ifs:
                    out.append(f"{rel}:{n.lineno}: {ROUTE_FN} comprehension over {READY} carries a filter: "
                               f"every ready row must be routed each tick")
                elif isinstance(n, ast.GeneratorExp) and not _eager_parent(fn, n):
                    out.append(f"{rel}:{n.lineno}: {ROUTE_FN} in an unconsumed generator over {READY}: creating it "
                               f"routes nothing; use a list/dict comprehension")
                elif all(_conditional_inside(n, c) for c in calls):
                    out.append(f"{rel}:{n.lineno}: {ROUTE_FN} in the comprehension over {READY} is conditional: "
                               f"every ready row must be routed each tick")
                d1 = True
        elif isinstance(n, ast.For) and _over_ready(n.iter) and any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == ROUTE_FN
                for c in ast.walk(n)):
            out += [f.replace("ready loop", f"{ROUTE_FN} loop") for f in _early_exits(n.body, rel, raises=True)]
            if id(n) in guarded:
                out.append(f"{rel}:{n.lineno}: {ROUTE_FN} loop over {READY} sits under a guard (if/try/with/loop/match): "
                           f"every ready row must be routed each tick, unconditionally (Prism 1d133dcecb68)")
            if not _unconditional_in_loop(n):
                out.append(f"{rel}:{n.lineno}: {ROUTE_FN} in the loop over {READY} is conditional or skippable: "
                           f"every ready row must be routed each tick")
            d1 = True
    if not d1:
        out.append(f"{rel}: no {ROUTE_FN} call over every {READY} in {SPAWN_FN}: the routing check inspects "
                   f"nothing (S6)")
    # D2: the spawn loop
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For) and _over_ready(n.iter)
             and not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == ROUTE_FN
                         for c in ast.walk(n.iter))]
    spawn_loops = [n for n in loops if len(n.body) > 3]   # the real loop, not a 1-line helper walk
    if not spawn_loops:
        out.append(f"{rel}: no spawn loop `for ... in {READY}` in {SPAWN_FN}: the loop check inspects nothing (S6)")
    for lp in spawn_loops:
        out += _early_exits(lp.body, rel)
    return out


# ---------------------------------------------------------------- pool (bash text)
SLOT_LOOP = re.compile(r'^\s*while\b.*"\$i"\s+-le\s+"\$effective_pool".*;\s*do\s*(?:#.*)?$')
OPEN_KW = re.compile(r"(?:^|[\s;])(?:do|then)\s*(?:#.*)?$|^\s*(?:while|until|for|if|case)\b")
SH_TOKEN = re.compile(r"&&|\|\||;;|[;&|()]|[^\s;&|()]+")
SH_OPS = frozenset(("&&", "||", ";;", ";", "&", "|", "(", ")"))
SH_PREFIX = frozenset(("!", "{", "}", "then", "else", "elif", "do", "time"))


def _strip(line: str) -> str:
    """Drop a trailing comment and quoted strings, so `echo "break"` is not a break."""
    s = re.sub(r"'[^']*'", "''", line)
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
    return re.sub(r"(^|\s)#.*$", "", s)


def check_pool(root: Path) -> list[str]:
    rel = TARGETS["pool"]
    try:
        lines = (root / rel).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return [f"{rel}: pool check cannot read its target ({e.__class__.__name__}): red, never skipped (S6)"]
    starts = [i for i, ln in enumerate(lines) if SLOT_LOOP.match(ln)]
    if not starts:
        return [f"{rel}: the slot loop (`while ... \"$i\" -le \"$effective_pool\" ...; do`) not found: the check "
                f"inspects nothing (S6)"]
    out = []
    for s in starts:
        depth = 1   # 1 = the slot loop's own body; each `do` opens a level, each `done` closes one
        end = None
        for j in range(s + 1, len(lines)):
            cmd_pos = True
            toks = SH_TOKEN.findall(_strip(lines[j]))
            k = 0
            while k < len(toks):
                tok = toks[k]
                if tok in SH_OPS:
                    cmd_pos = True
                elif cmd_pos and tok in SH_PREFIX:
                    pass   # `! cmd`, `{ cmd`, `then cmd`, `do cmd`: the next word is still a command
                elif cmd_pos:
                    cmd_pos = False
                    if tok == "done":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                    elif tok in ("break", "continue", "return", "exit"):
                        arg = toks[k + 1] if k + 1 < len(toks) and toks[k + 1] not in SH_OPS else None
                        # `break N` leaves N loops: it ends the slot loop when N >= the current depth.
                        # `continue N` resumes the Nth enclosing loop: at N >= depth that is the OUTER tick loop,
                        # which abandons the remaining slots exactly like a break (Prism cda9416d1d9f). A plain
                        # `continue` (N=1) is the sanctioned refused-slot shape. A non-numeric N fails closed;
                        # return/exit always end it.
                        n_lvl = 1 if arg is None else (int(arg) if arg.isdigit() else 10 ** 6)
                        # `continue N` at nesting depth d targets the Nth enclosing loop; N <= d stays inside the
                        # slot loop (N == d IS the slot loop: the sanctioned refused-slot shape), N > d is the tick loop.
                        if tok == "continue" and n_lvl <= depth:
                            pass
                        elif tok not in ("break", "continue") or n_lvl >= depth:
                            out.append(f"{rel}:{j + 1}: `{tok}` inside the slot loop ends the tick early: a refused "
                                       f"slot must be `i=$((i + 1)); continue` so every slot is presented (§4.2)")
                if tok == "do":
                    depth += 1
                    cmd_pos = True
                k += 1
            if end is not None:
                break
        if end is None:
            out.append(f"{rel}:{s + 1}: slot loop has no matching `done`: cannot inspect it (S6)")
    return out


CHECKS = {"dispatcher": check_dispatcher, "pool": check_pool}


def lint(root: Path, checks: list[str]) -> list[str]:
    out = []
    for c in checks:
        out += CHECKS[c](root)
    return out


# ---------------------------------------------------------------- selftest
DISPATCH_OK = '''
def _dispatch_once_locked(conn, ready_rows, route_first):
    row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}
    record_only = None
    for row_index, row in enumerate(ready_rows):
        if record_only is not None:
            continue
        if budget_gone():
            if route_first:
                record_only = "budget"
                continue
            break
        for rest in ready_rows[row_index + 1:]:
            if rest:
                break
        spawn(row)
'''
DISPATCH_409_BREAK = DISPATCH_OK.replace("        spawn(row)\n", "        if admit(row) == 409:\n            break\n"
                                                                   "        spawn(row)\n")
DISPATCH_RETURN = DISPATCH_OK.replace("        spawn(row)\n", "        if refused(row):\n            return\n"
                                                                "        spawn(row)\n")
DISPATCH_WRAPPED = DISPATCH_OK.replace(
    '    row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '    row_route = {}\n    if should_route:\n        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n', 1)
DISPATCH_BOTH_ARMS = DISPATCH_OK.replace(
    '    row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '    if planned:\n        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    '    else:\n        row_route = {}\n        for r in ready_rows:\n            row_route[r["id"]] = ("wait", "pool_unavailable")\n', 1)
# Prism 2e9888a37c28: a ready_rows loop in the other arm counts only if its body routes
DISPATCH_WRAPPED_EMPTY_ELSE = DISPATCH_WRAPPED.replace(
    '        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    '    else:\n        for r in ready_rows:\n            pass\n', 1)
DISPATCH_BOTH_ARMS_OTHER_MAP = DISPATCH_BOTH_ARMS.replace('            row_route[r["id"]] = ("wait"',
                                                          '            seen[r["id"]] = ("wait"', 1)
DISPATCH_FILTER = DISPATCH_OK.replace("for r in ready_rows}", "for r in ready_rows if r['ok']}")
DISPATCH_NO_ROUTE = DISPATCH_OK.replace('    row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n', "")
# Prism 1d133dcecb68: only an UNCONDITIONAL route at the arm's own statement level counts
_COMP = '    row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
DISPATCH_ELSE_NESTED_IF = DISPATCH_OK.replace(_COMP, (
    '    if planned:\n        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    '    else:\n        row_route = {}\n        if cond:\n            for r in ready_rows:\n'
    '                row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_ELSE_COND_BODY = DISPATCH_OK.replace(_COMP, (
    '    if planned:\n        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    '    else:\n        row_route = {}\n        for r in ready_rows:\n            if r["ok"]:\n'
    '                row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_GUARDED_FOR = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    if should_route:\n        for r in ready_rows:\n'
    '            row_route[r["id"]] = _route_row(conn, r)\n'), 1)
# the live dispatcher: `if spillover is not None:` routes every row with the plan; the no-plan else arm writes
# only pinned rows and the spawn loop's row_route.get(id, ("local", None)) default covers the rest
DISPATCH_PLAN_GUARD = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    if plan is not None:\n'
    '        row_route = {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n'
    '    else:\n        for r in ready_rows:\n            if pinned(r):\n'
    '                row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_PLAN_GUARD_NOT_AN_ARG = DISPATCH_PLAN_GUARD.replace("_route_row(conn, r, plan)", "_route_row(conn, r)", 1)
# desk send-back on #7231: the plan-guard exemption is the live shape only (plan arg + a no-plan else loop)
DISPATCH_CONN_GUARD_NO_ELSE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    if conn is not None:\n'
    '        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'), 1)
DISPATCH_PLAN_GUARD_NO_ELSE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    if plan is not None:\n'
    '        row_route = {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n'), 1)
DISPATCH_PLAN_GUARD_ELSE_PASS = DISPATCH_PLAN_GUARD_NO_ELSE.replace(
    'for r in ready_rows}\n', 'for r in ready_rows}\n    else:\n        pass\n', 1)
DISPATCH_CONN_GUARD_WITH_ELSE = DISPATCH_PLAN_GUARD_NOT_AN_ARG.replace("if plan is not None:", "if conn is not None:", 1)
_ELSE_LOOP = '    else:\n        row_route = {}\n        for r in ready_rows:\n'
_PLANNED = '    if planned:\n        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
DISPATCH_ELSE_IF_BOTH_BRANCHES = DISPATCH_OK.replace(_COMP, _PLANNED + _ELSE_LOOP + (
    '            if r["ok"]:\n                row_route[r["id"]] = ("local", None)\n'
    '            else:\n                row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_ELSE_INNER_LOOP_CONTINUE = DISPATCH_OK.replace(_COMP, _PLANNED + _ELSE_LOOP + (
    '            for d in r["deps"]:\n                if not d:\n                    continue\n'
    '            row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_ELSE_CONTINUE_FILTER = DISPATCH_OK.replace(_COMP, _PLANNED + _ELSE_LOOP + (
    '            if not r["ok"]:\n                continue\n'
    '            row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
# desk r2 on #7231: pin each _plan_guard / _routes_ready / _block_routes condition with a fixture of its own
DISPATCH_CONN_GUARD_PLAN_ARG = DISPATCH_PLAN_GUARD.replace("if plan is not None:", "if conn is not None:", 1)
DISPATCH_PLAN_GUARD_ELSE_OTHER_ROWS = DISPATCH_PLAN_GUARD.replace(
    '    else:\n        for r in ready_rows:\n            if pinned(r):\n                row_route',
    '    else:\n        for r in pinned_rows:\n            if pinned(r):\n                row_route', 1)
DISPATCH_PLAN_GUARD_ELSE_NO_WRITE = DISPATCH_PLAN_GUARD.replace(
    '            if pinned(r):\n                row_route[r["id"]] = ("wait", "pool_unavailable")\n',
    '            log(r)\n', 1)
DISPATCH_PLAN_GUARD_NESTED_IF = DISPATCH_PLAN_GUARD.replace(
    '    if plan is not None:\n        row_route = {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n',
    '    if plan is not None:\n        if cond:\n            row_route = {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n', 1)
# a compound wrapper that does not itself mark a guard (a for over anything but ready_rows may run 0 times)
DISPATCH_PLAN_GUARD_NESTED_FOR = DISPATCH_PLAN_GUARD_NESTED_IF.replace(
    '        if cond:\n            row_route', '        for _ in attempts:\n            row_route', 1)
DISPATCH_PLAN_GUARD_IS_NONE = DISPATCH_PLAN_GUARD.replace("if plan is not None:", "if plan is None:", 1)
DISPATCH_PLAN_GUARD_NOT_FALSE = DISPATCH_PLAN_GUARD.replace("if plan is not None:", "if plan is not False:", 1)
DISPATCH_ELSE_INNER_LOOP_ELSE_CONTINUE = DISPATCH_OK.replace(_COMP, _PLANNED + _ELSE_LOOP + (
    '            for d in r["deps"]:\n                pass\n            else:\n                continue\n'
    '            row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_ARM_COMP_IN_DEF = DISPATCH_OK.replace(_COMP, (
    '    if planned:\n        def later():\n'
    '            return {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    '    else:\n        row_route = {}\n        for r in ready_rows:\n'
    '            row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_ELSE_LOOP_OTHER_ROWS = DISPATCH_BOTH_ARMS.replace(
    '        for r in ready_rows:\n            row_route[r["id"]] = ("wait"',
    '        for r in pinned_rows:\n            row_route[r["id"]] = ("wait"', 1)
DISPATCH_ELSE_LOOP_ROUTE_CALL = DISPATCH_OK.replace(_COMP, _PLANNED + _ELSE_LOOP + (
    '            _route_row(conn, r)\n'), 1)
# t_0266053d: plan guard over-strict shapes, a trailing loop exit, the else-arm guard marking, inner-scope `plan`
_ELSE_PIN = ('    else:\n        for r in ready_rows:\n            if pinned(r):\n'
             '                row_route[r["id"]] = ("wait", "pool_unavailable")\n')
DISPATCH_PLAN_GUARD_NESTED_IF_ELSE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    if plan is not None:\n        if fast:\n'
    '            row_route = {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n        else:\n'
    '            row_route = {r["id"]: _route_row(conn, r, plan, 1) for r in ready_rows}\n') + _ELSE_PIN, 1)
DISPATCH_PLAN_GUARD_NESTED_IF_ELSE_ONE_ARM = DISPATCH_PLAN_GUARD_NESTED_IF_ELSE.replace(
    '            row_route = {r["id"]: _route_row(conn, r, plan, 1) for r in ready_rows}\n',
    '            row_route = {}\n', 1)
DISPATCH_PLAN_GUARD_KWARG = DISPATCH_PLAN_GUARD.replace("_route_row(conn, r, plan)", "_route_row(conn, r, plan=plan)", 1)
DISPATCH_PLAN_GUARD_KWARG_OTHER = DISPATCH_PLAN_GUARD.replace("_route_row(conn, r, plan)",
                                                              "_route_row(conn, r, plan=conn)", 1)
DISPATCH_PLAN_GUARD_COMP_REBINDS = DISPATCH_PLAN_GUARD.replace(
    '{r["id"]: _route_row(conn, r, plan) for r in ready_rows}',
    '{plan["id"]: _route_row(conn, plan, plan) for plan in ready_rows}', 1)
DISPATCH_PLAN_GUARD_LAMBDA = DISPATCH_PLAN_GUARD.replace(
    '        row_route = {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n',
    '        row_route = (lambda plan: {r["id"]: _route_row(conn, r, plan) for r in ready_rows})\n', 1)
DISPATCH_UNCALLED_DEF = DISPATCH_OK.replace(_COMP, (
    '    def later(plan):\n        return {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n'
    '    row_route = {}\n'), 1)
DISPATCH_HELPER_UNDER_IF = DISPATCH_OK.replace(_COMP, (
    '    def _route_all():\n        return {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    '    row_route = {}\n    if cond:\n        row_route = _route_all()\n'), 1)
_ELSE_ROUTE = '            row_route[r["id"]] = ("wait", "pool_unavailable")\n'
DISPATCH_ELSE_ROUTE_THEN_BREAK = DISPATCH_BOTH_ARMS.replace(_ELSE_ROUTE, _ELSE_ROUTE + '            break\n', 1)
DISPATCH_ELSE_ROUTE_THEN_RETURN = DISPATCH_BOTH_ARMS.replace(_ELSE_ROUTE, _ELSE_ROUTE + '            return\n', 1)
DISPATCH_ELSE_ROUTE_THEN_CONTINUE = DISPATCH_BOTH_ARMS.replace(_ELSE_ROUTE, _ELSE_ROUTE + '            continue\n', 1)
DISPATCH_ELSE_ROUTE_THEN_INNER_BREAK = DISPATCH_BOTH_ARMS.replace(
    _ELSE_ROUTE, _ELSE_ROUTE + '            for d in r["deps"]:\n                break\n', 1)
DISPATCH_SKIP_ELSE_ROUTES = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    if skip:\n        pass\n    else:\n        for r in ready_rows:\n'
    '            row_route[r["id"]] = _route_row(conn, r)\n'), 1)
DISPATCH_BOTH_ARMS_NESTED_IF_ELSE = DISPATCH_BOTH_ARMS.replace(
    '    if planned:\n        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '    if planned:\n        if fast:\n            row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    '        else:\n            row_route = {r["id"]: _route_row(conn, r, 1) for r in ready_rows}\n', 1)
# the plan only reaches _route_row through an uncalled lambda; the comprehension that runs routes without it
DISPATCH_PLAN_GUARD_PLAN_ONLY_IN_LAMBDA = DISPATCH_PLAN_GUARD_NOT_AN_ARG.replace(
    '        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '        later = lambda: {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n'
    '        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n', 1)
# desk r2 on #7256: the routing arm's comprehension is only in deferred code (a def body, a lambda); the else arm
# routes with _route_row in a ready_rows loop, so only the arm check can turn the `if` into a guard
_ELSE_ROUTE_CALL = ('    else:\n        row_route = {}\n        for r in ready_rows:\n'
                    '            row_route[r["id"]] = _route_row(conn, r)\n')
DISPATCH_ARM_DEF_ELSE_ROUTE_CALL = DISPATCH_OK.replace(_COMP, (
    '    if planned:\n        def later():\n'
    '            return {r["id"]: _route_row(conn, r) for r in ready_rows}\n') + _ELSE_ROUTE_CALL, 1)
DISPATCH_ARM_LAMBDA_ELSE_ROUTE_CALL = DISPATCH_OK.replace(_COMP, (
    '    if planned:\n        later = lambda: {r["id"]: _route_row(conn, r) for r in ready_rows}\n') + _ELSE_ROUTE_CALL, 1)
DISPATCH_ELSE_ROUTE_THEN_DEF_RETURN = DISPATCH_BOTH_ARMS.replace(
    _ELSE_ROUTE, _ELSE_ROUTE + '            def cb():\n                return r\n', 1)
# Prism 84d48881bad3: a routing loop / comprehension under a compound statement that runs on some ticks only
_ROUTE_FOR = '        for r in ready_rows:\n            row_route[r["id"]] = _route_row(conn, r)\n'
DISPATCH_OTHER_LOOP_FOR = DISPATCH_OK.replace(_COMP, '    row_route = {}\n    for _ in attempts:\n' + _ROUTE_FOR, 1)
DISPATCH_OTHER_LOOP_COMP = DISPATCH_OK.replace(_COMP, '    for _ in attempts:\n    ' + _COMP, 1)
DISPATCH_LOOP_ELSE_FOR = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    for _ in attempts:\n        pass\n    else:\n') + _ROUTE_FOR, 1)
DISPATCH_MATCH_FOR = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    match mode:\n        case 1:\n') + _ROUTE_FOR.replace('        ', '            '), 1)
DISPATCH_TRYSTAR_FOR = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    try:\n        pass\n    except* ValueError:\n') + _ROUTE_FOR, 1)
DISPATCH_ASYNC_FOR = DISPATCH_OK.replace(_COMP, '    row_route = {}\n    async for _ in attempts:\n' + _ROUTE_FOR, 1)
_CASE_FOR = _ROUTE_FOR.replace('        ', '            ')
DISPATCH_MATCH_ALL_ROUTE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    match mode:\n        case 1:\n') + _CASE_FOR + '        case _:\n' + _CASE_FOR, 1)
DISPATCH_MATCH_ALL_ROUTE_NO_WILD = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    match mode:\n        case 1:\n') + _CASE_FOR + '        case 2:\n' + _CASE_FOR, 1)
DISPATCH_MATCH_ONE_CASE_SKIPS = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    match mode:\n        case 1:\n') + _CASE_FOR + '        case _:\n            pass\n', 1)
DISPATCH_IF_ARM_MATCH_ALL_ROUTE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    if planned:\n        match mode:\n            case 1:\n')
    + _CASE_FOR.replace('            ', '                ') + '            case _:\n'
    + _CASE_FOR.replace('            ', '                ') + '    else:\n' + _ROUTE_FOR, 1)
# a for's iterable runs whenever the for does: routing there is not under the for's guard
DISPATCH_ROUTE_IN_FOR_ITER = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    for rid, rt in [(r["id"], _route_row(conn, r)) for r in ready_rows]:\n'
    '        row_route[rid] = rt\n'), 1)
# t_f87e058c: pre-existing strictness shapes. (a) a try whose every handler ends in a bare `raise` aborts the tick
# on failure, so routing in its body is not skipped while the spawn loop runs; (b) a with body always runs
_TRY_BODY_FOR = _ROUTE_FOR.replace('        ', '            ')
DISPATCH_TRY_RERAISE_FOR = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    try:\n') + _TRY_BODY_FOR + '    except Exception:\n        log()\n        raise\n', 1)
DISPATCH_TRY_RERAISE_COMP = DISPATCH_OK.replace(_COMP, (
    '    try:\n    ' + _COMP + '    except Exception:\n        raise\n'), 1)
DISPATCH_TRYSTAR_RERAISE_FOR = DISPATCH_TRY_RERAISE_FOR.replace("except Exception:", "except* Exception:", 1)
DISPATCH_TRY_SWALLOW_FOR = DISPATCH_TRY_RERAISE_FOR.replace("        log()\n        raise\n", "        log()\n", 1)
DISPATCH_TRY_RERAISE_ONE_HANDLER_SWALLOWS = DISPATCH_TRY_RERAISE_FOR.replace(
    "    except Exception:\n", "    except KeyError:\n        pass\n    except Exception:\n", 1)
DISPATCH_TRY_RAISE_OTHER = DISPATCH_TRY_RERAISE_FOR.replace("        raise\n", "        raise RuntimeError()\n", 1)
DISPATCH_WITH_FOR = DISPATCH_OK.replace(_COMP, '    row_route = {}\n    with lk:\n' + _ROUTE_FOR, 1)
DISPATCH_WITH_COMP = DISPATCH_OK.replace(_COMP, '    with lk:\n    ' + _COMP, 1)
DISPATCH_WITH_IF_FOR = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    with lk:\n        if cond:\n') + _TRY_BODY_FOR, 1)
DISPATCH_ELSE_LOOP_WITH_ROUTE = DISPATCH_OK.replace(_COMP, _PLANNED + _ELSE_LOOP + (
    '            with lk:\n                row_route[r["id"]] = ("wait", "pool_unavailable")\n'), 1)
DISPATCH_LOOP_WITH_ROUTE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    for r in ready_rows:\n        with lk:\n'
    '            row_route[r["id"]] = _route_row(conn, r)\n'), 1)
DISPATCH_LOOP_TRY_RERAISE_ROUTE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    for r in ready_rows:\n        try:\n'
    '            row_route[r["id"]] = _route_row(conn, r)\n        except Exception:\n            raise\n'), 1)
DISPATCH_LOOP_TRY_CONTINUE_THEN_RAISE = DISPATCH_LOOP_TRY_RERAISE_ROUTE.replace(
    '        except Exception:\n            raise\n',
    '        except Exception:\n            if soft:\n                continue\n            raise\n', 1)
DISPATCH_IF_ARM_WITH_COMP = DISPATCH_OK.replace(_COMP, (
    '    if planned:\n        with lk:\n            row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n'
    + _ELSE_ROUTE_CALL), 1)
# (c) X3: an inner loop's else clause runs in the routing loop's iteration, so its break ends the routing loop;
# X4: a raise after the route ends the routing loop; X5: a helper def'd then called unconditionally routes
DISPATCH_LOOP_ROUTE_INNER_ELSE_BREAK = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    for r in ready_rows:\n        row_route[r["id"]] = _route_row(conn, r)\n'
    '        for d in r["deps"]:\n            pass\n        else:\n            break\n'), 1)
DISPATCH_LOOP_ROUTE_THEN_RAISE = DISPATCH_OK.replace(_COMP, (
    '    row_route = {}\n    for r in ready_rows:\n        row_route[r["id"]] = _route_row(conn, r)\n'
    '        if bad(r):\n            raise ValueError(r)\n'), 1)
DISPATCH_LOOP_ROUTE_CAUGHT_RAISE = DISPATCH_LOOP_ROUTE_THEN_RAISE.replace(
    '        if bad(r):\n            raise ValueError(r)\n',
    '        try:\n            raise ValueError(r)\n        except ValueError:\n            pass\n', 1)
DISPATCH_ELSE_ROUTE_THEN_RAISE = DISPATCH_BOTH_ARMS.replace(_ELSE_ROUTE, _ELSE_ROUTE + '            raise ValueError\n', 1)
DISPATCH_ELSE_ROUTE_THEN_INNER_ELSE_BREAK = DISPATCH_BOTH_ARMS.replace(
    _ELSE_ROUTE, _ELSE_ROUTE + '            for d in r["deps"]:\n                pass\n            else:\n                break\n', 1)
DISPATCH_ELSE_ROUTE_THEN_RERAISE_TRY = DISPATCH_BOTH_ARMS.replace(
    _ELSE_ROUTE, _ELSE_ROUTE + '            try:\n                note(r)\n            except Exception:\n                raise\n', 1)
DISPATCH_ELSE_RAISE_THEN_ROUTE = DISPATCH_BOTH_ARMS.replace(
    _ELSE_ROUTE, '            if bad(r):\n                raise ValueError(r)\n' + _ELSE_ROUTE, 1)
DISPATCH_ELSE_RERAISE_TRY_THEN_ROUTE = DISPATCH_BOTH_ARMS.replace(
    _ELSE_ROUTE, '            try:\n                note(r)\n            except Exception:\n                raise\n' + _ELSE_ROUTE, 1)
DISPATCH_ELSE_CAUGHT_RAISE_THEN_ROUTE = DISPATCH_BOTH_ARMS.replace(
    _ELSE_ROUTE, '            try:\n                raise ValueError(r)\n            except ValueError:\n                pass\n'
    + _ELSE_ROUTE, 1)
DISPATCH_HELPER_CALLED = DISPATCH_HELPER_UNDER_IF.replace('    if cond:\n        row_route = _route_all()\n',
                                                          '    row_route = _route_all()\n', 1)
DISPATCH_HELPER_CALLED_IFEXP = DISPATCH_HELPER_UNDER_IF.replace('    if cond:\n        row_route = _route_all()\n',
                                                                '    row_route = _route_all() if cond else {}\n', 1)
DISPATCH_HELPER_EARLY_RETURN = DISPATCH_HELPER_CALLED.replace(
    '    def _route_all():\n', '    def _route_all():\n        if cond:\n            return {}\n', 1)
# (d) Na2: a plan-guard body if/else where only ONE arm passes the plan (the other routes without it); M12r: the
# plan reaches _route_row only in an uncalled nested def in the guard body
DISPATCH_PLAN_GUARD_NESTED_IF_ELSE_NO_PLAN_ARM = DISPATCH_PLAN_GUARD_NESTED_IF_ELSE.replace(
    '_route_row(conn, r, plan, 1)', '_route_row(conn, r)', 1)
DISPATCH_PLAN_GUARD_PLAN_ONLY_IN_DEF = DISPATCH_PLAN_GUARD_NOT_AN_ARG.replace(
    '        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '        def later():\n            return {r["id"]: _route_row(conn, r, plan) for r in ready_rows}\n'
    '        row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n', 1)
# Prism 67a939c59339: a with whose manager is a call (contextlib.suppress) may swallow an error mid-routing
DISPATCH_WITH_SUPPRESS_FOR = DISPATCH_WITH_FOR.replace("    with lk:\n", "    with contextlib.suppress(Exception):\n", 1)
DISPATCH_WITH_SUPPRESS_COMP = DISPATCH_WITH_COMP.replace("    with lk:\n", "    with suppress(Exception):\n", 1)
DISPATCH_WITH_LK_SUPPRESS_FOR = DISPATCH_WITH_FOR.replace("    with lk:\n", "    with lk, suppress(Exception):\n", 1)
DISPATCH_LOOP_WITH_SUPPRESS_ROUTE = DISPATCH_LOOP_WITH_ROUTE.replace("        with lk:\n",
                                                                     "        with suppress(Exception):\n", 1)
# Prism 75ebb57d741f: calling an async def / generator runs none of its body; a rebound name is not the def
DISPATCH_HELPER_ASYNC = DISPATCH_HELPER_CALLED.replace("    def _route_all():\n", "    async def _route_all():\n", 1)
DISPATCH_HELPER_YIELD = DISPATCH_HELPER_CALLED.replace("        return {r[\"id\"]", "        yield {r[\"id\"]", 1)
_HELPER_CALL = "    row_route = _route_all()\n"
DISPATCH_HELPER_REBOUND_LAMBDA = DISPATCH_HELPER_CALLED.replace(_HELPER_CALL, "    _route_all = lambda: {}\n" + _HELPER_CALL, 1)
DISPATCH_HELPER_REBOUND_NAME = DISPATCH_HELPER_CALLED.replace(_HELPER_CALL, "    _route_all = noop\n" + _HELPER_CALL, 1)
DISPATCH_HELPER_REBOUND_FOR = DISPATCH_HELPER_CALLED.replace(
    _HELPER_CALL, "    for _route_all in hooks:\n        pass\n" + _HELPER_CALL, 1)
DISPATCH_HELPER_REBOUND_IMPORT = DISPATCH_HELPER_CALLED.replace(
    _HELPER_CALL, "    from x import noop as _route_all\n" + _HELPER_CALL, 1)
DISPATCH_HELPER_REBOUND_WALRUS = DISPATCH_HELPER_CALLED.replace(
    _HELPER_CALL, "    if (_route_all := noop):\n        pass\n" + _HELPER_CALL, 1)
# Prism 34a70a667291: a finally that continues / returns discards the handlers' re-raise, so the tick goes on
DISPATCH_LOOP_TRY_RERAISE_FINALLY_CONTINUE = DISPATCH_LOOP_TRY_RERAISE_ROUTE.replace(
    '        except Exception:\n            raise\n',
    '        except Exception:\n            raise\n        finally:\n            continue\n', 1)
DISPATCH_TRY_RERAISE_FINALLY_RETURN = DISPATCH_TRY_RERAISE_FOR.replace(
    "        raise\n", "        raise\n    finally:\n        return\n", 1)
DISPATCH_TRY_RERAISE_FINALLY_LOG = DISPATCH_TRY_RERAISE_FOR.replace(
    "        raise\n", "        raise\n    finally:\n        log()\n", 1)
# Prism a8f5e5d182f8: a nested def in the called helper's body routes nothing unless something calls it
DISPATCH_HELPER_NESTED_DEF_ONLY = DISPATCH_HELPER_CALLED.replace(
    '        return {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '        def later():\n            return {r["id"]: _route_row(conn, r) for r in ready_rows}\n        return {}\n', 1)
POOL_OK = '''#!/usr/bin/env bash
while true; do
  i=1
  while [ "$effective_pool" -ge 1 ] && [ "$i" -le "$effective_pool" ]; do
    if slot_free "$i"; then
      if [ "$fits" -lt 1 ]; then
        echo "refuse, do not break"
        i=$((i + 1))
        continue
      fi
      for k in 1 2; do [ "$k" = 2 ] && break; done
      spawn "$i" &
    fi
    i=$((i + 1))
  done
  sleep 3
done
'''
POOL_CONTINUE2 = POOL_OK.replace("        continue\n", "        continue 2\n", 1)   # continues `while true`, abandons the tick
DISPATCH_COND_ROUTE = DISPATCH_OK.replace(
    '    row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n',
    '    row_route = {}\n    for r in ready_rows:\n        if r["ok"]:\n            row_route[r["id"]] = _route_row(conn, r)\n')
DISPATCH_LOOP_ROUTE = DISPATCH_COND_ROUTE.replace(
    '        if r["ok"]:\n            row_route[r["id"]] = _route_row(conn, r)\n',
    '        row_route[r["id"]] = _route_row(conn, r)\n')
DISPATCH_IFEXP_ROUTE = DISPATCH_OK.replace("_route_row(conn, r) for r", "(_route_row(conn, r) if r['ok'] else None) for r")
DISPATCH_GUARD_BODY_BREAK = DISPATCH_OK.replace(
    '                record_only = "budget"\n', '                if refused(row):\n                    break\n'
    '                record_only = "budget"\n')
DISPATCH_LAZY_GEN = DISPATCH_OK.replace('{r["id"]: _route_row(conn, r) for r in ready_rows}',
                                        '(_route_row(conn, r) for r in ready_rows)')
DISPATCH_EAGER_GEN = DISPATCH_OK.replace('{r["id"]: _route_row(conn, r) for r in ready_rows}',
                                         'list(_route_row(conn, r) for r in ready_rows)')
POOL_409_BREAK = POOL_OK.replace("        i=$((i + 1))\n        continue\n", "        break\n")
POOL_BREAK2 = POOL_OK.replace('[ "$k" = 2 ] && break; done', '[ "$k" = 2 ] && break 2; done')
POOL_BREAK2_NESTED = POOL_OK.replace('      for k in 1 2; do [ "$k" = 2 ] && break; done\n',
                                     '      for k in 1 2; do\n        [ "$k" = 2 ] && break 2\n      done\n')
POOL_BREAK1_NESTED = POOL_BREAK2_NESTED.replace("break 2", "break")
POOL_EXIT_AFTER_NESTED = POOL_OK.replace('[ "$k" = 2 ] && break; done', '[ "$k" = 2 ] && break; done; exit 1')
POOL_BREAK_OR_TRUE = POOL_OK.replace("        i=$((i + 1))\n        continue\n", "        break || true\n")
POOL_EXIT = POOL_OK.replace('      spawn "$i" &\n', '      spawn "$i" || exit 1\n')


def selftest() -> int:
    def tree(files: dict) -> Path:
        d = Path(tempfile.mkdtemp(prefix="queue-loops-"))
        for n, b in files.items():
            (d / n).parent.mkdir(parents=True, exist_ok=True)
            (d / n).write_text(b)
        return d

    D, P = TARGETS["dispatcher"], TARGETS["pool"]
    cases = [
        ("S6: empty root, dispatcher check is RED", {}, ["dispatcher"], 1),
        ("S6: empty root, pool check is RED", {}, ["pool"], 1),
        ("dispatcher: route_first-guarded break + nested-loop break is GREEN", {D: DISPATCH_OK}, ["dispatcher"], 0),
        ("dispatcher: planted break on the first 409 is RED", {D: DISPATCH_409_BREAK}, ["dispatcher"], 1),
        ("dispatcher: return inside the ready loop is RED", {D: DISPATCH_RETURN}, ["dispatcher"], 1),
        ("dispatcher: filtered _route_row comprehension is RED", {D: DISPATCH_FILTER}, ["dispatcher"], 1),
        ("dispatcher: routing comprehension wrapped in `if should_route:` is RED (Prism 26752da165d9)", {D: DISPATCH_WRAPPED}, ["dispatcher"], 1),
        ("dispatcher: if/else where BOTH arms route every ready row is GREEN (the live dispatcher shape)", {D: DISPATCH_BOTH_ARMS}, ["dispatcher"], 0),
        ("dispatcher: `else: for r in ready_rows: pass` does not count as routing, RED (Prism 2e9888a37c28)",
         {D: DISPATCH_WRAPPED_EMPTY_ELSE}, ["dispatcher"], 1),
        ("dispatcher: else-arm ready_rows loop writing another map, not row_route, is RED (Prism 2e9888a37c28)",
         {D: DISPATCH_BOTH_ARMS_OTHER_MAP}, ["dispatcher"], 1),
        ("dispatcher: else-arm `if cond:` around a routing ready_rows loop is RED (Prism 1d133dcecb68)",
         {D: DISPATCH_ELSE_NESTED_IF}, ["dispatcher"], 1),
        ("dispatcher: else-arm ready_rows loop routing only `if r[\"ok\"]` is RED (Prism 1d133dcecb68)",
         {D: DISPATCH_ELSE_COND_BODY}, ["dispatcher"], 1),
        ("dispatcher: routing for-loop wrapped in `if should_route:` is RED (Prism 1d133dcecb68)",
         {D: DISPATCH_GUARDED_FOR}, ["dispatcher"], 1),
        ("dispatcher: `if plan is not None:` routing with plan, pinned-only no-plan else, is GREEN (live shape)",
         {D: DISPATCH_PLAN_GUARD}, ["dispatcher"], 0),
        ("dispatcher: `if X is not None:` where X is not a _route_row argument is RED (Prism 1d133dcecb68)",
         {D: DISPATCH_PLAN_GUARD_NOT_AN_ARG}, ["dispatcher"], 1),
        ("dispatcher: `if conn is not None:` around the routing comprehension, no else, is RED (#7231 desk)",
         {D: DISPATCH_CONN_GUARD_NO_ELSE}, ["dispatcher"], 1),
        ("dispatcher: `if plan is not None:` routing with plan, no else, is RED (#7231 desk)",
         {D: DISPATCH_PLAN_GUARD_NO_ELSE}, ["dispatcher"], 1),
        ("dispatcher: `if plan is not None:` routing with plan, `else: pass`, is RED (#7231 desk)",
         {D: DISPATCH_PLAN_GUARD_ELSE_PASS}, ["dispatcher"], 1),
        ("dispatcher: `if conn is not None:` (an argument, not the plan) with a no-plan else loop is RED (#7231 desk)",
         {D: DISPATCH_CONN_GUARD_WITH_ELSE}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routing in BOTH branches of an inner if/else is GREEN (#7231 desk)",
         {D: DISPATCH_ELSE_IF_BOTH_BRANCHES}, ["dispatcher"], 0),
        ("dispatcher: `continue` in a nested inner loop before the else-arm route is GREEN (#7231 desk)",
         {D: DISPATCH_ELSE_INNER_LOOP_CONTINUE}, ["dispatcher"], 0),
        ("dispatcher: `if not r[\"ok\"]: continue` before the else-arm route is RED (#7231 desk)",
         {D: DISPATCH_ELSE_CONTINUE_FILTER}, ["dispatcher"], 1),
        ("dispatcher: `if conn is not None:` passing plan third, with the no-plan else loop, is RED (#7231 desk r2)",
         {D: DISPATCH_CONN_GUARD_PLAN_ARG}, ["dispatcher"], 1),
        ("dispatcher: plan guard whose else loops another iterable, not ready_rows, is RED (#7231 desk r2)",
         {D: DISPATCH_PLAN_GUARD_ELSE_OTHER_ROWS}, ["dispatcher"], 1),
        ("dispatcher: plan guard whose else loop never writes row_route is RED (#7231 desk r2)",
         {D: DISPATCH_PLAN_GUARD_ELSE_NO_WRITE}, ["dispatcher"], 1),
        ("dispatcher: plan guard body routing under a nested `if cond:` is RED (#7231 desk r2)",
         {D: DISPATCH_PLAN_GUARD_NESTED_IF}, ["dispatcher"], 1),
        ("dispatcher: plan guard body routing inside a nested `for _ in attempts:` is RED (#7231 desk r2)",
         {D: DISPATCH_PLAN_GUARD_NESTED_FOR}, ["dispatcher"], 1),
        ("dispatcher: `if plan is None:` is not the plan guard, RED (#7231 desk r2)",
         {D: DISPATCH_PLAN_GUARD_IS_NONE}, ["dispatcher"], 1),
        ("dispatcher: `if plan is not False:` is not the plan guard, RED (#7231 desk r2)",
         {D: DISPATCH_PLAN_GUARD_NOT_FALSE}, ["dispatcher"], 1),
        ("dispatcher: `continue` in an inner loop's else clause before the else-arm route is RED (#7231 desk r2)",
         {D: DISPATCH_ELSE_INNER_LOOP_ELSE_CONTINUE}, ["dispatcher"], 1),
        ("dispatcher: routing comprehension inside a nested def in the arm is RED (#7231 desk r2)",
         {D: DISPATCH_ARM_COMP_IN_DEF}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop over another iterable, not ready_rows, is RED (#7231 desk r2)",
         {D: DISPATCH_ELSE_LOOP_OTHER_ROWS}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop with a bare top-level _route_row(...) call is GREEN (#7231 desk r2)",
         {D: DISPATCH_ELSE_LOOP_ROUTE_CALL}, ["dispatcher"], 0),
        ("dispatcher: plan guard body is an if/else routing with the plan in BOTH arms, GREEN (t_0266053d N4b)",
         {D: DISPATCH_PLAN_GUARD_NESTED_IF_ELSE}, ["dispatcher"], 0),
        ("dispatcher: plan guard body is an if/else routing in ONE arm only, RED (t_0266053d)",
         {D: DISPATCH_PLAN_GUARD_NESTED_IF_ELSE_ONE_ARM}, ["dispatcher"], 1),
        ("dispatcher: plan guard passing `plan=<plan>` by keyword is GREEN (t_0266053d N5)",
         {D: DISPATCH_PLAN_GUARD_KWARG}, ["dispatcher"], 0),
        ("dispatcher: plan guard passing `plan=<another name>` by keyword is RED (t_0266053d)",
         {D: DISPATCH_PLAN_GUARD_KWARG_OTHER}, ["dispatcher"], 1),
        ("dispatcher: plan guard whose comprehension target rebinds `plan` is RED (t_0266053d)",
         {D: DISPATCH_PLAN_GUARD_COMP_REBINDS}, ["dispatcher"], 1),
        ("dispatcher: plan guard body routing inside an uncalled `lambda plan:` is RED (t_0266053d)",
         {D: DISPATCH_PLAN_GUARD_LAMBDA}, ["dispatcher"], 1),
        ("dispatcher: routing comprehension only in an uncalled nested def is RED (t_0266053d)",
         {D: DISPATCH_UNCALLED_DEF}, ["dispatcher"], 1),
        ("dispatcher: routing helper called only under `if cond:` is RED (t_0266053d)",
         {D: DISPATCH_HELPER_UNDER_IF}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routes then `break` is RED (Prism 19377e67cb32 / b89a0617821b)",
         {D: DISPATCH_ELSE_ROUTE_THEN_BREAK}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routes then `return` is RED (Prism 19377e67cb32 / b89a0617821b)",
         {D: DISPATCH_ELSE_ROUTE_THEN_RETURN}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routes then `continue` is GREEN (t_0266053d)",
         {D: DISPATCH_ELSE_ROUTE_THEN_CONTINUE}, ["dispatcher"], 0),
        ("dispatcher: else-arm loop routes then a nested loop's own `break` is GREEN (t_0266053d)",
         {D: DISPATCH_ELSE_ROUTE_THEN_INNER_BREAK}, ["dispatcher"], 0),
        ("dispatcher: `if skip: pass / else:` routing for-loop is RED (Prism d75c20691309)",
         {D: DISPATCH_SKIP_ELSE_ROUTES}, ["dispatcher"], 1),
        ("dispatcher: if/else whose routing arm is itself an if/else routing in BOTH arms is GREEN (t_0266053d)",
         {D: DISPATCH_BOTH_ARMS_NESTED_IF_ELSE}, ["dispatcher"], 0),
        ("dispatcher: plan guard where the plan reaches _route_row only in an uncalled lambda is RED (t_0266053d)",
         {D: DISPATCH_PLAN_GUARD_PLAN_ONLY_IN_LAMBDA}, ["dispatcher"], 1),
        ("dispatcher: arm routing only in an uncalled nested def, else routes via _route_row, is RED (#7256 desk M7)",
         {D: DISPATCH_ARM_DEF_ELSE_ROUTE_CALL}, ["dispatcher"], 1),
        ("dispatcher: arm routing only in an uncalled lambda, else routes via _route_row, is RED (#7256 desk Nd2)",
         {D: DISPATCH_ARM_LAMBDA_ELSE_ROUTE_CALL}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routes then a nested def holding `return` is GREEN (#7256 desk Ne5)",
         {D: DISPATCH_ELSE_ROUTE_THEN_DEF_RETURN}, ["dispatcher"], 0),
        ("dispatcher: routing for-loop inside `for _ in attempts:` (may run 0 times) is RED (Prism 84d48881bad3)",
         {D: DISPATCH_OTHER_LOOP_FOR}, ["dispatcher"], 1),
        ("dispatcher: routing comprehension inside `for _ in attempts:` is RED (Prism 84d48881bad3)",
         {D: DISPATCH_OTHER_LOOP_COMP}, ["dispatcher"], 1),
        ("dispatcher: routing for-loop in a for-else clause is RED (Prism 84d48881bad3)",
         {D: DISPATCH_LOOP_ELSE_FOR}, ["dispatcher"], 1),
        ("dispatcher: routing for-loop inside a match case is RED (Prism 84d48881bad3)",
         {D: DISPATCH_MATCH_FOR}, ["dispatcher"], 1),
        ("dispatcher: routing for-loop inside an except* handler is RED (Prism 84d48881bad3)",
         {D: DISPATCH_TRYSTAR_FOR}, ["dispatcher"], 1),
        ("dispatcher: routing for-loop inside `async for _ in attempts:` is RED (Prism 84d48881bad3, #7265 desk)",
         {D: DISPATCH_ASYNC_FOR}, ["dispatcher"], 1),
        ("dispatcher: match whose every case routes, ending in `case _:`, is GREEN (#7265 desk)",
         {D: DISPATCH_MATCH_ALL_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: match whose every case routes but has no `case _:` is RED (#7265 desk)",
         {D: DISPATCH_MATCH_ALL_ROUTE_NO_WILD}, ["dispatcher"], 1),
        ("dispatcher: match ending in `case _:` with one case not routing is RED (#7265 desk)",
         {D: DISPATCH_MATCH_ONE_CASE_SKIPS}, ["dispatcher"], 1),
        ("dispatcher: if arm holding an every-case-routes match, else routes, is GREEN (#7265 desk)",
         {D: DISPATCH_IF_ARM_MATCH_ALL_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: routing comprehension as a for's iterable is GREEN (Prism 84d48881bad3)",
         {D: DISPATCH_ROUTE_IN_FOR_ITER}, ["dispatcher"], 0),
        ("dispatcher: try body routes, `except Exception: log(); raise` is GREEN (t_f87e058c a)",
         {D: DISPATCH_TRY_RERAISE_FOR}, ["dispatcher"], 0),
        ("dispatcher: try body routing comprehension, `except Exception: raise` is GREEN (t_f87e058c a)",
         {D: DISPATCH_TRY_RERAISE_COMP}, ["dispatcher"], 0),
        ("dispatcher: try body routes, `except* Exception: log(); raise` is GREEN (t_f87e058c a)",
         {D: DISPATCH_TRYSTAR_RERAISE_FOR}, ["dispatcher"], 0),
        ("dispatcher: try body routes, handler swallows the error, is RED (t_f87e058c a)",
         {D: DISPATCH_TRY_SWALLOW_FOR}, ["dispatcher"], 1),
        ("dispatcher: try body routes, one of two handlers swallows, is RED (t_f87e058c a)",
         {D: DISPATCH_TRY_RERAISE_ONE_HANDLER_SWALLOWS}, ["dispatcher"], 1),
        ("dispatcher: try body routes, handler raises a NEW exception (not bare raise), is RED (t_f87e058c a)",
         {D: DISPATCH_TRY_RAISE_OTHER}, ["dispatcher"], 1),
        ("dispatcher: routing for-loop under `with lk:` is GREEN (t_f87e058c b, probe3 S1)",
         {D: DISPATCH_WITH_FOR}, ["dispatcher"], 0),
        ("dispatcher: routing comprehension under `with lk:` is GREEN (t_f87e058c b, probe3 G6)",
         {D: DISPATCH_WITH_COMP}, ["dispatcher"], 0),
        ("dispatcher: `with lk:` holding `if cond:` around the routing loop is RED (t_f87e058c b)",
         {D: DISPATCH_WITH_IF_FOR}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routing inside `with lk:` is GREEN (t_f87e058c b)",
         {D: DISPATCH_ELSE_LOOP_WITH_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: _route_row in a `with lk:` inside the routing loop is GREEN (t_f87e058c b)",
         {D: DISPATCH_LOOP_WITH_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: _route_row in a re-raising try inside the routing loop is GREEN (t_f87e058c a)",
         {D: DISPATCH_LOOP_TRY_RERAISE_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: routing loop try whose handler may `continue` before the raise is RED (t_f87e058c a)",
         {D: DISPATCH_LOOP_TRY_CONTINUE_THEN_RAISE}, ["dispatcher"], 1),
        ("dispatcher: if arm routing under `with lk:`, else routes, is GREEN (t_f87e058c b)",
         {D: DISPATCH_IF_ARM_WITH_COMP}, ["dispatcher"], 0),
        ("dispatcher: routing loop, then an inner loop whose else clause breaks, is RED (t_f87e058c X3)",
         {D: DISPATCH_LOOP_ROUTE_INNER_ELSE_BREAK}, ["dispatcher"], 1),
        ("dispatcher: routing loop routes then `raise` is RED (t_f87e058c X4)",
         {D: DISPATCH_LOOP_ROUTE_THEN_RAISE}, ["dispatcher"], 1),
        ("dispatcher: routing loop, a raise caught by its own try, is GREEN (t_f87e058c X4)",
         {D: DISPATCH_LOOP_ROUTE_CAUGHT_RAISE}, ["dispatcher"], 0),
        ("dispatcher: else-arm loop routes then `raise` is RED (t_f87e058c X4)",
         {D: DISPATCH_ELSE_ROUTE_THEN_RAISE}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routes, then an inner loop whose else breaks, is RED (t_f87e058c X3)",
         {D: DISPATCH_ELSE_ROUTE_THEN_INNER_ELSE_BREAK}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop routes, then a re-raising try, is GREEN (t_f87e058c X4)",
         {D: DISPATCH_ELSE_ROUTE_THEN_RERAISE_TRY}, ["dispatcher"], 0),
        ("dispatcher: else-arm loop `raise` BEFORE the route is RED (t_f87e058c X4)",
         {D: DISPATCH_ELSE_RAISE_THEN_ROUTE}, ["dispatcher"], 1),
        ("dispatcher: else-arm loop, a re-raising try before the route, is GREEN (t_f87e058c X4)",
         {D: DISPATCH_ELSE_RERAISE_TRY_THEN_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: else-arm loop, a raise caught by its own try before the route, is GREEN (t_f87e058c X4)",
         {D: DISPATCH_ELSE_CAUGHT_RAISE_THEN_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: routing helper def'd then called unconditionally is GREEN (t_f87e058c X5)",
         {D: DISPATCH_HELPER_CALLED}, ["dispatcher"], 0),
        ("dispatcher: routing helper called only behind an IfExp is RED (t_f87e058c X5)",
         {D: DISPATCH_HELPER_CALLED_IFEXP}, ["dispatcher"], 1),
        ("dispatcher: called routing helper that may return before routing is RED (t_f87e058c X5)",
         {D: DISPATCH_HELPER_EARLY_RETURN}, ["dispatcher"], 1),
        ("dispatcher: plan guard body if/else where one arm routes WITHOUT the plan is RED (t_f87e058c Na2)",
         {D: DISPATCH_PLAN_GUARD_NESTED_IF_ELSE_NO_PLAN_ARM}, ["dispatcher"], 1),
        ("dispatcher: plan guard where the plan reaches _route_row only in an uncalled def is RED (t_f87e058c M12r)",
         {D: DISPATCH_PLAN_GUARD_PLAN_ONLY_IN_DEF}, ["dispatcher"], 1),
        ("dispatcher: routing for-loop under `with contextlib.suppress(Exception):` is RED (Prism 67a939c59339)",
         {D: DISPATCH_WITH_SUPPRESS_FOR}, ["dispatcher"], 1),
        ("dispatcher: routing comprehension under `with suppress(Exception):` is RED (Prism 67a939c59339)",
         {D: DISPATCH_WITH_SUPPRESS_COMP}, ["dispatcher"], 1),
        ("dispatcher: routing loop under `with lk, suppress(Exception):` is RED (Prism 67a939c59339)",
         {D: DISPATCH_WITH_LK_SUPPRESS_FOR}, ["dispatcher"], 1),
        ("dispatcher: _route_row in a `with suppress(Exception):` inside the routing loop is RED (Prism 67a939c59339)",
         {D: DISPATCH_LOOP_WITH_SUPPRESS_ROUTE}, ["dispatcher"], 1),
        ("dispatcher: called routing helper that is an async def is RED (Prism 75ebb57d741f)",
         {D: DISPATCH_HELPER_ASYNC}, ["dispatcher"], 1),
        ("dispatcher: called routing helper that yields is RED (Prism 75ebb57d741f)",
         {D: DISPATCH_HELPER_YIELD}, ["dispatcher"], 1),
        ("dispatcher: helper rebound to a lambda before the call is RED (Prism 75ebb57d741f)",
         {D: DISPATCH_HELPER_REBOUND_LAMBDA}, ["dispatcher"], 1),
        ("dispatcher: helper rebound to another name before the call is RED (Prism 75ebb57d741f)",
         {D: DISPATCH_HELPER_REBOUND_NAME}, ["dispatcher"], 1),
        ("dispatcher: helper rebound as a for target before the call is RED (Prism 75ebb57d741f)",
         {D: DISPATCH_HELPER_REBOUND_FOR}, ["dispatcher"], 1),
        ("dispatcher: helper rebound by an import alias before the call is RED (Prism 75ebb57d741f)",
         {D: DISPATCH_HELPER_REBOUND_IMPORT}, ["dispatcher"], 1),
        ("dispatcher: helper rebound by a walrus before the call is RED (Prism 75ebb57d741f)",
         {D: DISPATCH_HELPER_REBOUND_WALRUS}, ["dispatcher"], 1),
        ("dispatcher: routing loop re-raising try whose `finally: continue` is RED (Prism 34a70a667291)",
         {D: DISPATCH_LOOP_TRY_RERAISE_FINALLY_CONTINUE}, ["dispatcher"], 1),
        ("dispatcher: try body routes, re-raising handler, `finally: return` is RED (Prism 34a70a667291)",
         {D: DISPATCH_TRY_RERAISE_FINALLY_RETURN}, ["dispatcher"], 1),
        ("dispatcher: try body routes, re-raising handler, `finally: log()` is GREEN (Prism 34a70a667291)",
         {D: DISPATCH_TRY_RERAISE_FINALLY_LOG}, ["dispatcher"], 0),
        ("dispatcher: called helper that only defines a routing def and returns {} is RED (Prism a8f5e5d182f8)",
         {D: DISPATCH_HELPER_NESTED_DEF_ONLY}, ["dispatcher"], 1),
        ("dispatcher: no _route_row over ready_rows is RED", {D: DISPATCH_NO_ROUTE}, ["dispatcher"], 1),
        ("dispatcher: _route_row under `if` in a for loop is RED", {D: DISPATCH_COND_ROUTE}, ["dispatcher"], 1),
        ("dispatcher: unconditional _route_row in a for loop is GREEN", {D: DISPATCH_LOOP_ROUTE}, ["dispatcher"], 0),
        ("dispatcher: _route_row behind an IfExp is RED", {D: DISPATCH_IFEXP_ROUTE}, ["dispatcher"], 1),
        ("dispatcher: break INSIDE the route_first guard body is RED", {D: DISPATCH_GUARD_BODY_BREAK},
         ["dispatcher"], 1),
        ("dispatcher: _route_row in an unconsumed generator is RED", {D: DISPATCH_LAZY_GEN}, ["dispatcher"], 1),
        ("dispatcher: list(<generator>) routing is GREEN", {D: DISPATCH_EAGER_GEN}, ["dispatcher"], 0),
        ("pool: exit after a one-line nested loop on the same line is RED", {P: POOL_EXIT_AFTER_NESTED}, ["pool"], 1),
        ("pool: `break || true` is RED", {P: POOL_BREAK_OR_TRUE}, ["pool"], 1),
        ("pool: `break 2` in a one-line nested loop is RED", {P: POOL_BREAK2}, ["pool"], 1),
        ("pool: `break 2` in a multi-line nested loop is RED", {P: POOL_BREAK2_NESTED}, ["pool"], 1),
        ("pool: plain `break` in a multi-line nested loop is GREEN", {P: POOL_BREAK1_NESTED}, ["pool"], 0),
        ("pool: continue-on-refuse + nested-loop break + echo text is GREEN", {P: POOL_OK}, ["pool"], 0),
        ("pool: `continue 2` on refuse abandons the tick, RED (Prism cda9416d1d9f)", {P: POOL_CONTINUE2}, ["pool"], 1),
        ("pool: planted break on refuse is RED", {P: POOL_409_BREAK}, ["pool"], 1),
        ("pool: exit inside the slot loop is RED", {P: POOL_EXIT}, ["pool"], 1),
        ("pool: a file with no slot loop is RED", {P: "#!/usr/bin/env bash\nfor i in 1 2; do :; done\n"}, ["pool"], 1),
    ]
    bad = 0
    for name, files, checks, want in cases:
        got_f = lint(tree(files), checks)
        got = 1 if got_f else 0
        ok = got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name} (rc {got}, want {want})" + ("" if ok else f": {got_f}"))
    print(f"selftest: {len(cases) - bad}/{len(cases)} pass")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--check", action="append", choices=sorted(CHECKS))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.check:
        print("lint-queue-loops: name the loops this repo owns with --check dispatcher|pool", file=sys.stderr)
        return 2
    findings = lint(a.root.resolve(), a.check)
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
