"""Ratchet: no NEW test that proves behaviour by grepping source text.

Cluster A of the 2026-09-27 P2/P3 backfill (t_f4ef102c, 33 rows fleet-wide):
tests that call ``inspect.getsource(...)`` and then ``assert "x" in src``
pass whenever the text is present, whether or not the code path runs. Such a
test cannot fail on a behaviour regression that keeps the string.

Detector (AST, per test function): the function calls ``*.getsource(...)``
AND contains an ``assert`` whose test uses ``in`` / ``not in``. That is a
heuristic; a legitimately structural check (e.g. "this module must not import
X") opts out on the ``def`` line with ``# noqa: source-proxy <reason>``.

The 116 functions that matched when this landed (2026-09-28, t_cd88e043) are
frozen in ``source_proxy_baseline.json``: pre-existing, NOT endorsed. Replace
one with a behavioural test and delete its key; the stale-key check keeps the
count from creeping back.
"""
from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
BASELINE_FILE = Path(__file__).with_name("source_proxy_baseline.json")
NOQA = "# noqa: source-proxy"


def _is_proxy_test(fn: ast.AST) -> bool:
    calls_getsource = any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Attribute) and n.func.attr == "getsource")
            or (isinstance(n.func, ast.Name) and n.func.id == "getsource")
        )
        for n in ast.walk(fn)
    )
    if not calls_getsource:
        return False
    return any(
        isinstance(n, ast.Assert)
        and any(
            isinstance(c, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in c.ops)
            for c in ast.walk(n.test)
        )
        for n in ast.walk(fn)
    )


def proxy_tests(source: str, rel: str) -> set[str]:
    try:
        with warnings.catch_warnings():  # scanned files' own escape warnings
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines = source.splitlines()
    found = set()
    # Key on the qualified name (``rel::Class::test``) so same-named methods in
    # different classes of one file cannot shadow each other in the baseline.
    stack: list[tuple[ast.AST, tuple[str, ...]]] = [(tree, ())]
    while stack:
        node, scope = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                stack.append((child, scope + (child.name,)))
                continue
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.append((child, scope))
                continue
            qual = scope + (child.name,)
            stack.append((child, qual))
            if not child.name.startswith("test"):
                continue
            if NOQA in lines[child.lineno - 1]:
                continue
            if _is_proxy_test(child):
                found.add(f"{rel}::" + "::".join(qual))
    return found


def _scan() -> set[str]:
    found: set[str] = set()
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts or path == Path(__file__):
            continue
        rel = path.relative_to(REPO).as_posix()
        found |= proxy_tests(path.read_text(encoding="utf-8", errors="replace"), rel)
    return found


def test_no_new_source_proxy_asserts():
    baseline = set(json.loads(BASELINE_FILE.read_text()))
    current = _scan()
    added = sorted(current - baseline)
    removed = sorted(baseline - current)
    assert not added, (
        "NEW test(s) assert on inspect.getsource() text instead of running the code. "
        "Exercise the behaviour, or mark a genuinely structural check on its def line "
        f"with `{NOQA} <reason>`:\n  " + "\n  ".join(added)
    )
    assert not removed, (
        "Baseline entries no longer match -- good. Delete them from "
        f"{BASELINE_FILE.name} so the ratchet keeps them gone:\n  " + "\n  ".join(removed)
    )


def test_arm_detector_fires_on_the_cluster_a_shape():
    bad = (
        "import inspect\n"
        "def test_wired():\n"
        "    src = inspect.getsource(mod.run)\n"
        "    assert 'offload(' in src\n"
    )
    assert proxy_tests(bad, "t.py") == {"t.py::test_wired"}


def test_arm_same_method_name_in_two_classes_gets_distinct_keys():
    body = (
        "    def test_wired(self):\n"
        "        src = inspect.getsource(mod.run)\n"
        "        assert 'offload(' in src\n"
    )
    src = "import inspect\nclass TestA:\n" + body + "class TestB:\n" + body
    assert proxy_tests(src, "t.py") == {"t.py::TestA::test_wired", "t.py::TestB::test_wired"}


def test_arm_behavioural_and_noqa_are_green():
    good = (
        "def test_runs():\n"
        "    assert run() == 3\n"
        "    assert 'x' in {'x': 1}\n"
    )
    opted_out = (
        "import inspect\n"
        "def test_no_import():  # noqa: source-proxy structural import ban\n"
        "    assert 'import requests' not in inspect.getsource(mod)\n"
    )
    assert proxy_tests(good, "t.py") == set()
    assert proxy_tests(opted_out, "t.py") == set()
