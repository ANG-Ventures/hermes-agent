"""Cheap, required CI gates must not wait for a self-hosted runner."""
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        ("fleet-sast.yml", "sast"),
        ("fleet-secret-scan.yml", "secret_scan"),
        ("review-labels.yml", "check"),
        ("supply-chain-audit.yml", "scan"),
        ("skills-index-freshness.yml", "check-freshness"),
    ],
)
def test_portable_required_gate_has_hosted_runner(workflow: str, job: str) -> None:
    runs_on = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job]["runs-on"]
    # Literal, or the plumbing var that only the private CI mirror sets (tests/ci/test_plumbing_runner_routing.py).
    if runs_on != "ubuntu-latest":
        assert runs_on == "${{ vars.CI_PLUMBING_RUNNER && fromJSON(vars.CI_PLUMBING_RUNNER) || 'ubuntu-latest' }}"


def test_heavy_python_jobs_remain_variable_routed() -> None:
    jobs = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())["jobs"]
    assert jobs["test"]["runs-on"] == "${{ fromJSON(matrix.slice.runs_on) }}"
    assert "CI_RUNNER_LABELS" in jobs["e2e"]["runs-on"]
