"""Gate/plumbing scheduling must not wait for a self-hosted test slot."""

import re
from pathlib import Path

import pytest
import yaml

from tests.test_ci_overflow_workflow_contract import evaluate

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
# The fork leaves CI_PLUMBING_RUNNER unset (hosted, as before). The private CI mirror
# ANG-Ventures/hermes-agent-ci sets it to self-hosted labels: a private repo's hosted
# minutes are billed, and the org's Actions budget is a $0 hard stop.
PLUMBING = "CI_PLUMBING_RUNNER"
MIRROR_LABELS = '["self-hosted","hermes-ci"]'
HOSTED_LINUX = re.compile(r"^ubuntu-(latest|\d\d\.\d\d)$")
# Jobs that need root (sudo apt-get) or a docker socket. The self-hosted pool has neither
# (no-new-privileges, no socket), so the plumbing var must not move them there: they stay
# literal hosted. On the private mirror GitHub refuses them (no hosted budget), which the
# mirror reports as "not started", never as a green.
NEEDS_ROOT_OR_DOCKER = {
    ("docker-lint.yml", "hadolint"),          # hadolint-action is a container action
    ("rust-tests.yml", "bootstrap-installer"),  # sudo apt-get webkit2gtk
    ("js-tests.yml", "check"),                # sudo apt-get xvfb/gtk
    ("js-tests.yml", "ui-tests"),             # sudo apt-get xvfb/gtk
    ("e2e-desktop.yml", "e2e"),               # sudo apt-get gtk/nss/xvfb
}
_USES = re.compile(r"^\./\.github/workflows/([A-Za-z0-9._-]+\.ya?ml)$")


def _resolve(runs_on, variables):
    if isinstance(runs_on, str) and runs_on.strip().startswith("${{"):
        return evaluate(runs_on, {"vars": variables}, {})
    return runs_on


def _ci_graph_jobs():
    """(workflow, job_id, job) for every job reachable from ci.yaml through local `uses:`."""
    seen, todo, out = set(), ["ci.yaml"], []
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for job_id, job in yaml.safe_load((WORKFLOWS / name).read_text())["jobs"].items():
            m = _USES.match(job.get("uses", ""))
            if m:
                todo.append(m.group(1))
            elif "runs-on" in job:
                out.append((name, job_id, job))
    assert "tests.yml" in seen and len(out) > 10, "ci.yaml graph discovery must not silently shrink"
    return out


def _plumbing_routed():
    return [(w, j, job["runs-on"]) for w, j, job in _ci_graph_jobs()
            if isinstance(job["runs-on"], str) and PLUMBING in job["runs-on"]]


def test_ci_graph_has_no_literal_hosted_linux_runner():
    # A literal hosted label cannot be moved off GitHub-hosted runners by any repo var, so the
    # private mirror could not run it at all. Hosted-by-default jobs take the plumbing var.
    literal = [(w, j) for w, j, job in _ci_graph_jobs()
               if (w, j) not in NEEDS_ROOT_OR_DOCKER and any(isinstance(v, str) and HOSTED_LINUX.match(v)
                      for v in (job["runs-on"] if isinstance(job["runs-on"], list) else [job["runs-on"]]))]
    assert literal == []


def test_root_or_docker_jobs_never_take_the_plumbing_var():
    jobs = {(w, j): job["runs-on"] for w, j, job in _ci_graph_jobs()}
    for key in NEEDS_ROOT_OR_DOCKER:
        assert key in jobs, key
        assert _resolve(jobs[key], {PLUMBING: MIRROR_LABELS}) == "ubuntu-latest", key


@pytest.mark.parametrize("workflow,job_id,runs_on", _plumbing_routed(), ids=lambda v: str(v)[:40])
def test_plumbing_routed_job_is_hosted_unless_the_var_is_set(workflow, job_id, runs_on):
    assert HOSTED_LINUX.match(_resolve(runs_on, {})), (workflow, job_id)
    assert _resolve(runs_on, {PLUMBING: ""}) == _resolve(runs_on, {})
    assert _resolve(runs_on, {PLUMBING: MIRROR_LABELS}) == ["self-hosted", "hermes-ci"]
    # Never the heavy-job pool var: a saturated CI_RUNNER_LABELS pool must not strand plumbing.
    assert "CI_RUNNER_LABELS" not in runs_on


