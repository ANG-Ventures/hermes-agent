"""workflow_trigger_storm_lint (t_8ca8c919, ci-redundancy-audit class I): red on the storm shapes, green on
the fixes and markers, and clean on this repo's own workflows (lint.yml runs it with --repo .)."""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "workflow_trigger_storm_lint.py"
_spec = importlib.util.spec_from_file_location("wtsl", SCRIPT)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)

LISTENER = ("on:\n  workflow_run:{mark}\n    workflows: [{ups}]\n    types: [completed]\n"
            "jobs:\n  j:\n    if: {cond}\n")
GOOD_IF = "github.event.workflow_run.conclusion == 'failure'"


def test_listener_shapes():
    # 2026-10-04: fleet-ci-fail-alert.yml on 13 upstreams = 1,039 runs, 967 skipped
    assert L.lint_text("w", LISTENER.format(mark="", ups="A, B, C, D", cond=GOOD_IF))
    assert not L.lint_text("w", LISTENER.format(mark="", ups="A, B, C", cond=GOOD_IF))
    assert L.lint_text("w", LISTENER.format(mark="", ups="A", cond="github.event.workflow_run.event == 'schedule'"))
    assert not L.lint_text("w", LISTENER.format(mark="  # trigger-storm-ok: closes on green", ups="A", cond="true"))
    assert L.lint_text("w", LISTENER.format(mark="  # trigger-storm-ok:", ups="A", cond="true"))


def test_listener_types_must_be_exactly_completed():
    """Prism P1 2565a2e94fdd: `requested`/`in_progress` each create a run before any conclusion exists."""
    for types in ("[completed, requested, in_progress]", "[completed, requested]", "[requested]", "completed"):
        text = LISTENER.format(mark="", ups="A", cond=GOOD_IF).replace("[completed]", types)
        assert bool(L.lint_text("w", text)) is (types != "completed"), types
    assert L.lint_text("w", "on:\n  workflow_run:\n    workflows: [A]\njobs:\n  j:\n    if: %s\n" % GOOD_IF)


def test_default_repo_is_the_repo_root(tmp_path):
    """Prism P1 3ab412ae0825: with no --repo the lint scanned scripts/.github/workflows and passed vacuously."""
    import shutil
    clone = tmp_path / "r"
    (clone / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(SCRIPT, clone / "scripts" / "ci" / SCRIPT.name)
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "bare.yml").write_text("on:\n  pull_request:\njobs: {}\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(clone / "scripts" / "ci" / SCRIPT.name)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 1 and "bare.yml" in proc.stdout, proc.stdout + proc.stderr


def test_bom_prefixed_workflow_is_read(tmp_path):
    """Windows tooling BOMs files; the lint reads utf-8-sig (Prism P0 26fe00113f79, footguns policy)."""
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "bom.yml").write_bytes(b"\xef\xbb\xbfon:\n  pull_request:\njobs: {}\n")
    assert L.findings(tmp_path) == L.lint_text(".github/workflows/bom.yml", "on:\n  pull_request:\njobs: {}\n")


def test_lint_passes_the_windows_footguns_check():
    """Prism P0 26fe00113f79: the blocking footguns lint scans scripts/, so this script must pass it."""
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "check-windows-footguns.py"), str(SCRIPT)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_pull_request_shapes():
    assert L.lint_text("w", "on:\n  pull_request:\njobs: {}\n")
    assert L.lint_text("w", "on: [pull_request]\njobs: {}\n")
    assert not L.lint_text("w", "on:\n  pull_request:\n    paths: ['x/**']\njobs: {}\n")
    assert not L.lint_text("w", "on:\n  pull_request:\nconcurrency: {group: g}\njobs: {}\n")
    assert not L.lint_text("w", "on:\n  pull_request:  # no-paths-ok: reads the PR body\njobs: {}\n")


def test_selftest_and_this_repo_are_green():
    for args in (["--selftest"], ["--repo", str(ROOT)]):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_scalar_workflows_is_one_upstream():  # Prism P1 b731e7859a9b
    text = "on:\n  workflow_run:\n    workflows: Build\n    types: [completed]\njobs:\n  j:\n    if: github.event.workflow_run.conclusion == 'failure'\n"
    assert not L.lint_text("w", text)


def test_missing_repo_exits_2(tmp_path):  # Prism P1 1ec058f04ff8
    proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(tmp_path / "nope")], capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr


# --- Apollo send-back on hermes-home#3576: marker scope (Prism 95a4fac5d32d) + one file in two repos ----
SCRIPT_MARKER = "jobs:\n  j:\n    steps:\n      - run: |\n          pull_request: # no-paths-ok: example\n"


def test_marker_inside_a_run_script_does_not_exempt_the_trigger():
    assert L.lint_text("w", "on: [pull_request]\n" + SCRIPT_MARKER)
    assert L.lint_text("w", "on:\n  pull_request:\n" + SCRIPT_MARKER)
    assert L.lint_text("w", "on:\n  workflow_run:\n    workflows: [A, B, C, D]\n    types: [completed]\n"
                            "jobs:\n  j:\n    if: true\n    steps:\n      - run: |\n          workflow_run: # trigger-storm-ok: x\n")


def test_marker_on_the_trigger_itself_exempts():
    assert not L.lint_text("w", "on:\n  pull_request:  # no-paths-ok: reads the PR body\n" + SCRIPT_MARKER)
    assert not L.lint_text("w", "on: [pull_request]  # no-paths-ok: reads the PR body\njobs: {}\n")
    assert not L.lint_text("w", "on: pull_request  # no-paths-ok: reads the PR body\njobs: {}\n")


# hermes-home scripts/workflow-trigger-storm-lint.py pins the same digest (scripts/tests/
# test_workflow_trigger_storm_lint.py there): a change to one copy fails until both repos carry the same bytes.
LINT_SHA256 = "b687922e4606a5b4f37c0c70b5c9c17e0d7bb98f134f533c912e112cb114d9e3"


def test_lint_bytes_match_the_hermes_home_copy():
    import hashlib
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == LINT_SHA256
