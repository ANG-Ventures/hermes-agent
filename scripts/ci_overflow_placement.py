#!/usr/bin/env python3
"""Hosted ``placement`` job for tests.yml: read the external controller's plan, never write.

Polls the public ``ci-overflow-ledger`` branch ``state.json`` (contents:read
only) for up to 180 s for the plan the external ci-overflow-controller committed
for THIS run attempt. Only an exact identity match (repository_id, run_id,
run_attempt, head SHA, request digest) with a validated flag, allowlisted labels
and the complete job-id set may relabel the matrix. Anything else — no record,
timeout, read error, mismatch — emits ``plan_valid=false`` and NO matrix, and
the workflow runs everything on the local pool (spec §5.1 CB1/RC1).

``gate`` is the tests.yml aggregate: unexpectedly skipped test/e2e is failure.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ci_overflow_plan import APPROVED, ARM  # noqa: E402
from scripts.ci_overflow_request import is_core  # noqa: E402

LEDGER_BRANCH = "ci-overflow-ledger"
LEDGER_PATH = "state.json"
E2E_JOB_ID = "e2e"
POLL_DEADLINE_S = 180
POLL_INTERVAL_S = 5


class PlanInvalid(ValueError):
    """A record exists for this attempt but must not be acted on."""


def _ident(repository_id: int, run_id: int, run_attempt: int) -> str:
    return f"{repository_id}:{run_id}:{run_attempt}"


def expected_ids(matrix: dict) -> tuple[set[str], set[str]]:
    """(all job ids the plan must cover, ids that must never go to ARM)."""
    names = [row["name"] for row in matrix["slice"]]
    if len(names) != len(set(names)):
        raise PlanInvalid("duplicate slice names in generate matrix")
    return set(names) | {E2E_JOB_ID}, {"core smoke", E2E_JOB_ID}


def validate_record(state, *, repository_id: int, run_id: int, run_attempt: int, head_sha: str,
                    request_digest: str, matrix: dict, core_ids: set[str] | None = None):
    """Return the validated placement, ``None`` if no record exists yet, or raise PlanInvalid."""
    if type(state) is not dict or state.get("version") != 1 or type(state.get("attempts")) is not dict:
        raise PlanInvalid("ledger state schema")
    row = state["attempts"].get(_ident(repository_id, run_id, run_attempt))
    if row is None:
        return None
    plan = row.get("plan") if type(row) is dict else None
    summary = plan.get("summary") if type(plan) is dict else None
    if type(summary) is not dict or type(plan.get("jobs")) is not list:
        raise PlanInvalid("plan record schema")
    if summary.get("validated") is not True:
        raise PlanInvalid("plan not validated")
    if (type(row.get("jobs")) is not list or len(row["jobs"]) != len(plan["jobs"])
            or any({k: entry.get(k) for k in ("job_id", "labels", "reason", "reserved_minutes")} != decision
                   for entry, decision in zip(row["jobs"], plan["jobs"]))):
        raise PlanInvalid("plan jobs disagree with committed admission")
    for field, want in (("repository_id", repository_id), ("run_id", run_id), ("run_attempt", run_attempt)):
        if type(summary.get(field)) is not int or summary[field] != want:
            raise PlanInvalid(f"{field} mismatch")
    if not head_sha or summary.get("head_sha") != head_sha:
        raise PlanInvalid("head_sha mismatch")
    if not request_digest.startswith("sha256:") or summary.get("request_digest") != request_digest:
        raise PlanInvalid("request digest mismatch")
    if type(summary.get("policy_version")) is not str:
        raise PlanInvalid("policy_version missing")
    want_ids, never_arm = expected_ids(matrix)
    never_arm |= core_ids or set()
    labels = {}
    for job in plan["jobs"]:
        if (type(job) is not dict or type(job.get("job_id")) is not str or type(job.get("labels")) is not list
                or not all(type(x) is str for x in job["labels"]) or type(job.get("reason")) is not str
                or type(job.get("reserved_minutes")) is not int):
            raise PlanInvalid("job entry schema")
        if job["job_id"] in labels:
            raise PlanInvalid("duplicate job id")
        if tuple(job["labels"]) not in APPROVED:
            raise PlanInvalid(f"label set not allowlisted for {job['job_id']}")
        if job["labels"] == ARM and job["job_id"] in never_arm:
            raise PlanInvalid(f"{job['job_id']} may not run on ARM")
        labels[job["job_id"]] = job["labels"]
    if set(labels) != want_ids:
        raise PlanInvalid("job id set incomplete or foreign")
    placed = copy.deepcopy(matrix)
    for row_ in placed["slice"]:
        row_["runs_on"] = json.dumps(labels[row_["name"]], separators=(",", ":"))
    return {"matrix": placed, "e2e_runs_on": labels[E2E_JOB_ID], "plan": plan, "admitted_on": row.get("admitted_on"),
            "plan_attempt": run_attempt}


def fetch_state(repo: str, token: str) -> dict:
    url = f"https://api.github.com/repos/{repo}/contents/{LEDGER_PATH}?ref={LEDGER_BRANCH}"
    headers = {"Accept": "application/vnd.github.raw+json", "User-Agent": "ci-overflow-placement",
               "Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as resp:
        return json.loads(resp.read(1024 * 1024 + 1))


def poll(fetch, *, deadline_s=POLL_DEADLINE_S, interval_s=POLL_INTERVAL_S,
         clock=time.monotonic, sleep=time.sleep, **identity):
    """Return (placement | None, reason). Read errors retry; an invalid record stops at once."""
    end = clock() + deadline_s
    last = "no plan record"
    while True:
        try:
            found = validate_record(fetch(), **identity)
        except PlanInvalid as exc:
            return None, f"invalid plan: {exc}"
        except Exception as exc:  # network/JSON: the ledger may be briefly unreadable
            last = f"ledger read error: {type(exc).__name__}"
        else:
            if found is not None:
                return found, "validated plan"
        if clock() + interval_s > end:
            return None, f"timeout after {deadline_s}s ({last})"
        sleep(interval_s)


def _age(stamp, now: datetime) -> str:
    try:
        return f"{int((now - datetime.fromisoformat(stamp)).total_seconds())}s"
    except (TypeError, ValueError):
        return "unknown"


def summary_markdown(placement, reason: str, now: datetime) -> str:
    if placement is None:
        return (f"### CI overflow placement\n\n`plan_valid=false` — {reason}. Every test slice and e2e "
                "run on the local pool `self-hosted,Linux,X64,hermes-ci` (local fallback).\n")
    plan, s = placement["plan"], placement["plan"]["summary"]
    snap = s.get("snapshot") or {}
    obs = f"{snap.get('online', '?')}/{snap.get('idle', '?')}/{snap.get('queued_matching_jobs', '?')}"
    lines = ["### CI overflow placement", "",
             f"`plan_valid=true` — {reason}; exclusions: {s.get('exclusions', 'n/a')}", "",
             "| job/slice | reason | labels | online/idle/queued | K | snapshot age | mode | reservation | remaining | policy |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for job in plan["jobs"]:
        lines.append(f"| {job['job_id']} | {job['reason']} | {','.join(job['labels'])} | {obs} | {s.get('k_cap')} "
                     f"| {_age(snap.get('timestamp'), now)} | {s.get('mode')} | {job['reserved_minutes']} "
                     f"| {s.get('remaining_allowance')} | {s.get('policy_version')} |")
    if s.get("budget_overrides_cloud_only"):
        lines += ["", "**budget-overrides-cloud-only**: cloud-only requested, budget denied; queued on the local pool."]
    return "\n".join(lines) + "\n"


def write_outputs(path: str, placement) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        if placement is None:
            fh.write("plan_valid=false\n")
            return
        fh.write(f"matrix={json.dumps(placement['matrix'], separators=(',', ':'))}\n")
        fh.write(f"e2e_runs_on={json.dumps(placement['e2e_runs_on'], separators=(',', ':'))}\n")
        # Consumers take the plan ONLY when this equals the executing github.run_attempt:
        # "Re-run failed jobs" reuses these outputs in attempt N+1 without re-running
        # placement, so an unbound plan would run hosted work with no reservation.
        fh.write(f"plan_attempt={int(placement['plan_attempt'])}\n")
        fh.write("plan_valid=true\n")


def _int_env(name: str) -> int:
    value = os.environ.get(name, "")
    if not value.isascii() or not value.isdecimal():
        raise SystemExit(f"{name} must be a positive integer")
    return int(value)


def cmd_place(args) -> int:
    out = os.environ["GITHUB_OUTPUT"]
    placement, reason = None, "generate outputs unusable"
    try:
        matrix = json.loads(Path(os.environ["CI_MATRIX_FILE"]).read_text(encoding="utf-8"))
        digest = os.environ.get("CI_REQUEST_DIGEST", "").strip()
        if digest and not digest.startswith("sha256:"):
            digest = "sha256:" + digest
        core = {r["name"] for r in matrix["slice"] if is_core(r)}
        identity = dict(repository_id=_int_env("CI_REPOSITORY_ID"), run_id=_int_env("CI_RUN_ID"),
                        run_attempt=_int_env("CI_RUN_ATTEMPT"), head_sha=os.environ.get("CI_HEAD_SHA", ""),
                        request_digest=digest, matrix=matrix, core_ids=core)
        if not digest:
            reason = "request artifact digest missing (upload failed)"
        else:
            token = os.environ.get("GITHUB_TOKEN", "")
            placement, reason = poll(lambda: fetch_state(os.environ["CI_REPOSITORY"], token),
                                     deadline_s=args.deadline, **identity)
    except (KeyError, ValueError, TypeError, SystemExit) as exc:
        reason = f"generate outputs unusable: {exc}"
    write_outputs(out, placement)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(placement, reason, datetime.now(timezone.utc)))
    print(f"plan_valid={'true' if placement else 'false'}: {reason}")
    return 0


def gate(needs: dict) -> list[str]:
    """Problems that must fail the tests workflow; skipped required work is failure."""
    problems = [f"{job} result={needs.get(job, {}).get('result')}" for job in ("generate", "test", "e2e")
                if needs.get(job, {}).get("result") != "success"]
    return problems


def cmd_gate(_args) -> int:
    problems = gate({job: {"result": os.environ.get(f"{job.upper()}_RESULT")} for job in ("generate", "test", "e2e")})
    for p in problems:
        print(f"::error::required tests job not successful: {p}")
    return 1 if problems else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    place = sub.add_parser("place")
    place.add_argument("--deadline", type=int, default=POLL_DEADLINE_S)
    sub.add_parser("gate")
    args = parser.parse_args(argv)
    return {"place": cmd_place, "gate": cmd_gate}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
