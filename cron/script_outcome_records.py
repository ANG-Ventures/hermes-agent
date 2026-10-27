"""Durable outcome records for no_agent cron scripts that outlive the scheduler (t_854cd586).

A no_agent script runs in its own session (``start_new_session=True``), so a gateway restart
that does not drain it leaves the script running as an orphan. The script finishes, but the
thread that would have read its exit status and called ``mark_job_run`` died with the gateway.
Restart recovery could then only write ``unknown`` (``recover_interrupted_executions``), and the
job read Down until its next natural fire although it had finished cleanly (media-brain-bridge
2026-10-26: script finished 03:14:55, gateway restarted 03:09, crons.ace kept the 10-25 run).

The fix gives the script's own exit status a durable home:

* ``begin`` writes ``<cron>/script-runs/<execution_id>.json`` (job, execution, owner pid) and
  ``wrap_argv`` runs the script under a tiny ``/bin/sh`` shim that sends the script's stdout and
  stderr to files and writes its exit code to ``<execution_id>.rc`` the moment it exits, then
  replays the captured output on the original pipes. The script itself never writes to the
  scheduler's pipe, so a dead reader can no longer kill it with SIGPIPE either.
* The runner deletes the record once it has observed the exit itself (every normal, timeout and
  cancel path), so a record only survives when the owner process died mid-run.
* ``settle`` runs after restart recovery and on the periodic dead-owner reap. For a record whose
  owner process is gone it reads ``.rc``: the job is graded by that real outcome (when the
  attempt is still the job's newest). A record with no ``.rc`` whose script pid is still alive
  is left for a later pass; one whose script is gone without an ``.rc`` was killed, and stays
  as recovery recorded it.

POSIX only (the shim is ``/bin/sh``); Windows keeps the previous behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("cron.scheduler")

RECORDS_DIRNAME = "script-runs"
# Unparseable/stray records are removed after this long so the directory cannot grow forever.
STRAY_RECORD_MAX_AGE_SECONDS = 7 * 24 * 3600
# The settle output doc keeps the tail of the captured stdout/stderr, not an unbounded file.
_OUTPUT_TAIL_BYTES = 16 * 1024
_SUFFIXES = (".json", ".rc", ".rc.tmp", ".out", ".err")

# $1 = record base path, the rest = the script argv. The exit code is published with an atomic
# rename so a reader never sees a half-written .rc. The replay goes to the original pipes; if
# the reader is gone the shim may die on SIGPIPE, after the .rc is already durable.
_SHIM = (
    # fd 3 keeps the real stderr; the shim's own (e.g. macOS sh's "Terminated: 15") is dropped.
    'rec=$1; shift; exec 3>&2 2>/dev/null; '
    '"$@" >"$rec.out" 2>"$rec.err"; rc=$?; '
    'printf "%s\\n" "$rc" >"$rec.rc.tmp" && mv -f "$rec.rc.tmp" "$rec.rc"; '
    'cat "$rec.out"; cat "$rec.err" >&3; '
    # A signal death is re-raised so Popen still sees -N (the shutdown-kill re-queue reads it).
    'if [ "$rc" -gt 128 ]; then kill -s "$(kill -l "$rc")" $$ 2>/dev/null; fi; '
    'exit "$rc"'
)


def supported() -> bool:
    return sys.platform != "win32" and os.path.isfile("/bin/sh")


def _records_dir() -> Path:
    from cron.jobs import _current_cron_store

    return _current_cron_store().cron_dir / RECORDS_DIRNAME


def _base(execution_id: str) -> Optional[Path]:
    text = str(execution_id or "").strip()
    if not text or not text.replace("-", "").isalnum():
        return None
    return _records_dir() / text


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def begin(job_id: str, execution_id: Optional[str]) -> Optional[Path]:
    """Create the record for one script run; returns its base path, or None (not recorded)."""
    if not execution_id or not job_id or not supported():
        return None
    base = _base(execution_id)
    if base is None:
        return None
    try:
        from cron.executions import _process_start_time
        from cron.jobs import _ensure_cron_dir, _secure_dir

        _ensure_cron_dir(base.parent)
        _secure_dir(base.parent)
        _write_json_atomic(base.with_suffix(".json"), {
            "job_id": str(job_id),
            "execution_id": str(execution_id),
            "owner_pid": os.getpid(),
            "owner_started_at": _process_start_time(os.getpid()),
            "started_at": time.time(),
        })
        return base
    except Exception:
        logger.debug("Job '%s': could not create script outcome record", job_id, exc_info=True)
        discard(base)
        return None


def wrap_argv(base: Path, argv: List[str]) -> List[str]:
    return ["/bin/sh", "-c", _SHIM, "hermes-cron-script", str(base), *argv]


def note_pid(base: Path, pid: int) -> None:
    """Add the script process identity, so settle can tell a still-running orphan from a dead one."""
    meta_path = base.with_suffix(".json")
    try:
        from cron.executions import _process_start_time

        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        meta["script_pid"] = int(pid)
        meta["script_started_at"] = _process_start_time(int(pid))
        _write_json_atomic(meta_path, meta)
    except Exception:
        logger.debug("could not record script pid in %s", meta_path, exc_info=True)


def discard(base: Optional[Path]) -> None:
    if base is None:
        return
    for suffix in _SUFFIXES:
        try:
            os.unlink(str(base) + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("could not remove %s%s", base, suffix, exc_info=True)


def _read_rc(base: Path) -> Optional[int]:
    try:
        return int(Path(str(base) + ".rc").read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None


def _tail(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _OUTPUT_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").strip()
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text)
    except Exception:
        return "[REDACTED - redaction failed]" if text else ""


def _newer_run_recorded(job: Dict[str, Any], meta: Dict[str, Any]) -> bool:
    last_run = job.get("last_run_at")
    if not last_run:
        return False
    try:
        return datetime.fromisoformat(str(last_run)).timestamp() > float(meta.get("started_at") or 0)
    except (TypeError, ValueError):
        return False


def _record_outcome(meta: Dict[str, Any], base: Path, rc: int) -> bool:
    """Grade the job by the script's real exit code. True when ``jobs.json`` was written."""
    from cron.executions import latest_execution
    from cron.jobs import correct_counted_run_outcome, get_job, mark_job_run, save_job_output

    job_id = str(meta["job_id"])
    execution_id = str(meta["execution_id"])
    latest = latest_execution(job_id)
    if latest is not None and latest.get("id") != execution_id:
        # A newer attempt exists (an interval job fired again, or someone ran it by hand):
        # its own result is the current one.
        logger.info(
            "Job '%s': script outcome record %s is older than the newest attempt %s; not applied",
            job_id, execution_id, latest.get("id"))
        return False
    job = get_job(job_id)
    if job is None:
        return False
    # Recorded after this run started = restart recovery (or the shutdown mark) already counted
    # this run via mark_job_run. With the attempt still the newest, that write is about THIS run.
    already_counted = _newer_run_recorded(job, meta)
    if latest is None and already_counted:
        return False  # attempt pruned from the ledger and the job has run since
    try:
        finished = Path(str(base) + ".rc").stat().st_mtime
    except OSError:
        finished = time.time()
    finished_iso = datetime.fromtimestamp(finished).astimezone().isoformat()
    success = rc == 0
    error = None
    if not success:
        error = (f"Script exited with code {rc} (read from the script's own exit status after "
                 "the scheduler restarted mid-run)")
    if already_counted:
        # Replace the unknown/interrupted stamp; never count the run a second time (repeat,
        # next_run_at and streaks were advanced by that first write).
        if not correct_counted_run_outcome(job_id, success, error, last_run_at=finished_iso):
            return False
    elif not mark_job_run(job_id, success, error):
        return False
    name = job.get("name") or job_id
    stdout = _tail(Path(str(base) + ".out"))
    stderr = _tail(Path(str(base) + ".err"))
    doc = (
        f"# Cron Job: {name}\n\n**Job ID:** {job_id}\n"
        f"**Run Time:** {finished_iso}\n**Mode:** no_agent (script, settled after restart)\n\n"
        f"**Status:** {'ok' if success else f'exit {rc}'} — the scheduler restarted while this "
        "script ran; this record was written from the script's own exit status. Its output "
        "was not delivered.\n\n---\n\n"
        + (stdout or "(no stdout)")
        + (f"\n\nstderr:\n{stderr}" if stderr else "")
        + "\n"
    )
    try:
        save_job_output(job_id, doc)
    except Exception:
        logger.debug("Job '%s': could not save settled output", job_id, exc_info=True)
    logger.warning(
        "Job '%s': run %s outlived a scheduler restart and exited %d; recorded as %s from its "
        "own exit status (t_854cd586)", name, execution_id, rc, "ok" if success else "error")
    return True


