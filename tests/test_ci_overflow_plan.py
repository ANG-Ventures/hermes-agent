"""Contracts for the trusted CI overflow decision and untrusted request boundary."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import scripts.ci_overflow_plan as planner
from scripts.ci_overflow_plan import ARM, X64, Policy, Snapshot, parse_request, plan, sample_pool

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci_overflow"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

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


def test_archive_traversal_and_multi_entry_refused():
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


def test_arm_never_placed_on_core_when_core_goes_to_cloud():
    """C5: ARM is only for NON-core admitted cloud slices, even when core itself is cloud."""
    p = decide(idle=0, arm=3)
    labels = {j.job_id: j.labels for j in p.jobs}
    assert labels["core-smoke"] == X64 and labels["e2e"] == X64
    assert labels["slice-0"] == labels["slice-1"] == ARM


def test_duplicate_json_field_rejected():
    """C26: a repeated key must not silently last-win."""
    with pytest.raises(ValueError, match="duplicate JSON field"):
        parse_request(b'{"slices":[],"slices":[],"e2e":null}')


def test_archive_symlink_entry_refused():
    """C27: a real symlink entry (S_IFLNK mode bits) named request.json is refused."""
    info = zipfile.ZipInfo("request.json")
    info.create_system = 3  # unix, so external_attr carries st_mode
    info.external_attr = (0o120777 << 16)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(info, request())
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        assert (z.infolist()[0].external_attr >> 16) & 0o170000 == 0o120000  # entry really is a symlink
    with pytest.raises(ValueError, match="unsafe request archive"):
        parse_request(buf.getvalue())
    # Control: the same bytes as a regular-file entry are accepted.
    ok = io.BytesIO()
    with zipfile.ZipFile(ok, "w") as z:
        z.writestr("request.json", request())
    assert parse_request(ok.getvalue()).max_reservation == 2 * 35 + 20 + 35


class FixtureAPI:
    """Serves the committed GitHub response-shape fixtures; overrides are per-endpoint."""
    def __init__(self, runners=None, runs=None, jobs=None, tick=None):
        self.runners = runners or fixture("runners.json")
        self.runs = runs or fixture("runs.json")
        self.jobs = jobs or fixture("jobs.json")
        self.tick = tick
    def get(self, path, params=None):
        if self.tick:
            self.tick()
        if path == "actions/runners":
            return self.runners
        if path == "actions/runs":
            return self.runs
        assert path.startswith("actions/runs/") and "/attempts/" in path and path.endswith("/jobs")
        return self.jobs


def test_sampler_reads_fixture_shapes():
    s = sample_pool(FixtureAPI())
    assert (s.status, s.online, s.idle, s.queued_matching_jobs) == ("ok", 1, 1, 1)


def test_sampler_excludes_this_run_attempt():
    """C16: the controller's own attempt must not count as queued pool demand."""
    assert sample_pool(FixtureAPI(), exclude_attempt=(11, 2)).queued_matching_jobs == 1
    assert sample_pool(FixtureAPI(), exclude_attempt=(11, 1)).queued_matching_jobs == 0


def test_sampler_dedupes_jobs_by_id():
    """C18: the same job surfacing under two runs/pages counts once."""
    runs = {"total_count": 2, "workflow_runs": [{"id": 11, "run_attempt": 1, "status": "in_progress"},
                                                  {"id": 12, "run_attempt": 1, "status": "queued"}]}
    assert sample_pool(FixtureAPI(runs=runs)).queued_matching_jobs == 1


def test_sampler_busy_runner_is_online_not_idle():
    """C21: online+busy counts toward online, never toward idle."""
    runners = fixture("runners.json")
    runners["runners"][0]["busy"] = True
    s = sample_pool(FixtureAPI(runners=runners))
    assert (s.status, s.online, s.idle) == ("ok", 1, 0)


def test_sampler_collection_bound_20s_is_unknown(monkeypatch):
    """C19: a collection that takes >20 s of monotonic time is unknown, not a partial ok."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(planner, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    def tick():
        clock["t"] += 7  # three endpoint calls = 21 s
    s = sample_pool(FixtureAPI(tick=tick), now=datetime.now(timezone.utc))
    assert s.status == "unknown"
    clock["t"] = 1000.0
    def slow_but_in_bound():
        clock["t"] += 6  # 18 s total: still inside the bound
    assert sample_pool(FixtureAPI(tick=slow_but_in_bound), now=datetime.now(timezone.utc)).status == "ok"


def test_sampler_snapshot_older_than_60s_is_unknown():
    """C20: a snapshot whose collection started >60 s ago is unknown."""
    stale = datetime.now(timezone.utc) - timedelta(seconds=61)
    assert sample_pool(FixtureAPI(), now=stale).status == "unknown"
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert sample_pool(FixtureAPI(), now=fresh).status == "ok"


# Diagnostic/input contracts pinned from the Argus #931 branch-flip census (t_dc4e55e7):
# each case below kills a mutant that otherwise survived the whole suite.

@pytest.mark.parametrize("name,kwargs,reason", [
    ("unknown-overflow", {"status": "unknown"}, "local-queue"),
    ("unknown-cloud-only", {"status": "unknown", "mode": "cloud-only"}, "local-queue"),
    ("invalid-k-overflow", {"k": None}, "invalid-k"),
    ("self-only", {"mode": "self-only"}, "local-queue"),
    ("self-only-invalid-k", {"mode": "self-only", "k": None}, "invalid-k"),
])
def test_queued_local_reason_names_the_cause(name, kwargs, reason):
    """When cloud is off, every job queues on the pool with the diagnostic reason, never local-idle/budget-queue."""
    p = decide(idle=4, **kwargs)
    assert [(j.reason, j.labels, j.reserved_minutes) for j in p.jobs] == [(reason, POOL, 0)] * 4, name


def test_e2e_leads_when_no_core_slice():
    """Without a core slice the e2e job takes first local priority; alone it is still planned."""
    p = decide(idle=1, slices=SLICES[1:])
    assert [(j.job_id, j.reason) for j in p.jobs] == [
        ("e2e", "local-idle"), ("slice-0", "cloud-overflow"), ("slice-1", "cloud-overflow")]
    assert [(j.job_id, j.reason) for j in decide(idle=1, slices=[]).jobs] == [("e2e", "local-idle")]


@pytest.mark.parametrize("allowance", [-1, 1.5, True, "100", None])
def test_invalid_allowance_rejected(allowance):
    with pytest.raises(ValueError, match="invalid allowance"):
        decide(allowance=allowance)


@pytest.mark.parametrize("field", ["cost_slice", "cost_e2e"])
@pytest.mark.parametrize("cost", [0, -5, 1.5, True])
def test_invalid_reservation_cost_rejected(field, cost):
    with pytest.raises(ValueError, match="invalid reservation cost"):
        plan(SLICES, E2E, Snapshot(NOW, "ok", 2, 0, 0), Policy("overflow", 4, 0, **{field: cost}), 200)


def test_arm_never_relabels_an_unreserved_slice():
    """ARM goes only to slices that hold a cloud reservation, even when a lighter slice was budget-queued."""
    p = decide(allowance=35 + 20 + 35, arm=2)
    assert [(j.job_id, j.labels, j.reason) for j in p.jobs] == [
        ("core-smoke", X64, "cloud-overflow"), ("e2e", X64, "cloud-overflow"),
        ("slice-0", ARM, "cloud-overflow"), ("slice-1", POOL, "budget-queue")]
