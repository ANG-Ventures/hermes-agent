"""Capture -> verify -> delete for DONE cards whose workspace the survivor gate holds (t_83c38f7b).

The gate (``kanban_survivor.remove_workspace_dir``) is right to refuse a scratch tree no code survivor
vouches for: nothing proves its files are safe to lose. Nothing ever captured them either, so the refusal
never became "preserved, then deleted" and ~750 trees sat on fleet-scratch. Ace 2026-11-04 07:07: for a
done card, delete the tree AFTER preserving it as an attachment on the card.

Per card:
  capture  every file outside a repository plus every untracked/ignored file inside one, minus build
           residue (``kanban_survivor._is_residue`` + ``_DERIVED_DIRS``), into
           ``workspace-reclaim-<card>-<utc>.tar.gz``; tracked changes as ``git diff --binary HEAD`` per
           repository into ``workspace-reclaim-<card>-<utc>.patch``. Stored through ``kanban_survivor._store``.
  verify   the stored tarball re-read from the attachment dir hashes to the sha computed before the write
           and lists exactly the planned members; each patch section ``git apply --check``s against its
           repository's HEAD tree in a throwaway index.
  delete   ``kanban_db.safe_remove_workspace_dir(..., reclaimed=...)``: the same containment, liveness,
           owner, retention and audit gates, then ``remove_workspace_dir`` re-reads the tree and refuses
           if it changed since the capture. The survivor row records ``kind: reclaimed`` with
           ``ref: reclaimed:<attachment id>``; one ``workspace_reclaimed`` event names the attachments and
           the bytes freed.

Anything the capture cannot express (an unpushed commit, a stash, a skip-worktree flag, a nested
repository under residue, a capture over the attachment cap, a remote-host tree) HOLDS with a reason.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_survivor as ks

# The held reasons a capture resolves (card body + spec §1). "local commits not on any remote" is NOT
# here: a diff and a tarball do not carry commits.
RECLAIMABLE_PREFIXES = (
    "survivor_unavailable: remote workspace held: dirty, ignored or untracked work",
    "survivor_unavailable: missing repositories (.) are vouched for only by an unbound operator survivor",
    "survivor_unavailable: remote workspace held: unpreserved scratch files",
    "survivor_unavailable: survivor-none cannot authorize workspace deletion",
    "survivor_unavailable: workspace holds files outside any repository that no inferred survivor vouches for",
    # A previous reclaim attempt's own hold is retried (it may have been a changed tree or a busy cwd).
    "survivor_unavailable: reclaim held",
)
_TERMINAL = ("done", "archived")
_RAW_CAP = 256 * 1024 * 1024   # stop reading before compressing a tree that cannot fit the attachment cap
SECTION = "# workspace-reclaim repository: "


class ReclaimHeld(ValueError):
    """The tree cannot be captured faithfully; it stays."""


def reclaimable(reason: Optional[str]) -> bool:
    return bool(reason) and any(reason.startswith(p) for p in RECLAIMABLE_PREFIXES)


def _residue_dir(name: str) -> bool:
    return name in ks._RESIDUE_DIRS or name in ks._DERIVED_DIRS


def _residue_rel(rel: str) -> bool:
    parts = [p for p in rel.split("/") if p]
    return any(_residue_dir(p) for p in parts[:-1]) or (
        bool(parts) and any(fnmatch.fnmatchcase(parts[-1], f) for f in ks._RESIDUE_FILES))


def _git(repo: Path, *args: str, env=None) -> bytes:
    return ks._git(repo, *args, env=env).stdout


def _git_text(repo, *args: str) -> str:
    """ks.stranded's git: raises on failure; ls-remote is the one network read."""
    net = bool(args) and args[0] == "ls-remote"
    return ks._git(repo, *args, network=net, timeout=60 if net else 30).stdout.decode("utf-8", "surrogateescape")


@dataclass
class Plan:
    workspace: Path
    files: list = field(default_factory=list)       # workspace-relative paths, sorted
    diffs: dict = field(default_factory=dict)       # repo key -> (head sha, diff bytes)
    total_bytes: int = 0                            # lstat size of everything under the tree
    capture_bytes: int = 0                          # raw size of the planned members
    fingerprint: str = ""


def _holds_git(root: Path) -> bool:
    for _here, dirs, files in os.walk(root, onerror=_raise):
        if ".git" in dirs or ".git" in files or ("HEAD" in files and "objects" in dirs and "refs" in dirs):
            return True
    return False


