"""Exact scheduled identities, independent of mutable jobs.json dispatch stamps."""
from datetime import datetime, timedelta, timezone
import logging

logger = logging.getLogger(__name__)


def scheduled_instant(value):
    """Canonicalize aware instants; legacy/ambiguous values carry no exact identity."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            return None
        return instant.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


class LedgerUnavailable(Exception):
    """The executions ledger could not be read, so it proves nothing either way.

    Distinct from "this slot never ran": 2026-10-23 an EMFILE made every ledger open fail, the
    gate read that as not-completed, and nine completed one-shots were retired as MISSED and
    paged (t_9b3b6e4b). Callers decide per kind: a recurring job may still fire (a duplicate
    beats a silent skip); a one-shot must neither fire nor be retired."""


def completed_occurrence(job, instant):
    """True when *instant* itself completed. Unknown/failed/pruned attempts cannot prove completion:
    keep them eligible. A store rewound past a completed LATER occurrence is
    ``rewound_watermark``'s job, not this one's. Raises ``LedgerUnavailable`` when the ledger
    cannot be read."""
    return completed_occurrence_at(job, instant) is not None


def completed_occurrence_at(job, instant):
    """``completed_occurrence`` returning the proving row's finish stamp (or the slot when the row
    carries none) instead of True; None when the slot is not proven completed."""
    from cron.executions import _transaction

    instant = scheduled_instant(instant)
    if instant is None:
        return None
    try:
        with _transaction() as conn:
            rows = conn.execute(
                "SELECT id, finished_at, claimed_at FROM executions "
                "WHERE job_id=? AND scheduled_instant=? "
                "AND status='completed'", (str(job['id']), instant)
            ).fetchall()
        for row in rows:
            if proves_slot(row["finished_at"] or row["claimed_at"], instant):
                # Both dedup gates (due scan and fire claim) consume the slot on True without a
                # run or a ledger row, so this line is the only trace the skip leaves (#111414).
                logger.warning(
                    "Job '%s' (%s): scheduled occurrence %s was already completed by execution "
                    "%s (finished %s); skipping the due slot without a new run",
                    job.get("name", job.get("id")), job.get("id"), instant, row["id"],
                    row["finished_at"] or row["claimed_at"])
                return row["finished_at"] or row["claimed_at"] or instant
        return None
    except Exception as exc:
        logger.warning("Cannot check completed occurrence for job %s", job['id'], exc_info=True)
        raise LedgerUnavailable(str(exc)) from exc


def proves_slot(completed_at, slot):
    """A completion proves *slot* ran unless it is positively identified as poison: recorded more
    than FIRE_CLAIM_SKEW_SECONDS before the slot it claims (a skewed early fire legitimately
    completes just before it). Legacy/malformed stamps stay proof. The ONE predicate for every
    reader and for ledger retention (``executions._prune_unlocked``): a row retention keeps must be
    a row these readers accept, or a rejected poison row displaces the real watermark."""
    from cron.constants import FIRE_CLAIM_SKEW_SECONDS

    completed_at = scheduled_instant(completed_at)
    return completed_at is None or datetime.fromisoformat(completed_at) >= (
        datetime.fromisoformat(slot) - timedelta(seconds=FIRE_CLAIM_SKEW_SECONDS))


def rewound_watermark(job, instant):
    """Newest validly completed occurrence of *job* LATER than the stored *instant*, or None.
    Recurring and one-shot alike: a one-shot rescheduled later and completed, then restored from
    an older snapshot, has already run.

    Non-None means the store's schedule went backwards: jobs.json was replaced by an older snapshot
    (a hand "level" from git, a restore). 2026-10-05 22:44 that fired 402 catch-ups, one per daily
    job that had already run that day. The caller resumes the schedule AFTER the watermark rather
    than skipping to now, so an occurrence genuinely missed after the watermark still catches up."""
    from cron.executions import _transaction

    instant = scheduled_instant(instant)
    if instant is None:
        return None
    try:
        with _transaction() as conn:
            rows = conn.execute(
                "SELECT id, scheduled_instant, finished_at, claimed_at FROM executions "
                "WHERE job_id=? AND status='completed' AND scheduled_instant IS NOT NULL "
                "AND julianday(scheduled_instant) > julianday(?) "
                "ORDER BY julianday(scheduled_instant) DESC", (str(job['id']), instant)
            ).fetchall()
    except Exception as exc:
        logger.warning("Cannot check rewound schedule for job %s", job['id'], exc_info=True)
        raise LedgerUnavailable(str(exc)) from exc
    for row in rows:
        slot = scheduled_instant(row["scheduled_instant"])
        if slot is None or not proves_slot(row["finished_at"] or row["claimed_at"], slot):
            continue
        logger.warning(
            "cron.registry_rewound job='%s' id=%s: stored occurrence %s is older than occurrence "
            "%s, which execution %s already completed; resuming the schedule after it (jobs.json "
            "was rewound to an older snapshot?)",
            job.get("name", job.get("id")), job.get("id"), instant, slot, row["id"])
        return slot
    return None


# --- Rewound-store burst: one tick saw many cron.registry_rewound jobs ---
#
# 2026-10-23 16:20:05 one tick logged 422 cron.registry_rewound WARNINGs after a whole-tree
# `git checkout -- .` put an older jobs.json over the live store. The scheduler handled each job
# correctly (resume after the watermark), but the log was the only record and nobody saw it.
# max(job.updated_at) did not move (old blob and live store shared the same newest row), so the
# per-tick count is the signal. The tick writes this marker; hermes-home
# scripts/cron-jobs-integrity-watch.py pages #alerts once per marker (t_3d311bd1).
REWOUND_BURST_THRESHOLD = 20
REWOUND_BURST_MARKER = "registry_rewound_burst.json"
REWOUND_BURST_SAMPLE = 10


def rewound_burst_payload(job_ids, tick_at):
    """Marker body when *job_ids* (rewound in ONE tick) reach the threshold, else None."""
    ids = [str(i) for i in job_ids]
    if len(ids) < REWOUND_BURST_THRESHOLD:
        return None
    logger.error(
        "cron.registry_rewound_burst: %d jobs in one tick at %s were behind a completed occurrence "
        "(jobs.json replaced by an older snapshot?); marker %s, sample %s",
        len(ids), tick_at, REWOUND_BURST_MARKER, ",".join(ids[:REWOUND_BURST_SAMPLE]))
    return {"count": len(ids), "tick_at": tick_at, "threshold": REWOUND_BURST_THRESHOLD,
            "sample_ids": ids[:REWOUND_BURST_SAMPLE]}


# --- Pending slot: the occurrence a tick took off the schedule but has not yet claimed ---
#
# The tick advances a recurring job's ``next_run_at`` BEFORE dispatch (at-most-once: a crash
# mid-run must not re-fire on every restart). That leaves a window — advance done, fire claim
# not yet taken (interpreter finalizing, executor refusing work, process killed) — in which the
# process exiting loses the occurrence silently: the restarted scan sees a future ``next_run_at``
# and nothing ever ran (#107485). ``pending_slot`` is the durable record of that window: the due
# scan stamps it with the exact stored instant plus the stamping owner, ``claim_job_for_fire``
# (the point after which side effects may exist) clears it, and any explicit rewrite of the
# schedule drops it. A slot still pending once its owner is provably gone (or its lease has
# expired) was never claimed, so it is restored ONCE as the due instant and then flows through
# the ordinary late / fast-forward / ``cron.catch_up_missed`` policy — never N replays.

def pending_slot_stamp(next_run, now):
    """Store value for a recurring occurrence about to be handed to the dispatcher."""
    from cron.jobs import _machine_id

    return {"scheduled_at": next_run, "at": now.isoformat(), "by": _machine_id()}


def unclaimed_pending_slot(job, now):
    """Stored instant of a slot the dispatcher never claimed, or None.

    None for non-recurring jobs and malformed stamps (never a fire) and for a job still in this
    process's running set (its queued worker will claim and clear the slot itself). A stamp by
    THIS process on a job not running here is orphaned (dispatch refused). A stamp by another
    process is honoured while that owner may still be alive within the fire-claim lease — a
    second live gateway on the same store is mid-dispatch, not dead."""
    from cron.constants import FIRE_CLAIM_TTL_SECONDS
    from cron.jobs import _claim_is_live, _job_running_in_this_process, _machine_id

    pending = job.get("pending_slot")
    if not isinstance(pending, dict):
        return None
    slot = pending.get("scheduled_at")
    if job.get("schedule", {}).get("kind") not in {"cron", "interval"} or not isinstance(slot, str):
        return None
    try:
        datetime.fromisoformat(slot)
    except ValueError:
        return None
    if _job_running_in_this_process(str(job.get("id", ""))):
        return None
    if pending.get("by") != _machine_id() and _claim_is_live(pending, now, FIRE_CLAIM_TTL_SECONDS):
        return None
    return slot
