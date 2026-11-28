"""Where each incoming message from a person goes: one INFO line per disposition (card t_1eb0a176, Step 0).

Logging only. Nothing here changes what is delivered, queued, held, released, refused or in what
order; it reads no database, starts no thread and stores nothing. A line carries ids, a queue depth
and a session key, never message text. Neither function here can raise into the message path: any
error inside them is dropped.

Line shape::

    inbox_disposition disposition=<d> session=<key> platform=<p> msg_id=<id|-> depth=<n|-> path=<where>
    [into=<id>]

``<d>`` is one of:

- ``steered``: folded into the running turn.
- ``queued``: became the queue head (an empty pending slot) or a FIFO entry.
- ``held``: kept behind an earlier queued message (the same message's ``queued`` line follows; take
  the last line per ``msg_id``). Not the /stop held store, which keeps its own ``queue_mgmt item
  op=...`` lines.
- ``merged-into-head``: joined the queued head, which keeps the head's id; or a held release merged
  it into one turn, and ``into`` names the head's id.
- ``replaced-head``: took the pending slot in place of an earlier message, which is gone; ``into``
  names the replaced message's id.
- ``refused``: not taken.
- ``interrupt-requested``: its text was handed to the running turn as an interrupt. Whether a turn
  then runs on it is not logged here.
- ``requeued-leftover-steer``: steered text the turn never read was put back as the next turn's input
  (``path=leftover_steer``). It has no single platform id, so ``msg_id`` is ``-``.
- ``became-next-turn``: a turn starts on it.

Internal wakes write no line.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def log_disposition(disposition: str, session_key: Any, event: Any, *, depth: Any = None,
                    path: str = "-", into: Optional[Any] = None, source: Any = None) -> None:
    """One INFO line for *event* when it is a person's message. *depth* may be a callable, read here
    so a failing depth read is dropped with the line instead of reaching the caller. With
    ``event=None``, *source* logs an event-less line (``msg_id=-``); the caller skips internal ones."""
    try:
        if event is None:
            if source is None:
                return
        elif getattr(event, "internal", False) is True:
            return
        platform = getattr(source if event is None else getattr(event, "source", None), "platform", None)
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


def log_slot_write(session_key: Any, event: Any, before: Any, slots: Any, *, depth: Any = None,
                   path: str = "-") -> None:
    """The line for one ``merge_pending_message_event`` call, decided from the pending slot *before*
    the call and *slots* after it: ``queued`` (the slot was empty), ``merged-into-head`` (the earlier
    head is still there and absorbed *event*) or ``replaced-head`` (*event* now sits where the earlier
    head was; ``into`` names that head)."""
    try:
        after = slots.get(session_key)
        if before is None:
            disposition, into = "queued", None
        elif after is before:
            disposition, into = "merged-into-head", None
        else:
            mid = getattr(before, "message_id", None)
            disposition, into = "replaced-head", "-" if mid in (None, "") else mid
    except Exception:
        return
    log_disposition(disposition, session_key, event, depth=depth, path=path, into=into)
