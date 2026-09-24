"""Fail-closed contracts for the merge-queue attribution relay.

Every GitHub response shape used here is RECORDED from the real API (see
tests/ci/fixtures/attribution_relay/*.json; each file carries its read-only
capture command). Tests only choose WHICH recorded body a request receives;
they never invent response fields.
"""
import copy
import importlib.util
import json
import runpy
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[2] / "scripts/ci/attribution_merge_group.py"
spec = importlib.util.spec_from_file_location("attribution_merge_group", PATH)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

FIXTURES = Path(__file__).resolve().parent / "fixtures/attribution_relay"
REPO = "ANG-Ventures/hermes-agent"


def recorded(name):
    return json.loads((FIXTURES / name).read_text())["response"]


STATUS_OK = recorded("status_attributed.json")          # real fleet/attribution=success
STATUS_NONE = recorded("status_unattributed.json")      # real head without the context
QUEUE_955 = recorded("graphql_queue_955.json")          # real single-entry queue
COMPARE_955 = recorded("compare_group_955.json")        # real single-entry group compare
COMPARE_SPAN = recorded("compare_span_945_955.json")    # real 3 consecutive queue commits
RUNS = recorded("merge_group_runs.json")                # real merge_group runs

# Real (PR number, PR head, queue commit) triples for 945, 947, 955, joined from
# recorded merge_group runs (head_sha) and the recorded compare span.
SPAN_BASE = "a7942d7989e45ac26863188e7052e79bfe901893"
MEMBERS = [(945, "2536b20729c6e8ff20a34f7aa820cb311ebc7ab2", "cfa22067fe4baa9ae861abc10d97a5b358fff80a"),
           (947, "88a1b30c5d05f7e234e88ef4efa1a83a191da072", "f2fffde3acca1bcf68533bf516c851d636cf743e"),
           (955, "a9809aa3709262449095dab7821f54b1628403d3", "4956e38e1885f7d7b14e15e75120fbd9a161bbf1")]
# Real queue entry OUTSIDE the span: #829's queue commit is SPAN_BASE itself (the
# recorded pr-829 merge_group run head_sha), head 02e5879a carries no statuses.
OUTSIDER = (829, "02e5879abc4494baca78862af7db0b3a3769a9d5", SPAN_BASE)


def test_recorded_fixtures_are_what_the_relay_assumes():
    # Combined-status objects carry no per-status sha (the r2 defect's premise).
    assert STATUS_OK["statuses"] and all("sha" not in s for s in STATUS_OK["statuses"])
    assert all("sha" not in s for s in recorded("statuses_attributed.json"))
    # The span is exactly the three queue commits, in order.
    assert [c["sha"] for c in COMPARE_SPAN["commits"]] == [m[2] for m in MEMBERS]
    assert COMPARE_SPAN["total_commits"] == len(COMPARE_SPAN["commits"])
    # merge_group run head_sha is each entry's queue commit; branch names the PR.
    by_pr = {r["head_branch"].split("/pr-")[1].split("-")[0]: r["head_sha"] for r in RUNS}
    for number, _, group_sha in MEMBERS + [OUTSIDER]:
        assert by_pr[str(number)] == group_sha
    # The outsider's queue commit is the span base, so compare(base...head) excludes it.
    assert OUTSIDER[2] not in {c["sha"] for c in COMPARE_SPAN["commits"]}
    node = QUEUE_955["data"]["repository"]["mergeQueue"]["entries"]["nodes"][0]
    assert (node["pullRequest"]["number"], node["pullRequest"]["headRefOid"],
            node["headCommit"]["oid"]) == MEMBERS[2]


def test_has_success_on_recorded_combined_status():
    assert relay.has_success(STATUS_OK)
    failed = copy.deepcopy(STATUS_OK)
    for s in failed["statuses"]:
        s["state"] = "failure"
    assert not relay.has_success(failed)
    pending = copy.deepcopy(STATUS_OK)
    for s in pending["statuses"]:
        s["state"] = "pending"
    assert not relay.has_success(pending)
    assert not relay.has_success(STATUS_NONE)
    other = copy.deepcopy(STATUS_OK)
    for s in other["statuses"]:
        s["context"] = "ci/other"
    assert other["statuses"] and all(s["state"] == "success" for s in other["statuses"])
    assert not relay.has_success(other)


def queue_of(members, has_next=False):
    """Recorded GraphQL body, with the recorded node repeated per real member."""
    body = copy.deepcopy(QUEUE_955)
    template = body["data"]["repository"]["mergeQueue"]["entries"]["nodes"][0]
    nodes = []
    for number, pr_head, group_sha in members:
        node = copy.deepcopy(template)
        node["pullRequest"]["number"] = number
        node["pullRequest"]["headRefOid"] = pr_head
        node["headCommit"]["oid"] = group_sha
        nodes.append(node)
    entries = body["data"]["repository"]["mergeQueue"]["entries"]
    entries["nodes"] = nodes
    entries["pageInfo"]["hasNextPage"] = has_next
    return body


