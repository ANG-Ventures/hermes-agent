"""VM-class e2e lane routing (t_78d851d1): kill switch + load gate, static fall-through.

Evaluates the REAL ``runs-on`` expressions in tests.yml with the contract suite's own
GitHub-expression evaluator, so the assertion is about what GitHub would pick, not
about the text. The lane goes to ``ace-e2e-vm`` only when BOTH repo vars say so:
``CI_E2E_VM_ENABLED == 'true'`` (Ace's kill switch, default off) and
``CI_E2E_VM_ROUTE == 'open'`` (written by the Studio load gate). Any other value, or
an unset var, falls through to exactly what the job picked before this lane existed.
"""

from __future__ import annotations

import copy
import json

import pytest

from tests.test_ci_overflow_workflow_contract import (
    GEN_MATRIX,
    PLACEMENT_OUTCOMES,
    _ctx,
    _tests_yml,
    evaluate,
)

VM = ["self-hosted", "Linux", "X64", "ace-e2e-vm"]
# Static tail when neither lane applies (t_e95fdb01: GitHub-hosted, public repo).
STATIC = "ubuntu-latest"
STATUS = {"always": True, "cancelled": False, "failure": False, "success": True}

# (CI_E2E_VM_ENABLED, CI_E2E_VM_ROUTE) -> routed to the VM class?
GATE_CASES = [
    ("true", "open", True),
    ("true", "closed", False),
    ("true", None, False),
    ("false", "open", False),
    (None, "open", False),
    (None, None, False),
    ("TRUE", "open", True),  # GitHub's == is case-insensitive on strings
    ("1", "open", False),
]


REPO = "ANG-Ventures/hermes-agent"


def _vm_ctx(event, enabled, route, labels=None, placement="skipped", e2e_runner=None,
            shard="core/test_install_fresh", head_repo=REPO, vm_shards="core/test_install_fresh"):
    ctx = _ctx(event, copy.deepcopy(PLACEMENT_OUTCOMES[placement]), labels)
    ctx["matrix"] = {"shard": shard}
    ctx.setdefault("github", {})["repository"] = REPO
    ctx["github"]["event_name"] = event
    ctx["github"]["event"] = {"pull_request": {"head": {"repo": {"full_name": head_repo}}}} \
        if event == "pull_request" else {}
    ctx["vars"]["CI_E2E_VM_ENABLED"] = enabled
    ctx["vars"]["CI_E2E_VM_ROUTE"] = route
    ctx["vars"]["CI_E2E_RUNNER"] = e2e_runner
    ctx["vars"]["CI_E2E_VM_SHARDS"] = vm_shards
    return ctx


@pytest.mark.parametrize("enabled,route,vm", GATE_CASES)
@pytest.mark.parametrize("event", ["pull_request", "push", "merge_group"])
def test_e2e_routes_to_vm_only_when_switch_and_gate_agree(event, enabled, route, vm):
    expr = _tests_yml()["jobs"]["e2e"]["runs-on"]
    got = evaluate(expr, _vm_ctx(event, enabled, route), STATUS)
    assert got == (VM if vm else [STATIC])


@pytest.mark.parametrize("job", ["e2e", "e2e-upgrade"])
@pytest.mark.parametrize("event,head_repo", [
    ("pull_request", "someone-else/hermes-agent"),    # fork PR: untrusted code
    ("pull_request", None),                             # head repo deleted
    ("workflow_dispatch", REPO),                         # not in the trust list
    ("schedule", REPO),
])
def test_untrusted_events_never_reach_the_vm_class(job, event, head_repo):
    # Prism P0 (hermes-agent#1689): the class runs seccomp/AppArmor-unconfined on our hosts;
    # fork PRs must never leave GitHub/Blacksmith (same rule as CI_BLACKSMITH_SLICES).
    expr = _tests_yml()["jobs"][job]["runs-on"]
    got = evaluate(expr, _vm_ctx(event, "true", "open", head_repo=head_repo), STATUS)
    assert got not in (VM,) and "ace-e2e-vm" not in json.dumps(got)


@pytest.mark.parametrize("enabled,route,vm", GATE_CASES)
def test_e2e_upgrade_routes_to_vm_only_when_switch_and_gate_agree(enabled, route, vm):
    expr = _tests_yml()["jobs"]["e2e-upgrade"]["runs-on"]
    got = evaluate(expr, _vm_ctx("pull_request", enabled, route), STATUS)
    assert got == (VM if vm else STATIC)


