"""A file KILLED at the per-file wall ceiling is a HANG, never a "no-op".

Founding incident (t_b1b30c59, main CI run 35495902532 job 106038745136,
slice 3/16): ``tests/tools/test_local_env_blocklist.py`` hit the 402 s per-file
ceiling and was SIGKILL'd at ~39% of 81 collected items. Because a killed
pytest never prints its ``=== N passed ... ===`` counts line, the parsed
summary was empty — and the runner's classifier read "no counts" as "no tests
ran", printing::

    1 file where no tests ran (collection/import error, timeout before collection…)
    explicitly-requested file(s) collected 0 tests (no-op)

Both statements were false (81 collected, ~32 ran) and the Summary line read
``0 failed`` on a red job. The real event — a hang under load — was named
nowhere.

These tests pin the four asks:
  1. a killed-mid-run file gets its own TIMED OUT verdict with %, collected
     count and (under -v) the last test reached;
  2. ``no_tests_ran`` requires POSITIVE evidence of no collection, not merely
     an absent summary;
  3. the Summary line counts the timed-out file instead of reading 0 failed;
  4. the pre-existing genuine-no-op and import-error classifications are
     unchanged.

Worktree-bytes discipline: the runner is a script, so the helpers are imported
from the worktree file by absolute path and the e2e execs that same path.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location(
        "_timeout_verdict_run_tests_parallel_under_test", _RUNNER
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.__file__ and str(_REPO_ROOT) in mod.__file__, (
        f"imported runner is not worktree bytes: {mod.__file__}"
    )
    return mod


# ── The captured output from the founding incident ──────────────────────────
# Reproduced from job 106038745136 (81 collected, stalled at 39%, no counts
# line, prefixed by the runner's own kill banner).
INCIDENT_OUTPUT = (
    "(402s exceeded; process tree SIGKILL'd)\n"
    "============================= test session starts ==============================\n"
    "platform linux -- Python 3.11.9, pytest-9.1.1, pluggy-1.6.0\n"
    "rootdir: /home/runner/work/hermes-agent/hermes-agent\n"
    "collected 81 items\n"
    "\n"
    "tests/tools/test_local_env_blocklist.py ............................s... [ 39%]\n"
)

TIMEOUT_SUMMARY = {"timed_out": 1, "timeout_secs": 402, "collected": 81, "progress_pct": 39}


def test_incident_output_parses_as_running_not_empty() -> None:
    """The captured output carries the evidence collection SUCCEEDED."""
    mod = _load_runner_module()
    # The pre-existing counts parser finds nothing — that is the whole trap.
    assert mod._parse_pytest_summary(INCIDENT_OUTPUT) == {}, (
        "a killed pytest has no counts line; if this changes the premise moved"
    )
    progress = mod._parse_timeout_progress(INCIDENT_OUTPUT)
    assert progress["collected"] == 81, progress
    assert progress["progress_pct"] == 39, progress


def test_incident_is_not_classified_as_no_collection() -> None:
    """Ask #2: an ABSENT summary is not evidence that nothing was collected."""
    mod = _load_runner_module()
    assert mod._looks_like_no_collection(INCIDENT_OUTPUT, TIMEOUT_SUMMARY) is False, (
        "the killed-mid-run file was routed into the no-tests-ran bucket — "
        "the exact misdiagnosis this card exists to fix"
    )


def test_genuine_no_collection_cases_still_classify_as_such() -> None:
    """Ask #4: the pre-existing no-op / import-error verdicts are unchanged."""
    mod = _load_runner_module()
    zero_collect = (
        "============================= test session starts ===============================\n"
        "collected 0 items\n\n"
        "============================ no tests ran in 0.04s =============================\n"
    )
    import_error = (
        "==================================== ERRORS ====================================\n"
        "_______________ ERROR collecting tests/test_broken_import.py ___________________\n"
        "ImportError while importing test module 'tests/test_broken_import.py'.\n"
        "ModuleNotFoundError: No module named 'hermes_this_module_does_not_exist'\n"
    )
    assert mod._looks_like_no_collection(zero_collect, {}) is True
    assert mod._looks_like_no_collection(import_error, {}) is True
    # And the runner's own exit-5 tags still count as positive evidence.
    assert mod._looks_like_no_collection("", {"noop_exit5": 1}) is True
    assert mod._looks_like_no_collection("", {"noop_skip": 1}) is True
    assert mod._looks_like_no_collection("", {"noop_testless": 1}) is True


