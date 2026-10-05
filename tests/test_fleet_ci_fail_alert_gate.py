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


def _script(tmp_path: Path, body: str) -> str:
    """Write a step's script to a file: run as `bash <file>`, not `bash -c <script>`.

    The suite's live-system guard parses every subprocess argv, and its cost grows
    with argv length squared; a ~20 KB inline script took ~10 s per call in CI.
    """
    path = tmp_path / "step.sh"
    path.write_text(body, encoding="utf-8")
    return str(path)


# Hermetic stand-in for curl: answers the tag lookup from FAKE_TAG_CODE/FAKE_TAG_SHA
# and logs every URL, so no test touches the network.
_FAKE_CURL = r"""#!/usr/bin/env bash
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
    proc = subprocess.run(["bash", _script(tmp_path, step["run"])], env=env, capture_output=True, text=True, timeout=30)
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

_FAKE_CURL_RUNS = r"""#!/usr/bin/env bash
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
    proc = subprocess.run(["bash", _script(tmp_path, step["run"])], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert got["route"] == "alerts", proc.stdout


_FAKE_CURL_POST = r"""#!/usr/bin/env bash
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
    proc = subprocess.run(["bash", _script(tmp_path, post["run"])], env=env, capture_output=True, text=True, timeout=30)
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


_FAKE_CURL_BODY = r"""#!/usr/bin/env bash
body=""; prev=""
for a in "$@"; do [ "$prev" = "--data" ] && body="$a"; prev="$a"; done
printf '%s\n' "$body" >> "$FAKE_CURL_LOG"
printf '200'
"""


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
@pytest.mark.parametrize("event,trigger,actor", [
    ("schedule", "scheduled run, not a merge", "cron"),
    ("merge_group", "merge queue", "github-merge-queue[bot]"),
    ("push", "merge/push to main", "github-merge-queue[bot]"),
])
def test_page_names_scheduled_vs_merge(tmp_path, event, trigger, actor):
    """t_b4265523: a scheduled red paged 'by github-merge-queue[bot] (schedule)' and read as a merge."""
    steps = _workflow()["jobs"]["notify-on-failure"]["steps"]
    post = next(s for s in steps if s.get("name", "").startswith("Sign and POST"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(_FAKE_CURL_BODY)
    (bindir / "curl").chmod(0o755)
    log = tmp_path / "bodies.log"
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "FAKE_CURL_LOG": str(log),
           "CI_FAIL_WEBHOOK_SECRET": "s", "WEBHOOK_URL": "https://hooks.example/webhooks/ci-fail",
           "WF_NAME": "Install & Update E2E", "WF_BRANCH": "main", "WF_SHA": "abc", "WF_RUN_ID": "42",
           "WF_URL": "https://x/42", "WF_ACTOR": "github-merge-queue[bot]", "REPO": "o/r",
           "WF_EVENT": event, "EVENT_NAME": "workflow_run", "GITHUB_RUN_ID": "7"}
    proc = subprocess.run(["bash", _script(tmp_path, post["run"])], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(log.read_text().splitlines()[0])
    assert body["trigger"] == trigger
    assert body["actor"] == actor


# --- merge-queue dedupe (t_70f92e0b) -------------------------------------------
# Fake GitHub API: every URL is looked up by suffix in FAKE_API (a JSON file of
# {url-substring: response-json}); a miss exits 22 like `curl -f` on a 404.
_FAKE_API_CURL = r"""#!/usr/bin/env bash
url=""
for a in "$@"; do case "$a" in https://*) url="$a" ;; esac; done
echo "$url | $*" >> "$FAKE_CURL_LOG"
python3 - "$url" "$FAKE_API" <<'PY'
import json, sys
url, path = sys.argv[1], sys.argv[2]
table = json.load(open(path))
for key in sorted(table, key=len, reverse=True):
    if key in url:
        data = table[key]
        if isinstance(data, dict) and "jobs" in data:  # paginate like GitHub (per_page default 30)
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(url).query)
            per, page = int(q.get("per_page", ["30"])[0]), int(q.get("page", ["1"])[0])
            data = {"total_count": data.get("total_count", len(data["jobs"])),
                    "jobs": data["jobs"][(page - 1) * per:page * per]}
        print(json.dumps(data)); sys.exit(0)
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
    proc = subprocess.run(["bash", _script(tmp_path, step["run"])], env=env, capture_output=True, text=True, timeout=60)
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


def test_red_queue_run_of_already_merged_pr_goes_to_logs(tmp_path):
    # hermes-agent#1652 2026-10-03: its own group run went red, the stacked group
    # behind it passed, both merged; "ejected" was false. It must not page.
    api = _api([QTEST])
    api["pulls/1328"] = {"merged": True}
    got = _queue_route(tmp_path, api)
    assert got["route"] == "logs" and got["card"] == "merged-anyway"
    assert got["summary"] == f"PR #1328 merged anyway; its own queue run was red: {QTEST}"


def test_red_queue_run_of_unmerged_pr_still_pages(tmp_path):
    api = _api([QTEST])
    api["pulls/1328"] = {"merged": False}
    assert _queue_route(tmp_path, api)["route"] == "alerts"
    del api["pulls/1328"]  # lookup error -> fail loud
    assert _queue_route(tmp_path, api)["route"] == "alerts"


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


