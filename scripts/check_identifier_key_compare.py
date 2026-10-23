#!/usr/bin/env python3
"""Identifier keys compare byte-exact (t_75fdedd4, Operator-Surface S3 / P4 GUARD-BY-STRING).

Model ids, provider names, task ids, run ids, SHAs, lane / assignee / profile / board names are
KEYS: two of them are the same thing only when their bytes are equal. Case-folding, prefix or
substring tests on them, and SQLite ``LIKE`` (ASCII-case-insensitive) on their columns, make two
different keys collide. The template is #1921: ``model_force_comment_present`` used ``LIKE`` so
``custommodel`` inherited ``CustomModel``'s force exemption.

This lint walks the kanban modules with ``ast`` and reports every such site:

  fold     ``<key>.lower()`` / ``.casefold()`` / ``.upper()``
  affix    ``<key>.startswith(...)`` / ``.endswith(...)``
  substr   ``<key> in <text>`` (the right side is text: a str literal, f-string, or text-named value)
  sql      a SQL string applying ``LIKE`` / ``GLOB`` / ``lower()`` / ``COLLATE NOCASE`` to a key column

Each site is either fixed or carries a verdict in the ledger
(``scripts/identifier_key_compare_ledger.json``) explaining why the non-exact compare is right
there (GitHub repo names ARE case-insensitive; a hex digest's case is notation). The lint fails on
a site with no ledger entry, so a new non-exact key compare cannot land unreviewed.

    python scripts/check_identifier_key_compare.py            # lint (exit 1 on an unledgered site)
    python scripts/check_identifier_key_compare.py --audit    # every site with its verdict (markdown)
    python scripts/check_identifier_key_compare.py --json     # machine-readable site list
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "scripts" / "identifier_key_compare_ledger.json"
SCOPE = ("hermes_cli/kanban*.py", "tools/kanban_tools.py", "tools/kanban_*.py", "plugins/kanban/**/*.py")

# A name is an identifier KEY when its last word is one of these (snake_case words of the name).
_KEY_WORDS = {
    "model", "models", "provider", "providers", "task_id", "tid", "run_id", "sha", "sha12", "head_sha",
    "base_sha", "head", "commit", "lane", "lanes", "assignee", "assignees", "profile", "profiles",
    "board", "boards", "repo", "card", "card_id", "parent_id", "child_id", "slug", "agent_id",
    "session_id", "pool", "reviewer", "branch",
    # `<key>_override` / `_pin` / `_name` columns carry the same key (Prism r2 ea1314c14c2f:
    # `task.model_override.lower()` slipped past the suffix match)
}
_KEY_SUFFIXES = ("_override", "_overrides", "_pin", "_name", "_key")
_KEY_RE = re.compile(r"(?:^|_)(?:%s)$" % "|".join(sorted((re.escape(w) for w in _KEY_WORDS), key=len, reverse=True)))
_TEXT_RE = re.compile(r"(?:^|_)(?:body|text|summary|title|comment|comments|message|msg|line|lines|output|"
                      r"stdout|stderr|reason|note|notes|content|description|desc|blob|payload|raw|s)$")
_KEY_COLS = r"(?:model|provider|assignee|task_id|run_id|id|lane|profile|board|sha|head_sha|base_sha|reviewer|pool|session_id)"
# Every case-insensitive / pattern operator in kanban SQL is reported, whatever the column: the
# #1921 site was `body LIKE ?` with a MODEL id inside the bound token, so the column name alone
# cannot tell a key compare from a prose filter. The ledger records which is which.
_SQL_RE = re.compile(
    r"\b(?:lower|upper)\(\s*(?:\w+\.)?%(c)s\s*\)|\b(?:lower|upper)\((?:[^()]|\([^()]*\))*\)\s+(?:NOT\s+)?(?:LIKE|GLOB|IN|=)|"
    r"\b(?:\w+\.)?\w+\s+(?:NOT\s+)?(?:LIKE|GLOB)\b|\bCOLLATE\s+NOCASE\b" % {"c": _KEY_COLS}, re.I)
# Helpers whose whole job is folding a key (`def _norm(v): return str(v or "").strip().lower()`).
_FOLD_HELPERS = {"_norm", "normalize_provider", "_normalize_board_slug"}


def _subject(node: ast.AST) -> str | None:
    """The key name an expression denotes: `model`, `self.model`, `row["model"]`, `x.get("model")`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str" and node.args:
        return _subject(node.args[0])
    if isinstance(node, ast.BoolOp):  # `(model or "")`
        return next((s for s in map(_subject, node.values) if s), None)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)  # `model.strip()`
            and node.func.attr in ("strip", "lstrip", "rstrip", "lower", "casefold", "upper")):
        return _subject(node.func.value)
    return None


