"""Cron job storage: ~/.hermes/cron/jobs.json; output in
~/.hermes/cron/output/{job_id}/{timestamp}.md"""

import contextlib
import copy
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
import shutil
import tempfile
import threading
import time
import os
import re
import uuid

# Cross-process advisory locking for jobs.json: fcntl (Unix) or msvcrt (Windows). If both are
# absent, _jobs_lock() degrades to in-process locking rather than failing.
try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None
from datetime import datetime, timedelta, timezone
from pathlib import Path
from hermes_constants import get_hermes_home
from cron.constants import CLAIM_TTL_INACTIVITY_HEADROOM, FIRE_CLAIM_SKEW_SECONDS, FIRE_CLAIM_TTL_SECONDS
from cron.env_settings import cron_env_setting
from typing import Optional, Dict, List, Any, Callable, Set, Tuple, Union, Collection

logger = logging.getLogger(__name__)

from hermes_time import now as _hermes_now
from hermes_time import get_timezone
from hermes_cli.observability.shared_metrics_gateway import record_cron_missed
from utils import atomic_replace, atomic_write_text, fsync_directory

# croniter is imported lazily (slow import, only needed for cron exprs). HAS_CRONITER stays a
# module attribute: a monkeypatched value wins because _ensure_croniter only probes while None.
croniter = None
HAS_CRONITER: Optional[bool] = None


def _ensure_croniter() -> bool:
    """Import croniter on first use; honor a pre-set HAS_CRONITER override."""
    global croniter, HAS_CRONITER
    if HAS_CRONITER is None:
        try:
            from croniter import croniter as _croniter
            croniter = _croniter
            HAS_CRONITER = True
        except ImportError:
            HAS_CRONITER = False
    return bool(HAS_CRONITER)


# --- Configuration ---

# Cron is per-profile by design: anchor at get_hermes_home() (active profile home), NOT
# get_default_hermes_root() — the shared root would funnel every profile's jobs into one jobs.json
# and run them under the ticker's HERMES_HOME, leaking config/credentials/skills across profiles.
# Each profile owns its own cron store under its own HERMES_HOME, and a profile-scoped gateway runs that
# profile's jobs under that same HERMES_HOME — so a job authored in profile `coder` lives in
# `~/.hermes/profiles/coder/cron/jobs.json` and executes with `coder`'s `.env`, `config.yaml`, and skills.
# Do NOT change this to the default root: that re-breaks per-profile isolation. See also the dynamic
# `_get_hermes_home()` / `_get_lock_paths()` resolution in cron/scheduler.py. See #4707.
HERMES_DIR = get_hermes_home().resolve()
# Default-profile fallback and compatibility surface for callers/tests. Cross-profile callers must
# scope paths with use_cron_store() instead of mutating these process-wide.
CRON_DIR = HERMES_DIR / "cron"
JOBS_FILE = CRON_DIR / "jobs.json"
# Heartbeat: touched every ticker loop so `hermes cron status` can tell the ticker THREAD is alive,
# not just the gateway PROCESS; success = last tick that completed WITHOUT raising.
# The gateway process and the (separate) ``hermes cron status`` process share it so status can tell whether
# the ticker THREAD is alive, not just whether the gateway PROCESS exists — a ticker that dies silently
# inside a live gateway would otherwise report healthy (#32612, #32895).
TICKER_HEARTBEAT_FILE = CRON_DIR / "ticker_heartbeat"
TICKER_SUCCESS_FILE = CRON_DIR / "ticker_last_success"
# Single source of truth for the ticker interval (scheduler_provider.py) and the staleness
# threshold in `hermes cron status` (hermes_cli/cron.py), so they never drift apart.
TICKER_INTERVAL_SECONDS = 60

# In-process lock for load_jobs→modify→save_jobs cycles; without it, parallel tick threads'
# mark_job_run / advance_next_run calls clobber each other.
_jobs_file_lock = threading.RLock()
_jobs_lock_state = threading.local()
_fire_fence_locks: Dict[str, threading.RLock] = {}
_fire_fence_locks_guard = threading.Lock()
_fire_fence_lock_state = threading.local()

# Upper bound on waiting for the cross-process .jobs.lock. Every cron function funnels through
# _jobs_lock(), so blocking forever on a wedged sibling process would freeze the ticker and every
# job. 30s is far above any legitimate critical section yet under one status-alarm threshold.
_JOBS_LOCK_TIMEOUT_SECONDS = 30.0
OUTPUT_DIR = CRON_DIR / "output"
ONESHOT_GRACE_SECONDS = 120

# Restart catch-up window for one-shots (``cron.oneshot_catchup_s``). A
# gateway restart is routine (every runtime deploy), so a one-shot whose fire
# time fell inside the down window is FIRED LATE on boot — with a note
# prepended to its prompt — when it is past due by at most this much. Beyond
# it the record is retired and a loud MISSED notice is delivered. This only
# widens the due-scan / misfire-backstop gate: create/update/resume still
# reject a run time more than ONESHOT_GRACE_SECONDS in the past.
DEFAULT_ONESHOT_CATCHUP_SECONDS = 6 * 3600

# Transient (never persisted) key stamped on the in-memory due-job snapshot of
# a one-shot fired late; _build_job_prompt turns it into the late-fire note.
LATE_FIRE_KEY = "late_fire_seconds"


def _oneshot_catchup_seconds() -> float:
    """Resolve ``cron.oneshot_catchup_s`` from config.yaml.

    Default ``DEFAULT_ONESHOT_CATCHUP_SECONDS`` (6h). Never narrower than
    ONESHOT_GRACE_SECONDS; a non-positive value disables the catch-up (the
    pre-existing 120s behaviour).
    """
    value: float = float(DEFAULT_ONESHOT_CATCHUP_SECONDS)
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        raw = cron_cfg.get("oneshot_catchup_s", DEFAULT_ONESHOT_CATCHUP_SECONDS)
        value = float(raw) if raw is not None else float(DEFAULT_ONESHOT_CATCHUP_SECONDS)
    except Exception:
        value = float(DEFAULT_ONESHOT_CATCHUP_SECONDS)
    return max(float(ONESHOT_GRACE_SECONDS), value)


def late_fire_note(late_seconds: Any) -> str:
    """Prompt prefix for a one-shot fired late after a gateway restart."""
    try:
        minutes = max(1, int(round(float(late_seconds) / 60.0)))
    except (TypeError, ValueError):
        minutes = 1
    return (
        f"[Note: this scheduled task fired late by {minutes} min after a "
        "gateway restart — its scheduled time fell while the scheduler was "
        "down. Check that the action still makes sense now before acting.]"
    )


# One-shots retired by the due-scan because they were past due beyond the
# catch-up window. The scan runs under _jobs_lock and must not deliver, so it
# queues a snapshot here; the scheduler tick drains it and delivers a loud
# MISSED notice to the job's own deliver target.
_missed_oneshot_notices: List[Dict[str, Any]] = []
_missed_oneshot_notices_lock = threading.Lock()


def drain_missed_oneshot_notices() -> List[Dict[str, Any]]:
    """Pop every queued missed-one-shot notice (``{"job": ..., "text": ...}``)."""
    with _missed_oneshot_notices_lock:
        drained = list(_missed_oneshot_notices)
        _missed_oneshot_notices.clear()
    return drained


@dataclass(frozen=True)
class _CronStorePaths:
    cron_dir: Path
    jobs_file: Path
    output_dir: Path

    @classmethod
    def for_dir(cls, cron_dir: Path) -> "_CronStorePaths":
        return cls(cron_dir, cron_dir / "jobs.json", cron_dir / "output")


_cron_store_override: ContextVar[Optional[_CronStorePaths]] = ContextVar(
    "cron_store_override", default=None)


class _SelfRemovalDelivery:
    """Mutable run-local marker shared with the agent's copied ContextVar context."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.removed = False


_self_removal_delivery: ContextVar[Optional[_SelfRemovalDelivery]] = ContextVar(
    "self_removal_delivery", default=None)


@contextlib.contextmanager
def self_removal_delivery_scope(job_id: str):
    """Permit this run's final delivery after it removes its own job record."""
    marker = _SelfRemovalDelivery(job_id)
    token = _self_removal_delivery.set(marker)
    try:
        yield marker
    finally:
        _self_removal_delivery.reset(token)


def self_removal_delivery_allowed(job_id: str) -> bool:
    """Whether the active run deleted exactly its own job record and no record has since taken
    its id (a replacement record belongs to another owner, so that stays fail-closed)."""
    marker = _self_removal_delivery.get()
    if marker is None or marker.job_id != job_id or not marker.removed:
        return False
    return all(item.get("id") != job_id for item in load_jobs())

# Import-time snapshot so deliberate re-pointing of CRON_DIR/JOBS_FILE/OUTPUT_DIR (the documented
# escape hatch for tests/embedders) is distinguishable from the constants merely being stale.
_IMPORT_STORE = _CronStorePaths(CRON_DIR, JOBS_FILE, OUTPUT_DIR)


def _current_cron_store() -> _CronStorePaths:
    """Paths pinned to this execution context's profile. Precedence: (1) active use_cron_store()
    override; (2) deliberately re-pointed module constants; (3) the ACTIVE profile home via
    get_hermes_home(), so re-pointing HERMES_HOME after import uses ITS OWN store rather than the
    user's real jobs.json frozen at import; (4) import-time constants."""
    override = _cron_store_override.get()
    if override is not None:
        return override
    live_constants = _CronStorePaths(CRON_DIR, JOBS_FILE, OUTPUT_DIR)
    if live_constants != _IMPORT_STORE:
        return live_constants
    home = get_hermes_home().resolve()
    if home == HERMES_DIR:
        return live_constants
    return _CronStorePaths.for_dir(home / "cron")


@contextlib.contextmanager
def use_cron_store(home: Union[str, Path]):
    """Route cron storage to ``home`` without mutating process globals."""
    token = _cron_store_override.set(
        _CronStorePaths.for_dir(Path(home).expanduser().resolve() / "cron"))
    try:
        yield
    finally:
        _cron_store_override.reset(token)


def get_cron_output_dir() -> Path:
    """Return the output directory for the active cron store context."""
    return _current_cron_store().output_dir


# Fallback stale-recovery window for a one-shot's running-claim when HERMES_CRON_TIMEOUT=0
# (unlimited, no bound to derive from); also the floor so a tiny timeout can't expire a claim
# mid-run.
ONESHOT_RUN_CLAIM_TTL_SECONDS = 1800

# Derived TTL = inactivity timeout × CLAIM_TTL_INACTIVITY_HEADROOM (cron/constants.py). The TTL
# only recovers a claim left by a tick that DIED mid-run.

_DEFAULT_CRON_INACTIVITY_TIMEOUT = 600.0


def _oneshot_run_claim_ttl_seconds() -> float:
    """One-shot running-claim TTL from ``HERMES_CRON_TIMEOUT``: unset/invalid → 600s → 1800s;
    ``0`` (unlimited) → the fixed floor; positive N → ``max(N * headroom, floor)``."""
    raw = cron_env_setting("HERMES_CRON_TIMEOUT").strip()
    try:
        timeout = float(raw) if raw else _DEFAULT_CRON_INACTIVITY_TIMEOUT
    except (ValueError, TypeError):
        timeout = _DEFAULT_CRON_INACTIVITY_TIMEOUT
    if timeout <= 0:
        return float(ONESHOT_RUN_CLAIM_TTL_SECONDS)
    return max(timeout * CLAIM_TTL_INACTIVITY_HEADROOM, float(ONESHOT_RUN_CLAIM_TTL_SECONDS))


def _job_running_in_this_process(job_id: str) -> bool:
    """True when the scheduler in THIS process is still running ``job_id``: the run_claim TTL alone
    cannot distinguish "claiming tick died" from "alive but slow". Lazy import: scheduler imports
    us.

    Direct liveness signal for stale-entry recovery (#62002): the run_claim TTL alone cannot distinguish
    "the claiming tick died" from "the run is alive but slow" — a run stalled on network I/O (or a laptop
    that slept mid-run) legitimately outlives the TTL. The in-process ticker and the run share this process,
    so the scheduler's running set settles the common single-gateway case without any claim-age guesswork.

    Home-scoped: one process ticks every profile, so the host-wide union of bare job ids would let
    profile A's running ``daily-brief`` keep profile B's stale one-shot alive forever.
    """
    try:
        from cron.scheduler import is_job_running
        return is_job_running(job_id)
    except Exception:
        logger.warning(
            "Cron running-set liveness check failed for job %r; keeping the "
            "entry to avoid deleting a possibly live one-shot run",
            job_id, exc_info=True)
        return True


def _jobs_lock_file() -> Path:
    """Return the advisory lock path for the current cron directory."""
    return _current_cron_store().cron_dir / ".jobs.lock"


def _acquire_flock(lock_fd, timeout: float) -> Optional[bool]:
    """Bounded exclusive lock: True when acquired, False on timeout, None when no backend exists. A
    blocking flock(LOCK_EX) taken under the in-process lock would let a wedged sibling freeze
    EVERY cron function forever, so poll LOCK_NB against a deadline; the caller picks the
    degraded mode."""
    if fcntl is not None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
    if msvcrt is not None:
        getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        return True
    return None


