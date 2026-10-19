"""A run refused at script start because the scheduler is shutting down is a SKIP, not a failure.

fork #1307 stops the ticker scanning once the drain begins, but a job dispatched in the last tick
reaches ``_run_job_script`` after the flag flips and is refused there. Recording that refusal as
a failed run paged "Cronjob Failed" for every such job (5 pages at the 2026-10-19 14:15 restart)
and consumed the occurrence a whole period. Instead the run releases its fire claim, puts the due
slot back (a restart re-queue fire keeps its run-now stamp) and leaves it for the next process.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("cron.scheduler")

# Key the run's due-scan ``next_run_at`` rides on from the tick to the run body.
DUE_NEXT_RUN_KEY = "_due_next_run_at"

_lock = threading.Lock()
_refused: set = set()


def _key(job_id: Any) -> tuple:
    from cron.scheduler import _inflight_key

    return _inflight_key(str(job_id))


def note_shutdown_refused(job_id: Any) -> None:
    """``_run_job_script`` refused to start this job's script: the scheduler is draining."""
    if job_id:
        with _lock:
            _refused.add(_key(job_id))


def is_shutdown_refused(job_id: Any) -> bool:
    with _lock:
        return _key(job_id) in _refused


def consume_shutdown_refused(job_id: Any) -> bool:
    key = _key(job_id)
    with _lock:
        if key in _refused:
            _refused.discard(key)
            return True
    return False


def _release_fire(job_id: str, fire_owner: Optional[str], next_run_at: Optional[str]) -> bool:
    """Undo the claim-time bookkeeping of a run that never started; status fields untouched."""
    from cron import jobs as cj

    def apply(jobs, _i, job):
        if fire_owner is not None:
            claim = job.get("fire_claim")
            if not isinstance(claim, dict) or claim.get("by") != fire_owner:
                return False
        job["fire_claim"] = None
        if job.get("run_claim") is not None:
            job["run_claim"] = None
        schedule = job.get("schedule") or {}
        if schedule.get("kind") == "once":
            # claim_dispatch spent one finite dispatch before the script was refused.
            repeat = job.get("repeat") or {}
            if repeat.get("times") and int(repeat.get("completed") or 0) > 0:
                repeat["completed"] = int(repeat["completed"]) - 1
        elif next_run_at:
            job["next_run_at"] = next_run_at
        cj.save_jobs(jobs)
        return True

    return bool(cj._under_fire_fence(job_id, lambda: cj._with_job(job_id, apply, False)))


def record_shutdown_skip(job: dict, fire_owner: Optional[str], execution_id: str) -> None:
    """Bookkeeping for a run refused by the shutdown gate: no error status, no page."""
    from cron.scheduler import finish_execution
    from hermes_cli.observability.shared_metrics_gateway import note_cron_skipped

    job_id = str(job["id"])
    restored = job.get(DUE_NEXT_RUN_KEY)
    try:
        released = _release_fire(job_id, fire_owner, restored)
    except Exception:
        logger.warning("Job '%s': shutdown skip could not release its fire", job_id, exc_info=True)
        released = False
    logger.info(
        "PHASE=cron_shutdown_skip job=%s: not started, scheduler is shutting down; "
        "left for the next process (next_run_at=%s, released=%s)",
        job.get("name") or job_id, restored, released)
    note_cron_skipped(job)
    finish_execution(
        execution_id, success=False,
        error="Skipped: cron scheduler is shutting down; left for the next process.")
