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

    def fake_download(token, repo, artifact, dest, deadline=None):
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

    def fake_download(token, repo, artifact, dest, deadline=None):
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
    # `is not None` admitted a timeout of 0, which urllib treats as a
    # non-blocking socket that fails instantly — measured: setting
    # _REQUEST_TIMEOUT = 0 left this whole file green. The expectation is a
    # deliberate literal, NOT derived from _REQUEST_TIMEOUT, so mutating the
    # constant moves the code without moving the assertion.
    assert seen, "no API call was observed"
    for t in seen:
        assert isinstance(t, (int, float)) and not isinstance(t, bool), \
            f"an API call was made with a non-numeric socket timeout: {seen}"
        assert 5 <= t <= 120, (
            f"an API call used a {t}s socket timeout; a GitHub API call needs "
            "a real, bounded window (0 fails instantly, huge values hang)"
        )


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
        lambda t, r, artifact, dest, deadline=None: (
            None if state["fail"] else Path("/x.json")))
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


def _workflow_wall_budget():
    """The budget the workflow ACTUALLY runs with: override or default.

    An absent `--max-wall-seconds` means the script's default applies. A
    PRESENT one must be read in whatever form argparse accepts — argparse
    takes `--max-wall-seconds=1799` as readily as the space form, and a
    regex matching only the space form would silently substitute the safe
    default and let these guards pass while the poller ran with 1799s.
    A present-but-unparseable value is a hard failure, not a default.
    """
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/ci-review-comment.yml").read_text(
        encoding="utf-8")
    job_timeout_min = int(
        re.search(r"^\s*timeout-minutes:\s*(\d+)", text, re.M).group(1))
    # Only the invocation matters — the file's comments discuss the flag.
    invocation = text[text.index("live_comment.py"):]
    present = re.search(r"--max-wall-seconds(?:[=\s]+)(\S+)", invocation)
    if present is None:
        budget = _mod._DEFAULT_MAX_WALL_SECONDS
    else:
        raw = present.group(1).strip().strip("\\").strip()
        assert raw.isdigit(), (
            f"the workflow passes --max-wall-seconds {raw!r}, which this "
            "guard cannot evaluate — the poller would run with an unchecked "
            "wall-clock budget"
        )
        budget = int(raw)
    return job_timeout_min * 60, budget


def _workflow_poll_ceiling():
    """When the workflow's poller actually STOPS polling."""
    _, budget = _workflow_wall_budget()
    return budget - _mod._SHUTDOWN_RESERVE_SECONDS


def test_workflow_sets_a_wall_clock_budget_below_the_job_timeout():
    """A budget at or above timeout-minutes would never fire."""
    job_timeout, budget = _workflow_wall_budget()
    assert budget < job_timeout, (
        f"the poller's effective wall budget {budget} is not below the job's "
        f"timeout-minutes ({job_timeout}s), so "
        "the poller is still killed before it can stop cleanly"
    )


def test_workflow_keeps_the_supported_50_minute_monitoring_window():
    """F1: the monitoring window is a DECISION, not a side-effect.

    Active polling must still reach 3000s — the window the poller had
    before the shutdown reserve was carved out of the budget. Letting the
    reserve shorten it (budget 2700 => ceiling 2400) is a user-visible
    regression: a CI or merge-queue run finishing between 40 and 50 minutes
    has its final snapshot taken while still in progress and is never
    updated.

    A LITERAL, not derived from the constants — deriving it would move the
    expectation in lockstep with any future budget cut and gate nothing.
    """
    assert _workflow_poll_ceiling() >= 3000, (
        f"active polling now stops at {_workflow_poll_ceiling()}s; the "
        "supported monitoring window is 50 minutes (3000s), so a run "
        "finishing after that is reported as still running"
    )


def test_the_workflow_states_its_budget_explicitly():
    """F1: the window must not be INHERITED from the script's default.

    ``_workflow_poll_ceiling`` above is satisfied either by the flag or by
    the default, so each alone gates nothing. The workflow is where an
    operator reads the monitoring window, and a silent change to the
    script's default must not move it.
    """
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/ci-review-comment.yml").read_text(
        encoding="utf-8")
    invocation = text[text.index("live_comment.py"):]
    assert re.search(r"--max-wall-seconds[=\s]", invocation), (
        "the workflow passes no --max-wall-seconds, so its monitoring "
        "window is whatever the script's default happens to be"
    )


def test_the_script_default_also_holds_the_supported_window():
    """F1: the other half — the default is the fallback, not a shortcut.

    A poller invoked without the flag (a manual run, a future workflow)
    must still get the supported window rather than a silently shorter one.
    """
    ceiling = _mod._DEFAULT_MAX_WALL_SECONDS - _mod._SHUTDOWN_RESERVE_SECONDS
    assert ceiling >= 3000, (
        f"the script's own default stops polling at {ceiling}s, below the "
        "supported 3000s monitoring window"
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
    checks = {"n": 0, "released_at": None}

    def fake_queued(token, repo, pr):
        checks["n"] += 1
        still = memberships[min(checks["n"] - 1, len(memberships) - 1)]
        if not still and checks["released_at"] is None:
            checks["released_at"] = clock["t"]
        return still

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", fake_queued)
    # CI must PROGRESS across the queue period, or the post-dequeue body is
    # byte-identical to the pre-pause snapshot and is correctly deduped —
    # and then "a write happened after the dequeue" is unprovable for a
    # correct implementation as well as a broken one.
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests",
          "status": "completed" if checks["released_at"] is not None
          else "in_progress",
          "conclusion": "failure" if checks["released_at"] is not None else None,
          "html_url": "u"}],
        checks["released_at"] is not None))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    posted: list[tuple[float, str]] = []
    monkeypatch.setattr(
        _mod, "upsert_comment",
        lambda t, r, p, body, comment_id=None: posted.append((clock["t"], body)) or 7)

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
    assert checks["released_at"] is not None, \
        "the poller exited before the PR was ever seen to leave the queue"
    assert posted, \
        "the PR left the merge queue but no comment was ever published"
    # The pre-pause snapshot also writes, so "posted is non-empty" alone
    # does not prove the DEQUEUE was reported. Require a write at or after
    # the moment membership went false.
    assert any(t >= checks["released_at"] for t, _ in posted), (
        f"every write landed before the dequeue at t={checks['released_at']:.0f}s "
        f"(writes at {[round(t) for t, _ in posted]})"
    )


