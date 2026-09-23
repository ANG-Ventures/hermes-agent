"""Ledger CAS, admission, and exact-attempt phantom release contracts."""
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

import pytest

from scripts.ci_overflow_plan import POOL, JobPlacement, Plan
from scripts.ci_overflow_ledger import Ledger, Refusal, Reservation

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ci_overflow"
CANONICAL = {"version": 1, "attempts": {}, "daily_totals": {}}


def github_base64(raw: bytes) -> str:
    """GitHub Contents API wire shape: base64 wrapped at 60 columns, newline-terminated."""
    flat = base64.b64encode(raw).decode()
    return "".join(flat[i:i + 60] + "\n" for i in range(0, len(flat), 60))


def live_response():
    """The captured live `GET contents/state.json?ref=ci-overflow-ledger` response."""
    return json.loads((FIXTURES / "contents.json").read_text(encoding="utf-8"))


def proposed(*names):
    return Plan([JobPlacement(name, ["ubuntu-latest"], "cloud-overflow", 35) for name in names], [], {"mode": "overflow"})


class Contents:
    """Same Contents API wire shape, SHA checked under the storage lock."""
    def __init__(self, state=None):
        self.lock = threading.Lock()
        self.state = state if state is not None else {"version": 1, "attempts": {}, "daily_totals": {}}
        self.sha = "s0"
        self.writes = 0
        self.lose = False
        self.conflicts = 0
    def get(self, path, params):
        assert (path, params) == ("state.json", {"ref": "ci-overflow-ledger"})
        with self.lock:
            return {"sha": self.sha, "encoding": "base64", "content": github_base64(json.dumps(self.state).encode())}
    def put(self, path, payload):
        assert path == "state.json" and payload["branch"] == "ci-overflow-ledger"
        with self.lock:
            if self.conflicts:
                self.conflicts -= 1
                raise HTTPError(409)
            if payload.get("sha") != self.sha:
                raise HTTPError(409)
            self.state = json.loads(base64.b64decode(payload["content"]))
            self.writes += 1
            self.sha = f"s{self.writes}"
            if self.lose:
                self.lose = False
                raise ConnectionError("response lost after commit")
            return {"content": {"sha": self.sha}}


class HTTPError(Exception):
    def __init__(self, status):
        self.status = status


def ledger(api=None, *, day="2026-09-23", limit=70):
    return Ledger(api or Contents(), daily_limit=limit,
                  clock=lambda: datetime.fromisoformat(day + "T12:00:00+00:00"))


def key(run=1, attempt=1):
    return (123, run, attempt)


def evidence(run=1, attempt=1, status="completed", jobs=(), complete=True):
    return {"repository_id": 123, "run_id": run, "run_attempt": attempt,
            "status": status, "complete": complete, "jobs": list(jobs)}


def test_two_contending_writers_never_overdraw():
    class SimultaneousReads(Contents):
        def __init__(self):
            super().__init__()
            self.first_reads = threading.Barrier(2)
            self.read_count = 0
        def get(self, path, params):
            result = super().get(path, params)
            with self.lock:
                self.read_count += 1
                first = self.read_count <= 2
            if first:
                self.first_reads.wait(timeout=5)
            return result

    api = SimultaneousReads()
    barrier = threading.Barrier(2)
    results = []
    def admit(i):
        barrier.wait()
        results.append(ledger(api, limit=35).reserve(key(i), proposed("slice")))
    workers = [threading.Thread(target=admit, args=(i,)) for i in (1, 2)]
    for w in workers: w.start()
    for w in workers: w.join()
    assert sum(j.reserved_minutes for r in results if isinstance(r, Reservation) for j in r.plan.jobs) == 35
    assert api.writes == 2


def test_response_loss_reads_key_not_double_reserves():
    api = Contents()
    api.lose = True
    first = ledger(api).reserve(key(), proposed("a"))
    second = ledger(api).reserve(key(), proposed("different"))
    assert isinstance(first, Reservation) and second.plan == first.plan
    assert api.writes == 1


