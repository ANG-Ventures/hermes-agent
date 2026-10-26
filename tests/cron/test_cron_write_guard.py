"""Write-time cron guard (t_0a8548cf).

``<home>/scripts/cron-write-guard``, when present and executable, grades every candidate row before
``create_job`` / ``update_job`` save it: rc 0 saves, rc 1 refuses with the guard's stderr, a crash or a
timeout refuses as "cron write guard unavailable". Absent = unchanged behavior.
"""

from __future__ import annotations

import json
import stat

import pytest

# The guard lives at <tmp>/.hermes/scripts/cron-write-guard and the update op is argv[1], which the
# conftest live-system guard reads as `hermes update`. Every subprocess here is a tmp_path shell stub.
pytestmark = pytest.mark.live_system_guard_bypass


def _guard(home, body: str):
    path = home / "scripts" / "cron-write-guard"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _stored(home):
    data = json.loads((home / "cron" / "jobs.json").read_text())
    return data.get("jobs", data) if isinstance(data, dict) else data


def test_guard_absent_create_and_update_unchanged(hermes_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True,
                     deliver="discord:1480528231286181948")
    assert update_job(job["id"], {"deliver": "local"})["deliver"] == "local"


def test_guard_not_executable_is_ignored(hermes_env):
    from cron.jobs import create_job

    path = hermes_env / "scripts" / "cron-write-guard"
    path.write_text("#!/bin/sh\nexit 1\n")
    path.chmod(0o644)
    create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True)


def test_guard_rc1_refuses_create_with_stderr(hermes_env):
    from cron.jobs import create_job

    _guard(hermes_env, 'echo "double sender: use --deliver local" >&2; exit 1')
    with pytest.raises(ValueError, match="double sender: use --deliver local"):
        create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True,
                   deliver="discord:1480528231286181948")
    assert not (hermes_env / "cron" / "jobs.json").exists() or _stored(hermes_env) == []


def test_guard_rc1_refuses_update_with_stderr_and_store_untouched(hermes_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True, deliver="local")
    _guard(hermes_env, 'echo "refused by lint" >&2; exit 1')
    with pytest.raises(ValueError, match="refused by lint"):
        update_job(job["id"], {"deliver": "discord:1480528231286181948"})
    assert [j["deliver"] for j in _stored(hermes_env)] == ["local"]


def test_guard_receives_candidate_row_and_op(hermes_env):
    from cron.jobs import create_job, update_job

    log = hermes_env / "guard.log"
    _guard(hermes_env, f'echo "$1" >> {log}; cat >> {log}; echo >> {log}; exit 0')
    job = create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True, deliver="local")
    update_job(job["id"], {"deliver": "discord:1"})
    lines = [ln for ln in log.read_text().splitlines() if ln]
    assert lines[0] == "create" and json.loads(lines[1])["id"] == job["id"]
    assert lines[2] == "update" and json.loads(lines[3])["deliver"] == "discord:1"
    assert _stored(hermes_env)[0]["deliver"] == "discord:1"


def test_guard_skipped_for_bookkeeping_update_and_disabled_rows(hermes_env):
    from cron.jobs import create_job, pause_job, update_job

    job = create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True, deliver="local")
    _guard(hermes_env, "exit 1")
    update_job(job["id"], {"last_delivery_error": "boom"})   # scheduler bookkeeping, not lint-relevant
    pause_job(job["id"], reason="test")                      # the row leaves the enabled set
    create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True, paused=True)


def test_guard_crash_refuses_as_unavailable(hermes_env):
    from cron.jobs import create_job

    _guard(hermes_env, "exit 3")
    with pytest.raises(ValueError, match="cron write guard unavailable"):
        create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True)


def test_guard_timeout_refuses_as_unavailable(hermes_env, monkeypatch):
    import cron.jobs

    monkeypatch.setattr(cron.jobs, "CRON_WRITE_GUARD_TIMEOUT_S", 0.5)
    _guard(hermes_env, "sleep 5")
    with pytest.raises(ValueError, match="cron write guard unavailable.*did not answer"):
        cron.jobs.create_job(prompt=None, schedule="every 5m", script="x.sh", no_agent=True)
