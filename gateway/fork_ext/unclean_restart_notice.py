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
import math
import os
import re
import time
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


def prior_life_reference_at(sentinel: Optional[Dict[str, Any]]) -> Optional[str]:
    """Best ISO timestamp for WHEN the previous life ended.

    ``prior_exited_at`` only exists when an exit path ran. A SIGKILL at
    launchd ``ExitTimeOut`` (the normal end of a busy safe-restart) runs none,
    so fall back to the last loop heartbeat, then to THIS boot's start -- both
    are within seconds/minutes of the death. Never ``prior_started_at``: that is
    when the previous life BEGAN, possibly hours earlier (incident 2026-09-24
    12:12: a 70-minute-old reference pushed the kickstart row out of the window
    and a requested restart was announced as UNPLANNED).
    """
    data = sentinel if isinstance(sentinel, dict) else {}
    return (
        _as_str(data.get("prior_exited_at"))
        or _as_str(data.get("prior_last_heartbeat_at"))
        or _as_str(data.get("started_at"))
    )


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
            ended_at=prior_life_reference_at(data),
            site=site if isinstance(site, str) and site else None,
            planned=bool(planned),
            planned_by=_as_str((planned or {}).get("initiator_profile")) if planned else None,
            planned_detail=planned_restart_detail(planned) if planned else None,
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


# ``in_band`` is written by the gateway ITSELF right before an in-band restart
# enters ``stop()`` (SIGUSR1 / ``/restart`` / deferred arm). Incident 2026-09-23
# 04:31: the in-band requester wrote no row, so the boot notice read
# ``planned=False by=-`` for a restart the gateway had asked for itself.
PLANNED_RESTART_EVENTS = ("kickstart", "intent", "in_band")

# busy_policy=interrupt means "restart now". The gateway honours it by capping
# its after-turn wait to at most this many seconds after it first SEES the
# intent row, instead of sitting out ``restart_after_turn_timeout`` (1800 s).
INTERRUPT_DRAIN_CAP_MAX_S = 60.0
# An intent row older than this is a previous bounce, never the current one.
INTERRUPT_INTENT_MAX_AGE_S = 3600.0


def _current_profile(home: Optional[Path] = None) -> str:
    # hermes --profile sets HERMES_HOME, not HERMES_PROFILE. The ledger is
    # written into that profile's home; derive identity from the same root.
    base = Path(home) if home is not None else _process_home()
    if base.parent.name == "profiles":
        return base.name
    return (os.environ.get("HERMES_PROFILE") or "").strip() or "default"


