"""Rate-limit governor tests for scripts/ci/live_comment.py.

The poller shares ONE installation token (1000 req/hr/repo) with every other
job in the repo, and one poller runs per open PR. Measured 2026-09-19: a
cycle charged 11 requests and 12 pollers ran concurrently, which exhausted
the installation limit and failed unrelated jobs sharing the token (OSV
SARIF upload, E2E evidence publishing) — one of those failures ejected a PR
from the merge queue.

These tests lock the three defences:

  1. Conditional requests — a 304 replays the cached body and charges
     nothing (verified against the live API: two If-None-Match requests
     left X-RateLimit-Remaining unchanged).
  2. Artifact downloads are deduped by immutable artifact id.
  3. A 403/429 rate-limit response backs the poller off instead of
     propagating as a job failure.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "live_comment.py"
_spec = importlib.util.spec_from_file_location("live_comment_rl", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load live_comment.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["live_comment_rl"] = _mod
_spec.loader.exec_module(_mod)


@pytest.fixture(autouse=True)
def _fresh_state():
    _mod.STATE.reset()
    yield
    _mod.STATE.reset()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail any test that reaches the real GitHub API.

    Moving the merge-queue check into the poll loop made every
    ``run(dry_run=False)`` test call ``pr_is_in_merge_queue`` for real —
    measured 3 live requests to ``api.github.com/graphql``. They passed
    anyway because that helper fails open, so the suite was quietly
    network-dependent AND spending the very budget it exists to defend.

    Tests that want network behaviour monkeypatch ``urllib.request.urlopen``
    themselves, which overrides this fixture for that test.
    """
    def _blocked(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        raise AssertionError(
            f"test made a REAL network request to {url}; stub it instead"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)



class _Resp:
    """Minimal urlopen context-manager stand-in."""

    def __init__(self, body, headers):
        self._body = json.dumps(body).encode()
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, headers, body=b"{}"):
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", headers, io.BytesIO(body)
    )


def _raise(code, headers, body=b"{}"):
    """Raise like real urlopen does — it never *returns* an HTTPError."""
    raise _http_error(code, headers, body)


# ─── conditional requests ─────────────────────────────────────────────


