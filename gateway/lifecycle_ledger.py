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
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
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

def _process_hermes_home() -> Path:
    """HERMES_HOME for process-level identity files (ignore task overrides)."""
    from hermes_constants import get_hermes_home, get_process_hermes_home

    # get_process_hermes_home expands ``~``/``$VAR`` (python -m gateway.run skips the CLI normalizer).
    return get_process_hermes_home() if os.environ.get("HERMES_HOME", "").strip() else get_hermes_home()


def get_lifecycle_sentinel_path(home: Optional[Path] = None) -> Path:
    """Return ``<HERMES_HOME>/state/gateway.lifecycle.json``."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_LIFECYCLE_RELATIVE)


def get_teardown_timing_path(home: Optional[Path] = None) -> Path:
    """Return the last completed post-drain teardown timing path."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_TEARDOWN_TIMING_RELATIVE)


def read_last_teardown_seconds(
    home: Optional[Path] = None,
    *,
    max_seconds: Optional[float] = None,
) -> Optional[float]:
    """Read the last completed post-drain teardown duration, if valid.

    Two bounds keep a single anomalous sample from starving the next
    shutdown's drain to zero — the reserve is an input to a time budget,
    so it must never be unbounded:

    * Only samples recorded by a *budgeted* (signal-driven, launchd-timed)
      stop are representative of the next SIGTERM. An unconstrained stop —
      ``hermes gateway stop``, Ctrl+C, a foreground run — has no supervisor
      deadline, so its post-drain work can legitimately run far longer
      than any launchd budget. Records written before this field existed
      are treated as unbudgeted for the same reason.
    * ``max_seconds`` rejects a sample too large to be *actionable* under
      the live budget. A teardown that already meets or exceeds the whole
      usable window cannot be reserved for; honouring it would zero the
      drain and drop in-flight sessions while still not making the
      teardown fit. The bound is inclusive — a sample exactly AT the
      ceiling drives the drain to precisely zero, which is the
      starvation this rejects, not a usable reserve.

    ``inf``/``nan`` are rejected outright: no completed teardown can
    produce them, so they only arrive from a corrupt or hand-edited file.
    """
    data = _read_json(get_teardown_timing_path(home)) or {}
    raw = data.get("teardown_seconds")
    if raw is None:
        return None
    if not bool(data.get("budgeted")):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value < 0.0:
        return None
    if max_seconds is not None:
        try:
            ceiling = float(max_seconds)
        except (TypeError, ValueError):
            return value
        if math.isfinite(ceiling) and value >= ceiling:
            return None
    return value


def record_teardown_timing(
    teardown_seconds: float,
    *,
    total_shutdown_seconds: float,
    drain_seconds: float,
    budgeted: bool = False,
    home: Optional[Path] = None,
) -> None:
    """Persist and diagnose one completed post-drain teardown measurement.

    ``budgeted`` marks a stop that ran under a supervisor deadline, i.e.
    the only kind whose duration predicts the next SIGTERM. An unbudgeted
    sample is appended to the exit-diag log for diagnostics but must NOT
    reach the single-slot reserve file: writing it there clobbers the last
    actionable measurement (a manual ``hermes gateway stop`` between two
    SIGTERMs would erase a recorded 40s teardown, so the next launchd stop
    reserves the 15s default and can be SIGKILLed mid-persistence).
    """
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
        "budgeted": bool(budgeted),
    }
    path = get_teardown_timing_path(home)
    if budgeted:
        try:
            from utils import atomic_json_write

            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(path, record, indent=None)
        except Exception:
            logger.debug("Failed to persist teardown timing", exc_info=True)
    _append_exit_diag(record, home)


def _proc_fields(path: str, wanted: Dict[str, str]) -> Dict[str, int]:
    """``{dst: int}`` for each ``src: dst`` key found in a ``Key: value`` /proc file."""
    found: Dict[str, int] = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                key = line.split(":", 1)[0]
                if key in wanted:
                    found[wanted[key]] = int(line.split()[1])
                    if len(found) == len(wanted):
                        break
    except (OSError, ValueError, IndexError):
        return {}
    return found


