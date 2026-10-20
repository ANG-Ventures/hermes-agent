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


def _release_fire(job_id: str, fire_owner: Optional[str], next_run_at: Optional[str],
                  claimed_schedule: Any = None) -> bool:
    """Undo the claim-time bookkeeping of a run that never started; status fields untouched.

    The due slot goes back only while the stored schedule is still the claimed one: a schedule edited
    during the run already recomputed next_run_at, and an old-schedule instant would fire it at once
    after the restart (Prism 6d15940f4ae6). The claim is released either way."""
    from cron import jobs as cj

    def apply(jobs, _i, job):
        claim = job.get("fire_claim")
        # Only this run's own live claim is released: a missing owner, or a claim that is gone or
        # held by another run, means a newer tick owns the job and its next_run_at.
        if fire_owner is None or not isinstance(claim, dict) or claim.get("by") != fire_owner:
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
        elif next_run_at and (claimed_schedule is None or job.get("schedule") == claimed_schedule):
            job["next_run_at"] = next_run_at
        cj.save_jobs(jobs)
        return True

    return bool(cj._under_fire_fence(job_id, lambda: cj._with_job(job_id, apply, False)))


def monitor_checkpoint(job: dict) -> Optional[tuple]:
    """Monitor state before the gate runs, for a job whose pre-run script may still be refused:
    ``check_monitor`` commits a change before the script starts, and a refused run must not
    consume it (the next process would see "unchanged" and never run the agent)."""
    from cron.monitor import _read_last_output, job_has_monitor

    if not (job.get("script") and job_has_monitor(job)):
        return None
    return job.get("monitor_state"), _read_last_output(str(job["id"]))


def restore_monitor_checkpoint(job: dict, checkpoint: Optional[tuple]) -> None:
    if checkpoint is None:
        return
    from cron.monitor import _write_last_output

    from cron import jobs as cj

    state, last_output = checkpoint
    job_id = str(job["id"])
    claim = job.get("fire_claim")
    owner = claim.get("by") if isinstance(claim, dict) else None
    if not owner:
        return  # no claim of ours to vouch for: never roll back state another run may own

    def apply(jobs, _i, stored):
        # fenced + owner-checked: a run that took the claim over may already have processed a newer
        # change, and rolling its monitor state back would replay it (Prism daa521022dcb)
        held = stored.get("fire_claim")
        if not isinstance(held, dict) or held.get("by") != owner:
            return False
        _write_last_output(job_id, last_output)
        stored["monitor_state"] = state
        cj.save_jobs(jobs)
        return True

    try:
        if not cj._under_fire_fence(job_id, lambda: cj._with_job(job_id, apply, False)):
            logger.info("Job '%s': monitor checkpoint not restored (claim no longer ours)", job_id)
    except Exception:
        logger.warning("Job '%s': monitor checkpoint restore failed", job_id, exc_info=True)


def record_shutdown_skip(job: dict, fire_owner: Optional[str], execution_id: str) -> bool:
    """Bookkeeping for a run refused by the shutdown gate: no error status, no page.

    Only when this process put the occurrence back (the local due-scan path carries the slot and
    still owns the claim). Otherwise returns False and the caller records the ordinary failure:
    a refused run is never dropped silently (provider/webhook fires carry no slot to restore).
    """
    from cron.scheduler import finish_execution
    from hermes_cli.observability.shared_metrics_gateway import note_cron_skipped

    job_id = str(job["id"])
    restored = job.get(DUE_NEXT_RUN_KEY)
    if restored is None:
        return False
    try:
        released = _release_fire(job_id, fire_owner, restored, job.get("schedule"))
    except Exception:
        logger.warning("Job '%s': shutdown skip could not release its fire", job_id, exc_info=True)
        released = False
    if not released:
        return False
    logger.info(
        "PHASE=cron_shutdown_skip job=%s: not started, scheduler is shutting down; "
        "left for the next process (next_run_at=%s)", job.get("name") or job_id, restored)
    note_cron_skipped(job)
    finish_execution(
        execution_id, success=False,
        error="Skipped: cron scheduler is shutting down; left for the next process.")
    return True
