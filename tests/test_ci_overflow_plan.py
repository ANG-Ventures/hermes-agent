"""Contracts for the trusted CI overflow decision and untrusted request boundary."""
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

from scripts.ci_overflow_plan import Policy, Snapshot, parse_request, plan, sample_pool

POOL = ["self-hosted", "Linux", "X64", "hermes-ci"]
SLICES = [{"job_id": "core-smoke", "core": True, "estimated_duration_s": 10},
          {"job_id": "slice-0", "core": False, "estimated_duration_s": 30},
          {"job_id": "slice-1", "core": False, "estimated_duration_s": 5}]
E2E = {"job_id": "e2e", "estimated_duration_s": 10}
NOW = datetime.now(timezone.utc).isoformat()


def decide(*, idle=0, queued=0, online=2, status="ok", mode="overflow", k=4,
           arm=0, allowance=200, slices=SLICES, e2e=E2E):
    return plan(slices, e2e, Snapshot(NOW, status, online, idle, queued),
                Policy(mode, k, arm), allowance)


@pytest.mark.parametrize("name,idle,queued,expected", [
    ("idle-zero", 0, 0, []), ("idle-one", 1, 0, ["core-smoke"]),
    ("idle-many", 5, 0, ["core-smoke", "e2e", "slice-0", "slice-1"]),
    ("idle-minus-queued", 3, 2, ["core-smoke"]),
])
def test_local_priority(name, idle, queued, expected):
    p = decide(idle=idle, queued=queued)
    assert [j.job_id for j in p.jobs if j.reason == "local-idle"] == expected, name


@pytest.mark.parametrize("name,kwargs,incident,cloud", [
    ("confirmed-offline", {"online": 0}, "pool-offline", 4),
    ("unknown", {"status": "unknown"}, "telemetry-unavailable", 0),
    ("self-only", {"mode": "self-only"}, None, 0),
    ("cloud-only", {"mode": "cloud-only", "idle": 4}, None, 4),
    ("invalid-k", {"k": None}, "invalid-k", 0),
    ("explicit-zero-k", {"k": 0, "idle": 4}, None, 4),
])
def test_modes(name, kwargs, incident, cloud):
    p = decide(**kwargs)
    assert sum(j.reserved_minutes > 0 for j in p.jobs) == cloud, name
    assert (incident in p.incidents) if incident else not p.incidents


@pytest.mark.parametrize("name,budget,admitted", [
    ("zero", 0, []), ("below-slice", 34, ["e2e"]),
    ("slice", 35, ["core-smoke"]), ("e2e-plus-slice", 55, ["core-smoke", "e2e"]),
])
def test_budget_boundaries(name, budget, admitted):
    p = decide(allowance=budget)
    assert [j.job_id for j in p.jobs if j.reserved_minutes] == admitted, name
    assert all(j.reason == "budget-queue" for j in p.jobs if j.job_id not in admitted)


def test_cloud_only_budget_denial_is_prominent():
    p = decide(mode="cloud-only", allowance=0)
    assert all(j.reason == "budget-overrides-cloud-only" for j in p.jobs)
    assert p.summary["budget_overrides_cloud_only"] is True


def test_arm_only_cloud_tail_lightest_first():
    p = decide(idle=2, arm=1)
    labels = {j.job_id: j.labels for j in p.jobs}
    assert labels["core-smoke"] == labels["e2e"] == POOL
    assert labels["slice-1"] == ["ubuntu-24.04-arm"]
    assert labels["slice-0"] == ["ubuntu-latest"]
    assert all(j.labels != ["ubuntu-24.04-arm"] for j in decide(idle=4, arm=3).jobs)


@pytest.mark.parametrize("value", [None, "", "bad", "-1", 1.5, True])
def test_invalid_k_never_cloud(value):
    assert all(not j.reserved_minutes for j in decide(k=value).jobs)


def test_unapproved_policy_label_rejected():
    with pytest.raises(ValueError):
        plan(SLICES, E2E, Snapshot(NOW, "ok", 1, 0, 0),
             Policy("overflow", 1, 0, allowed_labels=["macos-latest"]), 100)


def request(slices=SLICES, e2e=E2E, **extra):
    data = {"slices": slices, "e2e": e2e, **extra}
    return json.dumps(data).encode()


@pytest.mark.parametrize("name,payload", [
    ("duplicate-id", request(SLICES + [SLICES[0]])),
    ("bad-core", request([{"job_id": "x", "core": 1, "estimated_duration_s": 1}])),
    ("negative-weight", request([{"job_id": "x", "core": False, "estimated_duration_s": -1}])),
    ("nan-weight", b'{"slices":[{"job_id":"x","core":false,"estimated_duration_s":NaN}],"e2e":null}'),
    ("huge-weight", request([{"job_id": "x", "core": False, "estimated_duration_s": 86401}])),
    ("unexpected", request(extra="injection")),
    ("generic-256", request([{"job_id": str(i), "core": False, "estimated_duration_s": 1} for i in range(257)])),
    ("controller-17", request([{"job_id": str(i), "core": False, "estimated_duration_s": 1} for i in range(18)])),
    ("too-many-bytes", b" " * (1024 * 1024 + 1)),
])
def test_request_rejects(name, payload):
    with pytest.raises(ValueError):
        parse_request(payload)


def test_request_accepts_17_and_bounded_reservation():
    rows = [{"job_id": str(i), "core": i == 0, "estimated_duration_s": 1} for i in range(17)]
    r = parse_request(request(rows))
    assert len(r.slices) == 17
    assert r.max_reservation == 615


def test_archive_traversal_and_symlink_refused():
    for filename in ("../request.json", "a/request.json"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(filename, request())
        with pytest.raises(ValueError):
            parse_request(buf.getvalue())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for filename in ("request.json", "second.json"):
            z.writestr(filename, request())
    with pytest.raises(ValueError):
        parse_request(buf.getvalue())


class Pages:
    def __init__(self, fail=False, partial=False):
        self.fail, self.partial = fail, partial
    def get(self, path, params=None):
        page = (params or {}).get("page", 1)
        if self.fail and "jobs" in path:
            raise RuntimeError("API 403")
        if "runners" in path:
            return {"total_count": 2, "runners": [
                {"id": 1, "status": "online", "busy": False, "labels": [{"name": x} for x in POOL]},
                {"id": 2, "status": "offline", "busy": False, "labels": [{"name": x} for x in POOL]}]}
        if path.endswith("/runs"):
            return {"total_count": 1, "workflow_runs": [{"id": 11, "run_attempt": 1, "status": "in_progress"}]}
        if self.partial and "jobs" in path:
            return {"total_count": 101, "jobs": []}
        return {"total_count": 2, "jobs": [
            {"id": 1, "status": "queued", "labels": POOL},
            {"id": 2, "status": "queued", "labels": ["ubuntu-latest"]}]}


@pytest.mark.parametrize("name,api,status,queued", [
    ("full-pages", Pages(), "ok", 1),
    ("api-error", Pages(fail=True), "unknown", 0),
    ("partial-pages", Pages(partial=True), "unknown", 0),
])
def test_sampler(name, api, status, queued):
    s = sample_pool(api)
    assert (s.status, s.queued_matching_jobs) == (status, queued), name
