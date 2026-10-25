"""Per-profile SQLite store for the hot RUNTIME fields of cron jobs (t_94e91559).

WHY: ``cron/jobs.json`` holds 600+ job DEFINITIONS (1.4 MB at indent=2). Before this module every
runtime touch (a 60 s ``fire_claim`` heartbeat per running job, every ``mark_job_run``, every
pending-slot stamp) was a whole-file read + deepcopy + json.dump + fsync + rename: 107 rewrites in
30 s on the live gateway, ~45% of the process's GIL time. Runtime fields now live one row per job in
``<cron dir>/runtime_state.db`` (WAL); a runtime touch is one row write and never touches
``jobs.json``. ``cron.jobs.load_jobs`` merges the rows back onto the definitions, so every reader
sees the same dict shape it saw before.

Row shape: ``job_runtime(job_id PK, fields TEXT, fire_claim_owner, run_claim_owner, updated_at)``.
``fields`` is a JSON object holding ONLY the runtime keys the job actually carries (absent stays
absent, ``None`` stays ``None``: several readers distinguish the two, e.g. ``run_claim``). The two
owner columns are derived from ``fields`` on every write and exist so the claim heartbeats are a
single compare-and-refresh ``UPDATE ... WHERE job_id=? AND <claim>_owner=?`` (rowcount 0 = lost).

Kill switch: ``HERMES_CRON_RUNTIME_STATE=json`` restores the pre-split behaviour; the first load in
json mode folds any rows back into ``jobs.json`` and removes the database (see ``cron.jobs``).

Callers hold ``cron.jobs._jobs_lock()`` around every write, exactly as they did for jobs.json, so
the SQLite transaction only has to be atomic, not the serialization point.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

# stdlib-only at import time: out-of-process readers (monitors, dashboards) import this module
# by path to merge runtime rows onto a raw jobs.json read (see ``merged_jobs``).

DB_NAME = "runtime_state.db"
ENV_MODE = "HERMES_CRON_RUNTIME_STATE"

# Every runtime key a job record carries, enumerated from the writers (cron/jobs.py unless noted):
#   heartbeat_fire_claim / claim_job_for_fire / mark_job_run     fire_claim
#   heartbeat_run_claim / clear_run_claim / due scan             run_claim
#   claim_job_for_fire / due scan / _restore_unclaimed_slot      pending_slot, last_dispatch
#   mark_job_run (_record_run_outcome + _advance_after_run)      last_run_at, next_run_at,
#       last_status, last_error, last_delivery_error, failure_streak, error_repeat_streak,
#       manual_run_at, manual_run_prompt, preflight_alerted, last_fire_error,
#       restart_requeue_count, interval_fire_at, repeat.completed
#   cron/unreachable_retry.py plan_retry / clear_state           unreachable_retry
#   cron/quota_hold.py plan_hold / clear_state                   quota_hold_until, quota_hold_cron_expr
#   request_restart_requeue / _apply_restart_requeue             restart_requeue, restart_requeue_count
#   trigger_job                                                  manual_run_at, manual_run_prompt
#   cron/scheduler_delivery.py (update_job per delivery)         last_delivery_unverified,
#                                                                last_delivery_queued
#   cron/monitor.py check_monitor                                monitor_state
#   note_fire_forward_failure                                    last_fire_error
# ``repeat.completed`` is the one nested key: ``repeat.times`` is definition, ``completed`` is
# bumped on every recurring run. ``state``/``enabled``/``paused_*`` stay definitions: they are
# operator-controlled and change only on pause/resume/terminal completion.
RUNTIME_FIELDS = frozenset({
    "fire_claim", "run_claim", "pending_slot", "last_dispatch",
    "last_run_at", "next_run_at", "last_status", "last_error", "last_delivery_error",
    "last_delivery_unverified", "last_delivery_queued", "last_fire_error",
    "failure_streak", "error_repeat_streak", "manual_run_at", "manual_run_prompt",
    "preflight_alerted", "restart_requeue", "restart_requeue_count", "interval_fire_at",
    "unreachable_retry", "quota_hold_until", "quota_hold_cron_expr", "monitor_state",
})
REPEAT_COMPLETED = "repeat.completed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runtime (
    job_id TEXT PRIMARY KEY,
    fields TEXT NOT NULL,
    fire_claim_owner TEXT,
    run_claim_owner TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_runtime (
    gen TEXT NOT NULL,
    job_id TEXT NOT NULL,
    fields TEXT,
    PRIMARY KEY (gen, job_id)
)
"""

