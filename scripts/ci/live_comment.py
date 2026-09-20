#!/usr/bin/env python3
"""Live-updating CI review comment.

Polls the GitHub Actions API for job statuses in the CI run, assembles
the review comment from whatever results are available, and upserts it as a
PR comment. Repeats every ``--interval`` seconds until all jobs are
completed (or ``--timeout`` is reached), so the comment updates in real time
as each job finishes.

The comment is identified by the ``<!-- hermes-ci-review-bot -->`` marker
— the same one ``assemble_review_comment.py`` uses — so it replaces any
previous comment from an earlier run.

This runs from ``.github/workflows/ci-review-comment.yml``, a separate
``workflow_run`` workflow. Thus ``CI_RUN_ID`` names the CI run to report
on, not the run that contains this script. (The variable cannot be
called ``GITHUB_RUN_ID``: the Actions runner sets the ``GITHUB_*``
defaults itself and ignores an ``env:`` override, so that name would
silently resolve to the poller's own run — which stays ``in_progress``
for as long as the poller runs, deadlocking it against itself.)
The poller reports on runs that
it does not belong to. This is also how it covers a workflow that CI does
not contain: ``WATCH_WORKFLOWS`` names sibling workflows that the same
commit triggered (the Docker image build). Their jobs join the comment.

Architecture:

  - :func:`classify_jobs` (pure, testable) — takes a list of raw API job
    dicts and returns ``(completed, pending, job_urls)`` where ``completed``
    is a ``{name: result}`` dict (for :func:`assemble_review_comment.assemble`)
    and ``pending`` is a list of job names still running.

  - :func:`select_watched_runs` (pure, testable) — picks the sibling runs
    to merge in, newest attempt per workflow.

  - :func:`find_comment_id` / :func:`upsert_comment` — thin API wrappers.

  - :func:`fetch_all_review_statuses` — lists all ``review-status-*``
    artifacts on the CI run (GitHub attaches reusable-workflow
    artifacts to the caller run), downloads each, parses the
    ``review_status=`` line from ``review-status.json``, and merges into
    one array. Recomputed from source every poll cycle, so statuses
    appear as soon as each job uploads its artifact.

  - :func:`run` — the polling loop. Calls the API, classifies,
    fetches artifacts, assembles, upserts, sleeps, repeats. Before
    its final exit, it gives downstream jobs a short grace period
    to appear.

The orchestrator job names (detect, all-checks-pass, comment-live, etc.)
are excluded from the comment — they're infrastructure, not review signal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API_BASE = "https://api.github.com"

# ---------------------------------------------------------------------------
# Rate-limit governor + conditional-request cache
# ---------------------------------------------------------------------------
#
# The poller shares ONE installation token (1000 req/hr/repo) with every other
# job in the repo. Measured 2026-09-19: a single cycle charged 11 requests, and
# 12 pollers were alive concurrently (one per open PR — the concurrency group
# works; it is the PR count that scales, not duplicate pollers). At a 15 s
# interval that is ~2,640 req/hr per poller, which exhausted the installation
# limit and failed unrelated jobs sharing the token.
#
# Two levers, both measured against the live API:
#
#   1. Conditional requests. A 304 Not Modified does NOT decrement
#      X-RateLimit-Remaining (verified: two If-None-Match requests left
#      remaining unchanged at 4691, the next unconditional GET took it to
#      4690). Every poll re-reads the same URLs, so caching by ETag makes a
#      steady-state cycle cost zero.
#   2. Immutable artifacts. Artifact zips were re-downloaded every cycle.
#      An artifact id never changes content (``overwrite: true`` mints a new
#      id), so one download per id suffices.
#
# On top of that, the governor watches X-RateLimit-Remaining and stretches the
# interval when the installation budget runs low, and treats a 403/429
# rate-limit response as "back off", never as a failure.

_LOW_REMAINING_THRESHOLD = 200
_LOW_REMAINING_INTERVAL = 60

# How long the poller pauses between merge-queue membership rechecks.
#
# Merge-queue membership is NOT terminal: required checks can fail or time
# out, and a human can remove a PR from the queue. Merge-group runs never
# post a comment (the workflow's ``if`` requires ``event == 'pull_request'``),
# so if this poller exited on a queued PR, a later dequeue would leave no
# process to publish the review result at all.
#
# So a queued PR pauses instead of exiting. The pause must be much longer
# than the poll interval or the recheck costs more budget than skipping the
# poll saves: one GraphQL call per 5 minutes is ~12 req/hr per poller,
# against the ~2 charged calls per 45 s cycle it suppresses.
_MERGE_QUEUE_RECHECK_INTERVAL = 300

# Socket timeout for every API call. Without it a hung connection blocks on
# the OS default (minutes, or forever), and the poller stops updating the
# comment with no log line and no error.
_REQUEST_TIMEOUT = 30




class RateLimitError(Exception):
    """A 403/429 that the rate limiter produced, not a real failure."""

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class _ApiState:
    """Per-process API bookkeeping: ETag cache, budget, call counters."""

    def __init__(self) -> None:
        self.etags: dict[str, tuple[str, object, str]] = {}
        self.artifacts: dict[str, list[dict]] = {}
        # Last successfully parsed statuses per artifact NAME, used to carry
        # a section across a transient download failure of a newer id.
        self.artifacts_by_name: dict[str, list[dict]] = {}
        # Keyed by (repo, pr_number): the PATCH URL for a comment carries only
        # the comment id, not the PR, so a process-global id would let a
        # second PR's poller overwrite the FIRST PR's comment.
        self.comment_ids: dict[tuple[str, str], int] = {}
        self.remaining: int | None = None
        self.limit: int | None = None
        self.retry_after: float = 0.0
        self.charged = 0
        self.not_modified = 0

    def reset(self) -> None:
        self.__init__()

    def comment_id_for(self, repo: str, pr_number: str) -> int | None:
        return self.comment_ids.get((repo, str(pr_number)))

    def set_comment_id(self, repo: str, pr_number: str, comment_id: int | None) -> None:
        key = (repo, str(pr_number))
        if comment_id:
            self.comment_ids[key] = comment_id
        else:
            self.comment_ids.pop(key, None)


    def observe(self, headers) -> None:
        """Record rate-limit headers from any response (success or error).

        ``retry_after`` reflects ONLY the response being observed. It used to
        be accumulated with ``max()`` and never cleared, so the largest delay
        ever seen outlived its response: after recovering from a 20-minute
        primary limit, a later 5-second secondary limit still backed off 20
        minutes, which ``_nap`` clamps to the remaining timeout — the poller
        then ran out the clock without publishing a final status.
        """
        def _int(name: str) -> int | None:
            raw = headers.get(name) if headers is not None else None
            if raw is None:
                return None
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                return None

        remaining = _int("X-RateLimit-Remaining")
        if remaining is not None:
            self.remaining = remaining
        limit = _int("X-RateLimit-Limit")
        if limit is not None:
            self.limit = limit

        retry_after = _int("Retry-After")
        if retry_after is not None and retry_after > 0:
            self.retry_after = float(retry_after)
            return
        # Secondary/primary limits without Retry-After carry a reset epoch.
        if remaining == 0:
            reset_at = _int("X-RateLimit-Reset")
            if reset_at is not None:
                self.retry_after = max(0.0, reset_at - time.time())
                return
        # This response imposes no delay — a stored one has been served out.
        self.retry_after = 0.0


    def effective_interval(self, base: int) -> int:
        """Stretch the poll interval when the installation budget is low."""
        if self.remaining is not None and self.remaining < _LOW_REMAINING_THRESHOLD:
            return max(base, _LOW_REMAINING_INTERVAL)
        return base

    def budget_line(self) -> str:
        remaining = "?" if self.remaining is None else str(self.remaining)
        limit = "?" if self.limit is None else str(self.limit)
        return (f"rate-limit remaining={remaining}/{limit} "
                f"charged={self.charged} not-modified={self.not_modified}")


STATE = _ApiState()


def _read_retry_after(headers) -> float:
    """Retry-After from a response, without touching the REST budget state.

    Used by the GraphQL path: GraphQL has its own point budget, so its
    headers must not update the REST governor's remaining/limit — but a
    Retry-After on it is still a real instruction to back off.
    """
    if headers is None:
        return 0.0
    raw = headers.get("Retry-After")
    if raw is None:
        return 0.0
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _is_rate_limited(err: urllib.error.HTTPError) -> bool:
    """True when an HTTP error is a rate-limit response, not a real failure."""
    if err.code == 429:
        return True
    if err.code != 403:
        return False
    headers = err.headers
    if headers is None:
        return False
    if str(headers.get("X-RateLimit-Remaining", "")).strip() == "0":
        return True
    if headers.get("Retry-After"):
        return True
    body = ""
    try:
        body = err.read().decode("utf-8", "replace")
    except Exception:
        pass
    return "rate limit" in body.lower()


def _raise_if_rate_limited(err: urllib.error.HTTPError) -> None:
    """Convert a rate-limit HTTPError into a RateLimitError; else re-raise."""
    STATE.observe(err.headers)
    if _is_rate_limited(err):
        raise RateLimitError(STATE.retry_after or float(_LOW_REMAINING_INTERVAL)) from err
    raise err

# Job names that are infrastructure (this script, the gate, the detector)
# and should never appear in the review comment.
_INFRA_JOBS = frozenset({
    "detect",
    "all-checks-pass",
    "comment-pending",
    "comment-results",
    "comment-live",
    "CI review comment (pending)",
    "CI review comment (results)",
    "CI review comment (live)",
    "All required checks pass",
    "Detect affected areas",
})

# Map GitHub API conclusion values to our result strings.
_CONCLUSION_MAP = {
    "success": "success",
    "failure": "failure",
    "skipped": "skipped",
    "cancelled": "skipped",
    "neutral": "skipped",
    "timed_out": "failure",
    "action_required": "skipped",
}

def classify_jobs(api_jobs: list[dict]) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Classify raw API job dicts into completed + pending + job_urls.

    Returns ``(completed, pending, job_urls)``:

    - ``completed``: ``{job_name: result}`` where result is
      ``"success"`` / ``"failure"`` / ``"skipped"``. Only non-infra jobs
      that have finished.
    - ``pending``: list of job names still running (in_progress / queued
      / waiting). Excludes infra jobs.
    - ``job_urls``: ``{job_name: html_url}`` — direct links to each
      job's logs page, for the assembler to use in ❌ Error links.

    The API returns orchestrator-level jobs and sub-workflow jobs
    (workflow_call) in separate runs — :func:`collect_run_jobs` merges
    them. Each sub-workflow job has a ``_workflow_name`` prefix so the
    display name is ``"Workflow / job"``.
    """
    completed: dict[str, str] = {}
    pending: list[str] = []
    job_urls: dict[str, str] = {}

    for job in api_jobs:
        name = job.get("name", "unknown")
        if job.get("_workflow_name"):
            name = f"{job['_workflow_name']} / {name}"
        if name in _INFRA_JOBS:
            continue
        status = job.get("status", "")
        conclusion = job.get("conclusion", "")
        html_url = job.get("html_url", "")

        if html_url:
            job_urls[name] = html_url

        if status in ("in_progress", "queued", "waiting"):
            pending.append(name)
        elif status == "completed":
            result = _CONCLUSION_MAP.get(conclusion, "skipped")
            completed[name] = result
        # else: unknown status → skip

    return completed, pending, job_urls


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _headers(token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    base = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ci-live-comment",
    }
    if extra:
        base.update(extra)
    return base


