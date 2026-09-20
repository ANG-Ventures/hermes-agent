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
