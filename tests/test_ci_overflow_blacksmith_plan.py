"""The Blacksmith rung is a PLANNED venue for merge_group, not a stamp Placement overwrites (t_fe4e801b).

Audit t_cd731d8e fire 2: three merge_group runs generated while CI_BLACKSMITH_SLICES=4, the
generator stamped Blacksmith on their tail, and Placement rewrote every stamp from a plan that
could only name POOL / ubuntu-latest / ARM. Blacksmith therefore never reached the lane that
saturates. The plan now carries the Blacksmith share (Policy.blacksmith_count, read by the
controller from the same variable), so plan == execution (AC6) covers it, and the ledger and
Placement accept exactly that one paid label for non-core, non-e2e, reserved slices.
"""
import base64
from datetime import datetime, timezone
import json

import pytest

from scripts.ci_overflow_ledger import Ledger
from scripts.ci_overflow_placement import PlanInvalid, validate_record
from scripts.ci_overflow_plan import (APPROVED, ARM, BLACKSMITH, POOL, X64, Policy, Snapshot, _labels,
                                      plan)

NOW = "2026-09-28T01:00:00+00:00"
SLICES = [{"job_id": "core smoke", "core": True, "estimated_duration_s": 40.0}] + [
    {"job_id": f"slice {i}/8", "core": False, "estimated_duration_s": 100.0 + i} for i in range(1, 8)]
E2E = {"job_id": "e2e", "estimated_duration_s": 60.0}
SATURATED = Snapshot(NOW, "ok", 4, 0, 6)   # pool busy: everything goes cloud


def decide(bs, arm=0, mode="cloud-only", allowance=6000):
    return plan(SLICES, E2E, SATURATED, Policy(mode, 4, arm, blacksmith_count=bs), allowance)


def labels_of(p):
    return {j.job_id: j.labels for j in p.jobs}


def test_plan_names_blacksmith_for_the_heaviest_non_core_cloud_slices():
    p = decide(bs=3)
    lab = labels_of(p)
    assert [jid for jid, x in lab.items() if x == BLACKSMITH] == ["slice 5/8", "slice 6/8", "slice 7/8"]
    assert lab["core smoke"] == lab["e2e"] == X64
    assert all(j.reserved_minutes == 35 for j in p.jobs if j.labels == BLACKSMITH)
    assert p.summary["blacksmith"] == 3


def test_blacksmith_never_takes_arm_core_or_e2e():
    p = decide(bs=17, arm=2)
    lab = labels_of(p)
    assert lab["core smoke"] == lab["e2e"] == X64
    assert [jid for jid, x in lab.items() if x == ARM] == ["slice 1/8", "slice 2/8"]   # lightest go ARM
    assert sorted(jid for jid, x in lab.items() if x == BLACKSMITH) == [f"slice {i}/8" for i in range(3, 8)]


def test_blacksmith_only_relabels_reserved_cloud_slices():
    # Pool idle: overflow keeps everything local, so no slice holds a reservation -> none may go paid.
    p = plan(SLICES, E2E, Snapshot(NOW, "ok", 20, 20, 0), Policy("overflow", 20, 0, blacksmith_count=4), 6000)
    assert all(j.labels == POOL for j in p.jobs)
    assert p.summary["blacksmith"] == 0


@pytest.mark.parametrize("value", [None, "", "0", 0, "bad", "-1", 1.5, True])
def test_unset_zero_or_invalid_count_places_nothing_on_blacksmith(value):
    p = decide(bs=value)
    assert not any(j.labels == BLACKSMITH for j in p.jobs)


def test_invalid_count_is_an_incident_not_a_silent_zero():
    assert "invalid-blacksmith" in decide(bs="bad").incidents
    assert "invalid-blacksmith" not in decide(bs=None).incidents


def test_policy_override_still_cannot_smuggle_a_paid_label():
    """AC7 keeps holding: the only path to BLACKSMITH is the counted share, never allowed_labels."""
    assert tuple(BLACKSMITH) not in APPROVED
    with pytest.raises(ValueError):
        _labels(BLACKSMITH)
    with pytest.raises(ValueError):
        plan(SLICES, E2E, SATURATED, Policy("cloud-only", 4, 0, allowed_labels=[POOL, X64, BLACKSMITH]), 6000)


