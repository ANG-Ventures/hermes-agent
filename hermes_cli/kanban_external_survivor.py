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


def redact(claim):
    """Strip URL userinfo and query strings before a claim is echoed or persisted.

    An unverifiable ``--survivor-ref`` is rejected precisely because it may
    carry a token; the rejection must not copy that token into errors, the
    hold reason, the event log, or the logfile.
    """
    claim = re.sub(r"(?<=://)[^/@\s]+@", "", claim)
    return re.sub(r"[?][^#\s]*", "", claim)


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


def verify_ref(claim, *, mined_for=None):
    """Resolve a ``<url>#<sha>`` claim against the remote's branch and tag tips.

    An operator flag is authority for the *identity* of the claim, never for
    its relevance: ``mined_for`` is set on both the explicit and the mined path
    so a SHA that is merely a tip somewhere is not accepted as THIS card's
    work. Only the ``--survivor-unbound`` override passes ``mined_for=None``.
    A mined ref is additionally only a hint: every SHA in a handoff is
    cross-producted with every remote URL, and "branched from <sha>" names the
    base, not the deliverable -- so a mined ref must sit on a branch that
    names the task.
    """
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
    if mined_for and mined_for not in ref:
        return None
    return {"remote": url, "branch": ref, "sha": oid, "external": True}


def verify_pr(claim, shas=(), *, mined_for=None, corroborate=("headRefName",)):
    """Resolve a PR claim against GitHub.

    Existence is not relevance. An OPEN/MERGED PR proves only that somebody
    shipped something somewhere, so ``mined_for`` is applied on the explicit
    operator/worker path too: the PR must corroborate the card by naming it.
    Only the ``--survivor-unbound`` operator override passes ``mined_for=None``.

    ``corroborate`` names the fields that may carry that naming, and the
    default is deliberately the narrow one the mined path has always used --
    the branch. The explicit path widens it to title and body, because those
    are the PR's own claim about which card it implements and an operator
    typing the number has already vouched for the PR's identity. Text on the
    *card* is what cannot be trusted, and that is mined separately in
    :func:`discover`; widening the mined path here would let a PR body that
    merely mentions a card id verify itself.

    A PR mined from handoff text is additionally only a hint: it must be
    corroborated by a claimed SHA that is the PR head or squash merge, or --
    with no SHA claimed -- by the same naming test. A bare ``owner/repo#N``
    mention proves nothing about THIS card's work.
    """
    match = _PR.fullmatch(claim) or _PR_URL.fullmatch(claim)
    if not match:
        return None
    slug, number = match.groups()
    output = _query(["gh", "pr", "view", number, "--repo", slug,
                     "--json", "state,headRefOid,headRefName,mergeCommit,title,body"])
    if output is None:
        return None
    try:
        view = json.loads(output)
        state, head = view["state"], view["headRefOid"]
        merge = (view.get("mergeCommit") or {}).get("oid")
        oid = merge if state == "MERGED" else head
        if state not in {"OPEN", "MERGED"} or not re.fullmatch(r"[0-9a-f]{40}", oid or ""):
            return None
        if shas:
            if not any(value and value.startswith(sha) for value in (head, merge) for sha in shas):
                return None
        elif mined_for and not any(
            mined_for in str(view.get(field) or "") for field in corroborate
        ):
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return {"remote": f"https://github.com/{slug}.git", "branch": f"refs/pull/{number}/head",
            "sha": oid, "pr": f"{slug}#{number}", "state": state, "external": True}


def discover(conn, task_id, metadata, evidence, urls):
    """Text supplies candidates, never verification. Bound remote lookups to six.

    Only HANDOFF text is mined: the completion evidence and metadata, the task
    result, and run summaries. Task bodies cite example/parent PRs and comments
    are discussion that routinely cites other cards' PRs; neither is a
    deliverable. A mined PR must still be corroborated (see ``verify_pr``).
    """
    sources = ["\n".join([*filter(None, evidence), json.dumps(metadata or {})])]
    task = kb.get_task(conn, task_id)
    if task.result:
        sources.append(task.result)
    sources.extend("\n".join(filter(None, row)) for row in conn.execute(
        "SELECT summary, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC", (task_id,)))
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
            verified = (verify_pr(claim, shas, mined_for=task_id) if kind == "pr"
                        else verify_ref(claim, mined_for=task_id))
            if verified:
                return verified
    return None