def _release_flock(lock_fd) -> None:
    """Unlock (best effort) and close a lock file opened for ``_acquire_flock``."""
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            getattr(msvcrt, "locking")(lock_fd.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
    except (OSError, IOError):
        pass
    finally:
        lock_fd.close()


@contextlib.contextmanager
def _jobs_lock():
    """Serialize a load_jobs→modify→save_jobs critical section: in-process RLock (parallel tick
    threads) plus a cross-process flock on ``<cron dir>/.jobs.lock`` (gateway vs. CLI writes —
    otherwise a `cron pause` could be clobbered and keep firing). Nested calls in one thread
    reuse the held lock. Without a flock backend, or on flock timeout (logged loudly), it
    degrades to in-process-only locking: a briefly torn cross-process write beats a dead
    scheduler."""
    depth = getattr(_jobs_lock_state, "depth", 0)
    if depth:
        _jobs_lock_state.depth = depth + 1
        try:
            yield
        finally:
            _jobs_lock_state.depth -= 1
        return

    with _jobs_file_lock:
        _jobs_lock_state.depth = 1
        # jobs.json stamp as of this section's load_jobs(): lets _save_jobs_unlocked skip the
        # shrink-merge parse when the file provably hasn't changed. Reset on entry/exit so stale
        # stamps from unlocked loads or prior sections can never suppress a needed merge.
        # See #80703.
        _jobs_lock_state.load_stamp = None
        _jobs_lock_state.load_baseline = None
        lock_fd = None
        try:
            try:
                ensure_dirs()
                lock_fd = open(_jobs_lock_file(), "a+", encoding="utf-8-sig")
                lock_fd.seek(0)
                if _acquire_flock(lock_fd, _JOBS_LOCK_TIMEOUT_SECONDS) is False:
                    logger.error(
                        "Timed out after %.0fs waiting for the cron "
                        "jobs lock (%s) — another process is holding "
                        "it. Proceeding with in-process locking only "
                        "so the scheduler stays alive (#60703).",
                        _JOBS_LOCK_TIMEOUT_SECONDS, _jobs_lock_file())
                    with contextlib.suppress(OSError):
                        lock_fd.close()
                    lock_fd = None
            except (OSError, IOError) as e:
                # A locking failure must never take down cron writes — in-process lock still held.
                logger.warning("jobs.json cross-process lock unavailable (%s); "
                               "proceeding with in-process lock only", e)
            try:
                yield
            finally:
                if lock_fd is not None:
                    _release_flock(lock_fd)
        finally:
            _jobs_lock_state.depth = 0
            _jobs_lock_state.load_stamp = None
            _jobs_lock_state.load_baseline = None


@contextlib.contextmanager
def _fire_job_lock(job_id: str, *, timeout: Optional[float] = None):
    """Serialize one job's owner mutations and external side effects. Unlike the global jobs lock
    this may be held across network delivery; scoped to one profile + job so unrelated jobs keep
    progressing. Fails closed when cross-process locking is unavailable.

    ``timeout`` bounds the WHOLE wait — the in-process lock as well as the
    cross-process flock. The gateway shutdown path is such a caller, and it
    runs on the event loop the fence holder is waiting on (t_8d085477).
    ``None`` keeps the default bound: ``_JOBS_LOCK_TIMEOUT_SECONDS`` for the
    in-process wait and again for the flock.
    """
    cron_dir = _current_cron_store().cron_dir
    lock_key = f"{cron_dir.resolve()}::{job_id}"
    with _fire_fence_locks_guard:
        local_lock = _fire_fence_locks.setdefault(lock_key, threading.RLock())

    local_timeout = _JOBS_LOCK_TIMEOUT_SECONDS
    flock_timeout = _JOBS_LOCK_TIMEOUT_SECONDS
    if timeout is not None:
        timeout = max(float(timeout), 0.0)
        local_timeout = timeout
        flock_timeout = min(flock_timeout, timeout)
    started = time.monotonic()
    if not local_lock.acquire(timeout=local_timeout):
        logger.error(
            "Timed out after %.1fs waiting for in-process fire fence of job %s (%s); failing closed",
            local_timeout, job_id, lock_key,
        )
        yield False
        return
    if timeout is not None:
        flock_timeout = max(flock_timeout - (time.monotonic() - started), 0.0)

    held_locks = _fire_fence_lock_state.__dict__.setdefault("held", {})
    if lock_key in held_locks:
        try:
            yield held_locks[lock_key]
        finally:
            local_lock.release()
        return

    try:
        ensure_dirs()
        lock_path = cron_dir / f".fire-{uuid.uuid5(uuid.NAMESPACE_URL, lock_key).hex}.lock"
        lock_fd = None
        acquired = False
        try:
            lock_fd = open(lock_path, "a+", encoding="utf-8")
            lock_fd.seek(0)
            result = _acquire_flock(lock_fd, flock_timeout)
            if result is None:  # pragma: no cover - supported platforms provide one backend
                logger.error("No cross-process lock backend for cron fire fence")
            elif not result:
                logger.error("Timed out waiting for fire fence %s; failing closed", lock_path)
            acquired = bool(result)
        except (OSError, IOError) as exc:
            logger.error("Cron fire fence unavailable for %s: %s", job_id, exc)

        held_locks[lock_key] = acquired
        try:
            yield acquired
        finally:
            held_locks.pop(lock_key, None)
            if lock_fd is not None:
                if acquired:
                    _release_flock(lock_fd)
                else:
                    lock_fd.close()
    finally:
        local_lock.release()


def _under_fire_fence(
    job_id: str, fn: Callable[[], Any], *, timeout: Optional[float] = None
) -> Any:
    """Run ``fn()`` holding the job's fire fence; False (fail closed) when it can't be acquired.
    ``timeout`` bounds the whole wait (see ``_fire_job_lock``)."""
    with _fire_job_lock(job_id, timeout=timeout) as acquired:
        if not acquired:
            return False
        return fn()


@contextlib.contextmanager
def fire_claim_fence(job_id: str, *, expected_owner: str):
    """Hold a per-job fence while an owner performs an external side effect. A missing record
    is accepted only for the active run that removed this exact job (#111039)."""
    with _fire_job_lock(job_id) as acquired:
        if not acquired:
            yield False
            return
        with _jobs_lock():
            job = next((item for item in load_jobs() if item.get("id") == job_id), None)
            claim = job.get("fire_claim") if isinstance(job, dict) else None
            owns_claim = isinstance(claim, dict) and claim.get("by") == expected_owner
            if job is None:
                owns_claim = self_removal_delivery_allowed(job_id)
        yield owns_claim


# Fields that must never change after creation: ``id`` is a path component under OUTPUT_DIR, so an
# update could leak ``../escape``/absolute/nested values into output writes/deletes.
_IMMUTABLE_JOB_FIELDS = frozenset({"id"})


def _job_output_dir(job_id: str) -> Path:
    """Resolve a job's output directory, rejecting any path-escape attempt (``..``, absolute
    paths, separators): only a single safe path component is accepted."""
    text = str(job_id or "").strip()
    if (
        not text or text in {".", ".."} or "/" in text or "\\" in text
        or Path(text).is_absolute() or Path(text).drive
    ):
        raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
    return _current_cron_store().output_dir / text


def _normalize_skill_list(skill: Optional[str] = None, skills: Optional[Any] = None) -> List[str]:
    """Normalize legacy/single-skill and multi-skill inputs into a unique ordered list."""
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)
    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _apply_skill_fields(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a job dict with canonical `skills` and legacy `skill` fields aligned."""
    normalized = dict(job)
    skills = _normalize_skill_list(normalized.get("skill"), normalized.get("skills"))
    normalized["skills"] = skills
    normalized["skill"] = skills[0] if skills else None
    return normalized


def _coerce_job_text(value: Any, fallback: str = "") -> str:
    """Coerce legacy/hand-edited nullable cron fields to strings for readers."""
    return fallback if value is None else str(value)


def humanize_cron(expression: str) -> str:
    """Describe the numeric five-field cron forms accepted by ``parse_schedule``."""
    parts = expression.split()
    if len(parts) != 5:
        return expression
    minute, hour, day, month, weekday = parts
    months = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    weekdays = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

    def join(items: List[str]) -> str:
        if len(items) < 3:
            return " and ".join(items)
        return f"{', '.join(items[:-1])}, and {items[-1]}"

    def numbers(field: str) -> List[int]:
        return [int(value) for value in field.split(",")]

    def clock(hour_value: int, minute_value: int = 0) -> str:
        suffix = "AM" if hour_value < 12 else "PM"
        return f"{hour_value % 12 or 12}:{minute_value:02d} {suffix}"

    def hour_scope(field: str) -> str:
        scopes = []
        for item in field.split(","):
            if "-" in item:
                start, end = (int(value) for value in item.split("-", 1))
                scopes.append(f"from {clock(start)} through {clock(end, 59)}")
            else:
                scopes.append(f"during the {clock(int(item))} hour")
        return join(scopes)

    try:
        if minute.startswith("*/"):
            time_text = f"Every {int(minute[2:])} minutes"
            if hour != "*":
                time_text += f" during each hour {hour_scope(hour)}"
        elif hour == "*":
            minute_values = numbers(minute)
            if minute_values == [0]:
                time_text = "At the start of every hour"
            else:
                label = "minute" if len(minute_values) == 1 else "minutes"
                time_text = f"At {label} {join([str(value) for value in minute_values])} past every hour"
        elif hour.startswith("*/"):
            interval = int(hour[2:])
            minute_values = numbers(minute)
            if minute_values == [0]:
                time_text = f"Every {interval} hours starting at midnight"
            else:
                time_text = f"At {join([str(value) for value in minute_values])} minutes past every {interval} hours starting at midnight"
        elif "-" in hour:
            minute_values = numbers(minute)
            if minute_values == [0]:
                time_text = f"At the start of each hour {hour_scope(hour)}"
            else:
                time_text = f"At minutes {join([str(value) for value in minute_values])} past each hour {hour_scope(hour)}"
        else:
            times = [clock(h, m) for h in numbers(hour) for m in numbers(minute)]
            time_text = f"At {join(times)}"

        if day == "*" and month == "*" and weekday == "*":
            date_text = "every day"
        elif day == "*" and month == "*":
            date_text = f"every {join([weekdays[value % 7] for value in numbers(weekday)])}"
        elif weekday == "*" and day != "*":
            day_values = numbers(day)
            day_text = f"day {day_values[0]}" if len(day_values) == 1 else f"days {join([str(value) for value in day_values])}"
            if month == "*":
                date_text = f"on {day_text} of every month"
            elif month.startswith("*/"):
                date_text = f"on {day_text}, every {int(month[2:])} months starting in January"
            else:
                month_names = [months[value - 1] for value in numbers(month)]
                if len(day_values) == len(month_names) == 1:
                    date_text = f"on {month_names[0]} {day_values[0]} every year"
                else:
                    date_text = f"on {day_text} in {join(month_names)}"
        else:
            return expression
    except (IndexError, ValueError):
        return expression

    separator = ", " if date_text.startswith("every day") else " "
    return f"{time_text}{separator}{date_text}"


# Fields whose presence in an update can turn a runnable job into an empty one.
_PAYLOAD_FIELDS = frozenset({"prompt", "script", "skill", "skills", "no_agent"})

EMPTY_PAYLOAD_ERROR = (
    "Cron job has nothing to run: the prompt is blank and no script or "
    "skill(s) are set. Provide a prompt, a script, or at least one skill."
)

NO_AGENT_WITHOUT_SCRIPT_ERROR = (
    "no_agent=True requires a script — with no agent and no script "
    "there is nothing for the job to run."
)


def job_payload_is_empty(job: Dict[str, Any]) -> bool:
    """True when a job record has nothing runnable (blank prompt, no script, no skills) AND at
    least one payload field is explicitly present. ``no_agent`` already requires a script."""
    if _coerce_job_text(job.get("prompt")).strip() or _coerce_job_text(job.get("script")).strip():
        return False
    if _normalize_skill_list(job.get("skill"), job.get("skills")):
        return False
    return any(k in job for k in ("prompt", "script", "skill", "skills"))


def _schedule_display_for_job(job: Dict[str, Any]) -> str:
    display = _coerce_job_text(job.get("schedule_display")).strip()
    if display:
        return display
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        for key in ("display", "value", "expr", "run_at"):
            text = _coerce_job_text(schedule.get(key)).strip()
            if text:
                return text
    elif schedule is not None:
        return str(schedule)
    return "?"


def _normalize_job_record(job: Dict[str, Any]) -> Dict[str, Any]:
    """Read-safe job shape: legacy/hand-edited records may have nullable ``prompt``, ``name``,
    ``schedule_display``. Storage is untouched; consumers never crash on formatting."""
    normalized = _apply_skill_fields(job)
    schedule = normalized.get("schedule")
    if isinstance(schedule, dict) and schedule.get("kind") == "cron":
        expression = _coerce_job_text(schedule.get("expr")).strip()
        if expression:
            # Humanize on read ONLY when the stored display IS the raw cron
            # expression (or missing) -- a legacy record that never got a
            # readable display. The goal of this pass is "never show a user a
            # bare cron expression", not "restate every schedule in our own
            # words" (parity merge 2026-08-29).
            #
            # A display that DIFFERS from the expression is already readable,
            # and is often the natural phrasing the user actually typed:
            # parse_schedule accepts "every monday 9am" / "every day at 9am"
            # and deliberately keeps that wording as the display while storing
            # the compiled expr. Re-humanizing it here rewrote the user's own
            # words back at them ("At 9:00 AM, every day"), which is what the
            # unconditional form did before this guard.
            stored_display = _coerce_job_text(schedule.get("display")).strip()
            if not stored_display or stored_display == expression:
                display = humanize_cron(expression)
                normalized["schedule"] = {**schedule, "display": display}
                normalized["schedule_display"] = display
            elif _coerce_job_text(normalized.get("schedule_display")).strip() == expression:
                # The schedule dict carries a readable display but the
                # denormalized mirror is still the raw expr -- sync the mirror
                # so no consumer reading schedule_display sees the expression.
                normalized["schedule_display"] = stored_display
    job_id = _coerce_job_text(normalized.get("id"), "unknown")
    prompt = _coerce_job_text(normalized.get("prompt"))
    normalized["id"] = job_id
    normalized["prompt"] = prompt

    name = _coerce_job_text(normalized.get("name")).strip()
    if not name:
        label_source = (
            prompt
            or (normalized["skills"][0] if normalized.get("skills") else "")
            or _coerce_job_text(normalized.get("script")).strip()
            or job_id
            or "cron job"
        )
        name = label_source[:50].strip() or "cron job"
    normalized["name"] = name
    normalized["schedule_display"] = _schedule_display_for_job(normalized)
    # Derived from the scheduler-honoured ``enabled`` flag so a half-paused record cannot render
    # "paused" while still firing. See effective_job_state().
    normalized["state"] = effective_job_state(normalized)
    return normalized


def _has_pause_marker(job: Dict[str, Any]) -> bool:
    """True when the record carries any operator-facing pause signal."""
    return _coerce_job_text(job.get("state")).strip() == "paused" or bool(job.get("paused_at"))


def is_job_runnable(job: Dict[str, Any]) -> bool:
    """True iff the scheduler may fire this job: ``enabled`` plus pause markers as a second gate so
    a contradictory half-paused record never fires even before self-heal runs."""
    return bool(job.get("enabled", True)) and not _has_pause_marker(job)


def effective_job_state(job: Dict[str, Any]) -> str:
    """Operator-facing state derived from ``enabled``: an enabled job must never display as paused
    (list looked frozen while jobs kept firing). Terminal states are preserved regardless."""
    stored = _coerce_job_text(job.get("state")).strip()
    if stored in {"completed", "error"}:
        return stored
    if not job.get("enabled", True):
        if _has_pause_marker(job) or stored == "paused":
            return "paused"
        return stored or "paused"
    # enabled=true is authoritative: never claim paused
    if stored == "paused" or job.get("paused_at"):
        return "scheduled"
    return stored or "scheduled"


def is_terminal_job(job: Dict[str, Any]) -> bool:
    """Return whether a job record is in a terminal scheduler state."""
    return job.get("state") in {"completed", "error"}


def _is_recoverable_error_job(job: Dict[str, Any]) -> bool:
    """True for a recurring job stuck in ``state=error`` (set ONLY when ``compute_next_run()`` fails
    for a cron/interval job: croniter missing, malformed schedule). Such a job still has future
    occurrences once the issue resolves, so treating it as terminal would block due-scan self-heal,
    pre-advance, dispatch claim and ``resume_job`` — wedging it forever.

    Unlike ``state=completed`` (a one-shot that genuinely has no more occurrences, ever), an error-state
    recurring job still has a schedule with future occurrences once the underlying issue resolves — it is
    stuck pending a ``next_run_at`` recompute, not truly done. See #16265.
    """
    return (
        job.get("state") == "error"
        and (job.get("schedule") or {}).get("kind") in {"cron", "interval"}
    )


def _secure_dir(path: Path):
    """Owner-only (0700) via the shared helper, so cron/ and cron/output honor the same managed/
    container/HERMES_HOME_MODE rules as the rest of HERMES_HOME (#10757)."""
    from hermes_cli.config import _secure_dir as _shared_secure_dir
    _shared_secure_dir(path)


def _secure_file(path: Path):
    """Owner-only (0600) via the shared helper (managed/container skip included)."""
    from hermes_cli.config import _secure_file as _shared_secure_file
    _shared_secure_file(path)


def _preserve_file_ownership(path: Path, before: Optional[os.stat_result]) -> None:
    """Restore a rewritten file's previous owner (POSIX, root writer only): atomic replace makes the
    file owned by the writer's euid, so a root CLI write (e.g. ``docker exec``) against the
    unprivileged gateway's store would flip jobs.json to root:root 0600 and lock the ticker out."""
    if before is None or os.name != "posix":
        return
    geteuid = getattr(os, "geteuid", None)
    getegid = getattr(os, "getegid", None)
    if geteuid is None or getegid is None:
        return
    try:
        euid = geteuid()
        if euid != 0 or (before.st_uid, before.st_gid) == (euid, getegid()):
            return  # unprivileged writer, or already ours before the rewrite
        os.chown(path, before.st_uid, before.st_gid)
    except OSError as e:
        logger.warning(
            "Could not restore ownership of %s to uid=%s gid=%s after rewrite: %s "
            "— if the gateway runs as a different user, its cron ticker may now "
            "be locked out (see issue #68483).",
            path, before.st_uid, before.st_gid, e)


def _is_named_profile_path(path: Path) -> bool:
    """True if *path* is under ``<hermes_home>/profiles/<name>/`` (default/custom homes are not).
    Checks the resolved path (symlinked parents) and the raw path (symlinked profile homes)."""
    with contextlib.suppress(OSError, RuntimeError):
        if "profiles" in path.resolve().parts:
            return True
    return "profiles" in path.parts


def _ensure_cron_dir(cron_dir: Path) -> None:
    """Create a cron directory without resurrecting a deleted profile home: a stale multiplex
    scheduler may still hold a deleted profile's path, so named profiles use ``parents=False`` and
    fail closed. Default/custom homes keep ``parents=True`` so first-run creation works."""
    if _is_named_profile_path(cron_dir):
        cron_dir.mkdir(exist_ok=True)
        return
    cron_dir.mkdir(parents=True, exist_ok=True)


def ensure_dirs():
    """Ensure cron directories exist with secure permissions."""
    store = _current_cron_store()
    _ensure_cron_dir(store.cron_dir)
    _ensure_cron_dir(store.output_dir)
    _secure_dir(store.cron_dir)
    _secure_dir(store.output_dir)


# --- Schedule Parsing ---

def normalize_repeat_value(repeat: Any) -> Optional[int]:
    """Coerce a repeat value (int or user-facing string) into ``Optional[int]``:
    ``'forever'``-family -> None, ``'once'``-family -> 1, numeric -> int, 0/negative -> None,
    else ValueError.

    The tool schema exposes ``repeat`` as an integer, but agents and users legitimately pass the user-facing
    strings ``'forever'``/``'once'`` or numeric strings (``'3'``). Uncoerced strings previously died with
    ``'<=' not supported between instances of 'str' and 'int'`` at create
    (#66824/#64520/#7142/#71987/#95706) and were stored raw by update paths, breaking ``mark_job_run``
    later.
    """
    if repeat is None:
        return None
    if isinstance(repeat, str):
        repeat_str = repeat.strip().lower()
        if repeat_str in ("forever", "infinite", "inf", "none", ""):
            return None
        if repeat_str in ("once", "one", "1x"):
            return 1
        try:
            repeat = int(repeat_str)
        except ValueError:
            raise ValueError(
                f"Invalid repeat value {repeat!r}: use an integer, "
                f"'forever', or 'once'."
            )
    return None if repeat <= 0 else int(repeat)


_DURATION_MULTIPLIERS = {'m': 1, 'h': 60, 'd': 1440}


def parse_duration(s: str) -> int:
    """Parse a duration into minutes: "30m" → 30, "2h" → 120, "1d" → 1440, bare "hour" → 60."""
    s = s.strip().lower()
    match = re.match(r'^(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$', s)
    if not match:
        raise ValueError(
            f"Invalid duration: '{s}'. Use format like '30m', '2h', '1d', "
            "or a bare unit like 'hour' (defaults to 1).")
    value = int(match.group(1)) if match.group(1) else 1
    return value * _DURATION_MULTIPLIERS[match.group(2)[0]]


# Day-spec phrases for "every monday 9am" / "every day at 9am". Cron weekday numbering is
# 0=Sunday … 6=Saturday (croniter's default).
_WEEKDAY_TO_CRON_DOW = {
    "sunday": "0", "sun": "0",
    "monday": "1", "mon": "1",
    "tuesday": "2", "tue": "2", "tues": "2",
    "wednesday": "3", "wed": "3", "weds": "3",
    "thursday": "4", "thu": "4", "thur": "4", "thurs": "4",
    "friday": "5", "fri": "5",
    "saturday": "6", "sat": "6",
}

# Keyword day-specs that expand to a cron weekday field.
_DAYSPEC_TO_CRON_DOW = {
    "day": "*", "daily": "*", "everyday": "*",
    "weekday": "1-5", "weekdays": "1-5",
    "weekend": "0,6", "weekends": "0,6",
}


def _parse_clock_time(text: str) -> Optional[tuple]:
    """Parse ``9am``/``9:30am``/``14:00``/``7`` (bare 24h hour)/``noon``/``midnight`` into a
    24-hour ``(hour, minute)`` tuple, or None when unrecognized."""
    t = text.strip().lower().replace(" ", "")
    if not t:
        return None
    if t in ("noon", "midday"):
        return (12, 0)
    if t == "midnight":
        return (0, 0)
    match = re.match(r'^(\d{1,2})(?::(\d{2}))?(am|pm)?$', t)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return (hour, minute)


def _natural_every_to_cron(rest: str) -> Optional[str]:
    """Convert ``<when> [at] <time>`` ("monday 9am", "weekday at 9am", "monday, wednesday at 9am")
    to a 5-field cron expr, or None so ``parse_schedule`` can fall back to the interval path."""
    tokens = rest.lower().replace(",", " ").split()
    if not tokens:
        return None
    # Leading day tokens: a keyword spec ("weekdays") or a comma/"and"-separated weekday list.
    dow = _DAYSPEC_TO_CRON_DOW.get(tokens[0])
    idx = 1
    if dow is None:
        days = []
        idx = len(tokens)
        for i, tok in enumerate(tokens):
            if tok == "and":
                continue
            mapped = _WEEKDAY_TO_CRON_DOW.get(tok)
            if mapped is None:
                idx = i
                break
            if mapped not in days:
                days.append(mapped)
        if not days:
            return None
        dow = ",".join(days)
    time_tokens = tokens[idx:]
    if time_tokens and time_tokens[0] == "at":  # optional separator: "every day at 9am"
        time_tokens = time_tokens[1:]
    if not time_tokens:
        return None
    parsed = _parse_clock_time(" ".join(time_tokens))
    if parsed is None:
        return None
    hour, minute = parsed
    return f"{minute} {hour} * * {dow}"


def _cron_schedule(
    expr: str, display: str, missing_croniter: str, invalid_label: str
) -> Dict[str, Any]:
    """Validate a cron expression with croniter and build the stored schedule dict."""
    if not _ensure_croniter():
        raise ValueError(f"{missing_croniter} Install with: pip install croniter")
    try:
        croniter(expr)
    except Exception as e:
        raise ValueError(f"Invalid {invalid_label} '{display}': {e}")
    return {"kind": "cron", "expr": expr, "display": display}


def _interval_schedule(minutes: int) -> Dict[str, Any]:
    return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}


def parse_schedule(schedule: str) -> Dict[str, Any]:
    """Parse a schedule string into ``{"kind": "once"|"interval"|"cron", ...}`` with ``run_at`` /
    ``minutes`` / ``expr``. "30m" and "every 30m" are recurring intervals; "every monday 9am" and
    "0 9 * * *" are cron; an ISO timestamp is once."""
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()

    # Natural day/time phrase → cron ("every monday 9am", or sans prefix "weekdays at 9am");
    # any other "every X" → recurring interval.
    is_every = schedule_lower.startswith("every ")
    rest = schedule[6:].strip() if is_every else schedule_lower
    cron_expr = _natural_every_to_cron(rest)
    # Reuse the same helper — the phrase shape is identical without the "every " prefix. See #51975.
    if cron_expr is not None:
        example = "every monday 9am" if is_every else "weekdays at 9am"
        return _cron_schedule(
            cron_expr, original,
            f"Weekday/time schedules like '{example}' require the 'croniter' package.", "schedule")
    if is_every:
        return _interval_schedule(parse_duration(rest))

    # Cron expression (5-6 fields). Letters are allowed so named months/weekdays (JAN-DEC, MON-FRI)
    # reach croniter, which supports them.
    parts = schedule.split()
    if len(parts) >= 5 and all(re.match(r'^[A-Za-z\d\*\-,/]+$', p) for p in parts[:5]):
        return _cron_schedule(
            schedule, humanize_cron(schedule), "Cron expressions require 'croniter' package.", "cron expression")
    # ISO timestamp (contains T or looks like date)
    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        try:
            dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            # Naive timestamps become aware in the CONFIGURED Hermes timezone (not server-local):
            # the due-check compares against hermes_time.now().
            # Make naive timestamps timezone-aware at parse time so the stored value doesn't depend on the
            # system timezone matching at check time. UTC) while now() runs in Asia/Kolkata, the stored
            # instant would land hours off from the user's wall-clock intent — far enough that one-shots
            # never become due and recurring jobs fire at the wrong time. Using the configured zone makes
            # "20:07" mean 20:07 on the same clock the scheduler checks against (#51021).
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_hermes_now().tzinfo)
            return {
                "kind": "once",
                "run_at": dt.isoformat(),
                "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}"
            }
        except ValueError as e:
            raise ValueError(f"Invalid timestamp '{schedule}': {e}")

    # "in 30m"/"in 2h" is the explicit one-shot-by-duration form; a bare duration ("30m") is a
    # RECURRING interval per the documented tool contract.
    if schedule_lower.startswith("in "):
        duration_str = schedule[3:].strip()
        try:
            minutes = parse_duration(duration_str)
        except ValueError:
            raise ValueError(
                f"Invalid duration '{duration_str}' after 'in '. Use e.g. 'in 30m', 'in 2h'.")
        now = _hermes_now()
        # Durations measure elapsed time, not wall-clock hours across a DST transition.
        run_at = (now.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(now.tzinfo)
        return {"kind": "once", "run_at": run_at.isoformat(), "display": f"once in {duration_str}"}
    with contextlib.suppress(ValueError):
        return _interval_schedule(parse_duration(schedule))

    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        f"  - Interval: '30m', 'every 30m', 'every 2h' (recurring)\n"
        f"  - One-shot delay: 'in 30m', 'in 2h' (fires once)\n"
        f"  - Weekly/daily: 'every monday 9am', 'weekdays at 9am' (recurring)\n"
        f"  - Cron: '0 9 * * *' (cron expression)\n"
        f"  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)"
    )


def _ensure_aware(dt: datetime) -> datetime:
    """Aware datetime in the configured Hermes timezone. Legacy naive values are read as
    *system-local* wall time (what created them) then converted, preserving ordering across
    timezone changes and avoiding false not-due results."""
    target_tz = _hermes_now().tzinfo
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(target_tz)
    return dt.astimezone(target_tz)


def _elapsed_seconds(later: datetime, earlier: datetime) -> float:
    """Return elapsed seconds between aware instants, independent of wall time."""
    return (later.astimezone(timezone.utc) - earlier.astimezone(timezone.utc)).total_seconds()


def _instant_after(left: datetime, right: datetime) -> bool:
    """Whether *left* is a later absolute instant than *right*."""
    return left.astimezone(timezone.utc) > right.astimezone(timezone.utc)


def _instant_at_or_before(left: datetime, right: datetime) -> bool:
    """Whether *left* is at or before *right* as an absolute instant."""
    return left.astimezone(timezone.utc) <= right.astimezone(timezone.utc)


def _instant_before(left: datetime, right: datetime) -> bool:
    """Whether *left* is an earlier absolute instant than *right*."""
    return left.astimezone(timezone.utc) < right.astimezone(timezone.utc)


def _seconds_after(dt: datetime, seconds: float) -> datetime:
    """*dt* plus real *seconds*, in *dt*'s zone. Aware ``+ timedelta`` is wall-clock arithmetic
    that drops ``fold``, so inside a fall-back hour it lands an hour off."""
    return (dt.astimezone(timezone.utc) + timedelta(seconds=seconds)).astimezone(dt.tzinfo)


def _parse_aware(value: Any) -> Optional[datetime]:
    """``_ensure_aware(datetime.fromisoformat(value))``, or None when *value* is not a parseable ISO
    string."""
    try:
        return _ensure_aware(datetime.fromisoformat(value))
    except Exception:
        return None


def _timezone_offset_mismatch(stored: datetime, current: datetime) -> bool:
    """True when a stored aware timestamp uses a different UTC offset. Naive values return False:
    they are normalized by ``_ensure_aware`` and intentionally never take the offset-repair path."""
    if stored.tzinfo is None or current.tzinfo is None:
        return False
    return stored.utcoffset() != current.utcoffset()


def _stored_wall_clock_is_future(stored: datetime, current: datetime) -> bool:
    """True when the stored local wall-clock time has not arrived yet. Cron expresses wall-clock
    intent; after a timezone change an old offset can make a future run look due (21:00+10 →
    13:00+02). Comparing naive wall clocks separates that from a genuine miss."""
    return stored.replace(tzinfo=None) > current.replace(tzinfo=None)


def _recoverable_oneshot_run_at(
    schedule: Dict[str, Any], now: datetime, *, last_run_at: Optional[str] = None,
) -> Optional[str]:
    """One-shot run time if still eligible: a small grace window covers jobs created just after
    their minute; once run, a one-shot is never eligible again."""
    if not isinstance(schedule, dict) or schedule.get("kind") != "once" or last_run_at:
        return None
    run_at = schedule.get("run_at")
    run_at_dt = _parse_aware(run_at) if run_at else None
    if run_at_dt is not None and _elapsed_seconds(now, run_at_dt) <= ONESHOT_GRACE_SECONDS:
        return run_at
    return None


_MIN_GRACE_SECONDS = 120
_MAX_GRACE_SECONDS = 7200


