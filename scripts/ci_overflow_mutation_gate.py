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
