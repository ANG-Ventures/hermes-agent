#!/usr/bin/env python3
"""Guard: an @asynccontextmanager must not burn a permit if its PRE-YIELD region raises.

THE CLASS
---------
An ``@asynccontextmanager`` async generator that acquires a permit (a
``Semaphore``/``Lock``/``BoundedSemaphore``) *before* its first ``yield`` has
exactly one release path for that window.  A generator that raises before its
first ``yield`` never runs ``__aexit__``, so the ``try/finally`` wrapped around
the ``yield`` never executes.  If anything between the acquire and the yield
raises — a cancel delivered at an await, a bookkeeping helper that starts
raising after a refactor, a logging call with a bad format arg — the permit is
burned permanently.  Repeat it ``cap`` times and the gate is dead until the
process restarts.

Live instances:
  * ``gateway/turn_admission.py::TurnAdmission.slot``            (PR #827)
  * ``hermes_cli/session_db_heavy_gate.py::session_db_heavy_read_slot``

THE DISCRIMINATOR (and why the obvious one is wrong)
----------------------------------------------------
#827's class sweep asked "is there an ``await`` between the acquire and the
yield?", reasoning that only a cancel can fire there and a cancel is only
delivered at an await.  That cleared site 2, which has zero awaits in its
window — and site 2 leaked anyway.  #827's *own* regression test injects a
**synchronous** raise (it monkeypatches ``logger.info`` to raise), so the class
the PR gates is "the pre-yield region raises for ANY reason", not "a cancel
lands at an await".

This guard therefore asks the right question:

  1. does any statement between the acquire and the first following ``yield``
     contain a raise-capable node (``Call`` / ``Attribute`` / ``Subscript`` /
     ``Await``)?  — not "is there an await?"
  2. if so, is that node enclosed in a ``try`` whose ``except BaseException`` /
     bare ``except`` handler, or whose ``finally``, calls ``.release()``?

A raise-capable node that answers yes to (1) and no to (2) is a violation.

DOES NOT COVER (stated boundary, not a hidden gap)
--------------------------------------------------
  * **The acquiring statement itself.**  ``await asyncio.wait_for(sem.acquire(),
    ...)`` raising means the acquire FAILED, so there is nothing to release.
    (``wait_for``'s own acquire/timeout race is a separate, upstream concern.)
  * **Statements inside the ``except``/``finally`` blocks of the try that wraps
    the acquire.**  A raise in such a ``finally`` is the same class in
    principle, but there are zero live offender sites (the only real one is
    ``notice.cancel()`` on an ``asyncio.Task``, which does not raise), and
    widening here flags correct code.  The same applies to the release
    machinery itself: ``semaphore.release()`` inside the very
    ``except BaseException`` this guard demands is not reported against itself
    (``Semaphore.release`` does not raise).
  * **Release through an alias** (``rel = sem.release`` then ``rel()``).  No
    live site spells it that way; a new spelling is WONTFIX unless it appears
    in production code.

Exemption: put ``noqa: preyield-permit`` in a comment on the acquiring line or
within the four lines above it when a site genuinely must not release.

Exit codes:
  0 — no violations
  1 — violations found
  2 — script error (including: ZERO sites enumerated, i.e. vacuously green)

Usage:
  python scripts/check_preyield_permit_release.py [paths...]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Methods whose call acquires a permit that must be handed back.
_ACQUIRE_METHODS = {"acquire"}
_RELEASE_METHODS = {"release"}

# Node types that can raise. Deliberately broad: the whole point of this guard
# is that "there is an await here" is too narrow a discriminator.
_RAISE_CAPABLE = (ast.Call, ast.Attribute, ast.Subscript, ast.Await)

EXEMPT_MARKER = "noqa: preyield-permit"

# Directories that are not part of the repo's own source.
_SKIP_PARTS = {
    ".git",
    ".worktrees",
    "node_modules",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    "site-packages",
}


def _decorator_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for dec in getattr(node, "decorator_list", []) or []:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _own_body_nodes(fn: ast.AST):
    """Yield every node inside ``fn`` except those in a NESTED function/lambda.

    A nested coroutine has its own lifecycle; its statements do not run in the
    outer generator's pre-yield window.
    """
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Do not descend: the nested callable's body runs on its own
            # lifecycle, not inside this generator's pre-yield window.
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def _calls_named(node: ast.AST, names: set[str]) -> bool:
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in names
        ):
            return True
    return False


def _build_parent_map(fn: ast.AST) -> dict[int, tuple[ast.AST, str]]:
    """Map id(child) -> (parent, field_name) for everything in ``fn``'s own body."""
    parents: dict[int, tuple[ast.AST, str]] = {}
    stack: list[ast.AST] = [fn]
    while stack:
        node = stack.pop()
        for field, value in ast.iter_fields(node):
            children = value if isinstance(value, list) else [value]
            for child in children:
                if not isinstance(child, ast.AST):
                    continue
                if node is not fn and isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                ):
                    continue
                parents[id(child)] = (node, field)
                stack.append(child)
    return parents