# --- already-red fold (t_be1b9134) --------------------------------------------
# 2026-09-28 20:07-23:19Z: main flapped red and six PRs were ejected on the same
# two tests, 15 #alerts pages. The fixture is the recorded API state of every run
# behind those pages; each page is replayed through the REAL route step, in page
# order, with the runs list the API would have served (ids below the replayed one
# are filtered by the step itself). created_at is pinned inside the window.
R10 = json.loads((ROOT / "tests" / "fixtures" / "fleet_ci_fail_alert" / "r10_2026-09-28.json").read_text())
PROXY = "tests/ci/test_no_new_source_proxy_asserts.py::test_no_new_source_proxy_asserts"
KANBAN = "tests/hermes_cli/test_kanban_core_functionality.py::test_gateway_dispatcher_disables_corrupt_board_without_traceback"


def _r10_api() -> dict:
    wid = R10["runs"][0]["workflow_id"]
    listed = [dict(r, created_at="2099-01-01T00:00:00Z") for r in R10["runs"] if r["conclusion"] == "failure"]
    api = {f"actions/workflows/{wid}/runs?status=failure": {"workflow_runs": listed},
           f"actions/workflows/{wid}/runs?event=merge_group": {
               "workflow_runs": [r for r in listed if r["event"] == "merge_group"]}}
    for rid, jobs in R10["jobs"].items():
        api[f"actions/runs/{rid}/jobs"] = {"jobs": jobs}
    for cid, msgs in R10["annotations"].items():
        api[f"check-runs/{cid}/annotations"] = [{"message": m} for m in msgs]
    return api


def _replay(tmp_path: Path, run_id: int, api: dict) -> dict:
    step = _route_step()
    run = next(r for r in R10["runs"] if r["id"] == run_id)
    d = tmp_path / str(run_id)
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "curl").write_text(_FAKE_API_CURL)
    (d / "bin" / "curl").chmod(0o755)
    (d / "api.json").write_text(json.dumps(api))
    out = d / "out"
    out.write_text("")
    payload = dict(run, html_url=f"https://x/{run_id}", actor={"login": "github-merge-queue[bot]"})
    env = {"PATH": f"{d / 'bin'}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "FAKE_CURL_LOG": str(d / "curl.log"), "FAKE_API": str(d / "api.json"), "GH_TOKEN": "x",
           "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main", "REPLAY_RUN_ID": "",
           "RUN_JSON": json.dumps(payload), "KNOWN_RED": step["env"]["KNOWN_RED"]}
    proc = subprocess.run(["bash", _script(tmp_path, step["run"])], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    got["_stdout"] = proc.stdout
    return got


def test_replay_2026_09_28_folds_already_red_tests_to_logs(tmp_path):
    api = _r10_api()
    routes = {int(rid): _replay(tmp_path, int(rid), api) for _ts, rid, _pr in R10["pages"]}
    paged = sorted(rid for rid, g in routes.items() if g["route"] == "alerts")
    # Still paging, each a DIFFERENT fault or one the fold cannot prove is repeated:
    #   36475808846 main 9d81866f  Windows-only job (job-name signature)
    #   36476853851 main aa9e59a3  slice 13: all tests passed, pytest exited non-zero
    #   36477241100 PR #1453       first red of test_no_new_source_proxy_asserts
    #   36477804956 main 51e9b286  slice 3: segfault in test_usageless_response_accounting
    #   36496636054 main ebf35a80  kanban test red but NOT annotated (no quarantine list on base)
    assert paged == [36475808846, 36476853851, 36477241100, 36477804956, 36496636054]
    folded = {rid: g for rid, g in routes.items() if g["route"] == "logs"}
    assert len(folded) == 10 and len(routes) == 15
    for rid, g in folded.items():
        assert g["card"].startswith("already-red (run "), (rid, g)
    # every ejection after the first proxy-asserts page folds, naming the PR
    ejected = {g["pr"] for g in folded.values() if g["pr"]}
    assert ejected == {"1426", "1453", "1458", "1459", "1463", "1464"}
    assert routes[36489068867]["summary"].startswith("PR #1463 ejected on an already-red test (also failed run ")
    # main 553943bd failed only the kanban test, already red on PR #1426's queue run; 71546f7d then on 553943bd
    for rid, anchor in ((36490159001, 36489800880), (36494384491, 36490159001)):
        assert routes[rid]["route"] == "logs" and routes[rid]["card"] == f"already-red (run {anchor})"
        assert routes[rid]["summary"].startswith(f"main red on an already-red test (also failed run {anchor})")
    # PR #1453's 23:14 run failed both tests: two different runs cover them, both named
    assert routes[36496092766]["card"].count(",") == 1


def test_fold_needs_every_failing_test_already_red(tmp_path):
    # PR #1453's 23:14 run failed BOTH tests; drop the kanban test from every earlier run and it pages.
    api = _r10_api()
    for key, anns in api.items():
        if key.startswith("check-runs/"):
            api[key] = [a for a in anns if KANBAN not in a["message"]]
    api["check-runs/" + str(next(j["id"] for j in R10["jobs"]["36496092766"] if "16/16" in j["name"])) + "/annotations"] = (
        [{"message": f"{KANBAN}[guard]: FAILED (not quarantined)"}])
    assert _replay(tmp_path, 36496092766, api)["route"] == "alerts"


def test_fold_lookup_error_pages(tmp_path):
    api = _r10_api()
    del api[next(k for k in api if k.endswith("runs?status=failure"))]
    assert _replay(tmp_path, 36490221633, api)["route"] == "alerts"


def test_fold_ignores_the_same_prs_own_earlier_runs(tmp_path):
    # Only another PR (or main) proves the test is not this PR's defect. Keep only PR #1463's own
    # 22:04 kanban run as a candidate: its 23:00 proxy run must not fold on it.
    api = _r10_api()
    key = next(k for k in api if k.endswith("runs?status=failure"))
    api[key]["workflow_runs"] = [r for r in api[key]["workflow_runs"] if "/pr-1463-" in (r["head_branch"] or "")]
    assert _replay(tmp_path, 36494513399, api)["route"] == "alerts"



def _other_pr_queue_api(*, extra_job=None):
    # PR #1333's queue run already failed QTEST; PR #1328 now fails it too.
    api = _api([QTEST], prior_runs=[(30, "1333", "aaa")])
    for r in api["actions/workflows/7/runs"]["workflow_runs"]:
        r["event"] = "merge_group"
    if extra_job:
        api["actions/runs/42/jobs"]["jobs"].append({"id": 950, "name": extra_job, "conclusion": "failure"})
        api["check-runs/950/annotations"] = [{"message": "Process completed with exit code 1."}]
    return api


def test_fold_other_prs_queue_run_on_same_test_goes_to_logs(tmp_path):
    got = _queue_route(tmp_path, _other_pr_queue_api())
    assert got["route"] == "logs" and got["card"] == "already-red (run 30)", got["_stdout"]


def test_fold_blocked_by_an_extra_unannotated_red_job(tmp_path):
    # FleetReview #1470 b3b3252a79bb: an old test red plus a NEW red job with no
    # FAILED annotation (Windows-only, JS & TS) is a new fault and must page.
    got = _queue_route(tmp_path, _other_pr_queue_api(extra_job="OS-specific tests / Windows-only tests"))
    assert got["route"] == "alerts", got["_stdout"]
    assert "Windows-only tests" in got["summary"]



def test_fold_never_covers_a_job_name_even_if_it_failed_before(tmp_path):
    # A job name (no node id) cannot prove the same fault, so it blocks the fold even
    # when the other PR's run failed the same job.
    api = _other_pr_queue_api(extra_job="OS-specific tests / Windows-only tests")
    api["actions/runs/30/jobs"]["jobs"].append({"id": 951, "name": "OS-specific tests / Windows-only tests",
                                                "conclusion": "failure"})
    api["check-runs/951/annotations"] = [{"message": "Process completed with exit code 1."}]
    got = _queue_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]

