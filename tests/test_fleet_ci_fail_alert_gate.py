"""fleet-ci-fail-alert page gate (t_957f5398).

Executes the REAL ``route`` step script from the workflow YAML against run
payloads, so a regression in the YAML (not a copy of it) fails. #alerts pages
only for reds on the default branch, merge_group and schedule; worker-branch
and pull_request reds stay on the PR.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "fleet-ci-fail-alert.yml"

pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("jq")), reason="needs bash + jq")


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in data:  # YAML 1.1 parses `on` as True
        data["on"] = data.pop(True)
    return data


def _route_step() -> dict:
    steps = _workflow()["jobs"]["notify-on-failure"]["steps"]
    return next(s for s in steps if s.get("id") == "route")


def _route(tmp_path: Path, *, event: str, branch: str, conclusion: str = "failure",
           name: str = "CI") -> dict:
    step = _route_step()
    run = {"name": name, "workflow_id": 1, "id": 42, "head_branch": branch, "head_sha": "abc",
           "conclusion": conclusion, "html_url": "https://x/42", "actor": {"login": "Kyzcreig"},
           "event": event}
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(out),
           "GH_TOKEN": "x", "REPO": "o/r", "EVENT_NAME": "workflow_run", "DEFAULT_BRANCH": "main",
           "REPLAY_RUN_ID": "", "RUN_JSON": json.dumps(run), "KNOWN_RED": step["env"]["KNOWN_RED"]}
    proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


@pytest.mark.parametrize("event,branch", [
    ("push", "main"),
    ("workflow_dispatch", "main"),
    ("merge_group", "gh-readonly-queue/main/pr-1267-059d3994cfe01c8e455738138163b7fba9ad0174"),
    ("schedule", "main"),
])
def test_actionable_reds_page(tmp_path, event, branch):
    assert _route(tmp_path, event=event, branch=branch)["route"] == "alerts"


def test_startup_failure_on_main_pages(tmp_path):
    assert _route(tmp_path, event="push", branch="main", conclusion="startup_failure")["route"] == "alerts"


@pytest.mark.parametrize("event,branch", [
    ("workflow_dispatch", "fix/install-e2e-node-pty-gyp-flake"),  # the card's sample page
    ("pull_request", "daedalus/t_1234abcd"),
    ("pull_request", "main"),  # fork PR whose head branch is named main
    ("push", "daedalus/t_bc0052b9-THROWAWAY-repro"),
    ("push", "feature/x"),
])
def test_branch_reds_do_not_page(tmp_path, event, branch):
    assert _route(tmp_path, event=event, branch=branch)["route"] == "none"


def test_known_red_on_branch_never_queries_or_pages(tmp_path):
    got = _route(tmp_path, event="workflow_dispatch", branch="fix/x", name="Install & Update E2E")
    assert got["route"] == "none" and got["card"] == ""


def test_non_failure_conclusion_is_silent(tmp_path):
    assert _route(tmp_path, event="push", branch="main", conclusion="success")["route"] == "none"


def test_post_step_is_gated_on_route():
    steps = _workflow()["jobs"]["notify-on-failure"]["steps"]
    post = next(s for s in steps if s.get("name", "").startswith("Sign and POST"))
    assert post["if"] == "${{ steps.route.outputs.route != 'none' }}"
