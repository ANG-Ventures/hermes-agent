"""Prove each safety contract goes red under an isolated one-line mutant."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
CASES = [
    ("blind-CAS", "scripts/ci_overflow_ledger.py", "self._write(state, sha)\n                return Reservation(decided)",
     "self._write(state, self._read()[1])\n                return Reservation(decided)", "test_two_contending_writers_never_overdraw", "tests/test_ci_overflow_ledger.py"),
    ("no-budget", "scripts/ci_overflow_ledger.py", "remaining = max(0, self.daily_limit - (self.headroom or 0) - self._consumed(state, today))",
     "remaining = self.daily_limit", "test_two_contending_writers_never_overdraw", "tests/test_ci_overflow_ledger.py"),
    ("API-error-as-empty", "scripts/ci_overflow_plan.py", "return Snapshot(timestamp, \"unknown\", 0, 0, 0)",
     "return Snapshot(timestamp, \"ok\", 0, 0, 0)", "test_sampler", "tests/test_ci_overflow_plan.py"),
    ("no-phantom-release", "scripts/ci_overflow_ledger.py", "if job is None:\n                    to_release.append(entry)",
     "if job is None:\n                    pass", "test_timeout_terminal_absent_job_restores_allowance_once", "tests/test_ci_overflow_ledger.py"),
    ("TTL-instead-of-terminal", "scripts/ci_overflow_ledger.py", "jobs.get(\"status\") != \"completed\"",
     "False", "test_no_phantom_release_without_exact_evidence", "tests/test_ci_overflow_ledger.py"),
    # Argus t_9b423c85 F1/F3/F4: each spec-named guard owns a test that must go RED without it.
    ("F1-validate-raw-base64", "scripts/ci_overflow_ledger.py", "base64.b64decode(\"\".join(content.split()), validate=True)",
     "base64.b64decode(content, validate=True)", "test_captured_live_contents_response_decodes", "tests/test_ci_overflow_ledger.py"),
    ("C16-no-exclude-this-attempt", "scripts/ci_overflow_plan.py", " or (run[\"id\"], run[\"run_attempt\"]) == exclude_attempt",
     "", "test_sampler_excludes_this_run_attempt", "tests/test_ci_overflow_plan.py"),
    ("C18-no-job-dedupe", "scripts/ci_overflow_plan.py", "if job[\"id\"] in seen:\n                    continue",
     "if False:\n                    continue", "test_sampler_dedupes_jobs_by_id", "tests/test_ci_overflow_plan.py"),
    ("C19-no-20s-bound", "scripts/ci_overflow_plan.py", "deadline = started + 20",
     "deadline = started + 10**9", "test_sampler_collection_bound_20s_is_unknown", "tests/test_ci_overflow_plan.py"),
    ("C20-no-60s-staleness", "scripts/ci_overflow_plan.py", "total_seconds() > 60",
     "total_seconds() > 10**9", "test_sampler_snapshot_older_than_60s_is_unknown", "tests/test_ci_overflow_plan.py"),
    ("C21-busy-counted-idle", "scripts/ci_overflow_plan.py", "sum(not r[\"busy\"] for r in matching)",
     "len(matching)", "test_sampler_busy_runner_is_online_not_idle", "tests/test_ci_overflow_plan.py"),
    ("C26-duplicate-field-last-wins", "scripts/ci_overflow_plan.py", "object_pairs_hook=_object_pairs,",
     "object_pairs_hook=None,", "test_duplicate_json_field_rejected", "tests/test_ci_overflow_plan.py"),
    ("C27-symlink-accepted", "scripts/ci_overflow_plan.py", " or (entries[0].external_attr >> 16) & 0o170000 == 0o120000",
     "", "test_archive_symlink_entry_refused", "tests/test_ci_overflow_plan.py"),
    ("C13-create-missing-ledger", "scripts/ci_overflow_ledger.py", "state, sha = self._read()\n            except (Exception):\n                return Refusal(\"ledger-unavailable\")",
     "state, sha = self._read()\n            except (Exception):\n                state, sha = {\"version\": 1, \"attempts\": {}, \"daily_totals\": {}}, None",
     "test_missing_ledger_never_created", "tests/test_ci_overflow_ledger.py"),
    ("C5-arm-on-core", "scripts/ci_overflow_plan.py", "if not j[\"core\"] and any(",
     "if any(", "test_arm_never_placed_on_core_when_core_goes_to_cloud", "tests/test_ci_overflow_plan.py"),
    ("C29-no-18-job-ceiling", "scripts/ci_overflow_ledger.py", "                or len(plan.jobs) > 18\n",
     "", "test_reserve_refuses_more_than_18_jobs", "tests/test_ci_overflow_ledger.py"),
    ("C4-reserve-label-allowlist-off", "scripts/ci_overflow_ledger.py", "any(j.labels not in (POOL, X64, ARM) or",
     "any(False or", "test_reserve_refuses_unapproved_label", "tests/test_ci_overflow_ledger.py"),
    ("C30-no-semantic-validation", "scripts/ci_overflow_ledger.py", "        _validate(data, self._today())\n", "",
     "test_semantically_corrupt_ledger_refuses_without_put", "tests/test_ci_overflow_ledger.py"),
    # Argus R3: restore the 586cd088 shape (no row key-set check, `row.get("terminal_on")`).
    ("C31-terminal-presence-unchecked", "scripts/ci_overflow_ledger.py",
     "        if set(row) != ROW_FIELDS:\n            raise ValueError(\"corrupt admission fields\")\n"
     "        _date(row[\"admitted_on\"], today)\n        terminal = row[\"terminal_on\"]\n",
     "        _date(row.get(\"admitted_on\"), today)\n        terminal = row.get(\"terminal_on\")\n",
     "missing-terminal-field", "tests/test_ci_overflow_ledger.py"),
    ("C32-duplicate-json-last-wins", "scripts/ci_overflow_ledger.py", "object_pairs_hook=_object_pairs)",
     "object_pairs_hook=None)", "raw_wire_corrupt and duplicate", "tests/test_ci_overflow_ledger.py"),
    ("C33-version-not-exact-int", "scripts/ci_overflow_ledger.py", " or type(data[\"version\"]) is not int\n", "\n",
     "version", "tests/test_ci_overflow_ledger.py"),
    # t_38a419e0: hosted jobs charge measured billed minutes; closed rows fold on every admission.
    ("F2-hosted-actual-ignored", "scripts/ci_overflow_ledger.py", "elif HOSTED not in entry:", "elif False:",
     "test_hosted_reconcile_releases_unused_remainder", "tests/test_ci_overflow_ledger.py"),
    ("F2-charge-flat-reservation", "scripts/ci_overflow_ledger.py", "return job.get(HOSTED, job[\"reserved_minutes\"])",
     "return job[\"reserved_minutes\"]", "test_admit_and_reconcile_n_runs", "tests/test_ci_overflow_ledger.py"),
    ("F2-fold-only-past-soft-limit", "scripts/ci_overflow_ledger.py", "            self._compact(state, today)\n",
     "            if len(_encode(state)) >= SOFT_LIMIT:\n                self._compact(state, today)\n",
     "test_reserve_folds_closed_rows_below_soft_limit", "tests/test_ci_overflow_ledger.py"),
    ("F2-hosted-bound-unchecked", "scripts/ci_overflow_ledger.py", "or not 0 < job[HOSTED] <= CEILING[_kind(job.get(\"job_id\"))]",
     "or False", "test_corrupt_hosted_minutes_refused", "tests/test_ci_overflow_ledger.py"),
    ("F2-billed-without-runner", "scripts/ci_overflow_ledger.py", "            or not job.get(\"runner_name\")):",
     "            or False):", "no-runner", "tests/test_ci_overflow_ledger.py"),
    # t_f459aa52: D4 amendment -- admit at the measured estimate E with headroom M.
    ("D4a-headroom-ignored", "scripts/ci_overflow_ledger.py", "self.daily_limit - (self.headroom or 0) - ",
     "self.daily_limit - 0 - ", "test_burst_beyond_cap_minus_headroom_goes_local", "tests/test_ci_overflow_ledger.py"),
    ("D4a-estimate-ignored", "scripts/ci_overflow_ledger.py", "_estimate(state, k) if estimating else CEILING[k]",
     "CEILING[k]", "test_admission_at_estimate_within_headroom", "tests/test_ci_overflow_ledger.py"),
    ("D4a-p90-becomes-median", "scripts/ci_overflow_ledger.py", "math.ceil(0.9 * len(samples)) - 1",
     "len(samples) // 2", "test_estimate_is_p90_of_ledger_samples", "tests/test_ci_overflow_ledger.py"),
    ("D4a-no-min-samples", "scripts/ci_overflow_ledger.py", "if len(samples) < MIN_SAMPLES:",
     "if not samples:", "test_estimate_is_p90_of_ledger_samples", "tests/test_ci_overflow_ledger.py"),
    ("D4a-refund-only", "scripts/ci_overflow_ledger.py", "if charge != entry[\"reserved_minutes\"]:",
     "if charge < entry[\"reserved_minutes\"]:", "test_reconcile_charges_billed_above_estimate",
     "tests/test_ci_overflow_ledger.py"),
    ("D4a-unmeasured-charges-estimate", "scripts/ci_overflow_ledger.py", "charge = ceiling if billed is None",
     "charge = entry[\"reserved_minutes\"] if billed is None", "test_unmeasured_executed_job_at_estimate",
     "tests/test_ci_overflow_ledger.py"),
    ("D4a-no-samples-recorded", "scripts/ci_overflow_ledger.py", "            if measured:\n",
     "            if False:\n", "test_reconcile_charges_billed_above_estimate", "tests/test_ci_overflow_ledger.py"),
    ("D4a-samples-unvalidated", "scripts/ci_overflow_ledger.py", "        raise ValueError(\"corrupt billed samples\")\n",
     "        pass\n", "sample-over-ceiling", "tests/test_ci_overflow_ledger.py"),
    ("D4a-runnerless-held", "scripts/ci_overflow_ledger.py", "elif _runnerless_cancel(job):",
     "elif False:", "test_runnerless_cancelled_job_releases", "tests/test_ci_overflow_ledger.py"),
    ("D4a-runnerless-ignores-steps", "scripts/ci_overflow_ledger.py", " and job.get(\"steps\") == []",
     "", "has-steps", "tests/test_ci_overflow_ledger.py"),
]


def main():
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "scripts").mkdir()
        (root / "tests").mkdir()
        for name, src, before, after, test, testfile in CASES:
            for filename in ("scripts/ci_overflow_plan.py", "scripts/ci_overflow_ledger.py", testfile):
                dst = root / filename
                dst.parent.mkdir(exist_ok=True)
                shutil.copyfile(ROOT / filename, dst)
            shutil.copytree(ROOT / "tests/fixtures/ci_overflow", root / "tests/fixtures/ci_overflow", dirs_exist_ok=True)
            target = root / src
            content = target.read_text(encoding="utf-8")
            assert content.count(before) == 1, (name, content.count(before))
            target.write_text(content.replace(before, after), encoding="utf-8")
            # -B: the scratch dir is reused, and two same-size mutants written within one mtime
            # second would otherwise import the PREVIOUS mutant's cached .pyc (false survivor).
            result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-o", "addopts=", testfile,
                                     "-k", test], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            print(f"{name}: exit={result.returncode}")
            for line in result.stdout.splitlines():
                if line.startswith("FAILED ") or line.startswith("E ") or "failed," in line:
                    print("  " + line[:180])
            if result.returncode == 0 or "FAILED " not in result.stdout:
                print(result.stdout[-2000:], result.stderr[-500:])
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
