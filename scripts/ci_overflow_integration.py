"""CI overflow Phase 2 integration certification (`ci_overflow_acceptance.py integration`).

Every gate is named and returns P0's CheckResult. There is no unknown=pass: a gate whose live
prerequisite is absent reports UNVERIFIABLE with the exact missing prerequisite.

Live evidence comes from real GitHub objects:
- disposable probe workflow runs on `argus-probe/**` branches (standard hosted, $0 on this public repo),
  identified by run id on the command line and re-read here from the API;
- the REAL controller App identity through the controller's own client (`--controller-root`), with a
  disposable branch for the CAS race and same-value writes for the K/BASELINE denial;
- the candidate workflow YAML read at `--candidate-ref` via the Contents API.
"""
from __future__ import annotations

import base64
import importlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_overflow_acceptance import CheckResult, api, check, command  # noqa: E402

LOCAL_LABELS = ["self-hosted", "Linux", "X64", "hermes-ci"]
# Identifiers of the controller App (app_id, installation_id, client_id, 1Password item id, key-file
# name). Public ids, but their presence on an Actions surface would mean the identity leaked there.
CONTROLLER_IDENTITY = re.compile(
    r"5048512|164087494|Iv23li0EdVCA6haUv6B4|nochhlkkokgq6i3cghcouq3pmy|ci-overflow-controller/app\.pem"
    r"|BEGIN (?:RSA )?PRIVATE KEY")
PRE_EXISTING_SECRETS = {"AUTOFIX_BOT_PAT", "CI_FAIL_WEBHOOK_SECRET"}
PROBE_LINE = re.compile(r"PROBE slice=(\S+) executing_attempt=(\d+) planned_attempt=(\d+)")


# -- pure gates (unit-tested; take data, not the network) ---------------------------------------
def _jobs(workflow_text: str) -> dict:
    data = yaml.safe_load(workflow_text)
    return data["jobs"]


def fallback_predicate(workflow_text: str) -> CheckResult:
    """merge_group fallback must survive a dead/failed/timed-out placement (spec §5.1) and select the
    STATIC split, never all-local (t_42bed567: all-local wedged the queue on a drained pool)."""
    name = "fallback_predicate_static"
    jobs = _jobs(workflow_text)
    problems = []
    for job_id in ("test", "e2e"):
        job = jobs.get(job_id) or {}
        cond = str(job.get("if", ""))
        for need in ("always()", "!cancelled()", "needs.generate.result == 'success'"):
            if need not in cond:
                problems.append(f"{job_id}.if lacks {need!r}")
        if "placement" not in (job.get("needs") or []):
            problems.append(f"{job_id} does not need placement")
    matrix = str(((jobs.get("test") or {}).get("strategy") or {}).get("matrix", ""))
    if not matrix.rstrip("} ").endswith("|| needs.generate.outputs.matrix)"):
        problems.append("test matrix does not end in the static generate.matrix fallback")
    if "local_matrix" in matrix:
        problems.append("test matrix can select the all-local generate.local_matrix")
    for gate in ("needs.placement.result == 'success'", "needs.placement.outputs.plan_valid == 'true'"):
        if gate not in matrix:
            problems.append(f"placement matrix not gated by {gate!r}")
    e2e_runs_on = str((jobs.get("e2e") or {}).get("runs-on", ""))
    if not e2e_runs_on.rstrip("} ").endswith("|| '[\"ubuntu-latest\"]'))") or "vars.CI_RUNNER_LABELS" not in e2e_runs_on:
        problems.append("e2e runs-on does not end in the static CI_RUNNER_LABELS fallback")
    if json.dumps(LOCAL_LABELS).replace(" ", "") in e2e_runs_on.replace(" ", "").replace("'", ""):
        problems.append("e2e runs-on can select the fixed local pool")
    evidence = {"test_if": str((jobs.get("test") or {}).get("if")), "e2e_if": str((jobs.get("e2e") or {}).get("if")),
                "problems": problems}
    return check(name, "BLOCK" if problems else "PASS", evidence, "; ".join(problems))


