"""Caller-impact check for Python edits (``file_tools.impact_check``, off by default).

After a ``.py`` write, compare the module-level function/class signatures before and after the
edit. When one changed or was removed, find the repo's other ``.py`` files that import it
(``git grep`` for the name, then ``ast``), and report the calls/imports that the old signature
accepted and the new one rejects, as ``path:line: message``. Static ``ast`` only: no import, no
type checker, no subprocess beyond one ``git rev-parse``, one ``git grep`` and one batched read.
Reports only; it never refuses or alters the write. Dynamic calls (``getattr``, ``*args``/``**kw``
splats, monkeypatched fakes, methods on instances) are out of its reach.
"""

import ast
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MAX_FILES = 200
MAX_REPORT = 20


@dataclass(frozen=True)
class Sig:
    positional: tuple[str, ...]  # posonly + regular params, in order
    posonly: int
    n_required_pos: int
    vararg: bool
    kwonly: tuple[str, ...]
    kwonly_required: tuple[str, ...]
    kwarg: bool

    def problem(self, call: ast.Call) -> Optional[str]:
        """Why this call no longer binds, or None. A call with ``*``/``**`` splats is not judged."""
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            return None
        npos = len(call.args)
        if npos > len(self.positional) and not self.vararg:
            return f"takes {len(self.positional)} positional arguments but {npos} were given"
        filled = set(self.positional[:npos])
        for kw in call.keywords:
            name = kw.arg
            ok = (name in self.positional[self.posonly:]) or name in self.kwonly
            if not ok and not self.kwarg:
                return f"got an unexpected keyword argument '{name}'"
            filled.add(name)
        missing = [p for p in self.positional[:self.n_required_pos] if p not in filled]
        missing += [k for k in self.kwonly_required if k not in filled]
        if missing:
            return "missing required argument(s): " + ", ".join(repr(m) for m in missing)
        return None


def _sig(args: ast.arguments, drop_first: bool = False) -> Sig:
    posonly = [a.arg for a in args.posonlyargs]
    regular = [a.arg for a in args.args]
    positional = posonly + regular
    n_defaults = len(args.defaults)
    n_posonly = len(posonly)
    if drop_first and positional:
        positional = positional[1:]
        n_posonly = max(0, n_posonly - 1)
    kwonly = [a.arg for a in args.kwonlyargs]
    kw_required = [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
    return Sig(tuple(positional), n_posonly, len(positional) - n_defaults, args.vararg is not None,
               tuple(kwonly), tuple(kw_required), args.kwarg is not None)


def module_signatures(src: str) -> dict[str, Optional[Sig]]:
    """Module-level ``def``/``class`` names -> call signature (a class -> its ``__init__`` minus
    ``self``; None when the class body defines no ``__init__``, i.e. not statically known)."""
    out: dict[str, Optional[Sig]] = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = _sig(node.args)
        elif isinstance(node, ast.ClassDef):
            init = next((n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.name == "__init__"), None)
            out[node.name] = _sig(init.args, drop_first=True) if init is not None else None
    return out


def signature_delta(pre: str, post: str) -> tuple[dict[str, tuple[Sig, Sig]], set[str]]:
    """(changed name -> (old, new) for names whose known signature changed, removed names)."""
    old, new = module_signatures(pre), module_signatures(post)
    changed = {n: (old[n], new[n]) for n in old.keys() & new.keys()
               if old[n] is not None and new[n] is not None and old[n] != new[n]}
    return changed, set(old) - set(new)


_HEADER = re.compile(r"(?:async[ \t]+def|def|class)[ \t]|[ \t]+(?:async[ \t]+)?def[ \t]+__init__\b")


def _header_end(lines: list[str], i: int) -> int:
    """Index of the line closing the def/class header that starts at ``i`` (its trailing ``:``)."""
    for j in range(i, min(i + 60, len(lines))):
        if lines[j].split("#", 1)[0].rstrip().endswith(":"):
            return j
    return i


def touches_signature(pre: str, post: str) -> bool:
    """Cheap text prefilter for ``signature_delta``: True iff the changed line range of this edit
    overlaps a module-level ``def``/``class`` header or a ``def __init__`` header (multi-line
    headers included) in the old or new text. A body-only edit costs a line compare, no parse."""
    a, b = pre.splitlines(), post.splitlines()
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    s = 0
    while s < len(a) - p and s < len(b) - p and a[-1 - s] == b[-1 - s]:
        s += 1
    for lines, end in ((a, len(a) - s), (b, len(b) - s)):
        if any(_HEADER.match(line) for line in lines[p:end]):
            return True
        for h in range(p - 1, max(-1, p - 61), -1):
            if _HEADER.match(lines[h]):
                if _header_end(lines, h) >= p:
                    return True
                break
    return False


def _module_names(rel_path: str) -> set[str]:
    """Every dotted name an importer might use for ``rel_path``: the full repo-relative path and
    each tail of it (``src/pkg/mod.py`` -> ``src.pkg.mod``, ``pkg.mod``, ``mod``)."""
    parts = rel_path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts[i:]) for i in range(len(parts))} if parts else set()


