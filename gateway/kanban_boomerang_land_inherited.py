"""Boomerang wake ``land_inherited``: an own-repo red whose red is PROVEN inherited from main (t_47d1968c).

Row ``own_repo_review_red`` only. The child's verdict carries a structured proof::

    "inherited": {"family": "<suites.json suite id | main-gate-set.json gate id>", "job": "ci-shard-4",
                  "anchor_or_test": {"file": "<source>", "anchor": "<literal>", "harness": "<mutate_*.sh>"}
                                    | "<test id / failing string>",
                  "main_sha": "<sha>", "pr_files": [...], "proof": ["<command + result>", ...]}

The executor trusts none of it except as a pointer. It RE-VERIFIES, from GitHub and the base tree, before its one
write (:func:`verify`):

(a) the PR's changed files (``pulls/N/files``, REST) touch none of the red check's owning paths. Grade mode: the
    family's own paths (``.github/ci/suites.json`` ``suites[family].paths`` with ``@globset`` and ``!`` negation, or
    ``scripts/lib/main-gate-set.json`` ``gates[id==family].paths``), read at main's tip. Anchor mode (the #6313
    shape): the two files that decide the failure, the anchored source and the harness naming the anchor; both
    must themselves be owned by the family.
(b) the red is main's own: anchor mode, the harness at main carries the anchor literal while the source at main
    carries it zero times; grade mode, the family is red in main's newest completed push:main grade
    (``state/main-green-streak.json``, fresh <= 30 min).

Then: a ``review_coverage`` APPROVE at the PR head on the card (the operator's; never stamped here), zero open
Prism P0/P1 at the head, and no ``--accept-open-p1`` / ``--bypass-fleetreview`` / ``--break-glass`` in the row's
existing fm_args. Any miss raises :class:`Degrade`; the executor then surfaces the wake with the reason.

On pass the ONE write is ``scripts/lib/landing_queue.py enqueue`` with the row's existing fm_args (an old
``--allow-red-ci`` replaced) plus ``--allow-red-ci "inherited: <family> red on main at <sha10> (...)"``; fleet-merge
still judges every red at land time (a NEW red refuses rc=9).

Kill switch: ``<HERMES_HOME>/state/boomerang-wake-land-inherited.disabled``, read on every verdict.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

ACTION = "land_inherited"
KILL_FILE = "boomerang-wake-land-inherited.disabled"
LEDGER_FILE = "boomerang-wake.jsonl"
SUITES_PATH = ".github/ci/suites.json"
GATES_PATH = "scripts/lib/main-gate-set.json"
FORBIDDEN_FM = ("--accept-open-p1", "--bypass-fleetreview", "--break-glass")
STREAK_FRESH_S = 30 * 60
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_COVERAGE_RE = re.compile(r"^review_coverage:\s*(\{.*\})\s*$", re.M)


class Degrade(Exception):
    """A re-check failed: ``land_inherited`` becomes ``surface`` with this reason."""


def _root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


def kill_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state" / KILL_FILE


def disabled() -> bool:
    return kill_path().exists()


# --- I/O (REST only; GraphQL is the shared per-user bucket) -------------------------------------

class GhIo:
    """Every outside read and the one write. Tests swap :data:`io` for a fake."""

    def _api(self, path: str, *, raw: bool = False) -> tuple:
        argv = ["gh", "api", path] + (["-H", "Accept: application/vnd.github.raw"] if raw else [])
        try:
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return p.returncode, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout))

    def _json(self, path: str) -> Any:
        rc, out = self._api(path)
        if rc != 0:
            raise Degrade(f"gh api {path.split('?')[0]} failed: {str(out).strip()[:160]}")
        try:
            return json.loads(out or "null")
        except ValueError as exc:
            raise Degrade(f"gh api {path.split('?')[0]}: not JSON") from exc

    def pr(self, repo: str, number: int) -> dict:
        return self._json(f"repos/{repo}/pulls/{int(number)}") or {}

    def pr_files(self, repo: str, number: int) -> list:
        files: list = []
        for page in range(1, 31):   # GitHub caps this list at 3000 files
            batch = self._json(f"repos/{repo}/pulls/{int(number)}/files?per_page=100&page={page}") or []
            files.extend(str(f.get("filename") or "") for f in batch)
            if len(batch) < 100:
                return files
        raise Degrade("PR file list past 3000 files: unreadable in full")

    def branch_sha(self, repo: str, branch: str) -> str:
        return str((self._json(f"repos/{repo}/commits/{quote(branch, safe='')}") or {}).get("sha") or "")

    def raw(self, repo: str, path: str, ref: str) -> Optional[str]:
        """File text at ``ref``; None when the file does not exist there (404); Degrade on any other error."""
        rc, out = self._api(f"repos/{repo}/contents/{quote(path)}?ref={ref}", raw=True)
        if rc == 0:
            return out
        if "404" in str(out):
            return None
        raise Degrade(f"{path}@{ref[:10]} unreadable: {str(out).strip()[:160]}")

    def prism_open(self, repo: str, number: int, head: str, card: str) -> tuple:
        """-> (p0, p1, line) through landing_queue's own counter (None, None, why) when unread."""
        mod = _landing_queue()
        return mod.prism_open(repo, number, head, card)

    def streak(self) -> Optional[dict]:
        try:
            return json.loads((_root() / "state" / "main-green-streak.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    def queue_row(self, repo: str, number: int) -> Optional[dict]:
        name = f"{repo.replace('/', '__')}__{int(number)}.json"
        for sub in ("landing-queue", "landing-queue.done"):
            try:
                row = json.loads((_root() / "state" / sub / name).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(row, dict):
                return row
        return None

    def enqueue(self, argv: list, actor: str = "") -> tuple:
        """-> (rc, output) of ``landing_queue.py enqueue``; ``actor`` becomes the row's ``enqueued_by``."""
        script = _root() / "scripts" / "lib" / "landing_queue.py"
        env = dict(os.environ, HERMES_SESSION_ID=actor or "boomerang-wake")
        try:
            p = subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, timeout=300, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def _landing_queue() -> Any:
    import importlib.util

    path = _root() / "scripts" / "lib" / "landing_queue.py"
    spec = importlib.util.spec_from_file_location("_hermes_landing_queue", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: Seam (tests): every GitHub / filesystem read and the enqueue.
io = GhIo()


# --- owning paths -----------------------------------------------------------------------------

def _glob_re(pattern: str) -> "re.Pattern":
    """GitHub ``paths:`` glob: ``**/`` = zero or more dirs, ``**`` = anything, ``*`` / ``?`` stay in one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            j = pattern.find("]", i)
            if j < 0:
                out.append(re.escape(pattern[i]))
                i += 1
            else:
                out.append(pattern[i:j + 1])
                i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def owned(path: str, patterns: list, globsets: Optional[dict] = None) -> bool:
    """Ordered globs, a later ``!`` pattern wins; ``@name`` expands to ``globsets[name]``."""
    hit = False
    for raw in patterns:
        neg = raw.startswith("!")
        pat = raw[1:] if neg else raw
        pats = (globsets or {}).get(pat[1:], []) if pat.startswith("@") else [pat]
        if any(_glob_re(p).match(path) for p in pats):
            hit = not neg
    return hit


def family_paths(repo: str, family: str, main_sha: str) -> tuple:
    """-> (patterns, globsets, source) of ``family`` at main's tip, or Degrade."""
    text = io.raw(repo, SUITES_PATH, main_sha)
    if text:
        doc = json.loads(text)
        suite = (doc.get("suites") or {}).get(family)
        if isinstance(suite, dict):
            if suite.get("tree_lint"):
                raise Degrade(f"suite {family} grades the whole tree (tree_lint): no owning-path set to clear")
            if not suite.get("paths"):
                raise Degrade(f"suite {family} has no paths: (a REF suite); owning paths unknown")
            return list(suite["paths"]), dict(doc.get("globsets") or {}), SUITES_PATH
    text = io.raw(repo, GATES_PATH, main_sha)
    if text:
        for gate in json.loads(text).get("gates") or []:
            if gate.get("id") == family:
                if not gate.get("paths"):
                    raise Degrade(f"gate {family} has no paths: owning paths are its workflow filter")
                return list(gate["paths"]), {}, GATES_PATH
    raise Degrade(f"family {family!r} is in neither {SUITES_PATH} nor {GATES_PATH} at main {main_sha[:10]}")


# --- the two re-checks -------------------------------------------------------------------------

def _anchor_mode(repo: str, family: str, proof: dict, files: list, patterns: list, globsets: dict,
                 main_sha: str, head: str) -> list:
    """(a)+(b) on the anchor shape; returns evidence lines or raises Degrade.

    The anchor must be present at the PR head's copy of the source (which the PR does not change, so it is the
    PR's base copy) and absent at main: main moved the line, the PR did not. That also rejects a mistyped or
    mis-escaped anchor, which would otherwise read "absent at main" for free."""
    src, raw_anchor, harness = (str(proof.get(k) or "") for k in ("file", "anchor", "harness"))
    if not (src and raw_anchor and harness):
        raise Degrade("anchor proof needs file, anchor and harness")
    for f in (src, harness):
        if not owned(f, patterns, globsets):
            raise Degrade(f"anchor file {f} is not owned by {family}")
        if f in files:
            raise Degrade(f"(a) the PR changes {f}, a file that decides the {family} failure")
    at_head = io.raw(repo, src, head) or ""
    anchor = next((a for a in dict.fromkeys((raw_anchor, _unescape(raw_anchor))) if at_head.count(a) >= 1), None)
    if anchor is None:
        raise Degrade(f"(b) anchor not found at {src}@PR head {head[:10]}: not a real anchor")
    htext = io.raw(repo, harness, main_sha)
    if htext is None or not _harness_names(htext, anchor):
        raise Degrade(f"(b) {harness}@main {main_sha[:10]} does not carry the anchor")
    n = (io.raw(repo, src, main_sha) or "").count(anchor)
    if n != 0:
        raise Degrade(f"(b) anchor present {n}x at {src}@main {main_sha[:10]}: the red is not main's")
    return [f"(a) PR touches neither {src} nor {harness}",
            f"(b) anchor present at {src}@PR head {head[:10]}, absent at main {main_sha[:10]}; "
            f"{harness}@main still names it"]


def _unescape(text: str) -> str:
    """bash $'...' escapes a child may have copied verbatim (\\n, \\t, \\', \\\\)."""
    out, i = [], 0
    table = {"n": "\n", "t": "\t", "'": "'", '"': '"', "\\": "\\"}
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in table:
            out.append(table[text[i + 1]])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _harness_names(text: str, anchor: str) -> bool:
    """The harness quotes the anchor (bash '...' / $'...' escapes are resolved for the compare)."""
    if anchor in text:
        return True
    esc = anchor.replace("\\", "\\\\").replace("\n", "\\n").replace("'", "\\'")
    return esc in text


def _grade_mode(repo: str, family: str, files: list, patterns: list, globsets: dict) -> list:
    hit = [f for f in files if owned(f, patterns, globsets)]
    if hit:
        raise Degrade(f"(a) the PR changes {len(hit)} file(s) {family} owns: {', '.join(hit[:3])}")
    st = io.streak()
    if not isinstance(st, dict) or str(st.get("repo") or "").lower() != repo.lower():
        raise Degrade(f"(b) no main grade for {repo} (main-green-streak missing or for another repo)")
    try:
        age = time.time() - _epoch(st.get("checked_at"))
    except (TypeError, ValueError) as exc:
        raise Degrade("(b) main grade has no checked_at") from exc
    if age > STREAK_FRESH_S:
        raise Degrade(f"(b) main grade stale ({int(age // 60)} min)")
    detail = st.get("red_detail") or {}
    names = set(st.get("red_families") or []) | set(detail)
    names |= {str(v.get("step") or "") for v in detail.values() if isinstance(v, dict)}
    if not any(family == n or family in n.split() for n in names):
        raise Degrade(f"(b) {family} is not red in main's newest grade ({str(st.get('red_run_sha') or '?')[:10]})")
    return [f"(a) PR touches none of {family}'s owning paths",
            f"(b) {family} red in main's newest grade at {str(st.get('red_run_sha') or '?')[:10]}"]


def _epoch(iso: Any) -> float:
    import calendar

    return float(calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")))


def coverage_at(comments: list, head: str) -> Optional[dict]:
    """The newest ``review_coverage`` APPROVE on the card naming ``head``; None when the newest at it is not one."""
    for body in reversed([str(c) for c in comments]):
        for m in reversed(list(_COVERAGE_RE.finditer(body))):
            try:
                rec = json.loads(m.group(1))
            except ValueError:
                continue
            if str(rec.get("head_sha") or "").lower() != head.lower():
                continue
            return rec if str(rec.get("verdict") or "").upper() == "APPROVE" else None
    return None


def verify(entry: dict, verdict: dict, comments: list) -> dict:
    """Re-verify the child's inherited claim; returns the write plan or raises :class:`Degrade`."""
    if disabled():
        raise Degrade(f"land_inherited disabled ({kill_path()} present)")
    proof = verdict.get("inherited")
    if not isinstance(proof, dict) or not str(proof.get("family") or "").strip():
        raise Degrade("no structured inherited proof (family missing)")
    family = str(proof["family"]).strip()
    repo, _, num = str(entry.get("pr") or "").partition("#")
    head = str(entry.get("head") or "").lower()
    if not (repo and num.isdigit() and _SHA_RE.fullmatch(head)):
        raise Degrade("card has no fleet PR at a known head")
    number = int(num)
    pr = io.pr(repo, number)
    if pr.get("state") != "open" or pr.get("merged"):
        raise Degrade(f"{repo}#{number} is not open")
    if str((pr.get("head") or {}).get("sha") or "").lower() != head:
        raise Degrade("PR head moved since the claim")
    base = str((pr.get("base") or {}).get("ref") or "main")
    files = io.pr_files(repo, number)
    main_sha = io.branch_sha(repo, base)
    if not _SHA_RE.fullmatch(main_sha):
        raise Degrade(f"{base} tip unreadable")
    patterns, globsets, source = family_paths(repo, family, main_sha)
    anchor = proof.get("anchor_or_test")
    reasons = []
    lines = None
    if isinstance(anchor, dict):
        try:
            lines = _anchor_mode(repo, family, anchor, files, patterns, globsets, main_sha, head)
        except Degrade as exc:
            reasons.append(str(exc))
    if lines is None:
        try:
            lines = _grade_mode(repo, family, files, patterns, globsets)
        except Degrade as exc:
            reasons.append(str(exc))
            raise Degrade("; ".join(reasons)) from exc
    cov = coverage_at(comments, head)
    if cov is None:
        raise Degrade(f"no operator review_coverage APPROVE at {head[:10]} on the card")
    p0, p1, pline = io.prism_open(repo, number, head, entry["task_id"])
    if p0 is None:
        raise Degrade(f"Prism P0/P1 count unread ({str(pline)[:120]})")
    if p0 or p1:
        raise Degrade(f"open Prism P0={p0} P1={p1} at {head[:10]}")
    row = io.queue_row(repo, number) or {}
    fm, skip = [], False
    for arg in row.get("fm_args") or []:
        if skip:
            skip = False
            continue
        if arg == "--allow-red-ci":
            skip = True
            continue
        fm.append(str(arg))
    bad = [a for a in fm if a.split("=", 1)[0] in FORBIDDEN_FM]
    if bad:
        raise Degrade(f"row fm_args carry {', '.join(bad)}: not a machine land")
    return {"repo": repo, "pr": number, "head": head, "family": family, "main_sha": main_sha, "base": base,
            "by": str(row.get("by") or cov.get("by") or "apollo/operator"), "fm_args": fm, "source": source,
            "lines": lines, "job": str(proof.get("job") or ""), "child_proof": [str(x) for x in proof.get("proof") or []][:5]}


def enqueue_argv(plan: dict, task_id: str, child_session: str) -> list:
    waiver = (f"inherited: {plan['family']} red on main at {plan['main_sha'][:10]} "
              f"(boomerang-wake land_inherited, child {child_session or '?'})")
    argv = ["enqueue", "--repo", plan["repo"], "--pr", str(plan["pr"]), "--card", task_id, "--head", plan["head"],
            "--reason", f"[boomerang-wake] land_inherited: {plan['family']} red is inherited from main",
            "--by", plan["by"], "--", *plan["fm_args"], "--allow-red-ci", waiver]
    if any(a.split("=", 1)[0] in FORBIDDEN_FM for a in argv):
        raise RuntimeError("forbidden fleet-merge flag in a land_inherited enqueue")
    return argv


def enqueue(plan: dict, task_id: str, child_session: str) -> str:
    rc, out = io.enqueue(enqueue_argv(plan, task_id, child_session), f"boomerang-wake:{child_session or '?'}")
    if rc != 0:
        raise RuntimeError(f"landing_queue enqueue rc={rc}: {out.splitlines()[-1][:200] if out else ''}")
    return out


def ledger_row(plan: dict, task_id: str, child_session: str) -> None:
    from hermes_constants import get_hermes_home

    row = {"event": ACTION, "card": task_id, "pr": f"{plan['repo']}#{plan['pr']}", "head": plan["head"],
           "family": plan["family"], "main_sha": plan["main_sha"], "child_session": child_session or None,
           "at": time.time()}
    path = get_hermes_home() / "state" / LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def comment_text(plan: dict, marker: str, delegation_id: str) -> str:
    proof = "".join(f"\n- {x}" for x in plan["lines"] + [f"child: {p}" for p in plan["child_proof"]])
    return (f"{marker} land_inherited: {plan['repo']}#{plan['pr']} @ {plan['head'][:10]} enqueued with "
            f"--allow-red-ci (family {plan['family']} per {plan['source']}, main {plan['main_sha'][:10]}; "
            f"delegation {delegation_id}). Re-verified:{proof}")


def logs_text(plan: dict, task_id: str) -> str:
    return (f"[boomerang-wake] land_inherited {task_id}: {plan['repo']}#{plan['pr']} @ {plan['head'][:10]} "
            f"enqueued, {plan['family']} red inherited from main {plan['main_sha'][:10]}")
