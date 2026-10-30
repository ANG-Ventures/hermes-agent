"""A closed own PR whose trusted ``CLOSED: SUPERSEDED-BY #N`` names a still-OPEN #N parks the card on #N (t_ec8ceae2).

t_d781ecd9: hermes-home#7730 was closed ``CLOSED: SUPERSEDED-BY #7816`` while #7816 was open. The closed-unmerged
gate accepts a superseder only once it is MERGED, and ``--abandon-routed-pr`` only ABANDONED/REJECTED/THROWAWAY
records, so every verb refused and the card sat blocked. With an explicit ``--superseded-by`` or
``--abandon-routed-pr``, :func:`park_targets` follows each closed PR's newest trusted ``CLOSED:`` line; when every one
names an OPEN fleet PR that names the card (its body or a trusted comment), :func:`park` moves the card to ``review``
with those PRs as its own PRs: the open-PR route, so the card closes when they merge (``survivor PR open; card closes
on merged=true``). Audited ``routed_pr_superseded``. Anything unreadable, untrusted or unnamed parks nothing and the
original refusal stands.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from typing import Callable, Optional

QUERY_TIMEOUT_SECONDS = 15
FOLLOW = ("SUPERSEDED-BY", "RE-CARRIED-AS")
_CLOSED_LINE = re.compile(r"^\W{0,6}CLOSED:\s*([A-Z][A-Z0-9_-]+)\b[ \t]*([^\n]*)", re.M)
_REF = re.compile(r"^(?:https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)|([\w.-]+/[\w.-]+)#(\d+)|#(\d+))\b")
TRUSTED_LOGINS = frozenset({"kyzcreig"})
TRUSTED_ASSOC = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_FLEET_BOT = re.compile(r"^ang-[a-z0-9-]+\[bot\]$")

ThreadFn = Callable[[str, int], Optional[dict]]


def trusted(login, assoc=None) -> bool:
    """The author set scripts/lib/superseded_by.py trusts: a fleet ``ang-*[bot]`` App, Kyzcreig, or a repo
    OWNER/MEMBER/COLLABORATOR."""
    lg = str(login or "").strip().lower()
    return bool(_FLEET_BOT.match(lg)) or lg in TRUSTED_LOGINS or str(assoc or "").upper() in TRUSTED_ASSOC


def close_token(comments) -> tuple:
    """``(TOKEN, rest)`` from the newest trusted comment carrying a ``CLOSED:`` line, else ``(None, "")``."""
    for c in reversed(comments or []):
        if not isinstance(c, dict) or not trusted(c.get("login"), c.get("assoc")):
            continue
        m = _CLOSED_LINE.search(str(c.get("body") or ""))
        if m:
            return m.group(1), m.group(2).strip()
    return None, ""


def pr_thread(repo: str, number: int) -> Optional[dict]:
    """``{"body": str, "comments": [{"body", "login", "assoc"}]}`` over REST, or None when unreadable."""
    def _gh(args):
        try:
            proc = subprocess.run(["gh", "api", "--hostname", "github.com", *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=QUERY_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            return None
        return proc.stdout if proc.returncode == 0 else None
    body = _gh([f"repos/{repo}/pulls/{int(number)}", "--jq", ".body // \"\" | @json"])
    out = _gh(["--paginate", f"repos/{repo}/issues/{int(number)}/comments?per_page=100", "--jq",
               ".[] | {body: (.body // \"\"), login: (.user.login // \"\"), assoc: (.author_association // \"\")}"])
    if body is None or out is None:
        return None
    try:
        return {"body": json.loads(body or '""'),
                "comments": [json.loads(ln) for ln in out.splitlines() if ln.strip()]}
    except ValueError:
        return None


def _default_thread() -> Optional[ThreadFn]:
    """The real reader, or None inside pytest (no live GitHub)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return pr_thread


def _names_card(thread: dict, task_id: str) -> bool:
    pat = re.compile(rf"(?<![0-9A-Za-z_]){re.escape(task_id)}(?![0-9A-Za-z_])")
    if pat.search(str(thread.get("body") or "")):
        return True
    return any(pat.search(str(c.get("body") or "")) for c in thread.get("comments") or ()
               if isinstance(c, dict) and trusted(c.get("login"), c.get("assoc")))


def successor(closed_ref, task_id: str, *, thread_fn: ThreadFn, query_fn) -> Optional[tuple]:
    """``(repo, n, token line)`` of the OPEN fleet PR ``closed_ref``'s trusted close token names, when that PR names
    ``task_id``; None otherwise (no token, a sha target, not open, unreadable, or it does not name the card)."""
    from hermes_cli import kanban_open_pr as _open_pr
    thread = thread_fn(closed_ref.repo, closed_ref.number)
    if not isinstance(thread, dict):
        return None
    tok, rest = close_token(thread.get("comments"))
    if tok not in FOLLOW:
        return None
    m = _REF.match(rest.lstrip(": "))
    if not m:
        return None
    repo = m.group(1) or m.group(3) or closed_ref.repo
    number = int(m.group(2) or m.group(4) or m.group(5))
    if (repo.lower(), number) == (closed_ref.repo.lower(), closed_ref.number):
        return None
    if repo.split("/", 1)[0].lower() not in _open_pr.FLEET_OWNERS:
        return None
    try:
        state = str((query_fn(repo, number) or {}).get("state") or "").upper()
    except Exception:  # noqa: BLE001 -- unreadable parks nothing
        return None
    if state != "OPEN":
        return None
    succ = thread_fn(repo, number)
    if not isinstance(succ, dict) or not _names_card(succ, task_id):
        return None
    return repo, number, f"CLOSED: {tok} {rest}"[:200]


