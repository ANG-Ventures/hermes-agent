#!/usr/bin/env python3
"""Read-only integration capability inventory for CI overflow (Phase 0)."""

import argparse
import copy
import datetime as dt
import fnmatch
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Literal, TypedDict

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github/workflows"
TARGETS = {".github/workflows/tests.yml", ".github/workflows/ci.yaml", "scripts/run_tests_parallel.py"}
APP = "ci-overflow-controller"


class CheckResult(TypedDict):
    name: str
    status: Literal["PASS", "BLOCK", "UNVERIFIABLE"]
    evidence: dict
    reason: str


def check(name: str, status: Literal["PASS", "BLOCK", "UNVERIFIABLE"], evidence: dict, reason: str = "") -> CheckResult:
    return {"name": name, "status": status, "evidence": evidence, "reason": reason}


def command(*args: str, cwd: Path = ROOT) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
    if proc.returncode:
        raise RuntimeError(f"{args[0]} {args[1] if len(args) > 1 else ''}: exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def api(path: str) -> dict | list:
    return json.loads(command("gh", "api", path))


def pages(path: str) -> list:
    """Fail closed if even one pagination request fails."""
    items = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        data = api(f"{path}{separator}per_page=100&page={page}")
        if not isinstance(data, list):
            raise ValueError(f"expected paginated array: {path}")
        items.extend(data)
        if len(data) < 100:
            return items
        page += 1


def workflow_data(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML YAML 1.1 parses the Actions `on` key as True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def inventory(repo: str) -> CheckResult:
    name = "routing_owner_inventory"
    try:
        head = api(f"repos/{repo}/commits/main")["sha"]
        sha = api(f"repos/{repo}/contents/.github/workflows/tests.yml?ref={head}")["sha"]
        prs = pages(f"repos/{repo}/pulls?state=open")
        collisions = []
        for pr in prs:
            files = pages(f"repos/{repo}/pulls/{pr['number']}/files")
            touched = sorted(TARGETS & {f["filename"] for f in files})
            if touched:
                collisions.append({"number": pr["number"], "title": pr["title"], "files": touched, "url": pr["html_url"]})
        return check(name, "PASS", {"main_sha": head, "tests_workflow_blob_sha": sha, "open_pr_count": len(prs), "collisions": collisions})
    except (RuntimeError, ValueError, KeyError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def workflow_identity(repo: str) -> CheckResult:
    name = "merge_group_one_call_site"
    try:
        data = workflow_data(WORKFLOWS / "ci.yaml")
        jobs = data["jobs"]
        calls = [key for key, job in jobs.items() if job.get("uses", "").endswith("/tests.yml")]
        workflows = api(f"repos/{repo}/actions/workflows?per_page=100")["workflows"]
        matches = [w for w in workflows if w["path"] == ".github/workflows/ci.yaml" and w["state"] == "active"]
        if len(matches) != 1:
            return check(name, "BLOCK", {"matches": matches}, "CI workflow ID not unique/active")
        ident = matches[0]["id"]
        runs = api(f"repos/{repo}/actions/workflows/{ident}/runs?event=merge_group&per_page=10")["workflow_runs"]
        observed = None
        for run in runs:
            jobs_data = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")
            if not isinstance(jobs_data, dict) or jobs_data.get("total_count", 101) > len(jobs_data.get("jobs", [])):
                raise ValueError("incomplete jobs pagination")
            producers = [j for j in jobs_data["jobs"] if j["name"].endswith("/ Generate slices") and j["conclusion"] == "success"]
            if producers:
                observed = {"run_id": run["id"], "name": producers[0]["name"], "job_id": producers[0]["id"]}
                break
        evidence = {"workflow_id": ident, "call_sites": calls, "merge_group_declared": "merge_group" in data["on"], "producer": observed}
        if len(calls) != 1 or not evidence["merge_group_declared"] or observed != None and observed["name"] != "Python tests / Generate slices" or observed is None:
            return check(name, "BLOCK", evidence, "one call site, merge_group trigger and successful producer job required")
        return check(name, "PASS", evidence)
    except (RuntimeError, ValueError, KeyError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def output_bytes(text: str) -> int:
    return len(text.encode("utf-16-le"))


def matrix_budget() -> CheckResult:
    name = "rc8_output_budget"
    try:
        counts = {}
        for scope in ("full", "plugin:memory"):
            raw = command(sys.executable, "scripts/run_tests_parallel.py", "--generate-slices", "16", "--test-scope", scope,
                          "--self-hosted-slots", "4", "--self-hosted-labels", '["self-hosted","hermes-ci"]', "--arm-hosted-slices", "3").strip()
            matrix = json.loads(raw)
            # plugin + core smoke, plus a dependents slice since #929; 16 = failed open.
            if scope != "full" and len(matrix["slice"]) not in (2, 3):
                raise ValueError("scoped generator failed open; no scoped measurement")
            local = copy.deepcopy(matrix)
            for row in local["slice"]:
                row["runs_on"] = '["ubuntu-latest"]'
            # Actions outputs are key=value plus newline; placement emits one more matrix.
            generate = output_bytes(f"matrix={raw}\nfallback_matrix={json.dumps(local)}\nrequest_digest=sha256:{'0' * 64}\n")
            placement = output_bytes(f"matrix={raw}\ne2e_runs_on=[\"ubuntu-latest\"]\nplan_valid=true\n")
            counts[scope] = {"slices": len(matrix["slice"]), "generate_bytes_utf16": generate,
                             "placement_bytes_utf16": placement, "run_bytes_utf16": generate + placement}
        allowed = all(v["generate_bytes_utf16"] < 750 * 1024 and v["placement_bytes_utf16"] < 750 * 1024
                      and v["run_bytes_utf16"] < 40 * 1024 * 1024 for v in counts.values())
        return check(name, "PASS" if allowed else "BLOCK", counts, "output byte ceiling exceeded" if not allowed else "")
    except (RuntimeError, ValueError, KeyError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def trigger_inventory(repo: str = "ANG-Ventures/hermes-agent", probe_sha: str | None = None) -> CheckResult:
    name = "ledger_push_safety"
    triggers = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        on = workflow_data(path).get("on") or {}
        if isinstance(on, dict):
            subset = {k: on[k] for k in ("push", "workflow_run") if k in on}
            if subset:
                triggers[path.name] = subset
    unsafe = [p for p, t in triggers.items() if "push" in t and (not isinstance(t["push"], dict) or
              (not t["push"].get("branches") and not t["push"].get("branches-ignore") and not t["push"].get("tags")) or
              any(fnmatch.fnmatchcase("ci-overflow-ledger-probe", branch) for branch in t["push"].get("branches", [])))]
    evidence = {"triggers": triggers, "unsafe": unsafe}
    if unsafe:
        return check(name, "BLOCK", evidence, "push trigger may match ledger probe")
    if not probe_sha:
        return check(name, "UNVERIFIABLE", evidence, "Supply --probe-sha after a disposable data-only push")
    try:
        commit = api(f"repos/{repo}/commits/{probe_sha}")
        files = [f["filename"] for f in commit["files"]]
        if files != ["state.json"]:
            return check(name, "BLOCK", {**evidence, "probe_files": files}, "probe commit was not data-only")
        runs = api(f"repos/{repo}/actions/runs?head_sha={probe_sha}&per_page=100")
        if runs["total_count"] != len(runs["workflow_runs"]):
            return check(name, "UNVERIFIABLE", evidence, "incomplete workflow-run pagination")
        evidence.update({"probe_sha": probe_sha, "probe_files": files, "runs": [r["html_url"] for r in runs["workflow_runs"]]})
        if runs["total_count"]:
            return check(name, "BLOCK", evidence, "data-only push triggered workflow runs")
        return check(name, "PASS", evidence)
    except (RuntimeError, KeyError, ValueError) as exc:
        return check(name, "UNVERIFIABLE", evidence, str(exc))


def app_capability(repo: str) -> CheckResult:
    name = "controller_app_capability"
    try:
        installations = api("orgs/ANG-Ventures/installations")["installations"]
        match = [item for item in installations if item["app_slug"] == APP]
        if not match:
            return check(name, "BLOCK", {"installation_apps": sorted(i["app_slug"] for i in installations)},
                         "App absent — see card t_73f36116")
        permissions = match[0].get("permissions", {})
        required = {"administration": "read", "actions": "read", "variables": "read", "contents": "write"}
        ok = all(permissions.get(k) == v or (v == "read" and permissions.get(k) == "write") for k, v in required.items())
        return check(name, "PASS" if ok else "BLOCK", {"installation_id": match[0]["id"], "permissions": permissions},
                     "App permissions incomplete" if not ok else "")
    except (RuntimeError, ValueError, KeyError) as exc:
        return check(name, "UNVERIFIABLE", {}, str(exc))


def gated_app_check(name: str, app: CheckResult) -> CheckResult:
    if app["status"] != "PASS":
        return check(name, "UNVERIFIABLE", {}, f"App identity unavailable: {app['reason'] or app['status']}")
    return check(name, "UNVERIFIABLE", {}, "App installed but intended external-controller credential not provisioned to preflight")


def e2e_portability() -> CheckResult:
    return check("e2e_hosted_portability", "BLOCK", {"current_e2e_runs_on": "CI_RUNNER_LABELS self-hosted/X64"},
                 "Existing workflow_call e2e job has no workflow_dispatch or hosted x64 override; requires reviewed branch wiring and real dispatched run before activation")


def host_cache(host: str) -> dict:
    # A single read-only SSH transaction. No cache contents or credentials enter stdout.
    script = r'''import hashlib,json,os,subprocess
from pathlib import Path
root=Path('/opt/ci-runners/cache')
def run(*args):
 p=subprocess.run(args,capture_output=True,text=True,check=True);return p.stdout.strip()
mount=run('findmnt','-no','SOURCE,FSTYPE,OPTIONS','/srv/ci')
reflink='reflink=1' in run('xfs_info','/srv/ci')
seeds={}
for d in sorted(root.iterdir()):
 if not d.is_dir():continue
 files=sorted(p for p in d.rglob('*') if p.is_file())
 digest=hashlib.sha256();size=0;manifest=[]
 for p in files:
  h=hashlib.sha256();size+=p.stat().st_size
  with p.open('rb') as f:
   for block in iter(lambda:f.read(1048576),b''):h.update(block)
  manifest.append({'path':str(p.relative_to(d)),'sha256':h.hexdigest()})
  digest.update(f'{p.relative_to(d)} {h.hexdigest()}\n'.encode())
 seeds[d.name]={'files':len(files),'bytes':size,'sha256_manifest':digest.hexdigest(),'manifest':manifest}
ids=run('docker','ps','-aq').splitlines();binds=[]
for i in ids:
 obj=json.loads(run('docker','inspect',i))[0]
 for m in obj.get('Mounts',[]):
  if '/opt/ci-runners/cache' in m.get('Source','') or '/opt/ci-runners/cache' in m.get('Destination',''):
   binds.append({'container':obj['Name'].lstrip('/'),'source':m['Source'],'destination':m['Destination'],'rw':m['RW']})
units=run('systemctl','list-units','--all','--plain','--no-legend','--type=service','--type=timer').splitlines()
cron=run('sudo','-n','sh','-c','for f in /etc/crontab /etc/cron.d/*; do [ ! -f "$f" ] || grep -l /opt/ci-runners/cache "$f" || :; done')
print(json.dumps({'mount':mount,'reflink':reflink,'seeds':seeds,'containers_inspected':len(ids),'binds':binds,'units_matching_cache':[x for x in units if 'cache' in x.lower() or 'runner' in x.lower()],'cron_paths':cron.splitlines()}))'''
    return json.loads(command("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3 -c " + shlex.quote(script)))


def caches() -> CheckResult:
    name = "cache_consumers"
    evidence = {}
    errors = []
    for host in ("ace-ai", "ace-media"):
        try:
            evidence[host] = host_cache(host)
        except (RuntimeError, KeyError, ValueError) as exc:
            errors.append(f"{host}: {exc}")
    return check(name, "UNVERIFIABLE", evidence, "; ".join(errors) if errors else
                 "Docker binds, mount and seed manifest measured; exhaustive warmer/pruner, user-cron and systemd unit-content inventory not yet established")


def rate_preflight(app: CheckResult) -> CheckResult:
    return check("rc11_rate_preflight", "UNVERIFIABLE", {"formula": "720*D + W*S + 12*T + P"},
                 "D/W/S/T/P need 20 real controller exchanges; App installation hourly limit unavailable" if app["status"] != "PASS" else
                 "D/W/S/T/P need 20 real controller exchanges; App installation identity not provisioned")


def preflight_checks(repo: str = "ANG-Ventures/hermes-agent", probe_sha: str | None = None) -> list[CheckResult]:
    results = [inventory(repo), workflow_identity(repo), matrix_budget(), trigger_inventory(repo, probe_sha)]
    app = app_capability(repo)
    results.extend([app, gated_app_check("contents_cas_and_ruleset", app), gated_app_check("app_runner_read_and_fork_isolation", app),
                    e2e_portability(), caches(), rate_preflight(app)])
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--repo", default="ANG-Ventures/hermes-agent")
    preflight.add_argument("--out", type=Path, required=True)
    preflight.add_argument("--probe-sha", help="SHA of disposable data-only branch push; verify zero workflow runs")
    import ci_overflow_integration  # sibling; late import (it imports this module)
    ci_overflow_integration.add_parser(sub)
    for action in ("caches", "verify-run"):
        sub.add_parser(action)
    args = parser.parse_args()
    if args.action == "integration":
        return _write_receipt(args.out, args.repo, ci_overflow_integration.integration_checks(args))
    if args.action != "preflight":
        print(f"{args.action}: not implemented", file=sys.stderr)
        return 2
    if args.repo != "ANG-Ventures/hermes-agent":
        parser.error("preflight is scoped to ANG-Ventures/hermes-agent")
    return _write_receipt(args.out, args.repo, preflight_checks(args.repo, args.probe_sha))


def _write_receipt(out: Path, repo: str, results: list[CheckResult]) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"schema_version": 1, "repo": repo, "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                               "checks": results}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in results:
        print(f"{item['status']:12} {item['name']}: {item['reason']}")
    return 0 if all(c["status"] == "PASS" for c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