def _conditional_get(url: str, token: str, cache_key: str):
    """GET ``url``, reusing the cached body when GitHub answers 304.

    A 304 Not Modified is free: it does not decrement the installation's
    ``X-RateLimit-Remaining``. The poller re-reads the same handful of URLs
    every cycle, so in steady state (nothing changed since the last poll)
    an entire cycle costs zero rate-limit budget.

    Returns ``(payload, link_header)``. ``payload`` is whatever the endpoint
    returns — a dict or a list; callers unwrap it (``_api_get_paginated``
    applies its own ``list_key``). On 304 the cached payload and Link header
    are replayed.
    """
    cached = STATE.etags.get(cache_key)
    extra = {"If-None-Match": cached[0]} if cached else None
    req = urllib.request.Request(url, headers=_headers(token, extra))
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            STATE.observe(resp.headers)
            STATE.charged += 1
            body = json.loads(resp.read())
            etag = resp.headers.get("ETag", "")
            link = resp.headers.get("Link", "")
            if etag:
                STATE.etags[cache_key] = (etag, body, link)
            return body, link
    except urllib.error.HTTPError as e:
        if e.code == 304 and cached is not None:
            STATE.observe(e.headers)
            STATE.not_modified += 1
            return cached[1], cached[2]
        _raise_if_rate_limited(e)
        raise


