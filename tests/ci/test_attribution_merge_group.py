"""Fail-closed contracts for the merge-queue attribution relay."""
import importlib.util
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