def park_targets(task_id: str, closed, *, superseded_by: Optional[str], query_fn,
                 thread_fn: Optional[ThreadFn] = None) -> Optional[list]:
    """``[{closed, successor, token}]`` when EVERY closed ref has an open successor naming the card (and, when
    ``superseded_by`` names PRs, each successor is one of them); else None."""
    from hermes_cli import kanban_open_pr as _open_pr
    thread_fn = thread_fn or _default_thread()
    if thread_fn is None or query_fn is None or not closed:
        return None
    named = {(r.repo.lower(), r.number) for r in _open_pr.extract_pr_refs(superseded_by)} if superseded_by else set()
    if superseded_by and not named:
        named |= {(None, int(n)) for n in re.findall(r"(?<![\w/])#(\d+)\b", superseded_by)}
    out = []
    for ref in closed:
        hit = successor(ref, task_id, thread_fn=thread_fn, query_fn=query_fn)
        if hit is None:
            return None
        repo, number, token = hit
        if named and (repo.lower(), number) not in named and (None, number) not in named:
            return None
        out.append({"closed": f"{ref.repo}#{ref.number}", "successor": f"{repo}#{number}", "token": token})
    return out


def commit_files(repo: str, sha: str) -> Optional[tuple]:
    """``(full sha, {paths})`` of one commit over REST, or None when unreadable."""
    try:
        proc = subprocess.run(["gh", "api", "--hostname", "github.com", f"repos/{repo}/commits/{sha}", "--jq",
                               "{sha: .sha, files: [.files[].filename]}"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=QUERY_TIMEOUT_SECONDS, check=False)
        if proc.returncode != 0:
            return None
        d = json.loads(proc.stdout)
        return str(d["sha"]), set(d["files"])
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, KeyError, TypeError):
        return None


def sha_close_covers(pr_key: str, paths, shas, *, thread_fn: Optional[ThreadFn] = None,
                     files_fn: Optional[Callable] = None) -> Optional[str]:
    """t_87e32ff7: the sha (one of ``shas``) the closed PR's own trusted ``CLOSED: SUPERSEDED-BY <sha>`` names, when
    that commit touched every path in ``paths`` (the files the line-match found worded differently); else None.
    The 4729 shape (a commit on main that touched 1 of the PR's 14 files) is still refused."""
    thread_fn = thread_fn or _default_thread()
    files_fn = files_fn or (None if os.environ.get("PYTEST_CURRENT_TEST") else commit_files)
    if thread_fn is None or files_fn is None:
        return None
    repo, _, n = pr_key.rpartition("#")
    thread = thread_fn(repo, int(n))
    if not isinstance(thread, dict):
        return None
    tok, rest = close_token(thread.get("comments"))
    m = re.match(r"([0-9a-f]{7,40})\b", rest.lstrip(": "))
    if tok not in FOLLOW or not m:
        return None
    named = m.group(1)
    for sha in shas:
        if not (sha.startswith(named) or named.startswith(sha)):
            continue
        got = files_fn(repo, sha)
        if got and got[0].startswith(named) and set(paths) <= got[1]:
            return got[0]
    return None


def park(conn: sqlite3.Connection, task_id: str, targets: list, *, expected_run_id: Optional[int] = None,
         summary: Optional[str] = None, metadata: Optional[dict] = None) -> bool:
    """Move the card to ``review`` with the successors as its own PRs (the open-PR route; the closer completes it
    when they merge). One ``routed_pr_superseded`` event + the route comment."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_open_pr as _open_pr
    succ = list(dict.fromkeys(t["successor"] for t in targets))
    refs = _open_pr.extract_pr_refs(*succ)
    note = _open_pr.route_note(refs) + " (" + "; ".join(f"{t['closed']} {t['token']}" for t in targets) + ")"
    meta = dict(metadata or {}, own_prs=succ, auto_routed_open_prs=succ,
                routed_pr_superseded={"targets": targets})
    with kb.write_txn(conn):
        sql = ("UPDATE tasks SET status = 'review', claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
               "block_kind = NULL WHERE id = ? AND status IN ('running', 'ready', 'blocked', 'review')")
        args: tuple = (task_id,)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            args += (int(expected_run_id),)
        if conn.execute(sql, args).rowcount != 1:
            return False
        kb._end_or_synthesize_run(conn, task_id, outcome="review_requested", status="review",
                                  summary="\n".join(x for x in (note, summary) if x), metadata=meta,
                                  synthesize=True)
        kb._append_event(conn, task_id, "routed_pr_superseded", {"targets": targets})
    kb.add_comment(conn, task_id, "kanban", _open_pr.route_comment(refs))
    return True
