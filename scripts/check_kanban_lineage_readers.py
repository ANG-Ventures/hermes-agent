#!/usr/bin/env python3
"""Fail on any reader of ``HERMES_KANBAN_LINEAGE`` outside the escapee reaper.

The dispatcher stamps ``HERMES_KANBAN_LINEAGE=<task_id>:<run_id>`` on every kanban worker
(option C, K6, docs/dev/delegate-background-on-kanban-runtime.md §6). Unlike
``HERMES_KANBAN_TASK``/``RUN_ID`` it survives ``scrub_kanban_env``, so it reaches the tool
processes of a worker's delegated children. That makes it a fine REAP tag and a dangerous
authority: a child that read it as "my card" could act on a card it was never granted.
So the only reader is ``kanban_db._env_names_run``, the reaper's matcher.

A mention of the name (the string literal or ``KANBAN_LINEAGE_ENV``) is allowed when it:

* is the constant's own definition (``KANBAN_LINEAGE_ENV = "HERMES_KANBAN_LINEAGE"``);
* is a write target (``env[KANBAN_LINEAGE_ENV] = ...``);
* is the key of a ``.pop(...)`` (scrubbing it);
* sits on a line carrying ``# lineage-reader: ok`` (the reaper's one read).

Everything else (``.get``, ``getenv``, a subscript load, ``in env``, passing it along) is a
reader and fails. ``tests/`` is not scanned.

Usage: python3 scripts/check_kanban_lineage_readers.py [paths...]
"""
from __future__ import annotations

import ast
import re
import subprocess
import unicodedata
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "HERMES_KANBAN_LINEAGE"
CONST = "KANBAN_LINEAGE_ENV"
SUPPRESS = "# lineage-reader: ok"
SELF = "scripts/check_kanban_lineage_readers.py"  # names the tag to look for it


# Raw-text prefilter for a STRING CONSTANT the AST check looks for. The decoded constant need
# not appear contiguously in the source: Python joins adjacent literals (``"kan" "" "ban"``),
# drops a backslash-newline inside a literal and decodes escapes (``"\x6bill"``). ``_spelled``
# builds a pattern for every such spelling: each character may be itself or an escape that can
# decode to it, and between characters any run of literal joints (quote, gap, optional prefix,
# quote) or continuations. A superset: a match only means "parse it" (Prism 675e33503531,
# 10b55641b9b8, 3c7e1b72c0b1; t_44dcc531).
_JOINT = r"""(?:['"]{1,3}(?:[ \t\f\r\n()]|\\(?:\r\n?|\n)|#[^\r\n]*)*[rRbBuUfF]{0,2}['"]{1,3}|\\(?:\r\n?|\n))*"""


def _spelled(word: str) -> str:
    parts = []
    for ch in word:
        o = ord(ch)
        escapes = r"\\(?:x(?i:%02x)|u(?i:%04x)|U(?i:%08x)|0{0,2}%o|N\{[^}]*\})" % (o, o, o, o)
        parts.append("(?:%s|%s)" % (re.escape(ch), escapes))
    return _JOINT.join(parts)


_NAME_TEXT = re.compile(_spelled(NAME))


def _mentions(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value == NAME:
        return True
    if isinstance(node, ast.Name) and node.id == CONST:
        return True
    return isinstance(node, ast.Attribute) and node.attr == CONST


def _allowed(node: ast.AST, parent: ast.AST) -> bool:
    if isinstance(parent, ast.Assign) and node is parent.value:
        return any(isinstance(t, ast.Name) and t.id == CONST for t in parent.targets)
    if isinstance(parent, ast.Assign) and node in parent.targets:
        return True  # ``KANBAN_LINEAGE_ENV = ...`` (the definition's own target)
    if isinstance(parent, ast.Subscript) and node is parent.slice:
        return isinstance(parent.ctx, ast.Store)
    if isinstance(parent, ast.Call) and node in parent.args[:1]:
        return isinstance(parent.func, ast.Attribute) and parent.func.attr == "pop"
    return False


def scan_source(src: str, rel: str) -> list[str]:
    # CONST is an identifier, and the tokenizer NFKC-normalises identifiers (``ＥＮＶ``).
    ident = src if src.isascii() else unicodedata.normalize("NFKC", src)
    if CONST not in ident and not _NAME_TEXT.search(src):
        return []  # every _mentions node spells one of them: skip the parse (all but a few files)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    problems = []
    for node in sorted(ast.walk(tree), key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))):
        if not _mentions(node):
            continue
        if _allowed(node, parents.get(node)):
            continue
        line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        if SUPPRESS in line:
            continue
        problems.append(f"{rel}:{node.lineno}: reads {NAME}; it is a reap tag, never ownership "
                        f"(only kanban_db._env_names_run may read it)")
    return problems


def _tracked_py() -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"], check=True,
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*.py") if ".git" not in p.parts]
    return [ROOT / line for line in out.splitlines() if line]


def main(argv: list[str]) -> int:
    paths = [Path(a).resolve() for a in argv] if argv else _tracked_py()
    problems: list[str] = []
    for path in paths:
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            rel = str(path)
        if rel.startswith("tests/") or rel == SELF or not path.is_file():
            continue
        problems.extend(scan_source(path.read_text(encoding="utf-8-sig"), rel))
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