def test_304_replays_cached_body_and_charges_nothing(monkeypatch):
    """The whole point: an unchanged resource must cost zero budget."""
    calls = []

    def fake_urlopen(req, *a, **k):
        calls.append(dict(req.header_items()))
        if len(calls) == 1:
            return _Resp({"status": "in_progress"},
                         {"ETag": 'W/"abc"', "X-RateLimit-Remaining": "900",
                          "X-RateLimit-Limit": "1000", "Link": ""})
        _raise(304, {"X-RateLimit-Remaining": "900"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    first = _mod._api_request("https://api.github.com/run", "tok")
    assert first == {"status": "in_progress"}
    assert _mod.STATE.charged == 1

    second = _mod._api_request("https://api.github.com/run", "tok")
    assert second == {"status": "in_progress"}, "cached body must be replayed"
    assert _mod.STATE.charged == 1, "a 304 must not be counted as charged"
    assert _mod.STATE.not_modified == 1

    # The second request must actually have carried the validator.
    assert any(k.lower() == "if-none-match" for k in calls[1]), \
        "second request sent no If-None-Match, so GitHub could never answer 304"
    assert calls[1]["If-none-match"] == 'W/"abc"'


def test_paginated_get_caches_each_page_separately(monkeypatch):
    """Page 2 must not reuse page 1's ETag, or it replays the wrong body."""
    page1 = "https://api.github.com/jobs"
    page2 = "https://api.github.com/jobs?page=2"
    seen = []

    def fake_urlopen(req, *a, **k):
        seen.append(req.full_url)
        if req.full_url == page1:
            return _Resp({"jobs": [{"name": "a"}]},
                         {"ETag": 'W/"p1"', "Link": f'<{page2}>; rel="next"'})
        return _Resp({"jobs": [{"name": "b"}]}, {"ETag": 'W/"p2"', "Link": ""})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = _mod._api_get_paginated(page1, "tok", list_key="jobs")
    assert [j["name"] for j in got] == ["a", "b"]
    assert set(_mod.STATE.etags) == {f"{page1}#p1", f"{page1}#p2"}


def test_cached_pagination_replays_the_link_header(monkeypatch):
    """A 304 on page 1 must still lead the caller to page 2."""
    page1 = "https://api.github.com/jobs"
    page2 = "https://api.github.com/jobs?page=2"

    state = {"first": True}

    def fake_urlopen(req, *a, **k):
        if state["first"]:
            if req.full_url == page1:
                return _Resp({"jobs": [{"name": "a"}]},
                             {"ETag": 'W/"p1"', "Link": f'<{page2}>; rel="next"'})
            return _Resp({"jobs": [{"name": "b"}]}, {"ETag": 'W/"p2"', "Link": ""})
        _raise(304, {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert len(_mod._api_get_paginated(page1, "tok", list_key="jobs")) == 2

    state["first"] = False
    again = _mod._api_get_paginated(page1, "tok", list_key="jobs")
    assert [j["name"] for j in again] == ["a", "b"], \
        "a 304 on page 1 dropped the Link header, so page 2 was never fetched"
    assert _mod.STATE.charged == 2, "the replay must charge nothing"


# ─── artifact dedupe ──────────────────────────────────────────────────


def test_artifacts_are_downloaded_once_per_id(monkeypatch, tmp_path):
    """Artifact content is immutable per id; re-downloading is pure waste."""
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    artifacts = [
        {"id": 1, "name": "review-status-lint", "archive_download_url": "u1"},
        {"id": 2, "name": "review-status-osv", "archive_download_url": "u2"},
    ]
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: artifacts)

    downloads = []

    def fake_download(token, repo, artifact, dest):
        downloads.append(artifact["id"])
        return Path(f"/nonexistent/{artifact['id']}.json")

    monkeypatch.setattr(_mod, "_download_artifact", fake_download)
    monkeypatch.setattr(
        _mod, "_parse_status_file",
        lambda p: [{"source": f"s{p.stem}", "results": []}],
    )

    first = _mod.fetch_all_review_statuses("tok", "o/r", "77")
    assert len(first) == 2
    assert downloads == [1, 2]

    second = _mod.fetch_all_review_statuses("tok", "o/r", "77")
    assert second == first, "cached statuses must match the fresh ones"
    assert downloads == [1, 2], "second cycle re-downloaded immutable artifacts"


def test_failed_artifact_download_is_not_cached(monkeypatch, tmp_path):
    """An artifact that is not uploaded yet must be retried next cycle."""
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    artifacts = [{"id": 9, "name": "review-status-x", "archive_download_url": "u"}]
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: artifacts)

    attempts = {"n": 0}

    def fake_download(token, repo, artifact, dest):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None
        return Path("/nonexistent/x.json")

    monkeypatch.setattr(_mod, "_download_artifact", fake_download)
    monkeypatch.setattr(_mod, "_parse_status_file",
                        lambda p: [{"source": "x", "results": []}])

    assert _mod.fetch_all_review_statuses("tok", "o/r", "77") == []
    assert len(_mod.fetch_all_review_statuses("tok", "o/r", "77")) == 1
    assert attempts["n"] == 2, "a failed download must not poison the cache"


# ─── rate-limit classification + backoff ──────────────────────────────


@pytest.mark.parametrize("code,headers", [
    (429, {}),
    (403, {"X-RateLimit-Remaining": "0"}),
    (403, {"Retry-After": "120"}),
])
def test_rate_limit_responses_are_classified(code, headers):
    assert _mod._is_rate_limited(_http_error(code, headers))


@pytest.mark.parametrize("code,headers,body", [
    (403, {"X-RateLimit-Remaining": "742"}, b'{"message":"Resource not accessible"}'),
    (404, {}, b"{}"),
    (422, {}, b"{}"),
])
def test_real_failures_are_not_mistaken_for_rate_limits(code, headers, body):
    """A permissions 403 is a real bug; swallowing it would hide it."""
    assert not _mod._is_rate_limited(_http_error(code, headers, body))


def test_rate_limited_get_raises_ratelimiterror_not_httperror(monkeypatch):
    def fake_urlopen(req, *a, **k):
        _raise(403, {"X-RateLimit-Remaining": "0", "Retry-After": "90"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(_mod.RateLimitError) as exc:
        _mod._api_request("https://api.github.com/run", "tok")
    assert exc.value.retry_after == 90


def test_non_rate_limit_http_error_still_propagates(monkeypatch):
    def fake_urlopen(req, *a, **k):
        _raise(500, {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        _mod._api_request("https://api.github.com/run", "tok")


def test_interval_stretches_when_budget_is_low():
    _mod.STATE.remaining = 900
    assert _mod.STATE.effective_interval(45) == 45
    _mod.STATE.remaining = 199
    assert _mod.STATE.effective_interval(45) >= 60, \
        "below the threshold the poller must slow to at least 60s"
    # A caller that already polls slower than the floor keeps its interval.
    _mod.STATE.remaining = 10
    assert _mod.STATE.effective_interval(120) == 120


def test_reset_epoch_is_used_when_retry_after_is_absent(monkeypatch):
    """Primary rate limits send X-RateLimit-Reset, not Retry-After."""
    monkeypatch.setattr(_mod.time, "time", lambda: 1000.0)
    _mod.STATE.observe({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1300"})
    assert _mod.STATE.retry_after == pytest.approx(300.0)


def test_rate_limited_poll_loop_backs_off_and_returns_success(monkeypatch):
    """A rate limit must never fail the job — the CI gate reports CI, not this."""
    calls = {"n": 0}

    def fake_collect(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _mod.RateLimitError(60.0)
        return [{"name": "Python tests", "status": "completed",
                 "conclusion": "success", "html_url": "u"}], True

    slept: list[float] = []
    monkeypatch.setattr(_mod, "collect_run_jobs", fake_collect)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    monkeypatch.setattr(_mod.time, "sleep", lambda s: slept.append(s))

    rc = _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
                  run_url="u", interval=45, timeout=3000, dry_run=True)

    assert rc == 0, "a rate-limit storm must not fail the poller job"
    assert slept[:2] == [60.0, 60.0], f"expected two 60s backoffs, got {slept[:2]}"
    # 3 = two backed-off attempts + the successful one. The loop then takes one
    # extra quiet-grace pass before exiting, so allow >= 3.
    assert calls["n"] >= 3, "the poller must resume polling after backing off"


def test_upsert_failure_does_not_mark_the_body_as_posted(monkeypatch):
    """A failed post must be retried, not silently treated as delivered.

    The body is identical on every cycle here, so the ONLY thing that can
    trigger a second upsert is ``last_body`` still being empty after the
    first attempt failed.
    """
    jobs = [{"name": "Python tests", "status": "in_progress", "html_url": "u"}]
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (jobs, False))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    attempts = {"n": 0}

    def fake_upsert(token, repo, pr, body, comment_id=None):
        attempts["n"] += 1
        return None if attempts["n"] == 1 else 4242

    monkeypatch.setattr(_mod, "upsert_comment", fake_upsert)

    # Advance the clock only when the loop sleeps, so exactly 3 cycles run.
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + 50.0))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
             run_url="u", interval=45, timeout=120, dry_run=False)
    assert attempts["n"] >= 2, "the failed comment body was never retried"


# ─── merge-queue skip ─────────────────────────────────────────────────


def test_merge_queue_pr_is_detected(monkeypatch):
    def fake_urlopen(req, *a, **k):
        assert req.get_method() == "POST"
        assert b"isInMergeQueue" in req.data
        return _Resp(
            {"data": {"repository": {"pullRequest": {"isInMergeQueue": True}}}},
            {"X-RateLimit-Remaining": "500"},
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _mod.pr_is_in_merge_queue("tok", "o/r", "731") is True


def test_non_queued_pr_still_polls(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **k: _Resp(
        {"data": {"repository": {"pullRequest": {"isInMergeQueue": False}}}}, {}))
    assert _mod.pr_is_in_merge_queue("tok", "o/r", "723") is False


def test_merge_queue_check_fails_open(monkeypatch):
    """A broken check must not silently disable the review comment."""
    def boom(req, *a, **k):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert _mod.pr_is_in_merge_queue("tok", "o/r", "723") is False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, *a, **k: _Resp({"errors": [{"message": "no"}]}, {}))
    assert _mod.pr_is_in_merge_queue("tok", "o/r", "723") is False


def test_empty_pr_number_is_not_queued():
    assert _mod.pr_is_in_merge_queue("tok", "o/r", "") is False


# ─── workflow wiring ──────────────────────────────────────────────────


def test_workflow_passes_the_slower_interval():
    """The 15s interval is what exhausted the installation budget."""
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/ci-review-comment.yml").read_text(encoding="utf-8")
    assert "--interval 45" in text
    assert "--interval 15" not in text


def test_default_interval_matches_the_workflow():
    """A drifting default silently restores the storm for any other caller.

    Asserted against the PRODUCTION parser, not a look-alike built here: a
    test-local ``add_argument(default=45)`` proves only that argparse works
    (FleetReview P2, tautological test).
    """
    parser = _mod.build_parser()
    assert parser.parse_args([]).interval == 45
    import inspect
    sig = inspect.signature(_mod.run)
    assert sig.parameters["interval"].default == 45


def test_production_parser_is_the_one_main_uses():
    """build_parser() must be main()'s parser, or the assertion above drifts."""
    import inspect
    src = inspect.getsource(_mod.main)
    assert "build_parser()" in src, \
        "main() builds its own parser, so build_parser() proves nothing"


# ─── P1: the comment cache must not cross PRs ─────────────────────────


def test_comment_id_cache_is_keyed_by_repo_and_pr(monkeypatch):
    """A process-global id lets PR B overwrite PR A's comment.

    The PATCH URL carries only the comment id, never the PR, so a second
    run() in the same interpreter PATCHed the first PR's comment — silently
    replacing the review shown on the wrong PR and leaving the second
    without one.
    """
    posted: list[tuple[str, str]] = []

    def fake_urlopen(req, *a, **k):
        posted.append((req.get_method(), req.full_url))
        # The POST endpoint encodes the PR; return a distinct id per PR.
        cid = 900 if "/issues/5/" in req.full_url else 901
        return _Resp({"id": cid}, {"X-RateLimit-Remaining": "900"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    _mod.upsert_comment("tok", "o/r", "5", "body-A", comment_id=0)
    _mod.upsert_comment("tok", "o/r", "6", "body-B", comment_id=None)

    assert _mod.STATE.comment_id_for("o/r", "5") == 900
    assert _mod.STATE.comment_id_for("o/r", "6") == 901, \
        "PR 6 reused PR 5's cached comment id and overwrote the wrong comment"
    # PR 6 must not have PATCHed PR 5's comment URL.
    assert not any(m == "PATCH" and "/issues/comments/900" in u
                   for m, u in posted), \
        f"PR 6 PATCHed PR 5's comment: {posted}"


# ─── P1: transport errors must not kill the poller ────────────────────


def test_network_error_during_upsert_does_not_crash(monkeypatch):
    """HTTPError is a SUBCLASS of URLError, not the reverse.

    A plain URLError / socket timeout escaped the HTTPError-only handler
    and run()'s RateLimitError-only handler, killing the poller and
    freezing the review comment for the rest of the CI run.
    """
    def boom(req, *a, **k):
        raise urllib.error.URLError("connection reset by peer")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    _mod.STATE.set_comment_id("o/r", "5", 111)
    assert _mod.upsert_comment("tok", "o/r", "5", "body") is None, \
        "a transport error must degrade to a retry, not an exception"


def test_find_comment_id_failure_does_not_crash_upsert(monkeypatch):
    """find_comment_id sits OUTSIDE upsert_comment's try block."""
    def boom(req, *a, **k):
        _raise(500, {})

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert _mod.upsert_comment("tok", "o/r", "5", "body") is None


def test_rate_limit_from_find_comment_id_still_propagates(monkeypatch):
    """Swallowing everything would hide the backoff signal."""
    def limited(req, *a, **k):
        _raise(403, {"X-RateLimit-Remaining": "0", "Retry-After": "77"})

    monkeypatch.setattr(urllib.request, "urlopen", limited)
    with pytest.raises(_mod.RateLimitError):
        _mod.upsert_comment("tok", "o/r", "5", "body")


def test_api_calls_carry_a_socket_timeout(monkeypatch):
    """A hung connection must not stall the poller silently."""
    seen: list[object] = []

    def fake_urlopen(req, *a, **k):
        seen.append(k.get("timeout"))
        return _Resp({"id": 1}, {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _mod._api_request("https://api.github.com/run", "tok")
    _mod.upsert_comment("tok", "o/r", "5", "body", comment_id=0)
    assert seen and all(t is not None for t in seen), \
        f"an API call was made with no socket timeout: {seen}"


def test_graphql_headers_do_not_pollute_the_rest_budget(monkeypatch):
    """GraphQL has its OWN 5000-point budget, separate from REST's 1000.

    Feeding its headers into the REST governor overwrote a low REST
    remaining with a healthy GraphQL one and silently disarmed the
    interval backoff.
    """
    _mod.STATE.observe({"X-RateLimit-Remaining": "50", "X-RateLimit-Limit": "1000"})
    assert _mod.STATE.effective_interval(45) >= 60

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **k: _Resp(
        {"data": {"repository": {"pullRequest": {"isInMergeQueue": False}}}},
        {"X-RateLimit-Remaining": "4900", "X-RateLimit-Limit": "5000"}))
    _mod.pr_is_in_merge_queue("tok", "o/r", "5")

    assert _mod.STATE.remaining == 50, (
        f"the GraphQL budget overwrote the REST one (remaining={_mod.STATE.remaining})"
    )
    assert _mod.STATE.effective_interval(45) >= 60, \
        "the low-budget backoff was disarmed by a GraphQL response"


def test_rate_limited_merge_queue_check_backs_off(monkeypatch):
    """A 403 here must not be read as 'not queued' and charge on regardless."""
    def limited(req, *a, **k):
        _raise(403, {"X-RateLimit-Remaining": "0", "Retry-After": "88"})

    monkeypatch.setattr(urllib.request, "urlopen", limited)
    with pytest.raises(_mod.RateLimitError) as exc:
        _mod.pr_is_in_merge_queue("tok", "o/r", "5")
    assert exc.value.retry_after == 88


def test_rate_limited_merge_queue_check_does_not_kill_the_loop(monkeypatch):
    """run() must survive it — the poller never fails on a rate limit."""
    calls = {"n": 0}

    def fake_queued(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _mod.RateLimitError(60.0)
        return False

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", fake_queued)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "completed",
          "conclusion": "success", "html_url": "u"}], True))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    posted: list[str] = []
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: posted.append(body) or 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    rc = _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
                  run_url="u", interval=45, timeout=3000, dry_run=False)
    assert rc == 0
    assert posted, "a rate-limited queue check stopped the poller publishing"


def test_transient_artifact_listing_failure_keeps_the_last_statuses(monkeypatch):
    """A listing blip must not republish the comment with sections deleted.

    Returning an empty list on failure is indistinguishable from "no
    artifacts exist", so the comment was rewritten with every review
    section gone and then restored on the next cycle.
    """
    bodies: list[str] = []
    cycle = {"n": 0}

    def fake_fetch(*a, **k):
        cycle["n"] += 1
        if cycle["n"] == 1:
            return [{"source": "lint", "results": [{"x": 1}]}]
        if cycle["n"] == 2:
            return None                         # transient listing failure
        return [{"source": "lint", "results": [{"x": 1}]}]

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", fake_fetch)
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}], False))
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: bodies.append(body) or 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
             run_url="u", interval=45, timeout=150, dry_run=False)

    assert cycle["n"] >= 3, "the test did not reach the post-failure cycle"
    # Exactly one body: the failure cycle must not have produced a different
    # (stripped) one, and therefore no re-post.
    assert len(set(bodies)) == 1, (
        "the comment was rewritten when the artifact listing failed — review "
        f"sections were dropped and restored ({len(set(bodies))} distinct bodies)"
    )


