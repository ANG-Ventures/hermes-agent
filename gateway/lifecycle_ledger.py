"""Gateway lifecycle ledger — durable termination-reason evidence (NS-608).

The gateway already has *graceful* shutdown forensics
(:mod:`gateway.shutdown_forensics` — who sent the SIGTERM) and an exit-path
diagnostic log (``gateway-exit-diag.log`` — every way ``asyncio.run`` can
return).  What it does NOT have is any record of an **unclean death**: a
SIGKILL, a kernel OOM kill, or the whole VM dying takes the process out
before any handler runs, so the next boot has no idea the previous life
ended violently — support tickets like NS-608 then require manually
cross-correlating four log files and two external APIs to answer "what
killed the gateway?".

This module closes that gap with a tiny state machine persisted to
``<HERMES_HOME>/state/gateway.lifecycle.json``:

* On startup, :func:`record_startup` reads the sentinel left by the
  previous life.  ``phase == "running"`` means that life never reached any
  exit path → it died uncleanly.  The finding — including the last
  heartbeat's memory sample, which is the closest thing to a pre-death
  telemetry snapshot — is appended to ``gateway-exit-diag.log`` as a
  ``gateway.previous_unclean_exit`` record and logged at WARNING.  The
  sentinel is then rewritten as ``phase=running`` for the new life.
* On every clean exit path, :func:`mark_exited` rewrites the sentinel as
  ``phase=exited`` with the exit code and a reason string.  Wired into
  ``_exit_after_graceful_shutdown`` (the single funnel for all graceful
  exits, #53107) and the two watchdog ``os._exit`` sites in
  :mod:`gateway.shutdown_watchdog`.

:func:`sample_memory` provides the cheap (<1ms, pure /proc reads) memory
snapshot that :func:`gateway.shutdown_watchdog.write_loop_heartbeat`
embeds in the 30s heartbeat — giving every unclean-death report a
"memory available N seconds before death" data point so OOM crash cycles
are classifiable from the volume alone (no Prometheus retention races).

Everything here is best-effort: a forensics failure must never affect the
gateway lifecycle it is observing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LIFECYCLE_RELATIVE = ("state", "gateway.lifecycle.json")
_TEARDOWN_TIMING_RELATIVE = ("state", "gateway.teardown.json")
_EXIT_DIAG_RELATIVE = ("logs", "gateway-exit-diag.log")

# Total wall-clock budget for the kill-attribution probe.  Boot must never
# wait longer than this, and the probe is fail-open: any timeout, missing
# tool, or parse miss degrades to ``killer=unattributed reason=<why>``.
KILL_ATTRIBUTION_TIMEOUT_S = 10.0
# Split across the two queries the macOS path makes (exit record, then the
# sender's own spawn record) so the pair still fits inside the total bound.
_KILL_ATTRIBUTION_STEP_TIMEOUT_S = KILL_ATTRIBUTION_TIMEOUT_S / 2.0

# Heuristic OOM-suspicion thresholds applied to the last heartbeat's memory
# sample.  Deliberately conservative: this only annotates the report with a
# hint; classification stays with the human reading the evidence.
_LOW_MEM_AVAILABLE_KIB = 64 * 1024  # < 64 MiB available
_LOW_MEM_AVAILABLE_FRACTION = 0.05  # < 5% of MemTotal available


def _process_hermes_home() -> Path:
    """HERMES_HOME for process-level identity files (ignore task overrides)."""
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def get_lifecycle_sentinel_path(home: Optional[Path] = None) -> Path:
    """Return ``<HERMES_HOME>/state/gateway.lifecycle.json``."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_LIFECYCLE_RELATIVE)


