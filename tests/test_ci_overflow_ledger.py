"""Ledger CAS, admission, and exact-attempt phantom release contracts."""
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import threading

import pytest

from scripts.ci_overflow_plan import POOL, JobPlacement, Plan
from scripts.ci_overflow_ledger import Ledger, Refusal, ReleaseResult, Reservation

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
    # Argus R3: every persisted field is required; an omitted key is corruption, not a default.
    ("missing-terminal-field", lambda s: s["attempts"]["123:9:1"].pop("terminal_on")),
    ("missing-admitted-field", lambda s: s["attempts"]["123:9:1"].pop("admitted_on")),
    ("missing-released-flag", lambda s: s["attempts"]["123:9:1"]["jobs"][0].pop("released_unemitted")),
    ("missing-plan-incidents", lambda s: s["attempts"]["123:9:1"]["plan"].pop("incidents")),
    ("unknown-row-field", lambda s: s["attempts"]["123:9:1"].update(refund=35)),
    ("unknown-job-field", lambda s: s["attempts"]["123:9:1"]["jobs"][0].update(credit=35)),
    ("unknown-plan-field", lambda s: s["attempts"]["123:9:1"]["plan"].update(extra=[])),
    ("unknown-top-level-field", lambda s: s.update(carry={"2026-09-23": -35})),
    ("boolean-version", lambda s: s.update(version=True)),
    ("float-version", lambda s: s.update(version=1.0)),
])
def test_semantically_corrupt_ledger_refuses_without_put(name, change):
    """Corrupt persisted state => ledger-unavailable on EVERY path: new-key reserve, same-key
    replay, hosted reconcile and absent-job reconcile. Zero cloud, zero PUT, no exception."""
    api = Contents()
    baseline = ledger(api, day="2026-09-22", limit=35).reserve(key(9), proposed("old"))
    assert isinstance(baseline, Reservation)
    api.writes = 0
    change(api.state)
    denied = ledger(api, limit=35).reserve(key(), proposed("a", "b"))
    assert isinstance(denied, Refusal) and denied.incident == "ledger-unavailable", name
    replay = ledger(api, limit=35).reserve(key(9), proposed("old"))
    assert isinstance(replay, Refusal) and replay.incident == "ledger-unavailable", name
    hosted = ledger(api).reconcile(key(9), evidence(run=9, jobs=[{
        "name": "old", "status": "completed", "labels": ["ubuntu-latest"], "runner_name": "GitHub Actions"}]))
    assert hosted == ReleaseResult(0, "ledger-unavailable"), name
    release = ledger(api).reconcile(key(9), evidence(run=9, jobs=[]))
    assert release == ReleaseResult(0, "ledger-unavailable"), name
    assert api.writes == 0, name


class RawContents(Contents):
    """Serves exact raw JSON bytes (wrapped base64) so wire-level corruption reaches the parser."""
    def __init__(self, raw):
        super().__init__()
        self.raw = raw
    def get(self, path, params):
        assert (path, params) == ("state.json", {"ref": "ci-overflow-ledger"})
        return {"sha": self.sha, "encoding": "base64", "content": github_base64(self.raw.encode())}


def test_raw_wire_clean_control_charges_recorded_total():
    api = RawContents('{"version":1,"attempts":{},"daily_totals":{"2026-09-23":35}}')
    result = ledger(api, limit=35).reserve(key(), proposed("a"))
    assert isinstance(result, Reservation) and result.plan.jobs[0].reserved_minutes == 0
    assert api.writes == 1


@pytest.mark.parametrize("name,raw", [
    ("duplicate-daily-totals", '{"version":1,"attempts":{},"daily_totals":{"2026-09-23":35},"daily_totals":{}}'),
    ("duplicate-day-inside-totals", '{"version":1,"attempts":{},"daily_totals":{"2026-09-23":35,"2026-09-23":0}}'),
    ("duplicate-version", '{"version":2,"version":1,"attempts":{},"daily_totals":{}}'),
    ("duplicate-attempts", '{"version":1,"attempts":{"x":1},"attempts":{},"daily_totals":{}}'),
    ("boolean-version", '{"version":true,"attempts":{},"daily_totals":{}}'),
    ("float-version", '{"version":1.0,"attempts":{},"daily_totals":{}}'),
])
def test_raw_wire_corrupt_ledger_refuses_without_put(name, raw):
    api = RawContents(raw)
    denied = ledger(api, limit=35).reserve(key(), proposed("a"))
    assert isinstance(denied, Refusal) and denied.incident == "ledger-unavailable", name
    assert ledger(api).reconcile(key(), evidence(jobs=[])) == ReleaseResult(0, "ledger-unavailable"), name
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
    # Oversize through a schema-valid field (unknown fields are corruption, not padding).
    row = api.state["attempts"]["123:9:1"]
    for placement in (row["jobs"][0], row["plan"]["jobs"][0]):
        placement["reason"] = "z" * 260000
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


