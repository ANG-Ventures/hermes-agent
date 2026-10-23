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


def _route_calls(node) -> list:
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == ROUTE_FN]


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
    # D1: _route_row presented for every ready row
    d1 = False
    for n in ast.walk(fn):
        if isinstance(n, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            calls = _route_calls(n)
            gens = n.generators
            if calls and len(gens) == 1 and _over_ready(gens[0].iter):
                if gens[0].ifs:
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
                    elif tok in ("break", "return", "exit"):
                        arg = toks[k + 1] if k + 1 < len(toks) and toks[k + 1] not in SH_OPS else None
                        # `break N` leaves N loops: it ends the slot loop when N >= the current depth.
                        # A non-numeric N fails closed; return/exit always end it.
                        n_lvl = 1 if arg is None else (int(arg) if arg.isdigit() else 10 ** 6)
                        if tok != "break" or n_lvl >= depth:
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
DISPATCH_FILTER = DISPATCH_OK.replace("for r in ready_rows}", "for r in ready_rows if r['ok']}")
DISPATCH_NO_ROUTE = DISPATCH_OK.replace('    row_route = {r["id"]: _route_row(conn, r) for r in ready_rows}\n', "")
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