def _is_baseexception_handler(handler: ast.ExceptHandler) -> bool:
    """A bare ``except:`` or an ``except BaseException`` catches a cancel."""
    if handler.type is None:
        return True
    targets = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    for target in targets:
        name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else None
        )
        if name == "BaseException":
            return True
    return False


def _try_releases(node: ast.Try) -> bool:
    for handler in node.handlers:
        if _is_baseexception_handler(handler) and any(
            _calls_named(stmt, _RELEASE_METHODS) for stmt in handler.body
        ):
            return True
    return any(_calls_named(stmt, _RELEASE_METHODS) for stmt in node.finalbody)


def _releases_on_the_way_out(node: ast.AST, parents: dict[int, tuple[ast.AST, str]]) -> bool:
    """True if ``node`` sits in the BODY of a try that releases on the way out."""
    current: ast.AST | None = node
    while current is not None:
        entry = parents.get(id(current))
        if entry is None:
            return False
        parent, field = entry
        if isinstance(parent, ast.Try) and field == "body" and _try_releases(parent):
            return True
        current = parent
    return False


def _in_release_machinery(node: ast.AST, parents: dict[int, tuple[ast.AST, str]]) -> bool:
    """True if ``node`` IS part of a releasing handler/finally — the guard itself.

    ``semaphore.release()`` inside the very ``except BaseException`` this check
    demands must not be reported as an unguarded raise-capable node; a fault in
    the release machinery is the same DOES-NOT-COVER boundary as the acquire
    failure path (``Semaphore.release`` does not raise).
    """
    current: ast.AST | None = node
    while current is not None:
        entry = parents.get(id(current))
        if entry is None:
            return False
        parent, field = entry
        if (
            isinstance(parent, ast.Try)
            and field in {"handlers", "finalbody"}
            and _try_releases(parent)
        ):
            return True
        current = parent
    return False


def _enclosing_stmt(node: ast.AST, parents: dict[int, tuple[ast.AST, str]]) -> ast.stmt | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.stmt):
            return current
        entry = parents.get(id(current))
        current = entry[0] if entry else None
    return None


def _tries_wrapping(node: ast.AST, parents: dict[int, tuple[ast.AST, str]]) -> set[int]:
    """ids of ``Try`` nodes that hold ``node`` in their ``body``."""
    found: set[int] = set()
    current: ast.AST | None = node
    while current is not None:
        entry = parents.get(id(current))
        if entry is None:
            break
        parent, field = entry
        if isinstance(parent, ast.Try) and field == "body":
            found.add(id(parent))
        current = parent
    return found


def _in_acquire_failure_path(
    node: ast.AST,
    parents: dict[int, tuple[ast.AST, str]],
    acquire_tries: set[int],
) -> bool:
    """True if ``node`` lives in the except/finally of a try wrapping the acquire.

    Those run when the acquire FAILED (or alongside the failure), so there is
    no permit to hand back — see the DOES NOT COVER section in the docstring.
    """
    current: ast.AST | None = node
    while current is not None:
        entry = parents.get(id(current))
        if entry is None:
            return False
        parent, field = entry
        if (
            isinstance(parent, ast.Try)
            and id(parent) in acquire_tries
            and field in {"handlers", "finalbody"}
        ):
            return True
        current = parent
    return False


def _exempt(lines: list[str], lineno: int) -> bool:
    window = lines[max(0, lineno - 5) : lineno]
    return any(EXEMPT_MARKER in line for line in window)