def _compute_grace_seconds(schedule: dict) -> int:
    """How late a job can be and still catch up rather than fast-forward: half the period, clamped
    to [120s, 2h], so daily jobs catch up but frequent jobs fast-forward quickly."""
    period_seconds = _schedule_cadence_seconds(schedule)
    if not period_seconds:
        return _MIN_GRACE_SECONDS
    return max(_MIN_GRACE_SECONDS, min(int(period_seconds) // 2, _MAX_GRACE_SECONDS))


# A recurring dispatch within this many seconds of schedule renders "on time": a busy once-a-minute
# ticker can slip a couple of minutes — normal cadence, not gateway downtime.
# See #99879.
_LATE_DISPATCH_TOLERANCE_SECONDS = 300


def _classify_dispatch_lateness(lateness_seconds: float, grace_seconds: int) -> str:
    """``on_time`` (within ticker slack), ``late`` (within the catch-up grace window), or
    ``catch_up`` (beyond grace; accumulated misses skipped, executed once now)."""
    if lateness_seconds > grace_seconds:
        return "catch_up"
    if lateness_seconds > _LATE_DISPATCH_TOLERANCE_SECONDS:
        return "late"
    return "on_time"


# Recovery counter for recurring jobs wedged in stale ``last_status == "error"`` with a future
# next_run_at.
_persisted_error_recoveries: int = 0
# Bounded in-memory history kept by every probe-visible fire-path counter.
_TELEMETRY_RECENT_HISTORY = 20
_persisted_error_recoveries_recent: list = []


def _job_is_stale_error_recurring(
    job: Dict[str, Any], schedule: Dict[str, Any], now: datetime,
) -> bool:
    """True when a recurring job (caller-checked) is wedged in a stale persisted error state:
    ``last_status == "error"``, not running in this process (never re-arm a live run underneath
    itself), and ``last_run_at`` older than ``cadence + grace`` (a job merely erroring-and-retrying
    on schedule stays fresh and is not flagged).

    Condition (all must hold): * it has NOT successfully re-fired within its natural cadence — its
    ``last_run_at`` is older than ``cadence + grace``, so this is not a normal transient-error retry that
    will fire on its own soon, it is a job that has been sitting errored for a full period with no recovery;
    See #62002.
    """
    if job.get("last_status") != "error":
        return False
    from cron.quota_hold import hold_active
    if hold_active(job, now):
        return False  # deliberately parked past a provider usage window, not wedged (#89376)
    if _job_running_in_this_process(str(job.get("id") or "")):
        return False
    # A fresh fire_claim means the job is running in ANOTHER process sharing this
    # store, not wedged; re-arming it here would only claim-fight the live run.
    if _claim_is_live(job.get("fire_claim"), now, FIRE_CLAIM_TTL_SECONDS):
        return False
    last_run = job.get("last_run_at")
    last_run_dt = _parse_aware(last_run) if last_run else None
    if last_run_dt is None:
        return False
    age_seconds = _elapsed_seconds(now, last_run_dt)
    if age_seconds < 0:
        return False
    grace = _compute_grace_seconds(schedule)
    cadence_seconds = _schedule_cadence_seconds(schedule)
    if cadence_seconds is None:
        # Unknown cadence: fall back to the grace window, never re-arming anything younger than it.
        cadence_seconds = grace
    return age_seconds > (cadence_seconds + grace)


# Per-expr cache for _schedule_cadence_seconds' croniter measurements.
_cron_cadence_cache: Dict[str, Optional[float]] = {}


def _schedule_cadence_seconds(schedule: Dict[str, Any]) -> Optional[float]:
    """Approximate schedule period in seconds, or None (croniter missing / malformed expr). Cron
    results are cached per expr because this runs under ``_jobs_lock`` every tick; the gap can vary
    with base time for irregular exprs, acceptable for a staleness *threshold*."""
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind == "interval":
        minutes = schedule.get("minutes")
        try:
            return float(minutes) * 60.0 if minutes else None
        except (TypeError, ValueError):
            return None
    if kind != "cron" or not _ensure_croniter():
        return None
    expr = schedule.get("expr")
    if not expr:
        return None
    if expr in _cron_cadence_cache:
        return _cron_cadence_cache[expr]
    try:
        it = croniter(expr, _hermes_now())
        first = it.get_next(datetime)
        gap = (it.get_next(datetime) - first).total_seconds()
        result = gap if gap > 0 else None
    except Exception:
        result = None
    # Hard bound so deleted/edited exprs can't grow the cache unboundedly in a long-lived gateway.
    if len(_cron_cadence_cache) >= 256:
        _cron_cadence_cache.clear()
    _cron_cadence_cache[expr] = result
    return result


def _append_telemetry_record(filename: str, entry: Dict[str, Any], recent: list) -> None:
    """Record ``entry`` in the bounded ``recent`` list and append to ``<cron_dir>/<filename>``
    (best effort — telemetry must never break a tick). Counters stay module-level ints per
    metric because tests reset them by name."""
    recent.append(entry)
    del recent[:-_TELEMETRY_RECENT_HISTORY]
    try:
        path = _current_cron_store().cron_dir / filename
        _ensure_cron_dir(path.parent)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.debug("Could not append %s record: %s", filename, exc)


def _record_persisted_error_recovery(job: Dict[str, Any], previous_next_run: str) -> None:
    """Persist a countable, probe-visible signal for one stale-error re-arm."""
    global _persisted_error_recoveries
    entry = {
        "job_id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "previous_next_run_at": previous_next_run,
        "rearmed_at": _hermes_now().isoformat(),
    }
    _persisted_error_recoveries += 1
    _append_telemetry_record(
        "persisted_error_recoveries.jsonl", entry, _persisted_error_recoveries_recent)


def get_persisted_error_recovery_stats() -> Dict[str, Any]:
    """Probe-visible snapshot of persisted-error recoveries."""
    return {
        "persisted_error_recoveries": _persisted_error_recoveries,
        "recent": list(_persisted_error_recoveries_recent),
    }


def _cron_next_run_matches_expr(schedule: Dict[str, Any], next_run_dt: datetime) -> bool:
    """Whether ``next_run_dt`` is an occurrence of the schedule's current expr (detects a
    hand-edited ``schedule.expr`` whose stored ``next_run_at`` came from the old one).
    Best-effort: anything uncheckable (non-cron, no expr, no croniter, malformed) reports a
    match.

    A direct ``jobs.json`` edit can change ``schedule.expr`` while leaving the stored ``next_run_at``
    computed under the *old* expression (#93049). The stored instant is stale exactly when it is not an
    occurrence of the current expression. Validation is best-effort: anything that cannot be checked
    (non-cron kind, missing expr, croniter unavailable, malformed input) reports a match so the fire path
    keeps its existing semantics.
    """
    if schedule.get("kind") != "cron":
        return True
    expr = schedule.get("expr")
    if not expr or not _ensure_croniter() or croniter is None:
        return True
    try:
        # Last occurrence at-or-before the instant: base one second past it so an exact hit is
        # included, then compare at second granularity (croniter is second-precision).
        prev = croniter(str(expr), next_run_dt + timedelta(seconds=1)).get_prev(datetime)
        return abs((prev - next_run_dt).total_seconds()) < 1.0
    except Exception:
        return True


# Classifications for a due cron instant NOT on the current expr (see
# _classify_stale_cron_next_run).
STALE_CRON_MATCH = "match"
STALE_CRON_TIMEZONE_MIGRATION = "timezone_migration"
STALE_CRON_EXPR_EDIT = "expr_edit"


def _classify_stale_cron_next_run(
    schedule: Dict[str, Any], raw_next_run_dt: datetime, next_run_dt: datetime,
) -> str:
    """Explain WHY a stored ``next_run_at`` misses the current cron lattice; the causes need
    opposite actions. ``expr_edit``: a hand edit changed ``schedule.expr`` — re-anchor WITHOUT
    firing. ``timezone_migration``: only the offset representation changed (legacy UTC rows
    normalized into the profile tz); treating it as an edit would skip a due, never-fired
    occurrence. Discriminator: normalization moved the wall clock AND the stored instant's OWN
    wall clock is a legal occurrence (when offsets agree a genuine expr edit can never be misread
    as a migration).

    * ``expr_edit`` — a direct ``jobs.json`` edit changed ``schedule.expr`` while leaving ``next_run_at``
    computed under the old one (#93049). Upgrading from a UTC-scheduling build to one that honours the
    profile timezone leaves legacy rows like ``2026-09-02T04:00:00+00:00`` for ``0 4 * * *``; normalizing to
    Europe/Brussels turns that into ``06:00+02``, which the expression excludes. Treating it as a stale edit
    re-anchored to tomorrow and silently skipped a due occurrence that had never fired.
    """
    if _cron_next_run_matches_expr(schedule, next_run_dt):
        return STALE_CRON_MATCH
    wall_clock_shifted = raw_next_run_dt.replace(tzinfo=None) != next_run_dt.replace(tzinfo=None)
    if wall_clock_shifted and _cron_next_run_matches_expr(schedule, raw_next_run_dt):
        return STALE_CRON_TIMEZONE_MIGRATION
    return STALE_CRON_EXPR_EDIT


# Offset-migration catch-ups on the fire path: climbing after a deploy = draining; steady = TZ
# churn.
_timezone_migration_catchups: int = 0
_timezone_migration_catchups_recent: list = []


def _record_timezone_migration_catchup(
    job: Dict[str, Any], raw_next_run_dt: datetime, next_run_dt: datetime,
) -> None:
    """Persist a countable signal for one offset-migration catch-up fire."""
    global _timezone_migration_catchups
    entry = {
        "job_id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "expr": (job.get("schedule") or {}).get("expr"),
        "stored_next_run_at": raw_next_run_dt.isoformat(),
        "normalized_next_run_at": next_run_dt.isoformat(),
        "fired_at": _hermes_now().isoformat(),
    }
    _timezone_migration_catchups += 1
    _append_telemetry_record(
        "timezone_migration_catchups.jsonl", entry, _timezone_migration_catchups_recent)


def get_timezone_migration_catchup_stats() -> Dict[str, Any]:
    """Probe-visible snapshot of offset-migration catch-up fires."""
    return {
        "timezone_migration_catchups": _timezone_migration_catchups,
        "recent": list(_timezone_migration_catchups_recent),
    }


def compute_next_run(schedule: Dict[str, Any], last_run_at: Optional[str] = None) -> Optional[str]:
    """Compute the next run time for a schedule as an ISO string, or None if no more runs."""
    now = _hermes_now()
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind == "once":
        return _recoverable_oneshot_run_at(schedule, now, last_run_at=last_run_at)
    # Recurring kinds anchor on last_run_at so a restart doesn't re-anchor the schedule.
    base_time = (_parse_aware(last_run_at) if last_run_at else None) or now
    if kind == "interval":
        minutes = schedule.get("minutes")
        if minutes is None:
            return None
        # Add in UTC so an interval keeps its duration when the profile's UTC offset changes.
        next_run = base_time.astimezone(timezone.utc) + timedelta(minutes=minutes)
        return next_run.astimezone(base_time.tzinfo).isoformat()
    if kind == "cron":
        expr = schedule.get("expr")
        if not expr:
            return None
        if not _ensure_croniter():
            logger.warning(
                "Cannot compute next run for cron schedule %r: 'croniter' is "
                "not installed. croniter is a core dependency as of v0.9.x; "
                "reinstall hermes-agent or run 'pip install croniter' in your runtime env.",
                expr)
            return None
        # Anchor cron matching to the CONFIGURED IANA timezone's WALL CLOCK,
        # not to the UTC offset carried by ``base_time``. croniter ignores
        # the tzinfo on its start time and uses the start's UTC offset as its
        # working offset, so a ``last_run_at`` stored in UTC (+00:00) would
        # push the next fire to 09:00 UTC instead of 09:00 local, and DST
        # transition days (spring-forward / fall-back) would land one hour off
        # (08:00 or 10:00). Render the base as the configured zone's naive
        # wall clock for croniter, then re-attach the zone to the result, so
        # the wall-clock hour stays correct every calendar day, including DST
        # boundaries (morning-routine 09:00 America/Toronto).
        # Fall back to the base's own zone only when nothing is configured.
        zone = get_timezone() or base_time.tzinfo
        base_wall = base_time.astimezone(zone).replace(tzinfo=None)
        it = croniter(expr, base_wall)
        # Strictly-after guard for the DST fall-back hour (qwen-code#11723 class):
        # attaching the zone to a naive wall clock resolves the repeated autumn hour
        # to its EARLIER occurrence (fold=0), so a base inside the second occurrence
        # got a "next run" up to an hour in the PAST — the fire path would re-fire
        # immediately and re-anchor, looping. Try both folds of each candidate wall
        # clock and return the earliest instant strictly after the base; a repeated
        # hour has two instants, so two candidates always suffice.
        base_ts = base_time.timestamp()
        next_wall = it.get_next(datetime)
        for _ in range(2):
            for fold in (0, 1):
                candidate = next_wall.replace(tzinfo=zone, fold=fold)
                if candidate.timestamp() > base_ts:
                    return candidate.isoformat()
            next_wall = it.get_next(datetime)
        return next_wall.replace(tzinfo=zone).isoformat()
    return None


# --- Ticker heartbeat (liveness signal for `hermes cron status`) ---

# Key on a recurring ``interval`` job: the instant the in-flight fire started
# (the tick/claim time that consumed a due ``next_run_at``). Written by the fire
# paths (``advance_next_runs`` for the ticker, ``claim_job_for_fire`` for
# external providers) and consumed + cleared by ``mark_job_run``, which re-arms
# the job at the first ``fire_at + k*interval`` after the run finished.
#
# Why not the finish time (the pre-t_b2f58c48 behaviour): ``finish + N`` always
# lands a few seconds AFTER tick k+N (the ticker waits 60 s between scans), so
# the job was only due on tick k+N+1 and every ``every N m`` job ran every N+1
# minutes. Why not the old ``next_run_at`` value: its phase is arbitrary, so a
# slot a few seconds after the tick phase fires ~1 tick late and the skip rule
# below then drops the next slot. The fire time IS a tick, and successive scans
# are >= 60 s apart, so ``fire_at + N`` is due no later than tick k+N.
INTERVAL_FIRE_AT_KEY = "interval_fire_at"


def _record_interval_fire(job: Dict[str, Any], now: datetime) -> None:
    """Stash when an interval fire started (see INTERVAL_FIRE_AT_KEY).

    Only recorded when the job is actually due (``next_run_at <= now``), so a
    second fire-path call for the same fire (the ticker advances, then claims)
    sees the already-advanced future value and keeps the first stamp.
    """
    if (job.get("schedule") or {}).get("kind") != "interval":
        return
    slot = job.get("next_run_at")
    if not slot:
        return
    try:
        if _ensure_aware(datetime.fromisoformat(slot)) <= now:
            job[INTERVAL_FIRE_AT_KEY] = now.isoformat()
    except (TypeError, ValueError):
        pass


def _next_interval_slot(
    schedule: Dict[str, Any], fire_at: Optional[str], now: datetime
) -> Optional[str]:
    """First ``fire_at + k*interval`` (k >= 1) strictly after ``now``.

    Slots that elapsed while an over-long run was in flight are skipped, never
    fired, so a slot cannot fire twice and missed slots do not burst. Returns
    None when the anchor or interval is unusable (caller falls back to
    ``compute_next_run``).
    """
    if not fire_at:
        return None
    try:
        period = timedelta(minutes=float(schedule.get("minutes")))
        anchor = _ensure_aware(datetime.fromisoformat(fire_at))
    except (TypeError, ValueError):
        return None
    if period <= timedelta(0):
        return None
    k = int((now - anchor) // period) + 1 if now >= anchor else 1
    return (anchor + k * period).isoformat()


def _write_marker(name: str, text: str, tmp_prefix: str) -> None:
    """Atomic (never torn) best-effort marker write; failures swallowed so markers never break the
    tick."""
    try:
        ensure_dirs()
        atomic_write_text(_current_cron_store().cron_dir / name, text, tmp_prefix=tmp_prefix, mode=0o600)
    except Exception:
        pass


def record_ticker_heartbeat(success: bool = False) -> None:
    """Record ticker liveness (+ last-success marker when ``success``) so `cron status` can tell
    "alive but failing" from "firing"; scoped per profile store.

    The ticker calls this once per loop iteration. ``success=True`` additionally bumps the *last successful
    tick* marker. We track two distinct signals so `hermes cron status` can tell a thread that is merely
    *alive and looping* (heartbeat fresh, success stale) from one that is actually *firing jobs* (both
    fresh) — a ticker stuck failing every tick would otherwise keep the plain heartbeat fresh and falsely
    report healthy (#32612, #32895).
    Resolution uses ``_current_cron_store()`` so the heartbeat is correctly scoped to the active profile's
    store — critical under multiplex_profiles where each profile needs its own liveness signal (#69377).
    """
    # ``<epoch> <pid>``: a killed ticker's last stamp reads fresh for ~3 minutes, so a reader with no
    # other proof of the scheduler (the in-process serve/Desktop ticker) checks the writer is alive.
    _write_marker("ticker_heartbeat", f"{time.time()} {os.getpid()}", ".hb_")
    if success:
        _write_marker("ticker_last_success", str(time.time()), ".hb_")


def _read_marker_fields(name: str) -> List[str]:
    try:
        return (_current_cron_store().cron_dir / name).read_text(encoding="utf-8-sig").split()
    except Exception:
        return []


def _epoch_file_age(name: str) -> Optional[float]:
    """Seconds since the epoch stamp stored in ``<cron_dir>/<name>``; None = missing/unreadable."""
    try:
        return max(0.0, time.time() - float(_read_marker_fields(name)[0]))
    except Exception:
        return None


def ticker_heartbeat_writer_alive() -> bool:
    """True when the process that wrote this store's ticker heartbeat is still running. A legacy
    bare-epoch stamp names no writer and is NOT proof of a live scheduler by itself."""
    fields = _read_marker_fields("ticker_heartbeat")
    try:
        from hermes_cli._subprocess_compat import pid_exists_stdlib
        return len(fields) >= 2 and pid_exists_stdlib(int(fields[1]))
    except Exception:
        return False


def get_ticker_heartbeat_age() -> Optional[float]:
    """Seconds since the ticker loop last iterated; None = missing/unreadable ("cannot determine",
    not "dead").

    Resolution uses ``_current_cron_store()`` so the heartbeat is correctly scoped to the active profile —
    critical under multiplex_profiles where ``hermes cron status`` must report per-profile liveness
    (#69377).
    """
    return _epoch_file_age("ticker_heartbeat")


def get_ticker_success_age() -> Optional[float]:
    """Seconds since the ticker last completed a tick WITHOUT raising, or None.

    Resolution uses ``_current_cron_store()`` so the heartbeat is correctly scoped to the active profile —
    critical under multiplex_profiles where ``hermes cron status`` must report per-profile liveness
    (#69377).
    """
    return _epoch_file_age("ticker_last_success")


def get_catch_up_occurrence_count() -> int:
    """Return the profile-local stale-schedule catch-up count."""
    path = _current_cron_store().cron_dir / "catch_up_occurrences"
    try:
        return max(0, int(path.read_text(encoding="utf-8-sig").strip()))
    except (OSError, ValueError):
        return 0


def record_catch_up_occurrence() -> None:
    """Increment the profile-local stale-schedule catch-up counter, best effort."""
    _write_marker("catch_up_occurrences", str(get_catch_up_occurrence_count() + 1), ".count_")


def record_ticker_error(message: str) -> None:
    """Persist the latest tick failure so `cron status` (another process) can show WHY, not just
    staleness."""
    _write_marker("ticker_last_error", f"{time.time()}\n{message.strip()}\n", ".terr_")


def clear_ticker_error() -> None:
    """Remove the last-tick-error marker after a successful tick. Best-effort."""
    with contextlib.suppress(OSError):
        (_current_cron_store().cron_dir / "ticker_last_error").unlink()


def get_ticker_last_error() -> Optional[str]:
    """Return the most recent recorded tick error message, or None."""
    try:
        raw = (_current_cron_store().cron_dir / "ticker_last_error").read_text(encoding="utf-8-sig")
    except Exception:
        return None
    lines = raw.splitlines()
    if len(lines) < 2:
        return None
    return "\n".join(lines[1:]).strip() or None


# --- Job CRUD Operations ---

def _parse_jobs_file(jobs_file: Path) -> Tuple[Any, bool]:
    """Tolerant jobs.json parse -> ``(data, used_strict_fallback)``: utf-8-sig absorbs a BOM, strict
    failure retries with ``strict=False``. IO/fallback errors propagate (caller decides repair vs
    bail)."""
    with open(jobs_file, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        return json.loads(raw, strict=False), True


def load_jobs() -> List[Dict[str, Any]]:
    """Load all jobs from storage."""
    jobs_file = _current_cron_store().jobs_file
    ensure_dirs()
    # Stamp BEFORE reading (fail-safe, see _record_load_stamp): a racing write then forces the
    # merge.
    pre_read_stamp = _jobs_file_stamp(jobs_file)
    if not jobs_file.exists():
        _record_load_stamp(None)
        _record_load_baseline([])
        return []

    try:
        data, _strict_retry = _parse_jobs_file(jobs_file)
    except IOError as e:
        logger.error("IOError reading jobs.json: %s", e)
        raise RuntimeError(f"Failed to read cron database: {e}") from e
    except Exception as e:
        logger.error("Failed to auto-repair jobs.json: %s", e)
        raise RuntimeError(f"Cron database corrupted and unrepairable: {e}") from e

    # Accept the canonical dict, or a bare list (auto-repair); any other top-level shape is
    # corruption. Repair details are logged only by the locked pass (an unlocked pass re-runs
    # under the lock below), so each warning is emitted once per repair.
    repair = "had invalid control characters" if _strict_retry else None
    notes: List[str] = []
    unmergeable = False  # disk shape _peek_jobs_unlocked cannot read: only a replace save fixes it
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if isinstance(jobs, dict):
            # ID-keyed map from external tools: flatten (inline "id" wins, else the key), skip junk.
            # _peek_jobs_unlocked deliberately does NOT flatten, so a merging save refuses this
            # shape; the repair below rewrites it with replace=True.
            skipped = [k for k, v in jobs.items() if not isinstance(v, dict)]
            if skipped:
                notes.append("Skipping %d non-dict entr%s in id-keyed jobs map: %s" % (
                    len(skipped), "y" if len(skipped) == 1 else "ies",
                    ", ".join(map(repr, skipped))))
            jobs = [{**v, "id": v.get("id") or k} for k, v in jobs.items() if isinstance(v, dict)]
            repair = "id-keyed jobs map flattened to list"
            unmergeable = True
        elif not isinstance(jobs, list):
            notes.append("Replacing invalid jobs.json 'jobs' field (%s) with an empty list"
                         % type(jobs).__name__)
            jobs = []
            repair = "invalid jobs field replaced with list"
            unmergeable = True
    elif isinstance(data, list):
        jobs = data
        repair = "bare list wrapped as dict"
    else:
        raise RuntimeError(
            f"Cron database corrupted: expected {{'jobs': [...]}}, got {type(data).__name__}")
    junk = [j for j in jobs if not isinstance(j, dict)]
    if junk:
        # Every reader and the due scan index records as dicts: one junk entry would crash the
        # whole tick and freeze every healthy sibling job, so skip it like the id-keyed map does.
        # Types only: the raw values are arbitrary file content and must not reach the logs.
        notes.append("Skipping %d non-object entr%s in jobs.json (types: %s)" % (
            len(junk), "y" if len(junk) == 1 else "ies",
            ", ".join(sorted({type(j).__name__ for j in junk}))))
        jobs = [j for j in jobs if isinstance(j, dict)]
        repair = repair or "non-object entries dropped"
    for job in jobs:
        # A hand-edited "completed" that is not a non-negative int (null, "2", 1.0, -5, Infinity)
        # would crash every counter reader (None += 1, "2" + 1), render as "None/3" / "2.0/3", or
        # grant extra runs; normalize it once here so readers can trust a non-negative int.
        # OverflowError: json.loads turns Infinity / 1e999 into float inf, and int(inf) raises.
        rep = job.get("repeat")
        if isinstance(rep, dict) and "completed" in rep and (
                type(rep["completed"]) is not int or rep["completed"] < 0):
            try:
                rep["completed"] = max(int(rep["completed"]), 0)
            except (TypeError, ValueError, OverflowError):
                rep["completed"] = 0
            repair = repair or "invalid repeat.completed normalized"
    # Persist even an empty result, or an all-junk store repeats the repair on every tick.
    if repair:
        if not getattr(_jobs_lock_state, "depth", 0):
            # An unlocked snapshot may predate a locked writer's update to a job it already holds
            # (the shrink-merge only restores missing ids), so re-read and repair under the lock.
            with _jobs_lock():
                return load_jobs()
        for note in notes:
            logger.warning("%s", note)
        # Keep the shrink-merge (a degraded-lock sibling's create may have landed since the read)
        # unless disk is STILL a shape the merge would refuse: a sibling may have rewritten it.
        save_jobs(jobs, replace=unmergeable and _peek_jobs_unlocked() is None)
        logger.warning("Auto-repaired jobs.json (%s)", repair)
    _record_load_stamp(pre_read_stamp)
    _record_load_baseline(jobs)
    return jobs


def _guard_against_test_write_to_live_store(jobs_file: Path) -> None:
    """Refuse a TEST-context cron write that targets a NON-TEMP (real) store.

    A test/e2e harness that imports ``cron.jobs`` while ``HERMES_HOME`` points at
    the real ``~/.hermes`` (a real-agent blackbox session, or a kanban worker
    booted from a worktree that shares the live home, running the cron test suite
    OUTSIDE pytest's hermetic conftest) would write its fixture jobs
    (``name=brief``/``claim job``/``paused job``, one-word prompts) straight into
    the LIVE ``cron/jobs.json`` — re-arming cron-config-lint and paging
    cron-health (2026-07-15 incident; recurred after the import-freeze fix
    because the harness runs with the live home, not a stale snapshot).

    Discriminator: a correctly-isolated test ALWAYS writes under the system temp
    dir (pytest's ``tmp_path`` lives under ``tempfile.gettempdir()`` — ``/tmp``,
    ``/var/folders``, ``/private/tmp``). The leak writes to a NON-temp home. So
    we fire ONLY when ``PYTEST_CURRENT_TEST`` is set (a genuine test context),
    no explicit ``use_cron_store()`` override is active, AND the target is NOT
    under the system temp dir. This is env-independent (unlike a ``Path.home()``
    check, which well-behaved tests legitimately monkeypatch to a tmp home), so
    it never false-fires on an isolated test while still catching the real leak.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _cron_store_override.get() is not None:
        return  # an explicit use_cron_store() scope is a deliberate target
    try:
        target = jobs_file.resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()
        try:
            target.relative_to(tmp_root)
            return  # under the system temp dir → a correctly-isolated test
        except ValueError:
            pass
        raise RuntimeError(
            f"Refusing to write the cron store to a NON-TEMP home ({jobs_file}) "
            f"from a pytest context ({os.environ.get('PYTEST_CURRENT_TEST')}). "
            "This is a test/e2e harness bypassing HERMES_HOME isolation — "
            "redirect HERMES_HOME to a tmp_path (pytest's autouse hermetic "
            "fixture) or wrap the write in use_cron_store(tmp_home)."
        )
    except RuntimeError:
        raise
    except Exception:
        # Temp-dir discovery is best-effort; never block a legitimate write.
        return


def _peek_jobs_unlocked() -> Optional[List[Dict[str, Any]]]:
    """Repair-free read under ``_jobs_lock()``: ``[]`` if missing, ``None`` if corrupt (never
    shrink-merge against an unknown baseline). Never saves — that would recurse."""
    jobs_file = _current_cron_store().jobs_file
    if not jobs_file.exists():
        return []
    try:
        data, _ = _parse_jobs_file(jobs_file)
    except Exception:
        return None
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        return jobs if isinstance(jobs, list) else None
    return data if isinstance(data, list) else None


def _jobs_file_stamp(jobs_file: Path) -> Optional[Tuple[int, int, int]]:
    """Shrink-merge fast-path stamp ``(mtime_ns, size, ino)``; ``None`` if unstatable. ``st_ino`` is
    included because every writer uses mkstemp+rename, so a same-size write in one mtime quantum
    can't false-match."""
    try:
        st = jobs_file.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def _record_load_stamp(stamp: Optional[Tuple[int, int, int]]) -> None:
    """Remember jobs.json's stamp for the enclosing _jobs_lock() section (no-op outside one) so the
    save path can skip the shrink-merge when disk provably hasn't changed. Capture it BEFORE
    reading: a mid-read sibling then mismatches (fail-safe); stamping after would certify an
    unseen write.

    Stamping after the read would let that sibling's write be certified as "seen" without being in the
    loaded payload, wrongly suppressing the recovery. See #80703.
    """
    if getattr(_jobs_lock_state, "depth", 0):
        _jobs_lock_state.load_stamp = stamp


def _unmerged_disk_jobs(
    jobs: List[Dict[str, Any]], removed_ids: Optional[Collection[str]]
) -> List[Dict[str, Any]]:
    """On-disk jobs missing from *jobs* and not intentionally removed. Stamp match => nothing
    landed, return without parsing; unreadable store => raise RuntimeError (fail closed: writing
    over an unknown baseline would silently drop every job in it)."""
    stamp = getattr(_jobs_lock_state, "load_stamp", None)
    if stamp is not None and _jobs_file_stamp(_current_cron_store().jobs_file) == stamp:
        return []
    disk_jobs = _peek_jobs_unlocked()
    if disk_jobs is None:
        raise RuntimeError(
            f"Cron database corrupted; refusing to overwrite {_current_cron_store().jobs_file}")
    seen = {str(j["id"]) for j in jobs if isinstance(j, dict) and j.get("id")}
    seen |= {str(i) for i in (removed_ids or ()) if i}
    recovered: List[Dict[str, Any]] = []
    for disk_job in disk_jobs:
        if not isinstance(disk_job, dict) or not disk_job.get("id"):
            continue
        disk_id = str(disk_job["id"])
        if disk_id not in seen:
            recovered.append(disk_job)
            seen.add(disk_id)
    return recovered


def _record_load_baseline(jobs: Optional[List[Dict[str, Any]]]) -> None:
    """Snapshot what this critical section READ, keyed by job id.

    The stamp above answers "did disk move?"; this answers the follow-up
    the save path actually needs — "which fields did *we* change?". Without
    it, a save is a whole-file read-modify-write from an in-memory copy:
    every field of every job in the payload overwrites disk, so a stale
    writer silently reverts a sibling's run-state update (``last_run_at``,
    ``last_status``, ``state``) even though neither writer touched the same
    field. That is the instance-3 shape: a one-shot ran, ``mark_job_run``
    wrote ``last_run_at``, and a concurrent editor holding a pre-run
    snapshot wrote it straight back to ``None``.

    Stored as a deep copy because every caller mutates the list ``load_jobs``
    handed back, in place. No-op outside a critical section (an unlocked
    load has no save to reconcile against).
    """
    if not getattr(_jobs_lock_state, "depth", 0):
        return
    if jobs is None:
        _jobs_lock_state.load_baseline = None
        return
    baseline: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        if isinstance(job, dict) and job.get("id"):
            baseline[str(job["id"])] = copy.deepcopy(job)
    _jobs_lock_state.load_baseline = baseline


def _reconcile_job_fields(
    payload_job: Dict[str, Any],
    disk_job: Dict[str, Any],
    baseline_job: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Three-way merge ONE job record: baseline -> (ours, disk).

    Returns the record to persist. A field we did not change since the
    baseline yields to disk (the sibling wrote it); a field we did change
    wins. A field we deleted stays deleted; a field only disk added is
    adopted. When both sides changed the same field there is no ordering
    information available, so ours wins (last-writer-wins) — the case is
    logged so a genuinely contended field is visible rather than silent.

    Without a baseline we cannot tell "unchanged" from "deliberately reset",
    so the payload is returned untouched (pre-existing behaviour).
    """
    if baseline_job is None:
        return payload_job

    merged = dict(payload_job)
    conflicts: List[str] = []
    for key in set(disk_job) | set(baseline_job):
        in_payload = key in payload_job
        base_val = baseline_job.get(key)
        ours_changed = (
            (key in baseline_job) != in_payload
            or (in_payload and payload_job[key] != base_val)
        )
        if ours_changed:
            if key in disk_job and disk_job[key] != base_val:
                conflicts.append(key)
            continue
        # We left this field alone since the baseline — defer to disk.
        if key in disk_job:
            merged[key] = disk_job[key]
        else:
            merged.pop(key, None)

    if conflicts:
        logger.warning(
            "Cron job %s: concurrent edits to the same field(s) %s — keeping "
            "this writer's values (no ordering information available)",
            payload_job.get("id"),
            sorted(conflicts),
        )
    return merged


def _merge_unexpected_disk_jobs(
    jobs: List[Dict[str, Any]], *, removed_ids: Optional[Collection[str]] = None,
) -> List[Dict[str, Any]]:
    """Return *jobs* reconciled against on-disk state (#80624).

    Two distinct losses are repaired here, both caused by a save being a
    whole-file read-modify-write from a possibly-stale in-memory copy:

    * **Vanished jobs** — an id present on disk but absent from the payload
      is a concurrent create, and is merged back.
    * **Reverted fields** — an id present in BOTH is reconciled field by
      field against this section's load baseline, so a field this writer
      never touched keeps the sibling's value instead of being overwritten
      with our stale copy. This is the run-state loss (``last_run_at``,
      ``last_status``, ``state`` reverting to their pre-run values).

    Under ``_jobs_lock()``'s degraded flock-timeout path (#60703), two
    processes can both believe they own the store. A writer that loaded an
    older snapshot then calls ``save_jobs`` and would otherwise clobber
    concurrent work (the filed ``no_agent`` watchdog pattern: CLI/tool
    create succeeds, then a gateway tick/remove rewrites ``jobs.json``
    empty or without the new id).

    Intentional deletes pass ``removed_ids`` and are never resurrected. The
    caller's list is never mutated — a new list is returned when anything
    was recovered or reconciled. An unreadable store raises RuntimeError
    (fail closed: writing over an unknown baseline would silently drop every
    job in it).

    Fast path: when the enclosing critical section recorded a load stamp
    and the file's ``(mtime_ns, size, ino)`` still matches, nothing can have
    changed underneath us, so the read+parse is skipped entirely — one
    ``stat()`` on the healthy no-race save.
    """
    stamp = getattr(_jobs_lock_state, "load_stamp", None)
    if stamp is not None and _jobs_file_stamp(_current_cron_store().jobs_file) == stamp:
        return jobs

    disk_jobs = _peek_jobs_unlocked()
    if disk_jobs is None:
        raise RuntimeError(
            f"Cron database corrupted; refusing to overwrite {_current_cron_store().jobs_file}")

    baseline = getattr(_jobs_lock_state, "load_baseline", None)
    disk_by_id: Dict[str, Dict[str, Any]] = {}
    for disk_job in disk_jobs:
        if isinstance(disk_job, dict) and disk_job.get("id"):
            disk_by_id[str(disk_job["id"])] = disk_job

    intended_remove = {str(i) for i in (removed_ids or ()) if i}

    # Pass 1: by-id field reconciliation for jobs present on both sides.
    reconciled: List[Dict[str, Any]] = []
    reconciled_ids: List[str] = []
    new_ids: Set[str] = set()
    changed = False
    for job in jobs:
        if not isinstance(job, dict) or not job.get("id"):
            reconciled.append(job)
            continue
        jid = str(job["id"])
        new_ids.add(jid)
        disk_job = disk_by_id.get(jid)
        if disk_job is None or baseline is None:
            reconciled.append(job)
            continue
        merged = _reconcile_job_fields(job, disk_job, baseline.get(jid))
        if merged != job:
            changed = True
            reconciled_ids.append(jid)
        reconciled.append(merged)

    if reconciled_ids:
        logger.warning(
            "Reconciled %d cron job record(s) against a concurrent writer's "
            "on-disk state; fields this writer did not modify kept the "
            "sibling's values instead of being reverted (#80624): %s",
            len(reconciled_ids),
            reconciled_ids,
        )

    # Pass 2: resurrect ids that exist on disk but not in the payload.
    recovered: List[Dict[str, Any]] = []
    for disk_id, disk_job in disk_by_id.items():
        if disk_id in new_ids or disk_id in intended_remove:
            continue
        recovered.append(disk_job)
        new_ids.add(disk_id)

    if recovered:
        logger.warning(
            "Preserved %d cron job(s) present on disk but missing from the "
            "in-memory save payload (concurrent create under degraded lock "
            "or stale writer) (#80624): %s",
            len(recovered),
            [j.get("id") for j in recovered],
        )
        return reconciled + recovered
    if changed:
        return reconciled
    return jobs


def _unlink_quiet(path: Optional[str]) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _stage_jobs_payload(jobs_file: Path, jobs: List[Dict[str, Any]]) -> str:
    """Serialize the store payload to a fsynced temp file next to *jobs_file*; return its path."""
    fd, tmp_path = tempfile.mkstemp(dir=str(jobs_file.parent), suffix=".tmp", prefix=".jobs_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"jobs": jobs, "updated_at": _hermes_now().isoformat()},
                f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        _unlink_quiet(tmp_path)
        raise
    return tmp_path


_SAVE_JOBS_MERGE_ATTEMPTS = 5


def _save_jobs_unlocked(
    jobs: List[Dict[str, Any]], *, removed_ids: Optional[Collection[str]] = None,
    replace: bool = False,
):
    """Save all jobs; caller must hold _jobs_lock(). ``removed_ids`` = intentional deletes;
    ``replace=True`` skips the shrink-merge guard and the corrupt-store refusal (wholesale
    rewrite for tests, disaster recovery and load_jobs' auto-repair of unmergeable shapes)."""
    jobs_file = _current_cron_store().jobs_file
    _guard_against_test_write_to_live_store(jobs_file)
    ensure_dirs()
    # Owner snapshot BEFORE replace so a root writer can hand the file back to the gateway user.
    _stat_before = None
    for probe in (jobs_file, jobs_file.parent):
        with contextlib.suppress(OSError):
            _stat_before = os.stat(probe)
            break

    # Shrink-merge loop: merge, stage, re-peek, repeat; the last attempt writes without a re-peek.
    tmp_path = None
    try:
        for attempt in range(_SAVE_JOBS_MERGE_ATTEMPTS + 1):
            if not replace:
                jobs = _merge_unexpected_disk_jobs(jobs, removed_ids=removed_ids)
            tmp_path = _stage_jobs_payload(jobs_file, jobs)
            # Verify-after-stage: a sibling landing during serialization forces another merge round.
            if (
                not replace
                and attempt < _SAVE_JOBS_MERGE_ATTEMPTS
                and _unmerged_disk_jobs(jobs, removed_ids)
            ):
                _unlink_quiet(tmp_path)
                tmp_path = None
                continue
            # fsync the directory the rename actually landed in (atomic_replace resolves symlinks).
            replaced = Path(atomic_replace(tmp_path, jobs_file))
            tmp_path = None
            fsync_directory(replaced.parent)
            _secure_file(jobs_file)
            _preserve_file_ownership(jobs_file, _stat_before)
            # Invalidate (never refresh) the stamp: a refresh would let a nested save certify disk
            # against an OUTER caller's stale payload. Later saves take the full merge (fail-safe).
            _record_load_stamp(None)
            return
    except BaseException:
        _unlink_quiet(tmp_path)
        raise


def _journal_created(job: Dict[str, Any]) -> None:
    """Journal an intentional create. Never raises into the caller."""
    try:
        from cron.lifecycle_journal import record_created

        record_created(str(job.get("id") or ""), name=job.get("name"))
    except Exception:
        logger.debug("cron lifecycle journal (create) unavailable", exc_info=True)


def _journal_removed(removed_ids: Optional[Collection[str]],
                     reason: Optional[str] = None) -> None:
    """Journal intentional removals. Never raises into the caller.

    Called from ``save_jobs`` rather than from each deletion site, because
    ``removed_ids`` is already the single argument every intentional delete
    must thread through (the shrink-merge guard refuses to drop an id that
    was not declared). Journalling here therefore cannot drift from the
    store: an unjournalled removal is one the store would have resurrected.
    """
    if not removed_ids:
        return
    try:
        from cron.lifecycle_journal import record_removed

        for jid in removed_ids:
            if jid:
                record_removed(str(jid), reason=reason)
    except Exception:
        logger.debug("cron lifecycle journal (remove) unavailable", exc_info=True)


def save_jobs(
    jobs: List[Dict[str, Any]], *, removed_ids: Optional[Collection[str]] = None,
    replace: bool = False,
):
    """Save all jobs to storage.

    See ``_save_jobs_unlocked`` for ``removed_ids`` / ``replace`` semantics
    (shrink-merge guard against concurrent-create clobber, #80624).
    """
    # Fire the test-context guard BEFORE acquiring _jobs_lock() (which calls
    # ensure_dirs() and touches the advisory .jobs.lock). This keeps a blocked
    # leak from leaving ANY filesystem side effect in the target home, not just
    # from skipping the jobs.json write. _save_jobs_unlocked re-checks as the
    # shared chokepoint for the other (non-save_jobs) write paths; the guard is
    # idempotent and cheap so the double-check is harmless.
    _guard_against_test_write_to_live_store(_current_cron_store().jobs_file)
    with _jobs_lock():
        _save_jobs_unlocked(jobs, removed_ids=removed_ids, replace=replace)
        # Journal INSIDE the lock, after the write lands. After, because a
        # removal that raised never happened and must not leave an audit
        # record claiming it did. Inside, because the journal's append order
        # is what the vanished-job guard reads as causality: released first,
        # a concurrent creator holding the same id could interleave its
        # create AFTER our removal, and the guard would then report an
        # intentionally-removed job as vanished.
        _journal_removed(removed_ids)


_MISSING = object()


def _with_job(
    job_id: Any, fn: Callable[[List[Dict[str, Any]], int, Dict[str, Any]], Any], missing: Any = None
) -> Any:
    """Run ``fn(jobs, i, job)`` on the first match under ``_jobs_lock()``; ``fn`` saves. *missing*
    if none."""
    with _jobs_lock():
        jobs = load_jobs()
        for i, job in enumerate(jobs):
            if job.get("id") == job_id:
                return fn(jobs, i, job)
    return missing


def _complete_job_record(job: Dict[str, Any]) -> None:
    """Retire *job* in place as a terminal completion (record kept for `cronjob list`)."""
    job.update(enabled=False, state="completed", next_run_at=None)


def _activate_job_record(job: Dict[str, Any]) -> None:
    """Clear pause markers in place so *job* is runnable again."""
    job.update(enabled=True, state="scheduled", paused_at=None, paused_reason=None)


def _normalize_workdir(workdir: Optional[str]) -> Optional[str]:
    """Workdir -> absolute path, or None when empty. ``~`` expands; relative paths are rejected
    (cron runs detached from any cwd); must be an existing dir now but is deliberately NOT
    re-checked at run time (scheduler falls back with a warning). ValueError when invalid."""
    if workdir is None:
        return None
    raw = str(workdir).strip()
    if not raw:
        return None
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError(
            f"Cron workdir must be an absolute path (got {raw!r}). "
            f"Cron jobs run detached from any shell cwd, so relative paths are ambiguous.")
    resolved = expanded.resolve()
    if not resolved.exists():
        raise ValueError(f"Cron workdir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Cron workdir is not a directory: {resolved}")
    return str(resolved)


def _main_model_pin() -> Tuple[Optional[str], Optional[str]]:
    """``(provider, model)`` the main agent runs on right now (``model.default`` + the provider it
    resolves to), for ``pinned=True`` jobs: the lock is a plain per-job pin, so the scheduler needs
    no second precedence axis. ``(None, None)`` when nothing is configured (the job stays unpinned)."""
    from hermes_cli.config_effective import load_user_config_effective

    cfg_path = get_hermes_home() / "config.yaml"
    cfg = load_user_config_effective(cfg_path) if cfg_path.exists() else {}
    model_cfg = cfg.get("model") or {}
    model = model_cfg.get("default") or model_cfg.get("model") if isinstance(model_cfg, dict) else model_cfg
    model = _normalize_job_optional_text(model)
    if not model:
        return None, None
    provider = None
    with contextlib.suppress(Exception):
        from hermes_cli.runtime_provider import resolve_runtime_provider
        provider = _normalize_job_optional_text(resolve_runtime_provider(requested=None).get("provider"))
    return (provider.lower() if provider else None), model


def _resolve_stored_model_pair(
    model: Optional[str], provider: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a config `model.aliases` key / `provider/model` pair at WRITE time.

    A cron job PINS its model for later runs, so an unresolved alias (e.g.
    ``grok`` from ``model.aliases``) reaches the scheduler's provider at fire
    time, 400s, and the fallback chain silently serves a different provider
    AND model (measured 2026-09-18). Resolving here — the shared normalization
    point for both ``create_job`` and ``update_job`` — also pins the record:
    retargeting the alias later cannot change what an existing job runs.

    Best-effort: anything unresolvable is stored verbatim (previous
    behaviour), and a resolution failure never blocks a job write.
    """
    if not model:
        return model, provider
    try:
        from hermes_cli.model_switch import resolve_model_pair_for_storage

        return resolve_model_pair_for_storage(model, provider)
    except Exception:  # pragma: no cover - a job write must never fail here
        logger.debug("cron model alias resolution failed for %r", model, exc_info=True)
        return model, provider


# Sentinel the ``cronjob`` tool (and config ``cron.default_model``) use to mean
# "pin to the CREATING agent's own model". It is an instruction, never a model
# id: the scheduler has no "auto" handling, so a persisted "auto" fires against
# a model that does not exist. Mirrors ``tools.cronjob_tools._AUTO_MODEL``.
_AUTO_MODEL_SENTINEL = "auto"
_AUTO_PIN_FLAGSHIP_REASON = "auto-pin: inherited creating agent's own elected model"


def _is_auto_model_sentinel(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == _AUTO_MODEL_SENTINEL


def _resolve_auto_model_sentinel(
    model: Any, provider: Any, no_agent: Any
) -> Tuple[Any, Any, bool]:
    """Store-level guarantee: the ``"auto"`` sentinel never reaches jobs.json.

    ONE owner for the class, called by both ``create_job`` and ``update_job``
    so no entry point (tool create/update, CLI, API, direct Python) can persist
    it. Returns ``(model, provider, auto_pinned)``:

    - not the sentinel: inputs unchanged, ``auto_pinned=False``;
    - script-mode (``no_agent``) job: no LLM runs, so there is nothing to pin —
      the sentinel AND the provider that rode along with it drop to None, so a
      later ``no_agent=False`` flip yields a plain unpinned LLM job rather than
      one pinned to the literal model "auto";
    - LLM job: resolved via ``tools.cronjob_tools._resolve_cron_llm_model``
      (the creating agent's published model, or unpinned when none is
      published). ``auto_pinned`` is True when it resolved to a real model.
    """
    if not _is_auto_model_sentinel(model):
        return model, provider, False
    if bool(no_agent):
        return None, None, False
    try:
        from tools.cronjob_tools import _resolve_cron_llm_model

        resolved_model, resolved_provider = _resolve_cron_llm_model(model, provider)
    except Exception:  # pragma: no cover - resolution must never write "auto"
        logger.debug("cron auto-model resolution failed", exc_info=True)
        resolved_model, resolved_provider = None, None
    if not resolved_model or _is_auto_model_sentinel(resolved_model):
        return None, None, False
    return resolved_model, resolved_provider, True


def _revalidate_resolved_provider(before: Any, provider: Any, base_url: Any) -> None:
    """Re-run the tool's base_url/provider exfil guard when store-level model
    resolution (auto sentinel, alias, legacy heal) CHANGED the provider.

    ``cronjob_tools`` validates the pair it was handed; the resolution here can
    rewrite the provider afterwards, and the stored pair was then never checked
    (FleetReview #1032). The scheduler's fire-time backstop still refuses such a
    pair, so the job would only fail on every run -- refuse it at write time.
    """
    before = _normalize_job_optional_text(before)
    after = _normalize_job_optional_text(provider)
    if before == after or not _normalize_job_optional_text(base_url):
        return
    from tools.cronjob_tools import _validate_cron_base_url

    err = _validate_cron_base_url(after, base_url)
    if err:
        raise ValueError(err)


def _normalize_job_optional_text(value: Any, *, strip_trailing_slash: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return (value.strip().rstrip("/") if strip_trailing_slash else value.strip()) or None


def _normalize_base_url(value: Any) -> Optional[str]:
    return _normalize_job_optional_text(value, strip_trailing_slash=True)


def _normalize_str_list(items: Any) -> Optional[List[str]]:
    """Non-blank stripped items of *items*, or None when nothing remains."""
    return [str(j).strip() for j in items if str(j).strip()] or None


def _normalize_context_from(value: Any) -> Optional[List[str]]:
    """Accept a job id or a list of ids; anything else is None."""
    if isinstance(value, str):
        value = [value]
    return _normalize_str_list(value) if isinstance(value, list) else None


def _normalize_failure_deliver(value: Any) -> Optional[str]:
    """failure_deliver shares deliver's value grammar; flatten str/list like the tool layer's
    _normalize_deliver_param for direct create_job callers. Semantic validation happens at
    resolution time via the shared deliver path."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(p).strip() for p in value if str(p).strip()) or None
    return _normalize_job_optional_text(value)


def _normalize_reasoning_effort(value: Any) -> Optional[str]:
    """Spelling-only validation via the shared parser (cron knob never stricter/looser than
    config.yaml); model capability is deliberately NOT checked (model unknowable at create time,
    transports clamp at send time). None for unset, lowercase level, or ValueError."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    from hermes_constants import parse_reasoning_effort

    if parse_reasoning_effort(text) is None:
        raise ValueError(
            f"Invalid reasoning_effort {value!r}. Valid levels: "
            "none, minimal, low, medium, high, xhigh, max, ultra "
            "(empty string clears the override).")
    if text in {"false", "disabled"}:
        return "none"
    return text


# Normalizers for create_job (all fields) / update_job (present fields). Invalid values raise BEFORE
# storing.
_CREATE_FIELD_NORMALIZERS: Dict[str, Callable[[Any], Any]] = {
    "model": _normalize_job_optional_text,
    "provider": _normalize_job_optional_text,
    "base_url": _normalize_base_url,
    "script": _normalize_job_optional_text,
    "monitor_script": _normalize_job_optional_text,
    "monitor_url": _normalize_job_optional_text,
    "enabled_toolsets": lambda v: _normalize_str_list(v) if v else None,
    "workdir": _normalize_workdir,
    "no_agent": bool,
    "context_from": _normalize_context_from,
    "failure_deliver": _normalize_failure_deliver,
    "interpreter": _normalize_job_optional_text,
}
_UPDATE_FIELD_NORMALIZERS: Dict[str, Callable[[Any], Any]] = {
    "workdir": lambda v: None if v in {None, "", False} else _normalize_workdir(v),
    "monitor_script": _normalize_job_optional_text,
    "monitor_url": _normalize_job_optional_text,
    "interpreter": _normalize_job_optional_text,
    "reasoning_effort": _normalize_reasoning_effort,
}


def _validate_job_mode_invariants(
    monitor_script: Optional[str],
    monitor_url: Optional[str],
    no_agent: bool,
    script: Optional[str],
) -> None:
    """Execution-mode invariants shared by create_job and update_job (no bypass via the update
    door)."""
    if monitor_script and monitor_url:
        raise ValueError(
            "monitor_script and monitor_url are mutually exclusive — a job "
            "can only have one monitor source.")
    if (monitor_script or monitor_url) and no_agent:
        raise ValueError(
            "monitor_script/monitor_url cannot be combined with no_agent=True — "
            "the whole point of a monitor job is to suppress or wake the AGENT "
            "based on source changes. Use a plain no_agent script job instead.")
    if no_agent and not script:
        raise ValueError(NO_AGENT_WITHOUT_SCRIPT_ERROR)


def _oneshot_past_grace_error(run_at: Any) -> ValueError:
    return ValueError(
        f"Requested one-shot time {run_at} is more than "
        f"{ONESHOT_GRACE_SECONDS}s in the past and cannot be scheduled.")


def _next_run_or_reject_past_oneshot(
    parsed_schedule: Dict[str, Any], label: str, fallback_run_at: Any, what: str,
) -> Optional[str]:
    """``compute_next_run`` that raises (after a warning log) for a one-shot outside the grace
    window, so a ghost job with ``next_run_at=None`` can never be stored."""
    next_run_at = compute_next_run(parsed_schedule)
    if parsed_schedule.get("kind") == "once" and next_run_at is None:
        run_at = parsed_schedule.get("run_at") or fallback_run_at
        logger.warning(
            "Rejecting one-shot cron job %s'%s': run_at %s is outside the %ss grace window",
            what, label, run_at, ONESHOT_GRACE_SECONDS)
        raise _oneshot_past_grace_error(run_at)
    return next_run_at


def create_job(
    prompt: Optional[str],
    schedule: str,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    origin: Optional[Dict[str, Any]] = None,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    allow_flagship_reason: Optional[str] = None,
    base_url: Optional[str] = None,
    script: Optional[str] = None,
    context_from: Optional[Union[str, List[str]]] = None,
    enabled_toolsets: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    no_agent: bool = False,
    attach_to_session: Optional[bool] = None,
    monitor_script: Optional[str] = None,
    monitor_url: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    failure_deliver: Optional[str] = None,
    paused: bool = False,
    paused_reason: Optional[str] = None,
    pinned: bool = False,
    interpreter: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new cron job and return the stored record.

    deliver defaults to "origin" when ``origin`` is given, else "local"; repeat None = forever.
    script: stdout is injected as prompt context, or with ``no_agent=True`` IS the job (stdout
    delivered verbatim, requires ``script``). context_from: job id(s) whose latest output is
    injected. workdir: absolute cwd for tools/scripts. monitor_script/monitor_url: cheap monitor
    source run FIRST each tick; unchanged output suppresses the agent run (mutually exclusive,
    incompatible with ``no_agent``). reasoning_effort: per-job pin; capability NOT validated.
    interpreter: absolute/``~`` Python for ``.py`` script/monitor_script, validated at run time
    (a venv can be rebuilt or moved after creation)."""
    if not isinstance(paused, bool):
        raise ValueError("paused must be a boolean.")
    if paused_reason is not None and not isinstance(paused_reason, str):
        raise ValueError("paused_reason must be a string.")
    if paused_reason is not None and not paused:
        raise ValueError("paused_reason requires paused=True.")
    parsed_schedule = parse_schedule(schedule)
    # Normalize repeat: treat 0 or negative values as None (infinite). String forms
    # ('forever'/'once'/numeric) coerce via normalize_repeat_value — the shared chokepoint with update paths
    # (#66824/#64520/#7142/#71987/#95706).
    repeat = normalize_repeat_value(repeat)
    if parsed_schedule["kind"] == "once" and repeat is None:
        repeat = 1
    if deliver is None:
        deliver = "origin" if origin else "local"
    job_id = uuid.uuid4().hex[:12]
    now = _hermes_now().isoformat()

    raw = locals()
    f = {key: norm(raw[key]) for key, norm in _CREATE_FIELD_NORMALIZERS.items()}
    normalized_skills = _normalize_skill_list(skill, skills)
    # Fork (t_2f1ca8d4 / #720 / #922 / #1032): store-level model resolution on the normalized pair —
    # the "auto" sentinel never reaches jobs.json, `model.aliases` are pinned at WRITE time, the
    # flagship policy is applied, and a provider rewritten by resolution is re-validated against
    # base_url.
    f["model"], f["provider"], _auto_pinned = _resolve_auto_model_sentinel(
        f["model"], f["provider"], f["no_agent"]
    )
    if _auto_pinned and not str(allow_flagship_reason or "").strip():
        # An auto pin inherits the creating agent's elected primary; it is
        # not a caller-chosen flagship route (same rule as the tool path).
        allow_flagship_reason = _AUTO_PIN_FLAGSHIP_REASON
    f["model"], f["provider"] = _resolve_stored_model_pair(f["model"], f["provider"])
    from hermes_cli.model_policy import validate_worker_model

    flagship_reason = validate_worker_model(
        f["model"], allow_flagship_reason=allow_flagship_reason
    )
    _revalidate_resolved_provider(provider, f["provider"], f["base_url"])
    normalized_attach = attach_to_session if isinstance(attach_to_session, bool) else None
    normalized_reasoning_effort = _normalize_reasoning_effort(reasoning_effort)

    _validate_job_mode_invariants(f["monitor_script"], f["monitor_url"], f["no_agent"], f["script"])
    prompt_text = _coerce_job_text(prompt).strip()
    if not prompt_text and not f["script"] and not normalized_skills:
        raise ValueError(EMPTY_PAYLOAD_ERROR)
    # Reject gateway-lifecycle commands (respawn loops) here, not just in the CLI: covers the tool.
    from cron.lifecycle_guard import check_gateway_lifecycle
    check_gateway_lifecycle(prompt_text, f["script"])

    label_source = (
        prompt_text
        or (normalized_skills[0] if normalized_skills else None)
        or (f["script"] if f["no_agent"] else None)
        or "cron job"
    )
    name = name or label_source[:50].strip()
    if pinned and not f["model"]:
        f["provider"], f["model"] = _main_model_pin()
    next_run_at = _next_run_or_reject_past_oneshot(parsed_schedule, name, schedule, "")

    job = {
        "id": job_id,
        "name": name,
        "prompt": prompt_text,
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "model": f["model"],
        "provider": f["provider"],
        "base_url": f["base_url"],
        "script": f["script"],
        "no_agent": f["no_agent"],
        "monitor_script": f["monitor_script"],
        "monitor_url": f["monitor_url"],
        "monitor_state": None,
        "context_from": f["context_from"],
        "schedule": parsed_schedule,
        "schedule_display": parsed_schedule.get("display", schedule),
        "repeat": {"times": repeat, "completed": 0},  # times None = forever
        "enabled": not paused,
        "state": "paused" if paused else "scheduled",
        "paused_at": now if paused else None,
        "paused_reason": ((paused_reason or "").strip() or "Created paused; awaiting operator approval.") if paused else None,
        "created_at": now,
        "next_run_at": None if paused else next_run_at,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        # Targets acked without message_id/raw_response (accepted but UNVERIFIED).
        "last_delivery_unverified": None,
        "failure_streak": 0,
        "deliver": deliver,
        "origin": origin,  # Tracks where job was created for "origin" delivery
        "enabled_toolsets": f["enabled_toolsets"],
        "workdir": f["workdir"],
    }
    # Optional keys are persisted only when explicitly set: an absent key falls back to global
    # config (attach/reasoning) or to ``deliver`` (failure_deliver), byte-identical to pre-feature
    # jobs.
    for key, value in (
        ("attach_to_session", normalized_attach), ("reasoning_effort", normalized_reasoning_effort),
        ("failure_deliver", f["failure_deliver"]), ("interpreter", f["interpreter"]),
    ):
        if value is not None:
            job[key] = value

    # Only persist reasoning_effort when explicitly set, so existing jobs stay
    # byte-identical (absent key => the scheduler falls back to config.yaml
    # agent.reasoning_effort, the pre-existing behaviour).
    if normalized_reasoning_effort is not None:
        job["reasoning_effort"] = normalized_reasoning_effort
    if flagship_reason:
        job["allow_flagship_reason"] = flagship_reason

    with _jobs_lock():
        save_jobs(load_jobs() + [job])
        # Journal INSIDE the same lock the store write took — see the note in
        # save_jobs. Journalled after release, a remover of this same id can
        # append its removal FIRST, inverting the append order the guard reads
        # as causality and turning an intentional removal into a false alarm.
        _journal_created(job)

    if flagship_reason:
        logger.info("flagship override: cron create id=%s model=%s reason=%s", job_id, f["model"], flagship_reason)
    return job


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Get a job by ID."""
    job = next((j for j in load_jobs() if j["id"] == job_id), None)
    return _normalize_job_record(job) if job is not None else None


class AmbiguousJobReference(LookupError):
    """Raised when a job name matches more than one job."""

    def __init__(self, ref: str, matches: List[Dict[str, Any]]):
        self.ref = ref
        self.matches = matches
        ids = ", ".join(m["id"] for m in matches)
        super().__init__(
            f"Job name '{ref}' is ambiguous — matches {len(matches)} jobs: {ids}. "
            f"Use the job ID instead.")


def resolve_job_ref(ref: str) -> Optional[Dict[str, Any]]:
    """Resolve an ID or name to a job record. Exact ID wins, then case-insensitive name; an
    ambiguous name raises AmbiguousJobReference rather than silently picking one."""
    if not ref:
        return None
    jobs = load_jobs()
    by_id = next((j for j in jobs if j["id"] == ref), None)
    if by_id is not None:
        return _normalize_job_record(by_id)
    ref_lower = ref.lower()
    name_matches = [j for j in jobs if (j.get("name") or "").lower() == ref_lower]
    if not name_matches:
        return None
    if len(name_matches) > 1:
        raise AmbiguousJobReference(ref, [_normalize_job_record(j) for j in name_matches])
    return _normalize_job_record(name_matches[0])


def list_jobs(include_disabled: bool = False) -> List[Dict[str, Any]]:
    """List all jobs, optionally including disabled ones."""
    jobs = [_normalize_job_record(j) for j in load_jobs()]
    if not include_disabled:
        jobs = [j for j in jobs if j.get("enabled", True)]
    try:
        from cron.executions import latest_executions

        latest = latest_executions([job.get("id", "") for job in jobs])
    except Exception:
        latest = {}
    for job in jobs:
        job["latest_execution"] = latest.get(job.get("id", ""))
    return jobs


def _pin_fallback_reasoning_effort(chain: Any, effort: str) -> Any:
    """Copy of a job ``fallback`` chain with every EXPLICIT per-entry
    ``reasoning_effort`` set to ``effort``; ``None`` when nothing changes.

    Accepts the list form and the single-dict form the scheduler accepts
    (``cron/scheduler.py`` job-chain resolution). Entries with no effort are
    left as-is: they already inherit the job pin at fallback time.
    """
    single = isinstance(chain, dict)
    entries = [chain] if single else chain
    if not isinstance(entries, list):
        return None
    out, changed = [], False
    for entry in entries:
        if (
            isinstance(entry, dict)
            and str(entry.get("reasoning_effort") or "").strip()
            and entry.get("reasoning_effort") != effort
        ):
            entry = {**entry, "reasoning_effort": effort}
            changed = True
        out.append(entry)
    if not changed:
        return None
    return out[0] if single else out


def _reject_terminal_activation(job: Dict[str, Any], updated: Dict[str, Any], job_id: str) -> None:
    """A genuinely terminal job cannot be reactivated through update_job (use cron resume)."""
    if (
        is_terminal_job(job)
        and not _is_recoverable_error_job(job)
        and (
            updated.get("state") not in {"completed", "error"}
            or updated.get("enabled") is True
            or updated.get("next_run_at") is not None
        )
    ):
        raise ValueError(
            f"Cannot activate terminal cron job '{job.get('name', job_id)}' "
            "through update_job; use cron resume --run-now or --at.")


def _apply_pin_update(job: Dict[str, Any], updates: Dict[str, Any]) -> None:
    """``pinned`` is not stored; it rewrites the per-job pin. ``True`` with no explicit model locks
    the main agent's current provider+model onto the job, ``False`` releases both so the job follows
    the main model again (an explicit ``model`` in the same update wins over either)."""
    if "pinned" not in updates:
        return
    pinned = bool(updates.pop("pinned"))
    if "model" in updates:
        return
    if pinned:
        if not _normalize_job_optional_text(job.get("model")):
            updates["provider"], updates["model"] = _main_model_pin()
    else:
        updates["provider"], updates["model"] = None, None


def _normalize_job_updates(job: Dict[str, Any], updates: Dict[str, Any]) -> None:
    """Normalize updates in place like create_job; invalid values raise BEFORE the merge. ``repeat``
    accepts the stored dict or a bare value (coerced, completed counter preserved)."""
    for key, norm in _UPDATE_FIELD_NORMALIZERS.items():
        if key in updates:
            updates[key] = norm(updates[key])
    if "repeat" in updates:
        _rp = updates["repeat"]
        completed = (job.get("repeat") or {}).get("completed", 0)
        if isinstance(_rp, dict):
            _rp = dict(_rp)
            _rp["times"] = normalize_repeat_value(_rp.get("times"))
            _rp.setdefault("completed", completed)
            updates["repeat"] = _rp
        else:
            updates["repeat"] = {"times": normalize_repeat_value(_rp), "completed": completed}


def _rederive_repeat_for_schedule_change(
    job: Dict[str, Any], updates: Dict[str, Any]
) -> None:
    """Re-derive the ``repeat`` default when a schedule update flips the kind.

    ``create_job`` derives it from the schedule kind (once -> 1, recurring -> forever); the update
    path must honour the same contract, otherwise a one-shot turned recurring keeps its ``times=1``
    budget and retires after one fire, while a recurring job turned one-shot never completes. An
    explicit ``repeat`` in the same update wins; a same-kind schedule edit leaves ``repeat`` alone.
    """
    if "schedule" not in updates or "repeat" in updates:
        return
    new_schedule = updates["schedule"]
    if isinstance(new_schedule, str):
        new_schedule = parse_schedule(new_schedule)
        updates["schedule"] = new_schedule
    old_kind = (job.get("schedule") or {}).get("kind")
    new_kind = new_schedule.get("kind")
    if old_kind == new_kind:
        return
    repeat = dict(job.get("repeat") or {})
    times = repeat.get("times")
    if new_kind == "once" and times is None:
        repeat["times"] = 1
    elif new_kind != "once" and old_kind == "once" and times == 1:
        repeat["times"] = None
    else:
        return
    repeat.setdefault("completed", 0)
    updates["repeat"] = repeat


def _apply_schedule_update(updated: Dict[str, Any], updates: Dict[str, Any], job_id: str) -> None:
    """Parse a string schedule, refresh ``schedule_display`` and (unless paused) ``next_run_at``."""
    updated_schedule = updated["schedule"]
    if isinstance(updated_schedule, str):
        updated_schedule = parse_schedule(updated_schedule)
        updated["schedule"] = updated_schedule
    updated["schedule_display"] = updates.get(
        "schedule_display", updated_schedule.get("display", updated.get("schedule_display")))
    if updated.get("state") != "paused":
        updated["next_run_at"] = _next_run_or_reject_past_oneshot(
            updated_schedule, updated.get("name", job_id), updated_schedule, "update ")


def _fill_missing_next_run(updated: Dict[str, Any]) -> None:
    """An enabled, unpaused record must never persist without ``next_run_at`` (it would never fire).
    """
    if (
        not updated.get("enabled", True)
        or updated.get("state") == "paused"
        or updated.get("next_run_at")
    ):
        return
    next_run = compute_next_run(updated["schedule"])
    if next_run is None and updated["schedule"].get("kind") == "once":
        run_at = updated["schedule"].get("run_at", "unknown")
        raise ValueError(
            f"Requested one-shot time {run_at} is in the past "
            f"(grace window: {ONESHOT_GRACE_SECONDS}s) and cannot be scheduled.")
    updated["next_run_at"] = next_run


def update_job(job_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update a job by ID, refreshing derived schedule fields when needed."""
    # ``id`` is a path component under OUTPUT_DIR — changing it would leak path-escape values.
    bad_fields = _IMMUTABLE_JOB_FIELDS.intersection(updates or {})
    if bad_fields:
        raise ValueError(f"Cron job field(s) cannot be updated: {', '.join(sorted(bad_fields))}")

    def apply(jobs, i, job):
        _rederive_repeat_for_schedule_change(job, updates)
        _normalize_job_updates(job, updates)
        if updates.get("reasoning_effort") is not None and "fallback" not in updates:
            # The pin must govern the job's OWN fallback chain too: an entry with an
            # explicit ``reasoning_effort`` overrides the pin on the fallback turn
            # (chat_completion_helpers #21256), so `cron edit --reasoning-effort low`
            # used to leave xhigh fallbacks running xhigh (t_ef1ba08b). Setting a pin
            # rewrites explicit entry values; entries without one already inherit.
            # Clearing (None) leaves entries alone; the CLI reports them.
            _fb = _pin_fallback_reasoning_effort(job.get("fallback"), updates["reasoning_effort"])
            if _fb is not None:
                updates["fallback"] = _fb
        _apply_pin_update(job, updates)

        # Resolve a config `model.aliases` key / `provider/model` pair the same way
        # create_job does, so `cron edit --model grok` cannot re-introduce a raw alias
        # the create path just eliminated. The explicit-provider slot takes a provider
        # THIS update asserts, never the stored one (the PREVIOUS model's provider);
        # the resolved provider is written unconditionally when the model changes at
        # all, so a new model never inherits the old model's backend. No "only if the
        # model string changed" narrowing: a self-named alias resolves the PROVIDER
        # while leaving the model byte-identical. `--model ""` clears the pin and is
        # untouched here. The "auto" sentinel is resolved (LLM job) or dropped (script
        # job) against the EFFECTIVE no_agent of this update — never stored.
        _pre_resolution_provider = (
            updates["provider"] if "provider" in updates else job.get("provider")
        )
        if "model" in updates and _is_auto_model_sentinel(updates["model"]):
            _eff_no_agent = updates["no_agent"] if "no_agent" in updates else job.get("no_agent")
            _am, _ap, _pinned = _resolve_auto_model_sentinel(
                updates["model"], updates.get("provider"), _eff_no_agent
            )
            updates["model"] = _am
            updates["provider"] = _ap
            if _pinned and not str(updates.get("allow_flagship_reason") or "").strip():
                updates["allow_flagship_reason"] = _AUTO_PIN_FLAGSHIP_REASON
        if "model" in updates and isinstance(updates["model"], str):
            _raw_model = updates["model"].strip()
            if _raw_model:
                _m, _p = _resolve_stored_model_pair(
                    _raw_model,
                    _normalize_job_optional_text(updates.get("provider")),
                )
                updates["model"] = _m
                updates["provider"] = _p
                from hermes_cli.model_policy import validate_worker_model

                updates["allow_flagship_reason"] = validate_worker_model(
                    _m, allow_flagship_reason=updates.get("allow_flagship_reason")
                )

        updated = _apply_skill_fields({**job, **updates})
        # A legacy row persisted before this guard may still hold "auto"; any update of
        # it heals the MERGED record so the write cannot carry the sentinel forward
        # (e.g. a no_agent=False flip).
        if _is_auto_model_sentinel(updated.get("model")):
            _am, _ap, _pinned = _resolve_auto_model_sentinel(
                updated.get("model"), updated.get("provider"), updated.get("no_agent")
            )
            _am, _ap = _resolve_stored_model_pair(_am, _ap)
            from hermes_cli.model_policy import validate_worker_model

            _reason = validate_worker_model(
                _am,
                allow_flagship_reason=(
                    updated.get("allow_flagship_reason")
                    or (_AUTO_PIN_FLAGSHIP_REASON if _pinned else None)
                ),
            )
            updated["model"] = _am
            updated["provider"] = _ap
            if _reason:
                updated["allow_flagship_reason"] = _reason
        _revalidate_resolved_provider(
            _pre_resolution_provider, updated.get("provider"), updated.get("base_url")
        )
        if "allow_flagship_reason" in updates and "model" not in updates:
            from hermes_cli.model_policy import validate_worker_model

            validate_worker_model(
                updated.get("model"),
                allow_flagship_reason=updated.get("allow_flagship_reason"),
            )

        _reject_terminal_activation(job, updated, job_id)
        # Re-check on the MERGED record; scoped to changed fields so legacy records keep loading.
        if {"monitor_script", "monitor_url", "no_agent", "script"}.intersection(updates):
            _validate_job_mode_invariants(
                updated.get("monitor_script") or None,
                updated.get("monitor_url") or None,
                bool(updated.get("no_agent")),
                _normalize_job_optional_text(updated.get("script")))
        if any(k in updates for k in _PAYLOAD_FIELDS) and job_payload_is_empty(updated):
            raise ValueError(EMPTY_PAYLOAD_ERROR)
        if "schedule" in updates:
            _apply_schedule_update(updated, updates, job_id)
            # next_run_at now follows the new schedule; a stale quota_hold_until would only shield
            # the record from the stale-error re-arm while no longer describing where it is
            # parked. The next fire re-parks (with a fresh notice) if the window is still closed.
            from cron.quota_hold import clear_state as _clear_quota_hold
            _clear_quota_hold(updated)
        if {"schedule", "next_run_at", "enabled", "state"}.intersection(updates):
            # An explicit schedule/lifecycle rewrite supersedes any occurrence the dispatcher
            # left unclaimed — pause/resume/edit must not resurrect a slot from before the edit.
            updated.pop("pending_slot", None)
        _fill_missing_next_run(updated)
        _reject_terminal_activation(job, updated, job_id)
        jobs[i] = updated
        save_jobs(jobs)
        if updates.get("allow_flagship_reason"):
            logger.info("flagship override: cron update id=%s model=%s reason=%s", job_id, updated.get("model"), updates["allow_flagship_reason"])
        return _normalize_job_record(updated)

    return _with_job(job_id, apply)


def pause_job(job_id: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Pause a job without deleting it. Accepts a job ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return None
    # Never write a silent disable: enabled=false with no reason reads as "forgotten" to every
    # audit, and a caller who paused "until X lands" has nothing left to remind it (t_6b72ef98).
    reason = (reason or "").strip() or (
        f"Paused with no reason given; resume with `hermes cron resume {job['id']}`."
    )
    return update_job(
        job["id"],
        {
            "enabled": False,
            "state": "paused",
            "paused_at": _hermes_now().isoformat(),
            "paused_reason": reason,
        },
    )


def resume_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Resume a paused job. Accepts a job ID or name.

    A recurring job paused across one of its slots must not lose that slot silently: the stored
    ``next_run_at`` (already past) survives resume as the due instant, and the ordinary late /
    catch-up / ``cron.catch_up_missed`` policy in the due scan decides what happens to it — one
    fire or a logged skip, never a silent re-anchor past it (#113603). One-shots and future
    instants recompute from now as before.
    """
    job = resolve_job_ref(job_id)
    if not job:
        return None
    stored_next = job.get("next_run_at")
    stored_dt = _parse_aware(stored_next) if stored_next else None
    if (
        job["schedule"].get("kind") in {"cron", "interval"}
        and stored_dt is not None
        and _instant_at_or_before(stored_dt, _hermes_now())
    ):
        next_run_at = stored_next
        logger.info(
            "Job '%s' resumed with occurrence %s that elapsed while paused kept due; the next "
            "tick fires it (late/catch-up) or logs the skip.",
            job.get("name", job["id"]), stored_next)
    else:
        next_run_at = compute_next_run(job["schedule"])
    if next_run_at is None and job["schedule"].get("kind") == "once":
        run_at = job["schedule"].get("run_at", "unknown")
        raise ValueError(
            f"Cannot resume: one-shot time {run_at} is in the past "
            f"(grace window: {ONESHOT_GRACE_SECONDS}s) and will never fire.")
    return update_job(job["id"], {
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "next_run_at": next_run_at,
    })


def trigger_job(job_id: str, extra_prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Schedule a job for the next tick (ID or name). ``extra_prompt`` is stamped as
    ``manual_run_prompt`` for that single fire only; ``mark_job_run`` clears it."""
    job = resolve_job_ref(job_id)
    if not job:
        return None
    if is_terminal_job(job):
        name = job.get("name", job_id)
        raise ValueError(
            f"Cannot run: job '{name}' is {job.get('state')} (terminal). "
            f"Create a new occurrence with 'hermes cron resume {name} "
            "--run-now' or '--at <ISO-8601>'.")
    manual_run_at = _hermes_now().isoformat()
    return update_job(job["id"], {
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "next_run_at": manual_run_at,
        # Run-now intent, so cron expression/TZ repair guards don't treat it as stale state.
        "manual_run_at": manual_run_at,
        "manual_run_prompt": (extra_prompt or None),
    })


def _claim_owner_is_dead(claim: Dict[str, Any]) -> bool:
    """True when the claim's ``by`` names a process on THIS host that provably no longer exists.
    ``_machine_id()`` stamps ``host:pid[:token]``; a foreign host, an explicit HERMES_MACHINE_ID,
    or any liveness-probe failure returns False (fail safe: only a proven death shortens the TTL)."""
    parts = str(claim.get("by") or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return False
    try:
        import socket
        if parts[0] != socket.gethostname():
            return False
        from gateway.status import _pid_exists
        return not _pid_exists(int(parts[1]))
    except Exception:
        return False


# Restart re-queue (t_1f4598ad). A gateway restart whose drain outlives the
# shutdown budget SIGTERMs in-flight cron scripts (terminate_running_scripts);
# the run is recorded as ``Script exited with code -15`` and, for a daily job,
# stays last_status=error for a whole day. The dying process stamps this
# marker AFTER its own completion bookkeeping; the next scheduler to scan the
# job (the restarted gateway) converts it into a single run-now fire.
# mark_job_run never touches the marker, so the interrupted run's own
# last_status/next_run_at write cannot erase it.
RESTART_REQUEUE_KEY = "restart_requeue"
RESTART_REQUEUE_COUNT_KEY = "restart_requeue_count"
# Re-fires allowed per streak without a successful run: a re-fire that is
# itself killed by another restart is NOT re-queued again.
RESTART_REQUEUE_MAX = 1
# A marker older than this is dropped instead of fired (job paused across the
# restart, gateway down for a day): same window as the one-shot restart
# catch-up default.
RESTART_REQUEUE_MAX_AGE_SECONDS = DEFAULT_ONESHOT_CATCHUP_SECONDS
# One restart kills every in-flight script at once, so the next boot scan
# consumes all their markers together. Firing them all in the same second
# stacks the heavy ones (2026-09-30 09:28:43: fleet-config-lint +
# fleet-upstream-leak-lint re-fired 71 ms apart -> Studio load1=67). The n-th
# re-fire in one scan is deferred n * this many seconds; the first fires now.
RESTART_REQUEUE_STAGGER_S = 300


def request_restart_requeue(job_id: str, reason: str) -> bool:
    """Persist a one-shot re-fire request for a run killed by a gateway restart.

    Returns True when the marker was written. Refuses (False) for unknown or
    non-recurring jobs, and once ``RESTART_REQUEUE_MAX`` re-fires have been
    spent without an intervening successful run.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            schedule = job.get("schedule")
            kind = schedule.get("kind") if isinstance(schedule, dict) else None
            if kind not in {"cron", "interval"}:
                return False
            count = int(job.get(RESTART_REQUEUE_COUNT_KEY) or 0)
            if count >= RESTART_REQUEUE_MAX:
                logger.warning(
                    "cron.restart_requeue.refused job='%s' id=%s — already "
                    "re-queued %d time(s) without a successful run",
                    job.get("name", job_id), job_id, count,
                )
                return False
            job[RESTART_REQUEUE_COUNT_KEY] = count + 1
            job[RESTART_REQUEUE_KEY] = {
                "at": _hermes_now().isoformat(),
                "reason": str(reason or "")[:500],
            }
            save_jobs(jobs)
            return True
    return False


def _apply_restart_requeue(
    job: Dict[str, Any],
    raw_jobs: List[Dict[str, Any]],
    now: datetime,
    fired_this_scan: Optional[List[Any]] = None,
) -> bool:
    """Turn a pending restart re-queue marker into a run-now fire.

    Mutates ``job`` (scan copy) and its ``raw_jobs`` record in place. Uses the
    same ``next_run_at == manual_run_at`` shape as ``trigger_job`` so the
    cron-expression / TZ / stale-grace guards treat it as an explicit run-now.
    ``fired_this_scan`` is the due scan's list of re-fires so far: the n-th
    one is stamped ``now + n * RESTART_REQUEUE_STAGGER_S`` instead of now.
    Returns True when storage needs saving.
    """
    marker = job.get(RESTART_REQUEUE_KEY)
    if not marker:
        return False
    raw = next((rj for rj in raw_jobs if rj.get("id") == job.get("id")), None)
    job.pop(RESTART_REQUEUE_KEY, None)
    if raw is not None:
        raw.pop(RESTART_REQUEUE_KEY, None)
    try:
        stamped = _ensure_aware(datetime.fromisoformat(str(marker.get("at"))))
        age = (now - stamped).total_seconds()
    except (AttributeError, TypeError, ValueError):
        age = None
    name = job.get("name", job.get("id", "?"))
    if age is None or age > RESTART_REQUEUE_MAX_AGE_SECONDS:
        logger.warning(
            "cron.restart_requeue.expired job='%s' id=%s — dropping re-queue "
            "marker (age=%s)", name, job.get("id"), age,
        )
        return True
    slot = 0
    if fired_this_scan is not None:
        slot = len(fired_this_scan)
        fired_this_scan.append(job.get("id"))
    delay_s = slot * RESTART_REQUEUE_STAGGER_S
    stamp = (now + timedelta(seconds=delay_s)).isoformat()
    for record in (job, raw):
        if record is not None:
            record["next_run_at"] = stamp
            record["manual_run_at"] = stamp
    logger.warning(
        "cron.restart_requeue.fire job='%s' id=%s — previous run was killed by "
        "a gateway restart (%s); firing once at %s (stagger %ds, slot %d)",
        name, job.get("id"), str(marker.get("reason") or "")[:200],
        stamp, delay_s, slot,
    )
    return True


def _claim_is_live(claim: Any, now: datetime, ttl_seconds: float) -> bool:
    """True for a well-formed claim aged within ``[0, ttl)`` whose owner is not provably dead:
    future-dated (clock/TZ skew) or malformed claims count as stale so they can never wedge a
    job, and a same-host owner pid that has exited releases the claim immediately instead of
    after the TTL (a killed ``hermes cron run`` otherwise blocks the next manual run for the
    full window with "already being fired")."""
    if not isinstance(claim, dict) or not claim.get("at"):
        return False
    claimed_at = _parse_aware(claim["at"])
    if claimed_at is None or not (0 <= _elapsed_seconds(now, claimed_at) < ttl_seconds):
        return False
    return not _claim_owner_is_dead(claim)


_REARM_RECURRING_ERROR = (
    "Cannot re-arm recurring jobs: re-arm is one-shot-only; use plain resume or cron run."
)


def rearm_oneshot(job_id: str, run_at: Any) -> Optional[Dict[str, Any]]:
    """Re-arm a completed one-shot as an explicit new occurrence."""
    job_ref = resolve_job_ref(job_id)
    if not job_ref:
        return None
    if isinstance(run_at, datetime):
        run_at = run_at.isoformat()
    parsed_schedule = parse_schedule(str(run_at))
    if parsed_schedule.get("kind") != "once":
        raise ValueError(_REARM_RECURRING_ERROR)
    next_run_at = compute_next_run(parsed_schedule)
    if next_run_at is None:
        raise _oneshot_past_grace_error(parsed_schedule.get("run_at") or run_at)

    def apply(jobs, _i, job):
        now = _hermes_now()
        if _claim_is_live(job.get("run_claim"), now, _oneshot_run_claim_ttl_seconds()):
            raise ValueError("Cannot re-arm one-shot over a live run claim.")
        if _claim_is_live(job.get("fire_claim"), now, FIRE_CLAIM_TTL_SECONDS):
            raise ValueError("Cannot re-arm one-shot over a live fire claim.")
        if job.get("schedule", {}).get("kind") != "once":
            raise ValueError(_REARM_RECURRING_ERROR)
        repeat = job.get("repeat") or {}
        repeat["completed"] = 0
        job.update(
            schedule=parsed_schedule, schedule_display=parsed_schedule.get("display", str(run_at)),
            repeat=repeat, run_claim=None, fire_claim=None)
        _activate_job_record(job)
        job["next_run_at"] = next_run_at
        save_jobs(jobs)
        return _normalize_job_record(job)

    return _with_job(job_ref["id"], apply)


def remove_job(job_id: str) -> bool:
    """Remove a job by ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return False
    canonical_id = job["id"]
    with _jobs_lock():
        jobs = load_jobs()
        original_len = len(jobs)
        jobs = [j for j in jobs if j["id"] != canonical_id]
        if len(jobs) == original_len:
            return False
        # Resolve BEFORE saving so a legacy unsafe ID fails closed without a half-applied removal.
        job_output_dir = _job_output_dir(canonical_id)
        save_jobs(jobs, removed_ids={canonical_id})
        marker = _self_removal_delivery.get()
        if marker is not None and marker.job_id == canonical_id:
            marker.removed = True
        if job_output_dir.exists():
            shutil.rmtree(job_output_dir)
        try:
            from cron.notepad import clear_notepad
            clear_notepad(canonical_id)
        except Exception:
            logger.debug("Failed to clear notepad for removed job %s", canonical_id, exc_info=True)
        # Prune the fire-fence lock entry so the registry doesn't grow monotonically.
        _fence_key = f"{_current_cron_store().cron_dir.resolve()}::{canonical_id}"
        with _fire_fence_locks_guard:
            _fire_fence_locks.pop(_fence_key, None)
        return True


def _set_alert_flag(job_id: str, field: str, value: bool) -> bool:
    """Set/clear a persisted alert-dedup marker (alert exactly once until the condition heals;
    survives restarts) and return the PRIOR value. Field: ``preflight_alerted`` (blocked config).

    The marker records that the operator was already alerted about this job's condition, so the scheduler
    alerts exactly once and stays silent on subsequent ticks until the condition heals (same alert-once
    shape as the dead-pin auto-pause in #73506).
    """
    def apply(jobs, _i, job):
        prior = bool(job.get(field))
        if value:
            job[field] = True
        else:
            job.pop(field, None)
        if prior != value:
            save_jobs(jobs)
        return prior

    return _with_job(job_id, apply, False)


def mark_preflight_alerted(job_id: str) -> bool:
    """Mark the job as preflight-alerted; return True if it already was."""
    return _set_alert_flag(job_id, "preflight_alerted", True)


def clear_preflight_alerted(job_id: str) -> None:
    """Clear the preflight alert-dedup marker (config validates again)."""
    _set_alert_flag(job_id, "preflight_alerted", False)


def note_fire_forward_failure(job_id: str, detail: str) -> bool:
    """Durably record (as ``last_fire_error``) that a scheduled fire could not be handed to the
    runner — written by the dashboard fire webhook when the loopback forward fails. Without it
    the miss is invisible (no execution row, last_status only covers started runs); mark_job_run
    clears it."""
    def apply(jobs, _i, job):
        job["last_fire_error"] = {
            "at": _hermes_now().isoformat(), "detail": str(detail or "")[:500]}
        save_jobs(jobs)
        return True

    return _with_job(job_id, apply, False)


def _record_run_outcome(
    job: Dict[str, Any], success: bool, error: Optional[str], delivery_error: Optional[str],
    status: Optional[str], now: str,
) -> None:
    """Stamp one completed run onto *job*: status fields, failure streak, alert markers, claims."""
    job["last_run_at"] = now
    job.pop("manual_run_at", None)
    # The transient manual-run context is single-fire: the run that just completed consumed it.
    job.pop("manual_run_prompt", None)
    delivery_failed = isinstance(delivery_error, str) and bool(delivery_error.strip())
    # Consecutive failures carrying the IDENTICAL error string — read (before this write) by
    # the scheduler's repeated-error page gate, which pages a stuck no_agent job once instead
    # of per tick (t_04822736). Any success or changed error resets.
    if success or not error:
        job["error_repeat_streak"] = 0
    elif job.get("last_status") == "error" and job.get("last_error") == error:
        job["error_repeat_streak"] = int(job.get("error_repeat_streak") or 0) + 1
    else:
        job["error_repeat_streak"] = 1
    job["last_status"] = status or (
        "error" if not success else ("delivery_failed" if delivery_failed else "ok"))
    job["last_error"] = None if success else error
    if success:
        # Healthy run: drop the alert-once dedup markers so a FUTURE break re-alerts, and clear
        # the forward-failure stamp so it only describes CURRENT auto-fire health.
        job.pop("preflight_alerted", None)
        job.pop("last_fire_error", None)
        # A clean run closes the restart re-queue streak (fork, #1301/#1548).
        job.pop(RESTART_REQUEUE_COUNT_KEY, None)
        job["failure_streak"] = 0
    else:
        # Consecutive agent-failure streak; delivery failures do NOT count
        # (scheduler._failure_streak_nudge).
        job["failure_streak"] = int(job.get("failure_streak") or 0) + 1
    job["last_delivery_error"] = delivery_error
    # Clear both claims: the run is over, so the job is claimable again.
    job["fire_claim"] = None
    job.pop("pending_slot", None)
    if job.get("run_claim") is not None:  # keep key absence for legacy records
        job["run_claim"] = None


def _advance_after_run(job: Dict[str, Any], now: str) -> None:
    """Bump ``repeat.completed`` and recompute ``next_run_at``; retire the record as a terminal
    completion when the repeat limit is reached or a one-shot has no further run."""
    # If no next run, decide whether this is terminal completion (one-shot) or a transient failure
    # (recurring schedule couldn't compute — e.g. 'croniter' missing from the runtime env). Recurring jobs
    # must NEVER be silently disabled: that turns a missing runtime dep into "job completed" and the user's
    # schedule quietly goes off. See issue #16265.
    kind = job.get("schedule", {}).get("kind")
    # One-shot dispatch-limit guard (issue #38758): a finite one-shot claimed via claim_dispatch() but whose
    # tick died before mark_job_run could remove it will have completed >= times while still looking due
    # (last_run_at was never written, so the recovery helper re-armed it). Remove it instead of re-firing.
    repeat = job.get("repeat")
    if repeat:
        times = repeat.get("times")
        finite = times is not None and times > 0
        completed = repeat.get("completed", 0)
        # Finite one-shots were pre-claimed by claim_dispatch() (completed already incremented) —
        # do not double-count; recurring jobs and direct callers still get the increment.
        if not (kind == "once" and finite and completed > 0):
            completed += 1
            repeat["completed"] = completed
        if finite and completed >= times:
            # Limit reached: retain a terminal record instead of popping it, so the status just
            # written stays inspectable in `cronjob list`; the retention sweep prunes it later.
            _complete_job_record(job)
            return

    # An interval fire re-arms off the instant it started, not off this finish time (t_b2f58c48).
    fire_at = job.pop(INTERVAL_FIRE_AT_KEY, None)
    next_slot = None
    if kind == "interval":
        next_slot = _next_interval_slot(job["schedule"], fire_at, datetime.fromisoformat(now))
    job["next_run_at"] = next_slot or compute_next_run(job["schedule"], now)
    if job["next_run_at"] is not None:
        if job.get("state") != "paused":
            job["state"] = "scheduled"
    elif kind in {"cron", "interval"}:
        # Recurring: transient failure (e.g. croniter missing) — disabling it would turn a missing
        # dep into "job completed" and silently drop the schedule.
        job["state"] = "error"
        if not job.get("last_error"):
            job["last_error"] = (
                "Failed to compute next run for recurring schedule (is the 'croniter' package "
                "installed in the gateway's Python env?)")
        logger.error(
            "Job '%s' (%s) could not compute next_run_at; "
            "leaving enabled and marking state=error so the job is not silently disabled.",
            job.get("name", job.get("id", "?")), kind)
    else:
        _complete_job_record(job)  # one-shot: terminal completion


def mark_job_run(
    job_id: str,
    success: bool,
    error: Optional[str] = None,
    delivery_error: Optional[str] = None,
    status: Optional[str] = None,
    *,
    expected_fire_owner: Optional[str] = None,
    lock_timeout: Optional[float] = None,
    model_unreachable: bool = False,
    quota_hold_seconds: Optional[float] = None,
    recover_consumed_fire: bool = False,
) -> bool:
    """Mark a job as run: update last_run_at/last_status, bump completed, recompute next_run_at,
    and retire the record as a terminal completion when the repeat limit is reached.

    ``delivery_error`` is separate from the agent error: agent succeeded but delivery failed records
    ``last_status = "delivery_failed"`` (never "ok") while ``failure_streak`` is left alone. An
    explicit ``status`` (e.g. "blocked_config") overrides the derived value. False when the fence
    can't be taken, the job is missing, or ``expected_fire_owner`` no longer holds the fire claim.

    ``model_unreachable``: this failed run never reached the model (transient network/DNS error,
    zero API calls). Recurring jobs then get a bounded automatic re-run — ``next_run_at`` is pulled
    earlier per ``cron.unreachable_retry.RETRY_DELAYS_SECONDS`` — instead of waiting a full period
    (Cowork-style; see cron/unreachable_retry.py).

    ``quota_hold_seconds``: the provider said it stays closed for this long (a quota 429 with
    ``retry after <N>s``). Recurring jobs are parked through the window instead of re-firing into
    it on every tick. ``recover_consumed_fire`` lets a scheduled sparse cron recover its
    consumed fire when the provider reopens; manual runs retain the natural schedule
    (cron/quota_hold.py, #89376).
    """
    def apply(jobs, _i, job):
        if expected_fire_owner is not None:
            claim = job.get("fire_claim")
            if not isinstance(claim, dict) or claim.get("by") != expected_fire_owner:
                logger.warning(
                    "mark_job_run: job_id %s fire claim owner changed; discarding stale completion",
                    job_id)
                return False
        now = _hermes_now().isoformat()
        _record_run_outcome(job, success, error, delivery_error, status, now)
        _advance_after_run(job, now)
        from cron import quota_hold
        from cron.unreachable_retry import clear_state, plan_retry

        if not success and model_unreachable and not is_terminal_job(job):
            plan_retry(job)
        else:
            # Any run that reached the model (either outcome) resets the re-run ladder.
            clear_state(job)
        if not success and quota_hold_seconds and not is_terminal_job(job):
            quota_hold.plan_hold(job, quota_hold_seconds, recover_consumed_fire=recover_consumed_fire)
        else:
            quota_hold.clear_state(job)
        save_jobs(jobs)
        return True

    def locked():
        found = _with_job(job_id, apply, missing=_MISSING)
        if found is _MISSING:
            logger.warning("mark_job_run: job_id %s not found, skipping save", job_id)
            return False
        return found

    return _under_fire_fence(job_id, locked, timeout=lock_timeout)


def _write_oneshot_diagnostic(job: Dict[str, Any], text: str, what: str) -> bool:
    """Best-effort operator-visible trace in the job's output dir; never breaks the caller."""
    try:
        save_job_output(job.get("id", ""), text)
        return True
    except Exception as e:
        logger.debug("Failed to write %s diagnostic for job %r: %s", what, job.get("id"), e)
        return False


def _write_wedged_oneshot_diagnostic(job: Dict[str, Any]) -> None:
    """Trace for a wedged one-shot removal: dispatch was claimed but mark_job_run never ran
    (interrupted mid-run); removing it silently would leave no output, error, or record.

    A finite one-shot whose dispatch was claimed (``repeat.completed`` >= ``repeat.times``) but which never
    reached ``mark_job_run`` (``last_run_at`` is null) was interrupted mid-run — scheduler restart, gateway
    kill, or a non-Exception escape (#73973). The recovery guards remove such jobs so they stop appearing
    due, but a silent removal leaves the user with no output, no error, and no job record. Write a small
    diagnostic file into the job's output directory so the removal is observable and debuggable.
    """
    if job.get("last_run_at") is not None:
        return  # a prior run was recorded — normal completion race, not a wedge
    repeat = job.get("repeat") or {}
    claim = job.get("run_claim") or {}
    written = _write_oneshot_diagnostic(
        job,
        "# Cron job removed without producing output\n\n"
        f"- job id: {job.get('id')}\n"
        f"- name: {job.get('name')}\n"
        f"- dispatch claimed: {repeat.get('completed', '?')}/{repeat.get('times', '?')}\n"
        f"- run claimed at: {claim.get('at', 'unknown')} by {claim.get('by', 'unknown')}\n"
        f"- removed at: {_hermes_now().isoformat()}\n\n"
        "This one-shot job's dispatch was claimed, but the run never "
        "completed (`last_run_at` was never written) — the scheduler "
        "process was most likely killed or restarted mid-execution. The "
        "job has been removed to stop it re-firing; recreate it to run "
        "again.\n",
        "wedged-oneshot")
    if written:
        logger.warning(
            "Job '%s': removed without a completed run — diagnostic written to "
            "its output directory",
            job.get("name", job.get("id", "?")))


def _write_missed_oneshot_diagnostic(
    job: Dict[str, Any],
    next_run: str,
    *,
    window_seconds: Optional[float] = None,
) -> None:
    """Leave an operator-visible trace when a never-ran one-shot is retired
    because its persisted run time fell outside the catch-up window.

    The record is removed because the scheduler will never fire it (see the
    grace gate in ``_get_due_jobs_locked``); without a diagnostic the job
    would just vanish. The same text is queued for LOUD delivery to the job's
    deliver target (drained by the scheduler tick). Best-effort: a diagnostic
    failure never blocks the removal itself.
    """
    if window_seconds is None:
        window_seconds = ONESHOT_GRACE_SECONDS
    try:
        now = _hermes_now()
        try:
            late_min = int(
                (now - _ensure_aware(datetime.fromisoformat(next_run))).total_seconds()
                // 60
            )
        except Exception:
            late_min = -1
        late_txt = f"{late_min} min" if late_min >= 0 else "unknown"
        text = (
            "# ⚠️ MISSED — scheduled job did NOT run (removed before firing, "
            "run time outside grace window)\n\n"
            f"- job id: {job.get('id')}\n"
            f"- name: {job.get('name')}\n"
            f"- scheduled run time: {next_run}\n"
            f"- past due by: {late_txt}\n"
            f"- catch-up window: {int(window_seconds)}s (cron.oneshot_catchup_s)\n"
            f"- removed at: {now.isoformat()}\n\n"
            "This one-shot NEVER RAN. Its run time is further in the past than "
            "the restart catch-up window (scheduler down past the window, host "
            "asleep, or jobs.json edited), so it was removed without running. "
            "Whatever it was armed to do has NOT happened — recreate it (or "
            "use the Run button) if it is still needed.\n"
        )
        save_job_output(job.get("id", ""), text)
        with _missed_oneshot_notices_lock:
            _missed_oneshot_notices.append({"job": dict(job), "text": text})
        logger.error(
            "Job '%s' (%s): one-shot MISSED — due %s, %s past due, beyond the "
            "%ds catch-up window; removed WITHOUT running",
            job.get("name", job.get("id", "?")),
            job.get("id"),
            next_run,
            late_txt,
            int(window_seconds),
        )
    except Exception as e:
        logger.debug(
            "Failed to write missed-oneshot diagnostic for job %r: %s",
            job.get("id"), e,
        )


def claim_dispatch(job_id: str) -> bool:
    """Atomically claim a finite one-shot dispatch BEFORE execution: ``repeat.completed`` is bumped
    and persisted under the jobs lock so a tick dying mid-execution cannot lose the dispatch
    (*at-most-times* instead of *at-least-once*). True if the caller may run the job; False when
    the limit is already reached. Only ``kind == "once"`` with ``repeat.times > 0`` is claimed.

    Increments ``repeat.completed`` under the cross-process jobs lock and persists the claim immediately, so
    that if the tick dies mid-execution (gateway kill, OOM, segfault, hard-timeout) the dispatch is not
    lost. This converts finite one-shot jobs from *at-least-once* to *at-most-times* semantics — a job that
    self-destructs fires at most ``repeat.times`` times instead of infinitely (issue #38758).
    """
    def apply(jobs, i, job):
        repeat = job.get("repeat") or {}
        times = repeat.get("times")
        # Recurring jobs use advance_next_run(); no/infinite repeat limit always dispatches.
        if job.get("schedule", {}).get("kind") != "once" or times is None or times <= 0:
            return True
        completed = repeat.get("completed", 0)
        label = job.get("name", job.get("id", "?"))
        if completed >= times:
            if job.get("last_run_at") is not None:
                # A prior run completed normally (mark_job_run raced this tick). Retain the terminal
                # record, as mark_job_run's repeat-limit branch does, instead of deleting the
                # status.
                _complete_job_record(job)
                save_jobs(jobs)
                logger.info(
                    "Job '%s': dispatch limit reached (%d/%d) — marking completed",
                    label, completed, times)
                return False
            # A prior tick claimed the dispatch then died — a genuinely wedged claim. Remove it so
            # it stops appearing due, leaving an operator-visible diagnostic.
            jobs.pop(i)
            # See #73973.
            save_jobs(jobs, removed_ids={job_id})
            _write_wedged_oneshot_diagnostic(job)
            logger.info(
                "Job '%s': dispatch limit reached (%d/%d) — removing", label, completed, times)
            return False
        # Claim this dispatch before the side effect runs.
        repeat["completed"] = completed + 1
        save_jobs(jobs)
        logger.debug("Job '%s': claimed dispatch %d/%d", label, repeat["completed"], times)
        return True

    claimed = _with_job(job_id, apply, missing=_MISSING)
    if claimed is _MISSING:
        logger.debug(
            "claim_dispatch: job_id %s not in store — proceeding without claim "
            "(handed-in job dict; nothing to persist a claim against)",
            job_id)
        return True
    return claimed


def _refresh_claim(jobs: List[Dict[str, Any]], claim: Any, expected_owner: str) -> bool:
    """Compare-and-refresh a claim's ``at`` stamp; False unless *expected_owner* still holds it."""
    if not isinstance(claim, dict) or claim.get("by") != expected_owner:
        return False
    claim["at"] = _hermes_now().isoformat()
    save_jobs(jobs)
    return True


def heartbeat_run_claim(job_id: str, *, expected_owner: str) -> bool:
    """Refresh a one-shot's ``run_claim`` timestamp while its run is alive, so an expired claim
    really means the claiming process died. Compare-and-refresh on ``expected_owner`` stops a stale
    runner from extending a claim another process has since taken over.

    Called periodically from the scheduler's run monitor (#62002) so a legitimately long run keeps its claim
    fresh: an expired claim then really does mean "the claiming process died", and neither another process's
    tick nor this process's own next tick will re-dispatch or stale-remove the job while the run is in
    flight. mark_job_run() clears the claim on completion.
    """
    def apply(jobs, _i, job):
        if job.get("schedule", {}).get("kind") != "once":
            return False
        return _refresh_claim(jobs, job.get("run_claim"), expected_owner)

    return _with_job(job_id, apply, False)


def clear_run_claim(job_id: str) -> bool:
    """Clear a one-shot's ``run_claim`` when dispatch itself fails: such a job never reaches
    mark_job_run, so the stale claim would block re-dispatch until the TTL expires.

    Calling this on every early-exit path restores the "the job stays due and will fire on the next healthy
    tick" invariant that the scheduler comment promises (#86522).
    """
    def apply(jobs, _i, job):
        if job.get("schedule", {}).get("kind") != "once" or job.get("run_claim") is None:
            return False  # recurring, or already cleared
        job["run_claim"] = None
        save_jobs(jobs)
        return True

    return _with_job(job_id, apply, False)


def advance_next_runs(job_ids) -> int:
    """Batch form of :func:`advance_next_run`: one load + at most one save for the whole due set;
    one-shot/unknown ids are skipped. Returns the count advanced. Persisted once at the end, so a
    crash mid-batch re-fires the whole set on restart rather than a prefix (sub-10ms window)."""
    ids = set(job_ids)
    if not ids:
        return 0
    with _jobs_lock():
        jobs = load_jobs()
        now_dt = _hermes_now()
        now = now_dt.isoformat()
        advanced = 0
        for job in jobs:
            if (
                job["id"] not in ids
                or (is_terminal_job(job) and not _is_recoverable_error_job(job))
                or job.get("schedule", {}).get("kind") not in {"cron", "interval"}
            ):
                continue
            new_next = compute_next_run(job["schedule"], now)
            if new_next and new_next != job.get("next_run_at"):
                _record_interval_fire(job, now_dt)
                job["next_run_at"] = new_next
                advanced += 1
        if advanced:
            save_jobs(jobs)
        return advanced


def advance_next_run(job_id: str) -> bool:
    """Advance a recurring job's next_run_at BEFORE run_job() so a mid-run crash cannot re-fire it
    on restart (at-most-once for recurring jobs — one missed run beats a crash-loop burst).
    One-shots are left unchanged so they can retry. Returns True if next_run_at was advanced."""
    # >= 1 (not == 1): duplicate ids in a corrupted file all advance; still report the advance.
    return advance_next_runs([job_id]) >= 1


def _machine_id() -> str:
    """Claim attribution/debugging id (NOT correctness — that comes from the file lock and the
    fresh-claim check): ``HERMES_MACHINE_ID`` if set, else hostname:pid."""
    explicit = os.getenv("HERMES_MACHINE_ID", "").strip()
    if explicit:
        return explicit
    try:
        import socket
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def claim_job_for_fire(
    job_id: str, *, claim_ttl_seconds: int = FIRE_CLAIM_TTL_SECONDS, force: bool = False,
    manual: bool = False, return_job: bool = False,
) -> Union[bool, Dict[str, Any]]:
    """Atomically claim a job for one external 'fire' (multi-machine at-most-once); True iff THIS
    caller won (``CronScheduler.fire_due``: exactly one of N replicas runs a job). Under the
    fence + file lock: reject missing/terminal/paused jobs unless ``force`` (explicit manual
    fire, which also resumes the job atomically; external callbacks must leave it false so a
    stale callback cannot resurrect a paused job). ``manual`` = off-tick run-now without the
    resume: no occurrence stamp, so the still-pending ``next_run_at`` slot is not skipped. Lose if a claim younger than
    ``claim_ttl_seconds`` exists (the TTL lets another fire reclaim after a crash; mark_job_run
    clears the claim). Otherwise stamp ``fire_claim`` and, for recurring jobs, advance
    ``next_run_at`` so a stale re-delivery cannot re-fire."""
    def apply(jobs, _i, job):
        if is_terminal_job(job) and not _is_recoverable_error_job(job):
            return False
        # Both enabled and pause markers must clear — a half-paused record must not claim. ``force``
        # (Trigger-now on a paused job) bypasses the gate and atomically resumes the job below.
        if not force and not is_job_runnable(job):
            return False
        now = _hermes_now()
        if _claim_is_live(job.get("fire_claim"), now, claim_ttl_seconds):
            return False  # someone holds a fresh claim
        from cron.occurrences import completed_occurrence, scheduled_instant

        # ``manual`` (an off-tick run-now) must NOT stamp an occurrence identity: outside a
        # scheduler tick ``next_run_at`` is the NEXT occurrence, not the one being run, so
        # stamping it would make completed_occurrence() skip that slot when it arrives.
        manual_fire = force or manual or job.get("manual_run_at") == job.get("next_run_at")
        instant = None if manual_fire else scheduled_instant(job.get("next_run_at"))
        # A scheduled tick only ever fires when now >= next_run_at
        # (_evaluate_due_job returns False while the stored occurrence is still
        # in the future), so a claim arriving BEFORE the stored next occurrence
        # cannot be the tick that owns it — it is a manual / dashboard / webhook
        # fire and must stay occurrence-free. Binding it would make run_one_job
        # stamp that FUTURE instant completed in the ledger: later manual fires
        # are then refused ("Job is already being fired by the scheduler") and
        # the scheduled tick dedupe-skips its real delivery (2026-09-08 live:
        # a manual run at 19:53 consumed the next day's 19:00 occurrence).
        # A claim within FIRE_CLAIM_SKEW_SECONDS of the slot is the fire for that slot
        # (provider clock skew); dropping its identity would leave the slot unrecorded, so
        # mark_job_run recomputes the same cron slot and the misfire backstop runs it twice.
        if (instant is not None
                and datetime.fromisoformat(instant) - now >= timedelta(seconds=FIRE_CLAIM_SKEW_SECONDS)):
            instant = None
        if instant and completed_occurrence(job, instant):
            if job.get("schedule", {}).get("kind") in {"cron", "interval"}:
                nxt = compute_next_run(job["schedule"], now.isoformat())
                if nxt:
                    job["next_run_at"] = nxt
                    save_jobs(jobs)
            return False
        if force:
            _activate_job_record(job)
        # Per-acquisition token: a process may legitimately reclaim its own stale lease, and the
        # previous runner must not heartbeat the new claim merely because hostname + PID match.
        job["fire_claim"] = {"at": now.isoformat(), "by": f"{_machine_id()}:{uuid.uuid4().hex}"}
        # Claimed: the occurrence is now owned by a run (its ledger row + fire claim carry it).
        job.pop("pending_slot", None)
        if job.get("schedule", {}).get("kind") in {"cron", "interval"}:
            nxt = compute_next_run(job["schedule"], now.isoformat())
            if nxt:
                _record_interval_fire(job, now)
                job["next_run_at"] = nxt
        save_jobs(jobs)
        return dict(copy.deepcopy(job), _scheduled_instant=instant) if return_job else True

    return _under_fire_fence(job_id, lambda: _with_job(job_id, apply, False))


def heartbeat_fire_claim(job_id: str, *, expected_owner: str) -> bool:
    """Refresh an active ``fire_claim`` without extending another owner's lease: an execution may
    outlive the TTL, and the owner check stops a stale runner from refreshing a recovered claim.
    Deliberately NOT under ``_under_fire_fence``: the run thread holds the per-job fence across
    delivery, and the fence's reentrancy is per thread, so the heartbeat thread would time out and
    report a false ownership loss. ``_jobs_lock`` already serializes the compare-and-refresh."""
    def apply(jobs, _i, job):
        return _refresh_claim(jobs, job.get("fire_claim"), expected_owner)

    return _with_job(job_id, apply, False)


# Completed one-shots are retained in jobs.json (final status stays inspectable) and pruned by
# _sweep_completed_oneshots once they age out.
COMPLETED_ONESHOT_RETENTION_DAYS = 7
_STALE_FIRE_LOCK_AGE_SECONDS = 60.0
_FIRE_LOCK_NAME_RE = re.compile(r"^\.fire-[0-9a-f]{32}\.lock$")


def _reap_stale_fire_locks(
    live_job_ids: Collection[str], *, now_epoch: Optional[float] = None
) -> int:
    """Remove old fire-fence pathnames that no current job can open again.

    Fire-fence files are durable synchronization pathnames and normally outlive
    each lock holder.  Only a file whose hashed job id is absent from the
    current store is residue.  The age floor avoids racing a just-deleted job,
    and a non-blocking exclusive ``flock`` preserves an orphan whose final run
    still has the inode open.  Current-job files are never unlinked: deleting a
    pathname while a waiter has opened the old inode could split future callers
    across two lock files.  Platforms without ``fcntl`` fail closed.
    """
    if fcntl is None:
        return 0

    cron_dir = _current_cron_store().cron_dir
    live_lock_names = {
        f".fire-{uuid.uuid5(uuid.NAMESPACE_URL, f'{cron_dir.resolve()}::{job_id}').hex}.lock"
        for job_id in live_job_ids
        if job_id
    }
    now = time.time() if now_epoch is None else now_epoch
    removed = 0
    try:
        candidates = list(cron_dir.glob(".fire-*.lock"))
    except OSError:
        return 0

    for lock_path in candidates:
        if (
            not _FIRE_LOCK_NAME_RE.fullmatch(lock_path.name)
            or lock_path.name in live_lock_names
        ):
            continue
        lock_fd = None
        acquired = False
        try:
            before = lock_path.stat()
            if before.st_size != 0 or now - before.st_mtime <= _STALE_FIRE_LOCK_AGE_SECONDS:
                continue
            lock_fd = open(lock_path, "a+", encoding="utf-8")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, IOError):
                continue

            # The pathname may have been replaced between stat() and flock().
            # Delete only the exact inode we inspected and locked.
            held = os.fstat(lock_fd.fileno())
            current = lock_path.stat()
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                continue
            if current.st_size != 0 or now - current.st_mtime <= _STALE_FIRE_LOCK_AGE_SECONDS:
                continue
            lock_path.unlink()
            removed += 1
        except (OSError, IOError):
            continue
        finally:
            if lock_fd is not None:
                try:
                    if acquired:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
                lock_fd.close()

    if removed:
        logger.info("Pruned %d stale cron fire-fence lock file(s)", removed)
    return removed


def _cron_config_number(key: str, default: Any, cast: Callable[[Any], Any]) -> Any:
    """Read ``cron.<key>`` from config as *cast*, falling back to *default* on any failure."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        return cast(cron_cfg.get(key, default))
    except Exception:
        return cast(default)


def _completed_oneshot_retention_days() -> float:
    """``cron.completed_retention_days``; non-positive disables the sweep (records kept forever)."""
    return _cron_config_number("completed_retention_days", COMPLETED_ONESHOT_RETENTION_DAYS, float)


def _sweep_completed_oneshots(
    raw_jobs: List[Dict[str, Any]], now: datetime, *, removed_ids: Optional[Set[str]] = None,
) -> bool:
    """Prune completed one-shot records past retention (in place; True when anything was removed).
    Removed ids go into *removed_ids* so save_jobs's shrink-merge guard allows the delete. Age is
    measured from ``last_run_at``; a record without a parseable one is kept (never guess into
    deletion)."""
    retention_days = _completed_oneshot_retention_days()
    if retention_days <= 0:
        return False
    cutoff = now - timedelta(days=retention_days)
    removed = False
    for rj in list(raw_jobs):
        try:
            if rj.get("state") != "completed":
                continue
            schedule = rj.get("schedule")
            if (schedule.get("kind") if isinstance(schedule, dict) else None) != "once":
                continue
            last_run = rj.get("last_run_at")
            last_run_dt = _parse_aware(last_run) if isinstance(last_run, str) else None
            if last_run_dt is None or last_run_dt >= cutoff:
                continue
            raw_jobs.remove(rj)
            removed = True
            rid = rj.get("id")
            if removed_ids is not None and rid:
                removed_ids.add(str(rid))
            logger.info(
                "Job '%s': pruning completed one-shot record (finished %s, retention %.1f days)",
                rj.get("name", rj.get("id", "?")), last_run, retention_days)
        except Exception:
            logger.debug(
                "Retention sweep skipped malformed job record %r", rj.get("id", "?"), exc_info=True)
    return removed


# --- Due scan ---

def get_due_jobs() -> List[Dict[str, Any]]:
    """Return all jobs due now. A recurring job more than one period stale (gateway down, or a run
    overran the interval) has its backlog collapsed — next_run_at fast-forwards so nothing
    burst-fires — but still fires ONCE now (via mark_job_run, consuming one ``repeat.times`` run),
    avoiding the perpetual-defer loop for runs longer than interval + grace.

    This prevents the perpetual-defer loop (#33315) where a job whose runtime exceeds ``interval + grace``
    would be skipped forever.
    """
    with _jobs_lock():
        return _get_due_jobs_locked()


@dataclass
class _DueScan:
    """Mutable state threaded through one due scan: the raw store records plus what to persist."""

    raw_jobs: List[Dict[str, Any]]
    now: datetime
    needs_save: bool = False
    removed: Set[str] = field(default_factory=set)

    def find(self, job_id: Any) -> Optional[Dict[str, Any]]:
        return next((rj for rj in self.raw_jobs if rj["id"] == job_id), None)

    def persist(self, job_id: Any, **fields: Any) -> None:
        """Write *fields* onto the raw record for *job_id* and flag a save (no-op if missing)."""
        rj = self.find(job_id)
        if rj is not None:
            rj.update(fields)
            self.needs_save = True

    def retire(self, job_id: Any) -> None:
        """Drop the raw record for *job_id* as an intentional removal."""
        rj = self.find(job_id)
        if rj is not None:
            self.raw_jobs.remove(rj)
            self.removed.add(str(job_id))
            self.needs_save = True


def _normalize_due_scan_records(raw_jobs: List[Dict[str, Any]]) -> bool:
    """Repair malformed store records in place BEFORE the due scan keys off them: a missing ``id``
    (older writers used ``job_id``), non-dict ``schedule``, or non-ISO timestamp used to abort the
    whole scan before save_jobs(), freezing the scheduler in a fast-forward loop."""
    changed = False
    for rj in raw_jobs:
        if not rj.get("id"):
            rj["id"] = rj.pop("job_id", None) or uuid.uuid4().hex[:12]
            changed = True
        if not isinstance(rj.get("schedule"), dict):
            rj["schedule"] = {}
            changed = True
        for key in ("next_run_at", "last_run_at"):
            value = rj.get(key)
            if value is not None and _parse_aware(value) is None:
                rj.pop(key, None)  # the "no next_run_at" path recomputes
                changed = True
    return changed


def _self_disable_half_paused(job: Dict[str, Any], scan: _DueScan) -> None:
    """Self-heal enabled=true with pause markers: the operator believes the job is frozen while the
    scheduler would still fire it. Force enabled=false so listings are honest; logged loudly since
    pause_job sets both fields atomically, so this should be rare."""
    jid = job.get("id")
    logger.error(
        "Job '%s' (%s) has pause markers while enabled=true; "
        "self-disabling so it cannot fire (pause must be authoritative).",
        job.get("name", jid), jid)
    rj = scan.find(jid)
    if rj is None:
        return
    rj.update(enabled=False, state="paused")
    if not rj.get("paused_at"):
        rj["paused_at"] = scan.now.isoformat()
    if not rj.get("paused_reason"):
        rj["paused_reason"] = "auto-disabled: enabled+paused contradiction"
    scan.needs_save = True


def _recover_missing_next_run(job: Dict[str, Any], scan: _DueScan) -> Optional[str]:
    """Recompute and persist a missing ``next_run_at``; None when unrecoverable. One-shots use the
    grace window; recurring jobs only get here after a direct jobs.json edit bypassed add_job(),
    and would otherwise be silently skipped forever."""
    schedule = job.get("schedule", {})
    kind = schedule.get("kind")
    recovered_next = _recoverable_oneshot_run_at(
        schedule, scan.now, last_run_at=job.get("last_run_at"))
    recovery_kind = "one-shot" if recovered_next else None
    if not recovered_next and kind in {"cron", "interval"}:
        recovered_next = compute_next_run(schedule, scan.now.isoformat())
        if recovered_next:
            recovery_kind = kind
    if not recovered_next:
        return None
    job["next_run_at"] = recovered_next
    logger.info(
        "Job '%s' had no next_run_at; recovering %s run at %s",
        job.get("name", job.get("id", "?")), recovery_kind, recovered_next)
    scan.persist(job["id"], next_run_at=recovered_next)
    return recovered_next


@dataclass
class _DueJob:
    """One candidate under evaluation: its record, schedule and the stored next_run in raw/aware
    form."""

    job: Dict[str, Any]
    scan: _DueScan
    next_run: str  # stored ISO string, compared string-exact against manual_run_at
    raw_next_run_dt: datetime  # as stored (may carry a pre-migration offset)
    next_run_dt: datetime  # normalized to the configured tz

    @property
    def schedule(self) -> Dict[str, Any]:
        return self.job.get("schedule", {})

    @property
    def kind(self) -> Optional[str]:
        return self.schedule.get("kind")

    @property
    def label(self) -> Any:
        return self.job.get("name", self.job.get("id", "?"))

    def recompute_next(self) -> Optional[str]:
        return compute_next_run(self.schedule, self.scan.now.isoformat())


def _repair_timezone_shifted_cron(d: _DueJob) -> bool:
    """Repair a cron job whose stored offset no longer matches now's (TZ migration).

    next_run_at is an absolute instant but the expr means local wall clock, so a TZ change can make
    it look due hours early. If the stored wall clock is still in the future, recompute so we fire
    at the intended local time. True when re-anchored (caller skips this tick). TRADE-OFF: a DST
    offset change meeting the same conditions SKIPS the pending occurrence; accepted as rare."""
    now = d.scan.now
    if not (
        _instant_at_or_before(d.next_run_dt, now)
        and _timezone_offset_mismatch(d.raw_next_run_dt, now)
        and _stored_wall_clock_is_future(d.raw_next_run_dt, now)
    ):
        return False
    new_next = d.recompute_next()
    if not new_next:
        return False
    logger.info(
        "Job '%s' next_run_at offset changed (%s -> %s). "
        "Recomputing cron run to preserve local wall-clock intent: %s",
        d.label, d.raw_next_run_dt.utcoffset(), now.utcoffset(), new_next)
    d.scan.persist(d.job["id"], next_run_at=new_next)
    return True


def _rearm_stale_error_recurring(d: _DueJob) -> datetime:
    """Re-arm a recurring job wedged in persisted last_status=error; returns the effective
    next_run_dt.

    Such a job errored, mark_job_run parked next_run_at in the future, and nothing re-dispatched it
    (the in-memory stale-claim sweep cannot see it). Interval jobs re-arm to now (always a legal
    fire); cron jobs re-arm to the next LEGAL occurrence, since re-arming to now would fire at times
    the expression excludes. A correctly-parked cron value is left as-is.
    """
    now = d.scan.now
    if not (
        d.kind in ("cron", "interval")
        and _instant_after(d.next_run_dt, now)
        and _job_is_stale_error_recurring(d.job, d.schedule, now)
    ):
        return d.next_run_dt
    if d.kind == "interval":
        recovered_next = now.isoformat()
        recovered_next_dt: Optional[datetime] = now
    else:
        recovered_next = d.recompute_next()
        recovered_next_dt = _parse_aware(recovered_next) if recovered_next else None
    if not (
        recovered_next and recovered_next_dt is not None
        and _instant_before(recovered_next_dt, d.next_run_dt)
    ):
        return d.next_run_dt
    jid = d.job.get("id")
    logger.warning(
        "cron.persisted_error.recovered job='%s' id=%s — recurring "
        "job wedged in stale last_status=error without re-firing for "
        "a full cadence; re-arming next_run_at to %s so it re-dispatches without force-run/resume",
        d.job.get("name", jid), jid, recovered_next)
    _record_persisted_error_recovery(d.job, d.next_run)
    d.job["next_run_at"] = recovered_next
    d.scan.persist(jid, next_run_at=recovered_next)
    return recovered_next_dt


def _reanchor_stale_cron(d: _DueJob) -> bool:
    """Stale-schedule guard for a due cron instant; True when re-anchored without firing.

    A direct edit of schedule.expr leaves next_run_at on the old lattice, so re-anchor first (from
    the current expr, so this converges). Some instants are off the lattice on purpose and
    authorize one fire instead: an offset-representation migration that would otherwise swallow
    a never-fired occurrence, and the failure-path planners' parked instants (a quota recovery
    before a sparse cron's next natural occurrence, an unreachable-model retry rung). All fall
    through to fire ONCE (at-most-once holds: completion rewrites the instant)."""
    stale_class = _classify_stale_cron_next_run(d.schedule, d.raw_next_run_dt, d.next_run_dt)
    if stale_class == STALE_CRON_EXPR_EDIT:
        from cron.quota_hold import is_recovery_fire
        from cron.unreachable_retry import is_retry_fire
        if is_recovery_fire(d.job, d.next_run) or is_retry_fire(d.job, d.next_run):
            logger.info(
                "cron.off_lattice_fire job='%s' id=%s expr=%r at=%s",
                d.label, d.job.get("id"), d.schedule.get("expr"), d.next_run)
            return False
        new_next = d.recompute_next()
        logger.info(
            "Job '%s' next_run_at %s does not match its current "
            "cron expression %r (direct jobs.json edit?); re-anchoring to %s without firing.",
            d.label, d.next_run, d.schedule.get("expr"), new_next)
        if new_next:
            d.scan.persist(d.job["id"], next_run_at=new_next)
        return True
    if stale_class == STALE_CRON_TIMEZONE_MIGRATION:
        logger.warning(
            "cron.timezone_migration.catch_up job='%s' id=%s expr=%r "
            "stored=%s normalized=%s — stored next_run_at carries a "
            "pre-migration UTC offset (%s, now %s) and is a legal "
            "occurrence at its own wall clock; firing the due run instead of re-anchoring past it.",
            d.label, d.job.get("id"), d.schedule.get("expr"), d.next_run,
            d.next_run_dt.isoformat(), d.raw_next_run_dt.utcoffset(), d.scan.now.utcoffset())
        _record_timezone_migration_catchup(d.job, d.raw_next_run_dt, d.next_run_dt)
    return False


def _fast_forward_missed_recurring(d: _DueJob, grace: int) -> bool:
    """Re-anchor accumulated misses; return whether catch-up was explicitly disabled.

    The fast-forward is persisted immediately — NOT redundant with advance_next_run/mark_job_run:
    it
    protects the crash window before mark_job_run and covers the external fire_due path, which never
    calls advance_next_run. mark_job_run re-anchors on completion, so the value is provisional.
    """
    if _elapsed_seconds(d.scan.now, d.next_run_dt) <= grace:
        return False
    new_next = d.recompute_next()
    if not new_next:
        return False
    d.scan.persist(d.job["id"], next_run_at=new_next)
    if (_instant_after(_ensure_aware(datetime.fromisoformat(new_next)), d.scan.now)
            and not _cron_config_number("catch_up_missed", True, lambda value: value is not False)):
        logger.info(
            "Job '%s' missed its scheduled time (%s, grace=%ds). "
            "Skipping missed occurrence because cron.catch_up_missed is false; next run: %s",
            d.label, d.next_run, grace, new_next)
        record_cron_missed(d.job)
        return True
    logger.info(
        "Job '%s' missed its scheduled time (%s, grace=%ds). "
        "Running now; next run provisionally set to: %s (re-anchored on completion)",
        d.label, d.next_run, grace, new_next)
    record_catch_up_occurrence()
    return False


def _retire_expired_oneshot(d: _DueJob, manual_run: bool = False) -> bool:
    """One-shot grace gate; True when the job must not fire this tick.

    A one-shot beyond the grace window must never fire (create/update/resume reject such schedules
    and recovery never revives them; only the due scan used to dispatch them hours late). With no
    claim stamped, retire it with a diagnostic (never silently delete). A claim may mean a run is
    still in flight elsewhere — skip but keep the record so its mark_job_run can land.

    Restart catch-up (fork, t_9bfdd7e3 / #1087): past the grace window but within
    ``cron.oneshot_catchup_s`` (default 6h), a never-dispatched one-shot FIRES LATE instead —
    its time fell inside a gateway restart, which is routine. The in-memory snapshot carries
    ``LATE_FIRE_KEY`` so the prompt gets a late-fire note."""
    overdue_s = _elapsed_seconds(d.scan.now, d.next_run_dt)
    if overdue_s <= ONESHOT_GRACE_SECONDS:
        return False
    catchup_s = _oneshot_catchup_seconds()
    unclaimed = not (d.job.get("run_claim") or d.job.get("fire_claim"))
    if unclaimed and not manual_run and overdue_s <= catchup_s and not d.job.get("last_run_at"):
        d.job[LATE_FIRE_KEY] = int(overdue_s)
        logger.warning(
            "Job '%s': one-shot due %s is %.0f min past due (inside the %ds restart catch-up "
            "window) — firing late now",
            d.label, d.next_run, overdue_s / 60, int(catchup_s))
        return False
    if unclaimed:
        _write_missed_oneshot_diagnostic(d.job, d.next_run, window_seconds=catchup_s)
        d.scan.retire(d.job["id"])
        record_cron_missed(d.job)
    return True


def _oneshot_dispatch_limit_reached(job: Dict[str, Any], scan: _DueScan) -> bool:
    """One-shot dispatch-limit guard; True when the job must not fire this tick.

    A finite one-shot claimed via claim_dispatch() whose tick died before mark_job_run has
    completed >= times while still looking due. Remove it instead of re-firing — unless THIS
    process is still running it (a run outliving the run_claim TTL is slow, not stale)."""
    repeat = job.get("repeat") or {}
    times = repeat.get("times")
    completed = repeat.get("completed", 0)
    if times is None or times <= 0 or completed < times:
        return False
    name = job.get("name", job.get("id", "?"))
    # A live run must never have its job record deleted underneath it (#62002): a run that outlives the
    # run_claim TTL (stream stall, laptop asleep mid-run) satisfies the same completed >= times +
    # expired-claim condition as a dead tick, but mark_job_run() still needs the record to land last_run_at
    # / last_status / last_delivery_error. If this process is still running the job, it is slow, not stale —
    # keep the entry and skip.
    if _job_running_in_this_process(job.get("id", "")):
        logger.info(
            "Job '%s': dispatch limit reached (%d/%d) but its run is still in flight in this "
            "process — keeping entry",
            name, completed, times)
        return True
    if job.get("last_run_at") is not None:
        # A record with last_run_at completed a real run and was re-armed without a budget reset
        # (old build or hand edit) — not the dead-tick case; warn so the removal leaves a trace.
        logger.warning(
            "Job '%s': one-shot dispatch limit reached (%d/%d) on a record that already completed "
            "a run (last_run_at=%s) — removing it WITHOUT firing. This record was re-armed "
            "without a budget reset (pre-#93615 store or hand edit); re-run it with "
            "'hermes cron resume <job> --run-now' (#93524).",
            name, completed, times, job.get("last_run_at"))
    else:
        logger.info(
            "Job '%s': one-shot dispatch limit reached (%d/%d) — removing stale due entry",
            name, completed, times)
    scan.retire(job["id"])
    # The claimed run never completed here by definition — leave an operator-visible diagnostic.
    _write_wedged_oneshot_diagnostic(job)
    return True


def _restore_unclaimed_slot(job: Dict[str, Any], scan: _DueScan) -> Optional[str]:
    """Put an occurrence the dispatcher advanced past but never claimed back on the schedule
    (#107485); returns the restored ``next_run_at`` or None. Restored ONCE: the stamp is dropped
    here, so the slot then meets the ordinary late / fast-forward / ``cron.catch_up_missed``
    policy like any other overdue instant — never a replay of every missed slot."""
    from cron.occurrences import unclaimed_pending_slot

    slot = unclaimed_pending_slot(job, scan.now)
    if slot is None:
        return None
    logger.warning(
        "Job '%s' (%s): occurrence %s was taken off the schedule but never claimed "
        "(scheduler stopped before dispatch); restoring it as the due instant (was %s).",
        job.get("name", job.get("id")), job.get("id"), slot, job.get("next_run_at"))
    job["next_run_at"] = slot
    job.pop("pending_slot", None)
    rj = scan.find(job["id"])
    if rj is not None:
        rj.pop("pending_slot", None)
    scan.persist(job["id"], next_run_at=slot)
    return slot


def _evaluate_due_job(job: Dict[str, Any], scan: _DueScan, run_claim_ttl: float) -> bool:
    """Decide whether one enabled, non-terminal job fires this tick, persisting any repairs.
    Ordering matters: recover missing next_run_at, repair timezone shifts, re-arm stale-error
    recurring jobs; then once due: re-anchor stale cron instants, fast-forward missed recurring
    runs, retire/guard one-shots, and finally stamp the run claim / dispatch record."""
    now = scan.now
    # Cross-process guard: another process's live one-shot run_claim (younger than TTL) — do NOT
    # re-dispatch. Malformed/future-dated claims (clock/TZ skew) count as stale, never eternally
    # fresh.
    if (
        job.get("schedule", {}).get("kind") == "once"
        and _claim_is_live(job.get("run_claim"), now, run_claim_ttl)
    ):
        return False

    next_run = _restore_unclaimed_slot(job, scan) or job.get("next_run_at") or _recover_missing_next_run(job, scan)
    if not next_run:
        return False
    raw_next_run_dt = datetime.fromisoformat(next_run)
    d = _DueJob(job, scan, next_run, raw_next_run_dt, _ensure_aware(raw_next_run_dt))
    kind = d.kind
    recurring = kind in {"cron", "interval"}
    # Intentionally string-exact on raw stored values: trigger_job stamps the SAME isoformat string
    # into both fields, and any rewrite of next_run_at (edit, re-anchor, fire-claim advance) must
    # invalidate the marker. Do not "fix" this with _ensure_aware normalization.
    manual_run = job.get("manual_run_at") == next_run
    from cron.occurrences import completed_occurrence, scheduled_instant

    if not manual_run and completed_occurrence(job, next_run):
        new_next = d.recompute_next() if recurring else None
        if new_next:
            scan.persist(job["id"], next_run_at=new_next)
        return False
    if kind == "cron" and not manual_run and _repair_timezone_shifted_cron(d):
        return False
    if not manual_run:  # a staggered restart re-fire is pending: leave its stamp alone
        d.next_run_dt = _rearm_stale_error_recurring(d)
    if _instant_after(d.next_run_dt, now):
        return False

    # Only the dispatch snapshot carries this field; never infer it from a later stamp.
    job["_scheduled_instant"] = None if manual_run else scheduled_instant(job.get("next_run_at"))

    if not manual_run and kind == "cron" and _reanchor_stale_cron(d):
        return False
    grace = _compute_grace_seconds(d.schedule)
    if not manual_run and recurring and _fast_forward_missed_recurring(d, grace):
        return False
    if kind == "once":
        if _retire_expired_oneshot(d, manual_run) or _oneshot_dispatch_limit_reached(job, scan):
            return False
        # Durably claim the one-shot for the DURATION of its run: a second scheduler process on the
        # same HERMES_HOME must not re-dispatch it while in flight, and advancing next_run_at by a
        # fixed window is not enough for a run that outlives a tick. The other process sees the
        # fresh claim and skips; mark_job_run() clears it. The TTL only covers a tick that DIES.
        claim = {"at": now.isoformat(), "by": _machine_id()}
        job["run_claim"] = claim
        scan.persist(job["id"], run_claim=claim)

    # Missed-run visibility: persist scheduled-vs-actual timing so separate CLI processes can show a
    # late catch-up. Recurring only — expired one-shots were retired above; manual triggers aren't
    # late.
    if not manual_run and recurring:
        lateness = max(0.0, _elapsed_seconds(now, d.next_run_dt))
        # See #99879.
        dispatch_stamp = {
            "scheduled_at": next_run,
            "dispatched_at": now.isoformat(),
            "lateness_seconds": round(lateness, 1),
            "kind": _classify_dispatch_lateness(lateness, grace),
        }
        job["last_dispatch"] = dispatch_stamp
        # The tick advances next_run_at past this occurrence before any fire claim exists; the
        # stamp survives a process death in that window so the slot is restored, not lost.
        from cron.occurrences import pending_slot_stamp

        scan.persist(
            job["id"], last_dispatch=dispatch_stamp,
            pending_slot=pending_slot_stamp(next_run, now))
    return True


def _get_due_jobs_locked() -> List[Dict[str, Any]]:
    """Inner implementation of get_due_jobs(); must be called with _jobs_lock held."""
    raw_jobs = load_jobs()
    scan = _DueScan(raw_jobs, _hermes_now())
    scan.needs_save = _normalize_due_scan_records(raw_jobs)
    jobs = [_apply_skill_fields(j) for j in copy.deepcopy(raw_jobs)]
    # One-shot run-claim TTL, resolved once per scan (see _oneshot_run_claim_ttl_seconds).
    run_claim_ttl = _oneshot_run_claim_ttl_seconds()

    # Retention sweep: completed one-shots are kept for inspection but must not accumulate forever.
    # The same GC pass reaps abandoned per-job fire-fence pathnames; a live holder makes the
    # non-blocking lock probe fail closed (#830).
    _reap_stale_fire_locks({job_id for job in raw_jobs if (job_id := job.get("id"))})
    if _sweep_completed_oneshots(raw_jobs, scan.now, removed_ids=scan.removed):
        scan.needs_save = True
        jobs = [j for j in jobs if scan.find(j.get("id")) is not None]

    due = []
    requeue_fired: List[Any] = []
    for job in jobs:
        # Per-job containment: one malformed record must never abort the whole scan. Normalization
        # above repairs known shapes; this catches FUTURE variants so healthy siblings still
        # run/persist.
        try:
            if is_terminal_job(job) and not _is_recoverable_error_job(job):
                continue
            if not job.get("enabled", True):
                continue
            if _has_pause_marker(job):
                _self_disable_half_paused(job, scan)
                continue
            # A run killed by the gateway restart drain re-fires once, staggered (#1301/#1548).
            if _apply_restart_requeue(job, raw_jobs, scan.now, requeue_fired):
                scan.needs_save = True
            if _evaluate_due_job(job, scan, run_claim_ttl):
                due.append(job)
        except Exception:
            logger.exception(
                "Skipping malformed cron job %r during due scan",
                job.get("name") or job.get("id") or "?")

    _warn_overdue_oneshots_not_due(jobs, due, scan.removed, scan.now)

    if scan.needs_save:
        save_jobs(raw_jobs, removed_ids=scan.removed or None)
    return due


# Last-warned monotonic time per one-shot id for _warn_overdue_oneshots_not_due.
_overdue_oneshot_warned: Dict[str, float] = {}
_OVERDUE_ONESHOT_WARN_INTERVAL_SECONDS = 600.0


def _warn_overdue_oneshots_not_due(
    jobs: List[Dict[str, Any]],
    due: List[Dict[str, Any]],
    removed_ids: Set[str],
    now: datetime,
) -> None:
    """Name the reason a past-due, never-run one-shot was left out of this scan.

    Every skip in the due-scan that can hold back a one-shot (disabled, terminal
    state, a live run/fire claim, a ``next_run_at`` parked after ``run_at``) is
    silent. On 2026-09-30 a 09:45 one-shot (28a5a721bb20) was skipped by every
    tick until it was removed by hand at 09:51, and nothing in the logs said why.
    This logs one WARNING per job per 10 min with the fields that decide it.
    """
    due_ids = {str(j.get("id")) for j in due}
    for job in jobs:
        try:
            schedule = job.get("schedule") or {}
            if schedule.get("kind") != "once" or job.get("last_run_at"):
                continue
            if job.get("state") == "paused" or job.get("paused_at"):
                continue  # operator pause: intentional, not a miss
            jid = str(job.get("id") or "")
            if not jid or jid in due_ids or jid in removed_ids:
                continue
            run_at = schedule.get("run_at") or job.get("next_run_at")
            if not run_at:
                continue
            overdue = (now - _ensure_aware(datetime.fromisoformat(run_at))).total_seconds()
            if overdue <= ONESHOT_GRACE_SECONDS:
                continue
            last = _overdue_oneshot_warned.get(jid)
            mono = time.monotonic()
            if last is not None and mono - last < _OVERDUE_ONESHOT_WARN_INTERVAL_SECONDS:
                continue
            _overdue_oneshot_warned[jid] = mono
            logger.warning(
                "cron.oneshot.overdue_not_due job='%s' id=%s run_at=%s overdue=%ds "
                "next_run_at=%s enabled=%s state=%s run_claim=%s fire_claim=%s "
                "manual_run_at=%s repeat=%s",
                job.get("name", jid),
                jid,
                run_at,
                int(overdue),
                job.get("next_run_at"),
                job.get("enabled", True),
                job.get("state"),
                job.get("run_claim"),
                job.get("fire_claim"),
                job.get("manual_run_at"),
                job.get("repeat"),
            )
        except Exception:
            logger.debug("overdue one-shot diagnostic failed", exc_info=True)


# --- Run output ---

# Per-run output files (`cron/output/<job>/<timestamp>.md`) are capped so a frequent job can't fill
# the disk.
# Unlike the quick-snapshot store (`hermes_cli.backup`, capped at 20) it had no retention, so a
# frequently-scheduled job on a long-running deploy accumulated one file per run forever and could fill the
# disk (#52383). Keep the most recent N files per job; a non-positive value disables pruning (opt-out).
_CRON_OUTPUT_DEFAULT_KEEP = 50


def _cron_output_keep() -> int:
    """Per-job output-file retention cap (``cron.output_retention``)."""
    return _cron_config_number("output_retention", _CRON_OUTPUT_DEFAULT_KEEP, int)


def _prune_job_output(job_output_dir: Path, keep: int) -> int:
    """Remove the oldest ``*.md`` run-output files beyond *keep*; returns count deleted. Filenames
    are timestamps, so a reverse lexical sort is newest-first. Non-positive *keep* disables pruning;
    failures are swallowed so they can never break output saving."""
    if keep <= 0:
        return 0
    try:
        files = sorted(
            (f for f in job_output_dir.glob("*.md") if f.is_file()),
            key=lambda f: f.name, reverse=True)
    except OSError:
        return 0
    deleted = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            deleted += 1
        except OSError as exc:
            logger.debug("Failed to prune cron output %s: %s", stale.name, exc)
    return deleted


def save_job_output(job_id: str, output: str):
    """Save job output to file."""
    ensure_dirs()
    job_output_dir = _job_output_dir(job_id)
    _ensure_cron_dir(job_output_dir)
    _secure_dir(job_output_dir)
    output_file = job_output_dir / f"{_hermes_now().strftime('%Y-%m-%d_%H-%M-%S')}.md"
    atomic_write_text(output_file, output, tmp_prefix=".output_", mode=0o600)
    _secure_file(output_file)
    # Bound per-job output growth so long-running deploys don't fill the disk (#52383).
    _prune_job_output(job_output_dir, _cron_output_keep())
    return output_file


# --- Skill reference rewriting (curator integration) ---

def _canonical_skill_ref(raw: Any) -> str:
    """Reduce a job skill reference (possibly an absolute path) to the bare name the curator matches
    on, resolving as the scheduler does — otherwise a path-referencing job's skill looks
    unreferenced and gets archived. Falls back to plain cleanup so a broken import can never lose
    a name."""
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        from agent.skill_utils import normalize_skill_lookup_name
        value = normalize_skill_lookup_name(value) or value
    except Exception:
        logger.debug("referenced_skill_names: could not normalize skill ref %r", raw, exc_info=True)
    return value.strip().lstrip("/")


def referenced_skill_names() -> Set[str]:
    """Skill names referenced by ANY cron job, deliberately including paused/disabled ones (resuming
    must still find them); the curator protects these from inactivity archival. Canonicalized as the
    scheduler does, so absolute paths are protected too. A corrupt store yields an empty set."""
    try:
        jobs = load_jobs()
    except Exception:
        logger.debug("referenced_skill_names: failed to load cron jobs", exc_info=True)
        return set()
    return {
        cleaned
        for job in jobs
        if isinstance(job, dict)
        for name in _normalize_skill_list(job.get("skill"), job.get("skills"))
        if (cleaned := _canonical_skill_ref(name))
    }


def rewrite_skill_refs(
    consolidated: Optional[Dict[str, str]] = None, pruned: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Rewrite cron job skill references after a curator consolidation pass (a job listing a
    consolidated/pruned skill would otherwise run without it). Consolidated names map to their
    umbrella target without duplication, pruned names are dropped, ordering is preserved, and the
    legacy ``skill`` field is realigned. Returns ``{"rewrites": [{job_id, job_name, before,
    after, mapped, dropped}, ...], "jobs_updated": N, "jobs_scanned": M}``. Load/save exceptions
    propagate."""
    consolidated = dict(consolidated or {})
    # A skill listed in both wins as "consolidated" — it has a target, the more useful outcome.
    pruned_set = set(pruned or []) - set(consolidated.keys())
    if not consolidated and not pruned_set:
        return {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0}

    with _jobs_lock():
        jobs = load_jobs()
        rewrites: List[Dict[str, Any]] = []
        for job in jobs:
            skills_before = _normalize_skill_list(job.get("skill"), job.get("skills"))
            if not skills_before:
                continue
            mapped: Dict[str, str] = {}
            dropped: List[str] = []
            new_skills: List[str] = []
            for name in skills_before:
                if name in consolidated:
                    target = consolidated[name]
                    mapped[name] = target
                    if target and target not in new_skills:
                        new_skills.append(target)
                elif name in pruned_set:
                    dropped.append(name)
                elif name not in new_skills:
                    new_skills.append(name)
            if not mapped and not dropped:
                continue
            job["skills"] = new_skills
            job["skill"] = new_skills[0] if new_skills else None
            rewrites.append({
                "job_id": job.get("id"),
                "job_name": job.get("name") or job.get("id"),
                "before": list(skills_before),
                "after": list(new_skills),
                "mapped": mapped,
                "dropped": dropped,
            })
        if rewrites:
            save_jobs(jobs)
            logger.info("Curator rewrote skill references in %d cron job(s)", len(rewrites))
        return {"rewrites": rewrites, "jobs_updated": len(rewrites), "jobs_scanned": len(jobs)}