def _api_request(url: str, token: str, cache_key: str | None = None) -> dict:
    """Authenticated GitHub API GET (single page), ETag-cached."""
    data, _ = _conditional_get(url, token, cache_key or url)
    return data if isinstance(data, dict) else {}


def _api_get_paginated(
    url: str, token: str, list_key: str | None = None, cache_key: str | None = None,
) -> list:
    """Authenticated GitHub API GET with pagination, ETag-cached per page."""
    results: list = []
    key_base = cache_key or url
    page = 0
    next_url: str | None = url
    while next_url:
        page += 1
        data, link_header = _conditional_get(next_url, token, f"{key_base}#p{page}")

        if list_key:
            results.extend(data.get(list_key, []) if isinstance(data, dict) else [])
        elif isinstance(data, list):
            results.extend(data)
        else:
            # Endpoint returned a non-list without a list_key — nothing to page.
            return results

        next_url = None
        for part in link_header.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1:part.find(">")]
                break

    return results


def select_watched_runs(
    runs: list[dict], watch_names: list[str], exclude_run_id: str = "",
) -> list[dict]:
    """Pick the sibling runs whose jobs belong in the comment.

    ``runs`` is the API's run list for one commit. ``watch_names`` holds
    workflow names from ``WATCH_WORKFLOWS``. One commit can have more than
    one run of the same workflow, after a rerun or a new push. Thus this
    keeps only the newest run for each workflow name. An older attempt
    reports results that a rerun replaced.

    ``exclude_run_id`` removes the CI run itself when its name is also in
    ``watch_names``.
    """
    newest: dict[str, dict] = {}
    wanted = {n.strip() for n in watch_names if n.strip()}

    for candidate in runs:
        name = str(candidate.get("name", ""))
        if name not in wanted:
            continue
        if exclude_run_id and str(candidate.get("id", "")) == str(exclude_run_id):
            continue
        current = newest.get(name)
        if current is None or str(candidate.get("created_at", "")) > str(current.get("created_at", "")):
            newest[name] = candidate

    return list(newest.values())


def runs_all_completed(runs: list[dict]) -> bool:
    """True only when every run in the list reports ``status: completed``.

    The job list alone cannot answer "is CI done": a run that GitHub just
    created has no jobs yet, and a mid-run poll can catch the moment where
    every visible job finished but a downstream sub-workflow has not
    spawned its jobs. Both look identical to "all done" at the job level.
    The run's own ``status`` is the authoritative signal, so the poller
    must not exit while any relevant run is still ``queued`` or
    ``in_progress``. An empty list is not done — it means the poller has
    no run information at all.
    """
    return bool(runs) and all(str(r.get("status", "")) == "completed" for r in runs)


def collect_run_jobs(
    token: str, repo: str, run_id: str, watch_workflows: list[str] | None = None,
) -> tuple[list[dict], bool]:
    """Collect all jobs in the CI run + any watched sibling runs.

    Returns ``(jobs, runs_completed)``: a flat list of job dicts (same
    shape as the API returns, plus ``_workflow_name`` on jobs from a
    watched run), and whether the CI run and every selected watched run
    report ``status: completed`` (see :func:`runs_all_completed`).

    Reusable-workflow (``workflow_call``) jobs need no special handling:
    GitHub flattens them into the caller run's job list, already named
    ``\"Workflow / job\"``. Watched runs are separate top-level runs
    (the Docker image build), so their jobs are fetched per run and
    prefixed here.
    """
    owner, repo_name = repo.split("/")
    run_info = _api_request(f"{API_BASE}/repos/{owner}/{repo_name}/actions/runs/{run_id}", token)
    head_sha = run_info.get("head_sha", "")

    # CI run jobs (includes every reusable-workflow job).
    all_jobs: list[dict] = []
    orch_jobs = _api_get_paginated(
        f"{API_BASE}/repos/{owner}/{repo_name}/actions/runs/{run_id}/jobs",
        token, list_key="jobs",
    )

    # Skip workflow-call placeholder steps (they're sub-workflow triggers,
    # not review signal), but KEEP in_progress / queued jobs so the poller
    # knows they're still running.
    for job in orch_jobs:
        steps = job.get("steps") or []
        if any(s.get("name", "").startswith("Run ./.github/workflows/") for s in steps):
            continue
        all_jobs.append(job)

    if not watch_workflows or not head_sha:
        return all_jobs, runs_all_completed([run_info])

    # Watched sibling runs for the same commit. A run can be absent on the
    # first polls. Then classify_jobs() shows nothing for it.
    sibling_runs = _api_get_paginated(
        f"{API_BASE}/repos/{owner}/{repo_name}/actions/runs?head_sha={head_sha}&per_page=100",
        token, list_key="workflow_runs",
    )
    relevant_runs = [run_info]
    for watched in select_watched_runs(sibling_runs, watch_workflows, exclude_run_id=run_id):
        relevant_runs.append(watched)
        watched_jobs = _api_get_paginated(
            f"{API_BASE}/repos/{owner}/{repo_name}/actions/runs/{watched['id']}/jobs",
            token, list_key="jobs",
        )
        for job in watched_jobs:
            job["_workflow_name"] = watched.get("name", "")
            all_jobs.append(job)

    return all_jobs, runs_all_completed(relevant_runs)


