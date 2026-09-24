"""Structural guard for GC path comparisons (t_ee808d83).

This is intentionally an AST contract: the safety invariant is about *every*
comparison site, including future sites not exercised by an existing fixture.
The behaviour tests in test_kanban_workspace_retention.py prove the effects.
"""

import ast
import inspect
from pathlib import Path

from hermes_cli import kanban_db as kb


_IDENTITY_CALLS = {
    "_path_identity", "_same_tree", "_same_path", "_is_managed_scratch_path",
    "_managed_scratch_path_info", "_live_owners_of_path", "_process_cwd_within",
}
_PATH_NAMES = {
    "path", "parent", "child", "candidate", "resolved", "stored", "root",
    "target", "cwd", "workspace", "p_abs", "managed_root", "pinned",
    "requested", "override", "native", "artifact", "workspace_root",
}


def _called(node):
    return {
        call.func.id for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _gc_path_offenders(source: str):
    module = ast.parse(source)
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    scoped = [fn for fn in functions if fn.name in _IDENTITY_CALLS | {"_unknown_owner_may_claim"}
              or _called(fn) & _IDENTITY_CALLS]
    assert len(scoped) >= 9, "GC path comparison scope unexpectedly empty"
    offenders = []
    for fn in scoped:
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and (
                isinstance(n.func, ast.Attribute) and n.func.attr in {"open", "close", "fcntl"}
                or isinstance(n.func, ast.Name) and n.func.id in {"open", "fcntl"}
            ) and fn.name in {"_path_identity", "_same_tree", "_same_path"}:
                offenders.append((fn.name, n.lineno, "descriptor in identity"))
            if (fn.name != "_unknown_owner_may_claim" and isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in {"casefold", "lower", "normalize"}):
                offenders.append((fn.name, n.lineno, "string fold in identity"))
        if fn.name == "_path_identity":
            continue
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            if isinstance(n.func, ast.Attribute) and n.func.attr == "is_relative_to":
                receiver = n.func.value
                if isinstance(receiver, ast.Name) and receiver.id in _PATH_NAMES:
                    offenders.append((fn.name, n.lineno, "raw Path.is_relative_to"))
            if (isinstance(n.func, ast.Attribute) and n.func.attr == "fullmatch"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "_TASK_DIR_NAME_RE"
                    and n.args and isinstance(n.args[0], ast.Attribute)
                    and n.args[0].attr == "name" and isinstance(n.args[0].value, ast.Name)
                    and n.args[0].value.id in _PATH_NAMES):
                offenders.append((fn.name, n.lineno, "raw owner leaf"))
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare) and any(isinstance(op, (ast.Eq, ast.In)) for op in n.ops):
                if (fn.name == "_live_owners_of_path"
                        and any(isinstance(v, ast.Attribute) and v.attr == "name"
                                for v in ast.walk(n))):
                    offenders.append((fn.name, n.lineno, "raw owner leaf"))
                variables = [v.id for part in (n.left, *n.comparators)
                             for v in ast.walk(part) if isinstance(v, ast.Name)]
                if sum(v in _PATH_NAMES for v in variables) >= 2:
                    offenders.append((fn.name, n.lineno, "raw path comparison"))
    return offenders


def test_gc_path_decisions_use_identity_not_raw_path_spelling():
    source = Path(inspect.getfile(kb)).read_text(encoding="utf-8")
    assert not _gc_path_offenders(source)


def test_unknown_owner_fallback_only_refuses():
    tree = ast.parse(Path(inspect.getfile(kb)).read_text(encoding="utf-8"))
    sites = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            if "_unknown_owner_may_claim" not in _called(node.test):
                continue
            sites.append(fn.name)
            body = [n for statement in node.body for n in ast.walk(statement)]
            if fn.name == "_process_cwd_within":
                assert any(isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                           and n.value.value is True for n in body)
            elif fn.name == "_live_owners_of_path":
                assert any(isinstance(n, ast.Return) and isinstance(n.value, ast.List)
                           and n.value.elts[0].value == "<unknown-owner-path>" for n in body)
            elif fn.name == "_durable_audit_log_path":
                assert any(isinstance(n, ast.Continue) for n in body)
            else:
                raise AssertionError(f"unknown-owner helper used at unreviewed site: {fn.name}")
    assert sorted(sites) == sorted(["_process_cwd_within", "_live_owners_of_path",
                                    "_durable_audit_log_path"])


def test_gc_path_contract_rejects_new_raw_owner_leaf_and_descriptor():
    source = Path(inspect.getfile(kb)).read_text(encoding="utf-8")
    old = "and _same_tree(resolved, managed_root / row[\"id\"], spelling_memo)"
    assert old in source
    mutant = source.replace(old, 'and resolved.name == row["id"]', 1)
    assert any(reason == "raw owner leaf" for _, _, reason in _gc_path_offenders(mutant))
    old = "stat = os.stat(os.path.realpath(os.path.expanduser(key)))"
    assert old in source
    mutant = source.replace(old, old + "\n        os.open(key, os.O_RDONLY)", 1)
    assert any(reason == "descriptor in identity" for _, _, reason in _gc_path_offenders(mutant))
    mutant = source.replace(old, old + "\n        key.casefold()", 1)
    assert any(reason == "string fold in identity" for _, _, reason in _gc_path_offenders(mutant))
