"""ONE comment on a card a STRUCTURAL placement rule has held too long (t_641d1ab8).

2026-10-19: t_b904be5a (p85) and t_7beef6f4 (p70) sat ready 16 h / 13 h with 60
and 48 ``placement_wait not_portable:host_reach`` events. The rule was right to
hold them and wrong forever: nothing about the board would change until someone
edited the body, and nobody was told what to edit. A capacity wait (pool full,
local full, a hot pin) clears on its own and is not reported here; a structural
one (the card's own body or skills decide it) is.

After ``STARVED_WAIT_SECONDS`` of one wait episode, or ``STARVED_WAIT_EVENTS``
placement_wait rows with the same code in it, a ready card with priority >=
``STARVED_MIN_PRIORITY`` gets ONE dispatcher comment naming the rule and the
one-line body fix, plus a ``placement_starved`` event that latches it. The latch
lives on the board (the event), so a gateway restart never re-posts; a new
episode (claimed, blocked, edited and requeued, ...) can be reported again.

Below the priority floor a hold is still reported once it repeats
``STARVED_REPEAT_EVENTS`` placement_wait rows with the same code (t_250a7fc5:
t_041ad167, p2..45, sat 20 h on ``not_portable:linked`` with nobody told).
Every report also sends ONE #logs line.
"""

from __future__ import annotations

import json
import sys
from typing import Mapping, Optional

STARVED_WAIT_SECONDS = 30 * 60
STARVED_WAIT_EVENTS = 3
STARVED_MIN_PRIORITY = 50
# Any priority: this many same-code rows (~1 h at the 900 s refresh) reports.
STARVED_REPEAT_EVENTS = 4
STARVED_EVENT = "placement_starved"
COMMENT_AUTHOR = "kanban-dispatcher"

# Code prefix -> the fix. First match wins; a code no entry names is a capacity
# or plan-wide wait and is never reported here.
_FIXES = (
    ("not_portable:host_reach",
     "the card is host-bound (a host-bound skill, or an ssh-shaped target in the body) and no "
     "pool host's fresh reach-matrix row proves rc 0 to every target. Fix: add ONE body line "
     "naming the host the work drives, `ssh <host>` (e.g. `ssh ace-ai`), or a `host: <pool host>` "
     "pin. A target no pool host reaches can only run on the Studio: pin `host: studio`."),
    ("requires_unsatisfied:",
     "no host that may run it advertises the capability its `requires:` line (or body) names; a "
     "`+studio_needs` suffix means a Studio-only resource word in the body (vault, skills tree, "
     "relay, launchd) also pins it to the Studio. Fix: state the real need on its own line "
     "(`requires: <capability>` or `requires: none`), or pin the host that serves it."),
    ("requires:",
     "the capability the card requires is on no pool host it may use. Fix: correct the "
     "`requires:` line, or pin the host that serves it."),
    ("not_portable:linked",
     "a link to a live card (named in the detail) holds it off the pool, so its `host:` pin "
     "is not honoured while that edge is live. Fix: drop the pin, or relink the edge "
     "`--kind derived-from` if it is lineage only."),
    ("not_portable:",
     "a placement rule keeps it off every host that has room. Fix: read `kanban placement <id>` "
     "and correct the body line that rule names."),
)


def fix_for(code: Optional[str]) -> Optional[str]:
    """The one-line fix for a structural wait code, else None (capacity wait)."""
    for prefix, fix in _FIXES:
        if code and code.startswith(prefix):
            return fix
    return None


def _code(payload: Mapping) -> Optional[str]:
    from hermes_cli.kanban_placement_view import wait_code
    return wait_code(payload)