def test_reserve_refuses_unapproved_label():
    """C4: reserve() admits only the POOL/X64/ARM label sets; a correctly priced unapproved label is refused unwritten."""
    api = Contents()
    rogue = Plan([JobPlacement("a", ["windows-latest"], "cloud-overflow", 35)], [], {"mode": "overflow"})
    r = ledger(api).reserve(key(), rogue)
    assert isinstance(r, Refusal) and r.incident == "invalid-plan"
    assert api.writes == 0
    assert isinstance(ledger(api).reserve(key(2), proposed("a")), Reservation)


def _terminal_row(admitted, terminal, minutes=35, released=False):
    job = {"job_id": "a", "labels": ["ubuntu-latest"], "reason": "cloud-overflow",
           "reserved_minutes": minutes, "released_unemitted": released}
    if released:
        job["release_receipt_sha256"] = "0" * 64
    planned = {k: job[k] for k in ("job_id", "labels", "reserved_minutes", "reason")}
    return {"admitted_on": admitted, "terminal_on": terminal, "jobs": [job],
            "plan": {"jobs": [planned], "incidents": [], "summary": {"mode": "overflow", "pad": "p" * 6000}}}


def test_compaction_folds_terminal_rows_without_changing_todays_charge():
    """Spec 5.3a amendment (t_e3d085c1): a terminal row folds as soon as folding is charge-exact,
    not after 30 days; today's consumption is identical before and after the fold."""
    today = "2026-09-25"
    attempts = {
        "1:1:1": _terminal_row("2026-09-24", "2026-09-24"),          # stopped carrying: folds
        "1:2:1": _terminal_row("2026-09-23", "2026-09-24"),          # stopped carrying: folds
        "1:3:1": _terminal_row(today, today),                        # charged today only: folds into today
        "1:4:1": _terminal_row(today, today, released=True),         # released: folds at 0
        "1:5:1": _terminal_row("2026-09-24", today),                 # carries through today: KEPT
        "1:6:1": _terminal_row("2026-09-24", None),                  # outstanding: KEPT
        "1:7:1": _terminal_row(today, None),                         # outstanding today: KEPT
    }
    state = {"version": 1, "attempts": json.loads(json.dumps(attempts)), "daily_totals": {}}
    before = {d: Ledger._consumed(state, d) for d in (today, "2026-09-26", "2026-10-30")}
    Ledger._compact(state, today)
    assert sorted(state["attempts"]) == ["1:5:1", "1:6:1", "1:7:1"]
    assert state["daily_totals"] == {"2026-09-24": 35, "2026-09-23": 35, today: 35}
    assert {d: Ledger._consumed(state, d) for d in before} == before


def test_merge_queue_volume_stays_under_hard_limit():
    """The measured failure: ~100 terminal attempts/day at ~6.2 KB each. With same-day folding the
    live state holds only outstanding rows, so admission never hits state-capacity."""
    api = Contents()
    for run in range(1, 301):                       # 3 days of traffic, well past HARD_LIMIT unfolded
        day = f"2026-09-{23 + (run - 1) // 100}"
        result = ledger(api, day=day, limit=0).reserve(key(run), Plan(
            [JobPlacement("a", POOL.copy(), "local-queue", 0)], [], {"mode": "self-only", "pad": "p" * 6000}))
        assert isinstance(result, Reservation), (run, result)
        api.state["attempts"][f"123:{run}:1"]["terminal_on"] = day
    assert len(json.dumps(api.state)) < 400 * 1024


def _hosted(name, minutes, labels=("ubuntu-latest",)):
    """A completed hosted job that ran `minutes` wall-clock minutes (GitHub bills ceil per job)."""
    return {"name": name, "status": "completed", "labels": list(labels), "runner_name": "GitHub Actions 7",
            "started_at": "2026-09-23T10:00:00Z", "completed_at": f"2026-09-23T10:{minutes - 1:02d}:30Z"}


def test_hosted_reconcile_releases_unused_remainder():
    """t_38a419e0 F2: a hosted job that billed 9 min of a 35-min reservation returns 26 min to today."""
    api = Contents()
    assert isinstance(ledger(api, limit=35).reserve(key(), proposed("a")), Reservation)
    assert ledger(api, limit=35).reserve(key(2), proposed("b")).plan.jobs[0].reserved_minutes == 0
    result = ledger(api, limit=35).reconcile(key(), evidence(jobs=[_hosted("a", 9)]))
    assert result == ReleaseResult(26)
    assert api.state["daily_totals"] == {"2026-09-23": 9}   # folded at its actual charge
    assert ledger(api, limit=35).reconcile(key(), evidence(jobs=[_hosted("a", 9)])) == ReleaseResult(0)
    # 35 - 9 = 26 < 35: still no room for a whole slice; a 20-min e2e fits.
    e2e = Plan([JobPlacement("e2e", ["ubuntu-latest"], "cloud-overflow", 20)], [], {"mode": "overflow"})
    got = ledger(api, limit=35).reserve(key(3), e2e)
    assert got.plan.jobs[0].reserved_minutes == 20 and got.plan.summary["remaining_allowance"] == 6


