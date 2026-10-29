"""A no_agent run that outlives a gateway restart is graded by its real outcome (t_854cd586).

Incident 2026-10-26: media-brain-bridge fired 02:45, the gateway restarted at 03:09, the script
(own session) kept running and finished cleanly at 03:14:55, but the process that would have
recorded it was gone. Restart recovery could only write ``unknown`` and crons.ace showed the job
Down until its next natural run.

These tests do the real thing: a separate "gateway" process runs ``run_one_job`` on a real
script, the test SIGKILLs that process mid-run (the script survives in its own session), and
this process plays the restarted gateway: startup recovery, then the periodic reap.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX sessions and /bin/sh shim")

REPO = Path(__file__).resolve().parents[2]

SCRIPT = """#!/usr/bin/env bash
touch "{started}"
while [ ! -f "{release}" ]; do sleep 0.05; done
echo "bridge done; ledger 3737"
exit {rc}
"""

GATEWAY = """
import sys
from cron.jobs import get_job
import cron.scheduler as sched
sched.run_one_job(get_job(sys.argv[1]))
print("RUN_ONE_JOB_RETURNED", flush=True)
"""


def _wait(pred, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


@pytest.fixture
def env(hermes_env):
    import cron.scheduler as sched

    sched.clear_shutdown()
    yield hermes_env
    sched.clear_shutdown()


def _make_job(home: Path, rc: int, **extra):
    from cron.jobs import create_job, get_job

    started, release = home / "started", home / "release"
    (home / "scripts" / "bridge.sh").write_text(
        SCRIPT.format(started=started, release=release, rc=rc), encoding="utf-8")
    job = create_job(prompt=None, schedule="45 2 * * *", name="media-brain-bridge",
                     script="bridge.sh", no_agent=True, deliver="local", **extra)
    return get_job(job["id"]), started, release


def _gateway_dies_mid_run(home: Path, job_id: str, started: Path) -> subprocess.Popen:
    """Run the job in a separate process, then SIGKILL that process while the script runs."""
    child_env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(REPO))
    gw = subprocess.Popen([sys.executable, "-c", GATEWAY, job_id], cwd=REPO, env=child_env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        _wait(started.exists, what="the script to start in the gateway process")
    except BaseException:
        gw.kill()
        out, err = gw.communicate()
        raise AssertionError(f"gateway output:\n{out}\n{err}")
    gw.send_signal(signal.SIGKILL)  # windows-footgun: ok — module is skipped off POSIX
    gw.wait(10)
    assert gw.returncode == -signal.SIGKILL  # windows-footgun: ok — module is skipped off POSIX
    return gw


def _restarted_gateway_recovers() -> int:
    from cron.scheduler_provider import InProcessCronScheduler

    return InProcessCronScheduler().recover_interrupted()


def _reap_tick():
    import cron.scheduler as sched

    sched._last_dead_owner_reap_at.clear()
    sched._maybe_reap_dead_owners()


def _records(home: Path):
    d = home / "cron" / "script-runs"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


@pytest.mark.parametrize("rc, status", [(0, "ok"), (3, "error")], ids=["finishes-ok", "fails"])
def test_run_that_outlives_restart_is_graded_by_its_exit_status(env, rc, status):
    from cron.executions import list_executions
    from cron.jobs import get_job

    job, started, release = _make_job(env, rc)
    before = get_job(job["id"])["last_run_at"]
    _gateway_dies_mid_run(env, job["id"], started)

    # Restart recovery while the orphaned script is still running: the attempt is unknown.
    assert _restarted_gateway_recovers() == 1
    mid = get_job(job["id"])
    assert mid["last_status"] == "unknown", mid
    assert [r["status"] for r in list_executions(job_id=job["id"])] == ["unknown"]
    assert any(n.endswith(".json") for n in _records(env)), "record must survive the dead owner"

    # The orphan finishes after the restart; the next reap tick grades it by its exit status.
    release.touch()
    rc_files = lambda: [n for n in _records(env) if n.endswith(".rc")]  # noqa: E731
    _wait(lambda: bool(rc_files()), what="the shim to publish the exit status")
    _reap_tick()

    final = get_job(job["id"])
    assert final["last_status"] == status, final
    assert final["last_run_at"] != before
    if rc == 0:
        assert final["last_error"] is None
        assert final["failure_streak"] == 0
    else:
        assert f"exited with code {rc}" in final["last_error"]
    assert _records(env) == [], "a settled record must be removed"
    out_dir = env / "cron" / "output" / job["id"]
    docs = [p.read_text(encoding="utf-8-sig") for p in out_dir.glob("*.md")]
    assert any("bridge done; ledger 3737" in d and "settled after restart" in d for d in docs)


def test_restart_recovery_settles_a_run_that_finished_while_the_gateway_was_down(env):
    """The 03:09 shape: the script finished before the new gateway's startup recovery ran."""
    from cron.jobs import get_job

    job, started, release = _make_job(env, 0)
    _gateway_dies_mid_run(env, job["id"], started)
    release.touch()
    _wait(lambda: any(n.endswith(".rc") for n in _records(env)), what="exit status")

    assert _restarted_gateway_recovers() == 1
    final = get_job(job["id"])
    assert final["last_status"] == "ok", final
    assert _records(env) == []


