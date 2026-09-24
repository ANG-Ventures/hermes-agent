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
    scoped = [fn for fn in functions if fn.name in _IDENTITY_CALLS or _called(fn) & _IDENTITY_CALLS]
    assert len(scoped) >= 9, "GC path comparison scope unexpectedly empty"
    offenders = []
    for fn in scoped:
        if fn.name == "_path_identity":
            # The identity function itself may use filesystem stat, but never
            # open/close a descriptor (SQLite's process-wide POSIX lock hazard).
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and (
                    isinstance(n.func, ast.Attribute) and n.func.attr in {"open", "close", "fcntl"}
                    or isinstance(n.func, ast.Name) and n.func.id in {"open", "fcntl"}
                ):
                    offenders.append((fn.name, n.lineno, "descriptor in identity"))
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
                variables = [v.id for part in (n.left, *n.comparators)
                             for v in ast.walk(part) if isinstance(v, ast.Name)]
                if sum(v in _PATH_NAMES for v in variables) >= 2:
                    offenders.append((fn.name, n.lineno, "raw path comparison"))
    return offenders


def test_gc_path_decisions_use_identity_not_raw_path_spelling():
    source = Path(inspect.getfile(kb)).read_text(encoding="utf-8")
    assert not _gc_path_offenders(source)


def test_gc_path_contract_rejects_new_raw_owner_leaf_and_descriptor():
    source = Path(inspect.getfile(kb)).read_text(encoding="utf-8")
    old = "_TASK_DIR_NAME_RE.fullmatch(owner_id)"
    assert old in source
    mutant = source.replace(old, "_TASK_DIR_NAME_RE.fullmatch(parent.name)", 1)
    assert any(reason == "raw owner leaf" for _, _, reason in _gc_path_offenders(mutant))
    old = "name = os.path.realpath(os.path.expanduser(key))"
    assert old in source
    mutant = source.replace(old, old + "\n    os.open(key, os.O_RDONLY)", 1)
    assert any(reason == "descriptor in identity" for _, _, reason in _gc_path_offenders(mutant))