def test_real_absence_of_artifacts_is_still_empty(monkeypatch):
    """None means 'unknown'; an empty list must still mean 'none exist'."""
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [])
    assert _mod.fetch_all_review_statuses("tok", "o/r", "77") == []


def test_listing_failure_returns_none_not_empty(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr(_mod, "_list_artifacts", boom)
    assert _mod.fetch_all_review_statuses("tok", "o/r", "77") is None, \
        "a listing failure is indistinguishable from 'no artifacts exist'"


def test_failed_download_of_a_new_id_keeps_the_published_section(monkeypatch, tmp_path):
    """`overwrite: true` mints a new id under the same NAME.

    A transient download failure on the new zip must not delete the section
    the old id already published — permanently, if it lands on the final
    cycle.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    state = {"id": 1, "fail": False}

    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": state["id"], "name": "review-status-lint",
         "archive_download_url": "u"}])
    monkeypatch.setattr(
        _mod, "_download_artifact",
        lambda t, r, artifact, dest: None if state["fail"] else Path("/x.json"))
    monkeypatch.setattr(_mod, "_parse_status_file",
                        lambda p: [{"source": "lint", "results": [{"ok": 1}]}])

    first = _mod.fetch_all_review_statuses("tok", "o/r", "77")
    assert len(first) == 1

    # An overwrite mints a new id; its download blips.
    state["id"] = 2
    state["fail"] = True
    second = _mod.fetch_all_review_statuses("tok", "o/r", "77")
    assert second == first, (
        "a transient download failure on a re-uploaded artifact deleted the "
        f"already-published review section ({second})"
    )


def test_queue_pause_is_bounded_by_the_wall_clock_budget(monkeypatch):
    """The Actions job has its own timeout-minutes; a SIGKILL publishes nothing.

    Excluding queued time from ``timeout`` is right, but an unbounded pause
    lets the job be killed mid-pause. _pause_for_queue must clamp to the
    remaining wall-clock room, not just rely on the loop guard catching it
    one pause later.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: True)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: ([], False))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    monkeypatch.setattr(_mod, "upsert_comment", lambda *a, **k: 1)

    slept: list[float] = []
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep", lambda s: (
        slept.append(s), clock.__setitem__("t", clock["t"] + max(s, 1.0))))

    budget = 700          # not a multiple of the 300s recheck interval
    rc = _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
                  run_url="u", interval=45, timeout=3000, dry_run=False,
                  max_wall_seconds=budget)

    assert rc == 0
    # No overrun tolerance: _pause_for_queue and _nap both clamp to the
    # remaining wall-clock room, so the poller must stop at or before the
    # budget. Allowing a 60s overrun here (as an earlier version did) let
    # the poller exceed the Actions deadline while the test stayed green.
    assert clock["t"] <= budget, (
        f"a permanently-queued PR ran {clock['t']:.0f}s past its "
        f"{budget}s wall-clock budget — the job would be SIGKILLed mid-pause"
    )
    # The LAST pause must have been clamped to the leftover room (100s),
    # not taken as a full 300s interval that overshoots the budget.
    assert slept, "the poller never paused"
    assert slept[-1] < _mod._MERGE_QUEUE_RECHECK_INTERVAL, (
        f"the final pause was not clamped to the remaining wall-clock room "
        f"(slept {slept[-1]}s of a {budget}s budget; pauses={slept})"
    )


