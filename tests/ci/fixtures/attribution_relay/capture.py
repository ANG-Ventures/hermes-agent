"""Read-only capture of real GitHub responses for the attribution relay fixtures.

Every file is {"capture": {"command": ..., "captured_at": ...}, "response": <raw body>}.
Only GET requests (plus a read-only GraphQL query) are issued.
"""
import datetime
import json
import pathlib
import subprocess
import sys

OUT = pathlib.Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
AGENT = "ANG-Ventures/hermes-agent"
HOME = "ANG-Ventures/hermes-home"
QUEUE_QUERY = ('query{repository(owner:"ANG-Ventures",name:"hermes-agent"){mergeQueue(branch:"main")'
               '{entries(first:100){pageInfo{hasNextPage} nodes{headCommit{oid} pullRequest{number headRefOid}}}}}}')

RUN_FIELDS = "{id,name,event,head_branch,head_sha,status,conclusion,created_at}"
CAPTURES = {
    # combined-status endpoint the relay calls, on a head that carries a real
    # fleet/attribution=success posted by fleet-merge.sh (hermes-home#455).
    "status_attributed.json": ["gh", "api", f"repos/{HOME}/commits/c4e598efd751b623bb00f2d070338a2e48d15ad5/status?per_page=100"],
    # list endpoint for the same head (shape reference: status objects carry no sha).
    "statuses_attributed.json": ["gh", "api", f"repos/{HOME}/commits/c4e598efd751b623bb00f2d070338a2e48d15ad5/statuses?per_page=100"],
    # hermes-agent#955 head, queued without fleet/attribution.
    "status_unattributed.json": ["gh", "api", f"repos/{AGENT}/commits/a9809aa3709262449095dab7821f54b1628403d3/status?per_page=100"],
    "graphql_queue_955.json": ["gh", "api", "graphql", "-f", "query=" + QUEUE_QUERY],
    # single-entry live group gh-readonly-queue/main/pr-955-f2fffde3...
    "compare_group_955.json": ["gh", "api", f"repos/{AGENT}/compare/f2fffde3acca1bcf68533bf516c851d636cf743e...4956e38e1885f7d7b14e15e75120fbd9a161bbf1?per_page=250",
                               "--jq", "del(.files)"],
    # three consecutive queue merges (945, 947, 955) from main a7942d79.
    "compare_span_945_955.json": ["gh", "api", f"repos/{AGENT}/compare/a7942d7989e45ac26863188e7052e79bfe901893...4956e38e1885f7d7b14e15e75120fbd9a161bbf1?per_page=250",
                                  "--jq", "del(.files)"],
    # merge_group-triggered CI runs: head_sha is each entry's queue commit, and
    # head_branch carries base_sha (gh-readonly-queue/main/pr-<n>-<base_sha>).
    # Only the runs the tests join against (945, 947, 955 in the span; 829 whose
    # queue commit is the span base): unused runs add nothing and one carried a
    # SHA that gitleaks 8.18.4 misreads as a Square token.
    "merge_group_runs.json": ["gh", "api", f"repos/{AGENT}/actions/runs?event=merge_group&per_page=100",
                              "--jq", f"[.workflow_runs[] | select(.name == \"CI\" and (.head_branch | test(\"/pr-(829|945|947|955)-\"))) | {RUN_FIELDS}]"],
}

ONLY = set(sys.argv[2:])  # optional: recapture just these fixture names
for name, cmd in CAPTURES.items():
    if ONLY and name not in ONLY:
        continue
    raw = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    body = {"capture": {"command": " ".join(cmd),
                        "captured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "response": json.loads(raw)}
    (OUT / name).write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    print(name, len(raw))
