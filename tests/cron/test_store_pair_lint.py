"""I4 lint (t_82c613d9): a cron store is the PAIR (jobs.json, runtime_state.db [+ pending
generations]). Only ``cron/store_pair.py`` (and the store's own modules ``cron/jobs.py`` /
``cron/runtime_state.py``) may copy, move, replace, rename or open-for-write a path that names
either half. Every other leg calls ``cron.store_pair``.

The check is a per-function taint walk: a name is tainted when it is assigned from an expression
that mentions ``jobs.json`` / ``runtime_state.db`` (a string literal, or one of the known
constants below), directly or through another tainted name (fixpoint). A write-shaped call with a
tainted argument is a finding.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ALLOWED = {"cron/store_pair.py", "cron/jobs.py", "cron/runtime_state.py"}
SKIP_DIRS = {"tests", "venv", ".venv", "node_modules", ".git", "build", "dist", ".worktrees"}

# Literals and identifiers that name one half of the pair.
NEEDLES = ("jobs.json", "runtime_state.db")
NAME_SENTINELS = {
    "_CRON_JOBS_REL", "_CRON_RUNTIME_REL", "JOBS_FILE", "DB_NAME", "_CRON_STORE_REL",
    "JOBS_NAME", "jobs_file",
}
ATTR_SENTINELS = {"jobs_file", "DB_NAME", "JOBS_FILE"}

COPY_FUNCS = {"copy", "copy2", "copyfile", "copyfileobj", "move", "copytree"}
OS_FUNCS = {"replace", "rename", "renames", "link", "symlink"}
PATH_METHODS = {"rename", "replace", "write_text", "write_bytes", "hardlink_to", "symlink_to",
                "touch"}
PATH_TARGET_METHODS = {"rename", "replace", "hardlink_to", "symlink_to"}
WRITE_HELPERS = {"_atomic_output_path", "atomic_json_write", "atomic_write", "_atomic_write_json",
                 "_extract_member_atomically", "_import_db_member", "_safe_restore_db"}


def _mentions(node, tainted):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(n in sub.value for n in NEEDLES):
                return True
        elif isinstance(sub, ast.Name) and (sub.id in NAME_SENTINELS or sub.id in tainted):
            return True
        elif isinstance(sub, ast.Attribute) and sub.attr in ATTR_SENTINELS:
            return True
    return False


def _targets(t):
    if isinstance(t, ast.Name):
        yield t.id
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from _targets(e)


def _taint(func):
    tainted = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(func):
            pairs = []
            if isinstance(node, ast.Assign):
                pairs = [(t, node.value) for t in node.targets]
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                pairs = [(node.target, node.value)]
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                pairs = [(node.optional_vars, node.context_expr)]
            elif isinstance(node, (ast.For, ast.comprehension)):
                pairs = [(node.target, node.iter)]
            elif isinstance(node, ast.NamedExpr):
                pairs = [(node.target, node.value)]
            for tgt, val in pairs:
                if _mentions(val, tainted):
                    for name in _targets(tgt):
                        if name not in tainted:
                            tainted.add(name)
                            changed = True
    return tainted


def _paths(call, n):
    """The first *n* positional args plus path-like keywords: the PATH operands of a write call
    (never its content: the card's rule is about the path a write lands on)."""
    return list(call.args[:n]) + [k.value for k in call.keywords
                                  if k.arg in ("src", "dst", "file", "path", "target")]


def _is_write_call(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        owner = f.value.id if isinstance(f.value, ast.Name) else None
        if owner == "shutil" and f.attr in COPY_FUNCS:
            return _paths(call, 2)
        if owner == "os" and f.attr in OS_FUNCS:
            return _paths(call, 2)
        if owner == "os" and f.attr == "open":
            return _paths(call, 1) if _os_open_writes(call) else None
        if owner not in ("os", "shutil") and f.attr in PATH_METHODS:
            # write_text/write_bytes/touch: the receiver is the path, the arg is content.
            return [f.value] + (list(call.args[:1]) if f.attr in PATH_TARGET_METHODS else [])
        if f.attr in WRITE_HELPERS:
            return _paths(call, len(call.args))
        if f.attr == "open" and _write_mode(call, method=True):
            return [f.value]
    elif isinstance(f, ast.Name):
        if f.id in WRITE_HELPERS:
            return _paths(call, len(call.args))
        if f.id in COPY_FUNCS:
            return _paths(call, 2)
        if f.id == "open" and _write_mode(call, method=False):
            return _paths(call, 1)
    return None


def _write_mode(call, *, method):
    idx = 0 if method else 1
    mode = None
    if len(call.args) > idx:
        mode = call.args[idx]
    for k in call.keywords:
        if k.arg == "mode":
            mode = k.value
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return False


def _os_open_writes(call):
    flags = call.args[1] if len(call.args) > 1 else None
    if flags is None:
        return False
    return any(isinstance(n, ast.Attribute) and n.attr in ("O_WRONLY", "O_RDWR", "O_CREAT")
               for n in ast.walk(flags))


def scan_source(src, rel):
    tree = ast.parse(src)
    findings = []
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for func in [tree] + funcs:
        tainted = _taint(func) if func is not tree else set()
        body_nodes = ast.walk(func) if func is not tree else (
            n for stmt in tree.body if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                             ast.ClassDef))
            for n in ast.walk(stmt))
        for node in body_nodes:
            if isinstance(node, ast.Call):
                args = _is_write_call(node)
                if args and any(_mentions(a, tainted) for a in args):
                    findings.append(f"{rel}:{node.lineno}")
    return sorted(set(findings), key=lambda s: int(s.rsplit(":", 1)[1]))


def scan_repo(root=REPO):
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if set(Path(rel).parts[:-1]) & SKIP_DIRS or rel in ALLOWED:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            out.extend(scan_source(src, rel))
        except SyntaxError:
            continue
    return out


def test_no_pair_file_handling_outside_store_pair():
    findings = scan_repo()
    assert not findings, (
        "I4: only cron/store_pair.py may copy/move/replace/rename/open-for-write jobs.json or "
        "runtime_state.db; route these through cron.store_pair:\n  " + "\n  ".join(findings))


def test_lint_catches_each_write_shape():
    src = '''
import os, shutil
from pathlib import Path
def a(home):
    p = home / "cron" / "jobs.json"
    shutil.copy2(p, "x")
def b(home):
    db = home / "cron" / "runtime_state.db"
    tmp = db.with_suffix(".tmp")
    os.replace(tmp, db)
def c(home):
    live = home / _CRON_JOBS_REL
    with open(live, "w") as f:
        f.write("x")
def d(home):
    (home / "cron" / "jobs.json").write_text("{}")
def e(snap):
    src = snap / _CRON_RUNTIME_REL
    src.rename(snap / "y")
def ok(home):
    with open(home / "cron" / "jobs.json") as f:
        return f.read()
'''
    hits = scan_source(src, "x.py")
    assert [h.split(":")[1] for h in hits] == ["6", "10", "13", "16", "19"]
