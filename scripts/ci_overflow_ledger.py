"""CAS-backed admission ledger for CI overflow; no credential handling here."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import math
import json
import re
import time

from scripts.ci_overflow_plan import ARM, POOL, X64, JobPlacement, Plan, _object_pairs

BRANCH = "ci-overflow-ledger"
PATH = "state.json"
SOFT_LIMIT = 400 * 1024
HARD_LIMIT = 500 * 1024
# Exact persisted shapes: a missing OR extra key is corruption, never a default.
STATE_FIELDS = {"version", "attempts", "daily_totals"}
ROW_FIELDS = {"admitted_on", "terminal_on", "jobs", "plan"}
PLAN_FIELDS = {"jobs", "incidents", "summary"}
JOB_FIELDS = {"job_id", "labels", "reserved_minutes", "reason", "released_unemitted"}
RECEIPT = "release_receipt_sha256"
# Minutes a completed hosted job actually billed (<= its reservation), recorded at reconcile. The
# unused remainder of the reservation returns to the day's allowance (t_38a419e0: a 580-min plan
# measured ~65 min actual; charging the flat reservation drained the 6000-min day by 20:45Z).
HOSTED = "hosted_minutes"
# D4 amendment 2026-09-26 (t_f459aa52): the timeout ceiling per job kind. Reserving it is the
# legacy conservative mode; in estimate mode it bounds the reservation, the charge and a sample.
CEILING = {"slice": 35, "e2e": 20}
# Rolling billed-minutes samples per job kind, recorded by reconcile; the admission estimate E is
# their p90. Optional top-level key: absent == no samples yet.
SAMPLES = "billed_samples"
SAMPLE_CAP = 200
MIN_SAMPLES = 20


def _kind(job_id):
    return "e2e" if job_id == "e2e" else "slice"


def _estimate(state, kind):
    """p90 (nearest rank) of the recorded billed minutes for this job kind, clamped to
    [1, ceiling]. Fewer than MIN_SAMPLES measured jobs means no estimate: reserve the ceiling."""
    samples = sorted(state.get(SAMPLES, {}).get(kind, []))
    if len(samples) < MIN_SAMPLES:
        return CEILING[kind]
    return max(1, min(CEILING[kind], samples[math.ceil(0.9 * len(samples)) - 1]))


def _runnerless_cancel(job):
    """A hosted job cancelled before any runner picked it up: it never executed (spec 5.3a)."""
    return (job.get("status") == "completed" and job.get("conclusion") == "cancelled"
            and not job.get("runner_name") and job.get("steps") == [])


def _charge(job):
    """What one persisted job consumes: nothing once released, else its measured hosted minutes
    when reconcile recorded them, else the full reservation."""
    if job.get("released_unemitted", False):
        return 0
    return job.get(HOSTED, job["reserved_minutes"])


def _billed_minutes(job):
    """GitHub bills each hosted job rounded UP to the whole minute. None unless the job is a
    completed hosted execution with a parseable, ordered started_at/completed_at pair."""
    if (job.get("status") != "completed" or job.get("labels") not in (X64, ARM)
            or not job.get("runner_name")):
        return None
    try:
        start = datetime.fromisoformat(str(job.get("started_at")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(job.get("completed_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None or end.tzinfo is None or end < start:
        return None
    return max(1, math.ceil((end - start).total_seconds() / 60))


@dataclass(frozen=True)
class Reservation:
    plan: Plan
    existing: bool = False


@dataclass(frozen=True)
class Refusal:
    incident: str


@dataclass(frozen=True)
class ReleaseResult:
    released_minutes: int
    incident: str | None = None


def _encode(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _ident(key):
    if len(key) != 3 or any(type(x) is not int or x <= 0 for x in key):
        raise ValueError("invalid attempt key")
    return ":".join(map(str, key))


def _plan(raw):
    return Plan([JobPlacement(**item) for item in raw["jobs"]], raw["incidents"], raw["summary"])


def _date(value, today):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("corrupt ledger date")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("corrupt ledger date") from exc
    if parsed.isoformat() != value or value > today:
        raise ValueError("corrupt ledger date")


def _validate(state, today):
    samples = state.get(SAMPLES, {})
    if (type(samples) is not dict or not set(samples) <= set(CEILING) or any(
            type(v) is not list or len(v) > SAMPLE_CAP
            or any(type(x) is not int or not 1 <= x <= CEILING[k] for x in v) for k, v in samples.items())):
        raise ValueError("corrupt billed samples")
    for day, total in state["daily_totals"].items():
        _date(day, today)
        if type(total) is not int or total < 0:
            raise ValueError("corrupt ledger total")
    for ident, row in state["attempts"].items():
        if (type(ident) is not str or not re.fullmatch(r"[1-9]\d*:[1-9]\d*:[1-9]\d*", ident)
                or type(row) is not dict):
            raise ValueError("corrupt admission")
        if set(row) != ROW_FIELDS:
            raise ValueError("corrupt admission fields")
        _date(row["admitted_on"], today)
        terminal = row["terminal_on"]
        if terminal is not None:
            _date(terminal, today)
            if terminal < row["admitted_on"]:
                raise ValueError("corrupt admission")
        jobs = row.get("jobs")
        raw = row.get("plan")
        if (type(jobs) is not list or len(jobs) > 18 or type(raw) is not dict or set(raw) != PLAN_FIELDS
                or type(raw.get("jobs")) is not list or type(raw.get("incidents")) is not list
                or type(raw.get("summary")) is not dict or len(raw["jobs"]) != len(jobs)):
            raise ValueError("corrupt admission")
        seen = set()
        for job, planned in zip(jobs, raw["jobs"]):
            if type(job) is not dict or type(planned) is not dict:
                raise ValueError("corrupt job")
            hosted = HOSTED in job
            if set(job) != (JOB_FIELDS | ({RECEIPT} if job.get("released_unemitted") is True or hosted else set())
                            | ({HOSTED} if hosted else set())):
                raise ValueError("corrupt job fields")
            if hosted and (terminal is None or job.get("released_unemitted") is not False
                           or type(job[HOSTED]) is not int or type(job.get("reserved_minutes")) is not int
                           or not 0 < job[HOSTED] <= CEILING[_kind(job.get("job_id"))]
                           or type(job.get(RECEIPT)) is not str or not re.fullmatch(r"[0-9a-f]{64}", job[RECEIPT])):
                raise ValueError("corrupt job")
            name, labels, charge = job.get("job_id"), job.get("labels"), job.get("reserved_minutes")
            if (type(name) is not str or not name or name in seen
                    or set(planned) != {"job_id", "labels", "reserved_minutes", "reason"}
                    or type(labels) is not list or labels not in (POOL, X64, ARM)
                    or type(charge) is not int or charge < 0
                    or (charge != 0 if labels == POOL else not 1 <= charge <= CEILING[_kind(name)])
                    or type(job.get("reason")) is not str
                    or type(job.get("released_unemitted")) is not bool
                    or any(job.get(field) != planned.get(field) for field in
                           ("job_id", "labels", "reserved_minutes", "reason"))
                    or job["released_unemitted"] and
                    (terminal is None or type(job.get("release_receipt_sha256")) is not str
                     or not re.fullmatch(r"[0-9a-f]{64}", job["release_receipt_sha256"]))):
                raise ValueError("corrupt job")
            seen.add(name)
        if (any(type(x) is not str for x in raw["incidents"])
                or type(raw["summary"].get("mode")) is not str):
            raise ValueError("corrupt plan")


class Ledger:
    def __init__(self, api, *, daily_limit=0, clock=None, estimate_headroom=None):
        self.api = api
        self.daily_limit = daily_limit if type(daily_limit) is int and daily_limit >= 0 else 0
        # None: legacy D4 (reserve the timeout ceiling). An int M >= 0: admit each hosted job at its
        # measured p90 estimate E while consumed + E + M <= daily_limit (D4 amendment, t_f459aa52).
        # Invalid values fail closed to the whole limit as headroom (no new cloud), never to 0.
        if estimate_headroom is None or (type(estimate_headroom) is int and estimate_headroom >= 0):
            self.headroom = estimate_headroom
        else:
            self.headroom = self.daily_limit
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _read(self):
        response = self.api.get(PATH, params={"ref": BRANCH})
        if not isinstance(response, dict) or not isinstance(response.get("sha"), str) or response.get("encoding") != "base64":
            raise ValueError("invalid Contents response")
        content = response.get("content")
        if not isinstance(content, str):
            raise ValueError("invalid Contents response")
        # GitHub wraps Contents base64 at 60 columns with "\n"; drop only that whitespace,
        # then decode strictly so any other non-alphabet byte still fails closed.
        # Duplicate keys are refused: last-wins parsing could hide a recorded charge.
        data = json.loads(base64.b64decode("".join(content.split()), validate=True), object_pairs_hook=_object_pairs)
        if (type(data) is not dict or set(data) not in (STATE_FIELDS, STATE_FIELDS | {SAMPLES}) or type(data["version"]) is not int
                or data["version"] != 1 or type(data.get("attempts")) is not dict
                or type(data.get("daily_totals")) is not dict):
            raise ValueError("corrupt ledger")
        _validate(data, self._today())
        return data, response["sha"]

    def _write(self, state, sha):
        return self.api.put(PATH, {"branch": BRANCH, "sha": sha,
            "message": "ci overflow ledger admission/reconciliation", "content": base64.b64encode(_encode(state)).decode()})

    def _today(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("ledger clock must be timezone aware")
        return now.astimezone(timezone.utc).date().isoformat()

    @staticmethod
    def _consumed(state, day):
        consumed = state["daily_totals"].get(day, 0)
        for record in state["attempts"].values():
            admitted = record["admitted_on"]
            if admitted > day:
                continue
            terminal = record["terminal_on"]
            if admitted != day and terminal is not None and terminal < day:
                continue
            consumed += sum(_charge(j) for j in record["jobs"])
        return consumed

    @staticmethod
    def _compact(state, day):
        # Fold a terminal row into daily_totals[admitted_on] as soon as that is charge-exact for every
        # day >= today (_consumed): it stopped carrying (terminal before today), or it only ever
        # charged its admission day (terminal on admitted_on). Retaining 30 days of ~6 KB rows cannot
        # fit HARD_LIMIT at merge-queue volume (spec 5.3a amendment, t_e3d085c1). Branch history keeps
        # the full rows; a terminal attempt is never re-admitted (discovery lists in-progress runs only).
        for key, row in list(state["attempts"].items()):
            terminal = row["terminal_on"]
            if terminal and (terminal < day or terminal == row["admitted_on"]):
                amount = sum(_charge(j) for j in row["jobs"])
                state["daily_totals"][row["admitted_on"]] = state["daily_totals"].get(row["admitted_on"], 0) + amount
                del state["attempts"][key]

    def reserve(self, key, plan: Plan) -> Reservation | Refusal:
        ident = _ident(key)
        if (len({j.job_id for j in plan.jobs}) != len(plan.jobs)
                or len(plan.jobs) > 18
                or sum(j.reserved_minutes for j in plan.jobs) > 615
                or any(j.labels not in (POOL, X64, ARM) or
                       (j.labels == POOL and j.reserved_minutes != 0) or
                       (j.labels != POOL and j.reserved_minutes != (20 if j.job_id == "e2e" else 35))
                       for j in plan.jobs)):
            return Refusal("invalid-plan")
        started = time.monotonic()
        for _ in range(3):
            if time.monotonic() - started >= 10:
                break
            try:
                state, sha = self._read()
            except (Exception):
                return Refusal("ledger-unavailable")
            if ident in state["attempts"]:
                raw = state["attempts"][ident]["plan"]
                return Reservation(_plan(raw), True)
            today = self._today()
            # Fold on EVERY admission, not only past SOFT_LIMIT (t_38a419e0): daily_totals[today] then
            # holds every closed attempt's charge, so it and remaining_allowance describe the same
            # day (limit - remaining == daily_totals[today] + outstanding rows) instead of
            # daily_totals reading 0 while unfolded terminal rows carry the whole day.
            self._compact(state, today)
            if len(_encode(state)) >= HARD_LIMIT:
                return Refusal("state-capacity")
            estimating = self.headroom is not None
            remaining = max(0, self.daily_limit - (self.headroom or 0) - self._consumed(state, today))
            cost = {k: _estimate(state, k) if estimating else CEILING[k] for k in CEILING}
            jobs = []
            for job in plan.jobs:
                price = cost[_kind(job.job_id)]
                if job.reserved_minutes and remaining >= price:
                    remaining -= price
                    jobs.append(JobPlacement(job.job_id, job.labels, job.reason, price))
                elif job.reserved_minutes:
                    reason = "budget-overrides-cloud-only" if plan.summary.get("mode") == "cloud-only" else "budget-queue"
                    jobs.append(JobPlacement(job.job_id, POOL.copy(), reason, 0))
                else:
                    jobs.append(job)
            summary = dict(plan.summary)
            summary.update(remaining_allowance=remaining, reserved_minutes=sum(j.reserved_minutes for j in jobs),
                           budget_overrides_cloud_only=any(j.reason == "budget-overrides-cloud-only" for j in jobs))
            if estimating:
                summary.update(admission_estimate=dict(cost, headroom=self.headroom, samples={
                    k: len(state.get(SAMPLES, {}).get(k, [])) for k in CEILING}))
            decided = Plan(jobs, plan.incidents, summary)
            state["attempts"][ident] = {"admitted_on": today, "terminal_on": None,
                "jobs": [{**asdict(j), "released_unemitted": False} for j in jobs], "plan": asdict(decided)}
            if len(_encode(state)) >= HARD_LIMIT:
                return Refusal("state-capacity")
            try:
                self._write(state, sha)
                return Reservation(decided)
            except Exception as exc:
                # A lost response may be a successful write. Always re-read the key.
                try:
                    current, _ = self._read()
                    if ident in current["attempts"]:
                        return Reservation(_plan(current["attempts"][ident]["plan"]), True)
                except Exception:
                    return Refusal("ledger-unavailable")
                if getattr(exc, "status", None) not in (409, 422):
                    return Refusal("ledger-unavailable")
        return Refusal("ledger-contention")

    def reconcile(self, key, jobs) -> ReleaseResult:
        ident = _ident(key)
        if (not isinstance(jobs, dict) or any(jobs.get(field) != value for field, value in
                zip(("repository_id", "run_id", "run_attempt"), key))):
            return ReleaseResult(0)
        started = time.monotonic()
        for _ in range(3):
            if time.monotonic() - started >= 10:
                break
            try:
                state, sha = self._read()
            except Exception:
                return ReleaseResult(0, "ledger-unavailable")
            row = state["attempts"].get(ident)
            if row is None or jobs.get("status") != "completed" or jobs.get("complete") is not True:
                return ReleaseResult(0)
            observed = jobs.get("jobs")
            if not isinstance(observed, list) or any(not isinstance(j, dict) or not isinstance(j.get("name"), str) for j in observed):
                return ReleaseResult(0)
            names = [j["name"] for j in observed]
            if len(names) != len(set(names)) or any(j.get("status") not in {"queued", "in_progress", "completed", "cancelled"} for j in observed):
                return ReleaseResult(0)
            mapping = {j["name"]: j for j in observed}
            to_release, to_bill, measured = [], [], []
            for entry in row["jobs"]:
                if not entry["reserved_minutes"] or entry.get("released_unemitted"):
                    continue
                job = mapping.get(entry["job_id"])
                if job is None:
                    to_release.append(entry)
                elif job.get("labels") == POOL and job.get("runner_name"):
                    to_release.append(entry)
                elif _runnerless_cancel(job):
                    to_release.append(entry)
                elif HOSTED not in entry:
                    # Charge the billed minutes, capped at the timeout ceiling; an executed job with
                    # no exact measurement (unmeasurable, no runner identity) is charged the ceiling.
                    ceiling = CEILING[_kind(entry["job_id"])]
                    billed = _billed_minutes(job)
                    charge = ceiling if billed is None else min(billed, ceiling)
                    if billed is not None and row["terminal_on"] is None:
                        measured.append((_kind(entry["job_id"]), charge))
                    if charge != entry["reserved_minutes"]:
                        to_bill.append((entry, charge))
            day = self._today()
            changed = bool(to_release) or bool(to_bill) or row["terminal_on"] is None
            if not changed:
                return ReleaseResult(0)
            row["terminal_on"] = day
            receipt = hashlib.sha256(_encode(observed)).hexdigest()
            amount = 0
            for entry in to_release:
                entry["released_unemitted"] = True
                entry["release_receipt_sha256"] = receipt
                amount += entry["reserved_minutes"]
            for entry, billed in to_bill:
                entry[HOSTED] = billed
                entry[RECEIPT] = receipt
                amount += entry["reserved_minutes"] - billed   # negative: billed above the estimate
            if measured:
                samples = state.setdefault(SAMPLES, {})
                for kind, minutes in measured:
                    samples[kind] = (samples.get(kind, []) + [minutes])[-SAMPLE_CAP:]
            self._compact(state, day)
            try:
                self._write(state, sha)
                return ReleaseResult(amount)
            except Exception as exc:
                try:
                    current, _ = self._read()
                    check = current["attempts"].get(ident)
                    if check is None and ident not in state["attempts"]:
                        return ReleaseResult(0)  # folded by our own write: it landed terminal
                    if check and check["terminal_on"] and all(any(
                            j["job_id"] == e["job_id"] and j.get("released_unemitted")
                            for j in check["jobs"]) for e in to_release):
                        return ReleaseResult(0)
                except Exception:
                    return ReleaseResult(0, "ledger-unavailable")
                if getattr(exc, "status", None) not in (409, 422):
                    return ReleaseResult(0, "ledger-unavailable")
        return ReleaseResult(0, "ledger-contention")