def test_queued_pause_does_not_consume_the_poll_lifetime(monkeypatch):
    """A long queue stay must not exhaust the timeout before the dequeue.

    The pause exists so a dequeued PR still gets reported. If queued time
    counted against ``timeout``, a PR queued for the whole window would exit
    the poller — and merge-group runs never comment, so nothing would ever
    publish its result. That is the same bug the pause replaced.
    """
    # Queued far longer than the whole 1800s timeout, then released.
    queued = {"n": 0, "released_at": None}

    def fake_queued(token, repo, pr):
        queued["n"] += 1
        still = queued["n"] <= 20        # 20 * 300s = 6000s >> timeout
        if not still and queued["released_at"] is None:
            queued["released_at"] = clock["t"]
        return still

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", fake_queued)
    # CI progresses across the queue period so the post-dequeue body really
    # differs from the pre-pause snapshot; an identical body is correctly
    # deduped and would make the post-dequeue assertion unprovable.
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests",
          "status": "completed" if queued["released_at"] is not None
          else "in_progress",
          "conclusion": "success" if queued["released_at"] is not None else None,
          "html_url": "u"}],
        queued["released_at"] is not None))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    posted: list[tuple[float, str]] = []
    monkeypatch.setattr(
        _mod, "upsert_comment",
        lambda t, r, p, body, comment_id=None: posted.append((clock["t"], body)) or 7)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    # The wall budget must exceed the 6000s queued period, or the poller
    # stops at its ceiling while STILL queued and the test proves nothing:
    # `posted` would be satisfied by the pre-pause snapshot alone.
    rc = _mod.run(token="t", repo="o/r", run_id="1", pr_number="5",
                  run_url="u", interval=45, timeout=1800, dry_run=False,
                  max_wall_seconds=20000)

    assert rc == 0
    assert clock["t"] > 1800, "the test did not actually outlast the timeout"
    assert queued["released_at"] is not None, (
        "the poller never saw the PR leave the queue — it stopped early, so "
        "this test never exercised the dequeue path at all"
    )
    assert posted, (
        f"the PR sat queued for {clock['t']:.0f}s (timeout 1800s), was then "
        "dequeued, and the poller had already exited — its result is "
        "permanently unreported"
    )
    # The load-bearing claim: a write happened AFTER the dequeue, not just
    # the pre-pause snapshot taken while it was still queued.
    assert any(t >= queued["released_at"] for t, _ in posted), (
        f"every write happened before the dequeue at t="
        f"{queued['released_at']:.0f}s "
        f"(writes at {[round(t) for t, _ in posted]}) — the PR was abandoned "
        "with only its stale initial snapshot"
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
    # 5 queued pauses happened before the PR was released. The poller is
    # allowed ONE collect to publish an initial comment before the first
    # pause (a PR queued from the start would otherwise never get one) —
    # but it must not keep polling for the rest of the queued stay.
    assert queued["n"] >= 6, "the PR was never actually paused while queued"
    assert collects["n"] <= 3, (
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
    bodies: list[str] = []
    failed_body = {"value": None}

    def flaky(token, repo, pr, body, comment_id=None):
        attempts["n"] += 1
        # The run is: cycle 1 (pending, posts), cycle 2 (all_done -> grace,
        # posts the completed body), cycle 3 = the TRUE final cycle, after
        # which the loop breaks. Fail that one.
        if attempts["n"] == 3:
            failed_body["value"] = body
            return None
        posted.append(attempts["n"])
        bodies.append(body)
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
    # Counting attempts is not enough: retrying the PREVIOUS body would
    # satisfy that while leaving the user-facing comment stale. The retry
    # must carry the body that failed, including the new Lint result.
    assert failed_body["value"] is not None, "no body actually failed"
    assert "Lint" in failed_body["value"], "the failed body was not the final one"
    assert bodies[-1] == failed_body["value"], (
        "the retry posted a DIFFERENT body than the one that failed — the "
        "published comment is stale even though the retry 'succeeded'"
    )


def test_artifact_slug_is_bounded(monkeypatch, tmp_path):
    """A long caller-chosen artifact name can exceed NAME_MAX.

    The resulting OSError reads as a transient download failure, silently
    omitting or staling the section.
    """
    # MULTIBYTE on purpose: NAME_MAX is a byte limit, so an ASCII-only
    # fixture passes against a character-based truncation that still
    # overflows in the real world.
    long_name = "review-status-" + "é中😀" * 120
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
    """The shutdown snapshot must have real slack past the whole budget.

    Active polling stops at ``budget - reserve``; the snapshot then runs
    until ``budget`` at the latest. But the artifact deadline only blocks
    STARTING new work — an in-flight redirect + blob request can each run a
    further socket timeout, and the final PATCH costs another. So the job
    deadline must sit at least that overrun past the FULL budget, not
    merely past the poll ceiling, or the snapshot is racing a SIGKILL.

    Every expectation here is a deliberate LITERAL. Deriving them from
    ``_SHUTDOWN_RESERVE_SECONDS`` / ``_REQUEST_TIMEOUT`` moved the assertion
    in lockstep with the code, so zeroing either constant stayed green
    (measured: 68 passed with ``_SHUTDOWN_RESERVE_SECONDS = 0``).
    """
    job_timeout, budget = _workflow_wall_budget()

    # A reserve that is actually a window, sized for the serial API calls
    # the snapshot makes. 60s is the floor a single artifact fetch needs.
    assert _mod._SHUTDOWN_RESERVE_SECONDS >= 60, (
        f"the shutdown reserve is {_mod._SHUTDOWN_RESERVE_SECONDS}s — too "
        "small for the snapshot's serial API calls (a single artifact fetch "
        "is a 30s redirect plus a 30s blob request)"
    )
    assert _mod._SHUTDOWN_RESERVE_SECONDS <= budget / 2, (
        "the shutdown reserve consumes more than half the poll budget"
    )
    poll_ceiling = budget - _mod._SHUTDOWN_RESERVE_SECONDS
    assert poll_ceiling > 0, "the reserve consumed the entire budget"

    # An in-flight redirect + blob request plus the final PATCH, at the
    # socket timeouts a GitHub API call and an artifact blob are allowed:
    # 30 + 30 + 30. Literal, not derived. 60 undercounted the very path
    # this docstring describes and would have admitted a 60-89s margin that
    # is still SIGKILLed before publishing.
    overrun = 90
    assert job_timeout - budget >= overrun, (
        f"the wall budget {budget}s ends only {job_timeout - budget}s before "
        f"the job's {job_timeout}s deadline, but an in-flight artifact "
        f"request plus the final PATCH can overrun it by {overrun}s"
    )


# ─── round 5 ──────────────────────────────────────────────────────────


def test_wall_clock_cutoff_publishes_before_exiting(monkeypatch):
    """A bare break at the wall-clock budget publishes nothing.

    If CI completes (or the PR is dequeued) during the final minutes, the
    poller would exit having written no final status at all.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: True)

    # CI progresses while the PR sits queued, so the state at the
    # wall-clock break genuinely differs from the pre-pause snapshot. A
    # constant state would be deduped by `body == last_body` and hide
    # whether the break path publishes at all.
    calls = {"n": 0}

    def progressing(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 1:
            return [{"name": "Python tests", "status": "in_progress",
                     "html_url": "u"}], False
        return [{"name": "Python tests", "status": "completed",
                 "conclusion": "failure", "html_url": "u"}], True

    monkeypatch.setattr(_mod, "collect_run_jobs", progressing)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    posted: list[str] = []

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    monkeypatch.setattr(
        _mod, "upsert_comment",
        lambda t_, r_, p_, body, comment_id=None: posted.append(body) or 1)

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False, max_wall_seconds=600)

    # Counting writes is not enough: an implementation that republishes the
    # PRE-PAUSE body at shutdown would satisfy `len(posted) >= 2` while
    # leaving users a stale "still running" status forever.
    assert len(posted) >= 2, (
        "the poller hit its wall-clock budget and broke out without a final "
        f"publish — only the pre-pause snapshot was written ({len(posted)})"
    )
    assert calls["n"] >= 2, (
        "the cutoff republished without re-collecting job state, so the "
        "final comment can only ever repeat the pre-pause snapshot"
    )
    assert posted[-1] != posted[0], (
        "CI completed during the queue pause but the final write is "
        "byte-identical to the pre-pause snapshot — it is stale"
    )
    assert "failure" in posted[-1].lower(), (
        "the job finished as a FAILURE during the pause and the final "
        f"comment does not report it: {posted[-1][:200]}"
    )


def test_pr_queued_from_the_start_still_gets_a_comment(monkeypatch):
    """The queue branch `continue`s before ever creating the comment."""
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: True)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "completed",
          "conclusion": "failure", "html_url": "u"}], True))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    posted: list[str] = []
    first_write_time = {"t": None}
    clock = {"t": 0.0}

    def record(t_, r_, p_, body, comment_id=None):
        if first_write_time["t"] is None:
            first_write_time["t"] = clock["t"]
        posted.append(body)
        return 1

    monkeypatch.setattr(_mod, "upsert_comment", record)
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=900, dry_run=False, max_wall_seconds=900)

    assert posted, (
        "a PR that was already queued when the poller started never had a "
        "comment created at all"
    )
    # The FIRST write must land before any pause completes, not only at the
    # wall-clock break 15 minutes later: the queue branch `continue`s before
    # reaching the normal publish path.
    assert first_write_time["t"] is not None
    assert first_write_time["t"] < _mod._MERGE_QUEUE_RECHECK_INTERVAL, (
        f"the comment was first written at t={first_write_time['t']:.0f}s — "
        "after a full queue pause, not before it"
    )


def test_rate_limit_on_the_final_retry_backs_off_instead_of_giving_up(monkeypatch):
    """Converting the RateLimitError to None exits with a stale comment.

    Budget usually remains; the poller must back off and try again.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    cycles = {"n": 0}

    def jobs(*a, **k):
        cycles["n"] += 1
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
    posted: list[str] = []

    def flaky(token, repo, pr, body, comment_id=None):
        attempts["n"] += 1
        if attempts["n"] == 3:          # the final cycle's write
            return None
        if attempts["n"] == 4:          # its immediate retry
            raise _mod.RateLimitError(30.0)
        posted.append(body)
        return 4242

    monkeypatch.setattr(_mod, "upsert_comment", flaky)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=3000, dry_run=False)

    assert any("Lint" in b for b in posted), (
        "a rate limit on the final retry made the poller give up; the "
        f"completed result was never published (posted={len(posted)})"
    )


def test_parse_failure_is_not_cached_as_an_empty_result(monkeypatch, tmp_path):
    """`[]` is a valid payload; a read/parse failure is not the same thing.

    Caching the failure against the immutable id erases an already-published
    section permanently.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    state = {"id": 1, "broken": False}

    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": state["id"], "name": "review-status-lint",
         "archive_download_url": "u"}])
    monkeypatch.setattr(_mod, "_download_artifact",
                        lambda t, r, artifact, dest, deadline=None: Path("/x.json"))
    monkeypatch.setattr(
        _mod, "_parse_status_file",
        lambda p: None if state["broken"] else [{"source": "lint", "results": [{"ok": 1}]}])

    first = _mod.fetch_all_review_statuses("tok", "o/r", "77")
    assert len(first) == 1

    state["id"] = 2          # overwrite: true mints a new id
    state["broken"] = True   # its local file fails to parse
    second = _mod.fetch_all_review_statuses("tok", "o/r", "77")
    assert second == first, (
        "a transient parse failure erased the already-published review "
        f"section ({second})"
    )


def test_parse_returns_none_on_failure_but_empty_list_on_empty_payload(tmp_path):
    good = tmp_path / "good.json"
    good.write_text("review_status=[]", encoding="utf-8")
    assert _mod._parse_status_file(good) == []

    bad = tmp_path / "bad.json"
    bad.write_text("review_status={not json", encoding="utf-8")
    assert _mod._parse_status_file(bad) is None, \
        "a parse failure is indistinguishable from a valid empty payload"

    assert _mod._parse_status_file(tmp_path / "missing.json") is None


def test_older_same_named_artifact_does_not_clobber_the_newer_one(monkeypatch, tmp_path):
    """GitHub lists artifacts newest-first, but the loop walks them all.

    An OLDER same-named artifact appearing later must not replace the
    newer parsed result and resurrect an obsolete section.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 20, "name": "review-status-lint", "archive_download_url": "new"},
        {"id": 10, "name": "review-status-lint", "archive_download_url": "old"},
    ])
    monkeypatch.setattr(
        _mod, "_download_artifact",
        lambda t, r, artifact, dest, deadline=None: Path(f"/{artifact['id']}.json"))
    monkeypatch.setattr(
        _mod, "_parse_status_file",
        lambda p: [{"source": "lint", "results": [{"id": p.stem}]}])

    _mod.fetch_all_review_statuses("tok", "o/r", "77")
    carried = _mod.STATE.artifacts_by_name["review-status-lint"]
    assert carried[0]["results"][0]["id"] == "20", (
        "the OLDER artifact (id 10) overwrote the newer one (id 20) as the "
        f"fallback, so a later blip would republish stale results ({carried})"
    )