def test_verdict_names_duration_percent_and_collected() -> None:
    """Ask #1: the verdict states what was actually measured."""
    mod = _load_runner_module()
    verdict = mod._format_timeout_verdict(INCIDENT_OUTPUT, TIMEOUT_SUMMARY)
    assert "TIMED OUT after 402s" in verdict, verdict
    assert "~39%" in verdict, verdict
    assert "81 collected" in verdict, verdict
    # No -v in the incident output, so no test name is invented.
    assert "last test reached" not in verdict, verdict


def test_verdict_names_the_last_test_under_verbose() -> None:
    """Under -v the verdict names the test that was in flight when killed."""
    mod = _load_runner_module()
    verbose = (
        "(402s exceeded; process tree SIGKILL'd)\n"
        "collected 81 items\n"
        "tests/tools/test_local_env_blocklist.py::test_earlier PASSED             [ 38%]\n"
        "tests/tools/test_local_env_blocklist.py::test_base_python_sanitizer_uses_validated_separate_runtime_venv \n"
    )
    verdict = mod._format_timeout_verdict(verbose, TIMEOUT_SUMMARY)
    assert (
        "last test reached: tests/tools/test_local_env_blocklist.py::"
        "test_base_python_sanitizer_uses_validated_separate_runtime_venv"
    ) in verdict, verdict


# ── E2E through the real runner (a real SIGKILL, not a simulated one) ───────

def _hanging_file(d: Path) -> Path:
    p = d / "test_hangs_midrun.py"
    p.write_text(
        textwrap.dedent(
            """
            import time

            def test_a():
                assert True

            def test_b():
                assert True

            def test_c_hangs():
                time.sleep(300)

            def test_d():
                assert True
            """
        ).lstrip()
    )
    return p


def _run_runner(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_RUNNER), "-j", "1", *args],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def test_e2e_killed_file_reports_timeout_not_noop(tmp_path: Path) -> None:
    """The load-bearing e2e: a REAL per-file kill, through the real runner.

    Drives the actual SIGKILL path (not a hand-built summary dict) and asserts
    all three reporting asks at once on one invocation.
    """
    probe = _hanging_file(tmp_path)
    proc = _run_runner("--file-timeout", "8", "--file-retries", "0",
                       str(probe), "--strict-noop")
    out = proc.stdout
    assert proc.returncode != 0, f"a killed file must be RED:\n{out}"
    # Ask #1 — its own verdict class, with the measured evidence.
    assert "TIMED OUT" in out, out
    assert "4 collected" in out, out
    # Ask #1 — and NOT the no-op gate.
    assert "collected 0 tests (no-op)" not in out, out
    # Ask #2 — not the no-tests-ran bucket either.
    assert "where no tests ran" not in out, out
    # Ask #3 — the Summary line counts it instead of reading a bare "0 failed".
    summary_line = next(ln for ln in out.splitlines() if ln.startswith("=== Summary:"))
    assert "1 timed-out file" in summary_line, summary_line


def test_e2e_killed_file_whose_output_mentions_importerror_is_still_a_timeout(
    tmp_path: Path,
) -> None:
    """The timed-out exclusion must be load-bearing, not decoration.

    ``_looks_like_no_collection`` is a text heuristic over the captured
    output. A file that legitimately PRINTS an ImportError (a caught,
    logged one — common in tests that probe an optional dependency) and
    then hangs would satisfy that heuristic and be mislabelled "no tests
    ran" all over again. The ``timed_out`` exclusion in the classifier is
    what stops it, so this is the mutation that proves that guard has teeth.
    """
    probe = tmp_path / "test_logs_importerror_then_hangs.py"
    probe.write_text(
        textwrap.dedent(
            """
            import time

            def test_reports_a_caught_import_error():
                try:
                    import hermes_this_module_does_not_exist  # noqa: F401
                except ImportError as exc:
                    print(f"ImportError while probing optional dep: {exc}")
                assert True

            def test_then_hangs():
                time.sleep(300)
            """
        ).lstrip()
    )
    proc = _run_runner("--file-timeout", "8", "--file-retries", "0",
                       str(probe), "--strict-noop", "--", "-s")
    out = proc.stdout
    assert proc.returncode != 0, out
    # Precondition for this test to mean anything: the heuristic DOES see the
    # ImportError text, so only the timed_out exclusion can save the verdict.
    assert "ImportError while probing optional dep" in out, (
        f"probe never printed its ImportError — test is vacuous:\n{out}"
    )
    assert "TIMED OUT" in out, out
    assert "where no tests ran" not in out, out
    assert "collected 0 tests (no-op)" not in out, out


