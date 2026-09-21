"""Append-only lifecycle journal for the cron job store + its vanished-job guard.

Why this exists
---------------
``cron/jobs.json`` is a whole-file store. Every writer loads it, mutates an
in-memory copy and writes the whole thing back, so a lost job leaves **no
trace at all**: the record is simply absent from the next write, and the
store's own history is one file with no prior versions. On 2026-09-20 three
jobs vanished (or lost their run state) and the only way to tell "somebody
removed it" from "the store ate it" was to reconstruct intent from chat
scrollback.

``cron/jobs.py``'s by-id merge closes the race that caused those losses. This
module closes the *observability* half: an append-only journal of every
intentional create and remove, plus a guard that answers the one question the
store cannot answer by itself —

    a job is gone; did anyone actually ask for that?

The journal is written at the store's own choke points (``create_job`` and the
``removed_ids`` argument that every intentional deletion already threads
through ``save_jobs``), so it cannot drift from the store the way a log
scraper would: a removal that does not pass ``removed_ids`` is refused by the
shrink-merge guard, and therefore cannot happen silently.

Failure policy: journal writes are best-effort and never propagate. Losing an
audit record must not be able to break a cron write.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

JOURNAL_FILENAME = "lifecycle.jsonl"

# Reconciliation covers only journalled creates inside this lookback. Older
# and unjournalled jobs are NOT certified, even when status is ok.
DEFAULT_WINDOW_HOURS = 24.0

# Entries older than this are pruned during guard checks to limit growth.
# Deliberately several times the default guard window: an entry must stay
# readable for the whole window even if nothing writes for days afterwards.
_RETENTION_DAYS = 14.0

EVENT_CREATED = "created"
EVENT_REMOVED = "removed"


def _journal_path() -> Path:
    from cron.jobs import _current_cron_store

    return _current_cron_store().cron_dir / JOURNAL_FILENAME


@contextlib.contextmanager
def _journal_lock(path: Path):
    """Serialize append and prune on a stable inode, never the replaced file.

    Unlike the jobs lock's availability-first fallback, audit writes must
    fail closed on timeout: an unlocked prune can erase another writer.
    """
    from cron.jobs import _ensure_cron_dir

    _ensure_cron_dir(path.parent)
    with open(path.parent / ".lifecycle.lock", "a+b") as lock:
        if os.name == "nt":
            import msvcrt

            def acquire():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + 5.0
        while True:
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("cron lifecycle journal lock unavailable")
                time.sleep(0.05)
        try:
            yield
        finally:
            release()


def _ends_with_newline(path: Path) -> bool:
    """True when the journal's last byte is a record terminator.

    A process killed between ``write`` and its newline leaves a partial record
    with no terminator. The next append would then FUSE onto it, producing one
    malformed line — and losing BOTH the torn record and the new event, which
    is the opposite of what an append-only audit log is for.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return True
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"
    except OSError:
        return True  # unreadable: the append below will fail loudly enough


def _append(record: Dict[str, Any]) -> None:
    """Append one record. Best effort — never raises into a cron write."""
    try:
        path = _journal_path()
        with _journal_lock(path):
            # Separator-before-record, under the lock: heals a torn tail so the
            # new event lands on its own parseable line. The torn remnant stays
            # on disk as an unparseable line — deliberately: the guard reports
            # it rather than silently swallowing evidence of a crash.
            prefix = "" if _ends_with_newline(path) else "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(prefix + json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        logger.debug("cron lifecycle journal append failed", exc_info=True)


def record_created(job_id: str, *, name: Optional[str] = None,
                   actor: Optional[str] = None) -> None:
    """Journal an intentional job creation."""
    if not job_id:
        return
    _append({
        "event": EVENT_CREATED,
        "job_id": str(job_id),
        "name": name,
        "actor": actor or _default_actor(),
        "at": _hermes_now().isoformat(),
    })


def record_removed(job_id: str, *, reason: Optional[str] = None,
                   actor: Optional[str] = None) -> None:
    """Journal an intentional job removal."""
    if not job_id:
        return
    _append({
        "event": EVENT_REMOVED,
        "job_id": str(job_id),
        "reason": reason,
        "actor": actor or _default_actor(),
        "at": _hermes_now().isoformat(),
    })


def _default_actor() -> str:
    """Best-effort writer identity: which process asked for this."""
    return f"{os.path.basename(os.environ.get('HERMES_ACTOR', '') or 'hermes')}:{os.getpid()}"


def read_entries(*, window_hours: float = DEFAULT_WINDOW_HOURS) -> List[Dict[str, Any]]:
    """Return journal entries inside the window, oldest first (append order).

    Each entry carries a ``_seq`` ordinal: its position in the file. The
    journal is append-only under ``_journal_lock``, so file order IS append
    order — a causally-correct sequence that does not depend on any writer's
    wall clock.

    Malformed lines are skipped rather than failing the read: the journal is
    append-only from multiple processes, so a torn final line is expected
    after a crash and must not blind the guard to everything before it. The
    count of skipped lines is reported by ``read_entries_with_health`` so the
    guard can refuse to certify what it could not parse.
    """
    entries, _ = read_entries_with_health(window_hours=window_hours)
    return entries