@pytest.mark.parametrize("name,state,incident", [
    ("missing", None, "ledger-unavailable"),
    ("corrupt", {"bad": 1}, "ledger-unavailable"),
])
def test_missing_or_corrupt_refuses_cloud(name, state, incident):
    api = Contents()
    if name == "missing":
        api.get = lambda *a, **k: (_ for _ in ()).throw(HTTPError(404))
    else:
        api.state = state
    r = ledger(api).reserve(key(), proposed("a"))
    assert isinstance(r, Refusal) and r.incident == incident
    assert api.writes == 0


def test_canonical_ledger_control_caps_admission():
    api = Contents()
    result = ledger(api, limit=35).reserve(key(), proposed("a", "b"))
    assert isinstance(result, Reservation)
    assert [j.reserved_minutes for j in result.plan.jobs] == [35, 0]
    assert api.writes == 1


@pytest.mark.parametrize("name,change", [
    ("negative-daily-total", lambda s: s["daily_totals"].update({"2026-09-23": -35})),
    ("fractional-daily-total", lambda s: s["daily_totals"].update({"2026-09-23": 0.5})),
    ("future-daily-total", lambda s: s["daily_totals"].update({"2026-09-24": 35})),
    ("negative-old-reservation", lambda s: s["attempts"]["123:9:1"]["jobs"][0].update(reserved_minutes=-35)),
    ("missing-reservation-field", lambda s: s["attempts"]["123:9:1"]["jobs"][0].pop("reserved_minutes")),
    ("future-dated-pending", lambda s: s["attempts"]["123:9:1"].update(admitted_on="2026-09-24")),
    ("bad-terminal-date", lambda s: s["attempts"]["123:9:1"].update(terminal_on="nonsense")),
    ("terminal-before-admission", lambda s: s["attempts"]["123:9:1"].update(terminal_on="2026-09-21")),
    ("missing-existing-plan", lambda s: s["attempts"]["123:9:1"].pop("plan")),
    ("malformed-existing-plan", lambda s: s["attempts"]["123:9:1"]["plan"].update(jobs="broken")),
    ("plan-job-mismatch", lambda s: s["attempts"]["123:9:1"]["plan"]["jobs"][0].update(reserved_minutes=0)),
    ("wrong-attempt-key", lambda s: s["attempts"].update({"bad-key": s["attempts"].pop("123:9:1")})),
    ("released-without-receipt", lambda s: s["attempts"]["123:9:1"]["jobs"][0].update(released_unemitted=True)),
])
def test_semantically_corrupt_ledger_refuses_without_put(name, change):
    api = Contents()
    baseline = ledger(api, day="2026-09-22", limit=35).reserve(key(9), proposed("old"))
    assert isinstance(baseline, Reservation)
    api.writes = 0
    change(api.state)
    denied = ledger(api, limit=35).reserve(key(), proposed("a", "b"))
    assert isinstance(denied, Refusal) and denied.incident == "ledger-unavailable", name
    release = ledger(api).reconcile(key(9), evidence(run=9, jobs=[]))
    assert release.incident == "ledger-unavailable", name
    assert api.writes == 0, name


def test_corrupt_existing_attempt_never_returns_stored_plan():
    api = Contents()
    assert isinstance(ledger(api).reserve(key(), proposed("a")), Reservation)
    api.state["attempts"]["123:1:1"].pop("plan")
    api.writes = 0
    result = ledger(api).reserve(key(), proposed("a"))
    assert isinstance(result, Refusal) and result.incident == "ledger-unavailable"
    assert api.writes == 0


def test_conflicts_recompute_allowance_and_retry_bounded():
    api = Contents()
    api.conflicts = 2
    assert isinstance(ledger(api).reserve(key(), proposed("a")), Reservation)
    assert api.writes == 1
    api.conflicts = 3
    assert isinstance(ledger(api).reserve(key(2), proposed("b")), Refusal)


