#!/usr/bin/env python3
"""Test-impact selection for the ``pull_request`` CI run.

Maps a PR's changed paths to the test files that can observe them, so the PR
run is a fast pre-filter instead of a second copy of the full suite.

SAFETY MODEL. This is a PRE-FILTER, never a gate. The merge_group run (the
only required gate — the ruleset has no PR status checks) and push:main always
run the full matrix. A false narrow here therefore costs a queue eviction, never a
broken main. The CLI returns full for every event but ``pull_request``.
Even so, every ambiguity widens:

* a changed path in ``_FULL_TRIGGERS`` (conftest, pytest/uv config, the test
  runner, workflows) -> ``None`` (caller runs the full matrix);
* a changed ``tests/**/conftest.py`` -> every test under that directory;
* a changed source module with no dependent test -> ``None``;
* a selection above ``_MAX_SHARE`` of the suite's duration -> ``None``.

MAPPING. A test file depends on module M when M is in the transitive closure of
what it references:

* test files: EVERY import (module, function and class scope) plus every
  dotted string literal that names a known module (``mock.patch("a.b.c")``,
  ``importlib.import_module("a.b")``), plus the imports of each ancestor
  ``conftest.py``;
* non-test modules: module-scope imports only (``if``/``try``/``with`` at module
  level included, ``if TYPE_CHECKING:`` excluded). Function-level imports are
  deliberately NOT followed: this codebase imports its god-modules lazily
  almost everywhere, and following those edges makes every module depend on
  every other (measured 2026-07-01: fan-in 1502/1935), i.e. selection
  degenerates to "always full". The full merge_group run covers those edges.
* importing ``a.b.c`` also depends on ``a`` and ``a.b`` (their ``__init__``
  runs), and ``from a.b import c`` may name submodule ``a.b.c``.
* any changed path whose repo-relative path or basename appears verbatim in a
  test file selects it (fixtures, ``scripts/*.py`` loaded by path, shell
  scripts, JSON/YAML data).

Stdlib only: runs in the ``generate`` job before any venv exists.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Iterable

# Paths whose change can affect every test file. Exact paths, or prefixes
# ending in "/".
_FULL_TRIGGERS: tuple[str, ...] = (
    "conftest.py",
    "tests/conftest.py",
    "pyproject.toml",
    "uv.lock",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "sitecustomize.py",
    "scripts/run_tests.sh",
    "scripts/run_tests_parallel.py",
    "scripts/ci/test_impact.py",
    "tests/sys_modules_leak_gate.py",
    ".github/",
)

# A selection costing more than this share of the whole suite is not worth a
# narrowed matrix: run the full one.
_MAX_SHARE = 0.6

_DEFAULT_DURATION = 2.0
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".worktrees"}
_TEST_SKIP_PARTS = {"integration", "e2e", "docker"}
_DOTTED_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


def _module_name(rel: str) -> str:
    """``a/b/c.py`` -> ``a.b.c``; ``a/b/__init__.py`` -> ``a.b``."""
    parts = rel[:-3].split("/") if rel.endswith(".py") else rel.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _prefixes(name: str) -> Iterable[str]:
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        yield ".".join(parts[:i])


def _is_test_file(rel: str) -> bool:
    return (
        rel.startswith("tests/")
        and rel.rsplit("/", 1)[-1].startswith("test_")
        and rel.endswith(".py")
    )


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _module_scope_nodes(body: list[ast.stmt]) -> Iterable[ast.stmt]:
    """Statements that execute at import time (module scope)."""
    for node in body:
        yield node
        if isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                yield from _module_scope_nodes(node.body)
            yield from _module_scope_nodes(node.orelse)
        elif isinstance(node, ast.Try) or (
            sys.version_info >= (3, 11) and isinstance(node, ast.TryStar)
        ):
            yield from _module_scope_nodes(node.body)
            for handler in node.handlers:
                yield from _module_scope_nodes(handler.body)
            yield from _module_scope_nodes(node.orelse)
            yield from _module_scope_nodes(node.finalbody)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield from _module_scope_nodes(node.body)


def _refs_from_import(node: ast.stmt, package: str) -> set[str]:
    out: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            out.update(_prefixes(alias.name))
    elif isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            pkg_parts = package.split(".") if package else []
            keep = len(pkg_parts) - (node.level - 1)
            if keep < 0:
                return out
            anchor = ".".join(pkg_parts[:keep])
            base = f"{anchor}.{base}" if anchor and base else (anchor or base)
        if not base:
            return out
        out.update(_prefixes(base))
        for alias in node.names:
            if alias.name != "*":
                out.add(f"{base}.{alias.name}")
    return out


class _Index:
    """Reverse dependency index over one checkout."""

    def __init__(self, repo_root: Path) -> None:
        self.root = repo_root
        self.test_files: list[str] = []
        self.modules: set[str] = set()
        # module-scope import edges of non-test modules: importer -> refs
        self.source_refs: dict[str, set[str]] = {}
        # every reference of a test file (imports anywhere + dotted strings)
        self.test_refs: dict[str, set[str]] = {}
        self.test_text: dict[str, str] = {}
        self.parse_failures: list[str] = []
        self._build()

    def _walk(self) -> Iterable[str]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    full = Path(dirpath) / fn
                    yield full.relative_to(self.root).as_posix()

    def _build(self) -> None:
        files = sorted(self._walk())
        self.modules = {_module_name(rel) for rel in files}
        for rel in files:
            try:
                text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
                with warnings.catch_warnings():
                    # Test-file docstring escapes are not our signal; keep CI logs clean.
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(text, filename=rel)
            except (OSError, SyntaxError, ValueError):
                self.parse_failures.append(rel)
                continue
            mod = _module_name(rel)
            package = mod if rel.endswith("__init__.py") else mod.rpartition(".")[0]
            if _is_test_file(rel):
                refs: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        refs |= _refs_from_import(node, package)
                    elif (
                        isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and _DOTTED_RE.match(node.value)
                    ):
                        refs.update(
                            p for p in _prefixes(node.value) if p in self.modules
                        )
                self.test_refs[rel] = refs
                self.test_text[rel] = text
                if not any(part in _TEST_SKIP_PARTS for part in rel.split("/")[:-1]):
                    self.test_files.append(rel)
            else:
                refs = set()
                for node in _module_scope_nodes(tree.body):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        refs |= _refs_from_import(node, package)
                self.source_refs[mod] = refs

    def affected_modules(self, changed_modules: set[str]) -> set[str]:
        """Changed modules plus every module that imports one at module scope."""
        reverse: dict[str, set[str]] = {}
        for importer, refs in self.source_refs.items():
            for ref in refs:
                reverse.setdefault(ref, set()).add(importer)
        affected = set(changed_modules)
        stack = list(changed_modules)
        while stack:
            name = stack.pop()
            for importer in reverse.get(name, ()):
                if importer not in affected:
                    affected.add(importer)
                    stack.append(importer)
        return affected


def _conftest_modules(test_rel: str) -> list[str]:
    parts = test_rel.split("/")[:-1]
    return [
        _module_name("/".join(parts[:i] + ["conftest.py"]))
        for i in range(1, len(parts) + 1)
    ]


def _full_trigger(path: str) -> bool:
    return any(
        path.startswith(t) if t.endswith("/") else path == t for t in _FULL_TRIGGERS
    )


def select(
    changed_paths: Iterable[str],
    repo_root: Path,
    durations: dict[str, float] | None = None,
    index: _Index | None = None,
) -> tuple[list[str] | None, str]:
    """Return ``(selected_test_files, reason)``; ``None`` means run the full suite."""
    paths = sorted({p.strip() for p in changed_paths if p.strip()})
    if not paths:
        return None, "empty diff"
    for p in paths:
        if "\\" in p or any(part in {"", ".", ".."} for part in p.split("/")):
            return None, f"malformed path {p!r}"
        if _full_trigger(p):
            return None, f"{p} can affect every test"
    idx = index or _Index(repo_root)
    selected: set[str] = set()
    changed_modules: set[str] = set()
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        if name == "conftest.py":
            prefix = p[: -len(name)]
            selected.update(t for t in idx.test_files if t.startswith(prefix))
            changed_modules.add(_module_name(p))
            continue
        if _is_test_file(p):
            if p in idx.test_files:
                selected.add(p)
            continue
        if p.endswith(".py"):
            changed_modules.add(_module_name(p))
        # Verbatim path / basename mention: data files, scripts loaded by path.
        for t in idx.test_files:
            text = idx.test_text[t]
            if p in text or name in text:
                selected.add(t)
    if changed_modules:
        affected = idx.affected_modules(changed_modules)
        for t in idx.test_files:
            refs = set(idx.test_refs.get(t, ()))
            for conf in _conftest_modules(t):
                refs |= idx.source_refs.get(conf, set())
            if refs & affected:
                selected.add(t)
    # Belt and braces: a changed non-test module that no selected test reaches
    # is a linkage the static graph cannot see (plugin loader, subprocess, CLI).
    for p in paths:
        if (
            p.endswith(".py")
            and not _is_test_file(p)
            and p.rsplit("/", 1)[-1] != "conftest.py"
            and (idx.root / p).exists()
            and not _reached_by_selection(idx, _module_name(p), selected)
        ):
            return None, f"{p}: no test depends on it statically"
    if not selected:
        return None, "no test file selected"
    durations = durations or {}
    total = sum(durations.get(t, _DEFAULT_DURATION) for t in idx.test_files) or 1.0
    chosen = sum(durations.get(t, _DEFAULT_DURATION) for t in selected)
    share = chosen / total
    if share > _MAX_SHARE:
        return None, f"selection is {share:.0%} of suite duration (> {_MAX_SHARE:.0%})"
    return sorted(selected), f"{len(selected)} files, {share:.1%} of suite duration"


def _reached_by_selection(idx: _Index, mod: str, selected: set[str]) -> bool:
    """True when some selected test depends on ``mod`` through the graph or
    mentions its file verbatim."""
    affected = idx.affected_modules({mod})
    rel_hint = mod.replace(".", "/")
    for t in selected:
        refs = set(idx.test_refs.get(t, ()))
        for conf in _conftest_modules(t):
            refs |= idx.source_refs.get(conf, set())
        if refs & affected or rel_hint in idx.test_text.get(t, ""):
            return True
    # Basename mention (e.g. a script loaded via spec_from_file_location).
    base = mod.rsplit(".", 1)[-1] + ".py"
    return any(base in idx.test_text.get(t, "") for t in selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--durations", default=None, help="test_durations.json")
    parser.add_argument(
        "--event",
        required=True,
        help="github.event_name. Anything but pull_request selects the full suite.",
    )
    parser.add_argument(
        "--full-slices",
        type=int,
        default=16,
        help="Slice count of the full matrix; the narrowed matrix scales from it.",
    )
    parser.add_argument(
        "--files-out",
        default=None,
        help="Write the selected test files here, one per line (only when narrowed).",
    )
    parser.add_argument(
        "changed", nargs="*", help="changed paths (default: read stdin, one per line)"
    )
    args = parser.parse_args(argv)
    if args.event != "pull_request":
        # merge_group and push:main are the gate: never narrowed.
        files, reason, slices = None, f"event {args.event!r} always runs full", 0
    else:
        changed = args.changed or sys.stdin.read().splitlines()
        durations: dict[str, float] = {}
        if args.durations and Path(args.durations).is_file():
            durations = json.loads(Path(args.durations).read_text(encoding="utf-8"))
        idx = _Index(Path(args.repo_root))
        files, reason = select(changed, idx.root, durations, idx)
        slices = 0
        if files is not None:
            total = sum(durations.get(t, _DEFAULT_DURATION) for t in idx.test_files)
            chosen = sum(durations.get(t, _DEFAULT_DURATION) for t in files)
            share = chosen / total if total else 1.0
            slices = max(1, min(args.full_slices, math.ceil(share * args.full_slices)))
            slices = min(slices, len(files))
            if args.files_out:
                Path(args.files_out).write_text("\n".join(files) + "\n", encoding="utf-8")
    print(f"Test impact: {reason}", file=sys.stderr)
    # slices == 0 means: run the full matrix.
    print(json.dumps({"slices": slices, "reason": reason, "count": len(files or [])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