def sample_memory() -> Dict[str, Any]:
    """Cheap /proc snapshot (KiB): own RSS + MemTotal/MemAvailable + swap used.  Linux-only
    (``{}`` elsewhere), never raises; the 30s heartbeat embeds it so OOM cycles are classifiable."""
    sample = _proc_fields("/proc/self/status", {"VmRSS": "rss_kib"})
    mem = _proc_fields("/proc/meminfo", {"MemTotal": "mem_total_kib", "MemAvailable": "mem_available_kib",
                                         "SwapTotal": "SwapTotal", "SwapFree": "SwapFree"})
    swap_total, swap_free = mem.pop("SwapTotal", None), mem.pop("SwapFree", None)
    sample.update(mem)
    if swap_total is not None and swap_free is not None:
        sample["swap_used_kib"] = swap_total - swap_free
    return sample


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_sentinel(payload: Dict[str, Any], home: Optional[Path]) -> None:
    try:
        from utils import atomic_json_write

        path = get_lifecycle_sentinel_path(home)
        from hermes_constants import mkdir_under_hermes_home

        mkdir_under_hermes_home(path.parent)
        atomic_json_write(path, payload, indent=None)
    except Exception:
        logger.debug("Failed to write lifecycle sentinel", exc_info=True)


def _append_exit_diag(record: Dict[str, Any], home: Optional[Path]) -> None:
    """Append a JSON line to gateway-exit-diag.log (same format as the CLI's
    ``_exit_diag`` records so existing tooling greps both)."""
    try:
        base = home if home is not None else _process_hermes_home()
        path = base.joinpath(*_EXIT_DIAG_RELATIVE)
        from hermes_constants import mkdir_under_hermes_home

        mkdir_under_hermes_home(path.parent)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        logger.debug("Failed to append unclean-exit record", exc_info=True)


def _pid_is_sentinel_owner(pid: Any, start_time: Any, create_time: Any) -> bool:
    """True when ``pid`` is a live process that is the sentinel's incarnation — guards the
    ``--replace`` race: a live matching owner mid-teardown is a handover, not a death.

    Identity is the psutil ``create_time`` the sentinel stamps at claim (epoch seconds, same
    producer on both sides). The sentinel's ``start_time`` is the ledger claim time and is NOT
    comparable with ``gateway.status.get_process_start_time`` (proc ticks on Linux, centiseconds
    elsewhere) — the old comparison never matched, so every ``--replace`` handover read as an
    unclean death. A pre-stamp sentinel (no ``create_time``) still has ``start_time`` in epoch
    seconds: the owner was born BEFORE it claimed, a PID reuser AFTER the owner died, so a birth
    later than the claim is a reuser."""
    try:
        pid_int = int(pid)
        # NOT os.kill(pid, 0): on Windows that sends CTRL_C_EVENT to the target's console group.
        from gateway.status import _pid_exists

        if pid_int <= 0 or not _pid_exists(pid_int):
            return False
    except Exception:
        return False
    from hermes_cli.process_identity import _process_create_time

    actual = _process_create_time(pid_int)
    if actual is None:
        return True  # can't disambiguate PID reuse
    if type(create_time) in (int, float):
        return abs(actual - float(create_time)) <= 2.0
    if type(start_time) in (int, float):
        return actual <= float(start_time) + 2.0
    return True


def _suspected_oom(mem: Dict[str, Any]) -> bool:
    """Heuristic only (classification stays with the reader); thresholds are
    memory_status' "critical" tier so a live warning and a post-mortem verdict agree."""
    from gateway.memory_status import _CRITICAL_AVAILABLE_FRACTION, _CRITICAL_AVAILABLE_KIB

    total, avail = mem.get("mem_total_kib"), mem.get("mem_available_kib")
    if not isinstance(avail, int):
        return False
    return avail < _CRITICAL_AVAILABLE_KIB or (
        isinstance(total, int) and total > 0 and avail / total < _CRITICAL_AVAILABLE_FRACTION
    )


def _run_log_command(argv: list, timeout: float) -> str:
    """Run a read-only forensics command, return stdout (never raises here —
    the caller's try/except owns fail-open)."""
    proc = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        text=True, encoding="utf-8",
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


