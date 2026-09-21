"""The ``/resume-handoff`` gateway command.

Companion to :mod:`agent.turn_handoff`. The turn-cut notice tells the user to
send ``/resume-handoff``; this renders the reply for that command by consuming
the saved handoff and returning its context, or saying plainly that there is
nothing to resume.

Kept in its own module (rather than inline in ``gateway.slash_commands``) so
the behaviour is testable without constructing a gateway runner.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agent.turn_handoff import consume_handoff_context

logger = logging.getLogger(__name__)

NO_HANDOFF_REPLY = (
    "No saved handoff for this chat. A handoff is written only when a turn is "
    "cut by an unrecoverable provider failure, and expires after 24 hours."
)


def render_resume_handoff_reply(agent, *, root: Optional[Path] = None) -> str:
    """Return the reply body for ``/resume-handoff``.

    Consumes the handoff on success (once-only, same as the automatic
    next-turn injection). Never raises — a broken handoff must not break the
    command that exists to recover from a broken turn.
    """
    try:
        context = consume_handoff_context(agent, root=root)
    except Exception:
        logger.debug("resume-handoff render failed", exc_info=True)
        return NO_HANDOFF_REPLY
    if not context:
        return NO_HANDOFF_REPLY
    return context
