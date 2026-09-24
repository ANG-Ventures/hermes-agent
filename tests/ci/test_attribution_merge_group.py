"""Fail-closed contracts for the merge-queue attribution relay."""
import importlib.util
import runpy
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[2] / "scripts/ci/attribution_merge_group.py"
spec = importlib.util.spec_from_file_location("attribution_merge_group", PATH)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def entry(number, group_sha, pr_sha):
    return {"headCommit": {"oid": group_sha},
            "pullRequest": {"number": number, "headRefOid": pr_sha}}


def test_all_batch_members_required_not_just_last():
    entries = [entry(1, "a" * 40, "1" * 40),
               entry(2, "b" * 40, "2" * 40),
               entry(3, "c" * 40, "3" * 40)]
    assert relay.batch_members(entries, ["a" * 40, "b" * 40], "b" * 40) == [(1, "1" * 40), (2, "2" * 40)]


def test_unknown_or_missing_queue_commit_fails_closed():
    with pytest.raises(ValueError):
        relay.batch_members([entry(1, "a" * 40, "1" * 40)], ["x" * 40], "x" * 40)
    with pytest.raises(ValueError):
        relay.batch_members([], ["x" * 40], "x" * 40)


def test_status_must_be_success_for_exact_pr_head():
    assert relay.has_success([{"context": "fleet/attribution", "state": "success", "sha": "1" * 40}], "1" * 40)
    assert not relay.has_success([{"context": "fleet/attribution", "state": "success", "sha": "2" * 40}], "1" * 40)
    assert not relay.has_success([{"context": "fleet/attribution", "state": "pending", "sha": "1" * 40}], "1" * 40)
    assert not relay.has_success([], "1" * 40)


@pytest.mark.parametrize("missing", [None, 0, 1, 2])
def test_verify_requires_every_batch_head_status(monkeypatch, missing):
    commits = [str(i) * 40 for i in range(1, 4)]
    heads = [chr(ord("a") + i) * 40 for i in range(3)]
    entries = [entry(i + 1, heads[i], commits[i]) for i in range(3)]
    calls = []

    def fake_api(path, token, payload=None):
        calls.append(path)
        if path == "graphql":
            return {"data": {"repository": {"mergeQueue": {"entries": {
                "pageInfo": {"hasNextPage": False}, "nodes": entries}}}}}
        if "/compare/" in path:
            return {"total_commits": 3, "commits": [{"sha": sha} for sha in heads]}
        for i, sha in enumerate(commits):
            if path.endswith(f"/commits/{sha}/status"):
                return {"statuses": [] if i == missing else [
                    {"context": relay.CONTEXT, "state": "success", "sha": sha}]}
        raise AssertionError(f"unexpected API request: {path}")

    monkeypatch.setattr(relay, "api", fake_api)
    if missing is None:
        relay.verify("owner/repo", "base", heads[-1], "token")
        assert len([path for path in calls if path.endswith("/status")]) == 3
    else:
        with pytest.raises(ValueError, match=f"PR #{missing + 1} head"):
            relay.verify("owner/repo", "base", heads[-1], "token")
    assert calls[0] == "graphql"
    assert any("/compare/" in path for path in calls)


@pytest.mark.parametrize("defect", ["queue_overflow", "comparison_truncated", "graphql_errors"])
def test_verify_rejects_incomplete_or_failed_discovery(monkeypatch, defect):
    head = "a" * 40
    def fake_api(path, token, payload=None):
        if path == "graphql":
            result = {"data": {"repository": {"mergeQueue": {"entries": {
                "pageInfo": {"hasNextPage": defect == "queue_overflow"},
                "nodes": [entry(1, head, "1" * 40)]}}}}}
            if defect == "graphql_errors":
                return {"errors": [{"message": "no access"}]}
            return result
        if "/compare/" in path:
            return {"total_commits": 2 if defect == "comparison_truncated" else 1,
                    "commits": [{"sha": head}]}
        raise AssertionError("status lookup must not run after incomplete discovery")

    monkeypatch.setattr(relay, "api", fake_api)
    with pytest.raises(ValueError):
        relay.verify("owner/repo", "base", head, "token")


def test_main_exits_nonzero_when_api_unreachable(monkeypatch, capsys):
    import urllib.error
    import urllib.request

    def unavailable(*args, **kwargs):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("MERGE_GROUP_BASE_SHA", "base")
    monkeypatch.setenv("MERGE_GROUP_HEAD_SHA", "head")
    monkeypatch.setenv("GH_TOKEN", "token")
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(PATH), run_name="__main__")
    assert exc.value.code != 0
    assert "failed closed" in capsys.readouterr().err