def test_workflow_sets_a_wall_clock_budget_below_the_job_timeout():
    """A budget at or above timeout-minutes would never fire."""
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/ci-review-comment.yml").read_text(encoding="utf-8")
    job_timeout_min = int(
        re.search(r"^\s*timeout-minutes:\s*(\d+)", text, re.M).group(1))
    budget = int(re.search(r"--max-wall-seconds (\d+)", text).group(1))
    assert budget < job_timeout_min * 60, (
        f"--max-wall-seconds {budget} is not below the job's "
        f"timeout-minutes ({job_timeout_min} = {job_timeout_min * 60}s), so "
        "the poller is still killed before it can stop cleanly"
    )


# ─── P1: merge-queue membership is not terminal ───────────────────────


def test_queued_pr_is_rechecked_not_abandoned(monkeypatch):
    """A queued PR can be ejected; exiting loses reporting forever.

    Merge-group runs deliberately never post a comment, so if this poller
    exits while the PR sits in the queue and the PR is then dequeued (a
    required check fails, or a human removes it), no process remains to
    publish the result. The poller must PAUSE and recheck, not exit.
    """
    memberships = [True, True, False]
    checks = {"n": 0}

    def fake_queued(token, repo, pr):
        checks["n"] += 1
        return memberships[min(checks["n"] - 1, len(memberships) - 1)]

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", fake_queued)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "completed",
          "conclusion": "success", "html_url": "u"}], True))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    posted: list[str] = []
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: posted.append(body) or 7)

    slept: list[float] = []
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep", lambda s: (
        slept.append(s), clock.__setitem__("t", clock["t"] + max(s, 1.0))))

    rc = _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
                  run_url="u", interval=45, timeout=3000, dry_run=False)

    assert rc == 0
    assert checks["n"] >= 2, \
        "membership was checked once and the poller gave up; a dequeue is unreported"
    assert posted, \
        "the PR left the merge queue but no comment was ever published"