def plan_bound_to_attempt(workflow_text: str) -> CheckResult:
    """Selective re-run reuses placement outputs from the prior attempt (measured live). A consumer may
    only take the placement plan when it is bound to the CURRENT github.run_attempt."""
    name = "ac3_plan_bound_to_current_attempt"
    jobs = _jobs(workflow_text)
    sites = {"test.strategy.matrix": str(((jobs.get("test") or {}).get("strategy") or {}).get("matrix", "")),
             "e2e.runs-on": str((jobs.get("e2e") or {}).get("runs-on", ""))}
    unbound = [k for k, expr in sites.items() if "needs.placement.outputs" in expr and "github.run_attempt" not in expr]
    return check(name, "BLOCK" if unbound else "PASS", {"unbound_consumers": unbound},
                 f"placement outputs consumed without a github.run_attempt binding: {unbound}" if unbound else "")


def selective_rerun_verdict(original: list[dict], rerun: list[dict], probe_lines: dict[str, str]) -> CheckResult:
    """original/rerun: job listings of attempt N and N+1 (dicts with name/runner_name/started_at).
    probe_lines: job name -> its PROBE log line in attempt N+1."""
    name = "ac3_selective_rerun_stale_plan"
    by_name = {j["name"]: j for j in original}
    reused, executed = [], []
    for job in rerun:
        prior = by_name.get(job["name"])
        same = prior is not None and prior["started_at"] == job["started_at"] and prior["runner_name"] == job["runner_name"]
        (reused if same else executed).append(job["name"])
    stale = []
    for job_name in executed:
        m = PROBE_LINE.search(probe_lines.get(job_name, ""))
        if m and m.group(2) != m.group(3):
            stale.append({"job": job_name, "executing_attempt": int(m.group(2)), "planned_attempt": int(m.group(3))})
    planners_reused = [n for n in reused if n.endswith(("Generate slices", "Placement"))]
    evidence = {"reused": reused, "executed": executed, "stale_executions": stale, "planners_reused": planners_reused}
    if not executed:
        return check(name, "UNVERIFIABLE", evidence, "no job executed in the re-run attempt; probe has no teeth")
    if stale and planners_reused:
        return check(name, "BLOCK", evidence,
                     "BLOCK ACTIVATION: selective re-run executed hosted work under the prior attempt's plan; "
                     "generate/placement were reused, so no fresh request/reservation exists for the new attempt")
    return check(name, "PASS", evidence)