def _resolve_from(importer_rel: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    pkg = importer_rel.split("/")[:-1]
    base = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
    return ".".join([*base, node.module] if node.module else base)


def _dotted(expr: ast.expr) -> str:
    """``a.b.c`` for a Name/Attribute chain, "" for anything else."""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return ""
    return ".".join([expr.id, *reversed(parts)])


def broken_callers(importer_rel: str, src: str, mod_names: set[str],
                   changed: dict[str, tuple[Sig, Sig]], removed: set[str]) -> list[str]:
    """``path:line: message`` for each import/call in ``src`` the edit broke."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    names: dict[str, str] = {}   # local name -> changed/removed def name
    modules: set[str] = set()    # local names bound to the edited module
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in mod_names:
                    modules.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom):
            src_mod = _resolve_from(importer_rel, node)
            for a in node.names:
                if src_mod in mod_names:
                    if a.name in removed:
                        out.append(f"{importer_rel}:{node.lineno}: ImportError: cannot import name "
                                   f"'{a.name}' (removed by this edit)")
                    elif a.name in changed:
                        names[a.asname or a.name] = a.name
                elif f"{src_mod}.{a.name}".lstrip(".") in mod_names:
                    modules.add(a.asname or a.name)
    if not names and not modules:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _dotted(node.value) in modules \
                and node.attr in removed and isinstance(node.ctx, ast.Load):
            out.append(f"{importer_rel}:{node.lineno}: AttributeError: '{node.attr}' "
                       f"(removed by this edit)")
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        target = None
        if isinstance(f, ast.Name) and f.id in names:
            target = names[f.id]
        elif isinstance(f, ast.Attribute) and f.attr in changed and _dotted(f.value) in modules:
            target = f.attr
        if target is None:
            continue
        old, new = changed[target]
        if old.problem(node) is None and (why := new.problem(node)):
            out.append(f"{importer_rel}:{node.lineno}: TypeError: {target}() {why}")
    return out


def check_callers(ops, path: str, pre: str, post: str) -> list[str]:
    """Broken callers of ``path``'s changed signatures across its git repo, via ``ops._exec``
    (works on every terminal backend). Any failure -> []: this never breaks a write."""
    try:
        if not touches_signature(pre, post):
            return []
        changed, removed = signature_delta(pre, post)
        if not changed and not removed:
            return []
        q = ops._escape_shell_arg
        top = ops._exec(f"git -C {q(os.path.dirname(path) or '.')} rev-parse --show-toplevel 2>/dev/null",
                        timeout=10)
        root = top.stdout.strip().splitlines()[-1] if top.exit_code == 0 and top.stdout.strip() else ""
        if not root:
            return []
        rel = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
        pats = " ".join(f"-e {q(n)}" for n in sorted(changed.keys() | removed))
        grep = ops._exec(f"git -C {q(root)} grep --untracked -l -w -F {pats} -- '*.py' 2>/dev/null", timeout=20)
        files = [f for f in grep.stdout.splitlines() if f.strip() and f != rel][:MAX_FILES]
        if not files:
            return []
        mark = f"@@impact-{uuid.uuid4().hex}@@"
        reads = ops._exec(f"cd {q(root)} && for f in {' '.join(q(f) for f in files)}; do "
                          f"printf '%s %s\\n' {q(mark)} \"$f\"; cat -- \"$f\"; echo; done", timeout=20)
        mod_names = _module_names(rel)
        report: list[str] = []
        for chunk in reads.stdout.split(mark + " ")[1:]:
            name, _, body = chunk.partition("\n")
            report += broken_callers(name.strip(), body, mod_names, changed, removed)
        if len(report) > MAX_REPORT:
            report = report[:MAX_REPORT] + [f"... {len(report) - MAX_REPORT} more"]
        return report
    except Exception:
        logger.debug("impact check failed for %s", path, exc_info=True)
        return []


def impact_check_enabled() -> bool:
    """``file_tools.impact_check: true`` in config.yaml; anything else (absent, unreadable) is off."""
    try:
        from hermes_cli.config import load_config_readonly
        section = load_config_readonly().get("file_tools")
        return isinstance(section, dict) and section.get("impact_check") is True
    except Exception:
        return False
