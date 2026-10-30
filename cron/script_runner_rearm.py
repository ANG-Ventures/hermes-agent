"""Watchdog trip history, self re-arm policy and operator notices for the cron script runner.

t_302ab1e8: a trip used to park the script lane on the gateway until an operator ran
``hermes cron runner rearm`` (one marker sat on disk 20 h), and the only page was the killed jobs'
own rc=-15 failures. Now:

* every trip appends one line to ``<process home>/cron/script_runner_trips.jsonl``: when, the
  blocked phase and the ledger lock holder (``cron.executions.ledger_activity``);
* the runner re-arms itself ``RUNNER_REARM_AFTER_S x trips-in-24h`` after the trip;
* the first ADMITTED tick after a re-arm posts one #logs receipt;
* the second trip inside ``RUNNER_TRIP_WINDOW_S`` pages #alerts through the fleet's episode pager.

Notices start the fleet's own senders (``scripts/notify.py``, ``scripts/lib/episode_pager.py``)
under the running root as a detached process, so the watchdog's ``os._exit`` cannot take one with
it. A missing sender (a hermetic home, a non-fleet install) is a logged no-op; nothing here raises
into the runner.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cron import lanes

logger = logging.getLogger(__name__)

TRIPS_NAME = "script_runner_trips.jsonl"
LOGS_CHAT = "1480525090331561984"  # fleet #logs
PRODUCER = "cron-script-runner-watchdog"
_KEEP_LINES = 200


def trips_path() -> Path:
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home() / "cron" / TRIPS_NAME


def record_trip(entry: Dict[str, Any]) -> None:
    """Append one trip line (trimmed to the last ``_KEEP_LINES``). Never raises."""
    try:
        path = trips_path()
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines = lines[-(_KEEP_LINES - 1):] + [json.dumps(entry, sort_keys=True, default=str)]
        from utils import atomic_write_text

        atomic_write_text(path, "\n".join(lines) + "\n", tmp_prefix=".script_runner_trips_",
                          mode=0o600)
    except Exception:
        logger.error("script runner: trip history write failed", exc_info=True)


def recent_trips(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Trips whose ``epoch`` is inside the window. Unreadable lines are skipped."""
    now = time.time() if now is None else now
    try:
        text = trips_path().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            if now - float(entry["epoch"]) <= lanes.RUNNER_TRIP_WINDOW_S:
                out.append(entry)
        except (ValueError, KeyError, TypeError):
            continue
    return out


def rearm_delay_s(trips_in_window: int) -> float:
    return float(lanes.RUNNER_REARM_AFTER_S) * max(1, int(trips_in_window))


REQUEUE_NAME = "script_runner_requeue.json"


def _requeue_path() -> Path:
    return trips_path().with_name(REQUEUE_NAME)


def _write_pending(path: Path, items) -> None:
    from utils import atomic_write_text

    atomic_write_text(path, json.dumps(sorted(items)) + "\n",
                      tmp_prefix=".script_runner_requeue_", mode=0o600)


def _pending_items(raw) -> Dict[tuple, float]:
    """``{(home, job_id): trip_epoch}`` from the file's list. A pre-epoch ``[home, job_id]`` row
    gets epoch 0 (no completion can be reconciled against it, so it re-fires as before)."""
    out: Dict[tuple, float] = {}
    for item in raw if isinstance(raw, list) else []:
        try:
            home, job_id, *rest = item
            key = (str(home), str(job_id))
            out[key] = max(out.get(key, 0.0), float(rest[0]) if rest else 0.0)
        except (TypeError, ValueError):
            logger.warning("script runner: dropping malformed pending re-queue %r", item)
    return out


_pending_tlock = threading.Lock()


@contextlib.contextmanager
def _pending_lock(path: Path, timeout: float = 5.0):
    """Serialize read-modify-write of the pending file across threads and processes (the tripped
    runner records while a restarted one may be replaying). Bounded: on flock timeout it proceeds
    in-process-only and logs, because the recording side runs on the watchdog's way to exit."""
    with _pending_tlock:
        fd = None
        try:
            import fcntl
        except ImportError:  # Windows: in-process lock only
            fcntl = None
        if fcntl is not None:
            try:
                fd = open(path.with_name(path.name + ".lock"), "a+")
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            logger.error("script runner: pending re-queue lock timed out; "
                                         "proceeding unlocked")
                            break
                        time.sleep(0.05)
            except OSError:
                logger.error("script runner: pending re-queue lock unavailable", exc_info=True)
        try:
            yield
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()


def _read_pending(path: Path) -> Dict[tuple, float]:
    try:
        return _pending_items(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}


def record_pending_requeues(items: List[tuple], trip_epoch: Optional[float] = None) -> None:
    """``[(home, job_id)]`` in flight at a trip, for the restarted runner, stamped with the trip
    time so the replay can skip a job that finished cleanly after the snapshot. Merged with any
    still pending, under ``_pending_lock``. Never raises."""
    if not items:
        return
    trip_epoch = time.time() if trip_epoch is None else float(trip_epoch)
    try:
        path = _requeue_path()
        with _pending_lock(path):
            pending = _read_pending(path)
            for h, j in items:
                pending[(str(h), str(j))] = trip_epoch
            _write_pending(path, [[h, j, e] for (h, j), e in pending.items()])
    except Exception:
        logger.error("script runner: pending re-queue write failed", exc_info=True)


