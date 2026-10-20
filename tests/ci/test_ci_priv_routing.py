"""ci-priv routing (t_df855b2a): root/docker jobs move to the ci-priv class only on the private mirror.

Evaluates the REAL ``runs-on`` expressions with the contract suite's GitHub-expression evaluator.
A job goes to ci-priv only when ``CI_PRIV_ENABLED == 'true'`` (Ace's switch) AND ``CI_PRIV_RUNNER``
names the labels AND the event is trusted (push, merge_group, same-repo pull_request). With the vars
unset (the public fork) every one of them is the literal ``ubuntu-latest`` it was before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.test_ci_overflow_workflow_contract import evaluate

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
REPO = "ANG-Ventures/hermes-agent-ci"
PRIV = '["self-hosted","ci-priv"]'
STATUS = {"always": True, "cancelled": False, "failure": False, "success": True}
JOBS = [
    ("rust-tests.yml", "bootstrap-installer"),
    ("js-tests.yml", "check"),
    ("js-tests.yml", "ui-tests"),
    ("docker-lint.yml", "hadolint"),
    ("e2e-desktop.yml", "e2e"),
]
CONTAINER_SHARDS = ["hosts/test_libc_musl", "core/test_fresh_process_entrypoints"]


def _runs_on(workflow, job):
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job]["runs-on"]


def _ctx(enabled, runner, event="pull_request", head_repo=REPO, shard=None, **more):
    gh = {"repository": REPO, "event_name": event,
          "event": {"pull_request": {"head": {"repo": {"full_name": head_repo}}}} if event == "pull_request" else {}}
    ctx = {"github": gh, "vars": {"CI_PRIV_ENABLED": enabled, "CI_PRIV_RUNNER": runner, **more}}
    if shard is not None:
        ctx["matrix"] = {"shard": shard}
    return ctx


@pytest.mark.parametrize("workflow,job", JOBS)
@pytest.mark.parametrize("event", ["pull_request", "push", "merge_group"])
def test_root_docker_job_routes_to_ci_priv_when_switched_on(workflow, job, event):
    got = evaluate(_runs_on(workflow, job), _ctx("true", PRIV, event), STATUS)
    assert got == ["self-hosted", "ci-priv"]


@pytest.mark.parametrize("workflow,job", JOBS)
@pytest.mark.parametrize("enabled,runner", [(None, None), ("false", PRIV), (None, PRIV), ("true", None), ("true", "")])
def test_root_docker_job_stays_hosted_unless_both_vars_say_so(workflow, job, enabled, runner):
    assert evaluate(_runs_on(workflow, job), _ctx(enabled, runner), STATUS) == "ubuntu-latest"


@pytest.mark.parametrize("workflow,job", JOBS)
@pytest.mark.parametrize("event,head_repo", [
    ("pull_request", "someone-else/hermes-agent"),  # fork PR: untrusted code never reaches our hosts
    ("pull_request", None),
    ("workflow_dispatch", REPO),
    ("schedule", REPO),
])
def test_untrusted_events_never_reach_ci_priv(workflow, job, event, head_repo):
    got = evaluate(_runs_on(workflow, job), _ctx("true", PRIV, event, head_repo), STATUS)
    assert got == "ubuntu-latest"


@pytest.mark.parametrize("shard", CONTAINER_SHARDS)
def test_container_sensitive_upgrade_shards_route_to_ci_priv(shard):
    expr = _runs_on("tests.yml", "e2e-upgrade")
    ctx = _ctx("true", PRIV, shard=shard, CI_E2E_VM_ENABLED="true", CI_E2E_VM_ROUTE="open", CI_E2E_VM_SHARDS=shard)
    assert evaluate(expr, ctx, STATUS) == ["self-hosted", "ci-priv"]


@pytest.mark.parametrize("shard", CONTAINER_SHARDS)
def test_container_sensitive_upgrade_shards_fall_back_without_ci_priv(shard):
    expr = _runs_on("tests.yml", "e2e-upgrade")
    ctx = _ctx(None, None, shard=shard, CI_E2E_RUNNER="hermes-ci")
    assert evaluate(expr, ctx, STATUS) == "hermes-ci"
    assert evaluate(expr, _ctx("true", PRIV, "pull_request", "someone-else/hermes-agent", shard=shard), STATUS) \
        == "ubuntu-latest"


def test_other_upgrade_shards_never_take_ci_priv():
    expr = _runs_on("tests.yml", "e2e-upgrade")
    ctx = _ctx("true", PRIV, shard="core/test_upgrade_path")
    assert evaluate(expr, ctx, STATUS) == "ubuntu-latest"
    vm = _ctx("true", PRIV, shard="core/test_upgrade_path", CI_E2E_VM_ENABLED="true", CI_E2E_VM_ROUTE="open",
              CI_E2E_VM_SHARDS="core/test_upgrade_path")
    assert evaluate(expr, vm, STATUS) == ["self-hosted", "Linux", "X64", "ace-e2e-vm"]