def test_fold_matches_deleted_ref_queue_run_by_squash_subject(tmp_path):
    # head_branch null once the queue ref is deleted: PR comes from "(#N)" in the subject.
    api = _null_branch_prior(_other_pr_queue_api(), 30, "1333")
    got = _queue_route(tmp_path, api)
    assert got["route"] == "logs" and got["card"] == "already-red (run 30)", got["_stdout"]


# --- CI placement probe (t_c58e7200) ----------------------------------------
# ci-speed-lint R5: the probe is workflow_dispatch-only, so this listener is its
# notifier. Bench waves (dispatched by ang-fleet-workers[bot]) are graded and paged
# by the bench itself; a hand-dispatched red pages once per wave.
PROBE = "CI placement probe"


def test_probe_is_listened_to():
    assert PROBE in _workflow()["on"]["workflow_run"]["workflows"]


def _probe_route(tmp_path, *, actor="Kyzcreig", title="placement probe hand1 #0", prior=None,
                 branch="main") -> dict:
    step = _route_step()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(_FAKE_API_CURL)
    (bindir / "curl").chmod(0o755)
    api = {} if prior is None else {"actions/workflows/9/runs": {"workflow_runs": prior}}
    (tmp_path / "api.json").write_text(json.dumps(api))
    log = tmp_path / "curl.log"
    log.write_text("")
    run = {"name": PROBE, "workflow_id": 9, "id": 500, "head_branch": branch, "head_sha": "abc",
           "conclusion": "failure", "html_url": "https://x/500", "actor": {"login": actor},
           "event": "workflow_dispatch", "display_title": title}
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "FAKE_CURL_LOG": str(log), "FAKE_API": str(tmp_path / "api.json"),
           "GH_TOKEN": "x", "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main",
           "REPLAY_RUN_ID": "", "RUN_JSON": json.dumps(run), "KNOWN_RED": step["env"]["KNOWN_RED"]}
    proc = subprocess.run(["bash", _script(tmp_path, step["run"])], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    got["_curl"] = log.read_text()
    got["_stdout"] = proc.stdout
    return got


_NOW = "2099-01-01T00:00:00Z"  # always inside the 24h window


def test_probe_hand_dispatched_red_pages(tmp_path):
    got = _probe_route(tmp_path, prior=[])
    assert got["route"] == "alerts"


def test_probe_bench_wave_red_is_silent_without_api_calls(tmp_path):
    got = _probe_route(tmp_path, actor="ang-fleet-workers[bot]", title="placement probe n20260928T0050-burst #3")
    assert got["route"] == "none" and got["_curl"] == ""


def _prior_probe(run_id, title, *, actor="Kyzcreig", branch="main", created=_NOW):
    return {"id": run_id, "created_at": created, "display_title": title,
            "actor": {"login": actor}, "head_branch": branch}


def test_probe_second_red_of_same_wave_is_silent(tmp_path):
    prior = [_prior_probe(499, "placement probe hand1 #1")]
    got = _probe_route(tmp_path, prior=prior)
    assert got["route"] == "none" and "already paged (run 499)" in got["_stdout"]


def test_probe_red_of_other_or_longer_wave_still_pages(tmp_path):
    prior = [{"id": 499, "created_at": _NOW, "display_title": "placement probe hand10 #0"},
             {"id": 498, "created_at": _NOW, "display_title": "placement probe other #0"},
             {"id": 501, "created_at": _NOW, "display_title": "placement probe hand1 #2"},  # later run
             {"id": 497, "created_at": "2000-01-01T00:00:00Z", "display_title": "placement probe hand1 #3"}]
    assert _probe_route(tmp_path, prior=prior)["route"] == "alerts"


def test_probe_dedupe_api_error_fails_loud(tmp_path):
    assert _probe_route(tmp_path, prior=None)["route"] == "alerts"


def test_probe_red_on_worker_branch_stays_silent(tmp_path):
    got = _probe_route(tmp_path, branch="ci/placement-no-checkout-t_eb230c34", prior=[])
    assert got["route"] == "none"


# FleetReview #1482 (189d887c7b25): dedupe must only count prior reds that THIS
# route paged. A bot bench red or a worker-branch red of the same wave was
# silent, so it must not suppress the first paging-eligible red of that wave.
@pytest.mark.parametrize("prior_run", [
    _prior_probe(499, "placement probe hand1 #1", actor="ang-fleet-workers[bot]"),
    _prior_probe(499, "placement probe hand1 #1", branch="ci/placement-no-checkout-t_eb230c34"),
    {"id": 499, "created_at": _NOW, "display_title": "placement probe hand1 #1"},  # no actor/branch
])
def test_probe_prior_red_that_never_paged_does_not_suppress(tmp_path, prior_run):
    got = _probe_route(tmp_path, prior=[prior_run])
    assert got["route"] == "alerts", got["_stdout"]


def test_probe_prior_lookup_is_scoped_to_default_branch(tmp_path):
    got = _probe_route(tmp_path, prior=[])
    assert "branch=main" in got["_curl"]



# --- main still red (t_30d3de38) -----------------------------------------------
# 2026-10-04: the e2e-upgrade jobs (no pytest annotation, so the fold cannot see
# them) failed on main 6 times in 4 h and paged 6 times. A default-branch push red
# whose every failed job/step was already red in its latest earlier main run that
# RAN it goes to #logs. Path-classified skips are walked past; a pass pages.
E2E = "Python tests / e2e-upgrade (core/test_upgrade_path)"
E2E_STEP = "Run upgrade e2e tests (core/test_upgrade_path)"


def _main_jobs(*jobs):
    """jobs: (name, conclusion, {step: conclusion})"""
    return {"jobs": [{"id": 700 + i, "name": n, "conclusion": c,
                      "steps": [{"name": s, "conclusion": sc} for s, sc in steps.items()]}
                     for i, (n, c, steps) in enumerate(jobs)]
            + [{"id": 1, "name": "All required checks pass", "conclusion": "failure", "steps": []}]}


def _red_e2e():
    return _main_jobs((E2E, "failure", {"Set up job": "success", E2E_STEP: "failure"}))


def _main_api(history, *, current=None):
    """history: newest first, (run_id, created_at, conclusion, jobs)."""
    api = {"actions/runs/42/jobs": current or _red_e2e(), "/annotations": [],
           "runs?branch=main&event=push": {"workflow_runs": [
               {"id": rid, "created_at": ts, "conclusion": c, "head_branch": "main", "event": "push"}
               for rid, ts, c, _j in history]}}
    for rid, _ts, _c, jobs in history:
        if jobs is not None:
            api[f"actions/runs/{rid}/jobs"] = jobs
    return api


def _main_route(tmp_path, api, env_extra=None) -> dict:
    step = _route_step()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(_FAKE_API_CURL)
    (bindir / "curl").chmod(0o755)
    (tmp_path / "api.json").write_text(json.dumps(api))
    log = tmp_path / "curl.log"
    log.write_text("")
    run = {"name": "CI", "workflow_id": 7, "id": 42, "head_sha": "abc", "conclusion": "failure",
           "head_branch": "main", "event": "push", "created_at": "2026-10-04T05:00:00Z",
           "html_url": "https://x/42", "actor": {"login": "Kyzcreig"}}
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "FAKE_CURL_LOG": str(log), "FAKE_API": str(tmp_path / "api.json"),
           "GH_TOKEN": "x", "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main",
           "REPLAY_RUN_ID": "", "RUN_JSON": json.dumps(run), "KNOWN_RED": step["env"]["KNOWN_RED"],
           **(env_extra or {})}
    proc = subprocess.run(["bash", _script(tmp_path, step["run"])], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    got["_stdout"] = proc.stdout
    got["_curl"] = log.read_text()
    return got


def test_main_red_already_red_on_previous_main_run_goes_to_logs(tmp_path):
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())]))
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 41)", got["_stdout"]
    assert got["summary"] == f"main still red on the same failure as run(s) 41: {E2E} / {E2E_STEP}"
    # commit order: only runs created before this one are asked for
    assert "created=%3C%3D2026-10-04T05:00:00Z" in got["_curl"]


