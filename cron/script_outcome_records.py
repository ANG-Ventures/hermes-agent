"""Durable outcome records for no_agent cron scripts that outlive the scheduler (t_854cd586).

A no_agent script runs in its own session (``start_new_session=True``), so a gateway restart
that does not drain it leaves the script running as an orphan. The script finishes, but the
thread that would have read its exit status and called ``mark_job_run`` died with the gateway.
Restart recovery could then only write ``unknown`` (``recover_interrupted_executions``), and the
job read Down until its next natural fire although it had finished cleanly (media-brain-bridge
2026-10-26: script finished 03:14:55, gateway restarted 03:09, crons.ace kept the 10-25 run).

The fix gives the script's own exit status a durable home:

* ``begin`` writes ``<cron>/script-runs/<execution_id>.json`` (job, execution, owner pid) and
  ``wrap_argv`` runs the script under a tiny stdlib Python shim that sends the script's stdout and
  stderr to files and writes its exit code to ``<execution_id>.rc`` the moment it exits, then
  replays the captured output on the original pipes. The script itself never writes to the
  scheduler's pipe, so a dead reader can no longer kill it with SIGPIPE either.
* The record is deleted once the owner has durably recorded the run (``finish_execution`` wrote
  the attempt's terminal row), so a record only survives when the owner process died before that.
* ``settle`` runs after restart recovery and on the periodic dead-owner reap. For a record whose
  owner process is gone it reads ``.rc``: the job is graded by that real outcome (when the
  attempt is still the job's newest). A record with no ``.rc`` whose script pid is still alive
  is left for a later pass; one whose script is gone without an ``.rc`` was killed, and stays
  as recovery recorded it.

POSIX only (the shim re-raises signals and relies on sessions); Windows keeps the previous
behaviour.
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
_SUFFIXES = (".json", ".pid", ".rc", ".rc.tmp", ".out", ".err")
# A record with no script identity at all (no note_pid, no shim .pid) this long after begin():
# the shim never started (its first act writes .pid), so no exit status can ever arrive.
_NO_SCRIPT_GRACE_SECONDS = 60

# argv[1] = record base path, the rest = the script argv. The shim is this interpreter run in
# isolated mode with the stdlib only, so the job's PATH and PYTHON* env cannot decide whether the
# exit status is published or the output replayed. Its own pid is written first, so settle knows
# the script's identity even when the owner died before note_pid. The script gets only fds 0-2
# (its output goes to files), so a helper it backgrounds never holds the runner's pipes open. The
# exit code is published with an atomic rename so a reader never sees a half-written .rc. The
# replay goes to the original pipes; if the reader is gone it is dropped, after the .rc is
# already durable. A shell shim cannot be used: ``$?`` is 143 both for ``exit 143`` and for a
# SIGTERM death, so re-raising every status above 128 turned a normal exit into a shutdown kill
# and made an already-run script eligible for the shutdown-kill re-fire (Prism 2b28c5552d47).
# The wait status read here keeps the two apart: only a real signal death is re-raised, so Popen
# still sees -N for it; the .rc keeps the shell convention (128+N) for settle's grading.
_SHIM = r"""
import os, signal, subprocess, sys
rec = sys.argv[1]
for name in ("SIGINT", "SIGPIPE"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
with open(rec + ".pid", "w") as fh:
    fh.write("%d\n" % os.getpid())
try:
    with open(rec + ".out", "wb") as out, open(rec + ".err", "wb") as err:
        code = subprocess.call(sys.argv[2:], stdout=out, stderr=err)
except OSError as exc:
    with open(rec + ".err", "ab") as err:
        err.write(("%s\n" % exc).encode())
    code = 127
rc = 128 - code if code < 0 else code
with open(rec + ".rc.tmp", "w") as fh:
    fh.write("%d\n" % rc)
os.replace(rec + ".rc.tmp", rec + ".rc")
for suffix, fd in ((".out", 1), (".err", 2)):
    try:
        with open(rec + suffix, "rb") as fh:
            data = fh.read()
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass
if code < 0:
    try:
        signal.signal(-code, signal.SIG_DFL)
    except (OSError, ValueError):
        pass  # SIGKILL/SIGSTOP take no handler; already default
    os.kill(os.getpid(), -code)
sys.exit(rc)
"""


def supported() -> bool:
    return sys.platform != "win32" and bool(sys.executable) and os.path.isfile(sys.executable)


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
    return [sys.executable, "-I", "-c", _SHIM, str(base), *argv]


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


def discard_execution(execution_id: Optional[str]) -> None:
    """The owner durably recorded this attempt: its outcome record is no longer evidence."""
    if not execution_id or not supported():
        return
    try:
        discard(_base(execution_id))
    except Exception:
        logger.debug("could not discard outcome record of %s", execution_id, exc_info=True)


def _read_int(base: Path, suffix: str) -> Optional[int]:
    try:
        return int(Path(str(base) + suffix).read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None


def _read_rc(base: Path) -> Optional[int]:
    return _read_int(base, ".rc")


def _script_alive(meta: Dict[str, Any], base: Path) -> Optional[bool]:
    """True/False when the script's identity is known; None when nothing identifies it yet."""
    from cron.executions import _owner_is_live, _process_start_time

    script_pid = meta.get("script_pid")
    if script_pid:
        return _owner_is_live(int(script_pid), meta.get("script_started_at"))
    shim_pid = _read_int(base, ".pid")
    if not shim_pid:
        return None
    # The shim wrote its own pid; no start time was persisted, so a live pid counts only when
    # that process started after begin() (a recycled pid is older than the record).
    try:
        from gateway.status import _pid_exists

        if not _pid_exists(shim_pid):
            return False
    except Exception:
        return True
    started = _process_start_time(shim_pid)
    if started is None:
        return True  # cannot compare -> cannot prove death
    begun = float(meta.get("started_at") or 0)
    # get_process_start_time is not always epoch seconds (Linux: clock ticks since boot);
    # compare only when it looks like an epoch value, else treat the live pid as the shim.
    if started > 10**9 and started + 2 < begun:
        return False
    return True


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


APPLIED, SKIPPED, RETRY = "applied", "skipped", "retry"


def _record_outcome(meta: Dict[str, Any], base: Path, rc: int) -> str:
    """Grade the job by the script's real exit code.

    APPLIED: ``jobs.json`` was written. SKIPPED: the outcome must never be applied (a newer
    attempt, the job deleted, or another settler already consumed the record). RETRY: the job's
    fire fence could not be taken; keep the record for a later pass.

    The whole consumption happens under one fire fence: the record is re-read (``rc`` passed in
    was read outside it), applied, its output saved and the record removed before the fence is
    released. A second settler that read the same record earlier then finds it gone and applies
    nothing (Prism e9387d975af3), and an attempt that finishes meanwhile cannot be overwritten.
    """
    from cron.executions import latest_execution
    from cron.jobs import (
        _fire_job_lock, correct_counted_run_outcome, get_job, mark_job_run, save_job_output,
    )

    job_id = str(meta["job_id"])
    execution_id = str(meta["execution_id"])
    with _fire_job_lock(job_id) as acquired:
        if not acquired:
            logger.warning("Job '%s': fire fence busy; script outcome %s kept for a later pass",
                           job_id, execution_id)
            return RETRY
        # Revalidate under the fence: a concurrent settler may have consumed the record since
        # the caller read it. No .json or no .rc now = nothing left to apply.
        rc = _read_rc(base)
        try:
            finished = Path(str(base) + ".rc").stat().st_mtime
            Path(str(base) + ".json").stat()
        except OSError:
            rc = None
        if rc is None:
            logger.info("Job '%s': script outcome record %s already consumed; not applied",
                        job_id, execution_id)
            return SKIPPED
        finished_iso = datetime.fromtimestamp(finished).astimezone().isoformat()
        success = rc == 0
        error = None
        if not success:
            error = (f"Script exited with code {rc} (read from the script's own exit status "
                     "after the scheduler restarted mid-run)")
        latest = latest_execution(job_id)
        if latest is not None and latest.get("id") != execution_id:
            # A newer attempt exists (an interval job fired again, or someone ran it by hand):
            # its own result is the current one.
            logger.info(
                "Job '%s': script outcome record %s is older than the newest attempt %s; "
                "not applied", job_id, execution_id, latest.get("id"))
            discard(base)
            return SKIPPED
        job = get_job(job_id)
        if job is None:
            discard(base)
            return SKIPPED
        # Recorded after this run started = restart recovery (or the shutdown mark) already
        # counted this run via mark_job_run. With the attempt still the newest, that write is
        # about THIS run.
        already_counted = _newer_run_recorded(job, meta)
        if latest is None and already_counted:
            discard(base)
            return SKIPPED  # attempt pruned from the ledger and the job has run since
        name = job.get("name") or job_id
        stdout = _tail(Path(str(base) + ".out"))
        stderr = _tail(Path(str(base) + ".err"))
        doc = (
            f"# Cron Job: {name}\n\n**Job ID:** {job_id}\n"
            f"**Run Time:** {finished_iso}\n**Mode:** no_agent (script, settled after restart)\n\n"
            f"**Status:** {'ok' if success else f'exit {rc}'} — the scheduler restarted while "
            "this script ran; this record was written from the script's own exit status. Its "
            "output was not delivered.\n\n---\n\n"
            + (stdout or "(no stdout)")
            + (f"\n\nstderr:\n{stderr}" if stderr else "")
            + "\n"
        )
        # Save the output BEFORE grading the run: with the owner dead the record holds the only
        # copy, so a failed save keeps it and grades nothing; the next pass retries both
        # (Prism 90a59cadf99a). Grading first would count the run twice on that retry.
        try:
            save_job_output(job_id, doc)
        except Exception:
            logger.warning("Job '%s': could not save settled output; script outcome %s kept for "
                           "a later pass", job_id, execution_id, exc_info=True)
            return RETRY
        # The fence is held (re-entrant), so a False below means the job vanished: SKIPPED.
        if already_counted:
            # Replace the unknown/interrupted stamp; never count the run a second time
            # (repeat, next_run_at and streaks were advanced by that first write).
            applied = correct_counted_run_outcome(job_id, success, error,
                                                  last_run_at=finished_iso)
        else:
            applied = mark_job_run(job_id, success, error)
        if not applied:
            discard(base)
            return SKIPPED
        # Consumed: remove the record before the fence is released.
        discard(base)
    logger.warning(
        "Job '%s': run %s outlived a scheduler restart and exited %d; recorded as %s from its "
        "own exit status (t_854cd586)", name, execution_id, rc, "ok" if success else "error")
    return APPLIED


def settle() -> int:
    """Apply every outcome record whose owner process is gone. Returns how many jobs were graded."""
    if not supported():
        return 0
    try:
        directory = _records_dir()
        names = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    from cron.executions import _owner_is_live, get_execution

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
        try:
            row = get_execution(str(meta["execution_id"]))
        except Exception:
            row = None
        if row is not None and row.get("status") in ("completed", "failed"):
            discard(base)  # the owner recorded this attempt itself
            continue
        if _owner_is_live(owner_pid, meta.get("owner_started_at")):
            continue  # the owner is still waiting on this script and will record it itself
        rc = _read_rc(base)
        if rc is None:
            alive = _script_alive(meta, base)
            if alive:
                continue  # orphan still running; a later pass reads its exit status
            if alive is None and (time.time() - float(meta.get("started_at") or 0)
                                  < _NO_SCRIPT_GRACE_SECONDS):
                continue  # the shim may not have written its pid yet
            # The shim may have published .rc and exited between the read above and the
            # liveness check (Prism d2efbca992a8): re-read before treating it as killed.
            rc = _read_rc(base)
        if rc is None:
            logger.info(
                "Job '%s': script run %s ended without an exit status (killed); left as restart "
                "recovery recorded it", meta.get("job_id"), meta.get("execution_id"))
            discard(base)
            continue
        try:
            result = _record_outcome(meta, base, rc)
        except Exception:
            logger.warning("Job '%s': applying script outcome record failed",
                           meta.get("job_id"), exc_info=True)
            continue
        if result == APPLIED:
            graded += 1
        # APPLIED/SKIPPED removed the record under the fence; RETRY keeps it.
    return graded
