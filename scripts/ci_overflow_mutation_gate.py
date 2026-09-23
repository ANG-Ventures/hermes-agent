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
    ("no-budget", "scripts/ci_overflow_ledger.py", "remaining = max(0, self.daily_limit - self._consumed(state, today))",
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
    ("C30-no-semantic-validation", "scripts/ci_overflow_ledger.py", "        _validate(data, self._today())\n", "",
     "test_semantically_corrupt_ledger_refuses_without_put", "tests/test_ci_overflow_ledger.py"),
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
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", testfile,
                                     "-k", test], cwd=root, capture_output=True, text=True, stdin=subprocess.DEVNULL)
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
