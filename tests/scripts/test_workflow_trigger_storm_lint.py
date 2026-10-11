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


def test_pull_request_shapes():
    assert L.lint_text("w", "on:\n  pull_request:\njobs: {}\n")
    assert L.lint_text("w", "on: [pull_request]\njobs: {}\n")
    assert not L.lint_text("w", "on:\n  pull_request:\n    paths: ['x/**']\njobs: {}\n")
    assert not L.lint_text("w", "on:\n  pull_request:\nconcurrency: {group: g}\njobs: {}\n")
    assert not L.lint_text("w", "on:\n  pull_request:  # no-paths-ok: reads the PR body\njobs: {}\n")


def test_selftest_and_this_repo_are_green():
    for args in (["--selftest"], ["--repo", str(ROOT)]):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
