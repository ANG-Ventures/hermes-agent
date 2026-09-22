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


class RemoteUnavailable(Exception):
    """The remote did not answer. This says NOTHING about the claim.

    ``None`` from a ``verify_*`` means "the remote answered and the claim does
    not hold up". A subprocess that exits non-zero, times out, or dies means
    the question was never asked -- an ordinary rate limit, auth hiccup or
    network blip. Collapsing the two lets a transient fault be reported, and
    durably persisted, as a conclusion about relevance the kernel never
    established (kanban card t_de2e348e, Argus round 3). Callers that only need
    a candidate (:func:`discover`) treat this as "no", callers that state a
    reason must say "could not verify" instead.
    """


def _query(args):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RemoteUnavailable(f"{args[0]} did not answer") from exc
    if result.returncode != 0:
        raise RemoteUnavailable(f"{args[0]} exited {result.returncode}")
    try:
        return result.stdout.decode()
    except UnicodeError as exc:
        raise RemoteUnavailable(f"{args[0]} returned undecodable output") from exc


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

    ``None`` means the remote answered and the claim does not hold up; a
    remote that did not answer raises :class:`RemoteUnavailable` instead, so a
    caller never reports a blip as a statement about relevance. A SHA that is
    the tip of more than one ref the binding cannot narrow to exactly one is
    also ``None``: ``branch`` is recorded as this survivor's provenance, so
    picking one of several would publish a recovery-index entry that is a
    guess.
    """
    url, sep, sha = claim.rpartition("#")
    if not sep or not re.fullmatch(_SHA, sha) or not _safe_url(url):
        return None
    output = _query(["git", "ls-remote", "--heads", "--tags", "--", url])
    matches = {}
    for line in output.splitlines():
        oid, _, ref = line.partition("\t")
        if re.fullmatch(r"[0-9a-f]{40}", oid) and oid.startswith(sha):
            # One OID routinely carries SEVERAL tips: an ordinary fast-forward
            # merge leaves the topic branch alive beside `main`, and a release
            # tag points at the same commit. Keying this dict by oid alone kept
            # only the LAST line, so `len(matches)` was still 1 and the binding
            # below was evaluated against whichever refname `ls-remote` emitted
            # last -- refname order puts `refs/heads/main` and `refs/tags/*`
            # after `refs/heads/kanban/<task>-fix`, so a claim's verdict, and
            # the `branch` recorded as its provenance, depended on emission
            # order. Keep every tip and decide over all of them.
            matches.setdefault(oid, []).append(ref)
    if len(matches) != 1:
        return None  # unknown or ambiguous abbreviation
    oid, tips = next(iter(matches.items()))
    if mined_for:
        # The binding is the question being asked, so ask it of every tip, not
        # of one drawn by emission order. A commit does not stop naming the
        # card because it is also reachable as `main` or as a tag.
        tips = [ref for ref in tips if mined_for in ref]
    if len(tips) != 1:
        # Still ambiguous after the binding narrowed it: REFUSE rather than
        # pick. `branch` is persisted as this survivor's provenance and read by
        # reclamation; choosing one of several candidates would write a
        # recovery-index entry this module cannot stand behind -- the same harm
        # as stamping one operator claim onto several missing repositories
        # (`kanban_survivor.preserve`). A caller who knows which ref carries the
        # work can name it with `--survivor-pr`.
        return None
    ref = tips[0]
    verified = {"remote": url, "branch": ref, "sha": oid, "external": True}
    return dict(verified, corroborated_by="branch") if mined_for else verified


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

    ``corroborated_by`` reports WHICH signal answered, because the signals are
    not equally strong and the caller must be able to tell them apart. A head
    branch or a claimed SHA ties the PR's *content* to the card; a substring in
    the title or body is only a mention, and an umbrella changelog or a
    dependency note ("does not address t_...") satisfies it. Callers treat the
    weak ones as an unbound claim (see ``kanban_survivor._verified_explicit``).

    A PR mined from handoff text is additionally only a hint: it must be
    corroborated by a claimed SHA that is the PR head or squash merge, or --
    with no SHA claimed -- by the same naming test. A bare ``owner/repo#N``
    mention proves nothing about THIS card's work.

    ``None`` means the remote answered and the claim does not hold up; a
    remote that did not answer raises :class:`RemoteUnavailable` instead, so a
    caller never reports a blip as a statement about relevance.
    """
    match = _PR.fullmatch(claim) or _PR_URL.fullmatch(claim)
    if not match:
        return None
    slug, number = match.groups()
    output = _query(["gh", "pr", "view", number, "--repo", slug,
                     "--json", "state,headRefOid,headRefName,mergeCommit,title,body"])
    corroborated_by = None
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
            corroborated_by = "sha"
        elif mined_for:
            corroborated_by = next(
                (field for field in corroborate if mined_for in str(view.get(field) or "")),
                None,
            )
            if corroborated_by is None:
                return None
            if corroborated_by == "headRefName":
                corroborated_by = "branch"
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    verified = {"remote": f"https://github.com/{slug}.git", "branch": f"refs/pull/{number}/head",
                "sha": oid, "pr": f"{slug}#{number}", "state": state, "external": True}
    return dict(verified, corroborated_by=corroborated_by) if corroborated_by else verified


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
            try:
                verified = (verify_pr(claim, shas, mined_for=task_id) if kind == "pr"
                            else verify_ref(claim, mined_for=task_id))
            except RemoteUnavailable:
                # Mining only looks for a candidate; an unanswered remote is
                # indistinguishable from "not this one" HERE, because discover
                # states no reason. It is the reason-stating callers that must
                # keep the two apart.
                continue
            if verified:
                return verified
    return None