def settle() -> int:
    """Apply every outcome record whose owner process is gone. Returns how many jobs were graded."""
    if not supported():
        return 0
    try:
        directory = _records_dir()
        names = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    from cron.executions import _owner_is_live

    graded = 0
    for name in names:
        if not name.endswith(".json") or name.endswith(".json.tmp"):
            continue
        meta_path = directory / name
        base = meta_path.with_suffix("")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            owner_pid = int(meta["owner_pid"])
            meta["job_id"], meta["execution_id"]
        except FileNotFoundError:
            continue
        except Exception:
            try:
                if time.time() - meta_path.stat().st_mtime > STRAY_RECORD_MAX_AGE_SECONDS:
                    discard(base)
            except OSError:
                pass
            continue
        if _owner_is_live(owner_pid, meta.get("owner_started_at")):
            continue  # the owner is still waiting on this script and will record it itself
        rc = _read_rc(base)
        if rc is None:
            script_pid = meta.get("script_pid")
            if script_pid and _owner_is_live(int(script_pid), meta.get("script_started_at")):
                continue  # orphan still running; a later pass reads its exit status
            if script_pid is None and time.time() - float(meta.get("started_at") or 0) < 60:
                continue  # owner died between begin() and Popen; give the record a moment
            logger.info(
                "Job '%s': script run %s ended without an exit status (killed); left as restart "
                "recovery recorded it", meta.get("job_id"), meta.get("execution_id"))
            discard(base)
            continue
        try:
            if _record_outcome(meta, base, rc):
                graded += 1
        except Exception:
            logger.warning("Job '%s': applying script outcome record failed",
                           meta.get("job_id"), exc_info=True)
            continue
        discard(base)
    return graded
