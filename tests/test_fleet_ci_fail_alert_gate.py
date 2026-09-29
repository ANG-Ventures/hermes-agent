"""fleet-ci-fail-alert page gate (t_957f5398).

Executes the REAL ``route`` step script from the workflow YAML against run
payloads, so a regression in the YAML (not a copy of it) fails. #alerts pages
only for reds on the default branch, merge_group and schedule; worker-branch
and pull_request reds stay on the PR.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "fleet-ci-fail-alert.yml"

pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("jq")), reason="needs bash + jq")


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in data:  # YAML 1.1 parses `on` as True
        data["on"] = data.pop(True)
    return data


def _route_step() -> dict:
    steps = _workflow()["jobs"]["notify-on-failure"]["steps"]
    return next(s for s in steps if s.get("id") == "route")


# Hermetic stand-in for curl: answers the tag lookup from FAKE_TAG_CODE/FAKE_TAG_SHA
# and logs every URL, so no test touches the network.
_FAKE_CURL = r"""#!/bin/bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; https://*) url="$1" ;; esac; shift
done
echo "$url" >> "$FAKE_CURL_LOG"
case "$url" in
  */commits/refs/tags/*)
    [ "$FAKE_TAG_CODE" = error ] && exit 7
    printf '{"sha":"%s"}' "$FAKE_TAG_SHA" > "$out"; printf '%s' "$FAKE_TAG_CODE" ;;
  *) exit 22 ;;
esac
"""


def _route(tmp_path: Path, *, event: str, branch: str, conclusion: str = "failure",
           name: str = "CI", tag_code: str = "404", tag_sha: str = "", sha: str = "abc") -> dict:
    step = _route_step()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(_FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    log = tmp_path / "curl.log"
    log.write_text("")
    run = {"name": name, "workflow_id": 1, "id": 42, "head_branch": branch, "head_sha": sha,
           "conclusion": conclusion, "html_url": "https://x/42", "actor": {"login": "Kyzcreig"},
           "event": event}
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "FAKE_CURL_LOG": str(log), "FAKE_TAG_CODE": tag_code, "FAKE_TAG_SHA": tag_sha,
           "GH_TOKEN": "x", "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main",
           "REPLAY_RUN_ID": "", "RUN_JSON": json.dumps(run), "KNOWN_RED": step["env"]["KNOWN_RED"]}
    proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    got["_curl"] = log.read_text()
    return got


@pytest.mark.parametrize("event,branch", [
    ("push", "main"),
    ("workflow_dispatch", "main"),
    ("merge_group", "gh-readonly-queue/main/pr-1267-059d3994cfe01c8e455738138163b7fba9ad0174"),
    ("schedule", "main"),
])
def test_actionable_reds_page(tmp_path, event, branch):
    assert _route(tmp_path, event=event, branch=branch)["route"] == "alerts"


# t_05da39f4: release-tag reds have no PR, so they must page. Shapes taken from
# real upstream runs: Install & Update E2E 35985783676 (push, head_branch
# v2026.9.24) and Deploy Site 35985600660 (release, head_branch v2026.9.24).
TAG_SHA = "f97608f178d1ffeca59860195ab7da295f7c8e5f"


def test_tag_push_red_pages(tmp_path):
    got = _route(tmp_path, event="push", branch="v2026.9.24", sha=TAG_SHA,
                 tag_code="200", tag_sha=TAG_SHA)
    assert got["route"] == "alerts"
    assert got["_curl"].strip().endswith("/repos/o/r/commits/refs/tags/v2026.9.24")


def test_release_red_pages_without_lookup(tmp_path):
    got = _route(tmp_path, event="release", branch="v2026.9.24", sha=TAG_SHA, name="Deploy Site")
    assert got["route"] == "alerts" and got["_curl"] == ""


def test_push_branch_whose_name_is_a_tag_at_another_sha_stays_silent(tmp_path):
    got = _route(tmp_path, event="push", branch="v2026.9.24", sha="abc",
                 tag_code="200", tag_sha=TAG_SHA)
    assert got["route"] == "none"


def test_tag_lookup_error_fails_loud(tmp_path):
    for code in ("500", "error"):
        assert _route(tmp_path, event="push", branch="feature/x", tag_code=code)["route"] == "alerts"


def test_non_push_branch_red_never_queries_tags(tmp_path):
    got = _route(tmp_path, event="workflow_dispatch", branch="fix/x")
    assert got["route"] == "none" and got["_curl"] == ""


def test_startup_failure_on_main_pages(tmp_path):
    assert _route(tmp_path, event="push", branch="main", conclusion="startup_failure")["route"] == "alerts"


@pytest.mark.parametrize("event,branch", [
    ("workflow_dispatch", "fix/install-e2e-node-pty-gyp-flake"),  # the card's sample page
    ("pull_request", "daedalus/t_1234abcd"),
    ("pull_request", "main"),  # fork PR whose head branch is named main
    ("push", "daedalus/t_bc0052b9-THROWAWAY-repro"),
    ("push", "feature/x"),
])
def test_branch_reds_do_not_page(tmp_path, event, branch):
    assert _route(tmp_path, event=event, branch=branch)["route"] == "none"


def test_known_red_on_branch_never_queries_or_pages(tmp_path):
    got = _route(tmp_path, event="workflow_dispatch", branch="fix/x", name="Install & Update E2E")
    assert got["route"] == "none" and got["card"] == ""


def test_non_failure_conclusion_is_silent(tmp_path):
    assert _route(tmp_path, event="push", branch="main", conclusion="success")["route"] == "none"


def test_post_step_is_gated_on_route():
    steps = _workflow()["jobs"]["notify-on-failure"]["steps"]
    post = next(s for s in steps if s.get("name", "").startswith("Sign and POST"))
    assert post["if"] == "${{ steps.route.outputs.route != 'none' }}"


# --- C6 (FleetReview backfill, #1196) ------------------------------------

_FAKE_CURL_RUNS = r"""#!/bin/bash
url=""; out=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; https://*) url="$1" ;; esac; shift
done
echo "$url" >> "$FAKE_CURL_LOG"
case "$url" in
  */actions/workflows/*/runs) printf '%s' "$FAKE_RUNS" ;;
  *) exit 22 ;;
esac
"""


def test_known_red_predecessor_is_the_latest_COMPLETED_run_not_latest_created(tmp_path):
    """Overlapping runs: B (created later) failed, then A (created earlier)
    succeeded last. The run before this red one, by completion, is GREEN, so
    this red is a regression and must page #alerts, not #logs."""
    step = _route_step()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(_FAKE_CURL_RUNS)
    (bindir / "curl").chmod(0o755)
    out = tmp_path / "out"
    out.write_text("")
    runs = {"workflow_runs": [  # API order: created_at desc
        {"id": 41, "conclusion": "failure", "updated_at": "2026-09-27T10:05:00Z"},
        {"id": 40, "conclusion": "success", "updated_at": "2026-09-27T10:10:00Z"},
    ]}
    run = {"name": "Install & Update E2E", "workflow_id": 1, "id": 42, "head_branch": "main",
           "head_sha": "abc", "conclusion": "failure", "html_url": "https://x/42",
           "actor": {"login": "Kyzcreig"}, "event": "push"}
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "FAKE_CURL_LOG": str(tmp_path / "log"), "FAKE_RUNS": json.dumps(runs), "GH_TOKEN": "x",
           "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main",
           "REPLAY_RUN_ID": "", "RUN_JSON": json.dumps(run), "KNOWN_RED": step["env"]["KNOWN_RED"]}
    proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert got["route"] == "alerts", proc.stdout


_FAKE_CURL_POST = r"""#!/bin/bash
url=""; out=""; event=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -H) case "$2" in "X-GitHub-Event: "*) event="${2#X-GitHub-Event: }" ;; esac; shift ;;
    https://*) url="$1" ;;
  esac; shift
done
echo "$url $event" >> "$FAKE_CURL_LOG"
: > "$out"
case "$url" in
  *-known) code="$FAKE_KNOWN_CODE" ;;
  *) code="$FAKE_ALERTS_CODE" ;;
esac
# "transport": what real curl does on DNS/refused/timeout: writes 000, exits nonzero.
if [ "$code" = "transport" ]; then printf '000'; exit 7; fi
printf '%s' "$code"
"""


def _post(tmp_path, *, known_code, alerts_code):
    steps = _workflow()["jobs"]["notify-on-failure"]["steps"]
    post = next(s for s in steps if s.get("name", "").startswith("Sign and POST"))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(_FAKE_CURL_POST)
    (bindir / "curl").chmod(0o755)
    log = tmp_path / "post.log"
    log.write_text("")
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "FAKE_CURL_LOG": str(log),
           "FAKE_KNOWN_CODE": known_code, "FAKE_ALERTS_CODE": alerts_code,
           "ROUTE": "logs", "CARD": "t_bab6df79", "CI_FAIL_WEBHOOK_SECRET": "s",
           "WEBHOOK_URL": "https://hooks.example/webhooks/ci-fail", "WF_NAME": "Install & Update E2E",
           "WF_BRANCH": "main", "WF_SHA": "abc", "WF_RUN_ID": "42", "WF_URL": "https://x/42",
           "WF_ACTOR": "k", "REPO": "o/r", "WF_EVENT": "push", "EVENT_NAME": "workflow_run",
           "GITHUB_RUN_ID": "7"}
    proc = subprocess.run(["bash", "-c", post["run"]], env=env, capture_output=True, text=True, timeout=30)
    return proc, log.read_text().split("\n")


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_failed_known_red_delivery_falls_back_to_alerts(tmp_path):
    proc, calls = _post(tmp_path, known_code="404", alerts_code="200")
    assert proc.returncode == 0, proc.stderr
    assert calls[0] == "https://hooks.example/webhooks/ci-fail-known ci_failure_known_red"
    assert calls[1] == "https://hooks.example/webhooks/ci-fail ci_failure"


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_known_red_and_fallback_both_failing_turns_the_step_red(tmp_path):
    proc, _ = _post(tmp_path, known_code="500", alerts_code="502")
    assert proc.returncode != 0


# --- merge-queue dedupe (t_70f92e0b) -------------------------------------------
# Fake GitHub API: every URL is looked up by suffix in FAKE_API (a JSON file of
# {url-substring: response-json}); a miss exits 22 like `curl -f` on a 404.
_FAKE_API_CURL = r"""#!/bin/bash
url=""
for a in "$@"; do case "$a" in https://*) url="$a" ;; esac; done
echo "$url" >> "$FAKE_CURL_LOG"
python3 - "$url" "$FAKE_API" <<'PY'
import json, sys
url, path = sys.argv[1], sys.argv[2]
table = json.load(open(path))
for key in sorted(table, key=len, reverse=True):
    if key in url:
        print(json.dumps(table[key])); sys.exit(0)
sys.exit(22)
PY
"""

QTEST = "tests/agent/test_redact_json_leaf.py::test_no_serialize_then_redact_text_call_sites"


def _jobs(*failed):
    return {"jobs": [{"id": 900 + i, "name": n, "conclusion": "failure"} for i, n in enumerate(failed)]
            + [{"id": 1, "name": "Python tests / Tests complete", "conclusion": "failure"}]}


def _ann(*tests):
    return [{"message": "Process completed with exit code 1."}] + [
        {"message": f"{t}: FAILED (not quarantined)"} for t in tests]


def _queue_route(tmp_path, api: dict, *, run_id=42, pr="1328", branch="", subject="") -> dict:
    step = _route_step()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(_FAKE_API_CURL)
    (bindir / "curl").chmod(0o755)
    (tmp_path / "api.json").write_text(json.dumps(api))
    log = tmp_path / "curl.log"
    log.write_text("")
    run = {"name": "CI", "workflow_id": 7, "id": run_id, "head_sha": "abc", "conclusion": "failure",
           "head_branch": f"gh-readonly-queue/main/pr-{pr}-dbc864fd2687ae3a61a03856201bd012a2967d56",
           "html_url": "https://x/42", "actor": {"login": "ang-fleet-lander[bot]"}, "event": "merge_group"}
    if branch != "":
        run["head_branch"] = branch
    if subject:
        run["head_commit"] = {"message": subject}
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "FAKE_CURL_LOG": str(log), "FAKE_API": str(tmp_path / "api.json"),
           "GH_TOKEN": "x", "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main",
           "REPLAY_RUN_ID": "", "RUN_JSON": json.dumps(run), "KNOWN_RED": step["env"]["KNOWN_RED"]}
    proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    got["_stdout"] = proc.stdout
    return got


def _prior(*runs):
    now = "2099-01-01T00:00:00Z"  # always inside the 24h window
    # A run whose ref was deleted comes back with head_branch null (seen live on
    # 2026-09-27); it must not break the lookup into a fail-loud page.
    return {"workflow_runs": [{"id": 1, "created_at": now, "head_branch": None}]
            + [{"id": rid, "created_at": now,
                "head_branch": f"gh-readonly-queue/main/pr-{pr}-{sha}"} for rid, pr, sha in runs]}


def _api(current_tests, prior_runs=(), prior_tests=None, slice_="6/16"):
    api = {"actions/runs/42/jobs": _jobs(f"Python tests / Run tests slice {slice_}"),
           "check-runs/900/annotations": _ann(*current_tests), "check-runs/1/annotations": [],
           "actions/workflows/7/runs": _prior(*prior_runs)}
    for rid, _pr, _sha in prior_runs:
        api[f"actions/runs/{rid}/jobs"] = {"jobs": [{"id": rid * 10, "name": "Python tests / Run tests slice 5/16",
                                                     "conclusion": "failure"}]}
        api[f"check-runs/{rid * 10}/annotations"] = _ann(*(prior_tests or current_tests))
    return api


def test_first_queue_ejection_pages_naming_pr_and_test(tmp_path):
    got = _queue_route(tmp_path, _api([QTEST]))
    assert got["route"] == "alerts"
    assert got["pr"] == "1328"
    assert got["summary"] == f"PR #1328 ejected from the merge queue: {QTEST}"


def test_queue_retry_same_pr_same_test_is_silent(tmp_path):
    # pr-1332 on 2026-09-27: one broken test, 6 ejections, 6 pages. Only the first pages.
    # The test moved slice (6/16 -> 5/16) between runs; the signature must not care.
    got = _queue_route(tmp_path, _api([QTEST], prior_runs=[(30, "1328", "aaa")]))
    assert got["route"] == "none"
    assert "already failed the queue" in got["_stdout"]


def test_queue_retry_new_failing_test_still_pages(tmp_path):
    got = _queue_route(tmp_path, _api([QTEST], prior_runs=[(30, "1328", "aaa")], prior_tests=["tests/x.py::t"]))
    assert got["route"] == "alerts"


def test_other_pr_same_test_still_pages(tmp_path):
    # A prior failure of a DIFFERENT PR is not this PR's page; the lookup filters by PR.
    got = _queue_route(tmp_path, _api([QTEST], prior_runs=[(30, "1333", "aaa")]))
    assert got["route"] == "alerts"


def test_queue_dedupe_api_error_fails_loud(tmp_path):
    api = _api([QTEST], prior_runs=[(30, "1328", "aaa")])
    del api["actions/workflows/7/runs"]
    assert _queue_route(tmp_path, api)["route"] == "alerts"
    api = _api([QTEST])
    del api["actions/runs/42/jobs"]
    got = _queue_route(tmp_path, api)
    assert got["route"] == "alerts" and "failing job unknown" in got["summary"]


def test_queue_signature_falls_back_to_job_name_without_slice(tmp_path):
    got = _queue_route(tmp_path, _api([]))
    assert got["summary"] == "PR #1328 ejected from the merge queue: Python tests / Run tests"


def test_queue_retry_same_test_plus_new_unannotated_red_job_still_pages(tmp_path):
    # FleetReview #1375 afff7c56: a lint job going red next to an already-paged test
    # failure is a new fault; the signature must carry the unannotated job too.
    api = _api([QTEST], prior_runs=[(30, "1328", "aaa")])
    api["actions/runs/42/jobs"]["jobs"].append({"id": 950, "name": "Lint (ruff + ty) / ruff", "conclusion": "failure"})
    api["check-runs/950/annotations"] = [{"message": "Process completed with exit code 1."}]
    got = _queue_route(tmp_path, api)
    assert got["route"] == "alerts"
    assert got["summary"] == f"PR #1328 ejected from the merge queue: Lint (ruff + ty) / ruff; {QTEST}"


def _null_branch_prior(api, rid, pr):
    # FleetReview #1375 f6fcb4be: the queue ref is deleted, head_branch comes back
    # null (run 36354859649, 2026-09-27); the squash subject still names the PR.
    runs = api["actions/workflows/7/runs"]["workflow_runs"]
    for r in runs:
        if r["id"] == rid:
            r["head_branch"] = None
            r["head_commit"] = {"message": f"fix(x): thing (#{pr})\n\nbody (#999)"}
    return api


def test_queue_retry_prior_run_with_deleted_ref_is_still_recognized(tmp_path):
    api = _null_branch_prior(_api([QTEST], prior_runs=[(30, "1328", "aaa")]), 30, "1328")
    got = _queue_route(tmp_path, api)
    assert got["route"] == "none"
    assert "already failed the queue" in got["_stdout"]


def test_deleted_ref_prior_run_of_another_pr_still_pages(tmp_path):
    api = _null_branch_prior(_api([QTEST], prior_runs=[(30, "1333", "aaa")]), 30, "1333")
    assert _queue_route(tmp_path, api)["route"] == "alerts"


def test_current_run_with_deleted_ref_takes_pr_from_commit_subject(tmp_path):
    got = _queue_route(tmp_path, _api([QTEST]), branch=None, subject="fix(y): z (#1328)")
    assert got["route"] == "alerts"
    assert got["summary"] == f"PR #1328 ejected from the merge queue: {QTEST}"


def test_post_step_puts_pr_summary_on_first_line():
    steps = _workflow()["jobs"]["notify-on-failure"]["steps"]
    post = next(s for s in steps if s.get("name", "").startswith("Sign and POST"))
    assert post["env"]["SUMMARY"] == "${{ steps.route.outputs.summary }}"
    assert 'WF_NAME="${WF_NAME} — ${SUMMARY}"' in post["run"]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_known_red_transport_failure_still_falls_back_to_alerts(tmp_path):
    """FleetReview #1362 (4f3672bf): a transport-level failure (curl exits
    nonzero) must reach the #alerts fallback, not end the step under set -e."""
    proc, calls = _post(tmp_path, known_code="transport", alerts_code="200")
    assert proc.returncode == 0, proc.stderr
    assert calls[1] == "https://hooks.example/webhooks/ci-fail ci_failure"
    assert "#alerts fallback delivered" in proc.stdout


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_known_red_and_fallback_transport_failures_turn_the_step_red(tmp_path):
    proc, _ = _post(tmp_path, known_code="transport", alerts_code="transport")
    assert proc.returncode != 0
    assert "both failed (HTTP 000)" in proc.stderr
