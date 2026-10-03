"""Guard: a "same mtime + size replacement" test must let the ctime clock tick first.

fork (parity 2026-10-01 CI). The pinned-mtime replacement tests (#111105 family) swap a file in with
``shutil.copy2`` + ``os.utime(ns=(old_atime, old_mtime))`` and assert that a (mtime, size, ino, ctime)
signature changed. ctime ticks at the kernel's coarse clock (~4 ms). When the original write, the
in-place copy2 and the utime land in one tick, every stat field is equal and the test fails. That
happened only on the 4-vCPU Blacksmith slices (#1624 runs 37107108840, 37108909064, 37114933458),
one file at a time. Two upstream files already use the fix idiom::

    while other.stat().st_ctime_ns <= <before>.st_ctime_ns:
        os.utime(other)

This guard finds every test function that pins an old mtime back with ``os.utime(..., ns=...)`` after
a ``copy2`` and requires a ``st_ctime_ns`` wait loop in the same function, so a new member of the
class fails here on every runner instead of intermittently on one.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def _calls(node: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def _pins_mtime(fn: ast.AST) -> bool:
    return any(any(k.arg == "ns" for k in c.keywords) for c in _calls(fn, "utime")) and bool(_calls(fn, "copy2"))


def _waits_for_ctime(fn: ast.AST) -> bool:
    return any(isinstance(n, ast.While) and "st_ctime_ns" in ast.unparse(n.test) for n in ast.walk(fn))


def offenders(root: Path = TESTS) -> list[str]:
    found = []
    for path in sorted(root.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _pins_mtime(fn) and not _waits_for_ctime(fn):
                found.append(f"{path.relative_to(root.parent)}::{fn.name}")
    return found


def test_pinned_mtime_replacements_wait_for_the_ctime_tick():
    assert offenders() == [], (
        "pinned-mtime replacement without a ctime-tick wait (flaky on coarse ctime clocks); add "
        "`while other.stat().st_ctime_ns <= before.st_ctime_ns: os.utime(other)` before copy2"
    )


def test_guard_bites(tmp_path):
    bad = tmp_path / "test_bad.py"
    bad.write_text(
        "import os, shutil\n"
        "def test_x(p, o, st):\n"
        "    shutil.copy2(o, p)\n"
        "    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))\n",
        encoding="utf-8",
    )
    good = tmp_path / "test_good.py"
    good.write_text(
        "import os, shutil\n"
        "def test_y(p, o, st):\n"
        "    while o.stat().st_ctime_ns <= st.st_ctime_ns:\n"
        "        os.utime(o)\n"
        "    shutil.copy2(o, p)\n"
        "    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))\n",
        encoding="utf-8",
    )
    assert [o.split("::")[1] for o in offenders(tmp_path)] == ["test_x"]
