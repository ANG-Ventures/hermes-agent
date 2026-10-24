"""S7 D6 rule 3: the global board pointers stay deleted.

(a) ``kanban_db`` has no current-board file API; (b) ``get_current_board`` reads no file and no
environment; (c) no runtime module outside ``tests/`` and ``website/`` names the
``HERMES_KANBAN_BOARD`` key as a string constant (covers ``os.environ[...]``, ``.get``,
``getenv``, ``putenv`` and any allow-list tuple). Mutations that turn it RED: restore any (a)
attribute; add ``os.environ.get("HERMES_KANBAN_BOARD")`` anywhere in the runtime, or the key in
an allow-list tuple. Honest limit: (c) is source-level; the runtime teeth are (b) plus the fact
that no reader exists. Spec: hermes-home
plans/2026-10-21_sdlc-simplification/S7-boards-no-global-pointer-SPEC.md D6, I4.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

REPO = Path(__file__).resolve().parents[2]
BOARD_KEY = "HERMES_KANBAN_BOARD"
_SKIP_TOP = {"tests", "website", "node_modules", ".git", ".venv", "venv"}


@pytest.mark.parametrize("name", ["set_current_board", "clear_current_board", "current_board_path"])
def test_no_pointer_file_api(name):
    assert not hasattr(kb, name)


def test_get_current_board_reads_no_file_and_no_environment():
    tree = ast.parse(textwrap.dedent(inspect.getsource(kb.get_current_board)))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"read_text", "exists", "environ", "getenv"}:
            offenders.append(node.attr)
        if isinstance(node, ast.Name) and node.id in {"environ", "getenv"}:
            offenders.append(node.id)
    assert offenders == []


def _runtime_py_files():
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if rel.parts[0] in _SKIP_TOP or any(p in {"node_modules", "__pycache__"} for p in rel.parts):
            continue
        yield path, rel


def test_no_runtime_module_names_the_board_env_key():
    hits = []
    for path, rel in _runtime_py_files():
        try:
            tree = ast.parse(path.read_bytes())
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == BOARD_KEY:
                hits.append(f"{rel}:{node.lineno}")
    assert hits == [], f"{BOARD_KEY} is read/written again (S7 deleted it): {hits}"
