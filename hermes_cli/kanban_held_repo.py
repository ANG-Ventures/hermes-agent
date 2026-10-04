"""The repo-hold wait flag a card carries while its PR sits behind ``fleet-merge --hold`` (t_9d046260).

The fleet lander (hermes-home ``scripts/fleet_merge_hold.py``) answers a land of a held repo with rc=27 and
posts ONE comment on the card::

    [fleet-merge] HELD-REPO <owner/repo#n> @ <hold_id>: ⏸ held behind <owner/repo#held> until <ISO-Z> · resume-on: ...

and ``[fleet-merge] HOLD-RELEASED <hold_id>`` when the hold lifts. ``kanban show`` prints the flag still in force
so an operator sees why a green review card is not landing. Writer and the other readers: hermes-home
``scripts/lib/held_lane.py``; this parser accepts the same shape.
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from typing import Iterable, Optional

_FLAG = re.compile(
    r"^\s*\[fleet-merge\] HELD-REPO (?P<pr>\S+#\d+) @ (?P<hold>\S+): ⏸ held behind (?P<held>\S+#\d+) "
    r"until (?P<until>\S+)"
)
_RELEASED = re.compile(r"^\s*\[fleet-merge\] HOLD-RELEASED (?P<hold>\S+)")


def held_repo(bodies: Iterable[str], now: Optional[float] = None) -> Optional[dict]:
    """``bodies`` oldest first -> {'pr', 'hold', 'held', 'until'} for the newest flag not released and not past its
    expiry, else None. An unparseable expiry is treated as not in force."""
    now = time.time() if now is None else now
    flag, released = None, set()
    for body in bodies:
        m = _FLAG.match(body or "")
        if m:
            flag = m.groupdict()
            continue
        r = _RELEASED.match(body or "")
        if r:
            released.add(r.group("hold"))
    if not flag or flag["hold"] in released:
        return None
    try:
        expires = _dt.datetime.strptime(flag["until"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None
    return flag if now < expires else None


def fmt_held_repo(flag: dict) -> str:
    return f"{flag['pr']} behind {flag['held']} until {flag['until']} ({flag['hold']})"