@pytest.mark.parametrize("name,job", [
    ("unmeasurable-no-timestamps", {"name": "a", "status": "completed", "labels": ["ubuntu-latest"],
                                    "runner_name": "GitHub Actions 7"}),
    ("no-runner", dict(_hosted("a", 9), runner_name=None)),
    ("cancelled", dict(_hosted("a", 9), status="cancelled")),
    ("backwards-clock", dict(_hosted("a", 9), completed_at="2026-09-23T09:00:00Z")),
    ("overran-reservation", _hosted("a", 50)),
])
def test_hosted_reconcile_charges_full_reservation_without_exact_actual(name, job):
    api = Contents()
    ledger(api, limit=35).reserve(key(), proposed("a"))
    assert ledger(api, limit=35).reconcile(key(), evidence(jobs=[job])).released_minutes == 0, name
    assert ledger(api, limit=35).reserve(key(2), proposed("b")).plan.jobs[0].reserved_minutes == 0, name


def test_admit_and_reconcile_n_runs_daily_totals_agree_with_remaining_allowance():
    """t_38a419e0 F2: after N admit+reconcile cycles, daily_totals[today] + outstanding rows is the
    exact complement of remaining_allowance, and daily_totals reads the real charge (not 0)."""
    api = Contents()
    limit, day = 6000, "2026-09-23"
    names = [f"slice {i}/8" for i in range(1, 9)] + ["e2e"]
    actual = {n: 20 if n != "e2e" else 7 for n in names}
    plan = Plan([JobPlacement(n, ["ubuntu-latest"], "cloud-overflow", 20 if n == "e2e" else 35) for n in names],
                [], {"mode": "overflow"})
    charged = 0
    for run in range(1, 13):
        got = ledger(api, day=day, limit=limit).reserve(key(run), plan)
        assert isinstance(got, Reservation) and got.plan.summary["reserved_minutes"] == 300
        rr = ledger(api, day=day, limit=limit).reconcile(key(run), evidence(
            run=run, jobs=[_hosted(n, actual[n]) for n in names]))
        assert rr == ReleaseResult(300 - sum(actual.values()))
        charged += sum(actual.values())
        state = api.state
        outstanding = Ledger._consumed({**state, "daily_totals": {}}, day)
        assert state["daily_totals"].get(day, 0) + outstanding == Ledger._consumed(state, day) == charged
    assert api.state["attempts"] == {} and api.state["daily_totals"] == {day: 12 * 167}
    probe = ledger(api, day=day, limit=limit).reserve(key(99), proposed("probe"))
    assert probe.plan.summary["remaining_allowance"] == limit - 12 * 167 - 35
    # The measured failure: flat reservations drained 6000 min in ~20 runs; actual charge leaves 5.3x more.
    assert limit - 12 * 167 > limit - 12 * 300


def test_reserve_folds_closed_rows_below_soft_limit():
    """daily_totals must not read 0 while small terminal rows carry the whole day (09-25 ledger)."""
    api = Contents()
    ledger(api, limit=70).reserve(key(1), proposed("a"))
    api.state["attempts"]["123:1:1"]["terminal_on"] = "2026-09-23"
    ledger(api, limit=70).reserve(key(2), proposed("b"))
    assert "123:1:1" not in api.state["attempts"] and api.state["daily_totals"] == {"2026-09-23": 35}


@pytest.mark.parametrize("name,change", [
    ("hosted-without-receipt", lambda j: j.update(hosted_minutes=9)),
    ("hosted-over-reservation", lambda j: j.update(hosted_minutes=36, release_receipt_sha256="0" * 64)),
    ("hosted-zero", lambda j: j.update(hosted_minutes=0, release_receipt_sha256="0" * 64)),
    ("hosted-bool", lambda j: j.update(hosted_minutes=True, release_receipt_sha256="0" * 64)),
    ("hosted-and-released", lambda j: j.update(hosted_minutes=9, released_unemitted=True,
                                               release_receipt_sha256="0" * 64)),
    ("hosted-on-outstanding-row", "outstanding"),
])
def test_corrupt_hosted_minutes_refused(name, change):
    api = Contents()
    ledger(api, day="2026-09-22").reserve(key(9), proposed("old"))
    row = api.state["attempts"]["123:9:1"]
    if change == "outstanding":
        row["jobs"][0].update(hosted_minutes=9, release_receipt_sha256="0" * 64)
    else:
        row["terminal_on"] = "2026-09-23"
        change(row["jobs"][0])
    api.writes = 0
    r = ledger(api, limit=35).reserve(key(), proposed("a"))
    assert isinstance(r, Refusal) and r.incident == "ledger-unavailable", name
    assert api.writes == 0