def test_normal_run_leaves_no_record_and_keeps_output(env):
    import cron.scheduler as sched
    from cron.jobs import get_job

    job, started, release = _make_job(env, 0)
    release.touch()
    assert sched.run_one_job(job) is True
    final = get_job(job["id"])
    assert final["last_status"] == "ok", final
    assert _records(env) == []
    docs = [p.read_text(encoding="utf-8-sig")
            for p in (env / "cron" / "output" / job["id"]).glob("*.md")]
    assert any("bridge done; ledger 3737" in d for d in docs), docs


def test_killed_orphan_stays_unknown(env):
    """The shim dies by signal after the owner is gone: no exit status, so the run stays unknown.
    The script kills its own parent (the shim) so no foreign pid is signalled from the test."""
    from cron.jobs import get_job

    job, started, release = _make_job(env, 0)
    (env / "scripts" / "bridge.sh").write_text(
        f'touch "{started}"\nwhile [ ! -f "{release}" ]; do sleep 0.05; done\n'
        'kill -KILL $PPID\nexit 0\n', encoding="utf-8")
    _gateway_dies_mid_run(env, job["id"], started)
    meta = next(json.loads((env / "cron" / "script-runs" / n).read_text(encoding="utf-8-sig"))
                for n in _records(env) if n.endswith(".json"))
    release.touch()
    from cron.executions import _owner_is_live

    _wait(lambda: not _owner_is_live(meta["script_pid"], meta.get("script_started_at")),
          what="shim death")
    assert not any(n.endswith(".rc") for n in _records(env))
    assert _restarted_gateway_recovers() == 1
    _reap_tick()
    final = get_job(job["id"])
    assert final["last_status"] == "unknown", final
    assert _records(env) == []


def test_signal_death_still_reported_as_negative_returncode(env):
    """The shim re-raises the script's signal so the shutdown re-queue still sees -15."""
    from cron import scheduler_script as ss

    (env / "scripts" / "self-term.sh").write_text("kill -TERM $$\nsleep 5\n", encoding="utf-8")
    ok, out = ss._run_job_script("self-term.sh", job_id="j1", execution_id="e1abc")
    assert not ok
    assert "exited with code -15" in out, out


def test_sigkill_death_propagates_through_the_shim(env):
    """Prism 9ee8fe887c81: SIGKILL takes no handler, so resetting it raised OSError and the shim
    exited 1. The shim must still die by SIGKILL (Popen -9) and keep .rc = 137."""
    from cron import script_outcome_records as rec

    base = rec.begin("j9", "e9abc")
    p = subprocess.run(rec.wrap_argv(base, ["/bin/sh", "-c", "kill -KILL $$"]),
                       capture_output=True, timeout=30)
    assert p.returncode == -signal.SIGKILL, (p.returncode, p.stderr)  # windows-footgun: ok — module is skipped off POSIX
    assert Path(str(base) + ".rc").read_text(encoding="utf-8").strip() == "137"