def test_artifact_slug_is_bounded_in_bytes_not_characters(monkeypatch, tmp_path):
    """NAME_MAX is a BYTE limit.

    80 four-byte characters plus the id and ".zip" exceeds 255 bytes, and
    the resulting OSError reads as a transient download failure.
    """
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
        {"id": 123456, "name": "review-status-" + "\U0001f600" * 200,
         "archive_download_url": "https://api/a"},
        tmp_path,
    )
    assert out is not None
    for component in captured + [out.parts[-2]]:
        encoded = len(component.encode("utf-8"))
        assert encoded <= 255, (
            f"path component is {encoded} BYTES (NAME_MAX is 255): "
            f"{component[:40]}..."
        )


def test_wall_clock_ceiling_is_finite_by_default():
    """An unset ceiling lets a queued PR pause until the job is SIGKILLed.

    Queue pauses are excluded from the active timeout, so without a finite
    default the only thing that stops the poller is the Actions deadline —
    which publishes nothing.
    """
    import inspect

    assert _mod._DEFAULT_MAX_WALL_SECONDS is not None
    assert _mod._DEFAULT_MAX_WALL_SECONDS > 0
    assert _mod.build_parser().parse_args([]).max_wall_seconds == \
        _mod._DEFAULT_MAX_WALL_SECONDS, \
        "the CLI default leaves the wall-clock ceiling unset"
    assert inspect.signature(_mod.run).parameters["max_wall_seconds"].default == \
        _mod._DEFAULT_MAX_WALL_SECONDS, \
        "run()'s default leaves the wall-clock ceiling unset"

    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/ci-review-comment.yml").read_text(encoding="utf-8")
    job_timeout = int(
        re.search(r"^\s*timeout-minutes:\s*(\d+)", text, re.M).group(1)) * 60
    assert _mod._DEFAULT_MAX_WALL_SECONDS < job_timeout, (
        f"the default ceiling ({_mod._DEFAULT_MAX_WALL_SECONDS}s) is not "
        f"below the job deadline ({job_timeout}s)"
    )


# ─── round 6 ──────────────────────────────────────────────────────────


def test_final_snapshot_keeps_prev_statuses_when_listing_fails(monkeypatch):
    """F1: `None` from the listing must not erase published sections.

    The normal polling path already preserves the last known statuses when
    the artifact listing fails transiently. The final-snapshot path coerced
    that `None` to `[]`, so a listing blip coinciding with the cutoff
    republished the comment with every review section deleted — and there
    is no later cycle to repair it.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    cycles = {"n": 0}

    def jobs(*a, **k):
        cycles["n"] += 1
        return [{"name": "Python tests", "status": "in_progress",
                 "html_url": "u"}], False

    monkeypatch.setattr(_mod, "collect_run_jobs", jobs)

    good = [{"source": "FleetReview", "results": [{"name": "r", "status": "ok"}]}]

    def statuses(token, repo, run_id, deadline=None):
        # Healthy on the first cycle, then the listing starts failing —
        # including on the final snapshot.
        return good if cycles["n"] <= 1 else None

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", statuses)

    # Assert on the review-status JSON the poller actually hands to the
    # assembler — the rendered markdown depends on the assembler's own
    # formatting rules, which is not what this finding is about.
    bodies: list[str] = []

    def spy_body(asm, completed, pending, run_url, job_urls,
                 review_statuses_json, commit_info="", waiting=False):
        bodies.append(review_statuses_json)
        return f"body:{review_statuses_json}:{sorted(completed.items())}:{pending}"

    monkeypatch.setattr(_mod, "build_comment_body", spy_body)

    posted: list[str] = []
    monkeypatch.setattr(
        _mod, "upsert_comment",
        lambda t, r, p, body, comment_id=None: posted.append(body) or 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False, max_wall_seconds=600)

    assert posted, "nothing was ever published"
    assert "FleetReview" in bodies[0], "the first body never carried the section"
    assert "FleetReview" in bodies[-1], (
        "the final snapshot hit a transient listing failure and republished "
        "the comment with the review section deleted"
    )


def test_queue_pause_snapshot_records_what_it_published(monkeypatch):
    """SEAM: `_publish_final_snapshot` must RECORD `prev_artifact_statuses`.

    The snapshot is not only a shutdown path — a poller that starts while
    the PR is already merge-queued calls it mid-run to create the comment,
    and polling then RESUMES after the dequeue. If it fetches statuses but
    does not record them, the next cycle's transient listing failure falls
    back to the stale `prev_artifact_statuses` (`[]`) and republishes the
    comment with every review section deleted — the exact F1 defect one
    seam over.

    The F1 test above proves the snapshot READS `prev_artifact_statuses`;
    nothing proved it WRITES it. Measured: deleting
    `prev_artifact_statuses = statuses` from the snapshot left the whole
    file green (71 passed) while the poller published a body with the
    section gone.
    """
    queue_checks = {"n": 0}
    listings = {"n": 0}

    def queued(token, repo, pr):
        queue_checks["n"] += 1
        return queue_checks["n"] == 1      # queued on cycle 1, released after

    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", queued)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))

    good = [{"source": "FleetReview", "results": [{"name": "r", "status": "ok"}]}]

    def statuses(token, repo, run_id, deadline=None):
        listings["n"] += 1
        # The queue-pause snapshot fetches successfully; every later cycle
        # hits a transient listing failure.
        return good if listings["n"] <= 1 else None

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", statuses)

    bodies: list[str] = []

    def spy_body(asm, completed, pending, run_url, job_urls,
                 review_statuses_json, commit_info="", waiting=False):
        bodies.append(review_statuses_json)
        return f"body:{review_statuses_json}:{pending}"

    monkeypatch.setattr(_mod, "build_comment_body", spy_body)
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False, max_wall_seconds=1200)

    assert queue_checks["n"] >= 2, "the PR was never rechecked after the pause"
    assert listings["n"] >= 2, "no cycle ran after the queue-pause snapshot"
    assert bodies, "nothing was ever rendered"
    assert "FleetReview" in bodies[0], (
        "the queue-pause snapshot never carried the section")
    assert "FleetReview" in bodies[-1], (
        "the queue-pause snapshot did not record what it published, so a "
        "later transient listing failure republished the comment with the "
        "review section deleted"
    )


def test_newest_empty_artifact_does_not_resurrect_an_older_failure(
        monkeypatch, tmp_path):
    """F5: an older attempt's failure must not survive a green rerun.

    A rerun leaves two non-expired artifacts with the SAME name. If the
    newest validly contains `[]` (producers emit that on success) it
    contributes no `source`, so appending every artifact and deduping by
    source afterwards let the OLDER failed attempt through.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        # GitHub lists newest-first; id 20 is the rerun, id 10 the failure.
        {"id": 20, "name": "review-status-lint", "archive_download_url": "u20"},
        {"id": 10, "name": "review-status-lint", "archive_download_url": "u10"},
    ])
    monkeypatch.setattr(
        _mod, "_download_artifact",
        lambda token, repo, artifact, dest, deadline=None: Path(
            f"/nonexistent/{artifact['id']}.json"))

    payloads = {
        "20": [],
        "10": [{"source": "lint", "results": [{"name": "lint", "status": "failure"}]}],
    }
    monkeypatch.setattr(_mod, "_parse_status_file",
                        lambda p: payloads[p.stem])

    out = _mod.fetch_all_review_statuses("tok", "o/r", "77")

    assert out == [], (
        "the rerun's artifact is empty (the job passed), but an older failed "
        f"attempt's result was resurrected into the comment: {out}"
    )