def _is_key(name: str | None) -> bool:
    if not name:
        return False
    low = name.lower()
    for suffix in _KEY_SUFFIXES:
        if low.endswith(suffix) and len(low) > len(suffix):
            low = low[: -len(suffix)]
            break
    return bool(_KEY_RE.search(low)) and low not in {"head", "s"}


def _is_text(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return True
    name = _subject(node)
    return bool(name) and bool(_TEXT_RE.search(name.lower()))


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str, src: str):
        self.rel, self.lines, self.sites, self.scope = rel, src.splitlines(), [], []

    def _add(self, node: ast.AST, kind: str, subject: str) -> None:
        line = self.lines[node.lineno - 1].strip() if node.lineno - 1 < len(self.lines) else ""
        self.sites.append({"file": self.rel, "line": node.lineno, "func": ".".join(self.scope) or "<module>",
                           "kind": kind, "subject": subject, "code": line[:160]})

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name) and f.id in _FOLD_HELPERS and node.args and _is_key(_subject(node.args[0])):
            self._add(node, "fold", _subject(node.args[0]))
        if isinstance(f, ast.Attribute):
            subj = _subject(f.value)
            if f.attr in ("lower", "casefold", "upper") and _is_key(subj):
                self._add(node, "fold", subj)
            elif f.attr in ("startswith", "endswith") and _is_key(subj):
                self._add(node, "affix", subj)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and _is_key(_subject(left)) and _is_text(right):
                self._add(node, "substr", _subject(left))
            left = right
        self.generic_visit(node)

    def _sql(self, node: ast.AST, text: str) -> None:
        m = _SQL_RE.search(text)
        if m and re.match(r"\s*(?:SELECT|UPDATE|DELETE|INSERT|WITH)\b", text, re.I):
            self._add(node, "sql", re.sub(r"\s+", " ", m.group(0)))

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._sql(node, node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        # f"SELECT id FROM {table} WHERE model LIKE ?": judge the WHOLE statement, each
        # interpolation kept as a placeholder word (Prism r2 85a0850c718b). Its constant parts are
        # not visited on their own, so one statement yields one site.
        text = "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " _x_ "
                       for v in node.values)
        self._sql(node, text)
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                self.visit(v)


def scan_source(rel: str, src: str) -> list[dict]:
    scanner = _Scan(rel, src)
    scanner.visit(ast.parse(src, filename=rel))
    return scanner.sites


def scan(root: Path = ROOT) -> list[dict]:
    files = sorted({p for pattern in SCOPE for p in root.glob(pattern) if p.is_file()})
    sites = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        sites += scan_source(rel, path.read_text(encoding="utf-8-sig"))
    return sites


def fingerprint(site: dict) -> str:
    """Stable across unrelated edits: no line number, whitespace-normalized code."""
    return "%s::%s::%s::%s::%s" % (site["file"], site["func"], site["kind"], site["subject"],
                                   re.sub(r"\s+", " ", site["code"]))


def load_ledger(path: Path = LEDGER) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    return data.get("sites", {})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--audit", action="store_true", help="markdown table of every site + verdict")
    ap.add_argument("--json", action="store_true", help="every site as JSON")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--ledger", type=Path, default=LEDGER)
    args = ap.parse_args(argv)
    sites = scan(args.root)
    ledger = load_ledger(args.ledger)
    for s in sites:
        s["fingerprint"] = fingerprint(s)
        entry = ledger.get(s["fingerprint"])
        s["verdict"], s["why"] = (entry["verdict"], entry["why"]) if entry else (None, None)
    if args.json:
        print(json.dumps(sites, indent=1))
        return 0
    if args.audit:
        print("| file:line | function | kind | key | verdict | why |\n|---|---|---|---|---|---|")
        for s in sites:
            print("| %s:%d | %s | %s | %s | %s | %s |" % (s["file"], s["line"], s["func"], s["kind"], s["subject"],
                                                         s["verdict"] or "UNLEDGERED", (s["why"] or "").replace("|", "/")))
        return 0
    bad = [s for s in sites if s["verdict"] is None]
    stale = sorted(set(ledger) - {s["fingerprint"] for s in sites})
    for s in bad:
        print("%s:%d: %s compare on identifier key `%s` (%s): `%s`\n    fix: compare the key bytes "
              "exactly (`==`, a set/dict lookup, SQL `=` or `substr(col,1,n)=?`); if a non-exact compare is "
              "correct here, add the site to %s with a verdict and the reason."
              % (s["file"], s["line"], s["kind"], s["subject"], s["func"], s["code"],
                 args.ledger.relative_to(args.root) if args.ledger.is_relative_to(args.root) else args.ledger))
    for fp in stale:
        print("stale ledger entry (site no longer exists; delete it): %s" % fp)
    print("identifier-key-compare: %d sites, %d ledgered, %d unledgered, %d stale"
          % (len(sites), len(sites) - len(bad), len(bad), len(stale)))
    return 1 if bad or stale else 0


if __name__ == "__main__":
    sys.exit(main())
