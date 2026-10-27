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


def _early_exits(body: list, path: str, guarded: bool = False) -> list[str]:
    """Break/return statements in a loop body that end the loop early (nested loops/functions are their own)."""
    out = []
    seen_guard = guarded
    for st in body:
        guard_after = _is_route_first_continue(st)   # exempts only the statements AFTER this one
        if isinstance(st, ast.Break) and not seen_guard:
            out.append(f"{path}:{st.lineno}: break in the ready loop ends the tick early (not behind "
                       f"`if route_first: continue`)")
        elif isinstance(st, ast.Return):
            out.append(f"{path}:{st.lineno}: return inside the ready loop ends the tick early")
        elif isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            # a nested loop's break is its own; only a return escapes it
            out += [f for f in _early_exits_returns_only(st, path)]
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        else:
            for fld in ("body", "orelse", "finalbody"):
                sub = getattr(st, fld, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    out += _early_exits(sub, path, seen_guard)
            for h in getattr(st, "handlers", []) or []:
                out += _early_exits(h.body, path, seen_guard)
            for c in getattr(st, "cases", []) or []:
                out += _early_exits(c.body, path, seen_guard)
        if guard_after:
            seen_guard = True
    return out


def _early_exits_returns_only(loop, path: str) -> list[str]:
    out = []
    stack = list(ast.iter_child_nodes(loop))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Return):
            out.append(f"{path}:{n.lineno}: return inside the ready loop ends the tick early")
        stack.extend(ast.iter_child_nodes(n))
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
    """_route_row runs on every iteration: it is in a TOP-LEVEL simple statement of the loop body, not under a
    condition, and no earlier top-level statement can skip it (continue / break / return anywhere in it)."""
    for st in loop.body:
        calls = _route_calls(st)
        if calls and not isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With,
                                         ast.Match if hasattr(ast, "Match") else ast.If)):
            return not all(_conditional_inside(st, c) for c in calls)
        if any(isinstance(n, (ast.Continue, ast.Break, ast.Return)) for n in ast.walk(st)) or calls:
            return False
    return False


COMPOUND = tuple(t for t in (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, getattr(ast, "TryStar", None),
                             ast.With, ast.AsyncWith, getattr(ast, "Match", None),
                             ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef) if t is not None)


def _ready_route_comp(n) -> bool:
    """A comprehension with one generator over `ready_rows` that calls _route_row."""
    return (isinstance(n, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)) and bool(_route_calls(n))
            and len(n.generators) == 1 and _over_ready(n.generators[0].iter))


def _leaves_outer_loop(st) -> bool:
    """`st` can skip the rest of the enclosing loop's iteration: a continue / break that belongs to that loop,
    or a return anywhere. A continue / break inside a nested loop is that loop's own."""
    def walk(n, own: bool) -> bool:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return False
        if isinstance(n, ast.Return) or (own and isinstance(n, (ast.Continue, ast.Break))):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            # the nested loop's continue/break are its own; its else clause runs in the outer iteration
            return (any(walk(c, False) for c in n.body)
                    or any(walk(c, own) for c in n.orelse))
        return any(walk(c, own) for c in ast.iter_child_nodes(n))
    return walk(st, True)


def _ends_outer_loop(st) -> bool:
    """`st` can END the enclosing loop: a break that belongs to it, or a return anywhere (a continue only ends
    the iteration, so it is harmless once the row is routed)."""
    def walk(n, own: bool) -> bool:
        if isinstance(n, DEFERRED):
            return False
        if isinstance(n, ast.Return) or (own and isinstance(n, ast.Break)):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            return any(walk(c, False) for c in n.body) or any(walk(c, own) for c in n.orelse)
        return any(walk(c, own) for c in ast.iter_child_nodes(n))
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
        if _leaves_outer_loop(st):
            return False
    return routed


def _loop_routes_every_row(loop) -> bool:
    """A loop over the ready rows routes every row only if its body routes on every iteration (_block_routes).
    Prism 2e9888a37c28 (`for r in ready_rows: pass`) and 1d133dcecb68 (`if r["ok"]: row_route[...] = ...`)."""
    return _block_routes(loop.body)


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
            elif not isinstance(st, COMPOUND) and any(_ready_route_comp(n) for n in _walk_eager(st)):
                return True
        return False
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
        elif isinstance(st, (ast.While, ast.Try, ast.With, ast.AsyncWith)):
            for inner in ast.walk(st):
                if inner is not st:
                    guarded.add(id(inner))
    # D1: _route_row presented for every ready row (eager code only: a comprehension inside a nested def or
    # lambda runs only if something calls it)
    d1 = False
    for n in _walk_eager(fn):
        if isinstance(n, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            calls = _route_calls(n)
            gens = n.generators
            if calls and len(gens) == 1 and _over_ready(gens[0].iter):
                if id(n) in guarded:
                    out.append(f"{rel}:{n.lineno}: {ROUTE_FN} comprehension over {READY} sits under a guard "
                               f"(if/try/with/loop): every ready row must be routed each tick, unconditionally")
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
            out += [f.replace("ready loop", f"{ROUTE_FN} loop") for f in _early_exits(n.body, rel)]
            if id(n) in guarded:
                out.append(f"{rel}:{n.lineno}: {ROUTE_FN} loop over {READY} sits under a guard (if/try/with/loop): "
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
