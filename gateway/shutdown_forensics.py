"""Shutdown forensics — capture context when the gateway receives SIGTERM/SIGINT.

``shutdown_signal_handler`` runs synchronously inside the asyncio loop, so
:func:`snapshot_shutdown_context` is a fast (<10ms) non-blocking probe and
:func:`spawn_async_diagnostic` is a fire-and-forget ``ps`` walk in a detached
subprocess. Anything that waits belongs in the async helper, never in the probe.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.restart import DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT, resolve_systemd_timeout_stop_sec
import contextlib

_SIGNAL_NAME_BY_NUM: Dict[int, str] = {
    int(getattr(signal, _name)): _name
    for _name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2")
    if getattr(signal, _name, None) is not None
}


def _signal_name(sig: Any) -> str:
    """Human-readable signal name (``str(sig)`` as fallback)."""
    if sig is None:
        return "UNKNOWN"
    try:
        sig_int = int(sig)
    except (TypeError, ValueError):
        return str(sig)
    return _SIGNAL_NAME_BY_NUM.get(sig_int, f"signal#{sig_int}")


def _read_proc_field(pid: int, key: str) -> Optional[str]:
    """Read a single field from /proc/<pid>/status.  Linux only; None elsewhere."""
    with contextlib.suppress(OSError), open(f"/proc/{pid}/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(key + ":"):
                return line.split(":", 1)[1].strip()
    return None


def _proc_summary(pid: int) -> Dict[str, Any]:
    """Compact /proc/<pid> identity (pid, name, state, ppid, uid). Never reads cmdline/argv —
    those bytes are not safe to persist (tokens, URIs, ``-e KEY=`` overlays)."""
    summary: Dict[str, Any] = {"pid": pid}
    if pid <= 0:
        return summary
    for out_key, proc_key in (("name", "Name"), ("state", "State")):
        if (value := _read_proc_field(pid, proc_key)) is not None:
            summary[out_key] = value
    if (ppid := _read_proc_field(pid, "PPid")) is not None:
        with contextlib.suppress(ValueError):
            summary["ppid"] = int(ppid)
    if (uid := _read_proc_field(pid, "Uid")) is not None:
        summary["uid"] = uid.split()[0] if uid else uid  # "real effective saved fs"
    return summary


_SHUTDOWN_SIGNAL_AT: Optional[float] = None


def shutdown_signal_seen_at() -> Optional[float]:
    """Return the monotonic time a shutdown signal was observed, else None.

    Process-global because the consumer is not the gateway object: a running
    AGENT turn needs to know a restart landed mid-turn so it can explain
    itself honestly. Before this existed, a turn whose session append was
    interrupted by a restart reported "This is often a full disk — free some
    space (or fix state.db permissions)" — measured 2026-08-10 on a box with
    6.1 TiB free and zero sqlite errors, which sent the operator hunting a
    problem that did not exist.

    Read via ``shutdown_landed_within(seconds)`` rather than directly.
    """
    return _SHUTDOWN_SIGNAL_AT


def shutdown_landed_within(seconds: float = 300.0) -> bool:
    """True when a shutdown signal was seen in the last *seconds*.

    Bounded so a long-lived process that survived an earlier drain (a
    cancelled restart, a signal the gateway rode out) does not attribute an
    unrelated failure minutes or hours later to that old signal.
    """
    at = _SHUTDOWN_SIGNAL_AT
    if at is None:
        return False
    try:
        return (time.monotonic() - at) <= float(seconds)
    except Exception:
        return False


def _read_marker(path: Path) -> Optional[str]:
    """Return the marker file's text, or None if absent/unreadable."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def snapshot_shutdown_context(received_signal: Any = None) -> Dict[str, Any]:
    """Fast (<10ms) snapshot of who/what is asking us to shut down: signal name/number, own + parent
    /proc summaries, systemd parentage, takeover/planned-stop markers, TracerPid, 1-min load,
    timestamps. Pure stdlib, never raises, never blocks."""
    pid, ppid = os.getpid(), os.getppid()
    monotonic = time.monotonic()
    # Record that a shutdown signal landed, for consumers outside the gateway
    # object (see shutdown_landed_within). Set here rather than in the signal
    # handler so every path that snapshots a shutdown — handler, drain,
    # forensics-only calls — marks it exactly once, and so a caller passing
    # received_signal=None (a non-signal snapshot) does not falsely arm it.
    global _SHUTDOWN_SIGNAL_AT
    if received_signal is not None:
        _SHUTDOWN_SIGNAL_AT = monotonic
    ctx: Dict[str, Any] = {
        "ts": time.time(), "ts_monotonic": monotonic,
        "signal": _signal_name(received_signal),
        "signal_num": int(received_signal) if received_signal is not None else None,
        "pid": pid, "ppid": ppid, "parent": _proc_summary(ppid), "self": _proc_summary(pid),
    }
    # INVOCATION_ID is set by systemd units; ppid==1 also suggests systemd forwarded the SIGTERM.
    for ctx_key, env_key in (("systemd_invocation_id", "INVOCATION_ID"),
                             ("systemd_journal_stream", "JOURNAL_STREAM")):
        if os.environ.get(env_key):
            ctx[ctx_key] = os.environ[env_key]
    ctx["under_systemd"] = bool(os.environ.get("INVOCATION_ID")) or ppid == 1
    # High load points at "something crushing the box" rather than an external killer.
    with contextlib.suppress(OSError, AttributeError):
        ctx["loadavg_1m"] = os.getloadavg()[0]  # admission-lint: not-a-decider shutdown forensics record
    # Nonzero TracerPid means a debugger/strace is attached.
    with contextlib.suppress(TypeError, ValueError):
        if (tracer := _read_proc_field(pid, "TracerPid")) is not None and tracer != "0":
            ctx["tracer_pid"] = int(tracer) if tracer.isdigit() else tracer
            ctx["tracer"] = _proc_summary(int(tracer)) if tracer.isdigit() else None
    # Race hint: a takeover marker on disk that does NOT name us is a smoking gun for "another
    # --replace instance is killing us". Filenames mirror gateway.status; literals keep the signal-
    # handler path import-light.
    with contextlib.suppress(Exception):  # noqa: BLE001 — never raise from a signal handler
        hermes_home_str = os.path.expanduser(os.environ.get("HERMES_HOME", ""))
        if hermes_home_str:
            raw = _read_marker(Path(hermes_home_str) / ".gateway-takeover.json")
            if raw is not None:
                ctx["takeover_marker"] = raw[:300]
                ctx["takeover_marker_for_self"] = (f'"target_pid": {pid}' in raw
                                                   or f"'target_pid': {pid}" in raw)
            raw = _read_marker(Path(hermes_home_str) / ".gateway-planned-stop.json")
            if raw is not None:
                ctx["planned_stop_marker"] = raw[:300]
    return ctx