def test_main_red_after_the_step_passed_on_main_pages(tmp_path):
    green = _main_jobs((E2E, "success", {E2E_STEP: "success"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "success", green),
                     (40, "2026-10-04T03:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]
    assert "passed on main in run 41" in got["_stdout"]


def test_main_red_walks_past_runs_that_skipped_the_job(tmp_path):
    # path-classified CI: a green run that never ran e2e-upgrade proves nothing
    skipped = _main_jobs((E2E, "skipped", {}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "success", skipped),
                     (40, "2026-10-04T03:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 40)", got["_stdout"]


def test_main_red_ignores_a_later_created_run(tmp_path):
    # An OLDER commit's run that finishes after this one is not "main now"; a run
    # created AFTER this one (listed by the API) must not count either.
    green = _main_jobs((E2E, "success", {E2E_STEP: "success"}))
    api = _main_api([(43, "2026-10-04T06:00:00Z", "success", green),
                     (41, "2026-10-04T04:00:00Z", "failure", _red_e2e())])
    assert _main_route(tmp_path, api)["route"] == "logs"


def test_main_red_with_a_new_failing_step_pages(tmp_path):
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}),
                         ("Lint (ruff + ty) / ruff", "failure", {"ruff": "failure"}))
    lint_green = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}),
                            ("Lint (ruff + ty) / ruff", "success", {"ruff": "success"}))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", lint_green)], current=current))
    assert got["route"] == "alerts", got["_stdout"]


