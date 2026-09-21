"""Verify remote implementation evidence without requiring a workspace clone."""
import json
import re
import subprocess
from urllib.parse import urlsplit

from hermes_cli import kanban_db as kb

_SHA = r"[0-9a-f]{7,40}"
_SLUG = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
_PR = re.compile(rf"(?<![\w/])({_SLUG})#([1-9][0-9]*)\b")
_PR_URL = re.compile(rf"https://github\.com/({_SLUG})/pull/([1-9][0-9]*)\b")
_REPO_URL = re.compile(r"(?:https?|ssh|git)://[^\s\"'<>]+|git@github\.com:[^\s\"'<>]+")
HINT = "use --survivor-pr <owner/repo#N> or --survivor-ref <repo-url>#<sha>; remote verification is required"


def _safe_url(url):
    # Never execute remote helpers, accept local scratch as durable, or persist
    # credential-bearing URLs from handoffs. SSH usernames are not credentials.
    parsed = urlsplit(url)
    if url.startswith("git@github.com:"):
        return bool(re.fullmatch(rf"git@github.com:{_SLUG}(?:\.git)?", url))
    return (parsed.scheme in {"https", "http", "ssh", "git"} and bool(parsed.hostname)
            and not parsed.password and not parsed.query and not parsed.fragment
            and not (parsed.username and parsed.scheme != "ssh"))


def _query(args):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        return result.stdout.decode() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def verify_ref(claim):
    url, sep, sha = claim.rpartition("#")
    if not sep or not re.fullmatch(_SHA, sha) or not _safe_url(url):
        return None
    output = _query(["git", "ls-remote", "--heads", "--tags", "--", url])
    if output is None:
        return None
    matches = {}
    for line in output.splitlines():
        oid, _, ref = line.partition("\t")
        if re.fullmatch(r"[0-9a-f]{40}", oid) and oid.startswith(sha):
            matches[oid] = ref
    if len(matches) != 1:
        return None  # unknown or ambiguous abbreviation
    oid, ref = next(iter(matches.items()))
    return {"remote": url, "branch": ref, "sha": oid, "external": True}


def verify_pr(claim, shas=()):
    match = _PR.fullmatch(claim) or _PR_URL.fullmatch(claim)
    if not match:
        return None
    slug, number = match.groups()
    output = _query(["gh", "pr", "view", number, "--repo", slug,
                     "--json", "state,headRefOid,mergeCommit"])
    if output is None:
        return None
    try:
        view = json.loads(output)
        state, head = view["state"], view["headRefOid"]
        merge = (view.get("mergeCommit") or {}).get("oid")
        oid = merge if state == "MERGED" else head
        if state not in {"OPEN", "MERGED"} or not re.fullmatch(r"[0-9a-f]{40}", oid or ""):
            return None
        if shas and not any(value and value.startswith(sha) for value in (head, merge) for sha in shas):
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return {"remote": f"https://github.com/{slug}.git", "branch": f"refs/pull/{number}/head",
            "sha": oid, "pr": f"{slug}#{number}", "state": state, "external": True}


def discover(conn, task_id, metadata, evidence, urls):
    """Text supplies candidates, never verification. Bound remote lookups to six.

    Do not mine task bodies: their example/parent PRs are not deliverables.
    Compare handoff SHA claims to the PR head or squash merge when supplied.
    """
    sources = ["\n".join([*filter(None, evidence), json.dumps(metadata or {})])]
    task = kb.get_task(conn, task_id)
    if task.result:
        sources.append(task.result)
    sources.extend("\n".join(filter(None, row)) for row in conn.execute(
        "SELECT summary, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC", (task_id,)))
    sources.extend(c.body for c in reversed(kb.list_comments(conn, task_id)))
    seen = set()
    for text in sources:
        shas = re.findall(rf"(?<!\w)({_SHA})(?!\w)", text)
        prs = [*(_PR_URL.findall(text)), *(_PR.findall(text))]
        remote_urls = list(urls)
        remote_urls.extend(m.group().rstrip(".,)").split("#")[0] for m in _REPO_URL.finditer(text))
        slugs = []
        for url in remote_urls:
            match = re.fullmatch(rf"(?:https://github.com/|git@github.com:)({_SLUG})", url)
            if match:
                slugs.append(match[1].removesuffix(".git"))
        slugs = list(dict.fromkeys(slugs))
        if len(slugs) == 1:
            prs.extend((slugs[0], n) for n in re.findall(r"\bPR\s*#([1-9][0-9]*)\b", text, re.I))
        candidates = [("pr", f"{slug}#{n}") for slug, n in prs]
        candidates.extend(("ref", f"{url}#{sha}") for url in remote_urls for sha in shas)
        for kind, claim in candidates:
            key = (kind, claim, tuple(shas))
            if key in seen:
                continue
            if len(seen) >= 6:
                return None
            seen.add(key)
            verified = verify_pr(claim, shas) if kind == "pr" else verify_ref(claim)
            if verified:
                return verified
    return None
