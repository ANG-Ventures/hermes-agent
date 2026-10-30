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

import json
import logging
import subprocess
import sys
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


def record_pending_requeues(items: List[tuple]) -> None:
    """``[(home, job_id)]`` in flight at a trip, for the restarted runner. Merged with any still
    pending. Never raises."""
    if not items:
        return
    try:
        path = _requeue_path()
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = []
        merged = {(str(h), str(j)) for h, j in (pending if isinstance(pending, list) else [])}
        merged |= {(str(h), str(j)) for h, j in items}
        from utils import atomic_write_text

        atomic_write_text(path, json.dumps(sorted(merged)) + "\n",
                          tmp_prefix=".script_runner_requeue_", mode=0o600)
    except Exception:
        logger.error("script runner: pending re-queue write failed", exc_info=True)


def apply_pending_requeues() -> int:
    """Persist one re-fire per job killed by the last trip (``request_restart_requeue``, capped at
    ``RESTART_REQUEUE_MAX``), then drop the file. A job that fails to re-queue is logged, not
    retried: the cap means a retry could not add a second re-fire anyway. Returns re-queued."""
    path = _requeue_path()
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        pending = []
    from cron.jobs import request_restart_requeue
    from cron.scheduler_provider import _profile_cron_scope

    done = 0
    for item in pending if isinstance(pending, list) else []:
        try:
            home, job_id = item
            with _profile_cron_scope(Path(home)):
                if request_restart_requeue(str(job_id), "script killed by the runner watchdog"):
                    done += 1
                    logger.warning("Job '%s': killed by the runner watchdog; re-queued to fire "
                                   "once", job_id)
        except Exception:
            logger.warning("watchdog re-queue of %r failed", item, exc_info=True)
    try:
        path.unlink()
    except OSError:
        pass
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
