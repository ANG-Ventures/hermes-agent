"""Where each incoming message from a person goes: one INFO line per disposition (card t_1eb0a176, Step 0).

Logging only. Nothing here changes what is delivered, queued, held, released, refused or in what
order; it reads no database, starts no thread and stores nothing. A line carries ids, a queue depth
and a session key, never message text. :func:`log_disposition` cannot raise into the message path:
any error inside it is dropped.

Line shape::

    inbox_disposition disposition=<d> session=<key> platform=<p> msg_id=<id|-> depth=<n|-> path=<where>
    [into=<head id>]

``<d>``: ``steered`` (into the running turn), ``queued`` (a new queue head or FIFO entry),
``held`` (kept behind an earlier queued message), ``merged-into-head`` (joined the queued head, or
a held release merged it into one turn: ``into`` names the head's id), ``refused`` (not taken), or
``became-next-turn`` (a turn starts on it). The held store keeps its own per-item lines
(``queue_mgmt item op=...``). Internal wakes write no line.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def log_disposition(disposition: str, session_key: Any, event: Any, *, depth: Any = None,
                    path: str = "-", into: Optional[Any] = None) -> None:
    """One INFO line for *event* when it is a person's message. *depth* may be a callable, read here
    so a failing depth read is dropped with the line instead of reaching the caller."""
    try:
        if event is None or getattr(event, "internal", False) is True:
            return
        platform = getattr(getattr(event, "source", None), "platform", None)
        platform = str(getattr(platform, "value", platform) or "-")
        mid = getattr(event, "message_id", None)
        if callable(depth):
            depth = depth()
        logger.info(
            "inbox_disposition disposition=%s session=%s platform=%s msg_id=%s depth=%s path=%s%s",
            disposition, session_key, platform, "-" if mid in (None, "") else mid,
            "-" if depth is None else depth, path, "" if into is None else f" into={into}",
        )
    except Exception:
        pass