def read_entries_with_health(
    *, window_hours: float = DEFAULT_WINDOW_HOURS,
) -> tuple[List[Dict[str, Any]], int]:
    """``(entries, malformed_line_count)`` — see ``read_entries``.

    ``malformed_line_count`` counts every non-empty line that is not a valid
    lifecycle record, ANYWHERE in the file (not only inside the window): the
    record must be an object with a recognized event, a non-empty string
    ``job_id``, and a usable timestamp. A record we cannot validate is a record
    whose timestamp or lifecycle effect we cannot trust to place inside or
    outside the window.
    """
    path = _journal_path()
    if not path.exists():
        return [], 0
    cutoff = _hermes_now() - timedelta(hours=window_hours)
    entries: List[Dict[str, Any]] = []
    malformed = 0
    with open(path, "r", encoding="utf-8") as f:
        for seq, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError("lifecycle record is not an object")
                event = rec.get("event")
                if not isinstance(event, str) or event not in (
                    EVENT_CREATED, EVENT_REMOVED
                ):
                    raise ValueError("lifecycle record has an unknown event")
                job_id = rec.get("job_id")
                if not isinstance(job_id, str) or not job_id.strip():
                    raise ValueError("lifecycle record has no usable job_id")
                at = _parse_at(rec.get("at"))
            except Exception:
                malformed += 1
                continue
            if at is None:
                malformed += 1
                continue
            if at < cutoff:
                continue
            rec["_seq"] = seq
            entries.append(rec)
    return entries, malformed


def _parse_at(raw: Any):
    from datetime import datetime, timezone

    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def prune() -> int:
    """Drop entries past retention. Returns the number of entries removed.

    Rewrites via a temp file + atomic replace so a concurrent reader never
    sees a half-written journal. Best effort: a failure leaves the journal
    intact and oversized, which is strictly better than losing audit records.
    """
    path = _journal_path()
    if not path.exists():
        return 0
    try:
        with _journal_lock(path):
            return _prune_locked(path)
    except Exception:
        logger.debug("cron lifecycle journal prune failed", exc_info=True)
        return 0


def _prune_locked(path: Path) -> int:
    """Read and replace while holding the same mutex as every appender."""
    cutoff = _hermes_now() - timedelta(days=_RETENTION_DAYS)
    kept: List[str] = []
    dropped = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    at = _parse_at(json.loads(stripped).get("at"))
                except Exception:
                    kept.append(stripped)  # unparseable: keep, never guess away
                    continue
                if at is not None and at < cutoff:
                    dropped += 1
                else:
                    kept.append(stripped)
        if not dropped:
            return 0
        from utils import atomic_replace

        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lifecycle_",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
        return dropped
    except Exception:
        logger.debug("cron lifecycle journal prune failed", exc_info=True)
        return 0


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_VANISHED = "vanished"
STATUS_UNAVAILABLE = "unavailable"

# The largest window any caller may ask for. Beyond retention the journal has
# already dropped entries, so a wider window would report `ok` over data that
# was pruned rather than data that was reconciled.
MAX_WINDOW_HOURS = _RETENTION_DAYS * 24.0


@dataclass
class VanishedJobReport:
    """Result of one expected-vs-present reconciliation.

    ``status`` is one of:

    * ``ok`` — every job created inside the window is either still present in
      ``jobs.json`` or has a matching removal record. Nothing to page.
    * ``vanished`` — at least one job was created, never removed, and is
      absent from the store. This is the incident shape; page #alerts.
    * ``unavailable`` — the journal or the store could not be read, so the
      reconciliation could not be performed. Explicitly NOT ``ok``: a guard
      that cannot see must never report green (it would have certified the
      very incident it exists to catch).
    """

    status: str
    vanished: List[Dict[str, Any]] = field(default_factory=list)
    created_count: int = 0
    removed_count: int = 0
    present_count: int = 0
    detail: Optional[str] = None
    window_hours: float = DEFAULT_WINDOW_HOURS

    @property
    def should_alert(self) -> bool:
        return self.status != STATUS_OK

    def summary(self) -> str:
        if self.status == STATUS_UNAVAILABLE:
            return f"cron vanished-job guard UNAVAILABLE: {self.detail}"
        if self.status == STATUS_OK:
            return (
                f"cron vanished-job guard: {self.created_count} journalled creates / "
                f"{self.removed_count} removals in last {self.window_hours:g}h; "
                f"{self.present_count} present in store; "
                "no losses detected among those creates (older jobs not checked; "
                "unjournalled jobs not covered)"
            )
        names = ", ".join(
            f"{v['job_id']}({v.get('name') or '?'})" for v in self.vanished
        )
        return (
            f"cron jobs VANISHED with no matching removal: {names} — "
            f"created in the last window and absent from jobs.json"
        )