def get_teardown_timing_path(home: Optional[Path] = None) -> Path:
    """Return the last completed post-drain teardown timing path."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_TEARDOWN_TIMING_RELATIVE)


def read_last_teardown_seconds(home: Optional[Path] = None) -> Optional[float]:
    """Read the last completed post-drain teardown duration, if valid."""
    data = _read_json(get_teardown_timing_path(home))
    raw = (data or {}).get("teardown_seconds")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0.0 else None


def record_teardown_timing(
    teardown_seconds: float,
    *,
    total_shutdown_seconds: float,
    drain_seconds: float,
    home: Optional[Path] = None,
) -> None:
    """Persist and diagnose one completed post-drain teardown measurement."""
    try:
        teardown = max(float(teardown_seconds), 0.0)
        total = max(float(total_shutdown_seconds), 0.0)
        drain = max(float(drain_seconds), 0.0)
    except (TypeError, ValueError):
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tag": "gateway.shutdown_teardown_timing",
        "pid": os.getpid(),
        "teardown_seconds": teardown,
        "total_shutdown_seconds": total,
        "drain_seconds": drain,
    }
    path = get_teardown_timing_path(home)
    try:
        from utils import atomic_json_write

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, record, indent=None)
    except Exception:
        logger.debug("Failed to persist teardown timing", exc_info=True)
    _append_exit_diag(record, home)


def sample_memory() -> Dict[str, Any]:
    """Cheap memory snapshot: own RSS + system availability + swap.

    Pure ``/proc`` reads, Linux-only (returns ``{}`` elsewhere), never
    raises.  Values in KiB to match the kernel's units.
    """
    sample: Dict[str, Any] = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    sample["rss_kib"] = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        meminfo: Dict[str, int] = {}
        wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key = line.split(":", 1)[0]
                if key in wanted:
                    meminfo[key] = int(line.split()[1])
                    if len(meminfo) == len(wanted):
                        break
        if "MemTotal" in meminfo:
            sample["mem_total_kib"] = meminfo["MemTotal"]
        if "MemAvailable" in meminfo:
            sample["mem_available_kib"] = meminfo["MemAvailable"]
        if "SwapTotal" in meminfo and "SwapFree" in meminfo:
            sample["swap_used_kib"] = meminfo["SwapTotal"] - meminfo["SwapFree"]
    except (OSError, ValueError, IndexError):
        pass
    return sample


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_sentinel(payload: Dict[str, Any], home: Optional[Path]) -> None:
    path = get_lifecycle_sentinel_path(home)
    try:
        from utils import atomic_json_write

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, payload, indent=None)
    except Exception:
        logger.debug("Failed to write lifecycle sentinel", exc_info=True)


def _append_exit_diag(record: Dict[str, Any], home: Optional[Path]) -> None:
    """Append a JSON line to gateway-exit-diag.log (same format as the CLI's
    ``_exit_diag`` records so existing tooling greps both)."""
    base = home if home is not None else _process_hermes_home()
    path = base.joinpath(*_EXIT_DIAG_RELATIVE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        logger.debug("Failed to append unclean-exit record", exc_info=True)


def _pid_alive_with_start_time(pid: Any, start_time: Any) -> bool:
    """True when ``pid`` is a live process matching ``start_time`` (±2s).

    Guards the takeover race: during ``--replace`` the old gateway can still
    be mid-teardown when the new one boots — a live matching owner is a
    planned handover, not an unclean death.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        # NOT os.kill(pid, 0): on Windows that sends CTRL_C_EVENT to the
        # target's console group (bpo-14484). _pid_exists is the repo's
        # canonical no-kill cross-platform probe (psutil-backed).
        from gateway.status import _pid_exists

        if not _pid_exists(pid_int):
            return False
    except Exception:
        return False
    if start_time is None:
        return True  # alive; can't disambiguate PID reuse — err on "alive"
    try:
        from gateway.status import get_process_start_time

        actual = get_process_start_time(pid_int)
        if actual is None:
            return True
        return abs(float(actual) - float(start_time)) <= 2.0
    except Exception:
        return True


def _run_log_command(argv: list, timeout: float) -> str:
    """Run a read-only forensics command, return stdout (never raises here —
    the caller's try/except owns fail-open)."""
    proc = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        text=True,
        errors="replace",
    )
    return proc.stdout or ""


# `... Service exited due to SIGKILL | sent by Python[35502], ran for 9086738ms`
_MACOS_SIGNAL_RE = re.compile(r"exited due to (SIG[A-Z0-9]+)")
_MACOS_SENT_BY_RE = re.compile(r"sent by ([A-Za-z0-9_.\-]+)\[(\d+)\]")
# `... Service bootout initiated by: launchctl[36598]<-Python[35502]`
_MACOS_BOOTOUT_RE = re.compile(r"bootout initiated by:\s*(.+)")
# Leading char excludes `-` so the `<-` in `launchctl[36598]<-Python[35502]`
# is not absorbed into the process name.
_MACOS_PROC_RE = re.compile(r"([A-Za-z0-9_.][A-Za-z0-9_.\-]*)\[(\d+)\]")
# `launchd: (ai.hermes.gateway-watchdog [35502]) Successfully spawned python3[35502]`
_MACOS_LABEL_RE = re.compile(r"\(([A-Za-z0-9_.\-]+)\s*\[(\d+)\]\)")