def test_queued_pause_does_not_consume_the_poll_lifetime(monkeypatch):
    """A long queue stay must not exhaust the timeout before the dequeue.

    The pause exists so a dequeued PR still gets reported. If queued time
    counted against ``timeout``, a PR queued for the whole window would exit
    the poller — and merge-group runs never comment, so nothing would ever
    publish its result. That is the same bug the pause replaced.
    """
    # Queued far longer than the whole 1800s timeout, then released.
    queued = {"n": 0}

    def fake_queued(token, repo, pr):
        queued["n"] += 1
        return queued["n"] <= 20        # 20 * 300s = 6000s >> timeout

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", fake_queued)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "completed",
          "conclusion": "success", "html_url": "u"}], True))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    posted: list[str] = []
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: posted.append(body) or 7)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    rc = _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
                  run_url="u", interval=45, timeout=1800, dry_run=False)

    assert rc == 0
    assert clock["t"] > 1800, "the test did not actually outlast the timeout"
    assert posted, (
        f"the PR sat queued for {clock['t']:.0f}s (timeout 1800s), was then "
        "dequeued, and the poller had already exited — its result is "
        "permanently unreported"
    )



def test_queued_pr_does_not_poll_the_jobs_api(monkeypatch):
    """Pausing must be cheap: one GraphQL check per pause, no job polling."""
    # Queued for a bounded number of checks, then the run ends. (Queued time
    # no longer consumes the timeout, so a permanently-queued PR would pause
    # forever by design.)
    queued = {"n": 0}

    def fake_queued(*a, **k):
        queued["n"] += 1
        return queued["n"] <= 5

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", fake_queued)

    collects = {"n": 0}

    def fake_collect(*a, **k):
        collects["n"] += 1
        return [{"name": "Python tests", "status": "completed",
                 "conclusion": "success", "html_url": "u"}], True

    monkeypatch.setattr(_mod, "collect_run_jobs", fake_collect)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    comments_while_queued = {"n": 0}
    monkeypatch.setattr(
        _mod, "upsert_comment",
        lambda *a, **k: comments_while_queued.__setitem__("n", comments_while_queued["n"] + 1) or 1,
    )

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    rc = _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
                  run_url="u", interval=45, timeout=300, dry_run=False)

    assert rc == 0
    # 5 queued pauses happened before the PR was released; during those the
    # poller must not have touched the jobs API at all.
    assert queued["n"] >= 6, "the PR was never actually paused while queued"
    assert collects["n"] <= 2, (
        f"a queued PR spent job-API budget it was supposed to be conserving "
        f"({collects['n']} collect calls across {queued['n']} membership checks)"
    )


