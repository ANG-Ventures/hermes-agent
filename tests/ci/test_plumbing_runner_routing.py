"""Gate/plumbing scheduling must not wait for a self-hosted test slot."""

from pathlib import Path

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


@pytest.mark.parametrize(
    "workflow,job_id",
    [
        ("ci.yaml", "detect"),
        ("tests.yml", "generate"),
        ("ci.yaml", "all-checks-pass"),
        ("ci.yaml", "ci-timings"),
        ("tests.yml", "save-durations"),
        ("osv-scanner.yml", "emit-status"),
    ],
)
def test_plumbing_job_is_unconditionally_hosted(workflow, job_id):
    # Assert the actual scheduling declaration, not a simulated default:
    # CI_RUNNER_LABELS may point at a saturated self-hosted pool.
    jobs = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]
    assert jobs[job_id]["runs-on"] == "ubuntu-latest"


def test_every_workflow_parses():
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert paths, "workflow discovery must not silently become empty"
    for path in paths:
        workflow = yaml.safe_load(path.read_text())
        assert isinstance(workflow.get("jobs"), dict), path.name


def test_ci_review_label_requests_full_rerun():
    job = yaml.safe_load((WORKFLOWS / "label-rerun.yml").read_text())["jobs"]["rerun-review-labels"]
    command = job["steps"][0]["run"]
    # Failed-only rerun of this dynamic matrix has produced startup_failure / zero jobs.
    assert command.startswith("set -euo pipefail\n")
    assert 'gh run rerun "$RUN_ID" --repo "$REPO"\n' in command
    assert "--failed" not in command
    assert 'gh run rerun "$RUN_ID" --repo "$REPO" || true' not in command


def test_e2e_self_hosted_architecture_and_hosted_fallback_binding():
    # Pin the declaration: injecting labels into a test would bypass this binding.
    job = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())["jobs"]["e2e"]
    # Every case except a validated, attempt-bound managed plan (non-merge_group,
    # switch OFF, or NO plan — t_42bed567) ends in the legacy binding; never a
    # fixed local pool.
    assert job["runs-on"].startswith("${{ fromJSON(github.event_name == 'merge_group' && "
                                     "vars.CI_OVERFLOW_PLACEMENT_ENABLED == 'true' && ")
    assert job["runs-on"].endswith(
        "&& needs.placement.outputs.e2e_runs_on || ("
        "contains(fromJSON(vars.CI_RUNNER_LABELS || '[\"ubuntu-latest\"]'), 'self-hosted') "
        "&& format('[\"{0}\",\"X64\"]', join(fromJSON(vars.CI_RUNNER_LABELS), '\",\"')) "
        "|| '[\"ubuntu-latest\"]')) }}"
    )
    assert "hermes-ci" not in job["runs-on"]


def test_e2e_invocation_emits_stacks_before_job_cancellation():
    job = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())["jobs"]["e2e"]
    step = next(step for step in job["steps"] if step.get("name") == "Run e2e tests")
    assert "python -m pytest tests/e2e/ -v --tb=short -o faulthandler_timeout=120" in step["run"]
    assert job["timeout-minutes"] * 60 > 120
