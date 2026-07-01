#!/usr/bin/env python3
"""Change-based test selection — map a PR's changed files to the test files that
could be affected, so CI runs a subset instead of all ~1,935 files.

INPUT  (stdin): newline-separated changed paths (same contract as
``classify_changes.py`` — the ``detect-changes`` action already produces this).
OUTPUT (stdout): EITHER the sentinel ``ALL`` (fail-open: run the whole suite),
OR a newline-separated list of repo-relative test files to run.

═══════════════════════════════════════════════════════════════════════════
SAFETY MODEL (this is the whole point — read before touching)
═══════════════════════════════════════════════════════════════════════════
A wrongly-SKIPPED test ships a bug. So selection is an *optimization* whose
correctness rests entirely on FAILING OPEN: whenever we cannot prove the
affected set with confidence, we output ``ALL`` and run everything. Ambiguity
⇒ run-all, NEVER ⇒ skip.

The layered guarantees (the merge queue was one option; it's unavailable on a
personal-account repo, so these carry the whole weight):

1. **Fail-open triggers → ALL.** Any of: a ``.github/`` change, a conftest /
   shared-fixture / ``pyproject.toml`` / ``uv.lock`` / lockfile / CI-config
   change, a "hub" module whose fan-in exceeds a threshold, a changed ``.py``
   with no static importer (new file / parse error), a changed file we can't
   map, or a change set larger than ``MAX_CHANGED_FILES``.

2. **Dynamic-loader blindness → ALL (Opus B2).** Static AST import graphs do
   NOT see ``registry.register`` / ``tools/*.py`` auto-import / entry-point /
   plugin auto-discovery. A leaf reached *through* a loader has no static
   importer, so its reverse-closure is empty → we'd select ~zero tests → ship
   the bug. Any change under a DYNAMIC_LOADER_DIR forces ALL.

3. **The runner still runs the FULL suite on push:[main]** (post-merge) — a
   wrong narrowing is caught there and alerted, before it can compound.

4. **Shadow-first rollout** — CI computes+logs the would-select set but runs
   FULL until a mutation-based Phase-0 proves zero false-narrows.

This script is stdlib-only (runs in the CI ``detect`` job, no deps).
"""
from __future__ import annotations

import ast
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

# ── Fail-open sentinel ──────────────────────────────────────────────────────
ALL = "ALL"

# ── Tunables (conservative defaults; tune from shadow-mode data, never looser
#    toward the speed win on a small sample — Opus "principled floor" note). ──
# A source module imported by more than this many test files is a "hub":
# changing it plausibly affects most of the suite, so just run everything.
HUB_FANIN = int(os.environ.get("HERMES_SELECT_HUB_FANIN", "150"))
# A change set larger than this is likely broad — run everything.
MAX_CHANGED_FILES = int(os.environ.get("HERMES_SELECT_MAX_CHANGED", "40"))

# ── Paths that ALWAYS force the full suite (fail-open) ──────────────────────
# CI config, dependency manifests, and shared test infrastructure change
# behavior globally — never narrow on them.
_FULL_SUITE_EXACT = {
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "conftest.py",
    "requirements.txt",
}
_FULL_SUITE_PREFIX = (
    ".github/",          # workflows, actions — CI itself
)
# Shared test infra: a conftest or fixtures/helpers module anywhere under tests/
# is imported by many files → run everything.
_SHARED_TEST_PARTS = ("conftest.py",)
_SHARED_TEST_DIR_PARTS = ("fixtures", "helpers", "_helpers", "support")

# ── Dynamic-loader dirs: static AST can't trace these → force ALL (B2) ──────
# A change to a plugin/tool/loader is reached at runtime via auto-discovery,
# so its static reverse-closure is unreliable. Fail open.
_DYNAMIC_LOADER_DIRS = (
    "plugins/",
    "hermes_tools/",       # auto-imported tool modules
    "tools/",
)
# Modules whose NAME implies dynamic dispatch/registration anywhere in the tree.
_DYNAMIC_LOADER_NAME_HINTS = ("registry", "plugin_loader", "autodiscover", "__init__")

# ── What counts as a source vs test file ────────────────────────────────────
def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py") and "/tests/" in f"/{rel}" or (
        rel.startswith("tests/") and name.startswith("test_") and name.endswith(".py")
    )


def _is_python(rel: str) -> bool:
    return rel.endswith(".py")


