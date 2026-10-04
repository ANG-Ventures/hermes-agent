"""VM-class e2e lane routing (t_78d851d1): kill switch + load gate, Blacksmith fall-through.

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
BLACKSMITH = "blacksmith-4vcpu-ubuntu-2404"
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


def _vm_ctx(event, enabled, route, labels=None, placement="skipped", e2e_runner=None,
            shard="core/test_install_fresh"):
    ctx = _ctx(event, copy.deepcopy(PLACEMENT_OUTCOMES[placement]), labels)
    ctx["matrix"] = {"shard": shard}
    ctx["vars"]["CI_E2E_VM_ENABLED"] = enabled
    ctx["vars"]["CI_E2E_VM_ROUTE"] = route
    ctx["vars"]["CI_E2E_RUNNER"] = e2e_runner
    return ctx


@pytest.mark.parametrize("enabled,route,vm", GATE_CASES)
@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch", "merge_group"])
def test_e2e_routes_to_vm_only_when_switch_and_gate_agree(event, enabled, route, vm):
    expr = _tests_yml()["jobs"]["e2e"]["runs-on"]
    got = evaluate(expr, _vm_ctx(event, enabled, route), STATUS)
    assert got == (VM if vm else [BLACKSMITH])


@pytest.mark.parametrize("enabled,route,vm", GATE_CASES)
def test_e2e_upgrade_routes_to_vm_only_when_switch_and_gate_agree(enabled, route, vm):
    expr = _tests_yml()["jobs"]["e2e-upgrade"]["runs-on"]
    got = evaluate(expr, _vm_ctx("pull_request", enabled, route), STATUS)
    assert got == (VM if vm else BLACKSMITH)


def test_e2e_upgrade_closed_gate_keeps_ci_e2e_runner_override():
    expr = _tests_yml()["jobs"]["e2e-upgrade"]["runs-on"]
    ctx = _vm_ctx("pull_request", "true", "closed", e2e_runner="ubuntu-latest")
    assert evaluate(expr, ctx, STATUS) == "ubuntu-latest"


@pytest.mark.parametrize("shard", ["hosts/test_libc_musl", "core/test_fresh_process_entrypoints"])
def test_container_sensitive_shards_never_route_to_vm(shard):
    # Proof run 37170873916: musl needs a docker daemon; doctor probes systemctl under /.dockerenv.
    expr = _tests_yml()["jobs"]["e2e-upgrade"]["runs-on"]
    assert evaluate(expr, _vm_ctx("pull_request", "true", "open", shard=shard), STATUS) == BLACKSMITH


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
