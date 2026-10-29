"""Does a ``SUPERSEDED-BY <sha>`` actually carry the closed PR's content? (t_8035c8af)

A SHA superseder used to be checked for ancestry only: "is <sha> on the default branch". On
2026-10-17 09:47 a watcher closed hermes-home#4729 ``SUPERSEDED-BY e4d8ee8797e3`` because a
frontmatter KEY the PR touched existed on main, while the PR's new VALUE did not: the named commit
was on main and carried none of the PR's content. Ancestry cannot tell those apart.

:func:`pr_content_check` first refuses a ``sha`` that is not reachable from the default branch.
It then reads the PR's file list and, for every file, compares the merge-base, the PR head and a
candidate commit as line MULTISETS: each line the PR added must be on the candidate at least as
often as on the head, each line it removed no more often (a later, newer edit on main that touches
neither passes), and each block the PR inserted must keep its order against its neighbours. A YAML
frontmatter header is compared by parsed key path instead (the drain re-serializes headers), with
typed deep equality, and an unparseable header on any side refuses. A renamed file is checked at its
new path and its old path must be gone. Candidates are ``sha`` and
the default-branch tip; one of them must carry EVERY file, and that commit is the survivor
evidence. A removed file must be absent; a file GitHub gives no patch for (binary, oversized) must
match the PR head's blob exactly. Every lookup that fails makes the answer unknown (None): the
caller fails closed on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import Counter
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


def _lines(data: bytes) -> list:
    return [line.rstrip("\r") for line in data.decode("utf-8", "replace").splitlines()]


def _counts(lines) -> Counter:
    """Multiset of non-blank lines: a line present twice counts twice (Prism 683cd59569ec)."""
    return Counter(line for line in lines if line.strip())


class _Unparseable(Exception):
    """A ``---`` fenced header that is not a YAML mapping (unclosed, invalid, or a scalar/list)."""


def split_frontmatter(data: Optional[bytes]):
    """``(mapping, body_lines)`` for a ``---``-fenced YAML header, ``None`` when there is no header.

    Raises :class:`_Unparseable` when the file opens with ``---`` but the header does not load as a
    mapping: an unreadable header is never "no header", it is a refusal.
    """
    if data is None:
        return None
    lines = _lines(data)
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise _Unparseable("unclosed frontmatter") from exc
    from ruamel.yaml import YAML
    try:
        mapping = YAML(typ="safe", pure=True).load("\n".join(lines[1:end]))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal, never a pass
        raise _Unparseable(str(exc)[:120]) from exc
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        raise _Unparseable("frontmatter is not a mapping")
    return mapping, lines[end + 1:]


_MISSING = object()


def _plain(value):
    """A loaded YAML value as a type-tagged plain tree, so equality is semantic and typed at every depth
    (Prism 78093f0a74dc): ``1`` never equals ``true`` or ``1.0`` (Python's ``1 == True`` would, even
    inside a list), and a ruamel ``CommentedMap``/``CommentedSeq``/``ScalarString`` compares like the
    plain dict/list/str it wraps, so formatting alone never refuses."""
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", int(value))
    if isinstance(value, float):
        return ("float", float(value))
    if isinstance(value, str):
        return ("str", str(value))
    if isinstance(value, dict):
        return ("map", tuple(sorted((_plain(k), _plain(v)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return ("seq", tuple(_plain(v) for v in value))
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted(_plain(v) for v in value)))
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value))
    if hasattr(value, "isoformat"):
        return (type(value).__name__, value.isoformat())
    return (type(value).__name__, repr(value))


def _keys(node, prefix=()) -> dict:
    """``{key path: plain value}``. A non-mapping value and an EMPTY mapping are each one value (an
    empty mapping is a value, not "no keys": Prism d20063658cfe). Path segments are typed too, so key
    ``1`` and key ``"1"`` are different paths."""
    if isinstance(node, dict) and node:
        out = {}
        for key, value in node.items():
            out.update(_keys(value, prefix + (_plain(key),)))
        return out
    return {prefix: _plain(node)}


def _has_path(node, path) -> bool:
    """Is there a node, scalar OR container, at ``path`` in the loaded mapping ``node``?"""
    for seg in path:
        if not isinstance(node, dict):
            return False
        node = next((v for k, v in node.items() if _plain(k) == seg), _MISSING)
        if node is _MISSING:
            return False
    return True


def frontmatter_carries(mb: dict, head: dict, cand: dict) -> bool:
    """Every key path whose value differs between merge-base and PR head holds the head value on the
    candidate (deep equality; a key the PR removed must be absent). Paths the PR did not change may
    differ: that is another writer's newer edit, or the drain's block-style re-serialization."""
    old, new, have = _keys(mb), _keys(head), _keys(cand)
    for path in set(old) | set(new):
        want = new.get(path, _MISSING)
        if old.get(path, _MISSING) == want:
            continue
        got = have.get(path, _MISSING)
        if want is _MISSING:
            # A key the PR removed must be absent from the candidate MAPPING, not just its leaves: a
            # mapping left under it is still the key (Prism d3b02265e3bf). A path the head turned
            # into a container is not removed; its new leaves are checked as their own paths.
            if any(p[:len(path)] == path for p in new):
                continue
            if _has_path(cand, path):
                return False
        elif got is _MISSING or got != want:  # plain, type-tagged trees: typed deep equality
            return False
    return True


def _in_scope_subsequence(needle, haystack, anchors) -> bool:
    """``needle`` appears in ``haystack`` in order, and no gap between two matched lines holds an
    ``anchors`` line (an unchanged head line): crossing one means the match left the block's scope
    (Prism 86fbd795f101: `run()` added to `a()` matched inside `b()`). Every start position is tried;
    a gap of lines that are not head context (a later edit on main) is fine."""
    for start in (i for i, h in enumerate(haystack) if h == needle[0]):
        pos, ok = start + 1, True
        for line in needle[1:]:
            while pos < len(haystack) and haystack[pos] != line and haystack[pos] not in anchors:
                pos += 1
            if pos >= len(haystack) or haystack[pos] != line:
                ok = False
                break
            pos += 1
        if ok:
            return True
    return False


def _order_carried(mb_lines, head_lines, cand_lines) -> bool:
    """Every block of lines the PR inserted sits in the same place relative to its neighbours on the
    candidate (Prism b4f089dc2ff6: a multiset passes a PR whose whole point is a reorder or a move).

    Blank lines are ignored. ``difflib`` marks the head lines the PR inserted or replaced; each maximal
    run of them, with the nearest unchanged head line before and after it, must appear on the candidate
    in that order. Lines a later edit on main put in between are fine, but an unchanged head line in
    between is not: the match would cross into another scope, so it refuses (fails closed). Each block
    is checked on its own, so unrelated later edits elsewhere never refuse."""
    import difflib
    mb = [line for line in mb_lines if line.strip()]
    head = [line for line in head_lines if line.strip()]
    cand = [line for line in cand_lines if line.strip()]
    ops = difflib.SequenceMatcher(None, mb, head, autojunk=False).get_opcodes()
    anchors = {line for tag, _i1, _i2, j1, j2 in ops if tag == "equal" for line in head[j1:j2]}
    for tag, _i1, _i2, j1, j2 in ops:
        if tag not in ("insert", "replace"):
            continue
        block = head[max(j1 - 1, 0):j2 + 1]
        if not _in_scope_subsequence(block, cand, anchors):
            return False
    return True


def lines_carry(mb_lines, head_lines, cand_lines) -> bool:
    """Line multiset check: a line the PR added N times appears at least as often on the candidate as
    on the PR head; a line the PR removed appears no more often than on the PR head (Prism
    320e231efa19). Then the order check (:func:`_order_carried`). A later newer edit on main that
    touches neither is fine."""
    mb, head, cand = _counts(mb_lines), _counts(head_lines), _counts(cand_lines)
    for line in set(mb) | set(head):
        if head[line] > mb[line] and cand[line] < head[line]:
            return False
        if head[line] < mb[line] and cand[line] > head[line]:
            return False
    return _order_carried(mb_lines, head_lines, cand_lines)


def file_carries(entry: dict, cand: Optional[bytes], mb: Optional[bytes], head: Optional[bytes]) -> bool:
    """Does the candidate's copy of the file (``None`` = absent) carry this PR file entry's change?

    ``mb``/``head`` are the file at the PR's merge-base and head (``None`` = absent there). A file whose
    head opens with YAML frontmatter is compared key by key on the header and line by line on the
    body after it; an unparseable header on any side refuses (Prism f1da33336140, 397de598ef77,
    04e61c328b13, 4e78214bba60).
    """
    if entry.get("status") == "removed":
        return cand is None
    if cand is None or head is None:
        return False
    if not isinstance(entry.get("patch"), str):
        # No patch (binary or too large to diff): only the exact blob proves it.
        return _blob_sha(cand) == entry.get("sha")
    try:
        mine = split_frontmatter(head)
        if mine is None:
            return lines_carry(_lines(mb) if mb is not None else [], _lines(head), _lines(cand))
        target = split_frontmatter(cand)
        before = split_frontmatter(mb) if mb is not None else ({}, [])
    except _Unparseable:
        return False
    if target is None:
        return False
    if before is None:  # merge-base had no header: all of it was body
        before = ({}, _lines(mb or b""))
    return frontmatter_carries(before[0], mine[0], target[0]) and lines_carry(before[1], mine[1], target[1])


def pr_content_check(repo: str, number: int, sha: str) -> Optional[dict]:
    """``{"sha": <carrier>, "requested": <full sha>, "default": <branch>, "differs": [paths]}``.

    ``None`` when unknown. The requested ``sha`` must be reachable from the default branch (compare
    status identical/behind) before any content is read; otherwise the answer carries
    ``"off_default": True`` and no carrier (Prism 4aa42857aa36). Candidates are the requested ``sha``
    and the default-branch tip, pinned to its commit sha. ``sha`` in the answer is the ONE candidate
    commit that carries the most PR files (the requested commit wins a tie), and ``differs`` lists
    every file that commit does not carry. Empty ``differs`` therefore means that commit alone carries
    all of the PR's content, so it is safe to record as survivor evidence (Prism 6265717768a4). The
    PR side is measured from the merge-base of base and head, never ``base.sha`` (a later main).
    """
    try:
        full = str(_json([f"repos/{repo}/commits/{quote(sha)}"]).get("sha") or "")
        default = str(_json([f"repos/{repo}"]).get("default_branch") or "")
        tip = str(_json([f"repos/{repo}/commits/{quote(default)}"]).get("sha") or "") if default else ""
        if not full or not default or not tip:
            return None
        reach = str(_json([f"repos/{repo}/compare/{tip}...{full}"]).get("status") or "")
        if reach not in ("identical", "behind"):
            return {"sha": None, "requested": full, "default": default, "differs": [],
                    "off_default": True}
        pr = _json([f"repos/{repo}/pulls/{int(number)}"])
        base_sha = str((pr.get("base") or {}).get("sha") or "")
        head_sha = str((pr.get("head") or {}).get("sha") or "")
        if not base_sha or not head_sha:
            return None
        mb_sha = str((_json([f"repos/{repo}/compare/{base_sha}...{head_sha}"])
                      .get("merge_base_commit") or {}).get("sha") or "")
        out = _gh(["--paginate", f"repos/{repo}/pulls/{int(number)}/files?per_page=100",
                   "--jq", ".[] | @json"])
        if not mb_sha or out is None:
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
            status = entry.get("status")
            old_path = str(entry.get("previous_filename") or path)
            mb = None if status == "added" else _file_bytes(repo, old_path, mb_sha)
            head = None if status == "removed" else _file_bytes(repo, path, head_sha)
            renamed_from = old_path if status == "renamed" and old_path != path else None
            for ref in candidates:
                # A rename is carried at its NEW path, and only once the old path is gone (Prism
                # 1b9ec408800f): a copy beside the original is not the PR's rename.
                if (not file_carries(entry, _file_bytes(repo, path, ref), mb, head)
                        or (renamed_from and _file_bytes(repo, renamed_from, ref) is not None)):
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

    def __init__(self, task_id: str, differs: dict, unverified: list, off_default=()):
        self.task_id = task_id
        self.differs = {pr: list(paths) for pr, paths in differs.items()}
        self.unverified = list(unverified)
        self.off_default = list(off_default)
        parts = ["done refused: --superseded-by names a commit that does not carry this card's PR content."]
        if self.off_default:
            parts.append(f"{', '.join(self.off_default)} is not on the default branch; a superseder "
                         f"must be a landed commit.")
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
    return list(dict.fromkeys(t for t in _TARGET_RE.findall(superseded_by) if _is_sha(t)))


def token_shas(*texts: Optional[str]) -> list:
    """Commit SHAs named by a ``SUPERSEDED-BY <sha>`` / ``RE-CARRIED-AS <sha>`` token in free text
    (``--result`` / ``--summary``), parsed with the closed-unmerged gate's own regex so the content
    gate sees every SHA that gate would accept (Prism abd009e5ca09 / 37e57666a117). PR-number targets
    are left to that gate's merged-PR check."""
    from hermes_cli.kanban_open_pr import _SUPERSEDE_RE
    out: list = []
    for text in texts:
        if isinstance(text, str):
            out += [t for t in _SUPERSEDE_RE.findall(text) if _is_sha(t)]
    return list(dict.fromkeys(out))


def _is_sha(token: str) -> bool:
    return all(c in "0123456789abcdef" for c in token) and any(c in "abcdef" for c in token)


def verify(task_id: str, prs, shas, *, check: Optional[ContentCheckFn] = None) -> list:
    """Survivor refs proving each fleet PR in ``prs`` is carried by one of ``shas`` or the default branch.

    ``[]`` when there is nothing to verify (no SHA, no own PR, or no oracle under pytest). Raises
    :class:`SupersededContentError` naming every PR whose files differ, or that could not be read.
    """
    if check is None:
        check = default_content_check()
    if check is None or not shas or not prs:
        return []
    differs, unverified, off_default, survivors = {}, [], [], []
    for ref in prs:
        key = f"{ref.repo}#{ref.number}"
        results = [check(ref.repo, ref.number, sha) for sha in shas]
        off = [r for r in results if r is not None and r.get("off_default")]
        if off:
            off_default.extend(r["requested"] for r in off if r["requested"] not in off_default)
            continue
        ok = next((r for r in results if r is not None and not r.get("differs") and r.get("sha")), None)
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
    if differs or unverified or off_default:
        raise SupersededContentError(task_id, differs, unverified, off_default)
    return survivors