# ── Module-name ⇄ path resolution ───────────────────────────────────────────
def _path_to_module(rel: str, source_roots: Set[str]) -> Optional[str]:
    """Best-effort convert a repo-relative .py path to an importable module name.

    The repo puts importable packages at the root (e.g. ``run_agent.py`` →
    ``run_agent``; ``hermes_cli/models.py`` → ``hermes_cli.models``). A leading
    source-root prefix (``src/``) is stripped. ``__init__.py`` maps to the
    package. Returns None if it doesn't look importable.
    """
    if not rel.endswith(".py"):
        return None
    p = rel
    for root in source_roots:
        if p.startswith(root + "/"):
            p = p[len(root) + 1:]
            break
    p = p[:-3]  # drop .py
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".") if p else None


def _imported_names(tree: ast.Module) -> Set[str]:
    """Every dotted module name referenced by *module-level* import statements.

    CRITICAL: only TOP-LEVEL imports (in ``tree.body``, and inside plain
    ``if``/``try`` blocks at module scope) count. Imports nested inside a
    function/method body are LAZY — they execute only when that function is
    called, NOT when the module is imported — so they do NOT couple the two
    modules at test-collection time. Counting them massively over-connects the
    graph (measured: it made ~every module transitively import the god-file
    ``gateway.run`` via lazy chains, collapsing selection to all-or-nothing).

    ``if TYPE_CHECKING:`` imports are also excluded — they never execute at
    runtime at all.
    """
    names: Set[str] = set()

    def _visit_block(body: list) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    names.add(node.module)
            elif isinstance(node, ast.If):
                # Skip `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` blocks.
                test = node.test
                is_type_checking = (
                    (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
                    or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
                )
                if not is_type_checking:
                    _visit_block(node.body)
                _visit_block(node.orelse)
            elif isinstance(node, ast.Try):
                # Module-level try/except import fallbacks are real top-level imports.
                _visit_block(node.body)
                for handler in node.handlers:
                    _visit_block(handler.body)
                _visit_block(node.orelse)
                _visit_block(node.finalbody)
            elif isinstance(node, ast.With):
                _visit_block(node.body)
            # Function/class bodies are NOT recursed — their imports are lazy.

    _visit_block(tree.body)
    return names


class ImportGraph:
    """Reverse import map: source module → set of test files that (transitively)
    import it. Built by AST-parsing every .py in the repo once.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.source_roots: Set[str] = {"src"} if (repo_root / "src").is_dir() else set()
        # module name → set of module names it imports directly
        self._imports: Dict[str, Set[str]] = defaultdict(set)
        # module name → its repo-relative path
        self._mod_to_path: Dict[str, str] = {}
        # repo-relative test path → set of module names it imports directly
        self._test_imports: Dict[str, Set[str]] = {}
        self._parse_errors: Set[str] = set()
        self._build()

    def _build(self) -> None:
        for path in self.repo_root.rglob("*.py"):
            if any(part in (".git", "__pycache__", ".venv", "venv", "node_modules")
                   for part in path.parts):
                continue
            rel = path.relative_to(self.repo_root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
            except (SyntaxError, ValueError):
                self._parse_errors.add(rel)
                continue
            imported = _imported_names(tree)
            if rel.startswith("tests/") and path.name.startswith("test_"):
                self._test_imports[rel] = imported
            mod = _path_to_module(rel, self.source_roots)
            if mod:
                self._mod_to_path[mod] = rel
                self._imports[mod] |= imported

    # ── forward closure: module → all modules it transitively imports ──
    def _forward_closure(self, start: Set[str]) -> Set[str]:
        seen: Set[str] = set()
        stack = list(start)
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            for dep in self._imports.get(m, ()):
                # normalize: keep only modules we actually know (map to a path)
                if dep in self._imports or dep in self._mod_to_path:
                    if dep not in seen:
                        stack.append(dep)
                # also add parent packages (a.b.c → a.b, a)
                parts = dep.split(".")
                for i in range(1, len(parts)):
                    pref = ".".join(parts[:i])
                    if (pref in self._imports or pref in self._mod_to_path) and pref not in seen:
                        stack.append(pref)
        return seen

    def tests_importing(self, target_modules: Set[str]) -> Set[str]:
        """Every test file whose transitive import set intersects target_modules."""
        hits: Set[str] = set()
        for test_rel, direct in self._test_imports.items():
            closure = self._forward_closure(set(direct))
            if closure & target_modules:
                hits.add(test_rel)
        return hits

    def fanin(self, module: str) -> int:
        """How many test files transitively import this module (hub detection)."""
        return len(self.tests_importing({module}))

    @property
    def all_test_files(self) -> List[str]:
        return sorted(self._test_imports)

    def module_for(self, rel: str) -> Optional[str]:
        return _path_to_module(rel, self.source_roots)

    def known_module(self, mod: Optional[str]) -> bool:
        return bool(mod) and (mod in self._imports or mod in self._mod_to_path)


# ── Fail-open predicates ────────────────────────────────────────────────────
def _forces_full_suite(rel: str) -> Optional[str]:
    """Return a reason string if this changed path forces the full suite, else None."""
    name = rel.rsplit("/", 1)[-1]
    if rel in _FULL_SUITE_EXACT or name in _FULL_SUITE_EXACT:
        return f"dependency/CI/config file ({rel})"
    if any(rel.startswith(pfx) for pfx in _FULL_SUITE_PREFIX):
        return f"CI-config path ({rel})"
    if name in _SHARED_TEST_PARTS:
        return f"shared test infra: conftest ({rel})"
    parts = rel.split("/")
    if parts[0] == "tests" and any(dp in parts for dp in _SHARED_TEST_DIR_PARTS):
        return f"shared test fixtures/helpers ({rel})"
    if any(rel.startswith(d) for d in _DYNAMIC_LOADER_DIRS):
        return f"dynamic-loader dir — static graph is blind here ({rel})"
    mod_name = name[:-3] if name.endswith(".py") else name
    if mod_name in _DYNAMIC_LOADER_NAME_HINTS and name.endswith(".py"):
        return f"dynamic-dispatch module ({rel})"
    return None


def select(changed: List[str], repo_root: Path) -> "str | List[str]":
    """Return ALL (sentinel string) or a sorted list of test files to run."""
    changed = [c.strip() for c in changed if c.strip()]

    if not changed:
        return ALL  # empty diff (push/dispatch/merge) → run everything

    if len(changed) > MAX_CHANGED_FILES:
        return ALL  # broad change set → run everything

    # Any single fail-open trigger short-circuits to ALL.
    for rel in changed:
        reason = _forces_full_suite(rel)
        if reason:
            print(f"# select: FULL SUITE — {reason}", file=sys.stderr)
            return ALL

    # A changed non-Python file we don't understand → fail open. (Assets/docs
    # are already handled upstream by classify_changes; if selection runs at
    # all, be conservative about anything non-.py that isn't provably inert.)
    non_py = [c for c in changed if not _is_python(c)]
    # Docs/markdown that classify already treats as python-irrelevant are safe
    # to ignore for *selection* (they won't be in `changed` when python=false).
    # But a stray non-.py that reached here (a .json fixture, a .sh) → ALL.
    if non_py:
        print(f"# select: FULL SUITE — non-python changed files present: {non_py}", file=sys.stderr)
        return ALL

    graph = ImportGraph(repo_root)

    # A changed .py that fails to parse, or that we can't resolve to a known
    # module → fail open.
    target_modules: Set[str] = set()
    changed_tests: Set[str] = set()
    for rel in changed:
        if rel in graph._parse_errors:
            print(f"# select: FULL SUITE — parse error in changed file ({rel})", file=sys.stderr)
            return ALL
        if rel.startswith("tests/") and rel.rsplit("/", 1)[-1].startswith("test_"):
            # A changed test file always runs itself.
            changed_tests.add(rel)
            continue
        mod = graph.module_for(rel)
        if mod is None or not graph.known_module(mod):
            print(f"# select: FULL SUITE — unmapped/new source module ({rel})", file=sys.stderr)
            return ALL
        # Hub check: a high-fan-in module affects most of the suite.
        fi = graph.fanin(mod)
        if fi > HUB_FANIN:
            print(f"# select: FULL SUITE — hub module {mod} (fan-in {fi} > {HUB_FANIN})", file=sys.stderr)
            return ALL
        target_modules.add(mod)

    selected: Set[str] = set(changed_tests)
    if target_modules:
        selected |= graph.tests_importing(target_modules)

    # Belt-and-suspenders: if we somehow selected nothing from a real source
    # change, fail open rather than skip everything.
    if target_modules and not (selected - changed_tests):
        print("# select: FULL SUITE — source change resolved to zero dependent tests "
              "(possible dynamic linkage)", file=sys.stderr)
        return ALL

    return sorted(selected)


def main() -> int:
    repo_root = Path(os.environ.get("HERMES_REPO_ROOT", ".")).resolve()
    changed = sys.stdin.read().splitlines()
    result = select(changed, repo_root)
    if isinstance(result, str):  # ALL sentinel
        print(result)
    else:
        total = 0
        try:
            total = len(ImportGraph(repo_root).all_test_files)
        except Exception:
            pass
        print("\n".join(result))
        print(f"# select: {len(result)}/{total or '?'} test files", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