def test_rc_published_during_the_liveness_check_is_applied(env, monkeypatch):
    """Prism d2efbca992a8: the shim publishes .rc and exits between settle's first .rc read and
    its liveness check. settle must re-read .rc and grade the run, not discard the record."""
    from cron import script_outcome_records as rec
    from cron.jobs import get_job

    job, _started, _release = _make_job(env, 0)
    base = rec.begin(job["id"], "e7abc")
    meta_path = Path(str(base) + ".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    meta["owner_pid"] = 2 ** 22 - 7  # a pid that is not running
    meta["owner_started_at"] = 1
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    Path(str(base) + ".out").write_text("late finish\n", encoding="utf-8")

    def shim_finishes_then_dies(_meta, b):
        Path(str(b) + ".rc").write_text("0\n", encoding="utf-8")
        return False

    monkeypatch.setattr(rec, "_script_alive", shim_finishes_then_dies)
    assert rec.settle() == 1
    assert get_job(job["id"])["last_status"] == "ok"
    assert _records(env) == []


def test_explicit_exit_143_is_not_a_signal_death(env):
    """Prism 2b28c5552d47: a script that EXITS 143 was not killed. The shim must keep it a normal
    exit (Popen 143), or the shutdown-kill re-queue would re-fire a run whose side effects ran."""
    import cron.scheduler as sched
    from cron import scheduler_script as ss

    import threading

    started, release = env / "started-143", env / "release-143"
    (env / "scripts" / "exit-143.sh").write_text(
        f'touch "{started}"\nwhile [ ! -f "{release}" ]; do sleep 0.05; done\n'
        "echo side-effect\nexit 143\n", encoding="utf-8")

    def shutdown_mid_run():
        _wait(started.exists, what="the script to start")
        sched.signal_shutdown("test: scheduler shutdown while the script exits 143")
        release.touch()

    t = threading.Thread(target=shutdown_mid_run)
    t.start()
    ok, out = ss._run_job_script("exit-143.sh", job_id="j143", execution_id="e143abc")
    t.join()
    assert sched.is_shutting_down()
    assert not ok
    assert "exited with code 143" in out, out
    assert "Killed by gateway shutdown" not in out, out
    assert "j143" not in sched._restart_killed_job_ids
    assert (env / "cron" / "script-runs" / "e143abc.rc").read_text(encoding="utf-8").strip() == "143"


@pytest.mark.parametrize("rc", [0, 3], ids=["ok", "fails"])
def test_settled_outcome_counts_the_run_once(env, rc):
    """Prism 3b8d4e8ba014: recovery's reconcile already ran mark_job_run for this run; the settled
    outcome replaces that stamp and must not advance repeat/next_run_at/streak a second time."""
    from cron.jobs import get_job

    job, started, release = _make_job(env, rc, repeat=3)
    _gateway_dies_mid_run(env, job["id"], started)
    release.touch()
    _wait(lambda: any(n.endswith(".rc") for n in _records(env)), what="exit status")

    from cron.executions import recover_interrupted_executions
    from cron.scheduler_provider import InProcessCronScheduler, _settle_script_outcomes

    assert recover_interrupted_executions() == 1
    InProcessCronScheduler._reconcile_jobs_after_recovery()
    counted = get_job(job["id"])
    assert counted["last_status"] == "unknown"
    assert counted["repeat"]["completed"] == 1
    assert _settle_script_outcomes() == 1

    final = get_job(job["id"])
    assert final["last_status"] == ("ok" if rc == 0 else "error"), final
    assert final["repeat"]["completed"] == 1, final["repeat"]
    assert final["next_run_at"] == counted["next_run_at"]
    assert final["failure_streak"] == (0 if rc == 0 else 1), final
    assert final.get("enabled", True) and final.get("state") != "completed"


def test_record_outlives_the_runner_until_the_run_is_recorded(env):
    """Prism 111cd4f574ad: the runner returning is not the run being recorded. The record stays
    until finish_execution writes the attempt's terminal row."""
    from cron import scheduler_script as ss
    from cron import script_outcome_records as rec

    (env / "scripts" / "ok.sh").write_text("echo hi\n", encoding="utf-8")
    ok, out = ss._run_job_script("ok.sh", job_id="j1", execution_id="e2abc")
    assert ok and out.strip() == "hi"
    assert "e2abc.rc" in _records(env), _records(env)
    rec.discard_execution("e2abc")
    assert _records(env) == []


def test_shim_does_not_depend_on_the_job_path(env):
    """Prism ca1a3f91facb: a script run with a PATH that has no mv/cat still publishes its exit
    status and still delivers its output."""
    from cron import script_outcome_records as rec

    base = rec.begin("j1", "e3abc")
    argv = rec.wrap_argv(base, ["/bin/sh", "-c", "printf hello"])
    res = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                         env={"PATH": "/nonexistent"})
    assert res.returncode == 0
    assert res.stdout == "hello"
    assert (Path(str(base) + ".rc")).read_text(encoding="utf-8-sig").strip() == "0"