@pytest.mark.parametrize("listing_order", ["newest-first", "oldest-first"])
def test_newest_artifact_wins_regardless_of_listing_order(
        monkeypatch, tmp_path, listing_order):
    """F5 SEAM: the selection must SORT, not trust the listing's order.

    The sibling test above feeds artifacts newest-first, which is the order
    the API happens to return today — so deleting the sort entirely left it
    green (measured: 68 passed with `group = list(group)`). Nothing pinned
    the ordering guarantee itself. Feeding the SAME data oldest-first fails
    the moment the sort is dropped, because then the stale failed attempt is
    chosen first and published.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    newest = {"id": 20, "name": "review-status-lint",
              "archive_download_url": "u20"}
    oldest = {"id": 10, "name": "review-status-lint",
              "archive_download_url": "u10"}
    listing = ([newest, oldest] if listing_order == "newest-first"
               else [oldest, newest])
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: list(listing))
    monkeypatch.setattr(
        _mod, "_download_artifact",
        lambda token, repo, artifact, dest, deadline=None: Path(
            f"/nonexistent/{artifact['id']}.json"))

    payloads = {
        "20": [],
        "10": [{"source": "lint",
                "results": [{"name": "lint", "status": "failure"}]}],
    }
    monkeypatch.setattr(_mod, "_parse_status_file", lambda p: payloads[p.stem])

    out = _mod.fetch_all_review_statuses("tok", "o/r", "77")

    assert out == [], (
        f"with the listing in {listing_order} order the poller published a "
        f"stale failure from the older attempt: {out} — the selection is "
        "relying on the listing's order instead of sorting by id"
    )


def test_unreadable_newest_artifact_is_unknown_not_the_older_attempt(
        monkeypatch, tmp_path):
    """F10: unknown beats stale — an older same-named artifact is NOT used.

    Ruled 2026-09-20: when the NEWEST artifact for a name cannot be read,
    the group is unknown. Substituting the older same-named artifact
    publishes a SUPERSEDED attempt's result as if it were the current one —
    on a rerun a cold poller would show the previous attempt's failure until
    the new archive becomes readable, or permanently if this is the final
    snapshot. It also contradicts the newest-wins tests above.

    A value this PROCESS already published under the name is a different
    case and is still carried (covered by
    ``test_failed_download_of_a_new_id_keeps_the_published_section``): that
    section is already on screen, and deleting it over one blip is the
    regression the carry exists to prevent.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 20, "name": "review-status-lint", "archive_download_url": "u20"},
        {"id": 10, "name": "review-status-lint", "archive_download_url": "u10"},
    ])

    attempted: list[int] = []

    def download(token, repo, artifact, dest, deadline=None):
        attempted.append(artifact["id"])
        if artifact["id"] == 20:
            return None            # not uploaded yet / transient blip
        return Path("/nonexistent/10.json")

    monkeypatch.setattr(_mod, "_download_artifact", download)
    monkeypatch.setattr(
        _mod, "_parse_status_file",
        lambda p: [{"source": "lint",
                    "results": [{"name": "lint", "status": "failure"}]}])

    out = _mod.fetch_all_review_statuses("tok", "o/r", "77")
    assert out == [], (
        f"the superseded attempt's artifact (id 10) was published as the "
        f"current result: {out}"
    )
    assert attempted == [20], (
        f"the older same-named artifact was downloaded anyway: {attempted}"
    )


def test_artifact_fetch_stops_downloading_past_its_deadline(
        monkeypatch, tmp_path):
    """F6: the shutdown snapshot must not chain unbounded downloads.

    Each uncached artifact costs a redirect request plus a blob request,
    each up to _REQUEST_TIMEOUT. Started with no deadline after the budget
    was already spent, enough uncached artifacts push the process past the
    Actions job deadline and it is SIGKILLed having published nothing.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": i, "name": f"review-status-{i}", "archive_download_url": f"u{i}"}
        for i in range(1, 6)
    ])

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])

    downloads: list[int] = []

    def slow_download(token, repo, artifact, dest, deadline=None):
        downloads.append(artifact["id"])
        clock["t"] += 60.0          # a realistic worst-case artifact fetch
        return Path(f"/nonexistent/{artifact['id']}.json")

    monkeypatch.setattr(_mod, "_download_artifact", slow_download)
    monkeypatch.setattr(
        _mod, "_parse_status_file",
        lambda p: [{"source": f"s{p.stem}", "results": []}])

    out = _mod.fetch_all_review_statuses(
        "tok", "o/r", "77", deadline=150.0)

    assert out is not None
    assert len(downloads) <= 3, (
        f"the fetch kept downloading past its 150s deadline: {len(downloads)} "
        f"downloads ending at t={clock['t']:.0f}s"
    )
    assert downloads, "the deadline blocked even the first download"
    assert clock["t"] <= 210.0, (
        f"the fetch ran to t={clock['t']:.0f}s despite a 150s deadline"
    )


def test_final_snapshot_wires_a_real_deadline_into_the_artifact_fetch(
        monkeypatch):
    """F6 SEAM: the deadline must REACH the fetch, not merely be honoured.

    The sibling test above calls ``fetch_all_review_statuses`` DIRECTLY with
    an explicit deadline, so it only proves the parameter is respected once
    supplied. Nothing proved the shutdown snapshot supplies one — measured:
    hardcoding ``deadline=None`` at the call site left the whole file green
    (68 passed). This drives the real ``run()`` to its wall-clock cutoff and
    asserts on what the seam actually handed over.
    """
    seen: dict[str, object] = {}

    def spy_fetch(token, repo, run_id, deadline=None):
        seen["deadline"] = deadline
        seen["at"] = clock["t"]
        return []

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", spy_fetch)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        _mod.time, "sleep",
        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    budget = 1200
    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False,
             max_wall_seconds=budget)

    deadline = seen.get("deadline")
    assert deadline is not None, (
        "the shutdown snapshot fetched artifacts with NO deadline — a run "
        "with many uncached artifacts can chain 30-60s downloads past the "
        "Actions job deadline and be SIGKILLed having published nothing"
    )
    assert isinstance(deadline, (int, float))
    # An absolute time.time() instant, bounded by the budget it must fit
    # in — AND still in the future when the fetch is invoked, with a usable
    # window. `0 < deadline <= budget` admitted `deadline=1`, an instant
    # already long gone by the time the snapshot runs near t=budget: the
    # real fetch would then skip every uncached artifact and publish an
    # incomplete result while this assertion stayed green.
    at = float(seen["at"])  # type: ignore[arg-type]
    assert deadline <= budget, (
        f"the snapshot's deadline ({deadline}) is outside the {budget}s "
        f"wall-clock budget"
    )
    assert deadline - at >= _mod._REQUEST_TIMEOUT, (
        f"the snapshot invoked the fetch at t={at} with a deadline of "
        f"{deadline} — only {deadline - at}s of usable window, less than a "
        f"single {_mod._REQUEST_TIMEOUT}s request, so every uncached "
        "artifact is skipped"
    )


def test_poll_ceiling_reserves_time_for_the_shutdown_snapshot(monkeypatch):
    """F6: the cutoff must fire BEFORE the budget, not at it.

    Starting the snapshot at t=budget leaves it racing the Actions job
    deadline. Active polling must stop a reserve early so the snapshot's
    serial API calls fit inside the remaining wall clock.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: True)
    # The job state must CHANGE during the run, or the cutoff's body equals
    # the pre-pause snapshot, is deduped, and the "last write" is the t=0
    # one — which passes no matter where the ceiling is.
    cycles = {"n": 0}

    def progressing(*a, **k):
        cycles["n"] += 1
        if cycles["n"] <= 1:
            return [{"name": "Python tests", "status": "in_progress",
                     "html_url": "u"}], False
        return [{"name": "Python tests", "status": "completed",
                 "conclusion": "failure", "html_url": "u"}], True

    monkeypatch.setattr(_mod, "collect_run_jobs", progressing)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses",
                        lambda *a, **k: [])

    budget = 1200.0
    # A LITERAL bound, not `min(_SHUTDOWN_RESERVE_SECONDS, budget/2)`. The
    # derived form moved with the code: zeroing the constant slid both the
    # cutoff and this expectation to t=budget and stayed green (measured: 68
    # passed). The contract is "polling stops a real window before the
    # budget", so assert a window the snapshot can actually use.
    min_reserve = 60.0

    snapshot_times: list[float] = []
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(_mod.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))
    monkeypatch.setattr(
        _mod, "upsert_comment",
        lambda t, r, p, body, comment_id=None: snapshot_times.append(clock["t"]) or 1)

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False,
             max_wall_seconds=int(budget))

    assert len(snapshot_times) >= 2, (
        f"expected a pre-pause write and a cutoff write, got {snapshot_times}"
    )
    last = snapshot_times[-1]
    assert last <= budget - min_reserve, (
        f"the last write happened at t={last:.0f}s of a {budget:.0f}s budget; "
        f"only {budget - last:.0f}s remained for it, against a "
        f"{min_reserve:.0f}s minimum reserve — it is racing the job deadline"
    )


