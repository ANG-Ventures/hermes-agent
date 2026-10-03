"""Cron deliveries to the fleet #logs channel ride the #logs digest spool (t_42a9c32b).

notify.py spools #logs receipts into ``<fleet root>/state/logs-digest/spool.jsonl`` while the
4 h flusher is alive (contract: ``<fleet root>/scripts/lib/logs_digest.py``, hermes-home #2487).
Cron deliveries never pass through notify.py, so 81 enabled rows posted straight to #logs and
the spool never saw them (24 h census 2026-10-03: 55 cron-footer lines). This gate applies the
SAME contract at the scheduler's one delivery choke point.

Spooled: a SUCCESSFUL run of a ``no_agent`` job whose target is the #logs channel (no thread,
no attachments, not a host-down demotion). Everything else posts at once, as before:
  * failed runs (the house page; same rule as notify.py's sev error/critical);
  * agent rows (morning-digest, daily-journal, weekly-review, ...): deliberate long-form reports,
    which a one-line-per-producer digest would cut to a 110-char sample;
  * any delivery while the spool is DISARMED (no flusher heartbeat in 5 h, a ``disabled`` file,
    NOTIFY_LOGS_DIGEST=0), or when no fleet root holds the library (any non-fleet install).
Fail-open: any error here returns False and the line posts directly. A spooled line is never
lost: the flusher archives its full text and posts one digest per window.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

LOGS_CHAT = "1480525090331561984"  # fleet #logs (== logs_digest.LOGS_CHANNEL)
_LIB_REL = Path("scripts") / "lib" / "logs_digest.py"


def _fleet_root(home: Path) -> Optional[Path]:
    for cand in (home, *home.parents):
        if (cand / _LIB_REL).is_file():
            return cand
    return None


def _load_lib(root: Path):
    spec = importlib.util.spec_from_file_location("_fleet_logs_digest", root / _LIB_REL)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def maybe_spool(job: dict, *, platform: str, chat_id, thread_id, success: bool,
                has_media: bool, demoted: bool, text: str,
                get_home: Callable[[], Path]) -> bool:
    """True = the delivery was appended to the #logs digest spool (do not post it)."""
    try:
        if str(platform).lower() != "discord" or str(chat_id) != LOGS_CHAT:
            return False
        if thread_id or has_media or demoted or not success or not job.get("no_agent"):
            return False
        if not (text or "").strip():
            return False
        root = _fleet_root(Path(get_home()))
        if root is None:
            return False
        lib = _load_lib(root)
        if lib is None or str(getattr(lib, "LOGS_CHANNEL", "")) != LOGS_CHAT:
            return False
        d = lib.state_dir(root)
        if not lib.armed(d):
            return False
        producer = "cron:" + str(job.get("name") or job.get("id") or "unknown")
        lib.spool(d, producer, "info", text)
        return True
    except Exception as e:  # the digest must never cost a line
        logger.warning("Job '%s': #logs digest spool failed, posting now: %r", job.get("id"), e)
        return False
