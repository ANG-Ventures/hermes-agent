"""A cron job's ``env`` field reaches its script child (t_dbfaea4d).

``srv-ci-workspace-reap`` sets ``env: {SRV_CI_WORKSPACE_REAP_LIVE: "1"}`` and still ran dry: the
script runner built the child env from the sanitizer + interpreter overlay and never read
``job["env"]``. These drive the real ``run_job`` on a real no_agent script.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sched(hermes_env):
    import cron.scheduler as sched

    sched.clear_shutdown()
    yield sched
    sched.clear_shutdown()


def _job(home, body, **fields):
    from cron.jobs import create_job, get_job, update_job

    (home / "scripts" / "envprobe.sh").write_text(body, encoding="utf-8")
    job = create_job(prompt=None, schedule="every 30m", name="env-probe", script="envprobe.sh",
                     no_agent=True, deliver="local")
    if fields:
        update_job(job["id"], fields)
    return get_job(job["id"])


@pytest.mark.platforms("posix")
def test_job_env_reaches_no_agent_script(hermes_env, sched):
    job = _job(hermes_env, 'echo "x=${X-ABSENT}"\n', env={"X": "1"})
    ok, _doc, final, error = sched.run_job(job)
    assert ok is True, error
    assert "x=1" in (final or ""), final


@pytest.mark.platforms("posix")
def test_job_env_reaches_monitor_and_cron_run_paths(hermes_env, sched):
    # `cron run --wait` is run_job_now -> run_job, the same boundary; the monitor path calls
    # _run_job_script with _job_script_kwargs(job) too.
    from cron.monitor import _run_monitor_source

    (hermes_env / "scripts" / "mon.sh").write_text('echo "x=${X-ABSENT}"\n', encoding="utf-8")
    ok, out = _run_monitor_source({"id": "m1", "name": "m1", "monitor_script": "mon.sh",
                                   "env": {"X": "2"}})
    assert ok is True and "x=2" in out, out


@pytest.mark.parametrize("key", ["HERMES_CRON_JOB_ID", "_HERMES_CRON_SCRIPT_RUN"])
def test_colliding_key_is_a_job_error(hermes_env, sched, key):
    from cron.scheduler_script import _run_job_script

    (hermes_env / "scripts" / "envprobe.sh").write_text("echo ran\n", encoding="utf-8")
    ok, out = _run_job_script("envprobe.sh", job_id="j1", job_env={key: "x"})
    assert ok is False and key in out and "ran" not in out, out


def test_reserved_keys_track_the_runner_constants():
    from cron.job_env import RESERVED_JOB_ENV_KEYS
    from cron.scheduler_script import SCRIPT_RUN_TOKEN_ENV
    from hermes_cli.process_bootstrap import GATEWAY_MARKER_ENV

    assert {SCRIPT_RUN_TOKEN_ENV, GATEWAY_MARKER_ENV} <= RESERVED_JOB_ENV_KEYS


@pytest.mark.parametrize("env", [{"X": 1}, {"X": None}, ["X=1"], {"": "1"}, {"A=B": "1"}])
def test_bad_env_shape_is_a_job_error(hermes_env, sched, env):
    from cron.scheduler_script import _run_job_script

    (hermes_env / "scripts" / "envprobe.sh").write_text("echo ran\n", encoding="utf-8")
    ok, out = _run_job_script("envprobe.sh", job_id="j1", job_env=env)
    assert ok is False and "env" in out and "ran" not in out, out


def test_update_refuses_env_on_a_job_with_no_script(hermes_env):
    from cron.jobs import create_job, get_job, update_job

    job = create_job(prompt="say hi", schedule="every 30m", name="agent-only", deliver="local")
    with pytest.raises(ValueError, match="env"):
        update_job(job["id"], {"env": {"X": "1"}})
    assert "env" not in get_job(job["id"])


def test_update_refuses_colliding_env_key(hermes_env):
    _job_home = hermes_env
    (_job_home / "scripts" / "envprobe.sh").write_text("echo ran\n", encoding="utf-8")
    from cron.jobs import create_job, update_job

    job = create_job(prompt=None, schedule="every 30m", name="env-probe", script="envprobe.sh",
                     no_agent=True, deliver="local")
    with pytest.raises(ValueError, match="HERMES_CRON_JOB_ID"):
        update_job(job["id"], {"env": {"HERMES_CRON_JOB_ID": "x"}})
