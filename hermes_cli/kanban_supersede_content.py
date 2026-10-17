"""Does a ``SUPERSEDED-BY <sha>`` actually carry the closed PR's content? (t_8035c8af)

A SHA superseder used to be checked for ancestry only: "is <sha> on the default branch". On
2026-10-17 09:47 a watcher closed hermes-home#4729 ``SUPERSEDED-BY e4d8ee8797e3`` because a
frontmatter KEY the PR touched existed on main, while the PR's new VALUE did not: the named commit
was on main and carried none of the PR's content. Ancestry cannot tell those apart.

:func:`pr_content_check` reads the PR's file list and, for every file, asks whether the PR's added
lines are all present in that file (a later, newer edit on main is a superset and passes). The
check runs against two candidates, ``sha`` and the default-branch tip; one of them must carry EVERY
file, and that commit (not merely the requested one) is the survivor evidence. A removed file must be absent; a file GitHub gives no patch for
(binary, oversized) must match the PR head's blob exactly. A file whose change only deletes lines
has no added line to look for and passes. Every lookup that fails makes the answer unknown (None):
the caller fails closed on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Callable, Optional
from urllib.parse import quote

QUERY_TIMEOUT_SECONDS = 15
#: A PR with more files than this is not content-checked line by line; the answer is "unknown".
MAX_FILES = 300

ContentCheckFn = Callable[[str, int, str], Optional[dict]]


class _Unreadable(Exception):
    """GitHub did not answer (network, auth, rate limit, malformed payload)."""


def _gh(args: list, *, raw: bool = False):
    """``gh api`` stdout (bytes when ``raw``), ``None`` on HTTP 404; raises :class:`_Unreadable` else."""
    try:
        proc = subprocess.run(["gh", "api", *args], capture_output=True, text=not raw,
                              timeout=QUERY_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise _Unreadable(str(exc)) from exc
    if proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b"").decode("utf-8", "replace")
        if "HTTP 404" in err:
            return None
        raise _Unreadable(err.strip()[:200])
    return proc.stdout


def _json(args: list):
    out = _gh(args)
    if out is None:
        raise _Unreadable(f"404: {' '.join(args)}")
    try:
        return json.loads(out)
    except ValueError as exc:
        raise _Unreadable(f"bad json from {' '.join(args)}") from exc


def _file_bytes(repo: str, path: str, ref: str) -> Optional[bytes]:
    """The file's bytes at ``ref``; ``None`` when the path does not exist there."""
    return _gh(["-H", "Accept: application/vnd.github.raw",
                f"repos/{repo}/contents/{quote(path)}?ref={quote(ref)}"], raw=True)


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def added_lines(patch: str) -> set:
    """Non-blank lines a unified-diff hunk body adds (GitHub's ``files[].patch`` has no ``+++`` header)."""
    out = set()
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++ "):
            body = line[1:].rstrip("\r")
            if body.strip():
                out.add(body)
    return out


def _lines(data: bytes) -> list:
    return [line.rstrip("\r") for line in data.decode("utf-8", "replace").splitlines()]


def split_frontmatter(data: Optional[bytes]):
    """``(mapping, frontmatter_lines, body_lines)`` for a ``---``-fenced YAML header, else ``None``."""
    if data is None:
        return None
    lines = _lines(data)
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    from ruamel.yaml import YAML
    try:
        mapping = YAML(typ="safe", pure=True).load("\n".join(lines[1:end]))
    except Exception:  # noqa: BLE001 - any parse failure means "not comparable", never a pass
        return None
    if not isinstance(mapping, dict):
        return None
    return mapping, lines[:end + 1], lines[end + 1:]


