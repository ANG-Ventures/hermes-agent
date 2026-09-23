"""Tell a boot-resumed session WHY the gateway was restarted (fork).

Incident 2026-09-20 16:01 (Apollo). The event loop was blocked ~30s inside
``utils.py:230`` ``atomic_replace`` writing ``sessions.json``; the loop-liveness
watchdog fired ``os._exit(75)`` from its own thread at 16:01:08. launchd
relaunched the gateway at 16:04:16 and three Discord sessions were boot-resumed
(``reason=restart_interrupted``). Their replies landed 16:08-16:09 with **no
explanation of the seven-minute silence**.

The existing user-facing notice
(``gateway/run.py::_INTERRUPT_REASON_GATEWAY_RESTART = "Gateway restarting"``)
is emitted only from the GRACEFUL drain path -- SIGTERM, ``/restart``, a safe
restart. An ``os._exit`` from a watchdog thread runs *no* drain, so there is no
in-process moment left in which to speak. The only surface that can still
explain the gap is the **next boot**, on the resume path, immediately before the
resumed turn runs.

This module owns the three decidable pieces of that, kept out of
``gateway/run.py`` so they are unit-testable without constructing a runner:

* :func:`classify_prior_life` -- did the PREVIOUS life end uncleanly, and what
  do we know about how? Reads the lifecycle sentinel
  (``gateway/lifecycle_ledger.py``) which already carries the killer/sender
  attribution (``prior_killer`` / ``prior_kill_sender`` /
  ``prior_kill_sender_label``) and the watchdog's own ``mark_exited`` record.
* :func:`format_restart_notice` -- ONE short line, or ``None`` when the prior
  life exited cleanly (the drain already told them).
* :func:`claim_restart_notice` -- a durable once-per-session-per-boot claim, so
  a re-scheduled resume or a crash loop cannot spam a channel.

Everything here is best-effort and read-mostly: a notice failure must never
delay boot or take the resumed turn down with it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Only these boot-resume reasons mean "we killed a live turn and the user is
# owed an explanation". A CLEAN self-restart (``restart_consumed``) already
# told the session what was happening before it went down.
UNCLEAN_NOTICE_RESUME_REASONS = frozenset(
    {
        "restart_interrupted",
        "reboot_interrupted",
        "restart_timeout",
        "shutdown_timeout",
    }
)

# Exit reasons recorded by the two ``os._exit`` sites in
# gateway/shutdown_watchdog.py. These write ``phase=exited`` via
# ``mark_exited`` -- so ``detect_unclean_exit`` (which keys on a *stale*
# ``phase=running`` sentinel) correctly does NOT flag them, yet from the user's
# point of view they are exactly as unclean as a SIGKILL: no drain ran.
_WATCHDOG_EXIT_REASONS = frozenset({"loop_liveness_watchdog", "shutdown_watchdog"})

_NOTICE_LEDGER_RELATIVE = ("state", "gateway.restart-notice.json")
_GATEWAY_LOG_RELATIVE = ("logs", "gateway.log")

# `PHASE=event_loop_blocked platform=discord seconds=30 site=utils.py:230 ...`
_BLOCKED_SITE_RE = re.compile(r"PHASE=event_loop_blocked\b.*?\bsite=(.+?)\s*$")

# Bound the log tail read: the site line, if present, is from the seconds
# before death and lives at the very end of the previous generation.
_LOG_TAIL_BYTES = 256 * 1024


@dataclass(frozen=True)
class PriorLifeVerdict:
    """What the current boot can say about how the previous life ended."""

    unclean: bool
    boot_id: Optional[str] = None
    exit_code: Optional[int] = None
    exit_reason: Optional[str] = None
    killer: Optional[str] = None
    kill_sender: Optional[str] = None
    kill_sender_label: Optional[str] = None
    ended_at: Optional[str] = None
    site: Optional[str] = None
    planned: bool = False          # a safe-restart we REQUESTED explains the death
    planned_by: Optional[str] = None
    planned_detail: Optional[str] = None   # ledger `detail` (e.g. 'kickstart -k', 'full-reload')


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def classify_prior_life(
    sentinel: Optional[Dict[str, Any]],
    *,
    site: Optional[str] = None,
    planned: Optional[Dict[str, Any]] = None,
) -> PriorLifeVerdict:
    """Read the CURRENT life's sentinel for the PREVIOUS life's verdict.

    ``gateway.lifecycle_ledger._claim_sentinel`` carries the previous life's
    outcome forward onto the new sentinel -- that is the only machine-readable
    place the finding survives. Three shapes count as unclean:

    * ``prior_unclean_exit`` -- no exit path ran at all (SIGKILL / OOM / VM
      death), already attributed by the ledger's boot-time probe;
    * a watchdog ``mark_exited`` record (``loop_liveness_watchdog`` /
      ``shutdown_watchdog``) -- an ``os._exit`` with no drain;
    * any non-zero exit code on a recorded prior exit.

    Never raises: a malformed sentinel yields ``unclean=False`` rather than
    taking the boot-resume path down with it.
    """
    try:
        data = sentinel if isinstance(sentinel, dict) else {}
        exit_code = _as_int(data.get("prior_exit_code"))
        exit_reason = _as_str(data.get("prior_exit_reason"))
        killer = _as_str(data.get("prior_killer"))
        if killer == "unattributed":
            killer = None

        unclean = bool(data.get("prior_unclean_exit"))
        if not unclean and _as_str(data.get("prior_phase")) == "exited":
            if exit_reason in _WATCHDOG_EXIT_REASONS:
                unclean = True
            elif exit_code is not None and exit_code != 0:
                unclean = True

        return PriorLifeVerdict(
            unclean=unclean,
            boot_id=_as_str(data.get("started_at")),
            exit_code=exit_code,
            exit_reason=exit_reason,
            killer=killer,
            kill_sender=_as_str(data.get("prior_kill_sender")),
            kill_sender_label=_as_str(data.get("prior_kill_sender_label")),
            ended_at=_as_str(data.get("prior_exited_at"))
            or _as_str(data.get("prior_started_at")),
            site=site if isinstance(site, str) and site else None,
            planned=bool(planned),
            planned_by=_as_str((planned or {}).get("initiator_profile")) if planned else None,
            planned_detail=_as_str((planned or {}).get("detail")) if planned else None,
        )
    except Exception:  # pragma: no cover - classification is fail-quiet
        logger.debug("Prior-life classification failed", exc_info=True)
        return PriorLifeVerdict(unclean=False)


def _local_hhmm(iso: Optional[str]) -> Optional[str]:
    """Render an ISO timestamp as local ``HH:MM`` -- what the user experienced."""
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    try:
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%H:%M")
    except Exception:  # pragma: no cover
        return None


def _cause_phrase(verdict: PriorLifeVerdict) -> Optional[str]:
    """The parenthetical: the most specific honest thing we can say."""
    if verdict.exit_reason == "loop_liveness_watchdog":
        code = verdict.exit_code if verdict.exit_code is not None else 75
        phrase = "exit %s: event loop blocked" % code
        if verdict.site:
            phrase += " at %s" % verdict.site
        return phrase
    if verdict.exit_reason == "shutdown_watchdog":
        code = verdict.exit_code if verdict.exit_code is not None else 1
        return "exit %s: shutdown watchdog fired" % code
    if verdict.killer:
        by = verdict.kill_sender_label or verdict.kill_sender
        return "%s by %s" % (verdict.killer, by) if by else verdict.killer
    if verdict.exit_code is not None and verdict.exit_code != 0:
        if verdict.exit_reason:
            return "exit %d: %s" % (verdict.exit_code, verdict.exit_reason)
        return "exit %d" % verdict.exit_code
    return None


def format_restart_notice(verdict: PriorLifeVerdict) -> Optional[str]:
    """ONE short line, or ``None`` when no notice is owed.

    ``None`` for a clean prior exit is load-bearing: the graceful drain path
    already sent "Gateway restarting" before going down, and a second notice on
    the way back up would be noise.
    """
    if not verdict.unclean:
        return None
    if verdict.planned:
        # A death our own safe-restart caused. 2026-09-22 22:5x, Ace: "can we get a
        # notification in chat if it's related to a safe restart or what caused this
        # freeze?" — silence read as a mystery freeze. So: ONE short line that names
        # the initiator and the mechanism, no "resuming" scare-phrasing, no exit code.
        who = verdict.planned_by or "fleet"
        how = (verdict.planned_detail or "safe-restart").strip()
        when = _local_hhmm(verdict.ended_at)
        head = "\U0001f504 Restarted%s by %s (%s)" % ((" at %s" % when) if when else "", who, how)
        return head + " — planned; resuming where I left off."
    when = _local_hhmm(verdict.ended_at)
    head = ("\u26a0\ufe0f Restarted at %s" % when) if when else "\u26a0\ufe0f Restarted"
    cause = _cause_phrase(verdict)
    head += " — UNPLANNED (%s)" % cause if cause else " — UNPLANNED"
    return head + "; resuming where I left off."


# --------------------------------------------------------------------------
# Idempotency: once per session per boot
# --------------------------------------------------------------------------


def _process_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def get_restart_notice_ledger_path(home: Optional[Path] = None) -> Path:
    """``<HERMES_HOME>/state/gateway.restart-notice.json``."""
    base = home if home is not None else _process_home()
    return base.joinpath(*_NOTICE_LEDGER_RELATIVE)


def claim_restart_notice(
    boot_id: Optional[str], session_key: str, home: Optional[Path] = None
) -> bool:
    """Claim the right to post ONE notice to ``session_key`` this boot.

    ``True`` exactly once per (boot_id, session_key). The ledger is keyed on
    the boot id, so a NEW boot re-arms every session -- which is the behaviour a
    crash loop needs (each restart is a fresh unexplained silence) while a
    second resume attempt inside one boot stays silent.

    Fail-OPEN on a corrupt/unreadable ledger: it is better to risk a duplicate
    notice than to silently swallow the only explanation the user gets.
    """
    path = get_restart_notice_ledger_path(home)
    payload: Dict[str, Any] = {"boot_id": boot_id, "notified": []}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("boot_id") == boot_id:
            notified = existing.get("notified")
            if isinstance(notified, list):
                if session_key in notified:
                    return False
                payload["notified"] = [n for n in notified if isinstance(n, str)]
    except (OSError, ValueError):
        pass
    except Exception:  # pragma: no cover
        logger.debug("Restart-notice ledger read failed", exc_info=True)

    payload["notified"].append(session_key)
    try:
        from utils import atomic_json_write

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, payload, indent=None)
    except Exception:
        # Persisting is best-effort; the claim still stands for this process so
        # two resumes in one boot do not both send even if the disk is wedged.
        logger.debug("Restart-notice ledger write failed", exc_info=True)
    return True


# --------------------------------------------------------------------------
# Planned-restart suppression: a REQUESTED bounce is not an unexplained death
# --------------------------------------------------------------------------

_RESTART_LEDGER_RELATIVE = ("logs", "gateway-restart-ledger.jsonl")
_LEDGER_TAIL_BYTES = 64 * 1024
# A kickstart row this close BEFORE the prior life's end is the cause of it.
PLANNED_RESTART_WINDOW_S = 180.0


def _restart_ledger_path(home: Optional[Path] = None) -> Path:
    base = home if home is not None else _process_home()
    return base.joinpath(*_RESTART_LEDGER_RELATIVE)


def read_planned_restart(
    ended_at: Optional[str], home: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """The safe-restart ``kickstart``/``intent`` row that explains ``ended_at``.

    2026-09-21/22: Apollo posted "I was restarted at 20:48 (exit 1: shutdown
    watchdog fired) and am resuming" into FOUR Discord channels per bounce,
    and Ace read it as a crash loop. Every one of those bounces was a
    safe-restart the fleet had REQUESTED (deploy lane / --interrupt-busy /
    his own /restart): with 5-12 live turns the 30 s drain always times out,
    the 50 s shutdown watchdog fires, and ``mark_exited(1, "shutdown_watchdog")``
    makes the prior life look unclean. The watcher's ledger is the only
    machine-readable record that the SIGTERM was ours. Read it; if a
    ``kickstart`` (or ``intent``) row for this profile landed within
    PLANNED_RESTART_WINDOW_S before the death, the restart was planned.

    Returns the ledger row (dict) or ``None``. Fail-OPEN on any read error:
    a missing/corrupt ledger must not silence a genuine unexplained death.
    """
    if not ended_at:
        return None
    try:
        ended = datetime.fromisoformat(ended_at)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        ended_epoch = ended.timestamp()
    except (ValueError, OverflowError):
        return None
    path = _restart_ledger_path(home)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _LEDGER_TAIL_BYTES:
                fh.seek(size - _LEDGER_TAIL_BYTES)
                fh.readline()  # drop the partial first line
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    profile = (os.environ.get("HERMES_PROFILE") or "").strip() or "default"
    best: Optional[Dict[str, Any]] = None
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("event") not in ("kickstart", "intent"):
            continue
        if (row.get("target_profile") or "default") != profile:
            continue
        try:
            epoch = float(row.get("epoch"))
        except (TypeError, ValueError):
            continue
        delta = ended_epoch - epoch
        if -PLANNED_RESTART_WINDOW_S <= delta <= PLANNED_RESTART_WINDOW_S:
            if best is None or abs(delta) < abs(ended_epoch - float(best["epoch"])):
                best = row
    return best


# --------------------------------------------------------------------------
# Optional enrichment: the site that blocked the loop
# --------------------------------------------------------------------------


def read_last_event_loop_blocked_site(home: Optional[Path] = None) -> Optional[str]:
    """Last ``PHASE=event_loop_blocked ... site=<site>`` from the gateway log.

    Cheap (bounded tail read) and entirely optional -- the notice is still
    useful without it. ``None`` on any miss; never raises.
    """
    base = home if home is not None else _process_home()
    path = base.joinpath(*_GATEWAY_LOG_RELATIVE)
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(-_LOG_TAIL_BYTES, os.SEEK_END)
            except OSError:
                fh.seek(0)
            tail = fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    except Exception:  # pragma: no cover
        logger.debug("event_loop_blocked site read failed", exc_info=True)
        return None

    site: Optional[str] = None
    for line in tail.splitlines():
        match = _BLOCKED_SITE_RE.search(line)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                site = candidate
    return site
