"""Relay fleet/attribution from PR heads to merge-group check runs.

The workflow's job is the check on the group SHA; this script exits nonzero
unless every queue entry represented by the candidate commits is attributed.
"""
import json
import os
import sys
import urllib.error
import urllib.request


CONTEXT = "fleet/attribution"


def batch_members(entries, commit_shas, head_sha):
    commits = set(commit_shas)
    if head_sha not in commits:
        raise ValueError("group head absent from comparison")
    matched = [e for e in entries if e.get("headCommit") and e["headCommit"].get("oid") in commits]
    if not matched or not any(e["headCommit"]["oid"] == head_sha for e in matched):
        raise ValueError("group head absent from merge queue")
    members = []
    for entry in matched:
        pr = entry.get("pullRequest") or {}
        if not isinstance(pr.get("number"), int) or not pr.get("headRefOid"):
            raise ValueError("incomplete queue entry")
        members.append((pr["number"], pr["headRefOid"]))
    return members


def has_success(combined_status):
    """True if a combined-status response carries a success fleet/attribution.

    The response belongs to the SHA named in the request URL; status objects
    carry no sha of their own, so the association is never read from the body.
    """
    return any(s.get("context") == CONTEXT and s.get("state") == "success"
               for s in combined_status["statuses"])


def api(path, token, payload=None):
    request = urllib.request.Request(
        "https://api.github.com/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def verify(repo, base_sha, head_sha, token):
    owner, name = repo.split("/", 1)
    query = '''query($owner:String!, $name:String!) {
      repository(owner:$owner, name:$name) {
        mergeQueue(branch:"main") { entries(first:100) {
          pageInfo { hasNextPage }
          nodes { headCommit { oid } pullRequest { number headRefOid } }
        } }
      }
    }'''
    result = api("graphql", token, {"query": query, "variables": {"owner": owner, "name": name}})
    if result.get("errors"):
        raise ValueError("merge queue query failed: " + str(result["errors"]))
    queue = result["data"]["repository"]["mergeQueue"]["entries"]
    if queue["pageInfo"]["hasNextPage"]:
        raise ValueError("queue exceeds first 100 entries")
    comparison = api(f"repos/{repo}/compare/{base_sha}...{head_sha}?per_page=250", token)
    if comparison["total_commits"] != len(comparison["commits"]):
        raise ValueError("comparison truncated")
    members = batch_members(queue["nodes"], [c["sha"] for c in comparison["commits"]], head_sha)
    for number, sha in members:
        status = api(f"repos/{repo}/commits/{sha}/status?per_page=100", token)
        if not has_success(status):
            raise ValueError(f"PR #{number} head {sha} has no successful {CONTEXT} status")
        print(f"PR #{number} head {sha}: attributed")
    print(f"Group {head_sha}: {len(members)} attributed PR(s)")


if __name__ == "__main__":
    try:
        verify(os.environ["GITHUB_REPOSITORY"], os.environ["MERGE_GROUP_BASE_SHA"],
               os.environ["MERGE_GROUP_HEAD_SHA"], os.environ["GH_TOKEN"])
    except (ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        print(f"Attribution relay failed closed: {exc}", file=sys.stderr)
        sys.exit(1)