def _iso_not_before(value: Any, floor: Any) -> bool:
    """``value >= floor`` for ISO timestamps; True when either is unparseable."""
    try:
        v, f = datetime.fromisoformat(value), datetime.fromisoformat(floor)
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        if f.tzinfo is None:
            f = f.replace(tzinfo=timezone.utc)
        return v >= f
    except (TypeError, ValueError):
        return True


def detect_unclean_exit(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Inspect the previous life's sentinel; return an evidence dict when it
    died uncleanly, else ``None``.  Read-only — does not rewrite the sentinel.
    """
    sentinel = _read_json(get_lifecycle_sentinel_path(home))
    if not sentinel or sentinel.get("phase") != "running":
        return None
    if _pid_is_sentinel_owner(sentinel.get("pid"), sentinel.get("start_time"), sentinel.get("create_time")):
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
    # The heartbeat file outlives its writer: a life that died before its first
    # heartbeat leaves an OLDER life's file behind. Only use it when it names
    # the dead process and postdates its start (pid reuse) (C7 k95).
    if hb and not (
        hb.get("pid") == sentinel.get("pid")
        and _iso_not_before(hb.get("updated_at"), sentinel.get("started_at"))
    ):
        hb = None
    if hb:
        evidence["last_heartbeat_at"] = hb.get("updated_at")
        mem = hb.get("mem")
        if isinstance(mem, dict):
            evidence["last_heartbeat_mem"] = mem
            if _suspected_oom(mem):
                evidence["suspected_oom"] = True
    return evidence


# Startup-watchdog lease held while the unclean-exit integrity check runs (#115542):
# ``PRAGMA quick_check(1)`` is one monolithic SQLite operation and per-call leases
# clamp at 900s, so a single entry lease cannot cover a multi-thousand-second healthy
# check on a huge store — without renewal the watchdog kills the attempt with exit 75
# and the restart loop never reaches the cron ticker. The progress handler below renews
# a phase-owned lease for as long as SQLite is making progress, and always returns 0
# so it never aborts the check; the ok/absent/first-complaint verdicts stay fail-closed.
_INTEGRITY_CHECK_LEASE_PHASE = "state_db_unclean_integrity_check"
_INTEGRITY_CHECK_LEASE_S = 900.0
# VM instructions between progress-handler invocations; each invocation is a
# monotonic-clock compare, renewing at most every _INTEGRITY_CHECK_LEASE_RENEW_S.
_INTEGRITY_CHECK_PROGRESS_OPS = 100_000
_INTEGRITY_CHECK_LEASE_RENEW_S = 60.0


def _install_integrity_check_lease(conn: sqlite3.Connection) -> None:
    """Claim the integrity-check lease and renew it from a SQLite progress handler.

    ``report_startup_progress`` is a no-op when no watchdog is armed and never raises.
    The handler always returns 0 -- it must never abort the verdict PRAGMA.  Synchronous
    by design: no checker worker thread can outlive the check.
    """
    from hermes_startup_watchdog import report_startup_progress

    report_startup_progress(_INTEGRITY_CHECK_LEASE_S, phase=_INTEGRITY_CHECK_LEASE_PHASE)
    last_renew = time.monotonic()

    def _on_progress() -> int:
        nonlocal last_renew
        now = time.monotonic()
        if now - last_renew >= _INTEGRITY_CHECK_LEASE_RENEW_S:
            last_renew = now
            report_startup_progress(_INTEGRITY_CHECK_LEASE_S, phase=_INTEGRITY_CHECK_LEASE_PHASE)
        return 0

    conn.set_progress_handler(_on_progress, _INTEGRITY_CHECK_PROGRESS_OPS)


def check_state_db_integrity(home: Optional[Path] = None) -> str:
    """``"ok"``, ``"absent"``, or the first ``quick_check`` complaint.  Never raises.

    Only after an unclean death — SIGKILL mid-WAL-checkpoint can leave half-written
    b-tree pages.  ``quick_check(1)`` stops at the first problem (~2s on a healthy
    500MB store): cheap once per unclean boot, too costly every boot.  Opened
    normally: a WAL store needs its -shm sidecar for read-only, and the PRAGMA writes nothing.

    Holds a phase-owned startup-watchdog lease for the check duration
    (renewed from a SQLite progress handler, #115542) so a long healthy check
    on a huge store is not mistaken for a parked deadlock.
    """
    base = home if home is not None else _process_hermes_home()
    path = base / "state.db"
    if not path.exists():
        return "absent"
    try:
        with closing(sqlite3.connect(str(path))) as conn:
            _install_integrity_check_lease(conn)
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
    except Exception as exc:
        return f"check-failed: {exc}"
    return "check-failed: no result" if not row or row[0] is None else str(row[0])


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
    """Integrity-check the store, persist the exit-diag record, log at WARNING."""
    # The death may have torn the store; this is the only moment we know to look.
    verdict = evidence["state_db_integrity"] = check_state_db_integrity(home=home)
    if verdict not in ("ok", "absent"):
        logger.error(
            "state.db FAILED integrity check after an unclean gateway exit: %s — sessions may read as "
            "missing until it is repaired. Run `hermes doctor`.",
            verdict,
        )
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tag": "gateway.previous_unclean_exit",
        "pid": os.getpid(),
        **evidence,
    }
    _append_exit_diag(record, home)
    logger.warning(
        "Previous gateway life (pid=%s, started_at=%s) exited UNCLEANLY "
        "(no exit path ran — SIGKILL / OOM / VM death, or a process kill issued by the agent "
        "or one of its descendants, e.g. a pkill/taskkill of the host interpreter image; see #113667). "
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
        if previous.get("pid") is not None:
            claim["prior_pid"] = previous.get("pid")
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
        # Process birth (psutil), distinct from ``start_time`` (the ledger claim, seconds later once
        # imports finish): the Windows start attestation binds PIDs to birth time (#110020 review).
        from hermes_cli.process_identity import _process_create_time

        create_time = _process_create_time(os.getpid())
        if create_time is not None:
            claim["create_time"] = create_time
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
            # Identity + last liveness of the dead life. The boot restart notice
            # joins the safe-restart ledger on pid_before == prior_pid, and a
            # SIGKILLed life has no exited_at, so its last heartbeat is the
            # closest record of WHEN it died (2026-09-24 12:12 incident).
            for _src, _dst in (
                ("prior_pid", "prior_pid"),
                ("prior_started_at", "prior_started_at"),
                ("last_heartbeat_at", "prior_last_heartbeat_at"),
            ):
                if evidence.get(_src) is not None:
                    claim[_dst] = evidence[_src]
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

    EVERY step of the boot record is blocking, not just the probe:

    * ``detect_unclean_exit`` reads the sentinel and the heartbeat file, and
      ``_pid_is_sentinel_owner`` reaches psutil for the prior pid;
    * the attribution probe shells out to ``log show`` / ``journalctl`` for up
      to ``KILL_ATTRIBUTION_TIMEOUT_S``;
    * ``_emit_unclean_report`` appends to ``gateway-exit-diag.log``;
    * ``_claim_sentinel`` ends in ``atomic_json_write`` — ``mkstemp`` +
      ``fsync`` + ``os.replace``, whose duration is unbounded under
      filesystem pressure.

    An earlier shape offloaded only the probe and left the other three on the
    loop thread, so a stalled sentinel rename still starved every platform
    heartbeat at boot (the 2026-09-20 incident class: a rename several
    plain-``def`` frames below a coroutine).  The whole synchronous body is
    therefore run in one worker thread via ``asyncio.to_thread`` (the house
    pattern); :func:`record_startup` is that body verbatim, so the two paths
    cannot drift.  Never raises.
    """
    try:
        return await asyncio.to_thread(record_startup, home)
    except Exception:
        logger.debug("Lifecycle startup record offload failed", exc_info=True)
        return None


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
        exited: Dict[str, Any] = {
            "phase": "exited",
            "pid": os.getpid(),
            "exit_code": exit_code,
            "exit_reason": reason,
            "exited_at": datetime.now(timezone.utc).isoformat(),
        }
        # Carry the incarnation identity: the Windows start attestation matches a clean exit by
        # PID *and* start time so a reused PID's exit cannot vouch for a different life (#110020).
        for key in ("start_time", "create_time"):
            if sentinel is not None and sentinel.get(key) is not None:
                exited[key] = sentinel[key]
        _write_sentinel(exited, home)
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
