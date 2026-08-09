"""An INTERRUPTED cron run is an UNKNOWN outcome, not an observed failure.

Regression guard for the class where a scheduler restart loses a run's outcome
and the job is recorded ``last_status="error"``. The script may well have
completed (a ``no_agent`` script outlives the gateway), so asserting "error"
manufactures a failure nobody observed — monitors then page for a healthy job.

Live incident (2026-08-08): two gateway restarts produced exactly one such
reconciliation out of 1,001 executions; ``self-heal-delegations`` paged
"Cron broken ... last_status=error" and the very next tick completed fine.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def jobs_mod(tmp_path, monkeypatch):
    """Load cron.jobs against a temp HERMES_HOME so no real jobs.json is touched."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "cron").mkdir(parents=True, exist_ok=True)

    import cron.jobs as jobs
    importlib.reload(jobs)
    return jobs


def _seed_job(jobs_mod, tmp_path, job_id="j_test", **extra):
    job = {
        "id": job_id,
        "name": "test-job",
        "schedule": {"kind": "interval", "minutes": 5},
        "enabled": True,
        "prompt": "noop",
        "last_status": "ok",
        "last_run_at": "2026-01-01T00:00:00-00:00",
        **extra,
    }
    path = Path(jobs_mod.get_jobs_file()) if hasattr(jobs_mod, "get_jobs_file") else tmp_path / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([job]), encoding="utf-8")
    return job


def _read_back(jobs_mod, job_id="j_test"):
    for j in jobs_mod.load_jobs():
        if j["id"] == job_id:
            return j
    raise AssertionError(f"job {job_id} vanished")


# --------------------------------------------------------------------------
# The fix
# --------------------------------------------------------------------------

def test_interrupted_run_records_unknown_not_error(jobs_mod, tmp_path):
    """A lost-outcome reconciliation must NOT claim the job failed."""
    _seed_job(jobs_mod, tmp_path)

    jobs_mod.mark_job_run(
        "j_test", False, "Interrupted: the scheduler process exited ...",
        status="unknown",
    )

    job = _read_back(jobs_mod)
    assert job["last_status"] == "unknown", (
        "an undetermined outcome must be recorded as 'unknown' — recording "
        "'error' asserts a failure nobody observed and pages for a healthy job"
    )
    assert job["last_status"] != "error"


def test_unknown_status_still_carries_the_explanation(jobs_mod, tmp_path):
    """The operator still needs to know WHY the outcome is unknown."""
    _seed_job(jobs_mod, tmp_path)
    jobs_mod.mark_job_run(
        "j_test", False, "Interrupted: the scheduler process exited ...",
        status="unknown",
    )
    assert "Interrupted" in (_read_back(jobs_mod)["last_error"] or "")


# --------------------------------------------------------------------------
# NEGATIVE CONTROLS — a real failure must STILL be an error
# --------------------------------------------------------------------------

def test_observed_failure_is_still_error(jobs_mod, tmp_path):
    """The common path is unchanged: an observed failure stays 'error'.

    This is the control that stops the fix from muting real failures.
    """
    _seed_job(jobs_mod, tmp_path)
    jobs_mod.mark_job_run("j_test", False, "Script exited with code 1")

    job = _read_back(jobs_mod)
    assert job["last_status"] == "error"
    assert job["last_error"] == "Script exited with code 1"


def test_success_is_still_ok(jobs_mod, tmp_path):
    _seed_job(jobs_mod, tmp_path, last_status="error", last_error="boom")
    jobs_mod.mark_job_run("j_test", True)

    job = _read_back(jobs_mod)
    assert job["last_status"] == "ok"
    assert job["last_error"] is None


def test_status_override_does_not_leak_into_normal_calls(jobs_mod, tmp_path):
    """Existing call sites pass no `status=`; they must behave exactly as before."""
    _seed_job(jobs_mod, tmp_path)
    jobs_mod.mark_job_run("j_test", False, "real failure")
    assert _read_back(jobs_mod)["last_status"] == "error"

    jobs_mod.mark_job_run("j_test", True)
    assert _read_back(jobs_mod)["last_status"] == "ok"


# --------------------------------------------------------------------------
# The reconciler passes the honest status (guards the CALL SITE, not just the API)
# --------------------------------------------------------------------------

def test_reconciler_call_site_passes_unknown():
    """`_reconcile_jobs_after_recovery` must use status='unknown'.

    Without this, the API supports honesty while the only caller that needs it
    keeps writing 'error' — the exact fake-fix shape.
    """
    src = (REPO_ROOT / "cron" / "scheduler_provider.py").read_text(encoding="utf-8")
    idx = src.find("_reconcile_jobs_after_recovery")
    assert idx != -1, "reconciler not found — did it move?"
    body = src[idx: idx + 3000]
    assert "mark_job_run(" in body, "reconciler no longer calls mark_job_run"
    assert 'status="unknown"' in body, (
        "the recovery reconciler must record status='unknown'; recording the "
        "default 'error' is the bug this guards"
    )