def test_main_red_with_no_proof_in_history_pages(tmp_path):
    # the first red of an episode: no earlier run failed the step
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "success",
                                            _main_jobs((E2E, "skipped", {})))]))
    assert got["route"] == "alerts", got["_stdout"]
    assert _main_route(tmp_path, _main_api([]))["route"] == "alerts"


def test_main_still_red_api_errors_page(tmp_path):
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", None)])  # prior jobs 404
    assert _main_route(tmp_path, api)["route"] == "alerts"
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())])
    del api["runs?branch=main&event=push"]
    assert _main_route(tmp_path, api)["route"] == "alerts"
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())])
    del api["actions/runs/42/jobs"]
    assert _main_route(tmp_path, api)["route"] == "alerts"


def test_main_still_red_never_applies_to_startup_failure(tmp_path):
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())])
    step = _route_step()
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(_FAKE_API_CURL)
    (bindir / "curl").chmod(0o755)
    (tmp_path / "api.json").write_text(json.dumps(api))
    run = {"name": "CI", "workflow_id": 7, "id": 42, "head_sha": "abc", "conclusion": "startup_failure",
           "head_branch": "main", "event": "push", "created_at": "2026-10-04T05:00:00Z",
           "html_url": "https://x/42", "actor": {"login": "Kyzcreig"}}
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "FAKE_CURL_LOG": str(tmp_path / "c.log"), "FAKE_API": str(tmp_path / "api.json"),
           "GH_TOKEN": "x", "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main",
           "REPLAY_RUN_ID": "", "RUN_JSON": json.dumps(run), "KNOWN_RED": step["env"]["KNOWN_RED"]}
    subprocess.run(["bash", _script(tmp_path, step["run"])], env=env, capture_output=True, text=True, timeout=60, check=True)
    assert "route=alerts" in out.read_text()


# --- truncated jobs (Prism P1 30c28a402d93, 2026-10-04) ------------------------------------------
# A run can carry > 100 jobs. Reading only the first page drops a failed job (a new red behind a
# known one goes to #logs or folds) or a job that passed (main looks still red). Every page is
# read; a list shorter than total_count pages.
def _filler(n):
    return [(f"filler {i}", "success", {"run": "success"}) for i in range(n)]


def test_main_red_new_failed_job_past_the_first_jobs_page_pages(tmp_path):
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), *_filler(120),
                         ("Lint (ruff + ty) / ruff", "failure", {"ruff": "failure"}))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=current))
    assert got["route"] == "alerts", got["_stdout"]
    assert "page=2" in got["_curl"]


def test_main_red_prior_pass_past_the_first_jobs_page_pages(tmp_path):
    green_late = _main_jobs(*_filler(120), (E2E, "success", {E2E_STEP: "success"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "success", green_late),
                     (40, "2026-10-04T03:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts" and "passed on main in run 41" in got["_stdout"], got["_stdout"]


def test_main_red_jobs_list_shorter_than_total_count_pages(tmp_path):
    current = _red_e2e()
    current["total_count"] = 150  # API says 150; only 2 jobs ever come back
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=current))
    assert got["route"] == "alerts", got["_stdout"]


def test_fold_blocked_by_an_extra_red_job_past_the_first_jobs_page(tmp_path):
    # Prior generation (FleetReview #1470): an extra unannotated red job blocks the fold. It must
    # still block when it sits on page 2 of the jobs list.
    api = _other_pr_queue_api()
    jobs = api["actions/runs/42/jobs"]["jobs"]
    jobs += [{"id": 2000 + i, "name": f"filler {i}", "conclusion": "success"} for i in range(120)]
    jobs.append({"id": 950, "name": "OS-specific tests / Windows-only tests", "conclusion": "failure"})
    api["check-runs/950/annotations"] = [{"message": "Process completed with exit code 1."}]
    got = _queue_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]
    assert "Windows-only tests" in got["summary"]