def _async_diagnostic_script(signal_name: str, self_pid: int) -> str:
    """POSIX listing used by the detached diagnostic. Columns are identity/resource only — no argv."""
    return (
        f"echo '=== shutdown diagnostic @ {signal_name} ==='; "
        "echo '--- date ---'; date -u +%Y-%m-%dT%H:%M:%SZ; "
        "echo '--- ps (top 60 by cpu, comm only) ---'; "
        # ``sort`` instead of GNU ``--sort=-pcpu`` so BSD ps (macOS) produces a listing too; the header
        # line is echoed first so ``sort`` does not bury it among the 0.0-cpu rows.
        "ps -eo pid,ppid,user,pcpu,pmem,stat,comm 2>/dev/null | { IFS= read -r h; echo \"$h\"; sort -nrk4; } | head -60; "
        f"echo '--- pstree of self ---'; pstree -pl {self_pid} 2>/dev/null | head -40 || true; "
        "echo '--- loadavg ---'; cat /proc/loadavg 2>/dev/null || sysctl -n vm.loadavg 2>/dev/null || true; "  # admission-lint: not-a-decider shutdown forensics dump
        "echo '--- recent dmesg (oom/killed) ---'; "
        "dmesg -T 2>/dev/null | tail -20 || journalctl --user -n 20 --no-pager 2>/dev/null | tail -20 || true; "
        "echo '=== end ==='"
    )