class _Contents:
    """In-memory Contents API (same wire shape the Ledger reads/writes)."""

    def __init__(self):
        self.state, self.sha = {"version": 1, "attempts": {}, "daily_totals": {}}, "s0"

    def get(self, path, params):
        return {"sha": self.sha, "encoding": "base64",
                "content": base64.b64encode(json.dumps(self.state).encode()).decode()}

    def put(self, path, payload):
        assert payload["sha"] == self.sha
        self.state, self.sha = json.loads(base64.b64decode(payload["content"])), self.sha + "'"
        return {"content": {"sha": self.sha}}


def test_ledger_admits_a_plan_that_names_blacksmith():
    api = _Contents()
    led = Ledger(api, daily_limit=6000, clock=lambda: datetime(2026, 9, 28, 1, tzinfo=timezone.utc))
    res = led.reserve((1, 2, 1), decide(bs=2))
    assert getattr(res, "reason", None) is None, res
    row = next(iter(api.state["attempts"].values()))
    assert sum(1 for j in row["jobs"] if j["labels"] == BLACKSMITH) == 2


def test_ledger_recounts_blacksmith_after_budget_demotion():
    """A reservation the ledger cannot afford is demoted to POOL; the persisted summary must count
    the Blacksmith slices actually admitted, not the planner's pre-ledger share (FleetReview ad229ed8)."""
    api = _Contents()
    led = Ledger(api, daily_limit=6000, clock=lambda: datetime(2026, 9, 28, 1, tzinfo=timezone.utc))
    p = decide(bs=4)
    assert p.summary["blacksmith"] == 4
    led.daily_limit = 35 * 2 + 20   # affords core smoke, e2e and one slice; every Blacksmith slice demotes
    res = led.reserve((1, 2, 1), p)
    assert getattr(res, "reason", None) is None, res
    admitted = sum(1 for j in res.plan.jobs if j.labels == BLACKSMITH)
    assert res.plan.summary["blacksmith"] == admitted == 0
    stored = next(iter(api.state["attempts"].values()))["plan"]["summary"]
    assert stored["blacksmith"] == 0


def _record(p, matrix):
    jobs = [{"job_id": j.job_id, "labels": j.labels, "reason": j.reason, "reserved_minutes": j.reserved_minutes}
            for j in p.jobs]
    summary = {"validated": True, "repository_id": 1, "run_id": 2, "run_attempt": 1, "head_sha": "abc",
               "request_digest": "sha256:x", "policy_version": "v"}
    return {"version": 1, "attempts": {"1:2:1": {"plan": {"jobs": jobs, "summary": summary},
                                                  "jobs": [dict(j) for j in jobs]}}}


MATRIX = {"slice": [{"name": s["job_id"], "runs_on": json.dumps(X64)} for s in SLICES]}
IDENT = dict(repository_id=1, run_id=2, run_attempt=1, head_sha="abc", request_digest="sha256:x", matrix=MATRIX,
             core_ids={"core smoke"})


def test_placement_executes_the_planned_blacksmith_labels():
    placed = validate_record(_record(decide(bs=2), MATRIX), **IDENT)
    runs_on = {r["name"]: json.loads(r["runs_on"]) for r in placed["matrix"]["slice"]}
    assert [n for n, x in runs_on.items() if x == BLACKSMITH] == ["slice 6/8", "slice 7/8"]


@pytest.mark.parametrize("victim", ["core smoke", "e2e"])
def test_placement_refuses_blacksmith_on_core_or_e2e(victim):
    state = _record(decide(bs=0), MATRIX)
    row = state["attempts"]["1:2:1"]
    for jobs in (row["plan"]["jobs"], row["jobs"]):
        next(j for j in jobs if j["job_id"] == victim)["labels"] = list(BLACKSMITH)
    with pytest.raises(PlanInvalid, match="may not run on Blacksmith"):
        validate_record(state, **IDENT)