# Statement counter for tests/bench (done-when 1): one per row INSERT/UPDATE that changed a row.
UPSERT_COUNT = 0


def mode() -> str:
    """``sqlite`` (default) or ``json`` (the kill switch). Anything else reads as ``sqlite``."""
    from cron.env_settings import cron_env_setting

    return "json" if cron_env_setting(ENV_MODE).strip().lower() == "json" else "sqlite"


def enabled() -> bool:
    return mode() == "sqlite"


def db_path(cron_dir: Path) -> Path:
    return Path(cron_dir) / DB_NAME


def split_job(job: Dict[str, Any]) -> tuple:
    """``(definition, runtime)`` for one job record. Neither input nor output share mutable
    containers with each other except untouched definition values."""
    definition: Dict[str, Any] = {}
    runtime: Dict[str, Any] = {}
    for key, value in job.items():
        if key in RUNTIME_FIELDS:
            runtime[key] = value
        elif key == "repeat" and isinstance(value, dict) and "completed" in value:
            runtime[REPEAT_COMPLETED] = value["completed"]
            definition[key] = {k: v for k, v in value.items() if k != "completed"}
        else:
            definition[key] = value
    return definition, runtime


def strip_runtime(job: Dict[str, Any]) -> Dict[str, Any]:
    return split_job(job)[0]


def has_runtime(job: Dict[str, Any]) -> bool:
    if any(key in RUNTIME_FIELDS for key in job):
        return True
    rep = job.get("repeat")
    return isinstance(rep, dict) and "completed" in rep


def apply_runtime(job: Dict[str, Any], runtime: Dict[str, Any]) -> None:
    """Overlay *runtime* onto *job* in place (the inverse of ``split_job``)."""
    for key, value in runtime.items():
        if key == REPEAT_COMPLETED:
            rep = job.get("repeat")
            if isinstance(rep, dict):
                rep["completed"] = value
            elif rep is None:
                job["repeat"] = {"completed": value}
        else:
            job[key] = value


def _owner(claim: Any) -> Optional[str]:
    if isinstance(claim, dict) and claim.get("by") is not None:
        return str(claim["by"])
    return None


