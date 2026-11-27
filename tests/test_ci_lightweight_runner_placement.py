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


@pytest.mark.parametrize("event", ["push", "merge_group", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("head_repo", ["o/r", "outside/fork"])
def test_heavy_python_jobs_remain_variable_routed(event, head_repo) -> None:
    from tests.test_ci_overflow_workflow_contract import evaluate

    jobs = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())["jobs"]
    labels = ["self-hosted", "universal-ci"]
    ctx = {"github": {"event_name": event, "repository": "o/r",
                      "event": {"pull_request": {"head": {"repo": {"full_name": head_repo}}}}},
           "matrix": {"slice": {"runs_on": '["self-hosted","universal-ci"]'}}}
    trusted = event in {"push", "merge_group"} or (event == "pull_request" and head_repo == "o/r")
    assert evaluate(jobs["test"]["runs-on"], ctx, {}) == (labels if trusted else "ubuntu-latest")
    if not trusted:
        ctx["matrix"]["slice"]["runs_on"] = "malformed-json"
        assert evaluate(jobs["test"]["runs-on"], ctx, {}) == "ubuntu-latest"
    assert "CI_RUNNER_LABELS" in jobs["e2e"]["runs-on"]
