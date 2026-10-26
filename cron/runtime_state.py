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

``jobs.json`` stays a COALESCED, READ-ONLY MIRROR of the merged view (definitions + the current
row of every job). The database is the source of truth: no reader inside hermes-agent takes a
runtime value from the file once the job has a row, and the claim CAS reads only the row. Every row
write sets ``mirror_state.dirty_since`` (same transaction); ``cron.jobs.maybe_flush_runtime_mirror``
rewrites the file at most once per ``HERMES_CRON_MIRROR_INTERVAL_S`` (default 30 s) when it is set,
from the scheduler tick and after a write section; ``0`` = flush on every touch (the pre-split I/O
shape, for debugging). Definition saves write the merged view at once and clear the flag.

DO NOT REMOVE THE MIRROR as an optimisation. These consumers read runtime fields (last_run_at,
next_run_at, last_status, last_error, ...) straight out of jobs.json with json.load, and would see
every job as never-run without it (hermes-home, census 2026-10-24, PR #2287 review):
  scripts/cron-health.py, scripts/cron-status-sweep.py, scripts/live_evidence_recheck.py,
  scripts/cron-registry-deadman.py, scripts/cron-config-lint.py, scripts/cachehop-deadman.py,
  scripts/cron-dashboard-refresh.py,
  skills-shared/devops/handle-alerts-and-logs/scripts/crons-down-detail.py,
  scripts/home-reconcile-origin.py, scripts/cron-jobs-runtime-committer.py.

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
# The database file and every side file SQLite may leave next to it (WAL mode: -wal/-shm; a
# configured ``journal_mode: delete``: -journal).
SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
ENV_MODE = "HERMES_CRON_RUNTIME_STATE"
ENV_MIRROR_INTERVAL = "HERMES_CRON_MIRROR_INTERVAL_S"
DEFAULT_MIRROR_INTERVAL_S = 30.0

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
    staged_at REAL,
    base TEXT,
    published_at REAL,
    PRIMARY KEY (gen, job_id)
);
CREATE TABLE IF NOT EXISTS mirror_state (
    k TEXT PRIMARY KEY,
    v REAL NOT NULL
)
"""

# Top-level jobs.json keys a mirror write embeds (first, so a 256-byte peek reads them): the seq
# of the row snapshot it reflects, and the database epoch that seq belongs to.
MIRROR_EPOCH_KEY = "_mirror_epoch"
MIRROR_SEQ_KEY = "_mirror_seq"
# Fixed temp name ``fold_staged_copy`` writes next to a staged jobs.json (profile export refuses a
# symlink at it).
FOLD_TMP_NAME = "jobs.json.fold-tmp"

# Statement counter for tests/bench (done-when 1): one per row INSERT/UPDATE that changed a row.
UPSERT_COUNT = 0
# jobs.json mirror rewrites performed by this process (tests/bench).
MIRROR_WRITES = 0


def mode() -> str:
    """``sqlite`` (default) or ``json`` (the kill switch). Anything else reads as ``sqlite``."""
    from cron.env_settings import cron_env_setting

    return "json" if cron_env_setting(ENV_MODE).strip().lower() == "json" else "sqlite"


def enabled() -> bool:
    return mode() == "sqlite"


def mirror_interval() -> float:
    """Seconds between jobs.json mirror rewrites (``0`` = every touch). Unset, unparsable or
    negative reads as the default."""
    from cron.env_settings import cron_env_setting

    raw = cron_env_setting(ENV_MIRROR_INTERVAL).strip()
    try:
        value = float(raw) if raw else DEFAULT_MIRROR_INTERVAL_S
    except ValueError:
        return DEFAULT_MIRROR_INTERVAL_S
    return value if value >= 0 else DEFAULT_MIRROR_INTERVAL_S


def _now() -> float:
    """Wall clock for the mirror window (indirection so tests can move it)."""
    return time.time()


# mirror_state keys: ``seq`` counts row writes, ``flushed`` is the seq the last jobs.json mirror
# write reflected, ``dirty_since`` is when seq first got ahead of ``flushed`` (the window start:
# touches inside the window coalesce into one rewrite).

def _state(conn: sqlite3.Connection, key: str) -> Optional[float]:
    row = conn.execute("SELECT v FROM mirror_state WHERE k=?", (key,)).fetchone()
    return float(row[0]) if row is not None else None


def _ensure_epoch(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO mirror_state(k, v) VALUES ('epoch', ?)",
                 (float(int.from_bytes(os.urandom(6), "big")),))


def mirror_epoch(conn: sqlite3.Connection) -> Optional[float]:
    """Random id of THIS database's seq counter (a recreated db restarts seq at 0): a ``seq``
    embedded in jobs.json is only comparable when its epoch matches."""
    return _state(conn, "epoch")


def mark_dirty(conn: sqlite3.Connection) -> None:
    """The jobs.json mirror lags the rows from now on (caller owns the transaction)."""
    _ensure_epoch(conn)
    conn.execute("INSERT INTO mirror_state(k, v) VALUES ('seq', 1) "
                 "ON CONFLICT(k) DO UPDATE SET v = v + 1")
    conn.execute("INSERT OR IGNORE INTO mirror_state(k, v) VALUES ('dirty_since', ?)", (_now(),))


def mirror_seq(conn: sqlite3.Connection) -> float:
    return _state(conn, "seq") or 0.0


def dirty_since(conn: sqlite3.Connection) -> Optional[float]:
    """Window start, or ``None`` when the mirror reflects every row write."""
    if mirror_seq(conn) <= (_state(conn, "flushed") or 0.0):
        return None
    return _state(conn, "dirty_since") or 0.0


def mark_flushed(conn: sqlite3.Connection, seq: float) -> None:
    """A jobs.json write reflected every row write up to *seq* (read in the same snapshot as the
    rows it wrote). A row write that landed after that snapshot keeps the mirror dirty."""
    _ensure_epoch(conn)
    conn.execute("INSERT INTO mirror_state(k, v) VALUES ('flushed', ?) "
                 "ON CONFLICT(k) DO UPDATE SET v = max(v, excluded.v)", (seq,))
    if mirror_seq(conn) <= seq:
        conn.execute("DELETE FROM mirror_state WHERE k='dirty_since'")


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
    treat as persisted (claims, completion counters, a migration that already stripped jobs.json).

    Opened through ``hermes_cli.sqlite_util.open_db`` (Prism 173ea387a852), the policy every other
    Hermes database takes: the configured ``database.journal_mode`` (``delete`` is honoured on a
    new database), the WAL-reset-bug gate, the cross-VM filesystem guard, never a live downgrade.
    Late import: this module stays stdlib-only at import time for out-of-process readers."""
    from hermes_cli.sqlite_util import open_db

    path = db_path(cron_dir)
    if not path.exists():
        if not create:
            yield None
            return
        Path(cron_dir).mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            os.close(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    conn = open_db(path, db_label=f"cron/{DB_NAME}", busy_timeout_ms=30000,
                   synchronous_full=True, row_factory=None)
    try:
        conn.isolation_level = None  # explicit BEGIN/COMMIT only (see ``transaction``)
        conn.executescript(_SCHEMA)
        # A database created before pending_runtime.staged_at/base/published_at existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_runtime)")}
        for col, decl in (("staged_at", "REAL"), ("base", "TEXT"), ("published_at", "REAL")):
            if col not in cols:
                with contextlib.suppress(sqlite3.OperationalError):  # a sibling added it first
                    conn.execute(f"ALTER TABLE pending_runtime ADD COLUMN {col} {decl}")
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
    return _rows_and_unreadable(conn)[0]


def _rows_and_unreadable(conn: sqlite3.Connection) -> tuple:
    """``(rows, unreadable_ids)``: every row that parses to a JSON object, and the ids of the rows
    that do not (counted, never silently dropped by a caller that deletes afterwards)."""
    out: Dict[str, Dict[str, Any]] = {}
    bad: List[str] = []
    for job_id, fields in conn.execute("SELECT job_id, fields FROM job_runtime").fetchall():
        try:
            value = json.loads(fields)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            out[str(job_id)] = value
        else:
            bad.append(str(job_id))
    return out, sorted(bad)


def row_texts(conn: sqlite3.Connection) -> Dict[str, str]:
    """``{job_id: stored fields text}``: the compare token for ``delete_rows_if_unchanged``."""
    return {str(jid): text for jid, text in
            conn.execute("SELECT job_id, fields FROM job_runtime").fetchall()}


def delete_rows_if_unchanged(conn: sqlite3.Connection, expected: Dict[str, str]) -> List[str]:
    """Compare-and-delete (caller owns the transaction, Prism c10b8ca4d934/fb81a100b27c): delete
    each row only while its stored text still equals *expected[job_id]* (the text an export folded
    into jobs.json). Returns the ids that were NOT deleted because a writer changed (or removed)
    the row since: their newer value is not in the file, so the caller must keep the database."""
    changed: List[str] = []
    for job_id, text in expected.items():
        if not conn.execute("DELETE FROM job_runtime WHERE job_id=? AND fields=?",
                            (str(job_id), text)).rowcount:
            changed.append(str(job_id))
    return sorted(changed)


def published_token(conn: sqlite3.Connection) -> Optional[int]:
    """``max(rowid)`` of the published pending generations (None = none): one indexed read the
    single-job definition cache keys on, so a generation published without the rename that
    names it (crash between mark and rename, ruling R2) invalidates the cache (Prism
    f92e5396970e). Tolerates a database without the column."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_runtime)")}
    if "published_at" not in cols:
        return None
    row = conn.execute(
        "SELECT max(rowid) FROM pending_runtime WHERE published_at IS NOT NULL").fetchone()
    return row[0] if row else None


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
    dirty: bool = True,
) -> int:
    """Upsert *rows* and delete *delete_ids* on *conn* (caller owns the transaction). Rows equal to
    *current* (when given) are skipped. Returns the number of rows written. Any write marks the
    jobs.json mirror dirty unless *dirty* is False (rows adopted FROM the file)."""
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
    deleted = 0
    for job_id in (str(i) for i in delete_ids if i):
        deleted += conn.execute("DELETE FROM job_runtime WHERE job_id=?", (job_id,)).rowcount
    if dirty and (written or deleted):
        mark_dirty(conn)
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


def refresh_claim(cron_dir: Path, job_id: str, claim_key: str, expected_owner: str,
                  at: str) -> Optional[bool]:
    """``refresh_claim_conn`` in its own transaction."""
    with transaction(cron_dir) as conn:
        return refresh_claim_conn(conn, job_id, claim_key, expected_owner, at)


def refresh_claim_conn(conn: sqlite3.Connection, job_id: str, claim_key: str,
                       expected_owner: str, at: str) -> Optional[bool]:
    """Compare-and-refresh ``fields[claim_key].at`` for *expected_owner* (``fire_claim`` or
    ``run_claim``) on *conn* (caller owns the IMMEDIATE transaction): one UPDATE guarded by the
    owner column; rowcount 0 means the claim is gone or held by someone else. ``None`` = the job
    has no row yet (not adopted from jobs.json): the caller must take the full load path, never
    read that as a lost claim."""
    global UPSERT_COUNT
    column = {"fire_claim": "fire_claim_owner", "run_claim": "run_claim_owner"}[claim_key]
    row = conn.execute(
        f"SELECT fields FROM job_runtime WHERE job_id=? AND {column}=?",
        (str(job_id), str(expected_owner))).fetchone()
    if row is None:
        exists = conn.execute("SELECT 1 FROM job_runtime WHERE job_id=?",
                              (str(job_id),)).fetchone()
        return False if exists else None
    fields = json.loads(row[0])
    claim = fields.get(claim_key)
    if not isinstance(claim, dict) or claim.get("by") != expected_owner:
        return False
    claim["at"] = at
    cur = conn.execute(
        f"UPDATE job_runtime SET fields=?, updated_at=? WHERE job_id=? AND {column}=?",
        (_canonical(fields), time.time(), str(job_id), str(expected_owner)))
    if cur.rowcount:
        mark_dirty(conn)
        UPSERT_COUNT += 1
    return bool(cur.rowcount)


# --- two-phase commit for saves that change BOTH halves ---------------------------------------
# 1. stage_pending: the new rows go into pending_runtime under a fresh generation id, each with
#    the row it was computed from (``base``); COMMIT.
# 2. mark_published (own transaction, right BEFORE the rename): pending_runtime.published_at is
#    the generation's commit marker (Apollo ruling R2). The file name is no longer the only one:
#    a sibling's rename can erase "runtime_gen" from the file, never the row in the database.
# 3. jobs.json is renamed into place carrying the same id as top-level "runtime_gen".
# 4. apply_pending: pending rows move into job_runtime, three-way merged against ``base`` so a
#    row write that landed between 1 and 3 (a degraded-lock sibling) survives; COMMIT.
# recover_pending (every load): a gen that is published OR named by the file's "runtime_gen" is
# rolled forward (merge3 makes a replay harmless; a crash between 2 and 3 applies rows the file
# never named, accepted as the lesser evil versus losing a committed update). An unpublished gen
# is discarded only when its owner is provably dead, and never under a degraded jobs lock (R1).

def merge3(ours: Dict[str, Any], current: Dict[str, Any],
           base: Dict[str, Any]) -> Dict[str, Any]:
    """Per-key three-way merge base -> (ours, current), the same rule as
    ``cron.jobs._reconcile_job_fields``: a key *ours* changed wins, a key it left alone takes
    *current* (absent stays absent). Stdlib-only so out-of-process readers resolve a pending
    generation exactly the way ``apply_pending`` will."""
    merged = dict(ours)
    for key in set(current) | set(base):
        in_ours = key in ours
        ours_changed = (key in base) != in_ours or (in_ours and ours[key] != base.get(key))
        if ours_changed:
            continue
        if key in current:
            merged[key] = current[key]
        else:
            merged.pop(key, None)
    return merged


_NO_ROW = "null"  # pending_runtime.base for a job that had no row at stage time


def _resolve_pending(staged: Dict[str, Any], base_text: Optional[str],
                     current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The row a staged entry becomes on apply. ``base_text`` NULL = staged by an earlier PR
    head with no base recorded: the staged row wins as it did then."""
    if base_text is None:
        return staged
    base = json.loads(base_text)
    base = base if isinstance(base, dict) else {}
    if current is None or current == base:
        return staged
    return merge3(staged, current, base)


def stage_pending(conn: sqlite3.Connection, gen: str, rows: Dict[str, Dict[str, Any]],
                  delete_ids: Iterable[str] = (),
                  bases: Optional[Dict[str, Optional[Dict[str, Any]]]] = None) -> None:
    """Stage generation *gen*. ``bases[job_id]`` is the row each staged row was computed from
    (``None`` = the job had no row); read in this same transaction by the caller."""
    # Only THIS generation: under a degraded jobs lock a sibling save may have its own staged.
    conn.execute("DELETE FROM pending_runtime WHERE gen=?", (gen,))
    at = _now()
    for job_id, fields in rows.items():
        if bases is None:
            base_text = None  # no base known: the staged row wins on apply
        else:
            base = bases.get(str(job_id))
            base_text = _canonical(base) if base is not None else _NO_ROW
        conn.execute("INSERT INTO pending_runtime(gen, job_id, fields, staged_at, base)"
                     " VALUES (?,?,?,?,?)", (gen, str(job_id), _canonical(fields), at, base_text))
    for job_id in delete_ids:
        if job_id:
            conn.execute("INSERT OR REPLACE INTO pending_runtime(gen, job_id, fields, staged_at)"
                         " VALUES (?,?,NULL,?)", (gen, str(job_id), at))


def pending_gens(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT gen FROM pending_runtime").fetchall()]


def pending_discard_after() -> float:
    """Age (seconds, by ``staged_at``) past which a generation the file does NOT name is provably
    dead: ``max(2 x mirror interval, 60)``. A younger one may belong to a sibling save still in
    flight under a degraded jobs lock (between its stage and its rename)."""
    return max(2.0 * mirror_interval(), 60.0)


def pending_gen_ages(conn: sqlite3.Connection) -> Dict[str, Optional[float]]:
    """``{gen: newest staged_at}`` (``None`` = a row staged before the column existed)."""
    return {gen: (float(at) if at is not None else None) for gen, at in conn.execute(
        "SELECT gen, max(staged_at) FROM pending_runtime GROUP BY gen").fetchall()}


def mark_published(conn: sqlite3.Connection, gen: str) -> None:
    """Commit marker of generation *gen* (caller owns the transaction; call right BEFORE the rename
    that publishes it, ruling R2): from here on recovery rolls it forward whatever the file names."""
    conn.execute("UPDATE pending_runtime SET published_at=? WHERE gen=?", (_now(), gen))


def published_gens(conn: sqlite3.Connection) -> List[str]:
    """Pending generations whose commit marker is set, oldest publication first. Tolerates a
    database from an earlier PR head without the column (read-only callers cannot add it)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_runtime)")}
    if "published_at" not in cols:
        return []
    return [r[0] for r in conn.execute(
        "SELECT gen, max(published_at) AS p FROM pending_runtime WHERE published_at IS NOT NULL"
        " GROUP BY gen ORDER BY p, gen").fetchall()]


def _pending_entries(conn: sqlite3.Connection, gen: str) -> List[tuple]:
    """``[(job_id, fields_text_or_None, base_text_or_None)]`` for generation *gen* (a database
    from an earlier PR head may lack the ``base`` column; read-only callers cannot add it)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_runtime)")}
    base_col = "base" if "base" in cols else "NULL"
    return conn.execute(f"SELECT job_id, fields, {base_col} FROM pending_runtime WHERE gen=?",
                        (gen,)).fetchall()


def _apply_entries(rows: Dict[str, Dict[str, Any]], entries: List[tuple]) -> Dict[str, Any]:
    """Resolve pending *entries* onto *rows* (``{job_id: row}``) in place, exactly as
    ``apply_pending`` persists them. Returns ``{"upsert": {...}, "delete": [...]}``."""
    upsert: Dict[str, Dict[str, Any]] = {}
    deletes: List[str] = []
    for job_id, fields, base_text in entries:
        job_id = str(job_id)
        if fields is None:
            deletes.append(job_id)
            rows.pop(job_id, None)
            continue
        resolved = _resolve_pending(json.loads(fields), base_text, rows.get(job_id))
        upsert[job_id] = resolved
        rows[job_id] = resolved
    return {"upsert": upsert, "delete": deletes}


def apply_pending(conn: sqlite3.Connection, gen: str) -> int:
    """Move generation *gen* into job_runtime (NULL fields = delete) and clear its journal rows.
    Each staged row is three-way merged against the row it was computed from and the row as it is
    NOW (same IMMEDIATE transaction), so a key a degraded-lock sibling wrote between the stage and
    this apply (a manual-run request, a heartbeat) survives unless this save changed that key.
    Bumps the mirror seq on any write (Prism 8f09e01d8cd4): a concurrent save that read seq before
    this apply wrote a mirror without these rows, so it must not be able to mark the mirror clean.
    The cost is at most one coalesced mirror rewrite per definition save."""
    entries = _pending_entries(conn, gen)
    current = {jid: row for jid, row in ((str(e[0]), read_row(conn, str(e[0]))) for e in entries)
               if row is not None}
    plan = _apply_entries(current, entries)
    written = upsert_rows(conn, plan["upsert"], delete_ids=plan["delete"])  # dirty=True
    conn.execute("DELETE FROM pending_runtime WHERE gen=?", (gen,))
    return written


def resolve_committed_in_memory(conn: sqlite3.Connection, rows: Dict[str, Dict[str, Any]],
                                file_gen: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Apply every COMMITTED pending generation (published, R2, or named by the file) onto
    *rows* in memory, in recovery order, WITHOUT writing: the view a reader gets when it may not
    resolve the journal itself (read-only callers, a contended section under R4)."""
    committed = published_gens(conn)
    if isinstance(file_gen, str) and file_gen and file_gen not in committed:
        committed.append(file_gen)
    for gen in committed:
        _apply_entries(rows, _pending_entries(conn, gen))
    return rows


def merged_jobs(jobs_file: Path, *, strict: bool = False,
                complete: bool = False) -> List[Dict[str, Any]]:
    """READ-ONLY merged view for out-of-process readers (cron-health, dashboards, watchers that
    read jobs.json directly): the raw jobs list with each job's runtime row overlaid, i.e. the
    shape jobs.json had before the split. Never creates the database, never migrates, never
    writes; a row wins over a runtime field the file still carries.

    The rows and the pending generation are read in ONE read transaction (one WAL snapshot), so
    an apply landing between the two reads cannot make the committed update vanish. A database
    error returns the raw file view for monitors (best effort) and RAISES when *strict* (callers
    that delete the database afterwards must never treat the raw view as merged). With *complete*
    (implies strict) it also raises when a row would be left out of the view: a row that is not a
    JSON object, or a row whose job is not in the file (Prism 93002a296759)."""
    strict = strict or complete
    jobs_file = Path(jobs_file)
    with open(jobs_file, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    path = db_path(jobs_file.parent)
    if not path.exists():
        return jobs
    file_gen = data.get("runtime_gen") if isinstance(data, dict) else None
    rows: Dict[str, Dict[str, Any]] = {}
    try:
        # as_uri() percent-encodes '#', '?', '%' and spaces in the path (Prism 943ffb5fb553): a raw
        # f"file:{path}" would end the path at the first '#' or '?' and open a different file.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0,
                               isolation_level=None)
        try:
            conn.execute("BEGIN")
            for job_id, fields in conn.execute(
                    "SELECT job_id, fields FROM job_runtime").fetchall():
                try:
                    value = json.loads(fields)
                except (TypeError, ValueError):
                    if strict:
                        raise
                    continue
                if isinstance(value, dict):
                    rows[str(job_id)] = value
                elif complete:
                    raise ValueError(f"runtime row {job_id!r} is not a JSON object")
            # A save that crashed after publishing left its generation staged: published (R2) or
            # named by the file, it is committed. Resolve it the way load_jobs' recovery
            # (apply_pending, same order) will, without writing.
            resolve_committed_in_memory(conn, rows, file_gen)
            conn.execute("COMMIT")
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        if strict:
            raise
        return jobs
    if complete:
        ids = {str(j.get("id")) for j in jobs if isinstance(j, dict)}
        missing = sorted(jid for jid in rows if jid not in ids)
        if missing:
            raise ValueError(f"runtime rows with no definition in {jobs_file}: {missing}")
    return [overlay_row(job, rows[str(job.get("id"))])
            if isinstance(job, dict) and str(job.get("id")) in rows else job for job in jobs]


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
    where the export's secret scrubber reads it. Never call on a live store (no lock taken).
    All or nothing (Prism 93002a296759): returns False (database kept, nothing written) when it
    cannot fold EVERY row: no definitions, the database cannot be read in full, a row is not a
    JSON object, or a row's job is not in jobs.json (dropping the db would lose it, and keeping it
    would ship it unscrubbed). jobs.json is replaced atomically before the database is removed."""
    cron_dir = Path(cron_dir)
    jobs_file = cron_dir / "jobs.json"
    if not db_path(cron_dir).exists():
        return False
    # Refuse (keep the database, return False) when there are no definitions to fold into: the
    # rows would otherwise be the only copy of the runtime state and the delete would lose them.
    if not jobs_file.exists():
        return False
    try:
        with open(jobs_file, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return False
    try:
        # complete=True: resolves a pending generation the file names, raises on any row it
        # cannot place.
        merged = merged_jobs(jobs_file, complete=True)
    except (OSError, ValueError, sqlite3.Error):
        return False
    payload = dict(data) if isinstance(data, dict) else {}
    for key in ("runtime_gen", MIRROR_SEQ_KEY, MIRROR_EPOCH_KEY):  # they describe the dropped db
        payload.pop(key, None)
    payload["jobs"] = merged
    tmp = jobs_file.with_name(FOLD_TMP_NAME)
    try:
        # Never through a preserved entry at that name (a symlink would truncate its target
        # outside the staging dir, Prism 5a6d55ddbf83): unlink it, then create exclusively.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
        os.replace(tmp, jobs_file)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    drop_database(cron_dir)
    return True


def drop_database(cron_dir: Path) -> None:
    """Remove the database and its side files (WAL/SHM, and the rollback journal a
    ``journal_mode: delete`` database leaves after a crash, Prism 15391e1b7adf) after a fold back
    into jobs.json."""
    base = db_path(cron_dir)
    for suffix in SIDECAR_SUFFIXES:
        with contextlib.suppress(FileNotFoundError):
            Path(str(base) + suffix).unlink()