@contextlib.contextmanager
def connect(cron_dir: Path, *, create: bool = True) -> Iterator[Optional[sqlite3.Connection]]:
    """Short-lived connection (open per operation: no per-thread fd caches in a long-lived
    gateway). Yields ``None`` when ``create`` is False and the database does not exist.

    The database file is pre-created 0600 with O_EXCL before SQLite opens it, on EVERY path: SQLite
    would otherwise create it with the process umask (0644 under umask 022), and it holds the same
    private prompt/error text as the 0600 jobs.json. SQLite creates its -wal/-shm sidecars with the
    main file's mode, so they inherit 0600. ``synchronous=FULL``: a committed row is what callers
    treat as persisted (claims, completion counters, a migration that already stripped jobs.json)."""
    path = db_path(cron_dir)
    if not path.exists():
        if not create:
            yield None
            return
        Path(cron_dir).mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            os.close(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(cron_dir: Path) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` .. ``COMMIT`` (``ROLLBACK`` on any exception). A caller that must publish
    a second artifact atomically with the rows (the definitions file) does it INSIDE this block,
    so a failed SQLite write never leaves new definitions behind and a crash leaves at most the
    microseconds between the file rename and the COMMIT."""
    with connect(cron_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _rows_of(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for job_id, fields in conn.execute("SELECT job_id, fields FROM job_runtime").fetchall():
        try:
            value = json.loads(fields)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out[str(job_id)] = value
    return out


def read_row(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT fields FROM job_runtime WHERE job_id=?", (str(job_id),)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_all(cron_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Every row as ``{job_id: runtime_fields}``; creates the database when absent."""
    with connect(cron_dir) as conn:
        return _rows_of(conn)


def _canonical(fields: Dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def upsert_rows(
    conn: sqlite3.Connection,
    rows: Dict[str, Dict[str, Any]],
    *,
    delete_ids: Iterable[str] = (),
    current: Optional[Dict[str, Dict[str, Any]]] = None,
) -> int:
    """Upsert *rows* and delete *delete_ids* on *conn* (caller owns the transaction). Rows equal to
    *current* (when given) are skipped. Returns the number of rows written."""
    global UPSERT_COUNT
    now = time.time()
    written = 0
    for job_id, fields in rows.items():
        if current is not None and job_id in current and \
                _canonical(current[job_id]) == _canonical(fields):
            continue
        conn.execute(
            "INSERT INTO job_runtime(job_id, fields, fire_claim_owner, run_claim_owner,"
            " updated_at) VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET"
            " fields=excluded.fields, fire_claim_owner=excluded.fire_claim_owner,"
            " run_claim_owner=excluded.run_claim_owner, updated_at=excluded.updated_at",
            (str(job_id), _canonical(fields), _owner(fields.get("fire_claim")),
             _owner(fields.get("run_claim")), now))
        written += 1
    for job_id in (str(i) for i in delete_ids if i):
        conn.execute("DELETE FROM job_runtime WHERE job_id=?", (job_id,))
    UPSERT_COUNT += written
    return written


def write_rows(
    cron_dir: Path,
    rows: Dict[str, Dict[str, Any]],
    *,
    delete_ids: Iterable[str] = (),
    current: Optional[Dict[str, Dict[str, Any]]] = None,
) -> int:
    """``upsert_rows`` in its own transaction."""
    with transaction(cron_dir) as conn:
        return upsert_rows(conn, rows, delete_ids=delete_ids, current=current)


def refresh_claim(cron_dir: Path, job_id: str, claim_key: str, expected_owner: str, at: str) -> bool:
    """Compare-and-refresh ``fields[claim_key].at`` for *expected_owner* (``fire_claim`` or
    ``run_claim``): one UPDATE guarded by the owner column; rowcount 0 means the claim is gone or
    held by someone else."""
    global UPSERT_COUNT
    column = {"fire_claim": "fire_claim_owner", "run_claim": "run_claim_owner"}[claim_key]
    with transaction(cron_dir) as conn:
        row = conn.execute(
            f"SELECT fields FROM job_runtime WHERE job_id=? AND {column}=?",
            (str(job_id), str(expected_owner))).fetchone()
        if row is None:
            return False
        fields = json.loads(row[0])
        claim = fields.get(claim_key)
        if not isinstance(claim, dict) or claim.get("by") != expected_owner:
            return False
        claim["at"] = at
        cur = conn.execute(
            f"UPDATE job_runtime SET fields=?, updated_at=? WHERE job_id=? AND {column}=?",
            (_canonical(fields), time.time(), str(job_id), str(expected_owner)))
    if cur.rowcount:
        UPSERT_COUNT += 1
    return bool(cur.rowcount)


# --- two-phase commit for saves that change BOTH halves ---------------------------------------
# 1. stage_pending: the new rows go into pending_runtime under a fresh generation id; COMMIT.
# 2. jobs.json is renamed into place carrying the same id as top-level "runtime_gen".
# 3. apply_pending: pending rows move into job_runtime; COMMIT.
# recover_pending (every load): pending rows whose gen matches the file's "runtime_gen" are rolled
# forward (crash after 2); any other gen is discarded (crash before 2: the file never changed).

def stage_pending(conn: sqlite3.Connection, gen: str, rows: Dict[str, Dict[str, Any]],
                  delete_ids: Iterable[str] = ()) -> None:
    conn.execute("DELETE FROM pending_runtime")
    for job_id, fields in rows.items():
        conn.execute("INSERT INTO pending_runtime(gen, job_id, fields) VALUES (?,?,?)",
                     (gen, str(job_id), _canonical(fields)))
    for job_id in delete_ids:
        if job_id:
            conn.execute("INSERT OR REPLACE INTO pending_runtime(gen, job_id, fields)"
                         " VALUES (?,?,NULL)", (gen, str(job_id)))


def pending_gens(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT gen FROM pending_runtime").fetchall()]


def apply_pending(conn: sqlite3.Connection, gen: str) -> int:
    """Move generation *gen* into job_runtime (NULL fields = delete) and clear the journal."""
    rows: Dict[str, Dict[str, Any]] = {}
    deletes: List[str] = []
    for job_id, fields in conn.execute(
            "SELECT job_id, fields FROM pending_runtime WHERE gen=?", (gen,)).fetchall():
        if fields is None:
            deletes.append(job_id)
        else:
            rows[job_id] = json.loads(fields)
    written = upsert_rows(conn, rows, delete_ids=deletes)
    conn.execute("DELETE FROM pending_runtime")
    return written


def merged_jobs(jobs_file: Path) -> List[Dict[str, Any]]:
    """READ-ONLY merged view for out-of-process readers (cron-health, dashboards, watchers that
    read jobs.json directly): the raw jobs list with each job's runtime row overlaid, i.e. the
    shape jobs.json had before the split. Never creates the database, never migrates, never
    writes; a row wins over a runtime field the file still carries."""
    jobs_file = Path(jobs_file)
    with open(jobs_file, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    path = db_path(jobs_file.parent)
    if not path.exists():
        return jobs
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = dict(conn.execute("SELECT job_id, fields FROM job_runtime").fetchall())
    except sqlite3.Error:
        return jobs
    finally:
        conn.close()
    out = []
    for job in jobs:
        if isinstance(job, dict) and str(job.get("id")) in rows:
            try:
                row = json.loads(rows[str(job["id"])])
            except (TypeError, ValueError):
                row = None
            if isinstance(row, dict):
                job = overlay_row(job, row)
        out.append(job)
    return out


def overlay_row(job: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """The authoritative merged record for a job that HAS a row: the file's runtime keys are
    dropped first (a key absent from the row was cleared; keeping the file's copy would resurrect
    it), then the row is applied. An empty row still strips."""
    merged = strip_runtime(job)
    apply_runtime(merged, row)
    return merged


def fold_staged_copy(cron_dir: Path) -> bool:
    """For a STAGED copy of a cron dir (profile export): rewrite its jobs.json as the merged view
    and delete the database + sidecars, so the archive carries runtime text only inside jobs.json,
    where the export's secret scrubber reads it. Never call on a live store (no lock taken)."""
    cron_dir = Path(cron_dir)
    jobs_file = cron_dir / "jobs.json"
    if not db_path(cron_dir).exists():
        return False
    if jobs_file.exists():
        with open(jobs_file, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        merged = merged_jobs(jobs_file)
        payload = dict(data) if isinstance(data, dict) else {}
        payload["jobs"] = merged
        jobs_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    drop_database(cron_dir)
    return True


def drop_database(cron_dir: Path) -> None:
    """Remove the database and its WAL/SHM side files (after a fold back into jobs.json)."""
    base = db_path(cron_dir)
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            Path(str(base) + suffix).unlink()