def starved_holds(conn, waits: Mapping[str, str], now: int, *,
                  min_seconds: int = STARVED_WAIT_SECONDS,
                  min_events: int = STARVED_WAIT_EVENTS,
                  min_priority: int = STARVED_MIN_PRIORITY,
                  repeat_events: int = STARVED_REPEAT_EVENTS) -> list[dict]:
    """``[{task_id, code, since, waited, events, fix}]``: held cards to report
    this tick. One indexed read per structurally-held ready card; below
    ``min_priority`` only ``repeat_events`` same-code rows report it.
    Capacity waits cost nothing."""
    from hermes_cli import kanban_placement_reasons as _pr

    ends = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    hits: list[dict] = []
    for tid, reason in (waits or {}).items():
        code = _code({"reason": reason})
        fix = fix_for(code)
        if fix is None:
            continue
        row = conn.execute("SELECT status, priority FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is None or row[0] != "ready":
            continue
        high = int(row[1] or 0) >= min_priority
        count, start, latched = 0, None, False
        for kind, created, raw in conn.execute(
            "SELECT kind, created_at, payload FROM task_events WHERE task_id = ? "
            "AND kind IN (?, ?, %s) ORDER BY id DESC" % ",".join("?" * len(ends)),
            (tid, _pr.PLACEMENT_WAIT_EVENT, STARVED_EVENT, *ends),
        ):
            if kind not in (_pr.PLACEMENT_WAIT_EVENT, STARVED_EVENT):
                break  # episode end
            try:
                pl = json.loads(raw or "{}")
            except (TypeError, ValueError):
                pl = {}
            pl = pl if isinstance(pl, dict) else {}
            if kind == STARVED_EVENT:
                latched = latched or pl.get("code") == code
                continue
            if pl.get("cleared") or _code(pl) != code:
                break
            count += 1
            since = pl.get("since")
            start = min(x for x in (start, int(created or now),
                                    since if isinstance(since, int) else None) if x is not None)
        if latched or start is None:
            continue
        waited = now - start
        if (high and (waited >= min_seconds or count >= min_events)) or count >= repeat_events:
            hits.append({"task_id": tid, "code": code, "since": start, "waited": waited,
                         "events": count, "fix": fix})
    return hits


def comment_body(hit: Mapping) -> str:
    return (f"⏳ placement: held `{hit['code']}` for {hit['waited'] // 60} min "
            f"({hit['events']} placement_wait events) and it will not clear on its own: "
            f"{hit['fix']} Then `kanban placement {hit['task_id']}` shows the new route.")


def notify_starved_holds(conn, waits: Mapping[str, str], now: int) -> list[dict]:
    """Post the comment + latch event for each new hit; returns the hits posted."""
    from hermes_cli import kanban_db as _kb

    posted = []
    for h in starved_holds(conn, waits, now):
        try:
            with _kb.write_txn(conn):
                _kb.add_comment(conn, h["task_id"], COMMENT_AUTHOR, comment_body(h))
                _kb._append_event(conn, h["task_id"], STARVED_EVENT, {
                    k: h[k] for k in ("code", "since", "waited", "events")})
        except Exception as exc:  # one bad card never stops the rest
            _kb._log.warning("kanban placement-starved comment on %s failed: %s", h["task_id"], exc)
            continue
        _kb._log.warning("PHASE=kanban_placement_starved %s held %s %d min",
                         h["task_id"], h["code"], h["waited"] // 60)
        _logs_line(h)
        posted.append(h)
    return posted


def _logs_line(hit: Mapping) -> None:
    """ONE #logs line per report (t_250a7fc5). The board event already latched
    the episode, so a failed send is not retried. A hermetic home has no
    notify.py and sends nothing."""
    from hermes_cli import kanban_budget as _kbudget

    script = _kbudget._notify_script_path()
    if script is None:
        return
    body = (f"⏳ kanban placement: {hit['task_id']} held `{hit['code']}` "
            f"{hit['waited'] // 60} min ({hit['events']} placement_wait events); "
            f"will not clear on its own, fix named on the card.")
    _kbudget._run_notify([sys.executable, script, "--channel", "discord",
                          "--target", _kbudget.RECOVERY_TARGET, "--send", body])