def test_merge_queue_check_is_throttled_not_per_cycle(monkeypatch):
    """The recheck must not become a new per-cycle charge.

    Moving the membership check from startup into the loop is only correct
    if it is throttled: an unthrottled GraphQL call every 45 s cycle is
    ~80 req/hr per poller added to the very budget this work defends.
    """
    checks = {"n": 0}

    def fake_queued(token, repo, pr):
        checks["n"] += 1
        return False

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", fake_queued)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}], False))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    monkeypatch.setattr(_mod, "upsert_comment", lambda *a, **k: 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    # Floor the advance at 1s: _nap clamps its sleep to the remaining timeout,
    # which is exactly 0 at the deadline, and the loop guard is ``>``. A real
    # clock always advances; a fake one that does not would spin.
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
             run_url="u", interval=45, timeout=600, dry_run=False)

    cycles = 600 // 45
    expected_max = 600 // _mod._MERGE_QUEUE_RECHECK_INTERVAL + 1
    assert checks["n"] <= expected_max, (
        f"membership was checked {checks['n']} times over ~{cycles} cycles; "
        f"the throttle allows at most {expected_max}. An unthrottled check "
        "adds a charged GraphQL call to every cycle."
    )
    assert checks["n"] >= 1, "membership was never checked at all"


def test_merge_queue_recheck_interval_is_not_the_poll_interval():
    """Rechecking every 45s would cost more budget than it saves."""
    assert _mod._MERGE_QUEUE_RECHECK_INTERVAL >= 120


# ─── P1: stale backoff must not outlive its response ──────────────────


def test_retry_after_does_not_retain_the_previous_maximum():
    """A long limit followed by a short one must use the SHORT delay.

    ``max(self.retry_after, new)`` kept the largest delay ever seen and
    never cleared it, so after recovering from a 20-minute limit a later
    5-second limit still slept 20 minutes — long enough for _nap to consume
    the whole remaining timeout and exit without the final status.
    """
    _mod.STATE.observe({"Retry-After": "1200"})
    assert _mod.STATE.retry_after == pytest.approx(1200.0)

    # Recovery: a normal response carries no Retry-After.
    _mod.STATE.observe({"X-RateLimit-Remaining": "900", "X-RateLimit-Limit": "1000"})
    assert _mod.STATE.retry_after == 0.0, \
        "a successful response must clear the stored backoff"

    _mod.STATE.observe({"Retry-After": "5"})
    assert _mod.STATE.retry_after == pytest.approx(5.0), \
        "the stale 1200s delay resurfaced on an unrelated short rate limit"