def test_e2e_genuine_noop_still_reds_as_noop(tmp_path: Path) -> None:
    """FALSE-POSITIVE GUARD: the genuine no-op keeps its existing verdict.

    A file with a real ``def test_`` filtered to zero by an explicit -k exits
    5 and must still trip the strict-noop gate — the new timeout branch must
    not swallow it.
    """
    (tmp_path / "test_deselect_zero.py").write_text(
        "def test_present_but_filtered_out():\n    assert True\n"
    )
    (tmp_path / "test_real.py").write_text(
        "def test_a():\n    assert True\n"
    )
    proc = _run_runner("--file-timeout", "60",
                       str(tmp_path / "test_deselect_zero.py"),
                       str(tmp_path / "test_real.py"),
                       "-k", "test_a", "--strict-noop")
    assert proc.returncode != 0, proc.stdout
    assert "collected 0 tests (no-op)" in proc.stdout, proc.stdout
    assert "TIMED OUT" not in proc.stdout, proc.stdout


def test_e2e_import_error_still_reported_as_no_tests_ran(tmp_path: Path) -> None:
    """FALSE-POSITIVE GUARD: an import error keeps the no-tests-ran verdict.

    This is the case the tightened ``no_tests_ran`` predicate must still
    accept — it has POSITIVE evidence (an ImportError during collection).
    """
    probe = tmp_path / "test_broken_import.py"
    probe.write_text(
        "from hermes_this_module_does_not_exist import nope  # hard ImportError\n\n"
        "def test_would_have_run():\n    assert nope\n"
    )
    proc = _run_runner("--file-timeout", "60", "--file-retries", "0", str(probe))
    assert proc.returncode != 0, proc.stdout
    assert "where no tests ran" in proc.stdout, proc.stdout
    assert "TIMED OUT" not in proc.stdout, proc.stdout


def test_e2e_absent_summary_without_zero_collect_evidence_is_not_no_tests_ran(
    tmp_path: Path,
) -> None:
    """Ask #2, at its CALL SITE — an absent summary alone is never a no-op.

    Review round 1 found the predicate was unit-tested but its *call* was not:
    deleting ``and _looks_like_no_collection(_o, s)`` from the classifier
    survived the whole suite while restoring the false sentence verbatim. This
    drives the runner end-to-end over a file that collects fine, runs a test,
    then hard-exits (``os._exit``) — no counts line, no timeout, and no
    zero-collection evidence anywhere in the output. The only thing standing
    between it and "where no tests ran" is that predicate call.
    """
    probe = tmp_path / "test_hard_exits_midrun.py"
    probe.write_text(
        textwrap.dedent(
            """
            import os

            def test_a():
                assert True

            def test_b_hard_exits():
                os._exit(3)

            def test_c():
                assert True
            """
        ).lstrip()
    )
    proc = _run_runner("--file-timeout", "60", "--file-retries", "0", str(probe))
    out = proc.stdout
    assert proc.returncode != 0, out
    # Non-vacuity: this case must actually be the one under test — collection
    # SUCCEEDED, pytest printed no counts line, and nothing was killed.
    assert "collected 3 items" in out, f"probe did not collect — test is vacuous:\n{out}"
    assert "=== Summary:" in out and " passed" in out, out
    assert "no tests ran" not in out.replace(
        "where no tests ran", ""
    ), f"output carries real zero-collect evidence — wrong fixture:\n{out}"
    assert "TIMED OUT" not in out, out
    # The contract: reported as undetermined, never asserted as a no-op.
    assert "cause not determinable from output" in out, out
    assert "where no tests ran" not in out, out


def test_e2e_normal_passing_run_names_no_timeout(tmp_path: Path) -> None:
    """Sanity: an ordinary green run is untouched by any of this."""
    probe = tmp_path / "test_real.py"
    probe.write_text("def test_a():\n    assert True\n")
    proc = _run_runner("--file-timeout", "60", str(probe))
    assert proc.returncode == 0, proc.stdout
    assert "TIMED OUT" not in proc.stdout, proc.stdout
    assert "timed-out file" not in proc.stdout, proc.stdout