def _finished_after(job_id: str, trip_epoch: float) -> bool:
    """True when the job already re-fires (a pending marker) or recorded a clean run at or after
    the trip snapshot: the kill raced its completion, so a replay would run it twice."""
    from cron.jobs import RESTART_REQUEUE_KEY, _parse_aware, get_job

    job = get_job(job_id)
    if job is None:
        return False
    if job.get(RESTART_REQUEUE_KEY):
        return True
    last = _parse_aware(job.get("last_run_at"))
    return (bool(trip_epoch) and last is not None and last.timestamp() >= trip_epoch
            and job.get("last_status") in ("ok", "delivery_failed"))


def apply_pending_requeues() -> int:
    """Persist one re-fire per job killed by the last trip (``request_restart_requeue``, capped at
    ``RESTART_REQUEUE_MAX``). A job that completed cleanly after the snapshot, or already carries a
    re-fire marker, is skipped. Entries whose re-queue RAISED stay in the file for the next start;
    a refusal (cap, unknown or one-shot job) is final. An unreadable file is left in place.
    Returns re-queued."""
    path = _requeue_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except OSError:
        logger.error("script runner: pending re-queue read failed; kept for the next start",
                     exc_info=True)
        return 0
    try:
        pending = _pending_items(json.loads(text))
    except ValueError:
        bad = path.with_name(path.name + ".bad")
        logger.error("script runner: pending re-queue file is not JSON; moved to %s", bad)
        with contextlib.suppress(OSError):
            path.replace(bad)
        return 0
    from cron.jobs import request_restart_requeue
    from cron.scheduler_provider import _profile_cron_scope

    done = 0
    resolved: Dict[tuple, float] = {}
    for (home, job_id), trip_epoch in pending.items():
        try:
            with _profile_cron_scope(Path(home)):
                if _finished_after(job_id, trip_epoch):
                    logger.warning("Job '%s': finished or already re-queued after the watchdog "
                                   "snapshot; not replayed", job_id)
                elif request_restart_requeue(job_id, "script killed by the runner watchdog"):
                    done += 1
                    logger.warning("Job '%s': killed by the runner watchdog; re-queued to fire "
                                   "once", job_id)
            resolved[(home, job_id)] = trip_epoch
        except Exception:
            logger.warning("watchdog re-queue of %s failed; kept for the next start", job_id,
                           exc_info=True)
    # Commit against the file as it is NOW: drop only what this replay resolved, so a row
    # recorded (or re-stamped by a newer trip) while the replay ran survives.
    try:
        with _pending_lock(path):
            current = _read_pending(path)
            for key, epoch in resolved.items():
                if key in current and current[key] <= epoch:
                    del current[key]
            if current:
                _write_pending(path, [[h, j, e] for (h, j), e in current.items()])
            else:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
    except (OSError, ValueError):
        logger.error("script runner: pending re-queue rewrite failed", exc_info=True)
    return done


def trip_summary(entry: Dict[str, Any]) -> str:
    holder = (entry.get("ledger") or {}).get("lock_holder") or {}
    held = (f"; ledger lock held by {holder.get('thread')} in {holder.get('label')} "
            f"for {holder.get('held_s')} s") if holder else ""
    return f"tick blocked in {entry.get('phase') or 'unknown'}{held}"


def _script(rel: str) -> Optional[Path]:
    from hermes_constants import get_default_hermes_root

    path = get_default_hermes_root() / rel
    return path if path.is_file() else None


def _detach(argv_tail: List[str], rel: str, what: str) -> bool:
    """Start the fleet sender in its own session and return: the watchdog's ``os._exit`` must not
    take the notice with it. True when it was started."""
    script = _script(rel)
    if script is None:
        logger.warning("script runner %s skipped: %s not found", what, rel)
        return False
    try:
        subprocess.Popen([sys.executable, str(script), *argv_tail], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        logger.warning("script runner %s dispatched via %s", what, rel)
        return True
    except Exception:
        logger.error("script runner %s failed", what, exc_info=True)
        return False


def post_logs(text: str) -> bool:
    return _detach(["--channel", "discord", "--target", LOGS_CHAT, "--send", text],
                   "scripts/notify.py", "#logs receipt")


def page_alerts(text: str) -> bool:
    return _detach(["page", "--producer", PRODUCER, "--key", "tripped", "--text", text,
                    "--resolve-text", "✅ cron script runner re-armed itself and ticked under "
                                      "budget; watchdog episode closed"],
                   "scripts/lib/episode_pager.py", "#alerts page")


def clear_alerts() -> bool:
    """Close the pager episode (``quiet`` when none is open)."""
    return _detach(["clear", "--producer", PRODUCER, "--key", "tripped"],
                   "scripts/lib/episode_pager.py", "#alerts episode clear")
