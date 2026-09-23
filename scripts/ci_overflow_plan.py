"""Pure CI placement decisions and fail-closed read-only GitHub pool sampling."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import time
from urllib.request import Request as URLRequest, urlopen
import zipfile

POOL = ["self-hosted", "Linux", "X64", "hermes-ci"]
X64 = ["ubuntu-latest"]
ARM = ["ubuntu-24.04-arm"]
APPROVED = {tuple(POOL), tuple(X64), tuple(ARM)}
TERMINAL = {"completed"}


@dataclass(frozen=True)
class Snapshot:
    timestamp: str
    status: str
    online: int
    idle: int
    queued_matching_jobs: int


@dataclass(frozen=True)
class Policy:
    mode: str = "self-only"
    k_cap: object = None
    arm_count: int = 0
    allowed_labels: list = field(default_factory=lambda: [POOL, X64, ARM])
    cost_slice: int = 35
    cost_e2e: int = 20


@dataclass(frozen=True)
class JobPlacement:
    job_id: str
    labels: list[str]
    reason: str
    reserved_minutes: int


@dataclass(frozen=True)
class Plan:
    jobs: list[JobPlacement]
    incidents: list[str]
    summary: dict


@dataclass(frozen=True)
class Request:
    slices: list[dict]
    e2e: dict | None
    max_reservation: int


def _natural(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _labels(labels):
    if tuple(labels) not in APPROVED:
        raise ValueError("unapproved runner labels")
    return list(labels)


def plan(slices_with_weights, e2e, snapshot: Snapshot, policy: Policy, allowance) -> Plan:
    """Make one deterministic, side-effect-free placement decision for an entire attempt."""
    if any(tuple(x) not in APPROVED for x in policy.allowed_labels):
        raise ValueError("unapproved policy label")
    if snapshot.status not in ("ok", "unknown"):
        raise ValueError("invalid snapshot status")
    if type(allowance) is not int or allowance < 0:
        raise ValueError("invalid allowance")
    mode = policy.mode if policy.mode in ("self-only", "overflow", "cloud-only") else "self-only"
    k = _natural(policy.k_cap)
    arm = _natural(policy.arm_count)
    incidents = []
    if mode != policy.mode:
        incidents.append("invalid-mode")
    if k is None:
        incidents.append("invalid-k")
    if arm is None:
        incidents.append("invalid-arm")
        arm = 0
    jobs = sorted(slices_with_weights, key=lambda s: (not s["core"], s.get("index", slices_with_weights.index(s))))
    if e2e is not None:
        jobs.insert(1 if jobs and jobs[0]["core"] else 0, e2e)
    if len({x["job_id"] for x in jobs}) != len(jobs):
        raise ValueError("duplicate job IDs")
    if snapshot.status == "unknown":
        incidents.append("telemetry-unavailable")
    elif snapshot.online == 0:
        incidents.append("pool-offline")
    available = max(0, snapshot.idle - snapshot.queued_matching_jobs)
    local_count = min(len(jobs), available, k) if k is not None and mode == "overflow" and snapshot.status == "ok" and snapshot.online else 0
    cloud_allowed = mode != "self-only" and k is not None and snapshot.status == "ok"
    if mode == "self-only" or not cloud_allowed:
        local_count = len(jobs)  # queue rather than assume an idle slot
    placements = []
    remaining = allowance
    for index, job in enumerate(jobs):
        cost = policy.cost_e2e if job is e2e else policy.cost_slice
        if type(cost) is not int or cost <= 0:
            raise ValueError("invalid reservation cost")
        if index < local_count:
            reason = "local-idle" if mode == "overflow" and cloud_allowed else ("invalid-k" if k is None else "local-queue")
            placements.append(JobPlacement(job["job_id"], _labels(POOL), reason, 0))
        elif cloud_allowed and remaining >= cost:
            remaining -= cost
            placements.append(JobPlacement(job["job_id"], _labels(X64), "cloud-overflow", cost))
        else:
            reason = "budget-overrides-cloud-only" if mode == "cloud-only" else "budget-queue"
            placements.append(JobPlacement(job["job_id"], _labels(POOL), reason, 0))
    candidates = sorted((j for j in slices_with_weights if not j["core"] and any(
        p.job_id == j["job_id"] and p.reserved_minutes for p in placements)),
        key=lambda s: (s["estimated_duration_s"], s.get("index", slices_with_weights.index(s))))
    arm_ids = {x["job_id"] for x in candidates[:arm]}
    placements = [JobPlacement(p.job_id, _labels(ARM) if p.job_id in arm_ids else p.labels,
                               p.reason, p.reserved_minutes) for p in placements]
    return Plan(placements, incidents, {"mode": mode, "available": available, "k_cap": k,
        "reserved_minutes": allowance - remaining, "remaining_allowance": remaining,
        "budget_overrides_cloud_only": any(p.reason == "budget-overrides-cloud-only" for p in placements),
        "snapshot": asdict(snapshot), "exclusions": "Bootstrap, PR CI, OS jobs and storage outside this admission budget"})


def _object_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON field")
        out[key] = value
    return out


def parse_request(raw: bytes) -> Request:
    """Decode the untrusted request artifact (JSON or one-entry ZIP) before admission."""
    if len(raw) > 2 * 1024 * 1024:  # ZIP wire limit, then decompressed limit below
        raise ValueError("request too large")
    if raw.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries = z.infolist()
            if len(entries) != 1 or entries[0].filename != "request.json" or entries[0].is_dir() or (entries[0].external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("unsafe request archive")
            if entries[0].file_size > 1024 * 1024:
                raise ValueError("request too large")
            with z.open(entries[0]) as stream:
                raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("request too large")
    try:
        data = json.loads(raw, object_pairs_hook=_object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite weight")))
        if type(data) is not dict or set(data) != {"slices", "e2e"} or type(data["slices"]) is not list or len(data["slices"]) > 256:
            raise ValueError("invalid request envelope")
        if len(data["slices"]) > 17:
            raise ValueError("controller slice ceiling")
        seen = set()
        for row in data["slices"]:
            if type(row) is not dict or set(row) != {"job_id", "core", "estimated_duration_s"} or type(row["core"]) is not bool:
                raise ValueError("invalid slice")
            _check_row(row, seen)
        e2e = data["e2e"]
        if e2e is not None:
            if type(e2e) is not dict or set(e2e) != {"job_id", "estimated_duration_s"}:
                raise ValueError("invalid e2e")
            _check_row(e2e, seen)
        return Request(data["slices"], e2e, len(data["slices"]) * 35 + (20 if e2e else 0))
    except (UnicodeError, json.JSONDecodeError, TypeError, OverflowError) as exc:
        raise ValueError("invalid request JSON") from exc


def _check_row(row, seen):
    job_id, weight = row["job_id"], row["estimated_duration_s"]
    if type(job_id) is not str or not job_id or len(job_id) > 128 or job_id in seen:
        raise ValueError("invalid or duplicate job ID")
    if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 86400:
        raise ValueError("invalid weight")
    seen.add(job_id)


def _pages(api, path, key, deadline):
    page = 1
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("sampling exceeded 20 seconds")
        data = api.get(path, params={"per_page": 100, "page": page})
        if type(data) is not dict or type(data.get(key)) is not list or type(data.get("total_count")) is not int:
            raise ValueError("invalid API page")
        rows = data[key]
        yield from rows
        if page * 100 >= data["total_count"]:
            if (page - 1) * 100 + len(rows) != data["total_count"]:
                raise ValueError("incomplete final page")
            return
        if len(rows) != 100:
            raise ValueError("incomplete API page")
        page += 1


def sample_pool(api, *, exclude_attempt=None, now=None) -> Snapshot:
    """Enumerate all runners and all nonterminal run attempts; any gap is unknown."""
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    timestamp = now.isoformat()
    try:
        deadline = started + 20
        runners = list(_pages(api, "actions/runners", "runners", deadline))
        matching = [r for r in runners if set(POOL).issubset({x["name"] for x in r["labels"]}) and r["status"] == "online"]
        seen = set()
        queued = 0
        for run in _pages(api, "actions/runs", "workflow_runs", deadline):
            if run["status"] in TERMINAL or (run["id"], run["run_attempt"]) == exclude_attempt:
                continue
            if run["status"] not in {"queued", "in_progress", "waiting", "pending", "requested"}:
                raise ValueError("unsupported run status")
            for job in _pages(api, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs", deadline):
                if job["id"] in seen:
                    continue
                seen.add(job["id"])
                if job["status"] == "queued" and set(job["labels"]).issubset(set(POOL)) and job["labels"]:
                    queued += 1
        if (datetime.now(timezone.utc) - now).total_seconds() > 60 or time.monotonic() > deadline:
            raise TimeoutError("stale snapshot")
        return Snapshot(timestamp, "ok", len(matching), sum(not r["busy"] for r in matching), queued)
    except (Exception):  # API/permissions/schema/rate-limit/partial pagination all fail closed
        return Snapshot(timestamp, "unknown", 0, 0, 0)


class PublicAPI:
    """Read-only unauthenticated API; unavailable private endpoints yield unknown, never offline."""
    def __init__(self, repo):
        self.repo = repo
    def get(self, path, params=None):
        from urllib.parse import urlencode
        url = f"https://api.github.com/repos/{self.repo}/{path}?{urlencode(params or {})}"
        with urlopen(URLRequest(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ci-overflow-dry-run"}), timeout=5) as response:
            return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--slices-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--snapshot-json", type=Path, help="captured sampler snapshot for deterministic replay")
    args = parser.parse_args()
    req = parse_request(args.slices_json.read_bytes())
    snapshot = Snapshot(**json.loads(args.snapshot_json.read_text())) if args.snapshot_json else sample_pool(PublicAPI(args.repo))
    result = plan(req.slices, req.e2e, snapshot, Policy(), 0)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