def _raise(exc):
    raise exc


def _tree_bytes(workspace: Path) -> int:
    total = 0
    for here, dirs, files in os.walk(workspace, onerror=_raise):
        for name in dirs + files:
            try:
                total += os.lstat(os.path.join(here, name)).st_size
            except FileNotFoundError:
                continue
    return total


def _repo_dirt(repo: Path, key: str, workspace: Path, repo_set: set, files: list) -> Optional[tuple]:
    """Add the repository's untracked/ignored non-residue files to ``files``; return (head, diff) or None."""
    head = ks._git(repo, "rev-parse", "--verify", "HEAD", check=False)
    try:
        why = ks.stranded(_git_text, repo, ("HEAD",) if head.returncode == 0 else ())
    except (ks.SurvivorUnavailable, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise ReclaimHeld(f"repository {key}: stranded work unknown ({type(exc).__name__})") from exc
    if why:
        raise ReclaimHeld(f"repository {key} has {why}")
    if _git(repo, "stash", "list").strip():
        raise ReclaimHeld(f"repository {key} has stashes")
    flags = _git(repo, "ls-files", "-v", "-z").decode("utf-8", "surrogateescape")
    if any(r[:1] == "S" or r[:1].islower() for r in flags.split("\0") if len(r) > 2):
        raise ReclaimHeld(f"repository {key} has skip-worktree or assume-unchanged files")
    recs = [r for r in _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored",
                            "--ignore-submodules=none").decode("utf-8", "surrogateescape").split("\0") if r]
    tracked = False
    for rec in recs:
        code, rel = rec[:2], rec[3:]
        if code not in ("??", "!!"):
            tracked = True
            continue
        full = repo / rel.rstrip("/")
        if rel.endswith("/"):
            # A collapsed directory: git did not descend (an ignored dir, or a nested repository).
            if full in repo_set:
                continue
            if any(_residue_dir(part) for part in rel.split("/") if part):
                if _holds_git(full):
                    raise ReclaimHeld(f"nested repository under residue in {key}: {rel}")
                continue
            _scan(full, workspace, repo_set, files)
        elif not _residue_rel(rel):
            files.append(str(full.relative_to(workspace)))
        elif _holds_residue_repo(full, repo):
            raise ReclaimHeld(f"nested repository under residue in {key}: {rel}")
    if not tracked:
        return None
    if head.returncode:
        raise ReclaimHeld(f"repository {key} has staged files but no commit")
    sha = head.stdout.decode().strip()
    return sha, _git(repo, "diff", "--binary", "--no-color", "--no-ext-diff", "HEAD")


def _holds_residue_repo(path: Path, top: Path) -> bool:
    roots = [p for p in (path.parent, *path.parents) if _residue_dir(p.name) and p.is_relative_to(top)]
    return bool(roots) and _holds_git(roots[-1])


def _scan(root: Path, workspace: Path, repo_set: set, files: list) -> None:
    """Every non-residue file under ``root`` that no repository below it owns."""
    for here, dirs, names in os.walk(root, onerror=_raise):
        here_p = Path(here)
        keep = []
        for d in dirs:
            p = here_p / d
            if p in repo_set or d == ".git":
                continue
            if _residue_dir(d):
                if _holds_git(p):
                    raise ReclaimHeld(f"nested repository under residue: {p.relative_to(workspace)}")
                continue
            if p.is_symlink():
                files.append(str(p.relative_to(workspace)))
                continue
            keep.append(d)
        dirs[:] = keep
        for n in names:
            if n == ".git":
                continue
            rel = str((here_p / n).relative_to(workspace))
            if not any(fnmatch.fnmatchcase(n, f) for f in ks._RESIDUE_FILES):
                files.append(rel)