def scan_source(source: str, filepath: str) -> tuple[list[dict], list[dict]]:
    """Return (sites, violations) for one module's text.

    ``sites`` is every enumerated acquire-before-yield window (the covered set —
    assert on it, a guard that enumerates nothing is vacuously green).
    """
    tree = ast.parse(source)
    lines = source.split("\n")
    sites: list[dict] = []
    violations: list[dict] = []

    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        if "asynccontextmanager" not in _decorator_names(fn):
            continue

        parents = _build_parent_map(fn)
        body_nodes = list(_own_body_nodes(fn))

        acquires = [
            node
            for node in body_nodes
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _ACQUIRE_METHODS
        ]
        if not acquires:
            continue
        acquire = min(acquires, key=lambda n: (n.lineno, n.col_offset))

        acquire_stmt = _enclosing_stmt(acquire, parents)
        window_start = (
            getattr(acquire_stmt, "end_lineno", None) or acquire.lineno
            if acquire_stmt is not None
            else acquire.lineno
        )
        acquire_tries = _tries_wrapping(acquire, parents)

        yields = [
            node
            for node in body_nodes
            if isinstance(node, (ast.Yield, ast.YieldFrom))
            and node.lineno > acquire.lineno
        ]
        if not yields:
            # No yield after the acquire: not the pre-yield window this guard
            # governs (the permit is not handed to a caller at all here).
            continue
        first_yield = min(yields, key=lambda n: (n.lineno, n.col_offset))

        site = {
            "file": filepath,
            "function": fn.name,
            "acquire_line": acquire.lineno,
            "yield_line": first_yield.lineno,
            "exempt": _exempt(lines, acquire.lineno),
        }
        sites.append(site)
        if site["exempt"]:
            continue

        offenders = []
        for node in body_nodes:
            if not isinstance(node, _RAISE_CAPABLE):
                continue
            if not (window_start < node.lineno < first_yield.lineno):
                continue
            if _in_acquire_failure_path(node, parents, acquire_tries):
                continue
            if _in_release_machinery(node, parents):
                continue
            if _releases_on_the_way_out(node, parents):
                continue
            offenders.append(node)

        if offenders:
            worst = min(offenders, key=lambda n: (n.lineno, n.col_offset))
            violations.append({
                "file": filepath,
                "function": fn.name,
                "acquire_line": acquire.lineno,
                "yield_line": first_yield.lineno,
                "first_unguarded_line": worst.lineno,
                "unguarded_count": len(offenders),
                "snippet": lines[worst.lineno - 1].strip()[:110],
            })

    return sites, violations


def iter_python_files(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            found.append(root)
            continue
        for path in root.rglob("*.py"):
            if _SKIP_PARTS & set(path.parts):
                continue
            found.append(path)
    return sorted(set(found))


def main(argv: list[str]) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    roots = [Path(a).resolve() for a in argv[1:]] or [repo_root]

    all_sites: list[dict] = []
    all_violations: list[dict] = []
    scanned = 0

    for path in iter_python_files(roots):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "asynccontextmanager" not in source:
            continue
        try:
            rel = str(path.relative_to(repo_root))
        except ValueError:
            rel = str(path)
        try:
            sites, violations = scan_source(source, rel)
        except SyntaxError:
            continue
        scanned += 1
        all_sites.extend(sites)
        all_violations.extend(violations)

    if not all_sites:
        print(
            "❌ enumerated ZERO acquire-before-yield sites across "
            f"{scanned} @asynccontextmanager module(s) — this guard is now "
            "vacuously green; the discovery shape is broken.",
            file=sys.stderr,
        )
        return 2

    if all_violations:
        print(f"❌ {len(all_violations)} @asynccontextmanager pre-yield permit leak(s):")
        for v in all_violations:
            print(
                f"  {v['file']}:{v['first_unguarded_line']}: {v['function']}() "
                f"acquires at line {v['acquire_line']} and yields at line "
                f"{v['yield_line']}; {v['unguarded_count']} raise-capable node(s) "
                "in that window are not covered by a try that releases.\n"
                f"      {v['snippet']}"
            )
        print(
            "\nA generator that raises before its first yield never runs __aexit__, "
            "so the pre-yield region is the ONLY release path. Wrap it in\n"
            "    try: ...\n"
            "    except BaseException:\n"
            "        semaphore.release()\n"
            "        raise\n"
            f"or mark the site '{EXEMPT_MARKER}' if it genuinely must not release."
        )
        return 1

    print(
        f"✅ {len(all_sites)} @asynccontextmanager acquire-before-yield site(s) "
        "release their permit if the pre-yield region raises"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