def parse_launchd_exit_record(text: str, pid: int) -> Dict[str, Any]:
    """Parse macOS unified-log launchd output for ``pid``'s death.

    Returns ``{"killer": "SIGKILL", "sender": "Python[35502]",
    "sender_pid": 35502}`` — any subset that could be determined, or ``{}``.
    Never raises.
    """
    out: Dict[str, Any] = {}
    try:
        for line in (text or "").splitlines():
            m = _MACOS_SIGNAL_RE.search(line)
            if m and "killer" not in out:
                out["killer"] = m.group(1)
            m = _MACOS_SENT_BY_RE.search(line)
            if m and "sender" not in out:
                out["sender"] = f"{m.group(1)}[{m.group(2)}]"
                out["sender_pid"] = int(m.group(2))
            if "sender" not in out:
                m = _MACOS_BOOTOUT_RE.search(line)
                if m:
                    # `launchctl[36598]<-Python[35502]` — the LAST element of
                    # the chain is the originating process, not the launchctl
                    # trampoline it shelled out through.
                    procs = _MACOS_PROC_RE.findall(m.group(1))
                    if procs:
                        name, spid = procs[-1]
                        out["sender"] = f"{name}[{spid}]"
                        out["sender_pid"] = int(spid)
    except Exception:  # pragma: no cover - parser is fail-open by contract
        logger.debug("launchd exit-record parse failed", exc_info=True)
    return out


def parse_launchd_spawn_label(text: str, sender_pid: int) -> Optional[str]:
    """Extract the launchd *label* that owns ``sender_pid`` from its spawn
    record (``launchd: (ai.hermes.gateway-watchdog [35502]) Successfully
    spawned ...``).  ``None`` when unknown.  Never raises.
    """
    try:
        for line in (text or "").splitlines():
            for label, lpid in _MACOS_LABEL_RE.findall(line):
                if int(lpid) == int(sender_pid):
                    return label
    except Exception:  # pragma: no cover
        logger.debug("launchd spawn-label parse failed", exc_info=True)
    return None