# -- live gates ----------------------------------------------------------------------------------
def _run_jobs(repo: str, run_id: int, attempt: int) -> list[dict]:
    data = api(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    if data["total_count"] != len(data["jobs"]):
        raise ValueError("incomplete jobs pagination")
    return data["jobs"]


def _api_bytes(path: str) -> bytes:
    proc = subprocess.run(["gh", "api", path], capture_output=True, timeout=300, check=False)
    if proc.returncode:
        raise RuntimeError(f"gh api {path}: exit {proc.returncode}")
    return proc.stdout


def _zip_members(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if not info.is_dir():
                yield info.filename, zf.read(info)


def _job_log(repo: str, job_id: int) -> str:
    return command("gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs")


def selective_rerun(repo: str, run_id: int | None) -> list[CheckResult]:
    name = "ac3_selective_rerun_stale_plan"
    if not run_id:
        return [check(name, "UNVERIFIABLE", {}, "supply --rerun-probe-run")]
    try:
        run = api(f"repos/{repo}/actions/runs/{run_id}")
        a1, a2 = _run_jobs(repo, run_id, 1), _run_jobs(repo, run_id, 2)
        lines = {}
        for job in a2:
            hit = [ln for ln in _job_log(repo, job["id"]).splitlines() if "PROBE slice=" in ln and "echo" not in ln]
            lines[job["name"]] = hit[0] if hit else ""
        result = selective_rerun_verdict(a1, a2, lines)
        arts = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
        result["evidence"].update({"run_url": run["html_url"], "head_sha": run["head_sha"],
                                   "attempt2_request_artifacts": [a["name"] for a in arts["artifacts"]
                                                                  if a["name"].endswith(f"-{run_id}-2")]})
        # Controller consequence, from the same listing: attempt 2 shows a SUCCESSFUL producer copy but no
        # attempt-2 request artifact, so cioc admit() ends in `no-request` (no ledger row, no reservation).
        producer = [j for j in a2 if j["name"].endswith("Generate slices")]
        reservation = check("ac3_selective_rerun_fresh_reservation",
                            "BLOCK" if producer and producer[0]["conclusion"] == "success"
                            and not result["evidence"]["attempt2_request_artifacts"] else "UNVERIFIABLE",
                            {"producer_listed_in_rerun": bool(producer), "request_artifact_for_rerun_attempt": False,
                             "controller_path": "cioc/service.py admit(): producer ok -> no artifact -> no-request"},
                            "re-run attempt lists a reused producer success but can never carry its own request "
                            "artifact: the controller cannot reserve for it and reconciliation never sees it")
        return [result, reservation]
    except (RuntimeError, ValueError, KeyError) as exc:
        return [check(name, "UNVERIFIABLE", {}, str(exc))]


def full_rerun(repo: str, run_id: int | None, attempt: int | None) -> CheckResult:
    name = "ac3_full_rerun_regenerates_plan"
    if not run_id or not attempt:
        return check(name, "UNVERIFIABLE", {}, "supply --rerun-probe-run and --full-rerun-attempt")
    try:
        jobs = _run_jobs(repo, run_id, attempt)
        planned = []
        for job in jobs:
            m = PROBE_LINE.search(_job_log(repo, job["id"]))
            if m:
                planned.append({"job": job["name"], "executing": int(m.group(2)), "planned": int(m.group(3))})
        ok = planned and all(p["executing"] == p["planned"] == attempt for p in planned)
        return check(name, "PASS" if ok else "BLOCK", {"attempt": attempt, "slices": planned,
                     "note": "GitHub-side half only: controller fresh reservation is exercised under ac3_live_*"},
                     "" if ok else "full re-run did not re-plan")
    except (RuntimeError, ValueError, KeyError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def github_token_ledger_write(repo: str, run_id: int | None, head_before: str | None) -> CheckResult:
    name = "ac2_ledger_rejects_github_token"
    if not run_id or not head_before:
        return check(name, "UNVERIFIABLE", {}, "supply --token-probe-run and --ledger-head-before")
    try:
        job = api(f"repos/{repo}/actions/runs/{run_id}/jobs")["jobs"][0]
        log = _job_log(repo, job["id"])
        head_after = api(f"repos/{repo}/git/ref/heads/ci-overflow-ledger")["object"]["sha"]
        push_rejected = "GH013" in log and "Cannot update this protected ref" in log and "PROBE push_rc=1" in log
        api_rejected = re.search(r"PROBE api_patch_http=(\d+)", log)
        evidence = {"run_url": job["html_url"].split("/job/")[0], "push_rejected_by_ruleset": push_rejected,
                    "rest_patch_http": int(api_rejected.group(1)) if api_rejected else None,
                    "ledger_head_before": head_before, "ledger_head_after": head_after}
        ok = push_rejected and evidence["rest_patch_http"] == 422 and head_after == head_before
        return check(name, "PASS" if ok else "BLOCK", evidence, "" if ok else "GITHUB_TOKEN write not rejected")
    except (RuntimeError, ValueError, KeyError, IndexError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def identity_absent(repo: str, ref: str, run_id: int | None) -> CheckResult:
    name = "ac2_controller_identity_absent_from_actions"
    try:
        hits, secret_refs = [], {}
        listing = api(f"repos/{repo}/contents/.github/workflows?ref={ref}")
        for item in listing:
            text = base64.b64decode(api(f"repos/{repo}/contents/{item['path']}?ref={ref}")["content"]).decode()
            if CONTROLLER_IDENTITY.search(text):
                hits.append(item["path"])
            if item["name"] in ("tests.yml", "ci.yaml"):
                secret_refs[item["name"]] = sorted(set(re.findall(r"secrets\.[A-Za-z0-9_]+", text)))
        stores = {"repo": sorted(s["name"] for s in api(f"repos/{repo}/actions/secrets")["secrets"]),
                  "org": api("orgs/ANG-Ventures/actions/secrets")["total_count"],
                  "environments": {}}
        for env in api(f"repos/{repo}/environments")["environments"]:
            stores["environments"][env["name"]] = [s["name"] for s in
                                                   api(f"repos/{repo}/environments/{env['name']}/secrets")["secrets"]]
        new_secrets = sorted(set(stores["repo"]) - PRE_EXISTING_SECRETS)
        env_secrets = sorted(s for v in stores["environments"].values() for s in v)
        evidence = {"ref": ref, "workflow_hits": hits, "managed_lane_secret_refs": secret_refs,
                    "secret_stores": stores, "unexpected_secrets": new_secrets + env_secrets}
        scan_hits = []
        if run_id:
            scanned = {"log_files": 0, "artifact_files": 0}
            for member, data in _zip_members(_api_bytes(f"repos/{repo}/actions/runs/{run_id}/logs")):
                scanned["log_files"] += 1
                if CONTROLLER_IDENTITY.search(data.decode("utf-8", "replace")):
                    scan_hits.append(f"log:{member}")
            arts = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
            for art in (a for a in arts if a["name"].startswith("ci-overflow-")):
                for member, data in _zip_members(_api_bytes(f"repos/{repo}/actions/artifacts/{art['id']}/zip")):
                    scanned["artifact_files"] += 1
                    if CONTROLLER_IDENTITY.search(data.decode("utf-8", "replace")):
                        scan_hits.append(f"artifact:{art['name']}/{member}")
            evidence.update(scanned_run=run_id, scanned=scanned, run_hits=scan_hits)
            if not scanned["log_files"]:
                return check(name, "UNVERIFIABLE", evidence, "run logs empty: scan had no teeth")
        bad = hits or scan_hits or any(secret_refs.values()) or new_secrets or env_secrets or stores["org"]
        return check(name, "BLOCK" if bad else "PASS", evidence, "controller identity reachable from Actions" if bad else "")
    except (RuntimeError, ValueError, KeyError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def pr_ci_legacy(repo: str, run_id: int | None) -> CheckResult:
    name = "fallback_pr_ci_original_matrix"
    if not run_id:
        return check(name, "UNVERIFIABLE", {}, "supply --pr-ci-run")
    try:
        run = api(f"repos/{repo}/actions/runs/{run_id}")
        jobs = api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest")["jobs"]
        tests = [j for j in jobs if j["name"].startswith("Python tests / Run tests ")]
        placement = [j["conclusion"] for j in jobs if j["name"] == "Python tests / Placement"]
        forced = [j["name"] for j in tests if j["labels"] == LOCAL_LABELS]
        evidence = {"run_url": run["html_url"], "event": run["event"], "placement": placement,
                    "test_jobs": len(tests), "label_sets": sorted({",".join(j["labels"]) for j in tests}),
                    "jobs_on_forced_local_labels": forced}
        ok = run["event"] != "merge_group" and placement == ["skipped"] and len(tests) >= 2 and not forced
        return check(name, "PASS" if ok else "BLOCK", evidence, "" if ok else "PR CI did not take the legacy path")
    except (RuntimeError, ValueError, KeyError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def app_gates(repo: str, controller_root: Path | None) -> list[CheckResult]:
    names = ("ac2_app_reads_runners", "ac3_no_k_baseline_writes_identity", "ac2_cas_two_writers")
    if controller_root is None:
        return [check(n, "UNVERIFIABLE", {}, "supply --controller-root (fleet-ops-scripts/ci-overflow-controller)") for n in names]
    sys.path.insert(0, str(controller_root))
    gh_mod = importlib.import_module("cioc.github")
    cfg = json.loads((controller_root / "config.json").read_text(encoding="utf-8"))
    gh = gh_mod.GitHub(cfg["api_base"], gh_mod.InstallationTokens(cfg["api_base"], cfg["app_id"], cfg["installation_id"],
                                                                  os.path.expanduser(cfg["key_path"])))

    def call(method, path, body=None):
        try:
            return 200, gh.request(method, gh.repo_url(repo, path), body)
        except gh_mod.GitHubError as exc:
            return exc.status, None

    out = []
    st, runners = call("GET", "actions/runners?per_page=100")
    out.append(check(names[0], "PASS" if st == 200 and runners["total_count"] >= 1 else "BLOCK",
                     {"http": st, "total_count": runners and runners["total_count"]}))
    writes = {}
    for var in ("CI_SELF_HOSTED_SLOTS", "CI_SELF_HOSTED_SLOTS_BASELINE"):
        _, cur = call("GET", f"actions/variables/{var}")
        # Same-value write: even a fail-open identity would change nothing.
        writes[var] = call("PATCH", f"actions/variables/{var}", {"name": var, "value": cur["value"]})[0] if cur else None
    denied = all(code in (403, 404) for code in writes.values())
    out.append(check(names[1], "PASS" if denied else "BLOCK", {"patch_http": writes},
                     "" if denied else "controller identity can write K/BASELINE"))
    branch = "argus-probe/t_f02c34f7-cas"
    _, main = call("GET", "git/ref/heads/main")
    call("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": main["object"]["sha"]})
    enc = lambda doc: base64.b64encode(json.dumps(doc).encode()).decode()  # noqa: E731
    _, created = call("PUT", "contents/argus-cas-probe.json", {"message": "cas init", "content": enc({"n": 0}), "branch": branch})
    parent, codes, gate = created["content"]["sha"], {}, threading.Barrier(2)

    def writer(tag):
        gate.wait()
        codes[tag] = call("PUT", "contents/argus-cas-probe.json",
                          {"message": f"cas {tag}", "content": enc({"w": tag}), "sha": parent, "branch": branch})[0]
    threads = [threading.Thread(target=writer, args=(t,)) for t in "AB"]
    [t.start() for t in threads]
    [t.join() for t in threads]
    deleted = call("DELETE", f"git/refs/heads/{branch}")[0]
    one = sorted(codes.values()) == [200, 409]
    out.append(check(names[2], "PASS" if one else "BLOCK", {"writer_http": codes, "disposable_branch_deleted": deleted == 200},
                     "" if one else "not exactly one CAS winner"))
    return out


NOT_RUN = {
    "ac3_live_contention_ceiling": "two real merge_group runs contending for a disposable allowance",
    "ac3_live_cancel_retry_no_double_charge": "cancel after admission + retry, ledger shows one charge",
    "ac3_live_utc_rollover_carry": "outstanding admission carried across a UTC date",
    "ac3_live_full_rerun_fresh_reservation": "controller writes a new attempt row on full re-run",
    "ac3_live_timeout_terminal_release": "forced placement timeout -> terminal -> §5.3a release <= 5 min",
    "ac3_live_executed_delayed_ambiguous_charged": "executed/delayed/ambiguous hosted jobs stay charged",
    "ac3_live_missing_corrupt_ledger": "missing/corrupt ledger -> no cloud + page",
    "ac4_pages_delivered": "pool-offline + budget-exhausted delivered to #alerts, retry, dedupe, #logs recovery",
    "fallback_live_placement_failures": "placement exception/timeout/cancel + controller outage -> all local on a real merge_group",
}


def not_run(prereq: str) -> list[CheckResult]:
    return [check(n, "UNVERIFIABLE", {"needs": what}, prereq) for n, what in NOT_RUN.items()]


def integration_checks(args) -> list[CheckResult]:
    repo = args.repo
    tests_yml = base64.b64decode(api(f"repos/{repo}/contents/.github/workflows/tests.yml?ref={args.candidate_ref}")["content"]).decode()
    results = [fallback_predicate(tests_yml), plan_bound_to_attempt(tests_yml)]
    results += selective_rerun(repo, args.rerun_probe_run)
    results.append(full_rerun(repo, args.rerun_probe_run, args.full_rerun_attempt))
    results += app_gates(repo, args.controller_root)
    results.append(github_token_ledger_write(repo, args.token_probe_run, args.ledger_head_before))
    results.append(identity_absent(repo, args.candidate_ref, args.pr_ci_run))
    results.append(pr_ci_legacy(repo, args.pr_ci_run))
    main_has_placement = "placement:" in base64.b64decode(
        api(f"repos/{repo}/contents/.github/workflows/tests.yml?ref=main")["content"]).decode()
    results += not_run("P2b placement wiring is not on main (no merge_group run can carry a plan) and the controller "
                       "service is disabled; re-run on the amended build" if not main_has_placement else
                       "live merge_group phase not executed in this pass")
    return results


def add_parser(sub) -> None:
    p = sub.add_parser("integration")
    p.add_argument("--repo", default="ANG-Ventures/hermes-agent")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--candidate-ref", default="main", help="ref whose tests.yml is certified (e.g. the P2b head)")
    p.add_argument("--rerun-probe-run", type=int, help="probe run: attempt 1 fails a slice, attempt 2 = rerun-failed-jobs")
    p.add_argument("--full-rerun-attempt", type=int, help="attempt number of a FULL rerun of the same probe run")
    p.add_argument("--token-probe-run", type=int, help="run whose GITHUB_TOKEN attempted a ledger write")
    p.add_argument("--ledger-head-before", help="ledger branch head recorded before the token probe")
    p.add_argument("--pr-ci-run", type=int, help="ordinary pull_request CI run of the candidate")
    p.add_argument("--controller-root", type=Path, help="ci-overflow-controller checkout (App identity gates)")