def _leaves(node, prefix=()) -> dict:
    """``{key path: value}``; a non-mapping value (scalar or list) is one leaf."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            out.update(_leaves(value, prefix + (str(key),)))
        return out
    return {prefix: node}


def frontmatter_carries(entry: dict, data: Optional[bytes], base: Optional[bytes],
                        head: Optional[bytes]) -> bool:
    """Key-level check for a YAML-frontmatter file whose header a later writer re-serialized.

    The home-autocommit drain applies frontmatter retags KEY-level and re-emits the header in block
    style, so the PR's flow-style line never appears verbatim on main. Every leaf the PR changed
    (base -> head) must hold the head value at the target and every leaf it removed must be gone;
    the PR's added BODY lines must be present line by line. Leaves the PR did not touch may differ
    (another PR's newer edit). Unparseable on any side -> False.
    """
    target, mine = split_frontmatter(data), split_frontmatter(head)
    if target is None or mine is None:
        return False
    before = split_frontmatter(base) if base is not None else ({}, [], [])
    if before is None:
        return False
    old, new, have = _leaves(before[0]), _leaves(mine[0]), _leaves(target[0])
    for path, value in new.items():
        if old.get(path, _MISSING) != value and have.get(path, _MISSING) != value:
            return False
    if any(path in have for path in old if path not in new):
        return False
    header = set(mine[1])
    body_added = {line for line in added_lines(entry.get("patch") or "") if line not in header}
    return body_added <= set(target[2])


_MISSING = object()


def file_carries(entry: dict, data: Optional[bytes]) -> bool:
    """Does one ref's copy of the file (``None`` = absent) carry this PR file entry's change?"""
    if entry.get("status") == "removed":
        return data is None
    if data is None:
        return False
    patch = entry.get("patch")
    if not isinstance(patch, str):
        # No patch (binary or too large to diff): only the exact blob proves it.
        return _blob_sha(data) == entry.get("sha")
    return added_lines(patch) <= set(_lines(data))


def _carries(entry: dict, data: Optional[bytes], fetch_sides: Callable[[], tuple]) -> bool:
    """Line-level check, then the key-level frontmatter fallback (base/head fetched on demand)."""
    if file_carries(entry, data):
        return True
    if entry.get("status") == "removed" or not isinstance(entry.get("patch"), str):
        return False
    if not split_frontmatter(data):
        return False
    base, head = fetch_sides()
    return frontmatter_carries(entry, data, base, head)


def pr_content_check(repo: str, number: int, sha: str) -> Optional[dict]:
    """``{"sha": <carrier>, "requested": <full sha>, "default": <branch>, "differs": [paths]}``.

    ``None`` when unknown. Candidates are the requested ``sha`` and the default-branch tip, pinned
    to its commit sha. ``sha`` in the answer is the ONE candidate commit that carries the most PR
    files (the requested commit wins a tie), and ``differs`` lists every file that commit does not
    carry. Empty ``differs`` therefore means that commit alone carries all of the PR's content, so
    it is safe to record as survivor evidence. It is never a commit that only passed because a
    different ref held the content (Prism 6265717768a4).
    """
    try:
        full = str(_json([f"repos/{repo}/commits/{quote(sha)}"]).get("sha") or "")
        default = str(_json([f"repos/{repo}"]).get("default_branch") or "")
        tip = str(_json([f"repos/{repo}/commits/{quote(default)}"]).get("sha") or "") if default else ""
        pr = _json([f"repos/{repo}/pulls/{int(number)}"])
        base_sha = str((pr.get("base") or {}).get("sha") or "")
        head_sha = str((pr.get("head") or {}).get("sha") or "")
        out = _gh(["--paginate", f"repos/{repo}/pulls/{int(number)}/files?per_page=100",
                   "--jq", ".[] | @json"])
        if not full or not default or not tip or not base_sha or not head_sha or out is None:
            return None
        files = [json.loads(line) for line in out.splitlines() if line.strip()]
        if not files or len(files) > MAX_FILES:
            return None
        candidates = list(dict.fromkeys((full, tip)))
        missing = {ref: [] for ref in candidates}
        for entry in files:
            path = str(entry.get("filename") or "")
            if not path:
                return None
            sides: list = []

            def fetch_sides(entry=entry, path=path, sides=sides):
                if not sides:
                    old_path = str(entry.get("previous_filename") or path)
                    base = None if entry.get("status") == "added" else _file_bytes(repo, old_path, base_sha)
                    sides.append((base, _file_bytes(repo, path, head_sha)))
                return sides[0]

            for ref in candidates:
                if not _carries(entry, _file_bytes(repo, path, ref), fetch_sides):
                    missing[ref].append(path)
        chosen = min(candidates, key=lambda ref: len(missing[ref]))
    except (_Unreadable, ValueError, TypeError, AttributeError):
        return None
    return {"sha": chosen, "requested": full, "default": default, "differs": missing[chosen]}


def default_content_check() -> Optional[ContentCheckFn]:
    """The real ``gh``-backed check, or None inside pytest (no live GitHub; tests pass a stub)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return pr_content_check


class SupersededContentError(ValueError):
    """``--superseded-by <sha>`` names a commit that does not carry the card's PR content."""

    def __init__(self, task_id: str, differs: dict, unverified: list):
        self.task_id = task_id
        self.differs = {pr: list(paths) for pr, paths in differs.items()}
        self.unverified = list(unverified)
        parts = ["done refused: --superseded-by names a commit that does not carry this card's PR content."]
        for pr, paths in self.differs.items():
            parts.append(f"{pr}: no single commit (that sha, or the default-branch tip) carries the "
                         f"PR's added lines in {', '.join(paths)}.")
        if self.unverified:
            parts.append(f"GitHub could not be read to compare {', '.join(self.unverified)}; retry when "
                         f"`gh api repos/<owner>/<repo>/pulls/<n>/files` works.")
        parts.append("Name the commit that landed the content (or wait for it to land). "
                     f"{task_id} is still in-flight (no state change).")
        super().__init__(" ".join(parts))


def supersede_shas(superseded_by: Optional[str]) -> list:
    """Commit SHAs (7-40 hex with at least one letter) named in a ``--superseded-by`` pointer."""
    from hermes_cli.kanban_open_pr import _TARGET_RE
    if not isinstance(superseded_by, str):
        return []
    return list(dict.fromkeys(t for t in _TARGET_RE.findall(superseded_by)
                              if all(c in "0123456789abcdef" for c in t) and any(c in "abcdef" for c in t)))


def verify(task_id: str, prs, shas, *, check: Optional[ContentCheckFn] = None) -> list:
    """Survivor refs proving each fleet PR in ``prs`` is carried by one of ``shas`` or the default branch.

    ``[]`` when there is nothing to verify (no SHA, no own PR, or no oracle under pytest). Raises
    :class:`SupersededContentError` naming every PR whose files differ, or that could not be read.
    """
    if check is None:
        check = default_content_check()
    if check is None or not shas or not prs:
        return []
    differs, unverified, survivors = {}, [], []
    for ref in prs:
        key = f"{ref.repo}#{ref.number}"
        results = [check(ref.repo, ref.number, sha) for sha in shas]
        ok = next((r for r in results if r is not None and not r.get("differs")), None)
        if ok is not None:
            survivors.append({"remote": f"https://github.com/{ref.repo}", "branch": ok["default"],
                              "sha": ok["sha"], "pr": key, "external": True,
                              "corroborated_by": "superseded-content"})
            continue
        seen = next((r for r in results if r is not None), None)
        if seen is None:
            unverified.append(key)
        else:
            differs[key] = seen["differs"]
    if differs or unverified:
        raise SupersededContentError(task_id, differs, unverified)
    return survivors