# --- cancelled runs are evidence too (Prism P1 daaf469b6e2e / eb4c30f2ca3e, 2026-10-04) ----------
def test_main_red_step_passed_in_a_cancelled_run_pages(tmp_path):
    # older run red, next run passed e2e but was cancelled on another job, now red again
    passed = _main_jobs((E2E, "success", {E2E_STEP: "success"}), ("Lint (ruff + ty) / ruff", "cancelled", {}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "cancelled", passed),
                     (40, "2026-10-04T03:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts" and "passed on main in run 41" in got["_stdout"], got["_stdout"]


def test_main_red_cancelled_run_that_never_finished_the_job_is_walked_past(tmp_path):
    cut = _main_jobs((E2E, "cancelled", {E2E_STEP: "cancelled"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "cancelled", cut),
                     (40, "2026-10-04T03:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 40)", got["_stdout"]


# --- a stalled or failing route never stops the page (Prism P1 cc0510af974c / 8c9d210ef980) ------
def test_route_step_is_bounded_and_cannot_skip_the_post():
    job = _workflow()["jobs"]["notify-on-failure"]
    route = _route_step()
    assert route.get("continue-on-error") is True
    assert 0 < route["timeout-minutes"] < job["timeout-minutes"]
    post = next(s for s in job["steps"] if s.get("name", "").startswith("Sign and POST"))
    assert "failure()" not in str(post.get("if", "")) and "steps.route.outcome" not in str(post.get("if", ""))


def test_main_still_red_api_calls_are_time_bounded(tmp_path):
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())]))
    calls = [line for line in got["_curl"].splitlines() if "api.github.com" in line]
    assert calls and all("--max-time" in c and "--connect-timeout" in c for c in calls), calls


def test_main_still_red_spent_budget_pages(tmp_path):
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())]),
                      env_extra={"ROUTE_BUDGET_S": "0"})
    assert got["route"] == "alerts", got["_stdout"]
    assert "actions/runs/" not in got["_curl"]


# --- a step that passed inside a cancelled / timed-out JOB is a recovery (Prism P1 cb7d09e76216) --
@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out"])
def test_main_red_step_passed_in_a_cancelled_job_pages(tmp_path, conclusion):
    passed = _main_jobs((E2E, conclusion, {E2E_STEP: "success", "later step": conclusion}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", conclusion, passed),
                     (40, "2026-10-04T03:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts" and "passed on main in run 41" in got["_stdout"], got["_stdout"]


def test_main_red_cancelled_job_that_never_reached_the_step_is_walked_past(tmp_path):
    cut = _main_jobs((E2E, "cancelled", {"Set up job": "success"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "cancelled", cut),
                     (40, "2026-10-04T03:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 40)", got["_stdout"]


# --- Prism round 4 on e9718495 (t_1a8e095b) ------------------------------------------------------
# f-abb7efb339835ba6: a job that failed with NO failed step (job timeout, runner lost) is a stepless
# item [job, ""]. It is the same fault only if the earlier run's job ALSO failed with no failed step;
# an earlier failure on a named step is a different fault and must page.
def test_main_red_stepless_failure_after_a_named_step_failure_pages(tmp_path):
    hang = _main_jobs((E2E, "failure", {"Set up job": "success", E2E_STEP: "cancelled"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=hang)
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]
    assert got["summary"] == "" and "different fault" in got["_stdout"], got["_stdout"]


def test_main_red_stepless_failure_after_a_stepless_failure_goes_to_logs(tmp_path):
    hang = _main_jobs((E2E, "failure", {"Set up job": "success", E2E_STEP: "cancelled"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", hang)], current=hang)
    got = _main_route(tmp_path, api)
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 41)", got["_stdout"]
    assert got["summary"] == f"main still red on the same failure as run(s) 41: {E2E}"


# f-ac6b7f1a201ac052: a job-level timed_out is a failure. A new timed-out job next to a known red
# must page, and a prior timed-out step is red evidence, not "did not run".
def test_main_red_new_timed_out_job_next_to_a_known_red_pages(tmp_path):
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}),
                         ("Python tests / Run tests slice 3/16", "timed_out", {"Run tests": "cancelled"}))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=current))
    assert got["route"] == "alerts", got["_stdout"]


def test_main_red_prior_timed_out_step_is_red_evidence(tmp_path):
    timed = _main_jobs((E2E, "timed_out", {E2E_STEP: "timed_out"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "timed_out", timed)],
                    current=_main_jobs((E2E, "timed_out", {E2E_STEP: "timed_out"})))
    got = _main_route(tmp_path, api)
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 41)", got["_stdout"]


def test_fold_blocked_by_an_extra_timed_out_job(tmp_path):
    # sig_of feeds the queue dedupe and the already-red fold: a timed-out job must enter SIG.
    api = _other_pr_queue_api(extra_job="OS-specific tests / Windows-only tests")
    api["actions/runs/42/jobs"]["jobs"][-1]["conclusion"] = "timed_out"
    got = _queue_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]
    assert "Windows-only tests" in got["summary"]


# f-96b4792f11201e3e: two jobs with one display name cannot be told apart by name. Pick-first could
# match the wrong one, so a duplicate failed name in this run, or in the prior run, pages.
def test_main_red_duplicate_failed_job_name_in_this_run_pages(tmp_path):
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), (E2E, "failure", {E2E_STEP: "failure"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=current)
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]


def test_main_red_duplicate_job_name_in_the_prior_run_pages(tmp_path):
    prior = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), (E2E, "success", {E2E_STEP: "success"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts" and "not unique" in got["_stdout"], got["_stdout"]


# --- Prism round 5 on 6117082a (t_1a8e095b) ------------------------------------------------------
# "Ambiguous steps" (:389): two steps with one name in a job cannot be told apart by name. A failed
# step name that repeats in this run's job, or in the prior run's job, pages.
def test_main_red_duplicate_failed_step_name_in_this_run_pages(tmp_path):
    current = {"jobs": [{"id": 700, "name": E2E, "conclusion": "failure",
                         "steps": [{"name": E2E_STEP, "conclusion": "failure"},
                                   {"name": E2E_STEP, "conclusion": "failure"}]}]}
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=current))
    assert got["route"] == "alerts", got["_stdout"]