def test_reset_epoch_backoff_is_also_replaced_not_maximised(monkeypatch):
    monkeypatch.setattr(_mod.time, "time", lambda: 1000.0)
    _mod.STATE.observe({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000"})
    assert _mod.STATE.retry_after == pytest.approx(1000.0)
    _mod.STATE.observe({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"})
    assert _mod.STATE.retry_after == pytest.approx(30.0), \
        "an earlier, longer reset epoch overrode the current response"


def test_stale_backoff_is_not_replayed_by_the_poll_loop(monkeypatch):
    """End-to-end: a SHORT late limit must sleep short, not replay the long one.

    Under ``max(self.retry_after, ...)`` the stored 900s delay outlived its
    response, so a later 5s limit still slept 900s. ``_nap`` clamps that to
    the remaining timeout, which is how the poller ran out the clock without
    ever publishing a final status.
    """
    calls = {"n": 0}

    def fake_collect(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            _mod.STATE.observe({"Retry-After": "900"})       # long limit
            raise _mod.RateLimitError(_mod.STATE.retry_after)
        if calls["n"] == 2:
            _mod.STATE.observe({"X-RateLimit-Remaining": "900",
                                "X-RateLimit-Limit": "1000"})  # recovered
            return [{"name": "Python tests", "status": "in_progress",
                     "html_url": "u"}], False
        if calls["n"] == 3:
            _mod.STATE.observe({"Retry-After": "5"})          # brief limit
            raise _mod.RateLimitError(_mod.STATE.retry_after)
        return [{"name": "Python tests", "status": "completed",
                 "conclusion": "success", "html_url": "u"}], True

    monkeypatch.setattr(_mod, "collect_run_jobs", fake_collect)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)
    posted: list[str] = []
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: posted.append(body) or 1)

    slept: list[float] = []
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
             run_url="u", interval=45, timeout=100000, dry_run=False)

    assert len(posted) >= 2, "the final completed status was never published"
    # slept[0] is the long limit's own backoff and is legitimate. Every sleep
    # AFTER it must reflect the current response — the 5s limit's backoff is
    # floored at _LOW_REMAINING_INTERVAL, never the stale 900s.
    assert slept[0] == pytest.approx(900.0)
    later = slept[1:]
    assert later, "the poller never slept again after the long limit"
    assert max(later) <= float(_mod._LOW_REMAINING_INTERVAL), (
        "the stale 900s delay was replayed after recovery "
        f"(sleeps after the long limit: {later})"
    )



# ─── P1: a deleted comment must be recreated immediately ──────────────


def test_deleted_comment_is_recreated_in_the_same_call(monkeypatch):
    """A 404 on the final cycle previously lost the comment permanently.

    Clearing the cached id and deferring to "next cycle" is a no-op when
    the quiet grace has already been consumed and the loop is about to
    break.
    """
    requests: list[tuple[str, str]] = []

    def fake_urlopen(req, *a, **k):
        requests.append((req.get_method(), req.full_url))
        if req.get_method() == "PATCH":
            _raise(404, {"X-RateLimit-Remaining": "900"},
                   b'{"message":"Not Found"}')
        return _Resp({"id": 555}, {"X-RateLimit-Remaining": "899"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    _mod.STATE.set_comment_id("o/r", "5", 111)
    new_id = _mod.upsert_comment("tok", "o/r", "5", "body")

    assert new_id == 555, \
        "a deleted comment was not recreated; the final status was lost"
    assert [m for m, _ in requests] == ["PATCH", "POST"], \
        f"expected a PATCH then an immediate POST, got {requests}"
    assert _mod.STATE.comment_id_for("o/r", "5") == 555


def test_recreate_after_404_happens_only_once(monkeypatch):
    """A POST that also 404s must not recurse."""
    methods: list[str] = []

    def fake_urlopen(req, *a, **k):
        methods.append(req.get_method())
        _raise(404, {}, b'{"message":"Not Found"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _mod.STATE.set_comment_id("o/r", "5", 111)
    assert _mod.upsert_comment("tok", "o/r", "5", "body") is None
    assert methods == ["PATCH", "POST"], f"unbounded retry: {methods}"


def test_non_404_patch_error_does_not_trigger_a_recreate(monkeypatch):
    """A 500 is transient — recreating would post a duplicate comment."""
    methods: list[str] = []

    def fake_urlopen(req, *a, **k):
        methods.append(req.get_method())
        _raise(500, {})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _mod.STATE.set_comment_id("o/r", "5", 111)
    assert _mod.upsert_comment("tok", "o/r", "5", "body") is None
    assert methods == ["PATCH"], \
        f"a transient 500 caused a duplicate comment to be posted: {methods}"
    assert _mod.STATE.comment_id_for("o/r", "5") == 111, "a 500 must not discard the cached id"


# ─── P3: artifact extract paths must be keyed by immutable id ─────────


def test_two_artifact_ids_with_one_name_do_not_share_a_directory(tmp_path, monkeypatch):
    """``overwrite: true`` mints a new id under the SAME name.

    Download/extract paths keyed by name alone let a stale extract from the
    previous id be served for the new one.
    """
    seen: list[Path] = []

    class _FakeZip:
        def __init__(self, path):
            self._path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def namelist(self):
            return ["review-status.json"]

        def extractall(self, dest):
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / "review-status.json").write_text("review_status=[]")

    monkeypatch.setattr(_mod.zipfile, "ZipFile", _FakeZip)

    class _Blob:
        headers = {}

        def read(self):
            return b"zip"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener_open(req, *a, **k):
        _raise(302, {"Location": "https://blob/x"})

    monkeypatch.setattr(
        urllib.request, "build_opener",
        lambda *a, **k: type("O", (), {"open": staticmethod(opener_open)})(),
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Blob())

    for artifact_id in (1, 2):
        out = _mod._download_artifact(
            "tok", "o/r",
            {"id": artifact_id, "name": "review-status-lint",
             "archive_download_url": "https://api/archive"},
            tmp_path,
        )
        assert out is not None
        seen.append(out)

    assert seen[0] != seen[1], (
        "two distinct artifact ids extracted to the same directory; a stale "
        f"extract can be served for the newer id ({seen[0]})"
    )
    assert "1" in seen[0].parts[-2] and "2" in seen[1].parts[-2]


# ─── P3: dead parameter ───────────────────────────────────────────────


def test_conditional_get_has_no_dead_list_key_parameter():
    """``_api_get_paginated`` applies list_key itself; the param was unused."""
    import inspect
    params = inspect.signature(_mod._conditional_get).parameters
    assert "list_key" not in params, \
        "list_key is dead on _conditional_get — it misleads every caller"



# ─── round 4 ──────────────────────────────────────────────────────────


def test_backoff_sleeps_respect_the_wall_clock_budget(monkeypatch):
    """_nap clamped only against the ACTIVE timeout.

    Queued time is added back to ``start``, so the active timeout can have
    thousands of seconds left while the job has seconds. A long rate-limit
    backoff would then run into the Actions deadline and be SIGKILLed.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])
    monkeypatch.setattr(_mod, "upsert_comment", lambda *a, **k: 1)

    def always_limited(*a, **k):
        raise _mod.RateLimitError(2000.0)

    monkeypatch.setattr(_mod, "collect_run_jobs", always_limited)

    slept: list[float] = []
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep", lambda s: (
        slept.append(s), clock.__setitem__("t", clock["t"] + max(s, 1.0))))

    budget = 300
    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False, max_wall_seconds=budget)

    assert clock["t"] <= budget + 5, (
        f"a {slept[0]:.0f}s backoff ran to {clock['t']:.0f}s against a "
        f"{budget}s wall-clock budget — the job would be SIGKILLed"
    )


def test_final_upsert_failure_is_retried_before_exit(monkeypatch):
    """"will retry" is a lie on the final cycle: the loop breaks right after.

    The body must CHANGE on the last cycle (so an upsert is attempted then)
    and that attempt must fail. Earlier failures are covered by the ordinary
    next-cycle retry and prove nothing about the exit path.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    cycles = {"n": 0}

    def jobs(*a, **k):
        cycles["n"] += 1
        # Pending -> (grace) -> a NEW job appears on the final cycle, so its
        # body genuinely differs and an upsert is attempted there. Without
        # that, the final cycle posts nothing and the exit path is untested.
        if cycles["n"] <= 1:
            return [{"name": "Python tests", "status": "in_progress",
                     "html_url": "u"}], False
        if cycles["n"] == 2:
            return [{"name": "Python tests", "status": "completed",
                     "conclusion": "success", "html_url": "u"}], True
        return [{"name": "Python tests", "status": "completed",
                 "conclusion": "success", "html_url": "u"},
                {"name": "Lint", "status": "completed",
                 "conclusion": "failure", "html_url": "u2"}], True

    monkeypatch.setattr(_mod, "collect_run_jobs", jobs)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    attempts = {"n": 0}
    posted: list[int] = []

    def flaky(token, repo, pr, body, comment_id=None):
        attempts["n"] += 1
        # The run is: cycle 1 (pending, posts), cycle 2 (all_done -> grace,
        # posts the completed body), cycle 3 = the TRUE final cycle, after
        # which the loop breaks. Fail that one.
        if attempts["n"] == 3:
            return None
        posted.append(attempts["n"])
        return 4242

    monkeypatch.setattr(_mod, "upsert_comment", flaky)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=3000, dry_run=False)

    assert attempts["n"] >= 4, (
        "the FINAL comment write failed and the poller exited without "
        f"retrying (attempts={attempts['n']}) — the comment is left stale"
    )
    assert len(posted) >= 2, "the final status was never successfully written"


def test_artifact_slug_is_bounded(monkeypatch, tmp_path):
    """A long caller-chosen artifact name can exceed NAME_MAX.

    The resulting OSError reads as a transient download failure, silently
    omitting or staling the section.
    """
    long_name = "review-status-" + "x" * 300
    captured: list[str] = []

    monkeypatch.setattr(
        urllib.request, "build_opener",
        lambda *a, **k: type("O", (), {"open": staticmethod(
            lambda req, *a, **k: _raise(302, {"Location": "https://blob/x"}))})(),
    )

    class _Blob:
        headers = {}

        def read(self):
            return b"zip"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Blob())

    class _FakeZip:
        def __init__(self, path):
            captured.append(Path(path).name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def namelist(self):
            return ["review-status.json"]

        def extractall(self, dest):
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / "review-status.json").write_text("review_status=[]")

    monkeypatch.setattr(_mod.zipfile, "ZipFile", _FakeZip)

    out = _mod._download_artifact(
        "tok", "o/r",
        {"id": 7, "name": long_name, "archive_download_url": "https://api/a"},
        tmp_path,
    )
    assert out is not None, "a long artifact name broke the download path"
    for component in (captured + [out.parts[-2]]):
        assert len(component.encode()) <= 255, (
            f"path component exceeds NAME_MAX ({len(component)} bytes): "
            f"{component[:60]}..."
        )
    assert "7" in out.parts[-2], "the immutable id is no longer in the path"


def test_wall_clock_budget_leaves_a_real_shutdown_margin():
    """One second below the job timeout is not a margin."""
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/ci-review-comment.yml").read_text(encoding="utf-8")
    job_timeout = int(
        re.search(r"^\s*timeout-minutes:\s*(\d+)", text, re.M).group(1)) * 60
    budget = int(re.search(r"--max-wall-seconds (\d+)", text).group(1))
    margin = job_timeout - budget
    assert margin >= 120, (
        f"--max-wall-seconds {budget} leaves only {margin}s before the job's "
        f"{job_timeout}s deadline — not enough to publish a final status"
    )