def spawn_async_diagnostic(log_path: Path, signal_name: str, *,
                           timeout_seconds: float = 5.0) -> Optional[int]:
    """Fire-and-forget ``ps``-style snapshot appended to ``log_path``: a detached subprocess (own
    ``timeout`` so a wedged ``ps`` self-cleans) rather than a blocking process listing in the signal
    handler, which can freeze the loop >2s on a busy host. Returns the subprocess PID, or ``None``
    on failure / Windows (bash -c is available on every POSIX target; Windows has no ps anyway).
    The listing is comm-only: full argv is not persisted.
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if sys.platform == "win32":
        return None
    script = _async_diagnostic_script(signal_name, os.getpid())
    try:  # O_APPEND so concurrent diagnostics from rapid signals don't trample each other
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return None
    with contextlib.suppress(OSError):  # tighten logs created 0644 by earlier releases
        os.fchmod(fd, 0o600)
    # GNU ``timeout`` (Homebrew: ``gtimeout``) is absent from stock macOS; without it the detached
    # script still cannot block teardown, so run it unbounded rather than skip the diagnostic.
    timeout_bin = shutil.which("timeout") or shutil.which("gtimeout")
    bound = [timeout_bin, f"{timeout_seconds:.0f}"] if timeout_bin else []
    try:  # start_new_session: outlive systemd killing our cgroup (KillMode=control-group) to flush
        return subprocess.Popen(
            [*bound, "bash", "-c", script], stdout=fd,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
            close_fds=True).pid
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):  # subprocess inherited the fd; drop our handle
            os.close(fd)


# ── console-session loss (t_06816049) ────────────────────────────────────────
# 2026-11-22 21:23:35 PT launchd SIGTERM'd all 9 gateways because the console session was torn down
# (WindowServer exited a second earlier). The detached ``ps`` above listed nothing during that teardown
# and the home-channel notice read like a routine restart. The owner of /dev/console is the cheap,
# in-process witness: on macOS it is the logged-in GUI user and reverts to root when the session dies.
CONSOLE_PATH = "/dev/console"
SESSION_LOST_FIRST_LINE = (
    "console session lost: every service of this user is being stopped; not a planned restart")
SESSION_LOST_PRODUCER = "gateway-console-session-lost"
INPROCESS_DIAG_BUDGET_S = 1.0


def console_owner_uid() -> Optional[int]:
    """uid owning /dev/console, None when it cannot be read. One stat, never blocks."""
    try:
        return os.stat(CONSOLE_PATH).st_uid
    except (OSError, AttributeError):
        return None


def _user_name(uid: Optional[int]) -> str:
    if uid is None:
        return "?"
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError):
        return str(uid)


def _own_uid() -> Optional[int]:
    getuid = getattr(os, "getuid", None)
    return getuid() if callable(getuid) else None


def console_session_lost(baseline_uid: Optional[int], current_uid: Optional[int]) -> bool:
    """True when the console WAS this process's user (at gateway start) and no longer is. A host whose
    console never belonged to the gateway user (a Linux service, a headless Mac) never reads as lost."""
    own = _own_uid()
    return own is not None and baseline_uid == own and current_uid is not None and current_uid != own


def _process_rows(deadline: float) -> tuple:
    """(rows, total_seen, truncated): the process table read in-process (psutil = sysctl/libproc on macOS,
    /proc on Linux), no fork. Stops at ``deadline`` (monotonic). Identity columns only, never argv."""
    rows: List[str] = []
    truncated = False
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        attrs = ["pid", "ppid", "username", "status", "name"]

        def _row(i: Dict[str, Any]) -> str:
            return f"{i.get('pid')} {i.get('ppid')} {i.get('username')} {i.get('status')} {i.get('name')}"
        with contextlib.suppress(Exception):  # self first: a budget cut must not drop the gateway's own row
            rows.append(_row(psutil.Process().as_dict(attrs, ad_value="?")))
        for proc in psutil.process_iter(attrs, ad_value="?"):
            if time.monotonic() >= deadline:
                truncated = True
                break
            if proc.pid != os.getpid():
                rows.append(_row(proc.info))
        return rows, len(rows), truncated
    with contextlib.suppress(OSError):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            if time.monotonic() >= deadline:
                truncated = True
                break
            s = _proc_summary(int(entry))
            rows.append(f"{s.get('pid')} {s.get('ppid', '?')} {_user_name(int(s['uid'])) if str(s.get('uid', '')).isdigit() else '?'} "
                        f"{s.get('state', '?')} {s.get('name', '?')}")
    return rows, len(rows), truncated


def write_inprocess_diagnostic(log_path: Path, signal_name: str, *, console_uid: Optional[int],
                               baseline_console_uid: Optional[int], restart_requested: Optional[str],
                               budget_s: float = INPROCESS_DIAG_BUDGET_S) -> bool:
    """Append a diagnostic block that needs no child process: console user, restart record and the process
    table. Bounded by ``budget_s`` (the listing stops at the deadline and says so). True when written."""
    deadline = time.monotonic() + max(0.05, budget_s * 0.8)
    own = _own_uid()
    lines = [
        f"=== shutdown diagnostic (in-process) @ {signal_name} ===",
        "--- date ---", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "--- console ---",
        f"console_user={_user_name(console_uid)} (uid {console_uid}) "
        f"console_user_at_start={_user_name(baseline_console_uid)} (uid {baseline_console_uid}) "
        f"gateway_user={_user_name(own)} (uid {own}) "
        f"session_lost={'yes' if console_session_lost(baseline_console_uid, console_uid) else 'no'}",
        "--- restart requested ---", f"restart_requested={restart_requested or 'no'}",
    ]
    try:
        rows, seen, truncated = _process_rows(deadline)
    except Exception as exc:  # noqa: BLE001 — a diagnostic never raises into the signal handler
        rows, seen, truncated = [f"process table unreadable: {type(exc).__name__}: {exc}"], 0, False
    lines.append(f"--- processes (in-process, no ps; {seen} listed{', TRUNCATED at budget' if truncated else ''}) ---")
    lines.append("PID PPID USER STAT COMM")
    lines.extend(rows)
    lines.append("=== end ===")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return False
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8", errors="replace"))
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def session_lost_page_text(signal_name: str, console_uid: Optional[int], hermes_home: str) -> str:
    import socket
    return (f"{SESSION_LOST_FIRST_LINE}\n"
            f"host {socket.gethostname()}: gateway `{hermes_home}` got {signal_name} with no restart requested; "
            f"/dev/console now belongs to {_user_name(console_uid)}, not {_user_name(_own_uid())}.")


def page_console_session_lost(text: str) -> bool:
    """Fire-and-forget critical page through the fleet pager (episode_pager.py -> fleet_alert -> notify.py, which
    writes the notify-sends ledger). One key per 10-min window, so the N gateways of one teardown page once.
    Popen only (no wait): the sender is in its own session and outlives this process. False when not started."""
    try:
        from hermes_constants import get_default_hermes_root
        script = get_default_hermes_root() / "scripts" / "lib" / "episode_pager.py"
        if not script.is_file():
            return False
        key = f"console-lost-{int(time.time() // 600)}"
        subprocess.Popen(
            [sys.executable, str(script), "page", "--producer", SESSION_LOST_PRODUCER, "--key", key,
             "--via", "fleet-alert", "--severity", "critical", "--text", text],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def format_context_for_log(ctx: Dict[str, Any]) -> str:
    """Render a shutdown context dict as one scannable log line (parent identity, never argv)."""
    parent = ctx.get("parent") or {}
    load_str = f"{load:.2f}" if isinstance(load := ctx.get("loadavg_1m"), (int, float)) else "?"
    extras: List[str] = []
    if ctx.get("takeover_marker") is not None:
        who = 'self' if ctx.get('takeover_marker_for_self') else 'other'
        extras.append(f"takeover_marker_present={who}")
    if ctx.get("planned_stop_marker") is not None:
        extras.append("planned_stop_marker_present=yes")
    if ctx.get("tracer_pid"):
        extras.append(f"tracer_pid={ctx['tracer_pid']}")
    extras_str = (" " + " ".join(extras)) if extras else ""
    return (
        f"signal={ctx.get('signal', '?')} under_systemd={'yes' if ctx.get('under_systemd') else 'no'} "
        f"parent_pid={parent.get('pid') or '?'} parent_name={parent.get('name') or '?'} "
        f"loadavg_1m={load_str}{extras_str}"
    )


def context_as_json(ctx: Dict[str, Any]) -> str:
    """JSON-serialise a context dict for structured ingestion.  Never raises."""
    try:
        return json.dumps(ctx, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def check_systemd_timing_alignment(
    drain_timeout: float, cron_drain_timeout: float = DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
) -> Optional[Dict[str, Any]]:
    """At startup, sanity-check that systemd's TimeoutStopSec covers stop. A stale unit file
    (upgraded without re-running ``hermes setup``) can have ``TimeoutStopSec`` below the stop
    budget, so systemd SIGKILLs the cgroup mid-drain (a phantom ``code=killed status=9`` in the
    journal). ``None`` when aligned OR undeterminable (not under systemd, no ``systemctl``);
    otherwise a dict with ``timeout_stop_sec``/``drain_timeout``/``expected_min``/``mismatch``.
    """
    if not os.environ.get("INVOCATION_ID"):
        return None  # Not running under systemd (or at least not directly)
    # /proc/self/cgroup: "0::/user.slice/.../hermes-gateway.service"
    unit_name: Optional[str] = None
    with contextlib.suppress(OSError), open("/proc/self/cgroup", encoding="utf-8") as fh:
        for line in fh:
            parts = reversed(line.strip().split("/"))
            unit_name = next((p for p in parts if p.endswith(".service")), None)
            if unit_name:
                break
    if (timeout_us := _systemd_timeout_stop_us(unit_name) if unit_name else None) is None:
        return None
    timeout_stop_sec = timeout_us / 1_000_000.0
    expected = float(resolve_systemd_timeout_stop_sec(drain_timeout, cron_drain_timeout))
    return {"unit": unit_name, "timeout_stop_sec": timeout_stop_sec, "drain_timeout": drain_timeout,
            "cron_drain_timeout": cron_drain_timeout, "expected_min": expected,
            "mismatch": timeout_stop_sec < expected}


def _systemd_timeout_stop_us(unit_name: str) -> Optional[int]:
    """``TimeoutStopUSec`` of ``unit_name`` in microseconds; ``--user`` first (hermes' usual)."""
    for flag in (["--user"], []):
        try:
            result = subprocess.run(
                ["systemctl", *flag, "show", unit_name, "--property=TimeoutStopUSec"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2.0,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        # Output: "TimeoutStopUSec=1min 30s" or "TimeoutStopUSec=90000000"
        for line in result.stdout.splitlines() if result.returncode == 0 else ():
            if line.startswith("TimeoutStopUSec="):
                value = line.split("=", 1)[1].strip()
                timeout_us = int(value) if value.isdigit() else parse_systemd_duration_to_us(value)
                if timeout_us is not None:
                    return timeout_us
    return None


def parse_systemd_duration_to_us(raw: str) -> Optional[int]:
    """Parse 'TimeoutStopUSec=1min 30s' / '90s' style values to microseconds. Covers us, ms, s, min,
    h, d, w, month, y; a bare number is seconds. None on anything unexpected; never raises. Public: also consumed by
    hermes_cli.gateway's restart-wait sizing.
    """
    if not raw:
        return None
    units = {"us": 1, "ms": 1_000, "s": 1_000_000, "sec": 1_000_000,
             "min": 60_000_000, "h": 3_600_000_000, "hr": 3_600_000_000,
             # Fixed systemd time-util.h constants, not variable calendar months/years.
             "d": 86_400_000_000, "w": 604_800_000_000,
             "month": 2_629_800_000_000, "y": 31_557_600_000_000}
    total_us, token, digits = 0, "", ""

    def _flush() -> bool:  # fold the pending digits/token pair into total_us
        nonlocal total_us, token, digits
        multiplier = units.get(token.lower()) if token else 1_000_000
        if multiplier is None or not digits:
            return False
        try:
            total_us += int(float(digits) * multiplier)
        except (ValueError, OverflowError):
            return False
        digits = token = ""
        return True
    for ch in raw + " ":
        if ch.isdigit() or ch == ".":
            if token and not _flush():  # a digit after a unit ends the previous number
                return None
            digits += ch
        elif ch.isalpha():
            token += ch
        elif digits and not _flush():
            return None
    return total_us if total_us > 0 else None

