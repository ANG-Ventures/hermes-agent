"""A sync PR's own fix/restore commits must each leave a registry entry behind (t_f34a6e21).

On a parity/upstream sync branch, the commits the FORK authored (reachable from the head, not from
the base, not from upstream, not merges) whose subject says ``fix``/``restore``/``regress`` are
repairs of fork behavior the merge broke. Each one must add or update a ``fork-features.json``
entry whose ``tests`` name a test file that same commit added or modified. Otherwise the next sync
can break the same behavior silently (hermes-agent#1792: registry-pins contracts regressed by
#1624, fixed with tests nothing registered).

Failure shapes:
  * a repair commit that touches no test file            -> "restore without a canary"
  * its test files are named by no NEW or CHANGED entry  -> "canary not registered"
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from . import forkdelta, gitops

REPAIR_SUBJECT = re.compile(r"^(?:fix|restore|revert)\b|\b(?:restore[sd]?|regress(?:ed|ion)?)\b", re.IGNORECASE)
MANIFEST = str(forkdelta.DEFAULT_MANIFEST)


# `[ \t]*`, not `\s*`: an empty trailer must not borrow the next line (a Signed-off-by) as its reason
EXEMPT_TRAILER = re.compile(r"^Canary-Exempt:[ \t]*(\S.{9,})$", re.MULTILINE | re.IGNORECASE)


def fork_commits(repo: Path, base: str, head: str, upstream: str) -> list[tuple[str, str, str]]:
    """(sha, subject, body) of every non-merge commit the sync branch itself authored."""
    out = gitops.run_git(repo, ["log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", head, f"^{base}",
                                f"^{upstream}"]).stdout
    rows = []
    for rec in out.split("\x1e"):
        if rec.strip():
            sha, subject, body = (rec.strip("\n").split("\x1f") + ["", ""])[:3]
            rows.append((sha, subject.strip(), body))
    return rows


def touched_files(repo: Path, sha: str) -> list[str]:
    # -z: core.quotePath would otherwise quote non-ASCII paths and _is_test_file would never match them
    out = gitops.run_git(repo, ["show", "--no-renames", "--name-only", "-z", "--format=", sha]).stdout
    return [p.strip("\n") for p in out.split("\0") if p.strip("\n")]


def _entries(repo: Path, rev: str) -> dict[str, dict]:
    res = gitops.run_git(repo, ["show", f"{rev}:{MANIFEST}"], check=False)
    if res.returncode != 0:
        return {}
    return {str(e.get("feature")): e for e in json.loads(res.stdout)}


def _is_test_file(p: str) -> bool:
    return p.startswith("tests/") and p.endswith(".py") and "/test_" in "/" + p


def _def_sources(src: str) -> dict[str, str] | None:
    """Source of every top-level def/class (``name``) and class method (``Cls::meth``); None if unparsable."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    lines = src.splitlines()

    def seg(node: ast.AST) -> str:  # decorators included: a changed parametrize/mark is a changed test
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        return "\n".join(lines[start - 1:node.end_lineno])

    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = seg(node)
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[f"{node.name}::{sub.name}"] = seg(sub)
    return out


def _node_key(nodeid: str) -> str:
    """``f.py::Cls::test_m[1]`` -> ``Cls::test_m``; ``f.py::test_p[2]`` -> ``test_p``."""
    return "::".join(part.split("[", 1)[0] for part in nodeid.split("::")[1:3])


def _changed_defs(repo: Path, sha: str, path: str) -> set[str] | None:
    """Keys of the defs ``sha`` added or changed in ``path``; None when either side cannot be parsed."""
    def at(rev: str) -> str:
        res = gitops.run_git(repo, ["show", f"{rev}:{path}"], check=False)
        return res.stdout if res.returncode == 0 else ""
    new, old = _def_sources(at(sha)), _def_sources(at(f"{sha}^"))
    if new is None or old is None:
        return None
    return {k for k, v in new.items() if old.get(k) != v}