def plan(workspace: Path) -> Plan:
    """What a capture of ``workspace`` would hold. Raises ReclaimHeld / SurvivorUnavailable / OSError."""
    workspace = Path(workspace).resolve(strict=True)
    repos = ks._repos(workspace)
    repo_set = set(repos)
    out = Plan(workspace=workspace)
    files: list = []
    # Loose files: everything outside every repository (the worker's scratch dir).
    if workspace not in repo_set:
        _scan(workspace, workspace, repo_set, files)
    for repo in repos:
        key = str(repo.relative_to(workspace)) if repo != workspace else "."
        dirt = _repo_dirt(repo, key, workspace, repo_set, files)
        if dirt is not None:
            out.diffs[key] = dirt
    out.files = sorted(set(files))
    h = hashlib.sha256()
    for rel in out.files:
        st = os.lstat(workspace / rel)
        out.capture_bytes += st.st_size
        h.update(f"f\0{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode("utf-8", "surrogateescape"))
    for key in sorted(out.diffs):
        sha, diff = out.diffs[key]
        h.update(f"d\0{key}\0{sha}\0{hashlib.sha256(diff).hexdigest()}\n".encode())
    out.fingerprint = h.hexdigest()
    out.total_bytes = _tree_bytes(workspace)
    return out


def _tarball(p: Plan) -> bytes:
    if p.capture_bytes > _RAW_CAP:
        raise ReclaimHeld(f"capture of {p.capture_bytes} bytes exceeds the attachment limit")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in p.files:
            tar.add(p.workspace / rel, arcname=rel, recursive=False)
    data = buf.getvalue()
    if len(data) > kb.KANBAN_ATTACHMENT_MAX_BYTES:
        raise ReclaimHeld(f"capture compresses to {len(data)} bytes, over the attachment limit")
    return data


def _patch(p: Plan) -> bytes:
    return b"".join(f"{SECTION}{key} base {sha}\n".encode() + diff
                    for key, (sha, diff) in sorted(p.diffs.items()))


def _sections(data: bytes) -> dict:
    out, key, chunk = {}, None, []
    for line in data.splitlines(keepends=True):
        if line.startswith(SECTION.encode()):
            if key is not None:
                out[key] = b"".join(chunk)
            key, chunk = line[len(SECTION):].decode().rsplit(" base ", 1), []
            key = (key[0], key[1].strip())
        else:
            chunk.append(line)
    if key is not None:
        out[key] = b"".join(chunk)
    return out


def _apply_check(repo: Path, sha: str, diff: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="kanban-reclaim-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        ks._git(repo, "read-tree", sha, env=env)
        res = ks._git(repo, "apply", "--check", "--cached", "--binary", "-", input=diff, env=env, check=False)
        return res.returncode == 0


def _verify(conn, task_id, p: Plan, tar_ref: dict, patch_ref: Optional[dict]) -> None:
    stored = ks._attachment_dir(conn, task_id) / Path(tar_ref["path"]).name
    data = stored.read_bytes()
    if hashlib.sha256(data).hexdigest() != tar_ref["sha256"]:
        raise ReclaimHeld("stored capture does not match the sha computed before the write")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        if sorted(m.name for m in tar.getmembers()) != p.files:
            raise ReclaimHeld("stored capture does not list the planned files")
    if patch_ref is None:
        return
    stored = ks._attachment_dir(conn, task_id) / Path(patch_ref["path"]).name
    data = stored.read_bytes()
    if hashlib.sha256(data).hexdigest() != patch_ref["sha256"]:
        raise ReclaimHeld("stored patch does not match the sha computed before the write")
    sections = _sections(data)
    if {k for k, _ in sections} != set(p.diffs):
        raise ReclaimHeld("stored patch does not hold every repository's diff")
    for (key, sha), diff in sections.items():
        repo = p.workspace if key == "." else p.workspace / key
        if not _apply_check(repo, sha, diff):
            raise ReclaimHeld(f"stored patch for {key} does not apply to {sha[:12]}")


def _attachment_id(conn, task_id, path) -> int:
    row = conn.execute("SELECT id FROM task_attachments WHERE task_id = ? AND stored_path = ? "
                       "ORDER BY id DESC LIMIT 1", (task_id, str(path))).fetchone()
    return int(row[0])


def capture(conn, task_id: str, p: Plan) -> dict:
    """Store and verify the capture; return the ``reclaimed`` record for the deleter."""
    utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    stem = f"workspace-reclaim-{task_id}-{utc}"
    tar_ref = ks._store(conn, task_id, f"{stem}.tar.gz", _tarball(p), "application/x-tar+gzip")
    patch_ref = None
    if p.diffs:
        patch_ref = ks._store(conn, task_id, f"{stem}.patch", _patch(p), "text/x-diff")
    _verify(conn, task_id, p, tar_ref, patch_ref)
    attachments = [dict(tar_ref, id=_attachment_id(conn, task_id, tar_ref["path"]), files=len(p.files))]
    if patch_ref:
        attachments.append(dict(patch_ref, id=_attachment_id(conn, task_id, patch_ref["path"]),
                                repositories=sorted(p.diffs)))
    return {"ref": f"reclaimed:{attachments[0]['id']}", "attachments": attachments,
            "fingerprint": p.fingerprint, "bytes_freed": p.total_bytes}


def verify_unchanged(workspace: Path, reclaimed: dict) -> None:
    """The deleter's last read: the tree must still be what the capture holds."""
    if plan(workspace).fingerprint != reclaimed["fingerprint"]:
        raise ks.SurvivorUnavailable("survivor_unavailable: reclaim held (workspace changed after its capture)")


def record(conn, task_id: str, reclaimed: dict) -> dict:
    """The resolution row: ``kind: reclaimed``, the hold cleared, the earlier survivor kept under ``previous``
    (no write may shrink the recovery index), and one ``workspace_reclaimed`` event."""
    _bases, held, previous = ks._state(conn, task_id)
    survivor = {"kind": "reclaimed", "ref": reclaimed["ref"], "attachments": reclaimed["attachments"],
                "bytes_freed": reclaimed["bytes_freed"], "held_reason": held, "previous": previous}
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_workspace_survivors(task_id, survivor) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET survivor = excluded.survivor, held_reason = NULL",
            (task_id, json.dumps(survivor)))
        kb._append_event(conn, task_id, "workspace_reclaimed", {
            "ref": reclaimed["ref"], "attachment_ids": [a["id"] for a in reclaimed["attachments"]],
            "bytes_freed": reclaimed["bytes_freed"], "held_reason": held})
    return survivor


# --------------------------------------------------------------------------- the sweep

@dataclass
class Candidate:
    task_id: str
    status: str
    path: str
    reason: str
    host: Optional[str]
    completed_at: Optional[int]


def candidates(conn, *, min_age_days: float = 2, now: Optional[float] = None) -> list:
    from hermes_cli.kanban_continuation import STUDIO
    cutoff = (now if now is not None else time.time()) - min_age_days * 86400
    rows = conn.execute(
        "SELECT t.id, t.status, t.workspace_kind, t.workspace_path, t.completed_at, s.held_reason, "
        "(SELECT host FROM task_runs r WHERE r.task_id = t.id AND r.host IS NOT NULL "
        " ORDER BY r.id DESC LIMIT 1) AS host "
        "FROM tasks t JOIN task_workspace_survivors s ON s.task_id = t.id "
        "WHERE s.held_reason IS NOT NULL AND t.status IN ('done', 'archived') "
        "AND t.workspace_kind = 'scratch' AND t.workspace_path IS NOT NULL ORDER BY t.id").fetchall()
    out = []
    for r in rows:
        if not reclaimable(r["held_reason"]) or (r["completed_at"] or 0) > cutoff:
            continue
        if not Path(r["workspace_path"]).is_dir():
            continue
        host = r["host"] if r["host"] not in (None, STUDIO) else None
        out.append(Candidate(r["id"], r["status"], r["workspace_path"], r["held_reason"], host, r["completed_at"]))
    return out


def _skip_reason(conn, c: Candidate) -> Optional[str]:
    if c.host:
        return f"remote-host tree ({c.host}): capture over ssh is not built"
    task = kb.get_task(conn, c.task_id)
    if task is None or task.status not in _TERMINAL:
        return "card is not done/archived"
    if kb._task_has_live_run(conn, c.task_id):
        return "card has a live run"
    if kbw._has_active_children(conn, c.task_id):
        return "active-children-need-handoff"
    if not kb._is_managed_scratch_path(Path(c.path)):
        return "not a managed scratch path"
    if kb._process_cwd_within(Path(c.path)):
        return "a process has its cwd in the tree (or the cwd scan failed)"
    return None


def reclaim_one(conn, c: Candidate, *, dry_run: bool) -> dict:
    row = {"card": c.task_id, "reason": c.reason.strip()[:120], "path": c.path}
    skip = _skip_reason(conn, c)
    if skip:
        return dict(row, outcome="skipped", why=skip)
    try:
        p = plan(Path(c.path))
    except (ReclaimHeld, ks.SurvivorUnavailable, OSError, subprocess.SubprocessError, ValueError) as exc:
        why = str(exc).removeprefix("survivor_unavailable: ")
        if not dry_run:
            _hold(conn, c, why)
        return dict(row, outcome="held", why=why)
    row.update(bytes=p.total_bytes, files=len(p.files), capture_bytes=p.capture_bytes,
               repositories_with_diff=sorted(p.diffs))
    if dry_run:
        return dict(row, outcome="would-reclaim")
    try:
        reclaimed = capture(conn, c.task_id, p)
    except (ReclaimHeld, ks.SurvivorUnavailable, OSError, subprocess.SubprocessError, ValueError) as exc:
        why = str(exc).removeprefix("survivor_unavailable: ")
        _hold(conn, c, why)
        return dict(row, outcome="held", why=why)
    removed = kb.safe_remove_workspace_dir(Path(c.path), task_id=c.task_id, reason="reclaim_held",
                                           conn=conn, reclaimed=reclaimed)
    if not removed:
        return dict(row, outcome="held", why="deleter refused after capture (see workspace_held / deletion log)",
                    attachments=[a["id"] for a in reclaimed["attachments"]])
    return dict(row, outcome="reclaimed", attachments=[a["id"] for a in reclaimed["attachments"]])


def _hold(conn, c: Candidate, why: str) -> None:
    """Record the hold, keeping the original reason; a weekly re-run of an unchanged verdict writes nothing."""
    original = c.reason
    if original.startswith("survivor_unavailable: reclaim held") and "; was: " in original:
        original = original.split("; was: ", 1)[1]
    reason = f"survivor_unavailable: reclaim held ({why}); was: {original.strip()}"
    if reason != c.reason:
        ks._hold(conn, c.task_id, reason)


def sweep(conn, *, dry_run: bool = True, limit: Optional[int] = None, min_age_days: float = 2) -> list:
    found = candidates(conn, min_age_days=min_age_days)
    if limit is not None:
        found = found[:limit]
    with kb.process_cwd_snapshot_scope():
        return [reclaim_one(conn, c, dry_run=dry_run) for c in found]


# --------------------------------------------------------------------------- CLI

def add_arguments(p) -> None:
    p.add_argument("--apply", action="store_true",
                   help="Capture, verify and delete (default is a dry run that writes nothing). "
                        "Needs the operator token (KANBAN_OPERATOR_TOKEN).")
    p.add_argument("--dry-run", action="store_true", help="Report only (the default; explicit for scripts).")
    p.add_argument("--limit", type=int, default=None, help="At most N candidate cards.")
    p.add_argument("--min-age-days", type=float, default=2,
                   help="Only cards completed at least N days ago (default 2).")
    p.add_argument("--json", dest="as_json", action="store_true", help="One JSON object per card, then a total.")


def _gib(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GiB"


def run(args) -> int:
    import sys
    from hermes_cli import kanban_db_connect as kbc
    apply = bool(getattr(args, "apply", False))
    if apply and getattr(args, "dry_run", False):
        print("kanban reclaim-workspaces: --apply and --dry-run are exclusive", file=sys.stderr)
        return 2
    if apply and kb._operator_token_state() != "ok":
        print("kanban reclaim-workspaces: --apply deletes trees and needs the operator token: "
              f"{kb.OPERATOR_TOKEN_ENV}=$(cat {kb.operator_token_path()}) hermes kanban reclaim-workspaces "
              "--apply ...", file=sys.stderr)
        return 1
    with kbc.connect_closing() as conn:
        rows = sweep(conn, dry_run=not apply, limit=args.limit, min_age_days=args.min_age_days)
    totals: dict = {}
    for r in rows:
        t = totals.setdefault(r["outcome"], {"cards": 0, "bytes": 0})
        t["cards"] += 1
        t["bytes"] += int(r.get("bytes") or 0)
        if args.as_json:
            print(json.dumps(r, sort_keys=True))
        else:
            extra = (f"bytes={r['bytes']} files={r['files']} capture_bytes={r['capture_bytes']}"
                     if "bytes" in r else f"why={r.get('why')}")
            print(f"{r['outcome']:14s} {r['card']} {extra} reason={r['reason'][:70]!r}")
    if args.as_json:
        print(json.dumps({"total": totals, "applied": apply}, sort_keys=True))
    else:
        mode = "applied" if apply else "dry-run (nothing written)"
        parts = ", ".join(f"{k} {v['cards']} ({_gib(v['bytes'])})" for k, v in sorted(totals.items())) or "no candidates"
        print(f"reclaim-workspaces {mode}: {parts}")
    return 0