def _read_ledger_tail(home: Optional[Path] = None) -> Optional[str]:
    path = _restart_ledger_path(home)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _LEDGER_TAIL_BYTES:
                fh.seek(size - _LEDGER_TAIL_BYTES)
                fh.readline()  # drop the partial first line
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def read_interrupt_restart_intent(
    pid: Optional[int] = None,
    *,
    now: Optional[float] = None,
    home: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """The newest fresh ``busy_policy=interrupt`` intent aimed at THIS process.

    safe-restart.py writes an ``intent`` row into the target's ledger before it
    spawns the watcher, carrying ``busy_policy`` and ``pid_before`` (the
    target's pid). Incident 2026-09-23: the deploy lane ran with
    ``--busy-policy interrupt`` at 04:05 while the gateway was already 6 min
    into an 1800 s after-turn wait; nothing in the gateway read the policy, so
    it waited until 04:29 and the watcher paged a false quiesce timeout.

    Matching is deliberately strict (event, policy, profile, pid, freshness): a
    stale row or one aimed at a previous pid must never cut a live wait short.
    Fail-closed to ``None`` on any read/parse problem (= old behaviour).
    """
    pid = os.getpid() if pid is None else int(pid)
    current = time.time() if now is None else float(now)
    tail = _read_ledger_tail(home)
    if not tail:
        return None
    profile = _current_profile(home)
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("event") != "intent" or row.get("busy_policy") != "interrupt":
            continue
        if (row.get("target_profile") or "default") != profile:
            continue
        try:
            if int(row.get("pid_before")) != pid:
                continue
            age = current - float(row.get("epoch"))
        except (TypeError, ValueError):
            continue
        if -60.0 <= age <= INTERRUPT_INTENT_MAX_AGE_S:
            return row
    return None


def interrupt_drain_cap(row: Optional[Dict[str, Any]]) -> float:
    """Seconds of after-turn grace an interrupt intent allows (0..60)."""
    cap = INTERRUPT_DRAIN_CAP_MAX_S
    try:
        raw = float((row or {}).get("drain_cap_s"))
        if math.isfinite(raw) and raw >= 0:
            cap = raw
    except (TypeError, ValueError):
        pass
    return min(cap, INTERRUPT_DRAIN_CAP_MAX_S)


def record_in_band_restart(
    requester: str,
    *,
    detail: str = "",
    home: Optional[Path] = None,
) -> bool:
    """Append an ``in_band`` row so the next boot reads the restart as planned."""
    try:
        from datetime import datetime as _dt

        rec = {
            "ts": _dt.now().isoformat(timespec="seconds"),
            "epoch": round(time.time(), 3),
            "event": "in_band",
            "token": "",
            "target_profile": _current_profile(home),
            "initiator_profile": f"self:{requester or 'unknown'}",
            "origin_mode": "in_band",
            "pid_before": os.getpid(),
        }
        if detail:
            rec["detail"] = str(detail)[:500]
        path = _restart_ledger_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return True
    except Exception:
        logger.debug("in-band restart ledger row failed", exc_info=True)
        return False


def read_planned_restart(
    ended_at: Optional[str],
    home: Optional[Path] = None,
    *,
    prior_pid: Any = None,
    boot_at: Optional[str] = None,
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

    PID identity beats the time window: a row whose ``pid_before`` equals the
    previous life's pid (``prior_pid``) was aimed at exactly that process, so it
    explains the death however long the drain took -- provided it predates this
    boot (``boot_at``). Among pid matches the ``kickstart`` row wins (it carries
    the mechanism, e.g. ``full-reload``), then ``in_band``, then ``intent``.

    Returns the ledger row (dict) or ``None``. Fail-OPEN on any read error:
    a missing/corrupt ledger must not silence a genuine unexplained death.
    """
    ended_epoch = _iso_epoch(ended_at)
    boot_epoch = _iso_epoch(boot_at)
    pid_key = _as_pid(prior_pid)
    if ended_epoch is None and pid_key is None:
        return None
    tail = _read_ledger_tail(home)
    if tail is None:
        return None
    profile = _current_profile(home)
    best: Optional[Dict[str, Any]] = None
    pid_best: Optional[Dict[str, Any]] = None
    reasons: Dict[str, str] = {}
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("event") not in PLANNED_RESTART_EVENTS:
            continue
        if (row.get("target_profile") or "default") != profile:
            continue
        if _as_str(row.get("token")) and _as_str(row.get("reason")):
            reasons[row["token"]] = row["reason"]
        try:
            epoch = float(row.get("epoch"))
        except (TypeError, ValueError):
            continue
        if pid_key is not None and _as_pid(row.get("pid_before")) == pid_key:
            if boot_epoch is None or epoch <= boot_epoch + _PID_MATCH_BOOT_SLACK_S:
                if pid_best is None or _pid_rank(row, epoch) > _pid_rank(
                    pid_best, float(pid_best["epoch"])
                ):
                    pid_best = row
                continue
        if ended_epoch is None:
            continue
        delta = ended_epoch - epoch
        if -PLANNED_RESTART_WINDOW_S <= delta <= PLANNED_RESTART_WINDOW_S:
            if best is None or abs(delta) < abs(ended_epoch - float(best["epoch"])):
                best = row
    chosen = pid_best if pid_best is not None else best
    # The requester's reason usually rides the ``intent`` row; the ``kickstart``
    # row of the same token carries the mechanism. Show both.
    if chosen is not None and not _as_str(chosen.get("reason")):
        borrowed = reasons.get(chosen.get("token") or "")
        if borrowed:
            chosen = dict(chosen, reason=borrowed)
    return chosen


# A pid-matched row must predate this boot; allow clock skew between writers.
_PID_MATCH_BOOT_SLACK_S = 60.0
_EVENT_PREFERENCE = {"kickstart": 2, "in_band": 1, "intent": 0}


def _iso_epoch(value: Optional[str]) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _as_pid(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_rank(row: Dict[str, Any], epoch: float):
    return (_EVENT_PREFERENCE.get(row.get("event"), -1), epoch)


def planned_restart_detail(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Human mechanism for the banner, from a ledger row.

    ``kickstart`` rows carry it (``full-reload`` / ``kickstart -k``). An
    ``intent`` row's ``detail`` is resolver plumbing (``origin_source=...``)
    that means nothing to a reader, so it is dropped in favour of
    ``full-reload`` / ``None`` (the formatter then says ``safe-restart``). A
    ``reason`` field, when a requester recorded one, is appended: banners must
    NAME the cause.
    """
    if not isinstance(row, dict):
        return None
    detail = _as_str(row.get("detail"))
    if detail and detail.startswith("origin_source="):
        detail = None
    if not detail and row.get("full_reload"):
        detail = "full-reload"
    reason = _as_str(row.get("reason"))
    if reason:
        detail = "%s: %s" % (detail, reason) if detail else reason
    return detail


def read_planned_restart_for_sentinel(
    sentinel: Optional[Dict[str, Any]], home: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """The ledger row explaining the previous life's death, from this boot's sentinel.

    The ONE entry point the gateway uses, so the reference-time and
    pid-identity rules cannot drift between callers. Never raises.
    """
    try:
        data = sentinel if isinstance(sentinel, dict) else {}
        return read_planned_restart(
            prior_life_reference_at(data),
            home,
            prior_pid=data.get("prior_pid"),
            boot_at=_as_str(data.get("started_at")),
        )
    except Exception:
        logger.debug("Planned-restart lookup failed", exc_info=True)
        return None


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