def find_comment_id(token: str, repo: str, pr_number: str) -> int | None:
    """Find our existing review comment by marker prefix."""
    owner, repo_name = repo.split("/")
    comments = _api_get_paginated(
        f"{API_BASE}/repos/{owner}/{repo_name}/issues/{pr_number}/comments",
        token,
    )
    for c in comments:
        body = c.get("body", "") if isinstance(c, dict) else ""
        if body.startswith("<!-- hermes-ci-review-bot -->"):
            return c.get("id") if isinstance(c, dict) else None
    return None


def upsert_comment(
    token: str, repo: str, pr_number: str, body: str, comment_id: int | None = None,
    _allow_recreate: bool = True,
) -> int | None:
    """Create or update the review comment. Returns the comment ID.

    The comment id is cached for the life of the process: once the poller
    knows which comment is its own, every later cycle PATCHes it directly
    instead of re-listing the PR's comments (one fewer charged request per
    update, and the listing grows with PR chatter).

    If the cached comment was deleted, the PATCH answers 404. Clearing the
    cached id and deferring re-creation to "the next cycle" loses the comment
    entirely when the 404 lands on the final cycle (the quiet grace is already
    spent and the loop is about to break), so the re-create is issued
    immediately, once. ``_allow_recreate`` bounds that to a single retry.
    """
    owner, repo_name = repo.split("/")
    if comment_id is None:
        comment_id = STATE.comment_id_for(repo, pr_number)
    if comment_id is None:
        try:
            comment_id = find_comment_id(token, repo, pr_number)
        except RateLimitError:
            raise
        except Exception as e:
            # find_comment_id sits OUTSIDE the try below; an HTTPError or a
            # transport error here used to escape run()'s RateLimitError-only
            # handler and kill the poller.
            print(f"  Could not list comments ({e}) — will retry next cycle.",
                  file=sys.stderr)
            return None

    if comment_id:
        url = f"{API_BASE}/repos/{owner}/{repo_name}/issues/comments/{comment_id}"
        method = "PATCH"
    else:
        url = f"{API_BASE}/repos/{owner}/{repo_name}/issues/{pr_number}/comments"
        method = "POST"

    data = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(token, {
        "Content-Type": "application/json",
    }))
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            STATE.observe(resp.headers)
            STATE.charged += 1
            result = json.loads(resp.read())
            new_id = result.get("id")
            if new_id:
                STATE.set_comment_id(repo, pr_number, new_id)
            return new_id
    except urllib.error.HTTPError as e:
        STATE.observe(e.headers)
        if _is_rate_limited(e):
            raise RateLimitError(
                STATE.retry_after or float(_LOW_REMAINING_INTERVAL)
            ) from e
        if e.code == 404 and method == "PATCH":
            # Someone deleted our comment. Re-create it NOW: deferring to the
            # next cycle silently drops the final status when this is the last
            # one. ``_allow_recreate=False`` on the retry keeps it to one POST.
            STATE.set_comment_id(repo, pr_number, None)
            print("  Review comment was deleted — recreating it.", file=sys.stderr)
            if _allow_recreate:
                return upsert_comment(
                    token, repo, pr_number, body,
                    comment_id=0, _allow_recreate=False,
                )
            return None
        print(f"  API error {e.code}: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        # URLError / socket.timeout / ssl.SSLError. HTTPError is a SUBCLASS of
        # URLError, not the reverse, so the handler above does not cover these
        # — and an escape here killed the poller and froze the comment for the
        # rest of the run. Comment posting is best-effort; retry next cycle.
        print(f"  Network error updating the comment ({e}) — will retry.",
              file=sys.stderr)
        return None



# ---------------------------------------------------------------------------
# Artifact fetching (dynamic review-status artifacts)
# ---------------------------------------------------------------------------

# Prefix for all review-status artifacts uploaded by status-producing jobs.
# Each job uploads a ``review-status-<name>`` artifact containing a
# ``review-status.json`` file in GITHUB_OUTPUT format:
#   review_status=<json array of {source, results: [...]} objects>
_REVIEW_STATUS_ARTIFACT_PREFIX = "review-status-"

# Where artifact zips are staged. Module-level so tests can redirect it
# instead of writing into the real shared /tmp.
_ARTIFACT_TEMP_BASE = Path("/tmp/review-status-artifacts")


def _list_artifacts(token: str, repo: str, run_id: str) -> list[dict]:
    """List artifacts for a given run (paginated)."""
    owner, repo_name = repo.split("/")
    return _api_get_paginated(
        f"{API_BASE}/repos/{owner}/{repo_name}/actions/runs/{run_id}/artifacts",
        token, list_key="artifacts",
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that never follows — used to capture the Location."""

    def redirect_request(self, *args, **kwargs):
        return None


def _download_artifact(
    token: str, repo: str, artifact: dict, dest_dir: Path,
) -> Path | None:
    """Download a single artifact zip via the API and extract it.

    Returns the path to ``review-status.json`` inside the extracted dir,
    or ``None`` if the download or extraction failed.
    """
    owner, repo_name = repo.split("/")
    archive_download_url = artifact.get("archive_download_url", "")
    if not archive_download_url:
        return None

    # The archive_download_url is an API URL that 302s to a signed blob
    # URL. Hop 1 authenticates to the API; hop 2 follows the redirect
    # WITHOUT the Authorization header — the blob rejects a request that
    # carries both a SAS token and an Authorization header (401).
    opener = urllib.request.build_opener(_NoRedirectHandler)
    location = ""
    try:
        resp = opener.open(urllib.request.Request(
            archive_download_url, headers=_headers(token),
        ), timeout=30)
        STATE.observe(resp.headers)
        STATE.charged += 1
    except urllib.error.HTTPError as e:
        STATE.observe(e.headers)
        if e.code == 302:
            STATE.charged += 1
            location = e.headers.get("Location", "")
        elif _is_rate_limited(e):
            raise RateLimitError(
                STATE.retry_after or float(_LOW_REMAINING_INTERVAL)
            ) from e
    except Exception:
        location = ""
    if not location:
        return None

    # Download/extract paths are keyed by the immutable artifact ID, never by
    # name: an ``overwrite: true`` upload (every ``review-status-*``) mints a
    # NEW id under the SAME name, so name-keyed paths let a stale extract from
    # the previous id be served for the newer one.
    slug = f"{artifact['name']}-{artifact.get('id', 'noid')}"
    zip_path = dest_dir / f"{slug}.zip"
    try:
        # No auth headers here; further redirects are safe to follow.
        with urllib.request.urlopen(
            urllib.request.Request(location, headers={"User-Agent": "ci-live-comment"}),
            timeout=60,
        ) as resp:
            zip_path.write_bytes(resp.read())
    except Exception:
        return None

    extract_dir = dest_dir / slug
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if any(".." in name or name.startswith("/") for name in zf.namelist()):
                return None
            zf.extractall(extract_dir)
    except Exception:
        return None

    status_file = extract_dir / "review-status.json"
    return status_file if status_file.exists() else None


def _parse_status_file(status_file: Path) -> list[dict]:
    """Parse a review-status.json file in GITHUB_OUTPUT format."""
    try:
        content = status_file.read_text(encoding="utf-8").strip()
        if content.startswith("review_status="):
            content = content[len("review_status="):]
        statuses = json.loads(content)
        if isinstance(statuses, list):
            return statuses
    except (json.JSONDecodeError, OSError):
        pass
    return []


def fetch_all_review_statuses(
    token: str, repo: str, run_id: str,
) -> list[dict] | None:
    """Fetch and merge all review-status artifacts from the run.

    Lists artifacts with the ``review-status-`` prefix on the orchestrator
    run, downloads each, parses the ``review-status.json`` inside, and
    merges into a single flat array. GitHub attaches artifacts uploaded by
    reusable workflow jobs to the caller run, so one listing covers every
    status-producing job.

    An artifact's content is immutable for a given artifact id — re-uploading
    with ``overwrite: true`` mints a NEW id — so each id is downloaded at most
    once per process and the parsed statuses are cached. Before this, every
    poll cycle re-downloaded every artifact (measured: 4 extra charged
    requests per cycle on a typical run, plus the listing).

    Returns the merged list of ``{source, results: [...]}`` objects, or
    ``None`` when the artifact LISTING itself failed. ``None`` means
    "unknown", not "none exist": returning an empty list there made the
    caller republish the comment with every review section deleted.
    Artifacts that don't exist yet or fail to parse are silently skipped.
    """
    all_statuses: list[dict] = []
    temp_base = _ARTIFACT_TEMP_BASE

    try:
        artifacts = _list_artifacts(token, repo, run_id)
    except RateLimitError:
        raise
    except Exception as e:
        # A transient listing failure must not silently republish the comment
        # with every review section gone. Signal "unknown", not "empty", and
        # let the caller keep the last known statuses.
        print(f"  Could not list artifacts ({e}) — keeping the previous "
              f"review statuses.", file=sys.stderr)
        return None

    rs_artifacts = [
        a for a in artifacts
        if a.get("name", "").startswith(_REVIEW_STATUS_ARTIFACT_PREFIX)
    ]
    if not rs_artifacts:
        return all_statuses

    run_dl_dir = temp_base / str(run_id)
    run_dl_dir.mkdir(parents=True, exist_ok=True)

    for artifact in rs_artifacts:
        key = str(artifact.get("id", artifact.get("name", "")))
        cached = STATE.artifacts.get(key)
        if cached is not None:
            all_statuses.extend(cached)
            continue
        status_file = _download_artifact(token, repo, artifact, run_dl_dir)
        if status_file is None:
            # Not yet available, or a transient download failure. Leave it
            # uncached so a later poll retries — but if a PREVIOUS cycle
            # already parsed an artifact with this same source name, keep
            # that result rather than silently dropping the section from
            # the comment (an `overwrite: true` upload mints a new id, so a
            # blip on the new zip would otherwise delete a published
            # section, permanently if it lands on the final cycle).
            carried = STATE.artifacts_by_name.get(artifact.get("name", ""))
            if carried:
                all_statuses.extend(carried)
            continue
        statuses = _parse_status_file(status_file)
        STATE.artifacts[key] = statuses
        STATE.artifacts_by_name[artifact.get("name", "")] = statuses
        all_statuses.extend(statuses)

    # A re-run can leave several non-expired artifacts with the same name,
    # each carrying the same source — dedupe by source so the comment
    # doesn't render duplicate sections.
    seen: set[str] = set()
    deduped: list[dict] = []
    for status in all_statuses:
        src = status.get("source", "")
        if src in seen:
            continue
        if src:
            seen.add(src)
        deduped.append(status)
    return deduped


# ---------------------------------------------------------------------------
# Comment assembly
# ---------------------------------------------------------------------------


def _import_assembler():
    """Import assemble_review_comment.py from the same directory."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import assemble_review_comment as asm
    return asm


def build_comment_body(
    asm_mod,
    completed: dict[str, str],
    pending: list[str],
    run_url: str,
    job_urls: dict[str, str],
    review_statuses_json: str,
    commit_info: str = "",
    waiting: bool = False,
) -> str:
    """Assemble the comment body from current job states + static inputs."""
    needs_json = json.dumps(completed) if completed else ""

    return asm_mod.assemble(
        needs_json=needs_json,
        run_url=run_url,
        job_urls=job_urls,
        review_statuses_json=review_statuses_json,
        pending_jobs=pending if pending else None,
        commit_info=commit_info,
        waiting=waiting,
    )


def _commit_info_for_state(commit_info: str, pending: bool) -> str:
    """Use past tense in the final comment after every CI job completes."""
    if pending:
        return commit_info
    return commit_info.replace("<sub>running on ", "<sub>ran on ", 1)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


def run(
    token: str,
    repo: str,
    run_id: str,
    pr_number: str,
    run_url: str,
    commit_info: str = "",
    interval: int = 45,
    timeout: int = 1800,
    dry_run: bool = False,
    watch_workflows: list[str] | None = None,
    max_wall_seconds: int | None = None,
) -> int:
    """Poll for job statuses and update the PR comment until all done.

    Always returns 0. The poller reports on the CI run from a different run.
    Thus a failed CI job is not a failure of this job. The CI run has its
    own gate, which reports that. Comment posting is best-effort — including
    when the shared installation token runs out of rate-limit budget: the
    poller backs off and keeps going, and never fails the job over it.
    """
    asm = _import_assembler()
    start = time.time()
    wall_start = start
    last_body = ""
    quiet_grace_used = False
    prev_completed: dict[str, str] = {}
    prev_pending: list[str] = []
    prev_artifact_count = 0
    prev_artifact_statuses: list[dict] = []

    # -inf so the first cycle always checks; then throttled to one check per
    # _MERGE_QUEUE_RECHECK_INTERVAL so the recheck does not become a new
    # per-cycle charge against the budget this whole module defends.
    last_queue_check = float("-inf")


    def _nap(seconds: float) -> None:
        """Sleep, but never past the timeout."""
        remaining_time = timeout - (time.time() - start)
        time.sleep(max(0.0, min(seconds, remaining_time)))

    def _pause_for_queue(seconds: float) -> None:
        """Sleep while merge-queued, WITHOUT spending the poll lifetime.

        A queued pause is not polling work — it is waiting for the PR to
        come back. Counting it against ``timeout`` means a PR queued for the
        full 50-minute window exits the poller, and a later dequeue then has
        no reporter at all: exactly the failure the pause exists to prevent
        (merge-group runs never post a comment). So advance ``start`` by the
        paused duration, which leaves the poller its full budget of ACTIVE
        polling once the PR returns.

        It is bounded by ``max_wall_seconds`` regardless: the Actions job has
        its own hard ``timeout-minutes``, and being SIGKILLed mid-pause
        publishes nothing at all. Stopping cleanly before that lets the
        final status be written.
        """
        nonlocal start
        before = time.time()
        if max_wall_seconds is not None:
            room = max_wall_seconds - (before - wall_start)
            seconds = min(seconds, max(0.0, room))
        time.sleep(max(0.0, seconds))
        start += time.time() - before



    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            print(f"Timeout ({timeout}s) reached — stopping poll.", file=sys.stderr)
            break
        if max_wall_seconds is not None and (time.time() - wall_start) >= max_wall_seconds:
            # The Actions job's own timeout-minutes is about to SIGKILL us.
            # Exit cleanly instead: a killed process publishes nothing.
            print(f"Wall-clock budget ({max_wall_seconds}s) reached — stopping "
                  f"poll before the job is killed.", file=sys.stderr)
            break

        # A PR that has entered the merge queue is already approved and
        # merging; its live comment is read by nobody, while its poller keeps
        # spending the shared installation budget the merge-queue CI runs
        # need. But queue membership is NOT terminal — a required check can
        # fail, or a human can remove the PR — and merge-group runs never post
        # a comment, so exiting here would leave a dequeued PR with no
        # reporter at all. Pause and recheck instead.
        #
        # Throttled: one GraphQL call per _MERGE_QUEUE_RECHECK_INTERVAL, not
        # one per cycle, or the check itself becomes the cost it avoids.
        if pr_number and not dry_run and (
            time.time() - last_queue_check >= _MERGE_QUEUE_RECHECK_INTERVAL
        ):
            last_queue_check = time.time()
            try:
                queued = pr_is_in_merge_queue(token, repo, pr_number)
            except RateLimitError as e:
                backoff = max(e.retry_after, float(_LOW_REMAINING_INTERVAL))
                print(f"  Rate limited on the merge-queue check — backing off "
                      f"{backoff:.0f}s ({STATE.budget_line()})", file=sys.stderr)
                _nap(backoff)
                continue
            if queued:
                print(f"  PR #{pr_number} is in the merge queue — pausing "
                      f"{_MERGE_QUEUE_RECHECK_INTERVAL}s "
                      f"(will resume if it is dequeued).")
                _pause_for_queue(_MERGE_QUEUE_RECHECK_INTERVAL)
                continue

        cycle_start_charged = STATE.charged

        try:
            jobs, runs_completed = collect_run_jobs(token, repo, run_id, watch_workflows)
        except RateLimitError as e:
            backoff = max(e.retry_after, float(_LOW_REMAINING_INTERVAL))
            print(f"  Rate limited while collecting jobs — backing off {backoff:.0f}s "
                  f"({STATE.budget_line()})", file=sys.stderr)
            _nap(backoff)
            continue
        except Exception as e:
            print(f"  API error collecting jobs: {e}", file=sys.stderr)
            _nap(STATE.effective_interval(interval))
            continue

        completed, pending, job_urls = classify_jobs(jobs)
        total = len(completed) + len(pending)
        infra_count = len(jobs) - total
        print(f"  [{elapsed:.0f}s] fetched {len(jobs)} jobs from API "
              f"({infra_count} infra filtered) → {len(completed)} completed, "
              f"{len(pending)} pending ({total} review jobs)")

        # Log transitions since last poll.
        new_completed = {k: v for k, v in completed.items() if k not in prev_completed}
        new_pending = [j for j in pending if j not in prev_pending]
        gone_pending = [j for j in prev_pending if j not in pending and j not in completed]
        if new_completed:
            parts = [f"{name}={result}" for name, result in new_completed.items()]
            print(f"  → {len(new_completed)} job(s) newly completed: {', '.join(parts)}")
        if new_pending:
            print(f"  → {len(new_pending)} job(s) newly appeared: {', '.join(new_pending)}")
        if gone_pending:
            print(f"  → {len(gone_pending)} job(s) disappeared from pending: {', '.join(gone_pending)}")

        # Dynamically fetch all review-status artifacts from the run.
        try:
            artifact_statuses = fetch_all_review_statuses(token, repo, run_id)
        except RateLimitError as e:
            backoff = max(e.retry_after, float(_LOW_REMAINING_INTERVAL))
            print(f"  Rate limited while fetching artifacts — backing off "
                  f"{backoff:.0f}s ({STATE.budget_line()})", file=sys.stderr)
            _nap(backoff)
            continue
        if artifact_statuses is None:
            # The listing failed transiently. Reuse the last known statuses so
            # the comment is not republished with every review section gone.
            artifact_statuses = prev_artifact_statuses
        prev_artifact_statuses = artifact_statuses
        artifact_count_changed = len(artifact_statuses) != prev_artifact_count

        if artifact_count_changed:
            print(f"  Found {len(artifact_statuses)} review status entries from artifacts "
                  f"(was {prev_artifact_count} last poll)")
        prev_artifact_count = len(artifact_statuses)

        merged_json = json.dumps(artifact_statuses) if artifact_statuses else ""
        # The run status is authoritative for "done": an empty job list on
        # a run that is still queued/in_progress means GitHub has not
        # spawned the jobs yet, not that everything passed.
        all_done = not pending and runs_completed
        current_commit_info = _commit_info_for_state(commit_info, pending=not all_done)

        body = build_comment_body(
            asm, completed, pending, run_url, job_urls,
            merged_json,
            current_commit_info,
            waiting=not runs_completed,
        )

        if body != last_body:
            change_reasons = []
            if new_completed:
                change_reasons.append(f"{len(new_completed)} new completion(s)")
            if new_pending:
                change_reasons.append(f"{len(new_pending)} new pending job(s)")
            if gone_pending:
                change_reasons.append(f"{len(gone_pending)} job(s) left pending")
            if artifact_count_changed:
                change_reasons.append("artifact statuses updated")
            if not change_reasons:
                change_reasons.append("initial post")
            reason = "; ".join(change_reasons)

            if dry_run:
                print(f"  Comment body changed ({reason}) — DRY RUN:")
                print("--- DRY RUN — comment body ---")
                print(body)
                print("--- END ---")
                last_body = body
            else:
                try:
                    cid = upsert_comment(token, repo, pr_number, body)
                except RateLimitError as e:
                    backoff = max(e.retry_after, float(_LOW_REMAINING_INTERVAL))
                    print(f"  Rate limited while updating the comment — backing off "
                          f"{backoff:.0f}s ({STATE.budget_line()})", file=sys.stderr)
                    _nap(backoff)
                    continue
                if cid:
                    print(f"  Updated comment {cid} ({reason})")
                    last_body = body
                else:
                    # Leave last_body unchanged so the next cycle retries the
                    # same body instead of treating the failure as posted.
                    print(f"  Failed to update comment ({reason}, will retry)", file=sys.stderr)
        else:
            if pending:
                print(f"  No change since last poll. Still waiting on: {', '.join(pending)}")
            else:
                print("  No change since last poll.")

        # One budget line per cycle, so a rate-limit incident is diagnosable
        # from the job log alone.
        print(f"  {STATE.budget_line()} "
              f"(this cycle charged {STATE.charged - cycle_start_charged})")

        prev_completed = completed
        prev_pending = pending

        if all_done and not quiet_grace_used:
            quiet_grace_used = True
            print("  No jobs pending and runs report completed — "
                  "waiting 10s for downstream jobs to appear.")
            _nap(10)
            continue

        if all_done:
            failed = [name for name, result in completed.items() if result == "failure"]
            if failed:
                print(f"  All jobs done, {len(failed)} failed: {', '.join(failed)}")
            else:
                print("  All jobs completed — done.")
            break

        if not pending:
            print("  No visible jobs pending, but a run is still queued or "
                  "in progress — waiting for its jobs to appear.")

        quiet_grace_used = False
        sleep_for = STATE.effective_interval(interval)
        if sleep_for != interval:
            print(f"  Low rate-limit budget — stretching poll interval "
                  f"{interval}s → {sleep_for}s")
        _nap(sleep_for)

    return 0


def parse_watch_workflows(raw: str) -> list[str]:
    """Parse the ``WATCH_WORKFLOWS`` value into workflow names.

    One name per line. Not comma-separated: a workflow name can contain a
    comma ("Docker Build, Test, and Publish").
    """
    return [name.strip() for name in raw.splitlines() if name.strip()]


def resolve_pr_number(token: str, repo: str, head_sha: str) -> str:
    """Find the PR number for a commit when the event payload has none.

    ``workflow_run.pull_requests`` is empty for some runs. The poller has no
    comment to post without a number.
    """
    if not head_sha:
        return ""
    owner, repo_name = repo.split("/")
    try:
        results = _api_get_paginated(
            f"{API_BASE}/repos/{owner}/{repo_name}/commits/{head_sha}/pulls",
            token,
        )
    except Exception as e:
        print(f"  API error resolving PR number: {e}", file=sys.stderr)
        return ""
    for item in results:
        if isinstance(item, dict) and item.get("state") == "open":
            return str(item.get("number", ""))
    return ""


def pr_is_in_merge_queue(token: str, repo: str, pr_number: str) -> bool:
    """True when the PR has already entered the merge queue.

    A queued PR is approved and merging; a live-updating review comment on it
    is read by nobody, and its poller competes for the same installation
    rate-limit budget as the merge-queue CI runs that actually gate the merge.
    ``merge_group`` runs never get a comment at all (the workflow's ``if``
    requires ``event == 'pull_request'``), so skipping queued PRs costs no
    signal.

    ``isInMergeQueue`` is GraphQL-only — the REST pull object does not expose
    it. On any error this returns False: failing open keeps the comment
    working, which is the safer default for a best-effort reporter.
    """
    if not pr_number:
        return False
    owner, repo_name = repo.split("/")
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "pullRequest(number:$number){isInMergeQueue}}}"
    )
    payload = json.dumps({
        "query": query,
        "variables": {"owner": owner, "name": repo_name, "number": int(pr_number)},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/graphql", data=payload, method="POST",
        headers=_headers(token, {"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            # NOT STATE.observe(): GraphQL has its OWN budget (5000 points,
            # separate from the REST installation limit). Feeding its
            # headers into the REST governor overwrites a low REST remaining
            # with a healthy GraphQL one and silently disarms the backoff —
            # measured: REST 50/1000 (interval 60s) became 4900/5000
            # (interval 45s) after one membership check.
            STATE.charged += 1
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # A rate-limited membership check must back the poller off, not be
        # silently read as "not queued".
        if _is_rate_limited(e):
            print("  Merge-queue check was rate limited — backing off.",
                  file=sys.stderr)
            raise RateLimitError(
                _read_retry_after(e.headers) or float(_LOW_REMAINING_INTERVAL)
            ) from e
        print(f"  Could not check merge-queue membership ({e}) — polling anyway.",
              file=sys.stderr)
        return False
    except (ValueError, OSError) as e:
        print(f"  Could not check merge-queue membership ({e}) — polling anyway.",
              file=sys.stderr)
        return False

    try:
        return bool(data["data"]["repository"]["pullRequest"]["isInMergeQueue"])
    except (KeyError, TypeError):
        return False


def build_parser() -> argparse.ArgumentParser:
    """The poller's CLI parser.

    Factored out of :func:`main` so tests can assert against the REAL
    defaults. A test that rebuilds a look-alike parser with the expected
    default hardcoded asserts only that argparse works.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=45,
                        help="Seconds between polls (default: 45). The poller "
                             "shares one installation rate-limit budget with "
                             "every other job in the repo, and one poller runs "
                             "per open PR.")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Max seconds to poll before giving up (default: 1800).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print comment body instead of posting to PR.")
    parser.add_argument("--max-wall-seconds", type=int, default=None,
                        help="Hard wall-clock ceiling, INCLUDING merge-queue "
                             "pauses. Set it below the workflow job's "
                             "timeout-minutes so the poller stops cleanly "
                             "instead of being SIGKILLed mid-pause (a killed "
                             "process publishes nothing).")
    return parser


def main() -> int:
    args = build_parser().parse_args()


    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("CI_RUN_ID", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    run_url = os.environ.get("RUN_URL", "")

    # Sibling workflows to merge into the comment, one name per line. Their
    # runs are separate from the CI run, so the poller resolves them by name.
    watch_workflows = parse_watch_workflows(os.environ.get("WATCH_WORKFLOWS", ""))

    if not args.dry_run:
        if not token:
            print("GITHUB_TOKEN is required", file=sys.stderr)
            return 1
        if not repo:
            print("GITHUB_REPOSITORY is required", file=sys.stderr)
            return 1
        if not run_id:
            print("CI_RUN_ID is required", file=sys.stderr)
            return 1

    # Build commit info line from env vars (set by ci-review-comment.yml).
    commit_sha = os.environ.get("COMMIT_SHA", "")
    commit_msg = os.environ.get("COMMIT_MESSAGE", "")

    if not pr_number and not args.dry_run:
        pr_number = resolve_pr_number(token, repo, commit_sha)
        if not pr_number:
            print("No PR number found — nothing to comment on.", file=sys.stderr)
            return 0
        print(f"Resolved PR #{pr_number} from commit {commit_sha[:7]}")

    # Merge-queue membership is handled INSIDE run(): it is not terminal, so
    # a queued PR pauses and rechecks rather than exiting (exiting would leave
    # a later dequeue with no reporter — merge-group runs never comment).

    commit_url = os.environ.get("COMMIT_URL", "")
    if not commit_url and commit_sha and pr_number:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        commit_url = f"{server}/{repo}/pull/{pr_number}/commits/{commit_sha}"

    commit_info = ""
    if commit_sha:
        short_sha = commit_sha[:7]
        if commit_msg:
            # Truncate commit message to first line, max 60 chars.
            first_line = commit_msg.split("\n")[0][:60]
            if commit_url:
                commit_info = f"<sub>running on [{short_sha}]({commit_url}) — {first_line}</sub>"
            else:
                commit_info = f"<sub>running on {short_sha} — {first_line}</sub>"
        elif commit_url:
            commit_info = f"<sub>running on [{short_sha}]({commit_url})</sub>"
        else:
            commit_info = f"<sub>running on {short_sha}</sub>"

    return run(
        token=token,
        repo=repo,
        run_id=run_id,
        pr_number=pr_number,
        run_url=run_url,
        commit_info=commit_info,
        interval=args.interval,
        timeout=args.timeout,
        dry_run=args.dry_run,
        watch_workflows=watch_workflows,
        max_wall_seconds=args.max_wall_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
