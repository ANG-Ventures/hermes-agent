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


def test_artifacts_are_downloaded_once_per_id(monkeypatch):
    """Artifact content is immutable per id; re-downloading is pure waste."""
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


def test_failed_artifact_download_is_not_cached(monkeypatch):
    """An artifact that is not uploaded yet must be retried next cycle."""
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
    """A drifting default silently restores the storm for any other caller."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=45)
    assert parser.parse_args([]).interval == 45
    import inspect
    sig = inspect.signature(_mod.run)
    assert sig.parameters["interval"].default == 45