@pytest.mark.parametrize("name,prior,terminal,expected", [
    ("pending-carries", True, False, 0),
    ("terminal-prior-day-stops-carry", True, True, 35),
    ("current-terminal-charged", False, True, 0),
])
def test_day_boundary(name, prior, terminal, expected):
    api = Contents()
    d = "2026-09-22" if prior else "2026-09-23"
    assert isinstance(ledger(api, day=d, limit=35).reserve(key(), proposed("a")), Reservation)
    if terminal:
        # An executed hosted job remains charged on its terminal day.
        day = "2026-09-22" if prior else "2026-09-23"
        ledger(api, day=day, limit=35).reconcile(key(), evidence(jobs=[{
            "name": "a", "status": "completed", "labels": ["ubuntu-latest"], "runner_name": "GitHub Actions"}]))
    other = ledger(api, day="2026-09-23", limit=35).reserve(key(2), proposed("b"))
    assert sum(j.reserved_minutes for j in other.plan.jobs) == expected, name


def test_timeout_terminal_absent_job_restores_allowance_once():
    api = Contents()
    assert isinstance(ledger(api, limit=35).reserve(key(), proposed("a")), Reservation)
    assert ledger(api, limit=35).reserve(key(2), proposed("b")).plan.jobs[0].reserved_minutes == 0
    released = ledger(api, limit=35).reconcile(key(), evidence(jobs=[]))
    assert released.released_minutes == 35
    assert ledger(api, limit=35).reconcile(key(), evidence(jobs=[])).released_minutes == 0
    assert ledger(api, limit=35).reserve(key(3), proposed("c")).plan.jobs[0].reserved_minutes == 35


@pytest.mark.parametrize("name,report", [
    ("delayed-hosted-start", evidence(status="in_progress", jobs=[])),
    ("incomplete-pages", evidence(complete=False)),
    ("wrong-attempt", evidence(attempt=2)),
    ("canceled-hosted-job", evidence(status="completed", jobs=[{
        "name": "a", "status": "cancelled", "labels": ["ubuntu-latest"], "runner_name": None}])),
    ("ambiguous-runner", evidence(jobs=[{"name": "a", "status": "completed", "labels": [], "runner_name": None}])),
])
def test_no_phantom_release_without_exact_evidence(name, report):
    api = Contents()
    ledger(api, limit=35).reserve(key(), proposed("a"))
    assert ledger(api, limit=35).reconcile(key(), report).released_minutes == 0, name
    assert ledger(api, limit=35).reserve(key(3), proposed("b")).plan.jobs[0].reserved_minutes == 0


def test_self_hosted_only_job_releases_when_terminal():
    api = Contents()
    ledger(api, limit=35).reserve(key(), proposed("a"))
    result = ledger(api, limit=35).reconcile(key(), evidence(jobs=[{
        "name": "a", "status": "completed", "labels": ["self-hosted", "Linux", "X64", "hermes-ci"],
        "runner_name": "linux-runner"}]))
    assert result.released_minutes == 35


def test_rerun_new_attempt_consumes_new_admission():
    api = Contents()
    ledger(api, limit=35).reserve(key(), proposed("a"))
    assert ledger(api, limit=35).reserve(key(attempt=2), proposed("a")).plan.jobs[0].reserved_minutes == 0


def test_unreserved_hosted_placement_cannot_be_committed():
    api = Contents()
    unsafe = Plan([JobPlacement("a", ["ubuntu-latest"], "forged", 0)], [], {"mode": "overflow"})
    assert ledger(api).reserve(key(), unsafe).incident == "invalid-plan"
    assert api.writes == 0


def test_underpriced_hosted_placement_cannot_be_committed():
    api = Contents()
    unsafe = Plan([JobPlacement("a", ["ubuntu-latest"], "forged", 1)], [], {"mode": "overflow"})
    assert ledger(api).reserve(key(), unsafe).incident == "invalid-plan"
    assert api.writes == 0