def _guarded(repo: Path, sha: str, path: str, nodeids: dict[str, list[str]]) -> bool:
    """A repair's test file guards it when the registry names the file WHOLE, or names a nodeid whose def the
    commit added or changed. A nodeid elsewhere in the same file guards nothing: the lane never runs the rest."""
    named = nodeids.get(path) or []
    if any("::" not in n for n in named):
        return True
    keys = {_node_key(n) for n in named}
    if not keys:
        return False
    changed = _changed_defs(repo, sha, path)
    return bool(changed and keys & changed)


def check(repo: Path, *, base: str, head: str, upstream: str) -> tuple[list[str], list[str]]:
    """-> (errors, exemptions). A repair commit counts when it changes code outside tests/ (a test-only fix is
    test hygiene: fork-feature-canary-lane "Deliberate non-goals"). It passes when a test file it adds or
    modifies is named WHOLE by a registry entry at the head, or one of its registered nodeids is a def the commit
    added or changed (already guarded, or a new/updated row), or when its
    body carries ``Canary-Exempt: <reason>`` (>= 10 chars; listed in the output, so never silent)."""
    after = _entries(repo, head)
    nodeids: dict[str, list[str]] = {}
    for e in after.values():
        for n in e.get("tests") or []:
            nodeids.setdefault(n.split("::", 1)[0], []).append(n)
    errors, exempt = [], []
    for sha, subject, body in fork_commits(repo, base, head, upstream):
        if not REPAIR_SUBJECT.search(subject):
            continue
        files = touched_files(repo, sha)
        if not [p for p in files if not p.startswith("tests/")]:
            continue
        m = EXEMPT_TRAILER.search(body)
        if m:
            exempt.append(f"{sha[:10]} {subject!r}: Canary-Exempt: {m.group(1).strip()}")
            continue
        # only test files that still exist at the head can guard anything
        tests = {p for p in files if _is_test_file(p)
                 and gitops.run_git(repo, ["cat-file", "-e", f"{head}:{p}"], check=False).returncode == 0}
        if not tests:
            errors.append(f"{sha[:10]} {subject!r}: repair commit changes code but no test (restore without a canary; "
                          "add a RED-proven canary, fork-feature-canary-lane Phase 3-5, or a 'Canary-Exempt: <why>' "
                          "trailer when the repair restores no fork behavior)")
        elif not any(_guarded(repo, sha, p, nodeids) for p in tests):
            errors.append(f"{sha[:10]} {subject!r}: none of {sorted(tests)} is named by a {MANIFEST} entry at the "
                          "head (canary not registered: add or update the feature's row)")
    return errors, exempt


# Which PRs are parity/upstream syncs. Conventions in use, measured on #510/#643/#1624: head
# `parity-merge-<date>` / `sync/upstream-<date>` (cli.py `start`), titles "merge: parity sync with upstream",
# "parity: upstream sync <date>", cli.py `finish`'s default "Merge upstream parity into <branch>", and
# > 1,000 commits. Mirrors the sync half of hermes-home scripts/topology_gate.py (anchor/drain PRs are
# topology there; here they only count when they also carry the commit volume of a sync).
_SYNC_HEAD = re.compile(r"^(?:sync/upstream|parity-merge|parity/)", re.IGNORECASE)
_SYNC_TITLE = re.compile(
    r"\bparity (?:sync|merge)\b|\bupstream (?:parity )?sync\b|\bupstream catch-up\b"
    r"|(?<![\w-])parity\)?:\s*upstream\b|\bmerge upstream parity\b", re.IGNORECASE)
SYNC_COMMITS_MIN = 1000


def is_parity_pr(title: str, head_ref: str, commits: int) -> bool:
    return bool(_SYNC_HEAD.search(head_ref or "") or _SYNC_TITLE.search(title or "")
                or (commits or 0) > SYNC_COMMITS_MIN)


def classify_main(args) -> int:
    print("yes" if is_parity_pr(args.title, args.head_ref, args.commits) else "no")
    return 0


def main(args) -> int:
    repo = Path(args.worktree).resolve() if args.worktree else gitops.repo_root(Path.cwd())
    errors, exempt = check(repo, base=args.base, head=args.head, upstream=args.upstream)
    for e in exempt:
        print(f"EXEMPT {e}")
    for e in errors:
        print(e)
    print(f"parity-pr-check: {len(errors)} unregistered repair(s) -> {'RED' if errors else 'GREEN'}")
    return 1 if errors else 0