def test_e2e_upgrade_closed_gate_keeps_ci_e2e_runner_override():
    expr = _tests_yml()["jobs"]["e2e-upgrade"]["runs-on"]
    ctx = _vm_ctx("pull_request", "true", "closed", e2e_runner="blacksmith-4vcpu-ubuntu-2404")
    assert evaluate(expr, ctx, STATUS) == "blacksmith-4vcpu-ubuntu-2404"


@pytest.mark.parametrize("shard", ["hosts/test_libc_musl", "core/test_fresh_process_entrypoints"])
def test_container_sensitive_shards_never_route_to_vm(shard):
    # Proof run 37170873916: musl needs a docker daemon; doctor probes systemctl under /.dockerenv.
    # Even when an operator lists them in CI_E2E_VM_SHARDS.
    expr = _tests_yml()["jobs"]["e2e-upgrade"]["runs-on"]
    ctx = _vm_ctx("pull_request", "true", "open", shard=shard, vm_shards=shard)
    assert evaluate(expr, ctx, STATUS) == STATIC


# (CI_E2E_VM_SHARDS, shard) -> routed? Exact comma-delimited names only (t_38cee27b).
SLICE_CASES = [
    (None, "core/test_upgrade_path", False),             # unset: no shard routes
    ("", "core/test_upgrade_path", False),
    ("core/test_upgrade_path", "core/test_upgrade_path", True),
    ("git/test_shallow_install,core/test_upgrade_path", "core/test_upgrade_path", True),
    ("git/test_shallow_install,core/test_upgrade_path", "git/test_shallow_install", True),
    ("git/test_shallow_install,core/test_upgrade_path", "pm/test_generation_gc", False),
    ("core/test_upgrade_path_extra", "core/test_upgrade_path", False),  # no prefix match
    ("x/core/test_upgrade_path", "core/test_upgrade_path", False),      # no suffix match
]


@pytest.mark.parametrize("vm_shards,shard,vm", SLICE_CASES)
def test_only_named_upgrade_shards_route_to_vm(vm_shards, shard, vm):
    # 2026-10-04: all 31 shards on 2 one-slot runners -> 180 min wall vs 16 hosted. The class
    # takes a bounded slice the Studio gate has slots for; the rest stays hosted.
    expr = _tests_yml()["jobs"]["e2e-upgrade"]["runs-on"]
    ctx = _vm_ctx("merge_group", "true", "open", shard=shard, vm_shards=vm_shards)
    assert evaluate(expr, ctx, STATUS) == (VM if vm else STATIC)


def test_e2e_job_is_not_bound_by_the_shard_slice():
    # The single e2e job is counted by the gate's floor on its own; the slice var never gates it.
    expr = _tests_yml()["jobs"]["e2e"]["runs-on"]
    assert evaluate(expr, _vm_ctx("push", "true", "open", vm_shards=None), STATUS) == VM


def test_validated_merge_group_plan_still_wins_over_vm_lane():
    # The attempt-bound managed plan is the first clause; the VM lane must not pre-empt it.
    expr = _tests_yml()["jobs"]["e2e"]["runs-on"]
    ctx = _vm_ctx("merge_group", "true", "open", placement="valid")
    ctx["vars"]["CI_OVERFLOW_PLACEMENT_ENABLED"] = "true"
    want = json.loads(PLACEMENT_OUTCOMES["valid"]["outputs"]["e2e_runs_on"])
    assert evaluate(expr, ctx, STATUS) == want


def test_closed_gate_with_self_hosted_pool_labels_is_unchanged():
    expr = _tests_yml()["jobs"]["e2e"]["runs-on"]
    ctx = _vm_ctx("pull_request", "true", "closed", labels='["self-hosted","hermes-ci"]')
    assert evaluate(expr, ctx, STATUS) == ["self-hosted", "hermes-ci", "X64"]


def test_bubblewrap_step_needs_no_sudo_when_bwrap_is_preinstalled():
    # The VM-class container keeps no-new-privileges: an unconditional `sudo apt-get`
    # would fail the job there. Install only when bwrap is absent; sysctl is best-effort.
    job = _tests_yml()["jobs"]["e2e-upgrade"]
    run = next(s for s in job["steps"] if s.get("name") == "Install bubblewrap")["run"]
    lines = [ln.strip() for ln in run.splitlines()]
    guard = lines.index("if ! command -v bwrap >/dev/null 2>&1; then")
    end = lines.index("fi", guard)
    for i, ln in enumerate(lines):
        if ln.startswith("sudo apt-get"):
            assert guard < i < end, ln
        if "sysctl" in ln and ln.startswith("sudo"):
            assert ln.startswith("sudo -n ") and ln.endswith("|| true"), ln
    assert GEN_MATRIX  # imported fixture sanity: the contract module loaded