# ─── round 11: the deadline/reserve model ─────────────────────────────


def test_an_unknown_group_is_never_published_as_an_empty_result(
        monkeypatch, tmp_path):
    """F2: a deadline skip with no carried value is UNKNOWN, not empty.

    Converting it to ``[]`` returned an apparently-successful partial
    result; the final snapshot then treated it as authoritative and
    republished the comment with that group's review section deleted.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 1, "name": "review-status-lint", "archive_download_url": "u1"},
        {"id": 2, "name": "review-status-osv", "archive_download_url": "u2"},
    ])
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])

    def download(token, repo, artifact, dest, deadline=None):
        clock["t"] += 60.0
        return Path(f"/nonexistent/{artifact['id']}.json")

    monkeypatch.setattr(_mod, "_download_artifact", download)
    monkeypatch.setattr(
        _mod, "_parse_status_file",
        lambda p: [{"source": f"s{p.stem}", "results": []}])

    # Room for exactly one download; the second group is never read.
    out = _mod.fetch_all_review_statuses("tok", "o/r", "77", deadline=30.0)

    assert [s["source"] for s in out] == ["s1"], (
        f"expected only the group that was actually read, got {out}"
    )
    assert _mod.STATE.fetch_incomplete is True, (
        "a group skipped by the deadline with no carried value was reported "
        "as a complete result — the caller will publish it as authoritative "
        "and delete that section from the comment"
    )


def test_final_snapshot_keeps_known_statuses_when_the_fetch_is_partial(
        monkeypatch):
    """F2 SEAM: the snapshot must ACT on fetch_incomplete.

    A partial fetch returns a real list, so only the flag distinguishes it
    from a complete one. Without reading it, the snapshot republishes the
    partial view as the last word.
    """
    bodies: list[str] = []
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))
    monkeypatch.setattr(
        _mod, "upsert_comment",
        lambda t, r, p, body, comment_id=None: bodies.append(body) or 1)

    calls = {"n": 0}

    def fetch(token, repo, run_id, deadline=None):
        calls["n"] += 1
        if calls["n"] == 1:
            _mod.STATE.fetch_incomplete = False
            return [{"source": "lint", "results": [
                {"kind": "warning", "title": "lint",
                 "summary": "a rendered review section"}]}]
        # Every later fetch, including the shutdown snapshot's, is partial.
        _mod.STATE.fetch_incomplete = True
        return []

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", fetch)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        _mod.time, "sleep",
        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False, max_wall_seconds=1200)

    assert bodies, "the poller never published anything"
    assert "a rendered review section" in bodies[-1], (
        "the final snapshot republished a PARTIAL artifact fetch as "
        "authoritative and deleted the known review section"
    )


def test_artifact_requests_are_bounded_by_the_remaining_deadline(monkeypatch):
    """F5: a download must not START a full-length request near the cutoff.

    Checking the deadline only before a download lets a hop begin one
    second early and run a further 30-60s, eating the window the final
    PATCH needs.
    """
    timeouts: list[float] = []
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])

    class _Opener:
        def open(self, req, timeout=None):
            timeouts.append(timeout)
            raise _http_error(302, {"Location": "https://blob/x"})

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _Opener())
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: timeouts.append(k.get("timeout")) or _Resp({}, {}))

    artifact = {"id": 1, "name": "review-status-lint",
                "archive_download_url": "https://api/x"}
    # 10s of room against a 30s API timeout and a 60s blob timeout.
    _mod._download_artifact("tok", "o/r", artifact, Path("/tmp"), deadline=10.0)

    assert timeouts, "no request was attempted"
    for t in timeouts:
        assert t is not None and t <= 10.0, (
            f"a request was given a {t}s socket timeout with only 10s left "
            f"before the deadline: {timeouts}"
        )


def test_an_expired_deadline_starts_no_artifact_request(monkeypatch):
    """F5: past the cutoff, do not open a connection at all."""
    clock = {"t": 100.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])

    def boom(*a, **k):
        raise AssertionError("a request was started past the deadline")

    monkeypatch.setattr(urllib.request, "build_opener", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    artifact = {"id": 1, "name": "review-status-lint",
                "archive_download_url": "https://api/x"}
    assert _mod._download_artifact(
        "tok", "o/r", artifact, Path("/tmp"), deadline=50.0) is None


def test_download_timeouts_come_from_named_constants(monkeypatch):
    """F9: the artifact path's timeouts must be gated, not hardcoded.

    ``_download_artifact`` used literal 30/60. Zeroing the module's timeout
    constants left it untouched, so the socket-timeout guard covered only
    ``_api_request``/``upsert_comment``.
    """
    monkeypatch.setattr(_mod, "_REQUEST_TIMEOUT", 7)
    monkeypatch.setattr(_mod, "_ARTIFACT_BLOB_TIMEOUT", 11)
    timeouts: list[float] = []

    class _Opener:
        def open(self, req, timeout=None):
            timeouts.append(timeout)
            raise _http_error(302, {"Location": "https://blob/x"})

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _Opener())
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: timeouts.append(k.get("timeout")) or _Resp({}, {}))

    artifact = {"id": 1, "name": "review-status-lint",
                "archive_download_url": "https://api/x"}
    _mod._download_artifact("tok", "o/r", artifact, Path("/tmp"))

    assert timeouts == [7, 11], (
        f"the artifact download ignored the timeout constants: {timeouts}"
    )


def test_a_failed_final_snapshot_is_retried_inside_the_reserve(monkeypatch):
    """F6: a transient failure on the last write must not end the run.

    The break followed the snapshot unconditionally, so a single failed
    PATCH discarded the whole remaining shutdown reserve and left the
    comment stale.
    """
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: True)
    cycles = {"n": 0}

    def progressing(*a, **k):
        cycles["n"] += 1
        if cycles["n"] <= 1:
            return [{"name": "Python tests", "status": "in_progress",
                     "html_url": "u"}], False
        return [{"name": "Python tests", "status": "completed",
                 "conclusion": "failure", "html_url": "u"}], True

    monkeypatch.setattr(_mod, "collect_run_jobs", progressing)
    monkeypatch.setattr(_mod, "fetch_all_review_statuses", lambda *a, **k: [])

    attempts = {"n": 0}
    published: list[float] = []

    def flaky_upsert(t, r, p, body, comment_id=None):
        attempts["n"] += 1
        if attempts["n"] == 2:      # the first shutdown-snapshot write
            return 0                # transient failure
        published.append(clock["t"])
        return 1

    monkeypatch.setattr(_mod, "upsert_comment", flaky_upsert)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        _mod.time, "sleep",
        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    budget = 1200
    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False,
             max_wall_seconds=budget)

    assert len(published) >= 2, (
        f"the failed final snapshot was never retried (writes at {published}); "
        "the shutdown reserve was discarded and the comment left stale"
    )
    assert published[-1] <= budget, (
        f"a retry ran to t={published[-1]}s, past the {budget}s budget"
    )


def test_active_polling_passes_the_poll_ceiling_to_the_artifact_fetch(
        monkeypatch):
    """F8: the last active cycle must not eat the shutdown reserve.

    Only the snapshot passed a deadline. A cycle starting just before the
    ceiling could serially try every uncached artifact at 30-60s each,
    spend the whole reserve, and be SIGKILLed before the final write.
    """
    seen: list[object] = []

    def spy_fetch(token, repo, run_id, deadline=None):
        seen.append(deadline)
        return []

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", spy_fetch)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        _mod.time, "sleep",
        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    budget = 1200
    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False,
             max_wall_seconds=budget)

    # seen[0] is the FIRST active poll cycle, long before the snapshot.
    assert seen, "the poller never fetched artifacts"
    first = seen[0]
    assert first is not None, (
        "ordinary polling fetched artifacts with NO deadline — one cycle "
        "starting near the ceiling can consume the entire shutdown reserve"
    )
    expected_ceiling = budget - _mod._SHUTDOWN_RESERVE_SECONDS
    assert first == expected_ceiling, (
        f"the active cycle's deadline is {first}, not the poll ceiling "
        f"({expected_ceiling}) — it does not stop work at the cutoff"
    )


# ─── round-11: gates for the seams round 10 introduced ────────────────


def test_the_snapshot_fetch_cutoff_reserves_a_window_for_the_patch(monkeypatch):
    """F5 SEAM: the fetch cutoff must sit BEFORE the budget end.

    A fetch deadline equal to the budget end only bounds when a download
    may start; the PATCH that follows then inherits whatever a last hop
    left behind. Measured: `_PATCH_RESERVE_SECONDS = 0` and hardcoding the
    cutoff to the budget end BOTH left the whole file green — the sibling
    deadline test only requires one `_REQUEST_TIMEOUT` of window, which a
    zero reserve still satisfies.
    """
    seen: dict[str, object] = {}

    def spy_fetch(token, repo, run_id, deadline=None):
        seen["deadline"] = deadline
        return []

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", spy_fetch)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)
    monkeypatch.setattr(_mod, "upsert_comment",
                        lambda t, r, p, body, comment_id=None: 1)

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        _mod.time, "sleep",
        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    budget = 1200
    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False,
             max_wall_seconds=budget)

    deadline = seen.get("deadline")
    assert deadline is not None, "the snapshot fetched with no deadline"
    # A deliberate literal, NOT read back from _PATCH_RESERVE_SECONDS: the
    # point is that SOME real window is held back for the write, so moving
    # the constant moves the code without moving this assertion.
    slack = budget - float(deadline)  # type: ignore[arg-type]
    assert slack >= 30, (
        f"the snapshot's artifact fetch may run until t={deadline} of a "
        f"{budget}s budget, leaving only {slack}s for the final PATCH; a "
        "download that starts just before the cutoff can consume it and the "
        "comment is never written"
    )


def test_the_final_snapshot_retry_window_stops_before_the_budget_ends(
        monkeypatch):
    """F6 SEAM: retries must not spend the PATCH reserve itself.

    Measured: widening `retry_until` to the budget end left the file green
    — the existing retry test only proves that A retry happens, not that
    the retry loop stops early enough for the attempt it launches to land.
    """
    attempts: list[float] = []
    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        _mod.time, "sleep",
        lambda s: clock.__setitem__("t", clock["t"] + max(s, 1.0)))

    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))
    monkeypatch.setattr(_mod, "fetch_all_review_statuses",
                        lambda *a, **k: [])
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    def always_failing_upsert(t, r, p, body, comment_id=None):
        attempts.append(clock["t"])
        raise _mod.RateLimitError(0.0)

    monkeypatch.setattr(_mod, "upsert_comment", always_failing_upsert)

    budget = 1200
    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False,
             max_wall_seconds=budget)

    assert len(attempts) >= 2, (
        f"the failed final snapshot was not retried: {attempts}")
    # The LAST attempt must still have a full request's worth of room before
    # the budget ends, or it is launched only to be SIGKILLed mid-flight.
    last = attempts[-1]
    assert budget - last >= 30, (
        f"the final retry was launched at t={last} of a {budget}s budget, "
        f"{budget - last}s before the deadline — the PATCH it issues cannot "
        "complete, so the retry loop spent the reserve it was meant to use"
    )


def test_artifact_download_hops_carry_bounded_socket_timeouts(monkeypatch):
    """F9: the socket-timeout guard must cover the DOWNLOAD path too.

    `test_api_calls_carry_a_socket_timeout` only observes `_api_request`
    and `upsert_comment`. Measured: `_ARTIFACT_BLOB_TIMEOUT = 0` left the
    whole file green, and urllib treats `timeout=0` as a non-blocking
    socket that fails instantly — every artifact download would silently
    drop its section.
    """
    timeouts: list[object] = []

    class _Opener:
        def open(self, req, timeout=None):
            timeouts.append(timeout)
            raise _http_error(302, {"Location": "https://blob/x"})

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _Opener())
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: timeouts.append(k.get("timeout")) or _Resp({}, {}))

    artifact = {"id": 1, "name": "review-status-lint",
                "archive_download_url": "https://api/x"}
    # No deadline: the UNCLAMPED, shipped values must themselves be sane.
    _mod._download_artifact("tok", "o/r", artifact, Path("/tmp"))

    assert len(timeouts) >= 2, (
        f"expected a redirect hop and a blob hop, saw {timeouts}")
    for t in timeouts:
        assert isinstance(t, (int, float)) and not isinstance(t, bool), (
            f"an artifact download hop used a non-numeric timeout: {timeouts}")
        assert 5 <= t <= 300, (
            f"an artifact download hop used a {t}s socket timeout; 0 is a "
            f"non-blocking socket that fails instantly and huge values hang: "
            f"{timeouts}"
        )


def test_bounded_timeout_never_returns_an_unusable_socket_timeout():
    """`_bounded_timeout`'s floor is the guard, not an optimisation.

    The clamp exists so a request cannot outlive the deadline, but the
    clamped value is still handed to `socket.settimeout`. Without the
    floor a deadline 0.2s away yields a 0.2s socket timeout — in practice
    an instant failure, which reads as "the artifact is unavailable" and
    silently drops the section. Measured: dropping `max(1.0, ...)` left
    the whole file green.

    The expectations are deliberate literals, not read back from the
    module's constants.
    """
    import time as _time

    now = _time.time()

    # Deadline comfortably far away: the caller's own base wins, unclamped.
    assert _mod._bounded_timeout(30.0, now + 600) == 30.0

    # Deadline nearer than the base: clamped DOWN, but still usable.
    tight = _mod._bounded_timeout(30.0, now + 0.2)
    assert tight is not None, (
        "a deadline 0.2s away returned None; there is still room to try")
    assert tight >= 1.0, (
        f"_bounded_timeout handed a {tight}s socket timeout to a real "
        "request; anything under a second fails effectively instantly and "
        "reads as an unavailable artifact rather than a timeout")

    # Deadline already gone: refuse to start at all.
    assert _mod._bounded_timeout(30.0, now - 1) is None, (
        "a spent deadline must return None so the hop is not started")

    # No deadline at all: unchanged.
    assert _mod._bounded_timeout(30.0, None) == 30.0


def test_a_spent_deadline_stops_the_blob_hop_not_just_the_first_hop(
        monkeypatch, tmp_path):
    """Both download hops must refuse to start once the deadline is gone.

    The redirect hop's `None` check was gated; the blob hop's was not.
    Measured: replacing the blob hop's `if blob_timeout is None: return
    None` with a fall-back to the unclamped constant left the whole file
    green — a 60s bulk transfer could then start AFTER the shutdown cutoff
    and eat the window the final PATCH needs.
    """
    import time as _time

    blob_opens: list[object] = []

    class _Opener:
        def open(self, req, timeout=None):
            # Hop 1 succeeds (302) but consumes the entire window, so the
            # blob hop is reached with the deadline already spent.
            clock["t"] = deadline + 5
            raise _http_error(302, {"Location": "https://blob/x"})

    clock = {"t": _time.time()}
    deadline = clock["t"] + 10
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _Opener())
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: blob_opens.append(k.get("timeout")))

    artifact = {"id": 1, "name": "review-status-lint",
                "archive_download_url": "https://api/x"}
    result = _mod._download_artifact(
        "tok", "o/r", artifact, tmp_path, deadline=deadline)

    assert result is None
    assert blob_opens == [], (
        f"the blob hop started {len(blob_opens)}s past a spent deadline "
        f"with timeout(s) {blob_opens}; it must not begin at all")


def test_the_final_retry_backoff_is_a_real_pause(monkeypatch):
    """The retry pause must actually pause.

    `_FINAL_RETRY_BACKOFF` bounds how hard the shutdown reserve is spun.
    Measured: setting it to 0 left the whole file green, which is a busy
    loop hammering the very API that just rate-limited us — for the whole
    reserve. It must also stay clamped to the room left, so the last
    sleep cannot overshoot the retry window.
    """
    sleeps: list[float] = []
    attempts: list[float] = []
    clock = {"t": 0.0}
    phase = {"final": False}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])

    def fake_sleep(s):
        # Only the pauses INSIDE the shutdown-retry loop are the subject;
        # the poll loop's own interval sleeps sit in the same range and
        # would mask a zero retry backoff entirely.
        if phase["final"]:
            sleeps.append(s)
        clock["t"] += max(s, 1.0)

    monkeypatch.setattr(_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))

    def fetch(*a, **k):
        # The snapshot path is the only caller that passes a deadline; use
        # that as the phase marker rather than any budget constant.
        if k.get("deadline") is not None:
            phase["final"] = True
        return []

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", fetch)
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    def always_failing_upsert(t, r, p, body, comment_id=None):
        attempts.append(clock["t"])
        raise _mod.RateLimitError(0.0)

    monkeypatch.setattr(_mod, "upsert_comment", always_failing_upsert)

    budget = 1200
    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False,
             max_wall_seconds=budget)

    retry_sleeps = sleeps
    assert retry_sleeps, (
        "the shutdown-reserve retry loop never paused between attempts")
    # Deliberate literals: a pause short enough to be a busy loop is the
    # defect, and one long enough to swallow the reserve is the other.
    assert max(retry_sleeps) <= 60, (
        f"a retry pause of {max(retry_sleeps)}s can swallow the whole "
        f"shutdown reserve: {retry_sleeps}")
    assert min(retry_sleeps) >= 1, (
        f"a retry paused {min(retry_sleeps)}s between attempts — that is a "
        f"busy loop against an API that just rate-limited us: {retry_sleeps}")
    assert len(attempts) >= 2, f"no retry was attempted: {attempts}"


# ─── residual gates (t_a66612e1): constants and seams left ungated ────
#
# Every test below was RED-proved against its own single-behaviour mutant
# of scripts/ci/live_comment.py, applied to the real file, with the tree
# asserted byte-identical afterwards. Baseline before these tests: all ten
# mutants ran the full tests/ci suite at 350 passed, i.e. nine of the ten
# sites were completely unconstrained (the tenth,
# _REVIEW_STATUS_ARTIFACT_PREFIX, was already gated by 8 tests).
#
# Expectations here are deliberate literals. They are NEVER read back from
# the constant under test — a test that compares a constant to itself is
# green in every possible world, which is exactly how _FINAL_RETRY_BACKOFF
# went vacuous when it was first written.


def test_a_deadline_cut_download_marks_the_cycle_partial(
        monkeypatch, tmp_path):
    """Site C: a shutdown-truncated read must not be reported as complete.

    When a download is started inside the deadline but the deadline is
    spent by the time it returns empty-handed, the group's state is
    UNKNOWN-because-we-ran-out-of-time, not UNKNOWN-because-the-artifact-
    does-not-exist. Only the first sets ``fetch_incomplete``, and only
    that flag stops ``_publish_final_snapshot`` treating the result as
    authoritative and deleting the section permanently.

    Measured: the site was unconstrained in BOTH directions — deleting
    the ``skipped = True`` assignment left 350 passed, and widening it to
    fire on ANY failed download also left 350 passed.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 1, "name": "review-status-lint", "archive_download_url": "u1"},
    ])

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])

    def download_that_overruns(token, repo, artifact, dest, deadline=None):
        # Started while there was still room, returns after the cutoff.
        clock["t"] = 120.0
        return None

    monkeypatch.setattr(_mod, "_download_artifact", download_that_overruns)

    out = _mod.fetch_all_review_statuses("tok", "o/r", "77", deadline=30.0)

    assert out == []
    assert _mod.STATE.fetch_incomplete is True, (
        "a download cut short by the shutdown deadline was reported as a "
        "COMPLETE result; the final snapshot will republish it as "
        "authoritative and delete that review section for good"
    )


def test_an_unavailable_artifact_is_not_blamed_on_the_deadline(
        monkeypatch, tmp_path):
    """Site C, the other direction: don't over-report partial either.

    ``fetch_incomplete`` means \"we ran out of time to look\". A download
    that fails while the deadline is still comfortably in the future is a
    plain unavailable artifact, and marking the cycle partial there makes
    the poller permanently refuse to publish on a run where one producer
    never uploads. Widening the condition to any failed download was
    measured at 350 passed, so nothing named this direction.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 1, "name": "review-status-lint", "archive_download_url": "u1"},
    ])

    clock = {"t": 0.0}
    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    # Fails immediately, with the whole window still ahead of it.
    monkeypatch.setattr(
        _mod, "_download_artifact",
        lambda token, repo, artifact, dest, deadline=None: None)

    out = _mod.fetch_all_review_statuses("tok", "o/r", "77", deadline=600.0)

    assert out == []
    assert _mod.STATE.fetch_incomplete is False, (
        "an artifact that simply is not available yet was reported as a "
        "deadline-truncated read; the snapshot will then never treat any "
        "cycle as authoritative on a run where one producer never uploads"
    )


def test_a_run_with_no_artifacts_clears_a_stale_partial_flag(monkeypatch):
    """The no-artifacts branch must RESET ``fetch_incomplete``.

    ``STATE`` outlives a cycle. A cycle that ends partial, followed by a
    cycle whose listing legitimately returns no review-status artifacts,
    leaves the stale flag set — and the snapshot then discards a perfectly
    good empty result and republishes whatever it saw before. Measured:
    deleting ``STATE.fetch_incomplete = False`` left 350 passed.
    """
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 1, "name": "some-other-artifact", "archive_download_url": "u"},
    ])

    _mod.STATE.fetch_incomplete = True        # residue of an earlier cycle

    out = _mod.fetch_all_review_statuses("tok", "o/r", "77")

    assert out == []
    assert _mod.STATE.fetch_incomplete is False, (
        "a clean cycle with no review-status artifacts left the previous "
        "cycle's partial flag set; every later snapshot is then treated as "
        "non-authoritative forever"
    )


def test_two_artifact_names_carrying_one_source_render_once(
        monkeypatch, tmp_path):
    """The by-source dedupe must survive.

    Distinct artifact NAMES can carry the same ``source`` — a producer
    renamed across a rerun uploads under a new name while the old one has
    not expired. Both groups are newest-of-their-name, so the name-level
    selection cannot suppress either; only the by-source dedupe does.
    Measured: returning ``all_statuses`` directly left 350 passed, because
    every other artifact test uses one source per name.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 20, "name": "review-status-lint-v2",
         "archive_download_url": "u20"},
        {"id": 10, "name": "review-status-lint", "archive_download_url": "u10"},
    ])
    monkeypatch.setattr(
        _mod, "_download_artifact",
        lambda token, repo, artifact, dest, deadline=None: Path(
            f"/nonexistent/{artifact['id']}.json"))
    monkeypatch.setattr(
        _mod, "_parse_status_file",
        lambda p: [{"source": "lint", "results": [
            {"kind": "warning", "title": f"from-{p.stem}", "summary": "s"}]}])

    out = _mod.fetch_all_review_statuses("tok", "o/r", "77")

    sources = [s.get("source") for s in out]
    assert sources == ["lint"], (
        f"the same source was published {len(sources)} times ({sources}); a "
        "renamed producer's old and new artifacts both rendered, so the "
        "comment shows the section twice"
    )


def test_an_older_artifact_cannot_overwrite_a_newer_carry(
        monkeypatch, tmp_path):
    """The ``artifact_name_ids`` guard must be a real comparison.

    ``artifacts_by_name`` is the value carried across a transient failure.
    If an OLDER id may overwrite it, this sequence poisons the carry: the
    newest artifact is read successfully, then it is deleted/expires, then
    a later cycle reads the surviving older artifact and installs its
    superseded result as the carried value. Measured: replacing the
    ``prev_id is None or newest_id >= prev_id`` guard with ``if True`` left
    350 passed.
    """
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", tmp_path)
    monkeypatch.setattr(
        _mod, "_download_artifact",
        lambda token, repo, artifact, dest, deadline=None: Path(
            f"/nonexistent/{artifact['id']}.json"))

    payloads = {
        "20": [{"source": "lint", "results": [
            {"kind": "ok", "title": "lint", "summary": "the rerun passed"}]}],
        "10": [{"source": "lint", "results": [
            {"kind": "failure", "title": "lint",
             "summary": "the superseded attempt failed"}]}],
    }
    monkeypatch.setattr(_mod, "_parse_status_file", lambda p: payloads[p.stem])

    listings = [
        # Cycle 1: the rerun's artifact is present and readable.
        [{"id": 20, "name": "review-status-lint", "archive_download_url": "u20"},
         {"id": 10, "name": "review-status-lint", "archive_download_url": "u10"}],
        # Cycle 2: id 20 is gone (deleted / expired); only the older one is
        # listed, so it is the newest of its name and IS read.
        [{"id": 10, "name": "review-status-lint", "archive_download_url": "u10"}],
    ]
    calls = {"n": 0}

    def listing(*a, **k):
        out = listings[min(calls["n"], len(listings) - 1)]
        calls["n"] += 1
        return list(out)

    monkeypatch.setattr(_mod, "_list_artifacts", listing)

    _mod.fetch_all_review_statuses("tok", "o/r", "77")
    _mod.fetch_all_review_statuses("tok", "o/r", "77")

    carried = _mod.STATE.artifacts_by_name.get("review-status-lint")
    summaries = [r.get("summary")
                 for s in (carried or []) for r in s.get("results", [])]
    assert summaries == ["the rerun passed"], (
        f"the carried value for the name is now {summaries}; an OLDER "
        "artifact overwrote the newer attempt's result, so a later "
        "transient failure would republish a superseded failure"
    )
    assert _mod.STATE.artifact_name_ids.get("review-status-lint") == 20, (
        "the recorded id for the name moved BACKWARDS to the older artifact"
    )


def test_the_final_retry_never_starts_an_attempt_past_its_window(monkeypatch):
    """Item 5: the post-sleep ``break`` is NOT behaviour-neutral.

    The card recorded this mutation as measured-equivalent. Re-measured
    here against the real module at the shipped constants (budget 1200s,
    ``_PATCH_RESERVE_SECONDS`` 60, so ``retry_until`` is t=1140), driving
    the loop with an attempt that costs wall-clock time:

        attempt cost   shipped: last attempt   no break: last attempt
        0.0s           t=1135.00               t=1140.00
        0.3s           t=1138.50               t=1140.00 (ends 1140.30)
        1.2s           t=1135.60               t=1140.00 (ends 1141.20)

    Without the break the loop always starts one more PATCH at exactly
    ``retry_until`` — the instant the PATCH reserve begins. That is the
    whole point of the reserve: the retry window stops so the final write
    has a window of its own. The equivalence claim only held because the
    earlier measurement used a zero-cost attempt AND read the clock rather
    than which attempts were started.

    Expectations are literals: no attempt may begin at or after t=1140 of
    a 1200s budget.
    """
    clock = {"t": 0.0}
    phase = {"final": False}
    attempts: list[float] = []

    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        _mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    def fetch(*a, **k):
        # The snapshot path is the only caller passing a deadline.
        if k.get("deadline") is not None:
            phase["final"] = True
        return []

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", fetch)

    def failing_upsert(t, r, p, body, comment_id=None):
        if phase["final"]:
            attempts.append(clock["t"])
            clock["t"] += 1.2      # a real PATCH is not instantaneous
            return 0
        return 1

    monkeypatch.setattr(_mod, "upsert_comment", failing_upsert)

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False, max_wall_seconds=1200)

    assert len(attempts) >= 2, f"the snapshot was never retried: {attempts}"
    assert attempts[-1] < 1140, (
        f"a retry attempt started at t={attempts[-1]:.2f}s of a 1200s budget, "
        "at or past the t=1140s cutoff that opens the 60s PATCH reserve — "
        "the retry loop is spending the window the final write needs"
    )


def test_the_final_retry_pause_cannot_overrun_its_window(monkeypatch):
    """Item 6: the ``min()`` clamp on the retry pause is NOT neutral.

    Also recorded as measured-equivalent. Re-measured against the real
    module at the shipped constants, with an attempt that costs 1.2s:

        shipped:  59 attempts, last at t=1135.60, loop ends t=1140.00
        no clamp: 59 attempts, last at t=1135.60, loop ends t=1141.80

    Same attempts, but the unclamped final sleep runs 1.8s past
    ``retry_until`` and eats that much of the PATCH reserve. The earlier
    equivalence measurement used a zero-cost attempt, where the loop's
    stride divides the window exactly and the last sleep happens to land
    on the boundary; any non-zero attempt cost breaks the alignment.

    Asserted as a literal: the loop must not still be sleeping after
    t=1140 of a 1200s budget.
    """
    clock = {"t": 0.0}
    phase = {"final": False}
    sleep_ends: list[float] = []

    monkeypatch.setattr(_mod.time, "time", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s
        if phase["final"]:
            sleep_ends.append(clock["t"])

    monkeypatch.setattr(_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(_mod, "collect_run_jobs", lambda *a, **k: (
        [{"name": "Python tests", "status": "in_progress", "html_url": "u"}],
        False))
    monkeypatch.setattr(_mod, "pr_is_in_merge_queue", lambda *a, **k: False)

    def fetch(*a, **k):
        if k.get("deadline") is not None:
            phase["final"] = True
        return []

    monkeypatch.setattr(_mod, "fetch_all_review_statuses", fetch)

    def failing_upsert(t, r, p, body, comment_id=None):
        if phase["final"]:
            clock["t"] += 1.2
            return 0
        return 1

    monkeypatch.setattr(_mod, "upsert_comment", failing_upsert)

    _mod.run(token="t", repo="o/r", run_id="1", pr_number="5", run_url="u",
             interval=45, timeout=100000, dry_run=False, max_wall_seconds=1200)

    assert sleep_ends, "the retry loop never paused"
    assert max(sleep_ends) <= 1140, (
        f"a retry pause ran to t={max(sleep_ends):.2f}s of a 1200s budget, "
        "past the t=1140s cutoff that opens the 60s PATCH reserve — the "
        "pause is not clamped to the room left in the retry window"
    )


def test_the_redirect_hop_is_counted_against_the_budget(monkeypatch, tmp_path):
    """Every charged request must appear in the budget accounting.

    The per-cycle budget line printed by the poll loop is the only
    instrument that diagnosed the original incident. The artifact
    redirect hop IS a charged API request (it authenticates to
    ``api.github.com`` and returns a 302), so a hop missing from the count
    understates the cycle's real cost and hides the next storm. Measured:
    dropping ``STATE.charged += 1`` from the 302 branch left 350 passed.
    """
    class _Opener:
        def open(self, req, timeout=None):
            raise _http_error(302, {"Location": "https://blob/x"})

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _Opener())
    # The blob hop is not an API request and must NOT be charged.
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: _Resp({}, {}))

    artifact = {"id": 1, "name": "review-status-lint",
                "archive_download_url": "https://api/x"}
    before = _mod.STATE.charged
    _mod._download_artifact("tok", "o/r", artifact, tmp_path)

    assert _mod.STATE.charged - before == 1, (
        f"the artifact redirect hop charged "
        f"{_mod.STATE.charged - before} requests against the budget, not 1; "
        "the per-cycle budget line then understates the real cost and the "
        "rate-limit incident it exists to diagnose stays invisible"
    )


def test_artifact_staging_is_confined_to_the_named_temp_base(
        monkeypatch, tmp_path):
    """Downloads must stage under ``_ARTIFACT_TEMP_BASE``, not anywhere.

    Every artifact test redirects this constant, so none of them observes
    where production actually writes. Measured: repointing it at a
    different directory left 350 passed. The constant is load-bearing
    twice over — it is the one seam that keeps the suite out of the real
    shared ``/tmp``, and it is what makes the run-scoped subdirectory
    predictable.
    """
    staged = tmp_path / "staging-base"
    monkeypatch.setattr(_mod, "_ARTIFACT_TEMP_BASE", staged)
    monkeypatch.setattr(_mod, "_list_artifacts", lambda *a, **k: [
        {"id": 7, "name": "review-status-lint", "archive_download_url": "u7"},
    ])

    dests: list[Path] = []

    def download(token, repo, artifact, dest, deadline=None):
        dests.append(Path(dest))
        return None

    monkeypatch.setattr(_mod, "_download_artifact", download)

    _mod.fetch_all_review_statuses("tok", "o/r", "77")

    assert dests, "no artifact download was attempted"
    # Deliberate literal for the run-scoped layer: <temp base>/<run id>.
    assert dests[0] == staged / "77", (
        f"artifacts were staged in {dests[0]} instead of "
        f"{staged / '77'} — the download path ignores _ARTIFACT_TEMP_BASE, "
        "so the tests write into the real shared /tmp"
    )
    assert staged.is_dir(), (
        "the staging directory under _ARTIFACT_TEMP_BASE was never created"
    )


def test_the_artifact_staging_base_is_an_absolute_scratch_path():
    """The shipped ``_ARTIFACT_TEMP_BASE`` value, not a redirected one.

    The test above proves the download path ROUTES through the constant,
    but it monkeypatches the value — as every artifact test does — so
    nothing observes what production is actually configured with.
    Measured: changing the constant to a RELATIVE path left 360 passed.
    That is not cosmetic: the poller runs with its cwd inside the repo
    checkout, so a relative base stages artifact zips into the working
    tree, where they land in ``git status`` and in any path-walking gate.

    Properties, not a frozen literal (AGENTS.md: behaviour contracts over
    snapshots) — the directory may be renamed, but it must stay an
    absolute scratch path with a segment of its own rather than a bare
    system temp root shared with every other process.
    """
    base = _mod._ARTIFACT_TEMP_BASE

    assert base.is_absolute(), (
        f"_ARTIFACT_TEMP_BASE is {base!r}, a RELATIVE path — artifact zips "
        "are staged inside the repo checkout the poller runs from"
    )
    # Deliberate literals: the scratch roots a CI runner actually provides.
    assert base.parts[1] in ("tmp", "var", "private", "home", "runner"), (
        f"_ARTIFACT_TEMP_BASE is {base!r}, which is not under a scratch "
        "root; artifact staging must not write into a persistent location"
    )
    assert len(base.parts) >= 3, (
        f"_ARTIFACT_TEMP_BASE is {base!r} — staging directly in a shared "
        "temp root collides with every other process on the runner"
    )


def test_every_api_url_is_built_from_the_github_api_base():
    """``API_BASE`` must be the real GitHub API host, used everywhere.

    Measured: repointing ``API_BASE`` at another host left 350 passed —
    every test that exercises a URL stubs the transport, so no test ever
    observes the host that production actually talks to. A wrong or
    partially-applied base sends an installation token to a host that is
    not GitHub.

    The expected host is a deliberate literal; the source is scanned so a
    NEW hardcoded ``api.github.com`` call site cannot bypass the constant.
    """
    src = _PATH.read_text(encoding="utf-8")

    assert 'API_BASE = "https://api.github.com"' in src, (
        "API_BASE is no longer the GitHub API host; the poller's "
        "installation token would be sent somewhere else"
    )

    # Exactly one occurrence of the literal host: the constant itself.
    # Any other is a call site that bypasses the constant.
    assert src.count("api.github.com") == 1, (
        f"the literal API host appears {src.count('api.github.com')} times; "
        "a call site is hardcoding it instead of using API_BASE, so the "
        "constant no longer governs where requests go"
    )
