"""Cron deliveries to the fleet #logs channel take the #logs audience split (t_2b76e2d8).

Ace's ruling (Discord 1571854816139944097, 2026-11-16): a line only an agent can use goes to disk.
notify.py sends an agent-audience #logs line to ``state/agent-logs`` (t_ca89a948; contract:
``<fleet root>/scripts/lib/agent_inbox.py`` write_agent_log + ``fleet/notify-audience.json``).
Cron deliveries never pass through notify.py, so 123 enabled rows still posted to #logs (measured
2026-11-16, t_22fb8bc4). This gate makes the SAME decision at the scheduler's one delivery choke
point, with producer ``cron:<job name>``.

Agent audience: one ``write_agent_log`` row and the line is not posted. A FAILED run also gets an
``Inbox.record`` item (sev error), as an #alerts row does. Everything else posts as before:
  * human audience: the row sets ``"audience": "human"``; its producer (``cron:<name>`` or the bare
    name) is in ``human_logs_producers``; or the shared ``audience()`` says human (human_producers,
    human_markers, agreed #logs lines, NOTIFY_AUDIENCE=human);
  * no ``human_logs_producers`` list in the config (the gate is armed by that list, so the human
    rows are named before anything is diverted);
  * the kill switch (state/notify-logs-audience.DISABLED, NOTIFY_LOGS_AUDIENCE_SPLIT=0);
  * a thread target, attachments, or a host-down demoted copy;
  * no fleet root holding the library, a library without write_agent_log, or any error (fail open).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from cron.fork_ext.alerts_audience_gate import _fleet_root, _load_lib

logger = logging.getLogger(__name__)

LOGS_CHAT = "1480525090331561984"  # fleet #logs (== logs_digest_gate.LOGS_CHAT)


def maybe_agent_log(job: dict, *, platform: str, chat_id, thread_id, success: bool,
                    has_media: bool, demoted: bool, text: str,
                    get_home: Callable[[], Path]) -> bool:
    """True = the delivery was written to the agent log (do not post it)."""
    return logs_audience(job, platform=platform, chat_id=chat_id, thread_id=thread_id,
                         success=success, has_media=has_media, demoted=demoted, text=text,
                         get_home=get_home) == "agent"


def logs_audience(job: dict, *, platform: str, chat_id, thread_id, success: bool,
                  has_media: bool, demoted: bool, text: str,
                  get_home: Callable[[], Path]) -> str | None:
    """'agent' = written to the agent log (do not post it); 'human' = post now and skip the #logs
    digest spool (as notify.py's _logs_audience_gate, t_b3cb7cf7); None = not a #logs line, the
    split is off, or the gate failed (today's path)."""
    try:
        if str(platform).lower() != "discord" or str(chat_id) != LOGS_CHAT:
            return None
        if thread_id or has_media or demoted or not (text or "").strip():
            return None
        root = _fleet_root(Path(get_home()))
        if root is None:
            return None
        lib = _load_lib(root)
        if lib is None or not hasattr(lib, "write_agent_log") or lib.logs_split_disabled(root):
            return None
        cfg = lib.load_config(root)
        human_logs = cfg.get("human_logs_producers")
        if not isinstance(human_logs, list):
            return None
        if str(job.get("audience") or "").strip().lower() == "human":
            return "human"
        name = str(job.get("name") or job.get("id") or "unknown")
        producer = "cron:" + name
        if producer in human_logs or name in human_logs:
            return "human"
        if "human" in (lib.audience(producer, text, cfg), lib.audience(name, text, cfg)):
            return "human"
        lib.write_agent_log(root, producer, "info" if success else "error", text)
        if not success:
            item = lib.Inbox(lib.inbox_dir(root)).record(producer, "error", text)
            logger.info("Job '%s': failed #logs delivery to the agent inbox (item %s)",
                        job.get("id"), item.get("id"))
        logger.info("Job '%s': #logs delivery written to the agent log", job.get("id"))
        return "agent"
    except Exception as e:  # a line is never dropped
        logger.warning("Job '%s': #logs audience gate failed, posting to #logs: %r", job.get("id"), e)
        return None
