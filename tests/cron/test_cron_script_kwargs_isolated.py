"""Per-kwarg isolation for the cron script-timeout call site (t_db03da56, child of t_96049446).

Call site: ``cron.scheduler._job_script_kwargs`` derives ``timeout_seconds`` / ``job_name`` /
``job_id`` from the fork rule ``scheduler_ext.resolve_job_script_timeout(job, global)``, and
``cron.scheduler_script._run_job_script_with_claim_heartbeat`` splats them into
``_run_job_script`` on the ``script`` path (``run_one_job`` -> ``run_job`` -> no_agent / pre-run).

The older run_one_job timeout tests patch the GLOBAL timeout and ``test_cron_workdir.py``
swallows ``**kwargs``, so a parity merge that drops ``**_job_script_kwargs(job)`` stayed green.
Each kwarg here gets two tests that go red on exactly its own regression:

1. ``test_wiring_<kwarg>``: drive the real ``run_one_job`` and capture the kwarg at the
   ``_run_job_script`` boundary; its VALUE equals what the fork rule derives.
2. ``test_render_<kwarg>``: run a real script through ``run_job`` / ``run_one_job``; the
   visible effect follows the kwarg, and dropping it from ``_job_script_kwargs`` (the
   boundary) regresses the effect to the upstream shape.

Monitor path: tests/cron/test_cron_script_job_timeout.py::test_monitor_script_passes_job_ceiling.
No test reads source text.
"""

from __future__ import annotations

import threading
import time

import pytest

from cron.fork_ext import scheduler_ext

GLOBAL = 7200  # operator cap (cron.script_timeout_seconds); must differ from the 3600 default
_WHERE = "call site = cron.scheduler._job_script_kwargs -> _run_job_script (script path)"


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


@pytest.fixture
def sched(hermes_env, monkeypatch):
    import cron.scheduler as sched

    sched.clear_shutdown()
    monkeypatch.setattr(sched, "_SCRIPT_TIMEOUT", GLOBAL)
    yield sched
    sched.clear_shutdown()
    with sched._script_procs_lock:
        sched._restart_killed_job_ids.clear()


def _job(home, script_body, *, name="nightly-probe", schedule="every 30m", **fields):
    from cron.jobs import create_job, get_job, update_job

    (home / "scripts" / "probe.sh").write_text(script_body, encoding="utf-8")
    job = create_job(prompt=None, schedule=schedule, name=name, script="probe.sh",
                     no_agent=True, deliver="local")
    if fields:
        update_job(job["id"], fields)
    return get_job(job["id"])


def _capture(monkeypatch):
    """Replace the runner the heartbeat wrapper calls; record the kwargs it receives."""
    import cron.scheduler_script as sched_script

    calls = []

    def fake(script_path, workdir=None, cancel_event=None, interpreter=None, **kwargs):
        calls.append(kwargs)
        return True, ""

    monkeypatch.setattr(sched_script, "_run_job_script", fake)
    return calls


def _drop(monkeypatch, sched, key):
    """The fork input dropped at the boundary: the upstream shape for ``key``."""
    real = sched._job_script_kwargs

    def without(job):
        kwargs = dict(real(job))
        kwargs.pop(key, None)
        return kwargs

    monkeypatch.setattr(sched, "_job_script_kwargs", without)


def _one_call(sched, job, calls):
    sched.run_one_job(job)
    assert len(calls) == 1, f"{_WHERE}: runner called {len(calls)}x"
    return calls[0]


# ---------------------------------------------------------------------------
# timeout_seconds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, schedule, expected, source",
    [({"timeout_s": 45}, "every 30m", 45, "job"), ({}, "every 15m", 900, "interval")],
    ids=["explicit-timeout_s", "interval-ceiling"],
)
def test_wiring_timeout_seconds(hermes_env, sched, monkeypatch, fields, schedule, expected, source):
    job = _job(hermes_env, "echo hi\n", schedule=schedule, **fields)
    assert scheduler_ext.resolve_job_script_timeout(job, GLOBAL) == (expected, source)
    kwargs = _one_call(sched, job, _capture(monkeypatch))
    assert kwargs.get("timeout_seconds") == expected, (
        f"{_WHERE}: timeout_seconds={kwargs.get('timeout_seconds')!r}, fork rule gives "
        f"{expected} ({source}); global is {GLOBAL}")