def test_backgrounded_helper_does_not_hold_the_runner_stderr(env):
    """Prism 986c4845a87b: a helper the script backgrounds with its output redirected must not
    inherit the runner's stderr pipe through the shim's fd 3."""
    from cron import scheduler_script as ss

    release = env / "helper-release"
    # The helper exits on its own once released (bounded at ~30 s), so nothing is signalled.
    (env / "scripts" / "wrap.sh").write_text(
        f'( n=0; while [ ! -f "{release}" ] && [ $n -lt 300 ]; do sleep 0.1; n=$((n+1)); done )'
        ' >/dev/null 2>&1 </dev/null &\necho started\n', encoding="utf-8")
    t0 = time.monotonic()
    try:
        ok, out = ss._run_job_script("wrap.sh", job_id="j1", execution_id="e4abc",
                                     timeout_seconds=15)
    finally:
        release.touch()
    assert ok, out
    assert out.strip() == "started"
    assert time.monotonic() - t0 < 10


def test_record_without_note_pid_waits_for_the_running_shim(env):
    """Prism 289e05dabb81: the owner died between Popen and note_pid. The shim's own pid file
    identifies the running script, so settle keeps the record past the grace period."""
    from cron import script_outcome_records as rec

    base = rec.begin("j1", "e5abc")
    meta_path = Path(str(base) + ".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    meta["owner_pid"] = 2 ** 22 - 7  # a pid that is not running
    meta["owner_started_at"] = 1
    meta["started_at"] = time.time() - 120  # past the no-identity grace period
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    release = env / "release5"
    shim = subprocess.Popen(rec.wrap_argv(base, ["/bin/sh", "-c", f'while [ ! -f "{release}" ]; do sleep 0.05; done']),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait(lambda: Path(str(base) + ".pid").exists(), what="shim pid")
        assert rec.settle() == 0
        assert "e5abc.json" in _records(env), "a live shim's record must be kept"
    finally:
        release.touch()
        shim.wait(10)
    assert "e5abc.rc" in _records(env)


def test_busy_fence_keeps_the_record_for_a_later_pass(env, monkeypatch):
    """Prism f26e557657af: a fence that cannot be taken is not a reason to drop the evidence."""
    import contextlib

    import cron.jobs as jobs
    from cron.jobs import get_job

    job, started, release = _make_job(env, 0)
    _gateway_dies_mid_run(env, job["id"], started)
    release.touch()
    _wait(lambda: any(n.endswith(".rc") for n in _records(env)), what="exit status")
    from cron.executions import recover_interrupted_executions
    from cron.scheduler_provider import _settle_script_outcomes

    assert recover_interrupted_executions() == 1

    @contextlib.contextmanager
    def busy(job_id, *, timeout=None):
        yield False

    real = jobs._fire_job_lock
    monkeypatch.setattr(jobs, "_fire_job_lock", busy)
    assert _settle_script_outcomes() == 0
    assert any(n.endswith(".rc") for n in _records(env)), "record must survive a busy fence"
    monkeypatch.setattr(jobs, "_fire_job_lock", real)
    assert _settle_script_outcomes() == 1
    assert get_job(job["id"])["last_status"] == "ok"
    assert _records(env) == []


def test_newer_attempt_seen_under_the_fence_is_not_overwritten(env, monkeypatch):
    """Prism 00449436b876 / 696260cb6b5c: the newest-attempt check runs inside the fence. A newer
    attempt that lands while settle waits for the fence wins."""
    import contextlib

    import cron.jobs as jobs
    from cron.executions import create_execution, recover_interrupted_executions
    from cron.jobs import get_job, mark_job_run
    from cron.scheduler_provider import _settle_script_outcomes

    job, started, release = _make_job(env, 3)
    _gateway_dies_mid_run(env, job["id"], started)
    release.touch()
    _wait(lambda: any(n.endswith(".rc") for n in _records(env)), what="exit status")
    assert recover_interrupted_executions() == 1

    real = jobs._fire_job_lock
    landed = []

    @contextlib.contextmanager
    def newer_run_lands_first(job_id, *, timeout=None):
        # E2 starts and records success just before settle gets the fence (once).
        if not landed:
            landed.append(True)
            create_execution(job_id, source="manual")
            mark_job_run(job_id, True)
        with real(job_id, timeout=timeout) as acquired:
            yield acquired

    monkeypatch.setattr(jobs, "_fire_job_lock", newer_run_lands_first)
    assert _settle_script_outcomes() == 0
    final = get_job(job["id"])
    assert final["last_status"] == "ok", final
    assert final["last_error"] is None


def test_concurrent_settlers_apply_the_record_once(env):
    """Prism e9387d975af3: settler B read the record (meta and .rc) before settler A applied and
    removed it. B must not re-apply its cached copy: no current-time last_run_at, no empty
    output doc."""
    from cron import script_outcome_records as rec
    from cron.executions import recover_interrupted_executions
    from cron.jobs import get_job
    from cron.scheduler_provider import _settle_script_outcomes

    job, started, release = _make_job(env, 0)
    _gateway_dies_mid_run(env, job["id"], started)
    release.touch()
    _wait(lambda: any(n.endswith(".rc") for n in _records(env)), what="exit status")
    assert recover_interrupted_executions() == 1

    # B's view, taken before A runs.
    name = next(n for n in _records(env) if n.endswith(".json"))
    base = env / "cron" / "script-runs" / name[: -len(".json")]
    meta_b = json.loads(Path(str(base) + ".json").read_text(encoding="utf-8-sig"))
    rc_b = rec._read_rc(base)
    assert rc_b == 0

    # A settles and consumes the record.
    assert _settle_script_outcomes() == 1
    after_a = get_job(job["id"])
    assert after_a["last_status"] == "ok"
    assert _records(env) == []
    out_dir = env / "cron" / "output" / job["id"]
    docs_a = sorted(p.name for p in out_dir.glob("*.md"))

    time.sleep(1.1)  # a re-apply would stamp a visibly later last_run_at / a new output file
    assert rec._record_outcome(meta_b, base, rc_b) == rec.SKIPPED
    after_b = get_job(job["id"])
    assert after_b["last_run_at"] == after_a["last_run_at"], (after_a, after_b)
    assert after_b["last_status"] == "ok"
    assert sorted(p.name for p in out_dir.glob("*.md")) == docs_a
    assert not any("(no stdout)" in p.read_text(encoding="utf-8-sig") for p in out_dir.glob("*.md"))


def test_failed_output_save_keeps_the_record_and_grades_nothing(env, monkeypatch):
    """Prism 90a59cadf99a: with the owner dead the record holds the only copy of the output. A
    save that fails keeps the record and leaves the run ungraded; the next pass saves and grades
    it once."""
    import cron.jobs as jobs
    from cron.executions import recover_interrupted_executions
    from cron.jobs import get_job
    from cron.scheduler_provider import _settle_script_outcomes

    job, started, release = _make_job(env, 0)
    _gateway_dies_mid_run(env, job["id"], started)
    release.touch()
    _wait(lambda: any(n.endswith(".rc") for n in _records(env)), what="exit status")
    assert recover_interrupted_executions() == 1
    before = get_job(job["id"])

    real = jobs.save_job_output

    def unwritable(job_id, output):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(jobs, "save_job_output", unwritable)
    assert _settle_script_outcomes() == 0
    assert any(n.endswith(".out") for n in _records(env)), "captured stdout must survive"
    assert any(n.endswith(".rc") for n in _records(env)), "record must survive a failed save"
    assert get_job(job["id"])["last_status"] == before["last_status"]

    monkeypatch.setattr(jobs, "save_job_output", real)
    assert _settle_script_outcomes() == 1
    assert get_job(job["id"])["last_status"] == "ok"
    assert _records(env) == []
    docs = list((env / "cron" / "output" / job["id"]).glob("*.md"))
    assert len(docs) == 1
    assert "bridge done; ledger 3737" in docs[0].read_text(encoding="utf-8-sig")


# --- t_b82b0a6d: Prism residue of #2371 -------------------------------------------------------

_DEAD_PID = 2 ** 22 - 7  # a pid that is not running


def _orphan_record(job_id: str, execution_id: str, *, rc=None, out=None, **meta_extra) -> Path:
    """A record whose owner is dead, optionally with a published exit status and stdout."""
    from cron import script_outcome_records as rec

    base = rec.begin(job_id, execution_id)
    meta_path = Path(str(base) + ".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    meta.update(owner_pid=_DEAD_PID, owner_started_at=1, **meta_extra)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    if out is not None:
        Path(str(base) + ".out").write_text(out, encoding="utf-8")
    if rc is not None:
        Path(str(base) + ".rc").write_text(f"{rc}\n", encoding="utf-8")
    return base


def _recovery_counts_it(job_id: str):
    """What reconcile writes for a run it could not observe."""
    from cron.jobs import mark_job_run

    time.sleep(0.01)
    mark_job_run(job_id, False, "Interrupted: the scheduler process exited", status="unknown")


def test_settled_stamp_uses_the_profile_timezone_and_is_not_recounted(env, monkeypatch):
    """Prism 2146c280fe76 / a73e1cb4acd6: profile timezone Asia/Tokyo on a UTC host. The settled
    last_run_at must carry the profile zone, and a later reconcile must not count the run again."""
    import hermes_time
    from cron.executions import recover_interrupted_executions
    from cron.jobs import get_job
    from cron.scheduler_provider import InProcessCronScheduler, _settle_script_outcomes

    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")
    hermes_time.reset_cache()
    try:
        job, started, release = _make_job(env, 0, repeat=3)
        _gateway_dies_mid_run(env, job["id"], started)
        release.touch()
        _wait(lambda: any(n.endswith(".rc") for n in _records(env)), what="exit status")
        assert recover_interrupted_executions() == 1
        InProcessCronScheduler._reconcile_jobs_after_recovery()
        assert _settle_script_outcomes() == 1
        settled = get_job(job["id"])
        assert settled["last_status"] == "ok"
        assert settled["last_run_at"].endswith("+09:00"), settled["last_run_at"]

        # The next restart reconciles again; the settled run is the job's newest and stays unknown
        # in the ledger. It must read as already recorded.
        assert InProcessCronScheduler._reconcile_jobs_after_recovery() == 0
        final = get_job(job["id"])
        assert final["last_status"] == "ok", final
        assert final["repeat"]["completed"] == settled["repeat"]["completed"]
    finally:
        hermes_time.reset_cache()


def test_reconcile_compares_stamps_as_instants():
    """Prism a73e1cb4acd6: 03:05 UTC is after 12:00 +09:00 (03:00 UTC) although it sorts before."""
    from cron.scheduler_provider import _stamp_not_before

    assert _stamp_not_before("2026-10-27T03:05:00+00:00", "2026-10-27T12:00:00+09:00")
    assert not _stamp_not_before("2026-10-27T02:55:00+00:00", "2026-10-27T12:00:00+09:00")
    assert not _stamp_not_before("", "2026-10-27T12:00:00+09:00")


def test_pruned_attempt_is_not_mistaken_for_a_newer_one(env):
    """Prism e4ebf4ae5d41: the orphan's unknown row was pruned; the ledger's latest row is an OLDER
    kept completion. The orphan's real outcome must still be applied."""
    from cron.executions import create_execution, finish_execution, get_execution
    from cron.jobs import get_job
    from cron import script_outcome_records as rec

    job, _s, _r = _make_job(env, 0)
    old = create_execution(job["id"], source="scheduled")
    assert finish_execution(old["id"], success=True) is not None
    time.sleep(0.01)
    _orphan_record(job["id"], "e10abc", rc=0, out="late finish\n")
    _recovery_counts_it(job["id"])
    assert get_execution("e10abc") is None  # pruned
    assert rec.settle() == 1
    final = get_job(job["id"])
    assert final["last_status"] == "ok", final
    assert _records(env) == []


def test_pruned_attempt_still_yields_to_a_genuinely_newer_one(env):
    """The other side of e4ebf4ae5d41: a row claimed after the orphan began IS newer."""
    from cron.executions import create_execution, finish_execution
    from cron.jobs import get_job, mark_job_run
    from cron import script_outcome_records as rec

    job, _s, _r = _make_job(env, 3)
    _orphan_record(job["id"], "e11abc", rc=3)
    time.sleep(0.01)
    newer = create_execution(job["id"], source="manual")
    mark_job_run(job["id"], True, None)
    finish_execution(newer["id"], success=True)
    assert rec.settle() == 0
    assert get_job(job["id"])["last_status"] == "ok"
    assert _records(env) == []


def test_settled_output_tail_keeps_redaction_context(env):
    """Prism 63cb77dd5537: a 16 KiB tail cut right after ``SERVICE_TOKEN=`` kept the value
    without its key, so it went unredacted into the output doc."""
    from cron import script_outcome_records as rec

    job, _s, _r = _make_job(env, 0)
    token = "q" * 32
    _orphan_record(job["id"], "e12abc", rc=0, out="SERVICE_TOKEN=" + token + "\n" + "." * 16351)
    assert rec.settle() == 1
    docs = [p.read_text(encoding="utf-8-sig")
            for p in (env / "cron" / "output" / job["id"]).glob("*.md")]
    assert len(docs) == 1
    assert token not in docs[0]


def test_outcome_the_owner_recorded_is_not_overwritten(env):
    """Prism f917ad4f14b8: the reap marked a live external worker's row unknown; the owner then
    finished and recorded delivery_failed, and finish_execution matched nothing, so the record
    stayed. settle must not replace the owner's observed outcome."""
    from cron.executions import create_execution
    from cron.jobs import get_job, mark_job_run
    from cron import script_outcome_records as rec

    job, _s, _r = _make_job(env, 0)
    row = create_execution(job["id"], source="scheduled")
    _orphan_record(job["id"], row["id"], rc=0)
    time.sleep(0.01)
    mark_job_run(job["id"], True, None, delivery_error="discord 503")
    before = get_job(job["id"])
    assert before["last_status"] == "delivery_failed"
    out_dir = env / "cron" / "output" / job["id"]
    docs_before = sorted(out_dir.glob("*.md")) if out_dir.exists() else []
    from cron.executions import _transaction
    with _transaction() as conn:
        conn.execute("UPDATE executions SET status='unknown' WHERE id=?", (row["id"],))

    assert rec.settle() == 0
    final = get_job(job["id"])
    assert final["last_status"] == "delivery_failed", final
    assert final["last_run_at"] == before["last_run_at"]
    assert (sorted(out_dir.glob("*.md")) if out_dir.exists() else []) == docs_before
    assert _records(env) == []


def test_live_shim_with_linux_tick_fingerprint_is_not_called_dead(env, monkeypatch):
    """Prism 27d6882547f7: Linux start fingerprints are clock ticks since boot (~1.296e9 after
    150 days at USER_HZ=100), never epoch seconds. A live shim must not be judged by wall time."""
    import cron.executions as ex
    from cron import script_outcome_records as rec

    shim = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
    try:
        base = _orphan_record("j13", "e13abc", started_at=1790000000.0)
        meta_path = Path(str(base) + ".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        meta["owner_started_at"] = 1295990000  # ticks: the owner started before the shim
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        Path(str(base) + ".pid").write_text(f"{shim.pid}\n", encoding="utf-8")
        real = ex._process_start_time
        monkeypatch.setattr(ex, "_process_start_time",
                            lambda pid: 1296000000 if pid == shim.pid else real(pid))
        assert rec.settle() == 0
        assert "e13abc.json" in _records(env), "a live shim's record must be kept"
    finally:
        shim.kill()
        shim.wait(10)


def test_live_shim_noted_without_start_time_is_not_called_dead(env):
    """Prism 5cd3861095c5 (the window d2efbca992a8 left): note_pid recorded the shim pid but no
    start time. ``_owner_is_live(pid, None)`` means "is it this process", so a running shim was
    judged dead and its record discarded before its .rc could land."""
    from cron import script_outcome_records as rec

    release = env / "release14"
    base = _orphan_record("j14", "e14abc", started_at=time.time() - 120)
    shim = subprocess.Popen(rec.wrap_argv(base, ["/bin/sh", "-c",
                            f'while [ ! -f "{release}" ]; do sleep 0.05; done']),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        meta_path = Path(str(base) + ".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        meta.update(script_pid=shim.pid, script_started_at=None)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        assert rec.settle() == 0
        assert "e14abc.json" in _records(env), "a live shim's record must be kept"
    finally:
        release.touch()
        shim.wait(10)
    assert "e14abc.rc" in _records(env)
