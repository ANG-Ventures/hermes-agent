"""t_04822736: profile scripts may symlink into the fleet-shared scripts dir,
and a no_agent job stuck on one identical error pages once, not per tick.

Regression shape: profiles/aegis/scripts/agent-browser-gc.py was a symlink to
../../../scripts/agent-browser-gc.py. The fire-time guard resolved it outside
<profile>/scripts and refused it every hour for 19 h; each tick's alert was a
truncated one-liner that never named the cause.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Symlinks require elevated privileges on Windows"
)


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """A fleet root with a named profile; HERMES_HOME = the profile."""
    root = tmp_path / ".hermes"
    (root / "scripts").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n")
    home = root / "profiles" / "aegis"
    (home / "scripts").mkdir(parents=True)
    (home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return root, home


def _shared_script(root: Path, name: str, body: str) -> Path:
    target = root / "scripts" / name
    target.write_text(body)
    return target


# ---------------------------------------------------------------------------
# AC1: fire-time guard
# ---------------------------------------------------------------------------


def test_symlink_into_shared_scripts_dir_is_admitted(profile_env):
    root, home = profile_env
    from cron.scheduler import _run_job_script

    _shared_script(root, "agent-browser-gc.py", 'print("gc ok")\n')
    # Exactly the production link: relative, three levels up.
    (home / "scripts" / "agent-browser-gc.py").symlink_to(
        Path("..") / ".." / ".." / "scripts" / "agent-browser-gc.py"
    )

    ok, output = _run_job_script("agent-browser-gc.py")
    assert ok is True, output
    assert "gc ok" in output


def test_symlink_escaping_both_dirs_still_blocked(profile_env, tmp_path):
    _root, home = profile_env
    from cron.scheduler import _run_job_script

    evil = tmp_path / "evil.py"
    evil.write_text('print("escaped")\n')
    (home / "scripts" / "sneaky.py").symlink_to(evil)

    ok, output = _run_job_script("sneaky.py")
    assert ok is False
    assert "outside the scripts directory" in output


def test_traversal_into_shared_dir_without_symlink_still_blocked(profile_env):
    root, _home = profile_env
    from cron.scheduler import _run_job_script

    _shared_script(root, "x.py", 'print("x")\n')
    ok, output = _run_job_script("../../../scripts/x.py")
    assert ok is False
    assert "outside the scripts directory" in output


def test_absolute_path_into_shared_dir_still_blocked(profile_env):
    root, _home = profile_env
    from cron.scheduler import _run_job_script

    target = _shared_script(root, "x.py", 'print("x")\n')
    ok, output = _run_job_script(str(target))
    assert ok is False
    assert "outside the scripts directory" in output


def test_non_profile_home_gets_no_shared_escape(tmp_path, monkeypatch):
    """A home that is not <root>/profiles/<name> has no shared dir to admit."""
    home = tmp_path / "custom"
    (home / "scripts").mkdir(parents=True)
    other = tmp_path / "scripts"
    other.mkdir()
    (other / "x.py").write_text('print("x")\n')
    (home / "scripts" / "x.py").symlink_to(other / "x.py")
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    ok, output = cron.scheduler._run_job_script("x.py")
    assert ok is False
    assert "outside the scripts directory" in output


def test_create_time_validation_matches_fire_time_guard(profile_env, tmp_path):
    root, home = profile_env
    from tools.cronjob_tools import _validate_cron_script_path

    _shared_script(root, "shared.py", 'print("s")\n')
    (home / "scripts" / "shared.py").symlink_to(root / "scripts" / "shared.py")
    evil = tmp_path / "evil.py"
    evil.write_text("")
    (home / "scripts" / "sneaky.py").symlink_to(evil)

    assert _validate_cron_script_path("shared.py") is None
    assert "escapes" in (_validate_cron_script_path("sneaky.py") or "")
    assert "escapes" in (_validate_cron_script_path("../../../scripts/shared.py") or "")


# ---------------------------------------------------------------------------
# AC2: a no_agent job stuck on one identical error pages ONCE
# ---------------------------------------------------------------------------


def _wire_delivery(monkeypatch):
    from unittest.mock import MagicMock

    from gateway.config import Platform
    from cron import scheduler as sched

    cfg = MagicMock()
    cfg.get_connected_platforms.return_value = [Platform.TELEGRAM]
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: cfg)
    monkeypatch.setattr(sched, "heartbeat_fire_claim", lambda *_a, **_kw: True)
    delivered = []
    monkeypatch.setattr(
        sched,
        "_deliver_result",
        lambda _job, content, **_kw: delivered.append(content),
    )
    return delivered


def _tick(job_id):
    from cron.jobs import get_job
    from cron.scheduler import run_one_job

    assert run_one_job(get_job(job_id)) is True
    return get_job(job_id)


def test_identical_error_pages_once_then_again_on_state_change(profile_env, monkeypatch):
    _root, home = profile_env
    from cron.jobs import create_job

    script = home / "scripts" / "broken.py"
    script.write_text('import sys; print("disk full on /data"); sys.exit(1)\n')
    job = create_job(
        prompt=None,
        schedule="every 60m",
        script="broken.py",
        no_agent=True,
        deliver="telegram",
        name="broken-watchdog",
    )
    delivered = _wire_delivery(monkeypatch)

    streaks = []
    for _ in range(6):
        streaks.append(_tick(job["id"])["error_repeat_streak"])
    assert streaks == [1, 2, 3, 4, 5, 6]
    # Ticks 1-2 alert as before, tick 3 is the one stuck page, 4-6 are silent.
    assert len(delivered) == 3, delivered
    page = delivered[2]
    assert "stuck" in page
    assert "disk full on /data" in page  # the cause, untruncated
    assert "Fix:" in page
    assert "same error 3 runs in a row" in page

    # State change: a DIFFERENT error alerts again immediately.
    script.write_text('import sys; print("permission denied"); sys.exit(1)\n')
    assert _tick(job["id"])["error_repeat_streak"] == 1
    assert len(delivered) == 4
    assert "stuck" not in delivered[3]

    # Recovery resets the streak; the next failure starts a fresh count.
    script.write_text('print("")\n')
    assert _tick(job["id"])["error_repeat_streak"] == 0
    script.write_text('import sys; print("permission denied"); sys.exit(1)\n')
    assert _tick(job["id"])["error_repeat_streak"] == 1


def test_shared_symlink_escape_page_names_the_cause_and_fix(profile_env, monkeypatch, tmp_path):
    _root, home = profile_env
    from cron.jobs import create_job

    evil = tmp_path / "elsewhere.py"
    evil.write_text('print("x")\n')
    (home / "scripts" / "gc.py").symlink_to(evil)
    job = create_job(
        prompt=None,
        schedule="every 60m",
        script="gc.py",
        no_agent=True,
        deliver="telegram",
        name="agent-browser-gc",
    )
    delivered = _wire_delivery(monkeypatch)
    for _ in range(4):
        _tick(job["id"])

    assert len(delivered) == 3
    page = delivered[2]
    assert "resolves outside the scripts directory" in page
    assert "shared" in page and "scripts/" in page


def test_agent_jobs_are_not_gated(profile_env):
    from cron.scheduler import _repeated_script_error_page

    job = {"id": "a", "last_status": "error", "last_error": "e", "error_repeat_streak": 5}
    assert _repeated_script_error_page(job, "e") is None
    job["no_agent"] = True
    assert _repeated_script_error_page(job, "e") == ""
    assert _repeated_script_error_page(job, "different") is None


def test_jobs_json_roundtrip_carries_streak(profile_env):
    _root, home = profile_env
    from cron.jobs import create_job, mark_job_run

    job = create_job(prompt=None, schedule="every 60m", script="x.py", no_agent=True)
    mark_job_run(job["id"], False, "boom")
    mark_job_run(job["id"], False, "boom")
    raw = json.loads((home / "cron" / "jobs.json").read_text())
    jobs = raw["jobs"] if isinstance(raw, dict) else raw
    assert [j for j in jobs if j["id"] == job["id"]][0]["error_repeat_streak"] == 2