def test_main_red_duplicate_step_name_in_the_prior_run_pages(tmp_path):
    # prior: first "Test" failed, the second (if: always()) passed; now the second fails too
    prior = {"jobs": [{"id": 700, "name": E2E, "conclusion": "failure",
                       "steps": [{"name": E2E_STEP, "conclusion": "failure"},
                                 {"name": E2E_STEP, "conclusion": "success"}]}]}
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts" and "not unique" in got["_stdout"], got["_stdout"]


# "Retain independent failures inside Tests complete" (:352): the aggregate job's own quarantine
# lint + evidence gate is an independent check. Its failure next to a known red pages; its
# derivative "Fail on skipped or failed ..." step alone does not add an item.
TC = "Python tests / Tests complete"
QLINT = "Quarantine list lint + evidence gate"


def _tc(qlint):
    # an e2e-upgrade red trips the e2e-upgrade gate step, which is derivative of that job
    return (TC, "failure", {QLINT: qlint, "Fail on skipped or failed e2e-upgrade shards": "failure"})


def test_main_red_new_quarantine_gate_failure_in_tests_complete_pages(tmp_path):
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), _tc("failure"))
    prior = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), _tc("success"))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)], current=current))
    assert got["route"] == "alerts" and "passed on main in run 41" in got["_stdout"], got["_stdout"]


def test_main_red_derivative_tests_complete_failure_is_not_an_item(tmp_path):
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), _tc("success"))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=current))
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 41)", got["_stdout"]
    assert got["summary"] == f"main still red on the same failure as run(s) 41: {E2E} / {E2E_STEP}"


def test_fold_blocked_by_a_quarantine_gate_failure_in_tests_complete(tmp_path):
    # sig_of feeds the queue dedupe and the already-red fold: the independent step enters SIG.
    api = _other_pr_queue_api()
    tc = next(j for j in api["actions/runs/42/jobs"]["jobs"] if j["name"] == TC)
    tc["steps"] = [{"name": QLINT, "conclusion": "failure"},
                   {"name": "Fail on skipped or failed required tests", "conclusion": "failure"}]
    got = _queue_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]
    assert f"{TC} / {QLINT}" in got["summary"]


# Prism P2 "Argument Limit" (:229): a 100-job page is ~240 KB (run 37243401496), over Linux's
# 128 KiB per-argument cap, so the jobs accumulator must not ride on argv. Big pages still route.
def test_main_red_large_jobs_pages_do_not_ride_on_argv(tmp_path):
    def fat(i):
        return (f"filler {i}", "success", {f"step {k} " + "x" * 200: "success" for k in range(12)})
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), *[fat(i) for i in range(120)])
    prior = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), *[fat(i) for i in range(120)])
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)], current=current))
    assert got["route"] == "logs" and got["card"] == "main-still-red (run 41)", got["_stdout"]


# --- Prism round 1 on e7b489f4 (t_1a8e095b) ------------------------------------------------------
# 569d6f572e69 "Excluded Recovery": created_at has 1 s precision. A main run created in the same
# second cannot be ordered against this one, so the red pages instead of dropping that run.
def test_main_red_same_second_predecessor_pages(tmp_path):
    green = _main_jobs((E2E, "success", {E2E_STEP: "success"}))
    api = _main_api([(41, "2026-10-04T05:00:00Z", "success", green),
                     (40, "2026-10-04T04:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]


# b0cbda43048e "Dropped Timeouts": an aggregate job that timed out with no failed step (its running
# step cancelled) is a stepless item in ITEMS and in sig_of, not dropped.
def _tc_timeout():
    return (TC, "timed_out", {QLINT: "cancelled"})


def test_main_red_tests_complete_timeout_next_to_a_known_red_pages(tmp_path):
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), _tc_timeout())
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _red_e2e())], current=current))
    assert got["route"] == "alerts", got["_stdout"]


def test_fold_blocked_by_a_tests_complete_timeout(tmp_path):
    api = _other_pr_queue_api()
    tc = next(j for j in api["actions/runs/42/jobs"]["jobs"] if j["name"] == TC)
    tc["conclusion"], tc["steps"] = "timed_out", [{"name": QLINT, "conclusion": "cancelled"}]
    got = _queue_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]
    assert TC in got["summary"]


# 5515ab6c4f85 "Coarse Deduplication": a job/step match cannot tell two tests apart. A NEW pytest
# node id in an already-red slice pages; the same node id stays a standing red.
SLICE = "Python tests / Run tests slice 12/16"
QTEST2 = "tests/tools/test_other.py::test_new_regression"


def test_main_red_new_test_in_an_already_red_slice_pages(tmp_path):
    red = _main_jobs((SLICE, "failure", {"Run tests": "failure"}))
    prior = {"jobs": [dict(j, id=j["id"] + 100) for j in red["jobs"]]}
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)], current=red)
    api["check-runs/700/annotations"] = _ann(QTEST2)
    api["check-runs/800/annotations"] = _ann(QTEST)
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts" and QTEST2 in got["_stdout"], got["_stdout"]