def check_vanished_jobs(
    *,
    window_hours: float = DEFAULT_WINDOW_HOURS,
) -> VanishedJobReport:
    """Reconcile journalled creates against what jobs.json actually holds.

    A job counts as vanished when it was created inside the window, has no
    removal record **appended after** that creation, and is not in the store.
    The "after" ordering is taken from the journal's own append sequence, not
    from wall-clock timestamps: creates and removes are appended by different
    processes (and, on a fleet, different hosts), so a skewed or non-monotonic
    clock could otherwise invert causality and report an intentionally-removed
    job as lost. Ordering by append position matters for the legitimate
    create → remove → re-create-under-the-same-id sequence.

    The reconciliation runs **inside the jobs lock**, so the journal read and
    the store read observe one coherent snapshot. Reading them independently
    lets a create that lands between the two reads look like a job that was
    journalled and never stored.

    Any unparseable journal line makes the result ``unavailable``: a record we
    could not read may be the very create whose loss we exist to catch, so
    ``ok`` would be a guess, not a finding.

    Always read the unfiltered store. Caller snapshots may exclude disabled
    jobs and cannot establish absence. This is a window-limited check, not
    a lifetime integrity certificate.
    """
    if window_hours > MAX_WINDOW_HOURS:
        return VanishedJobReport(
            status=STATUS_UNAVAILABLE,
            window_hours=window_hours,
            detail=(
                f"window {window_hours:g}h exceeds journal retention "
                f"{MAX_WINDOW_HOURS:g}h — entries older than retention have "
                "already been pruned, so this window cannot be reconciled"
            ),
        )

    try:
        import cron.jobs as jobs_module
        from cron.jobs import _jobs_lock, load_jobs

        # One coherent snapshot: no create/remove can land between the two
        # reads, because every intentional mutation takes this same lock.
        # Save/restore the section's load stamp: _jobs_lock is reentrant, so
        # if an outer mutation section ever calls the guard, our read_jobs
        # must not overwrite the baseline that section's save-path merge
        # depends on.
        with _jobs_lock():
            _state = jobs_module._jobs_lock_state
            _saved = (getattr(_state, "load_stamp", None),
                      getattr(_state, "load_baseline", None))
            try:
                entries, malformed = read_entries_with_health(
                    window_hours=window_hours)
                jobs = load_jobs()
            finally:
                _state.load_stamp, _state.load_baseline = _saved
    except Exception as e:
        return VanishedJobReport(status=STATUS_UNAVAILABLE,
                                 window_hours=window_hours,
                                 detail=f"snapshot unreadable: {e}")

    if malformed:
        return VanishedJobReport(
            status=STATUS_UNAVAILABLE,
            window_hours=window_hours,
            detail=(
                f"{malformed} invalid or unparseable journal record(s) — the "
                "guard cannot certify a store it could not fully read"
            ),
        )

    present = {
        str(j["id"]) for j in jobs
        if isinstance(j, dict) and j.get("id")
    }

    # Latest create (by append sequence), and every removal's sequence, per id.
    created: Dict[str, Dict[str, Any]] = {}
    removed: Dict[str, List[int]] = {}
    for rec in entries:
        jid = rec.get("job_id")
        seq = rec.get("_seq")
        if not jid or not isinstance(seq, int):
            continue
        jid = str(jid)
        if rec.get("event") == EVENT_CREATED:
            prior = created.get(jid)
            if prior is None or seq >= prior["_seq"]:
                created[jid] = rec
        elif rec.get("event") == EVENT_REMOVED:
            removed.setdefault(jid, []).append(seq)

    vanished: List[Dict[str, Any]] = []
    for jid, rec in created.items():
        if jid in present:
            continue
        if any(r > rec["_seq"] for r in removed.get(jid, [])):
            continue  # removed on purpose after this create
        vanished.append({
            "job_id": jid,
            "name": rec.get("name"),
            "created_at": rec.get("at"),
            "actor": rec.get("actor"),
            "seq": rec["_seq"],
        })

    vanished.sort(key=lambda v: v["seq"])
    # Opportunistic retention: the guard is the one caller that has already
    # paid for a full journal read, so pruning here costs a rewrite only when
    # something is actually past retention, and never sits on a write path.
    prune()
    return VanishedJobReport(
        status=STATUS_VANISHED if vanished else STATUS_OK,
        vanished=vanished,
        created_count=len(created),
        removed_count=sum(len(v) for v in removed.values()),
        present_count=len(present),
        window_hours=window_hours,
    )


__all__ = [
    "DEFAULT_WINDOW_HOURS",
    "EVENT_CREATED",
    "EVENT_REMOVED",
    "JOURNAL_FILENAME",
    "MAX_WINDOW_HOURS",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "STATUS_VANISHED",
    "VanishedJobReport",
    "check_vanished_jobs",
    "prune",
    "read_entries",
    "read_entries_with_health",
    "record_created",
    "record_removed",
]
