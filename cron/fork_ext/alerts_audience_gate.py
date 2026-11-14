"""Cron deliveries to the fleet #alerts channel take notify.py's audience split (t_6a4c207a).

notify.py sends an agent-audience #alerts line to the agent inbox instead of the channel
(t_231f81c4; contract: ``<fleet root>/scripts/lib/agent_inbox.py`` + ``fleet/notify-audience.json``).
Cron deliveries never pass through notify.py, so 87 enabled rows still posted straight to #alerts
(measured 2026-11-14, first hour of the split). This gate makes the SAME decision at the
scheduler's one delivery choke point, with producer ``cron:<job name>``.

Agent audience: ``Inbox.record()`` and the line is not posted. Everything else posts as before:
  * human audience (producer listed in human_producers as ``cron:<name>`` or the bare job name, a
    human_markers hit on the first line, NOTIFY_AUDIENCE=human);
  * the kill switch (state/notify-audience.DISABLED, NOTIFY_AUDIENCE_SPLIT=0);
  * a thread target or attachments (the inbox holds text only);
  * no fleet root holding the library (any non-fleet install), or any error (fail open).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ALERTS_CHAT = "1480528231286181948"  # fleet #alerts
_LIB_REL = Path("scripts") / "lib" / "agent_inbox.py"


def _fleet_root(home: Path) -> Optional[Path]:
    for cand in (home, *home.parents):
        if (cand / _LIB_REL).is_file():
            return cand
    return None


def _load_lib(root: Path):
    spec = importlib.util.spec_from_file_location("_fleet_agent_inbox", root / _LIB_REL)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def maybe_inbox(job: dict, *, platform: str, chat_id, thread_id, success: bool,
                has_media: bool, text: str, get_home: Callable[[], Path]) -> bool:
    """True = the delivery was written to the agent inbox (do not post it)."""
    try:
        if str(platform).lower() != "discord" or str(chat_id) != ALERTS_CHAT:
            return False
        if thread_id or has_media or not (text or "").strip():
            return False
        root = _fleet_root(Path(get_home()))
        if root is None:
            return False
        lib = _load_lib(root)
        if lib is None or lib.split_disabled(root):
            return False
        cfg = lib.load_config(root)
        name = str(job.get("name") or job.get("id") or "unknown")
        producer = "cron:" + name
        if "human" in (lib.audience(producer, text, cfg), lib.audience(name, text, cfg)):
            return False
        item = lib.Inbox(lib.inbox_dir(root)).record(
            producer, "warn" if success else "error", text)
        logger.info("Job '%s': #alerts delivery to the agent inbox (item %s)",
                    job.get("id"), item.get("id"))
        return True
    except Exception as e:  # a page is never dropped
        logger.warning("Job '%s': agent inbox gate failed, posting to #alerts: %r", job.get("id"), e)
        return False