def test_main_red_same_test_in_an_already_red_slice_goes_to_logs(tmp_path):
    red = _main_jobs((SLICE, "failure", {"Run tests": "failure"}))
    prior = {"jobs": [dict(j, id=j["id"] + 100) for j in red["jobs"]]}
    api = _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)], current=red)
    api["check-runs/700/annotations"] = _ann(QTEST)
    api["check-runs/800/annotations"] = _ann(QTEST)
    got = _main_route(tmp_path, api)
    assert got["route"] == "logs", got["_stdout"]


# 891d36986f43 "missing-work gate failures": a Tests complete gate step that failed while none of
# its prerequisite jobs stopped (the requested e2e-upgrade shards were SKIPPED) is a new fault.
RUFF = ("Lint (ruff + ty) / ruff", "failure", {"ruff": "failure"})


def test_main_red_gate_failing_on_skipped_shards_next_to_a_known_red_pages(tmp_path):
    current = _main_jobs(RUFF, (E2E, "skipped", {}),
                         (TC, "failure", {QLINT: "success", "Fail on skipped or failed e2e-upgrade shards": "failure"}))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", _main_jobs(RUFF))], current=current))
    assert got["route"] == "alerts", got["_stdout"]


def test_main_red_gate_failing_on_skipped_shards_twice_goes_to_logs(tmp_path):
    jobs = _main_jobs(RUFF, (E2E, "skipped", {}),
                      (TC, "failure", {QLINT: "success", "Fail on skipped or failed e2e-upgrade shards": "failure"}))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", jobs)], current=jobs))
    assert got["route"] == "logs", got["_stdout"]


def test_main_red_skip_gate_now_vs_derivative_gate_before_pages(tmp_path):
    # the gate failed before only because e2e-upgrade failed; now it fails on a skip: different fault
    current = _main_jobs((E2E, "skipped", {}),
                         (TC, "failure", {"Fail on skipped or failed e2e-upgrade shards": "failure"}))
    prior = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}),
                       (TC, "failure", {"Fail on skipped or failed e2e-upgrade shards": "failure"}))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)], current=current))
    assert got["route"] == "alerts" and "different fault" in got["_stdout"], got["_stdout"]


# --- Prism round 2 on 1c0d16a5 (t_1a8e095b) ------------------------------------------------------
# d12fe5a4bf6d: two EARLIER main runs created in the same second cannot be ordered either.
def test_main_red_tied_predecessors_page(tmp_path):
    green = _main_jobs((E2E, "success", {E2E_STEP: "success"}))
    api = _main_api([(41, "2026-10-04T04:00:00Z", "success", green),
                     (40, "2026-10-04T04:00:00Z", "failure", _red_e2e())])
    got = _main_route(tmp_path, api)
    assert got["route"] == "alerts", got["_stdout"]


# 8a4efca8c2ad: a CANCELLED prerequisite is not an item, so the gate step that points at it stays one.
def test_main_red_cancelled_slice_next_to_a_known_red_pages(tmp_path):
    gate = "Fail on skipped or failed required tests"
    current = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}), (SLICE, "cancelled", {"Run tests": "cancelled"}),
                         (TC, "failure", {"Fail on skipped or failed e2e-upgrade shards": "failure", gate: "failure"}))
    prior = _main_jobs((E2E, "failure", {E2E_STEP: "failure"}),
                       (TC, "failure", {"Fail on skipped or failed e2e-upgrade shards": "failure", gate: "success"}))
    got = _main_route(tmp_path, _main_api([(41, "2026-10-04T04:00:00Z", "failure", prior)], current=current))
    assert got["route"] == "alerts", got["_stdout"]


def _slice_red(prior_tests, current_tests, *, older=None):
    red = _main_jobs((SLICE, "failure", {"Run tests": "failure"}))
    hist = [(41, "2026-10-04T04:00:00Z", "failure", {"jobs": [dict(j, id=j["id"] + 100) for j in red["jobs"]]})]
    if older is not None:
        hist.append((40, "2026-10-04T03:00:00Z", "failure", {"jobs": [dict(j, id=j["id"] + 200) for j in red["jobs"]]}))
    api = _main_api(hist, current=red)
    api["check-runs/700/annotations"] = _ann(*current_tests)
    api["check-runs/800/annotations"] = _ann(*prior_tests)
    if older is not None:
        api["check-runs/900/annotations"] = _ann(*older)
    return api


# 13f5bb458e0c: the node check is against the step's LATEST execution, not any older run. Run 41
# passed A (failed B in the same step); an older run failing A proves nothing about main now.
def test_main_red_test_that_passed_in_the_latest_red_run_pages(tmp_path):
    a, b = "tests/x.py::test_a", "tests/x.py::test_b"
    got = _main_route(tmp_path, _slice_red([b], [a], older=[a]))
    assert got["route"] == "alerts" and a in got["_stdout"], got["_stdout"]


# 702860bc5678: node ids are compared whole; a parameter id with "; " must not collapse two tests.
def test_main_red_semicolon_parameter_ids_are_not_truncated(tmp_path):
    got = _main_route(tmp_path, _slice_red(["tests/x.py::test_case[a; c]"], ["tests/x.py::test_case[a; b]"]))
    assert got["route"] == "alerts", got["_stdout"]


def test_main_red_same_semicolon_parameter_id_goes_to_logs(tmp_path):
    t = "tests/x.py::test_case[a; b]"
    got = _main_route(tmp_path, _slice_red([t], [t]))
    assert got["route"] == "logs", got["_stdout"]