def fake_github(queue, compare, attributed_heads, calls):
    def api(path, token, payload=None):
        calls.append(path)
        if path == "graphql":
            return queue
        if "/compare/" in path:
            return compare
        for _, pr_head, _ in MEMBERS + [OUTSIDER]:
            if path == f"repos/{REPO}/commits/{pr_head}/status?per_page=100":
                # Association comes from the request: the body is whichever
                # recorded response this head should receive.
                return STATUS_OK if pr_head in attributed_heads else STATUS_NONE
        raise AssertionError(f"unexpected API request: {path}")
    return api


@pytest.mark.parametrize("missing", [None, 0, 1, 2])
def test_verify_requires_every_batch_head_status(monkeypatch, missing):
    heads = {m[1] for i, m in enumerate(MEMBERS) if i != missing}
    calls = []
    monkeypatch.setattr(relay, "api", fake_github(queue_of(MEMBERS), COMPARE_SPAN, heads, calls))
    if missing is None:
        relay.verify(REPO, SPAN_BASE, MEMBERS[-1][2], "token")
        assert sorted(p for p in calls if p.endswith("per_page=100") and "/status" in p) == sorted(
            f"repos/{REPO}/commits/{m[1]}/status?per_page=100" for m in MEMBERS)
    else:
        with pytest.raises(ValueError, match=f"PR #{MEMBERS[missing][0]} head"):
            relay.verify(REPO, SPAN_BASE, MEMBERS[-1][2], "token")


def test_unattributed_queue_entry_outside_group_is_not_a_member(monkeypatch):
    # Queue also holds an unattributed entry whose commit is outside base...head:
    # it is not in this batch and must not turn the group red.
    calls = []
    monkeypatch.setattr(relay, "api", fake_github(queue_of([OUTSIDER] + MEMBERS), COMPARE_SPAN,
                                                  {m[1] for m in MEMBERS}, calls))
    relay.verify(REPO, SPAN_BASE, MEMBERS[-1][2], "token")
    assert not any(OUTSIDER[1] in p for p in calls)


@pytest.mark.parametrize("attributed", [True, False])
def test_verify_on_recorded_single_entry_group(monkeypatch, attributed):
    heads = {MEMBERS[2][1]} if attributed else set()
    monkeypatch.setattr(relay, "api", fake_github(QUEUE_955, COMPARE_955, heads, []))
    base = "f2fffde3acca1bcf68533bf516c851d636cf743e"
    if attributed:
        relay.verify(REPO, base, MEMBERS[2][2], "token")
    else:
        with pytest.raises(ValueError, match="PR #955 head"):
            relay.verify(REPO, base, MEMBERS[2][2], "token")


def test_group_head_not_in_queue_fails_closed(monkeypatch):
    monkeypatch.setattr(relay, "api", fake_github(queue_of(MEMBERS[:2]), COMPARE_SPAN,
                                                  {m[1] for m in MEMBERS}, []))
    with pytest.raises(ValueError, match="absent from merge queue"):
        relay.verify(REPO, SPAN_BASE, MEMBERS[-1][2], "token")


@pytest.mark.parametrize("defect", ["queue_overflow", "comparison_truncated", "graphql_errors"])
def test_verify_rejects_incomplete_or_failed_discovery(monkeypatch, defect):
    queue = queue_of(MEMBERS, has_next=defect == "queue_overflow")
    if defect == "graphql_errors":
        queue = {"errors": [{"message": "Resource not accessible by integration"}]}
    compare = copy.deepcopy(COMPARE_SPAN)
    if defect == "comparison_truncated":
        compare["commits"] = compare["commits"][1:]
    calls = []
    monkeypatch.setattr(relay, "api", fake_github(queue, compare, {m[1] for m in MEMBERS}, calls))
    with pytest.raises(ValueError):
        relay.verify(REPO, SPAN_BASE, MEMBERS[-1][2], "token")
    assert not any("/status" in p for p in calls)


def test_main_exits_nonzero_when_api_unreachable(monkeypatch, capsys):
    import urllib.error
    import urllib.request

    def unavailable(*args, **kwargs):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("MERGE_GROUP_BASE_SHA", SPAN_BASE)
    monkeypatch.setenv("MERGE_GROUP_HEAD_SHA", MEMBERS[-1][2])
    monkeypatch.setenv("GH_TOKEN", "token")
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(PATH), run_name="__main__")
    assert exc.value.code != 0
    assert "failed closed" in capsys.readouterr().err