# Plumbing jobs too big for the ci-box pool (4g memory, 3g /_work tmpfs). The mirror sets
# CI_BIG_PLUMBING_RUNNER to the 8g, disk-/_work ace-ai/ace-media pool (label `overflow`).
# Measured in the runner image on ci-box (t_08dc2e4e): docs build 5.7 GiB peak / 2.2 GiB /_work,
# install.sh E2E 5.6 GiB /_work.
BIG_PLUMBING = "CI_BIG_PLUMBING_RUNNER"
BIG_JOBS = {("docs-site-checks.yml", "docs-site-checks"), ("bootstrap-installer.yml", "posix")}


@pytest.mark.parametrize("workflow,job_id", sorted(BIG_JOBS))
def test_big_plumbing_job_prefers_the_big_runner_var(workflow, job_id):
    runs_on = {(w, j): job["runs-on"] for w, j, job in _ci_graph_jobs()}[(workflow, job_id)]
    big = '["self-hosted","hermes-ci","overflow"]'
    assert _resolve(runs_on, {BIG_PLUMBING: big, PLUMBING: MIRROR_LABELS}) == ["self-hosted", "hermes-ci", "overflow"]
    # Unset or empty big var: the plain plumbing routing, unchanged.
    assert _resolve(runs_on, {BIG_PLUMBING: "", PLUMBING: MIRROR_LABELS}) == ["self-hosted", "hermes-ci"]
    assert HOSTED_LINUX.match(_resolve(runs_on, {}))


def test_only_the_big_jobs_read_the_big_runner_var():
    readers = {(w, j) for w, j, job in _ci_graph_jobs() if BIG_PLUMBING in str(job["runs-on"])}
    assert readers == BIG_JOBS


@pytest.mark.parametrize(
    "workflow,job_id",
    [
        ("ci.yaml", "detect"),
        ("tests.yml", "generate"),
        ("ci.yaml", "all-checks-pass"),
        ("ci.yaml", "ci-timings"),
        ("tests.yml", "save-durations"),
        # osv-scanner.yml's emit-status wrapper was retired with the per-PR OSV
        # run (upstream; the scan is weekly against main, no review_status job).
    ],
)
def test_plumbing_job_is_unconditionally_hosted(workflow, job_id):
    # Assert the actual scheduling declaration: CI_RUNNER_LABELS may point at a saturated
    # self-hosted pool, so only the plumbing var (unset on the fork) may move these.
    runs_on = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job_id]["runs-on"]
    assert _resolve(runs_on, {}) == "ubuntu-latest"
    assert _resolve(runs_on, {"CI_RUNNER_LABELS": MIRROR_LABELS}) == "ubuntu-latest"


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
        "&& needs.placement.outputs.e2e_runs_on || "
        "vars.CI_E2E_VM_ENABLED == 'true' && vars.CI_E2E_VM_ROUTE == 'open' && "
        "(github.event_name == 'push' || github.event_name == 'merge_group' || "
        "github.event.pull_request.head.repo.full_name == github.repository) "
        "&& '[\"self-hosted\",\"Linux\",\"X64\",\"ace-e2e-vm\"]' || ("
        "contains(fromJSON(vars.CI_RUNNER_LABELS || '[\"ubuntu-latest\"]'), 'self-hosted') "
        "&& format('[\"{0}\",\"X64\"]', join(fromJSON(vars.CI_RUNNER_LABELS), '\",\"')) "
        "|| '[\"ubuntu-latest\"]')) }}"
    )
    # The static last resort is GitHub-hosted ubuntu-latest (public repo, free
    # minutes; t_e95fdb01): a literal, so an unset repo var can never route it.
    # Blacksmith is reachable only through the overflow placement plan.
    assert "blacksmith" not in job["runs-on"]
    assert "hermes-ci" not in job["runs-on"]


def test_e2e_invocation_emits_stacks_before_job_cancellation():
    job = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())["jobs"]["e2e"]
    step = next(step for step in job["steps"] if step.get("name") == "Run e2e tests")
    # Upstream runs the e2e files through the per-file runner (one pytest per
    # file); the fork's stall-stack flag (#792) rides along as a bare pytest flag.
    assert "scripts/run_tests.sh --include-integration" in step["run"]
    assert "-o faulthandler_timeout=120" in step["run"]
    assert job["timeout-minutes"] * 60 > 120