@pytest.mark.platforms("posix")
def test_render_timeout_seconds(hermes_env, sched, monkeypatch):
    # timeout_s: 1 under a 7200 s global: the run is killed at the job's ceiling.
    job = _job(hermes_env, "sleep 3\necho finished-late\n", timeout_s=1)
    ok, _doc, _final, error = sched.run_job(job)
    assert ok is False and "timed out after 1s" in (error or ""), (ok, error)

    # Dropped at the call site: the global ceiling applies and the script runs to completion.
    _drop(monkeypatch, sched, "timeout_seconds")
    ok, _doc, final, error = sched.run_job(job)
    assert ok is True and "finished-late" in (final or ""), (
        f"{_WHERE}: with timeout_seconds dropped the global {GLOBAL}s should apply", ok, error)


# ---------------------------------------------------------------------------
# job_name
# ---------------------------------------------------------------------------


def test_wiring_job_name(hermes_env, sched, monkeypatch):
    job = _job(hermes_env, "echo hi\n", name="nightly-probe")
    kwargs = _one_call(sched, job, _capture(monkeypatch))
    assert kwargs.get("job_name") == "nightly-probe", f"{_WHERE}: {kwargs!r}"


def _timeout_phase_line(sched, job, caplog):
    caplog.clear()
    ok, _doc, _final, error = sched.run_job(job)
    assert ok is False and "timed out" in (error or ""), (ok, error)
    lines = [r.getMessage() for r in caplog.records if "PHASE=cron_script_timeout" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


@pytest.mark.platforms("posix")
def test_render_job_name(hermes_env, sched, monkeypatch, caplog):
    # Global ceiling 1 s, so the timeout fires whatever timeout_seconds carries.
    monkeypatch.setattr(sched, "_SCRIPT_TIMEOUT", 1)
    caplog.set_level("WARNING")
    job = _job(hermes_env, "sleep 3\n", name="nightly-probe")
    assert "job=nightly-probe " in _timeout_phase_line(sched, job, caplog)

    # Dropped at the call site: the timeout line names the script file, not the job.
    _drop(monkeypatch, sched, "job_name")
    line = _timeout_phase_line(sched, job, caplog)
    assert "job=probe.sh " in line and "nightly-probe" not in line, line


# ---------------------------------------------------------------------------
# job_id
# ---------------------------------------------------------------------------


def test_wiring_job_id(hermes_env, sched, monkeypatch):
    job = _job(hermes_env, "echo hi\n")
    kwargs = _one_call(sched, job, _capture(monkeypatch))
    assert kwargs.get("job_id") == job["id"], f"{_WHERE}: {kwargs!r}"


_LONG = 'touch "$(dirname "$0")/started"\nsleep 30\necho never\n'


def _drain_kill(sched, home, job):
    """run_one_job in a thread; once the script is live, do what the gateway drain does."""
    started = home / "scripts" / "started"
    started.unlink(missing_ok=True)
    t = threading.Thread(target=sched.run_one_job, args=(job,), daemon=True)
    t.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with sched._script_procs_lock:
            if sched._active_script_procs and started.exists():
                break
        time.sleep(0.05)
    else:
        pytest.fail("script never started")
    sched.signal_shutdown("gateway shutdown drain")
    assert sched.terminate_running_scripts("gateway drain timeout") == 1
    t.join(20)
    assert not t.is_alive()
    sched.clear_shutdown()


@pytest.mark.platforms("posix")
def test_render_job_id(hermes_env, sched, monkeypatch):
    from cron.jobs import RESTART_REQUEUE_KEY, get_job

    # A drain-killed script is flagged by job_id, so run_one_job re-queues one fire.
    job = _job(hermes_env, _LONG)
    _drain_kill(sched, hermes_env, job)
    killed = get_job(job["id"])
    assert "eligible for one re-fire" in (killed["last_error"] or ""), killed["last_error"]
    assert killed.get(RESTART_REQUEUE_KEY), "restart re-queue marker not persisted"

    # Dropped at the call site: the kill reads as a plain failure and the fire is lost.
    _drop(monkeypatch, sched, "job_id")
    job2 = _job(hermes_env, _LONG, name="nightly-probe-2")
    _drain_kill(sched, hermes_env, job2)
    killed2 = get_job(job2["id"])
    assert "exited with code -15" in (killed2["last_error"] or ""), killed2["last_error"]
    assert "eligible for one re-fire" not in (killed2["last_error"] or "")
    assert not killed2.get(RESTART_REQUEUE_KEY), f"{_WHERE}: re-queued without job_id"
