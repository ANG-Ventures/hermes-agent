#!/usr/bin/env python3
"""Fail when a GitHub worktree card can be kept on the Studio for a reason nobody enumerated (t_20fa16c7).

Ace 2026-11-10 12:24: "All machines can make any checkout always ... harden and lint it so this can never
happen again." Every reason ``hermes_cli/kanban_worktree_port.py`` may give for keeping a worktree card
local is a ``why`` it appends (``_why(why, X)``) and the dispatcher reports as
``not_portable:workspace_kind:<X>``. This lint fails when:

* a ``_why(...)`` in the port module names a reason outside :data:`ENUMERATED` (a string literal, or a
  ``WHY_*`` constant whose value is not listed), or any module-level ``WHY_*`` constant is not listed;
* the module's ``LOCAL_ONLY_REASONS`` keys differ from :data:`ENUMERATED` (each needs a written
  justification there);
* ``hermes_cli/kanban_db_dispatch.py`` builds a ``not_portable:workspace_kind:`` reason from anything but
  the port's ``wt_why`` (a fixed string naming a repo list or a checkout location).

Adding a reason means editing BOTH the module and :data:`ENUMERATED` below, so it shows in review.

Usage: python3 scripts/check_worktree_local_reasons.py [port_module.py [dispatch_module.py]]
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = ROOT / "hermes_cli" / "kanban_worktree_port.py"
DISPATCH = ROOT / "hermes_cli" / "kanban_db_dispatch.py"

# The enumerated set. Each value's justification lives in LOCAL_ONLY_REASONS in the port module.
ENUMERATED = frozenset({
    "no_pool_host", "origin_not_ours", "origin_not_github", "not_a_repo", "diverged_from_origin",
    "operator_pin", "concrete_target_path", "own_tree_not_a_checkout", "own_tree_off_branch",
    "own_tree_probe_failed", "own_tree_repo_mismatch",
    # the dirty-tree carry's own refusals (t_894be273)
    "dirty_submodule_or_nested_repo", "dirty_tree_over_cap", "origin_tip_not_studio_head",
    "dirty_path_through_symlink", "dirty_path_not_plain_file",
    # kill-switch / hand-list only (derived: false, KANBAN_DIRTY_CARRY=0)
    "unpushed_dirty_tree", "unpushed_commits", "derived_mode_off",
    # _why's own runtime fallback for a reason outside the table (Prism b4c7ade40781)
    "unenumerated_local_reason",
})
# Methods that write a list in place: only `_why` may call one on a `why` list.
_LIST_WRITERS = frozenset({"append", "extend", "insert", "__setitem__", "__iadd__"})
# The one sanctioned fixed string (a host: pin the port does not serve); it names no repo list.
DISPATCH_FIXED_OK = re.compile(r"^not_portable:workspace_kind:worktree card not portable to the pinned host")


def _constants(tree: ast.Module) -> dict:
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _resolve(node: ast.AST, consts: dict):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _assignments(name: str, scope: ast.AST) -> list:
    """Every value bound to ``name`` in ``scope``: ``(kind, node)`` with kind ``value`` (an expression) or
    ``unknown`` (a binding the lint cannot read: a parameter, augmented assign, with/except target, ...)."""
    out = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = scope.args
        if any(p.arg == name for p in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if p):
            out.append(("unknown", scope))
    for n in ast.walk(scope):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append(("value", n.value))
                elif any(isinstance(e, ast.Name) and e.id == name for e in ast.walk(t)):
                    out.append(("unknown", n))
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and isinstance(n.target, ast.Name) \
                and n.target.id == name:
            out.append(("value", n.value) if n.value is not None else ("unknown", n))
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            out.append(("unknown", n))
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            if not any(isinstance(e, ast.Name) and e.id == name for e in ast.walk(n.target)):
                continue
            # literal iteration only: `for r in (A, B)` or `for x, r in ((x1, A), (x2, B))`
            if isinstance(n.target, ast.Name) and isinstance(n.iter, (ast.Tuple, ast.List)):
                out.extend(("value", e) for e in n.iter.elts)
            elif isinstance(n.target, ast.Tuple) and isinstance(n.iter, (ast.Tuple, ast.List)):
                idx = next((i for i, e in enumerate(n.target.elts) if isinstance(e, ast.Name) and e.id == name), None)
                for row in n.iter.elts:
                    if idx is None or not isinstance(row, (ast.Tuple, ast.List)) or len(row.elts) <= idx:
                        out.append(("unknown", n))
                    else:
                        out.append(("value", row.elts[idx]))
            else:
                out.append(("unknown", n))
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            if any(i.optional_vars is not None and any(isinstance(e, ast.Name) and e.id == name
                                                       for e in ast.walk(i.optional_vars)) for i in n.items):
                out.append(("unknown", n))
        elif isinstance(n, ast.ExceptHandler) and n.name == name:
            out.append(("unknown", n))
        elif isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            out.append(("unknown", n))
    return out


def _reason_values(node: ast.AST, scope: ast.AST, consts: dict, funcs: dict, seen=None) -> list:
    """Descriptions of every value ``node`` (a ``_why`` reason argument) can take that is NOT enumerated (Prism
    f202699b4161). A string literal or WHY_* constant is checked by value; a local Name is accepted only when
    every binding of it in its function resolves to enumerated reasons; a call to a module function only when
    each of its returns does (a tuple return is not a reason string). Anything else is an error."""
    seen = set() if seen is None else seen
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [] if node.value in ENUMERATED else [repr(node.value)]
    if isinstance(node, ast.IfExp):
        return (_reason_values(node.body, scope, consts, funcs, seen)
                + _reason_values(node.orelse, scope, consts, funcs, seen))
    if isinstance(node, ast.Name):
        key = (id(scope), node.id)
        if key in seen:
            return []
        seen.add(key)
        binds = _assignments(node.id, scope) if not isinstance(scope, ast.Module) else []
        if not binds:
            val = consts.get(node.id)
            if val is None:
                return [f"{node.id!r} (unbound here)"]
            return [] if val in ENUMERATED else [f"{val!r} ({node.id})"]
        bad = []
        for kind, value in binds:
            if kind == "unknown":
                bad.append(f"{node.id!r} (bound at line {getattr(value, 'lineno', '?')} to a value the lint "
                           "cannot read)")
            else:
                bad.extend(_reason_values(value, scope, consts, funcs, seen))
        return bad
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs:
        fn = funcs[node.func.id]
        key = ("fn", fn.name)
        if key in seen:
            return []
        seen.add(key)
        bad = []
        for ret in (n for n in ast.walk(fn) if isinstance(n, ast.Return)):
            if ret.value is None or isinstance(ret.value, ast.Tuple) or (
                    isinstance(ret.value, ast.Constant) and ret.value.value is None):
                continue  # not a reason string (the caller's isinstance(..., str) guard)
            bad.extend(_reason_values(ret.value, fn, consts, funcs, seen))
        return bad
    return [f"{ast.unparse(node)!r} (not a literal, WHY_* constant or traceable local)"]


def check_port(path: Path) -> list:
    src = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(src, str(path))
    consts = _constants(tree)
    errors = []
    # Every WHY_* constant is a reason some path can hand to _why (directly, or returned by a helper such as
    # dirty_census and passed on through a variable), so each one must be enumerated.
    for name, val in sorted(consts.items()):
        if name.startswith("WHY_") and val not in ENUMERATED:
            errors.append(f"{path.name}: {name} = {val!r} is not enumerated (add it to LOCAL_ONLY_REASONS with a "
                          "justification AND to scripts/check_worktree_local_reasons.py ENUMERATED)")
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    # `_why` is the only writer of a reason list: it maps an unlisted reason to unenumerated_local_reason at
    # runtime, so a write that goes round it would bypass that (Prism b4c7ade40781).
    if "_why" not in funcs:
        errors.append(f"{path.name}: the _why choke point is missing")
    inside_why = {id(n) for n in ast.walk(funcs["_why"])} if "_why" in funcs else set()
    for node in ast.walk(tree):
        if id(node) in inside_why:
            continue
        target = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LIST_WRITERS:
            target = node.func.value
        elif isinstance(node, ast.AugAssign):
            target = node.target
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Subscript):
                    target = t.value
        if isinstance(target, ast.Name) and target.id == "why":
            errors.append(f"{path.name}:{getattr(node, 'lineno', '?')}: writes the reason list outside _why "
                          "(only _why may: it records an unlisted reason as unenumerated_local_reason)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_why":
            arg = node.args[1] if len(node.args) >= 2 else next(
                (k.value for k in node.keywords if k.arg == "reason"), None)
            if arg is None:
                errors.append(f"{path.name}:{node.lineno}: _why call without a reason argument")
                continue
            scope = node
            while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents[scope]
            for desc in _reason_values(arg, scope, consts, funcs):
                errors.append(f"{path.name}:{node.lineno}: local-only reason {desc} is not enumerated (add it to "
                              "LOCAL_ONLY_REASONS with a justification AND to scripts/check_worktree_local_reasons.py "
                              "ENUMERATED)")
        if isinstance(node, ast.Tuple):
            # (base, WHY_X) pairs fed to _why through a loop variable
            for elt in node.elts:
                if isinstance(elt, ast.Name) and elt.id.startswith("WHY_"):
                    val = consts.get(elt.id)
                    if val is not None and val not in ENUMERATED:
                        errors.append(f"{path.name}:{node.lineno}: reason {val!r} ({elt.id}) is not enumerated")
    table = None
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if isinstance(target, ast.Name) and target.id == "LOCAL_ONLY_REASONS" and isinstance(node.value, ast.Dict):
            table = {}
            for k, v in zip(node.value.keys, node.value.values):
                key = _resolve(k, consts)
                just = v.value if isinstance(v, ast.Constant) else ast.unparse(v)
                table[key] = just
    if table is None:
        errors.append(f"{path.name}: LOCAL_ONLY_REASONS table missing")
    else:
        for key, just in table.items():
            if not (isinstance(just, str) and len(just.strip()) >= 20):
                errors.append(f"{path.name}: LOCAL_ONLY_REASONS[{key!r}] needs a written justification")
        if set(table) != set(ENUMERATED):
            errors.append(f"{path.name}: LOCAL_ONLY_REASONS keys differ from the lint's ENUMERATED: "
                          f"only in module {sorted(set(table) - ENUMERATED)}, "
                          f"only in lint {sorted(ENUMERATED - set(table))}")
    return errors


def check_dispatch(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
    errors = []
    in_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    # `x.startswith("not_portable:workspace_kind:")` reads a reason, it does not make one (#216's _route_row)
    in_fstring |= {id(a) for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "startswith" for a in n.args}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in in_fstring \
                and node.value.startswith("not_portable:workspace_kind:"):
            if not DISPATCH_FIXED_OK.match(node.value):
                errors.append(f"{path.name}:{node.lineno}: fixed workspace_kind reason {node.value[:80]!r}: "
                              "a worktree card's local reason must come from the port's wt_why")
        if isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and str(head.value).startswith("not_portable:workspace_kind:"):
                rest = node.values[1:]
                # the literal head is exactly the prefix: any text after it is a new, unenumerated reason
                # (Prism 4418d7472cae)
                ok = (head.value == "not_portable:workspace_kind:"
                      and len(rest) == 1 and isinstance(rest[0], ast.FormattedValue)
                      and ast.unparse(rest[0].value) == "wt_why[0]")
                if not ok:
                    errors.append(f"{path.name}:{node.lineno}: workspace_kind reason built from "
                                  f"{ast.unparse(node)[:80]}, not wt_why[0]")
    return errors


def main(argv: list) -> int:
    port = Path(argv[0]) if argv else PORT
    dispatch = Path(argv[1]) if len(argv) > 1 else DISPATCH
    errors = check_port(port) + (check_dispatch(dispatch) if dispatch.exists() else [])
    for e in errors:
        print(f"worktree-local-reasons: {e}", file=sys.stderr)
    if not errors:
        print(f"worktree-local-reasons: ok ({len(ENUMERATED)} enumerated reasons)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