def _macos_attribution(pid: int, when: Optional[datetime]) -> Dict[str, Any]:
    anchor = when or datetime.now(timezone.utc)
    start = (anchor - timedelta(minutes=3)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    end = (anchor + timedelta(minutes=1)).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def _show(predicate: str) -> str:
        return _run_log_command(
            [
                "log", "show",
                "--start", start,
                "--end", end,
                "--style", "syslog",
                "--predicate", predicate,
            ],
            timeout=_KILL_ATTRIBUTION_STEP_TIMEOUT_S,
        )

    text = _show(
        'process == "launchd" AND eventMessage CONTAINS "[%d]"' % int(pid)
    )
    parsed = parse_launchd_exit_record(text, int(pid))
    if not parsed:
        return {"killer": "unattributed", "reason": "no_launchd_exit_record"}

    sender_pid = parsed.get("sender_pid")
    if sender_pid:
        label = parse_launchd_spawn_label(
            _show(
                'process == "launchd" AND eventMessage CONTAINS "[%d]"'
                % int(sender_pid)
            ),
            int(sender_pid),
        )
        if label:
            parsed["sender_label"] = label
    parsed.setdefault("killer", "unattributed")
    if parsed["killer"] == "unattributed":
        parsed.setdefault("reason", "no_signal_in_launchd_record")
    return parsed


def _linux_attribution(pid: int, when: Optional[datetime]) -> Dict[str, Any]:
    """journald/dmesg analogue: the unit's ``Main process exited,
    code=killed, status=9/KILL`` line, plus a kernel OOM-kill check."""
    out: Dict[str, Any] = {}
    try:
        text = _run_log_command(
            ["journalctl", "_PID=%d" % int(pid), "--no-pager", "-n", "200"],
            timeout=_KILL_ATTRIBUTION_STEP_TIMEOUT_S,
        )
    except Exception:
        text = ""
    m = re.search(r"code=killed, status=\d+/([A-Z]+)", text or "")
    if m:
        out["killer"] = "SIG" + m.group(1)
    try:
        dmesg = _run_log_command(
            ["dmesg", "--ctime"], timeout=_KILL_ATTRIBUTION_STEP_TIMEOUT_S
        )
    except Exception:
        dmesg = ""
    if re.search(r"Killed process %d\b" % int(pid), dmesg or "") or re.search(
        r"oom-kill:.*\bpid=%d\b" % int(pid), dmesg or ""
    ):
        out["killer"] = "SIGKILL"
        out["sender"] = "kernel-oom"
        out["sender_label"] = "kernel:oom_reaper"
    if not out:
        return {"killer": "unattributed", "reason": "no_journald_or_dmesg_record"}
    return out


def attribute_unclean_exit(
    pid: Any, when: Optional[datetime] = None
) -> Dict[str, Any]:
    """Name the killer of ``pid``.  BOUNDED (<= ``KILL_ATTRIBUTION_TIMEOUT_S``),
    fail-open, never raises.

    Returns ``{"killer": "SIGKILL", "sender": "Python[35502]",
    "sender_label": "ai.hermes.gateway-watchdog"}`` when the platform's
    system log can say, else ``{"killer": "unattributed", "reason": ...}``.

    Blocking (subprocess) — call it from a thread, not the event loop; see
    :func:`record_startup_async`.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return {"killer": "unattributed", "reason": "no_prior_pid"}
    try:
        if sys.platform == "darwin":
            return _macos_attribution(pid_int, when)
        if sys.platform.startswith("linux"):
            return _linux_attribution(pid_int, when)
        return {
            "killer": "unattributed",
            "reason": "unsupported_platform:%s" % sys.platform,
        }
    except subprocess.TimeoutExpired:
        return {"killer": "unattributed", "reason": "probe_timeout"}
    except Exception as exc:
        return {"killer": "unattributed", "reason": "probe_failed:%s" % exc}


def detect_unclean_exit(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Inspect the previous life's sentinel; return an evidence dict when it
    died uncleanly, else ``None``.  Read-only — does not rewrite the sentinel.
    """
    sentinel = _read_json(get_lifecycle_sentinel_path(home))
    if not sentinel or sentinel.get("phase") != "running":
        return None
    if _pid_alive_with_start_time(sentinel.get("pid"), sentinel.get("start_time")):
        return None  # live owner — planned takeover in flight, not a death

    evidence: Dict[str, Any] = {
        "prior_pid": sentinel.get("pid"),
        "prior_started_at": sentinel.get("started_at"),
        "prior_start_time": sentinel.get("start_time"),
    }

    # Enrich with the last heartbeat: when did the loop last prove liveness,
    # and what did memory look like at that moment?
    try:
        from gateway.shutdown_watchdog import get_loop_heartbeat_path

        hb = _read_json(get_loop_heartbeat_path(home))
    except Exception:
        hb = None
    if hb:
        evidence["last_heartbeat_at"] = hb.get("updated_at")
        mem = hb.get("mem")
        if isinstance(mem, dict):
            evidence["last_heartbeat_mem"] = mem
            total = mem.get("mem_total_kib")
            avail = mem.get("mem_available_kib")
            if isinstance(avail, int) and (
                avail < _LOW_MEM_AVAILABLE_KIB
                or (
                    isinstance(total, int)
                    and total > 0
                    and avail / total < _LOW_MEM_AVAILABLE_FRACTION
                )
            ):
                evidence["suspected_oom"] = True
    return evidence


def _apply_attribution(evidence: Dict[str, Any], attribution: Optional[Dict[str, Any]]) -> None:
    """Fold a probe result into the evidence dict under stable keys."""
    att = attribution if isinstance(attribution, dict) else {}
    evidence["killer"] = att.get("killer") or "unattributed"
    if att.get("sender"):
        evidence["kill_sender"] = att["sender"]
    if att.get("sender_label"):
        evidence["kill_sender_label"] = att["sender_label"]
    if evidence["killer"] == "unattributed":
        evidence["kill_attribution_reason"] = att.get("reason") or "probe_unavailable"


def _probe_attribution(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Blocking wrapper: run the probe for this evidence, never raise."""
    try:
        return attribute_unclean_exit(evidence.get("prior_pid"))
    except Exception:
        logger.debug("Kill-attribution probe failed", exc_info=True)
        return {"killer": "unattributed", "reason": "probe_failed"}


def _format_unclean_suffix(evidence: Dict[str, Any]) -> str:
    parts = ["killer=%s" % evidence.get("killer", "unattributed")]
    if evidence.get("kill_sender"):
        parts.append("sender=%s" % evidence["kill_sender"])
    if evidence.get("kill_sender_label"):
        parts.append("sender_label=%s" % evidence["kill_sender_label"])
    if evidence.get("kill_attribution_reason"):
        parts.append("reason=%s" % evidence["kill_attribution_reason"])
    return " ".join(parts)


def _emit_unclean_report(evidence: Dict[str, Any], home: Optional[Path]) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tag": "gateway.previous_unclean_exit",
        "pid": os.getpid(),
        **evidence,
    }
    _append_exit_diag(record, home)
    logger.warning(
        "Previous gateway life (pid=%s, started_at=%s) exited UNCLEANLY "
        "(no exit path ran — SIGKILL / OOM / VM death). "
        "last_heartbeat_at=%s last_mem=%s suspected_oom=%s %s",
        evidence.get("prior_pid"),
        evidence.get("prior_started_at"),
        evidence.get("last_heartbeat_at"),
        evidence.get("last_heartbeat_mem"),
        evidence.get("suspected_oom", False),
        _format_unclean_suffix(evidence),
    )


def _carry_prior_exit_forward(claim: Dict[str, Any], home: Optional[Path]) -> None:
    """Copy the PREVIOUS life's recorded exit onto the new sentinel.

    ``detect_unclean_exit`` only fires when the previous life left a stale
    ``phase=running`` sentinel — i.e. nothing ran on the way out at all. The
    two watchdog ``os._exit`` sites DO run ``mark_exited`` first, so they leave
    ``phase=exited exit_code=75 exit_reason=loop_liveness_watchdog`` and are
    correctly *not* flagged as unattributed deaths. But from a user's point of
    view an ``os._exit`` from a watchdog thread is exactly as abrupt as a
    SIGKILL: no drain ran, so no session was ever told the gateway was going
    down (incident 2026-09-20 16:01).

    Claiming the sentinel for the new life is the moment that record would be
    lost, so mirror it forward under ``prior_*`` keys. Consumers:
    ``gateway.fork_ext.unclean_restart_notice.classify_prior_life`` (tells a
    boot-resumed session why it went quiet) and ``hermes gateway status``.
    Best-effort — never raises.
    """
    try:
        previous = _read_json(get_lifecycle_sentinel_path(home))
        if not previous or previous.get("phase") != "exited":
            return
        claim["prior_phase"] = "exited"
        if previous.get("exit_code") is not None:
            claim["prior_exit_code"] = previous.get("exit_code")
        if previous.get("exit_reason"):
            claim["prior_exit_reason"] = previous.get("exit_reason")
        if previous.get("exited_at"):
            claim["prior_exited_at"] = previous.get("exited_at")
    except Exception:
        logger.debug("Failed to carry prior exit record forward", exc_info=True)


def _claim_sentinel(evidence: Optional[Dict[str, Any]], home: Optional[Path]) -> None:
    try:
        claim: Dict[str, Any] = {
            "phase": "running",
            "pid": os.getpid(),
            "start_time": time.time(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _carry_prior_exit_forward(claim, home)
        # Carry the verdict on the PREVIOUS life forward on the new
        # sentinel: it is the only place the finding survives in
        # machine-readable form (the exit-diag log is append-only prose
        # for humans), and /api/status reads it to tell the user "your
        # agent restarted after (suspected) running out of memory"
        # (NS-656).  Scoped to this life only — the next clean exit or
        # boot rewrites the sentinel and the flags age out with it.
        if evidence is not None:
            claim["prior_unclean_exit"] = True
            if evidence.get("suspected_oom"):
                claim["prior_suspected_oom"] = True
            # Who killed it — so a later `hermes gateway status`/doctor can
            # show the attribution without re-running the forensics.
            if evidence.get("killer"):
                claim["prior_killer"] = evidence["killer"]
            if evidence.get("kill_sender"):
                claim["prior_kill_sender"] = evidence["kill_sender"]
            if evidence.get("kill_sender_label"):
                claim["prior_kill_sender_label"] = evidence["kill_sender_label"]
            if evidence.get("kill_attribution_reason"):
                claim["prior_kill_attribution_reason"] = evidence[
                    "kill_attribution_reason"
                ]
        _write_sentinel(claim, home)
    except Exception:
        logger.debug("Failed to claim lifecycle sentinel", exc_info=True)


def record_startup(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Boot-time entry point: report any unclean previous exit, then claim
    the sentinel for the current life.

    Returns the unclean-exit evidence dict (also persisted to
    ``gateway-exit-diag.log`` and logged at WARNING) or ``None``.  Never
    raises.

    Runs the kill-attribution probe INLINE (blocking, <=
    ``KILL_ATTRIBUTION_TIMEOUT_S``).  From an event loop use
    :func:`record_startup_async` instead.
    """
    evidence: Optional[Dict[str, Any]] = None
    try:
        evidence = detect_unclean_exit(home)
        if evidence is not None:
            _apply_attribution(evidence, _probe_attribution(evidence))
            _emit_unclean_report(evidence, home)
    except Exception:
        logger.debug("Unclean-exit detection failed", exc_info=True)

    _claim_sentinel(evidence, home)
    return evidence


async def record_startup_async(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Event-loop-safe :func:`record_startup`.

    The attribution probe shells out to ``log show`` / ``journalctl``; running
    it on the loop thread would stall platform heartbeats for up to
    ``KILL_ATTRIBUTION_TIMEOUT_S`` at boot.  Offloaded via
    ``asyncio.to_thread`` (the house pattern).  Never raises.
    """
    evidence: Optional[Dict[str, Any]] = None
    try:
        evidence = detect_unclean_exit(home)
        if evidence is not None:
            try:
                attribution = await asyncio.to_thread(_probe_attribution, evidence)
            except Exception:
                logger.debug("Kill-attribution offload failed", exc_info=True)
                attribution = {"killer": "unattributed", "reason": "offload_failed"}
            _apply_attribution(evidence, attribution)
            _emit_unclean_report(evidence, home)
    except Exception:
        logger.debug("Unclean-exit detection failed", exc_info=True)

    _claim_sentinel(evidence, home)
    return evidence


def mark_exited(
    exit_code: Optional[int] = None,
    reason: str = "graceful_shutdown",
    home: Optional[Path] = None,
) -> None:
    """Mark the current life as cleanly exited.  Idempotent, never raises.

    Only rewrites the sentinel when it is provably owned by this process —
    during a ``--replace`` takeover the replacement claims the sentinel
    before the old process finishes teardown, and the old life must not
    clobber the new owner's ``running`` phase on its way out.  A sentinel
    with ``pid=None`` (or a malformed pid) has *unknown* ownership and is
    likewise left alone: we must not overwrite evidence we cannot prove is
    ours with a ``clean exit`` claim.
    """
    try:
        sentinel = _read_json(get_lifecycle_sentinel_path(home))
        if sentinel is not None and sentinel.get("pid") != os.getpid():
            return
        _write_sentinel(
            {
                "phase": "exited",
                "pid": os.getpid(),
                "exit_code": exit_code,
                "exit_reason": reason,
                "exited_at": datetime.now(timezone.utc).isoformat(),
            },
            home,
        )
    except Exception:
        logger.debug("Failed to mark lifecycle sentinel exited", exc_info=True)


def read_prior_exit_label(profile_home: Path) -> str:
    """Container-boot helper: one-word summary of how the profile's last
    gateway life ended.  ``clean`` / ``unclean`` / ``unknown`` (no sentinel
    or never ran).  Read-only and exception-free — used by
    ``hermes_cli.container_boot`` to annotate ``container-boot.log``.
    """
    try:
        sentinel = _read_json(get_lifecycle_sentinel_path(profile_home))
        if not sentinel:
            return "unknown"
        phase = sentinel.get("phase")
        if phase == "exited":
            return "clean"
        if phase == "running":
            # At container boot the old PID namespace is gone — any
            # "running" sentinel is from a life that never exited cleanly.
            return "unclean"
    except Exception:
        pass
    return "unknown"