def test_oversized_essential_state_refuses_new_cloud():
    api = Contents()
    ledger(api, day="2026-09-22").reserve(key(9), proposed("old"))
    api.state["attempts"]["123:9:1"]["pad"] = "z" * 520000
    assert ledger(api).reserve(key(), proposed("a")).incident == "state-capacity"


class Static:
    def __init__(self, response):
        self.response, self.writes = response, 0
    def get(self, path, params):
        assert (path, params) == ("state.json", {"ref": "ci-overflow-ledger"})
        return self.response
    def put(self, path, payload):
        self.writes += 1
        raise AssertionError("must not write")


def test_captured_live_contents_response_decodes():
    """F1: GitHub's newline-wrapped base64 decodes; the live (pre-canonical) schema is then refused."""
    response = live_response()
    assert "\n" in response["content"]  # the real wire shape is wrapped
    api = Static(response)
    with pytest.raises(ValueError, match="corrupt ledger"):  # decoded fine, rejected on schema
        Ledger(api)._read()
    r = ledger(api).reserve(key(), proposed("a"))
    assert isinstance(r, Refusal) and r.incident == "ledger-unavailable" and api.writes == 0


def test_live_envelope_with_canonical_state_admits():
    response = dict(live_response(), content=github_base64(json.dumps(CANONICAL, indent=1).encode()))
    assert response["content"].count("\n") >= 1
    state, sha = Ledger(Static(response))._read()
    assert state == CANONICAL and sha == response["sha"]


def test_wrapped_base64_admission_round_trip():
    api = Contents()
    local = Plan([JobPlacement(f"pad-{i}", POOL, "local-idle", 0) for i in range(5)],
                 [], {"mode": "self-only"})
    ledger(api).reserve(key(9), local)
    assert api.get("state.json", {"ref": "ci-overflow-ledger"})["content"].count("\n") > 3
    assert ledger(api, limit=35).reserve(key(), proposed("a")).plan.jobs[0].reserved_minutes == 35


class CreatesOnMissing:
    """Real GitHub: GET on a missing path is 404, and PUT without `sha` CREATES the file."""
    def __init__(self):
        self.state = None
        self.writes = 0
    def get(self, path, params):
        if self.state is None:
            raise HTTPError(404)
        return {"sha": "s1", "encoding": "base64", "content": github_base64(json.dumps(self.state).encode())}
    def put(self, path, payload):
        if self.state is None and payload.get("sha") is None:
            self.state = json.loads(base64.b64decode(payload["content"]))
            self.writes += 1
            return {"content": {"sha": "s1"}}
        raise HTTPError(409 if self.state is not None else 422)


def test_missing_ledger_never_created():
    """C13: a missing ledger refuses cloud and is never auto-created, on reserve or reconcile."""
    api = CreatesOnMissing()
    r = ledger(api).reserve(key(), proposed("a"))
    assert isinstance(r, Refusal) and r.incident == "ledger-unavailable"
    assert ledger(api).reconcile(key(), evidence(jobs=[])).incident == "ledger-unavailable"
    assert api.writes == 0 and api.state is None


def test_reserve_refuses_more_than_18_jobs():
    """C29: controller ceiling is 17 slices + 1 e2e; a 19-job plan is refused even at 0 minutes."""
    api = Contents()
    at_limit = Plan([JobPlacement(f"j{i}", POOL, "local-queue", 0) for i in range(18)], [], {"mode": "overflow"})
    assert isinstance(ledger(api).reserve(key(), at_limit), Reservation)
    over = Plan([JobPlacement(f"j{i}", POOL, "local-queue", 0) for i in range(19)], [], {"mode": "overflow"})
    r = ledger(api).reserve(key(2), over)
    assert isinstance(r, Refusal) and r.incident == "invalid-plan"
    assert api.writes == 1
