"""A one-shot interrupted by a gateway stop drain is replayed exactly once on the next boot
(t_b5c315bb, Ace ruling C).

2026-11-08: one-shot 2a450ce62b25 fired 17:32:59, the gateway (pid 74812) got SIGTERM at
17:39:04 and its drain expired with the run in flight. The record kept fire_claim + run_claim
owned by the dead pid, repeat.completed=1, last_run_at=None, and the scheduler logged
cron.oneshot.overdue_not_due every ~10 min. The one-shot was lost silently.

Each test runs the claim + dispatch in a REAL owner process. On SIGTERM it runs the gateway's
own drain-expired sweep (``GatewayShutdownMixin._stop_kill_tool_subprocesses("post-interrupt")``),
then exits. The "restart" is the real ticker startup (``InProcessCronScheduler.start`` with the
stop event already set): hard-kill pass, execution recovery, replay-marker pass.
"""

import os
import signal
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
JOB_ID = "oneshot-drain"

_OWNER = textwrap.dedent(
    f"""
    import os, signal, sys, time
    from cron import scheduler as S
    from cron.executions import create_execution, finish_execution, mark_execution_running
    from cron.jobs import claim_dispatch, claim_job_for_fire, get_due_jobs, load_jobs, mark_job_run

    mode = sys.argv[1]
    due = [j["id"] for j in get_due_jobs()]
    assert due == [{JOB_ID!r}], due
    assert claim_job_for_fire({JOB_ID!r})
    assert claim_dispatch({JOB_ID!r})
    row = create_execution({JOB_ID!r}, source="tick")
    assert mark_execution_running(row["id"]) is not None
    owner = next(j for j in load_jobs() if j["id"] == {JOB_ID!r})["fire_claim"]["by"]
    # What run_one_job registers for an in-flight run.
    token, key = object(), S._inflight_key({JOB_ID!r})
    with S._running_lock:
        S._running_job_ids.add(key)
        S._running_fire_owners.setdefault(key, {{}})[token] = (owner, S._get_hermes_home().resolve())
        S._running_execution_ids[token] = row["id"]
    if mode == "finish":
        # The run finishes inside the drain: its own owner-fenced terminal write.
        assert mark_job_run({JOB_ID!r}, True, expected_fire_owner=owner)
        assert finish_execution(row["id"], success=True) is not None

    def on_term(*_):
        from gateway.run_shutdown import GatewayShutdownMixin
        marked = GatewayShutdownMixin._stop_kill_tool_subprocesses("post-interrupt")
        if mode != "ambiguous":
            S._finish_interrupted_run({{"id": {JOB_ID!r}}}, row["id"], None)
        print("SWEPT", ",".join(marked), flush=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    print("RUNNING", row["id"], flush=True)
    time.sleep(600)
    """
)


def _job() -> dict:
    from cron.jobs import load_jobs

    return next(j for j in load_jobs() if j["id"] == JOB_ID)


def _seed(no_agent: bool = False) -> None:
    from cron.jobs import save_jobs

    run_at = datetime.now(timezone.utc).astimezone() - timedelta(seconds=1)
    job = {
        "id": JOB_ID,
        "name": JOB_ID,
        "prompt": "say hi",
        "schedule": {"kind": "once", "run_at": run_at.isoformat()},
        "next_run_at": run_at.isoformat(),
        "last_run_at": None,
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": 1, "completed": 0},
        "deliver": "local",
    }
    if no_agent:
        job.update(no_agent=True, script="oneshot/x.sh", prompt="")
    save_jobs([job])


def _owner(home: Path, mode: str, sig: int) -> SimpleNamespace:
    """Run the claim + dispatch in a separate owner process, then stop it with ``sig``."""
    env = {k: v for k, v in os.environ.items() if k != "HERMES_MACHINE_ID"}
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        [sys.executable, "-c", _OWNER, mode], cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline().split()
        if not line or line[0] != "RUNNING":
            proc.kill()
            _, err = proc.communicate(timeout=30)
            pytest.fail(f"owner never reached the run: {line!r} {err[-3000:]}")
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
    swept = None
    for ln in out.splitlines():
        if ln.startswith("SWEPT"):
            swept = [x for x in ln[len("SWEPT"):].strip().split(",") if x]
    return SimpleNamespace(pid=proc.pid, run_id=line[1], swept=swept, err=err)


def _restart() -> None:
    """The real ticker startup of a fresh gateway: the loop exits at once (stop set)."""
    from cron.scheduler_provider import InProcessCronScheduler

    stop = threading.Event()
    stop.set()
    InProcessCronScheduler().start(stop, interval=60)


def _fire_if_due() -> list:
    """One tick's worth of firing: the due scan, then the dispatch claim a tick takes."""
    import cron.jobs as jobs_mod

    due = [j["id"] for j in jobs_mod.get_due_jobs()]
    for job_id in due:
        assert jobs_mod.claim_job_for_fire(job_id)
        assert jobs_mod.claim_dispatch(job_id)
    return due


def _finish_fired(success: bool = True) -> None:
    import cron.jobs as jobs_mod

    jobs_mod.mark_job_run(JOB_ID, success)


@pytest.fixture()
def home(hermes_env, monkeypatch):
    import cron.jobs as jobs_mod
    import cron.shutdown_replay as sr

    monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {"cron": {}})
    monkeypatch.setattr(jobs_mod, "_overdue_oneshot_warned", {}, raising=False)
    pages = []
    monkeypatch.setattr(sr, "page_alerts", lambda key, text: pages.append((key, text)) or True)
    return SimpleNamespace(path=hermes_env, pages=pages)


def _shift_clock(monkeypatch, seconds: float) -> None:
    import cron.jobs as jobs_mod

    later = jobs_mod._hermes_now() + timedelta(seconds=seconds)
    monkeypatch.setattr(jobs_mod, "_hermes_now", lambda: later)


def test_sigterm_mid_run_with_drain_expiring_fires_exactly_once_after_restart(home):
    from cron.executions import get_execution

    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    assert owner.swept == [JOB_ID], owner.err[-3000:]

    marked = _job()
    marker = marked["replay_pending"]
    assert marker["job_id"] == JOB_ID
    assert marker["run_id"] == owner.run_id
    assert marker["reason"] == "shutdown_interrupt"
    assert marker["boot_id"] and marker["marked_at"]
    # Released + rolled back in the same write: the dead pid holds nothing.
    assert marked["fire_claim"] is None and marked.get("run_claim") is None
    assert marked["repeat"]["completed"] == 0
    assert marked["last_run_at"] is None

    _restart()
    assert "replay_pending" not in _job()
    # The interrupted attempt is recorded honestly (startup recovery: owner gone).
    assert get_execution(owner.run_id)["status"] == "failed"
    assert _fire_if_due() == [JOB_ID]
    assert _fire_if_due() == []  # a second tick while the replay runs
    _finish_fired()
    assert _fire_if_due() == []
    _restart()
    assert _fire_if_due() == []
    assert home.pages == []


def test_job_that_finishes_during_drain_leaves_no_marker_and_no_replay(home):
    _seed()
    owner = _owner(home.path, "finish", signal.SIGTERM)
    assert owner.swept == [], owner.err[-3000:]
    done = _job()
    assert "replay_pending" not in done
    assert done["last_status"] == "ok" and done["last_run_at"] is not None
    _restart()
    assert _fire_if_due() == []
    assert home.pages == []


def test_marker_whose_run_has_terminal_execution_row_is_not_replayed(home):
    from cron import executions

    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    assert owner.swept == [JOB_ID], owner.err[-3000:]
    # The run's own terminal write landed (e.g. it completed right as the sweep marked it).
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='completed', finished_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), owner.run_id))

    _restart()
    job = _job()
    assert "replay_pending" not in job
    assert job["last_status"] == "ok" and job["last_run_at"] is not None
    assert job["repeat"]["completed"] == 1
    assert job["state"] == "completed" and job["enabled"] is False
    assert _fire_if_due() == []
    assert home.pages == []


def test_marker_from_run_that_delivered_before_interrupt_is_not_replayed(home):
    """The interrupted thread's own ledger write after delivery is a terminal outcome."""
    from cron import executions, shutdown_replay

    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='failed', error=? WHERE id=?",
            (shutdown_replay.INTERRUPTED_AFTER_DELIVERY_ERROR, owner.run_id))
    _restart()
    assert _fire_if_due() == []
    assert _job()["state"] == "completed"


def test_marker_from_run_interrupted_before_delivery_still_replays(home):
    """The interrupted thread's own 'before terminal completion' write is not an outcome."""
    from cron import executions, shutdown_replay

    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='failed', error=? WHERE id=?",
            (shutdown_replay.INTERRUPTED_ERROR, owner.run_id))
    _restart()
    assert _fire_if_due() == [JOB_ID]


def test_stale_marker_pages_once_and_is_not_replayed(home, monkeypatch):
    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    assert owner.swept == [JOB_ID], owner.err[-3000:]

    _shift_clock(monkeypatch, 6 * 3600 + 60)  # the gateway stayed down past the window
    _restart()
    assert "replay_pending" not in _job()
    assert _fire_if_due() == []
    assert len(home.pages) == 1
    key, text = home.pages[0]
    assert key == f"stale-{JOB_ID}"
    assert JOB_ID in text and f"hermes cron resume {JOB_ID} --run-now" in text

    _restart()
    assert len(home.pages) == 1  # the episode was paged once; the marker is gone


def test_stale_marker_retires_job_so_catchup_scan_cannot_fire_it(home, monkeypatch):
    """P1 d31d3ad3ad35: catch-up window (24 h) wider than the 6 h marker cap. The stale marker
    must retire the one-shot in the same write, or the catch-up scan fires it anyway."""
    import cron.jobs as jobs_mod

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: {"cron": {"oneshot_catchup_s": 86400}})
    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    assert owner.swept == [JOB_ID], owner.err[-3000:]

    _shift_clock(monkeypatch, 7 * 3600)  # past the 6 h cap, inside the 24 h catch-up window
    _restart()
    job = _job()
    assert "replay_pending" not in job
    assert job["enabled"] is False and job["state"] == "completed"
    assert job["fire_claim"] is None and job.get("run_claim") is None
    assert _fire_if_due() == []
    _shift_clock(monkeypatch, 60)  # one more scheduler tick
    from unittest.mock import patch
    from cron.scheduler import tick

    with patch("cron.scheduler.run_job") as run:
        tick(verbose=False, sync=True)
    run.assert_not_called()
    assert _fire_if_due() == []
    assert [k for k, _ in home.pages] == [f"stale-{JOB_ID}"]
    assert f"hermes cron resume {JOB_ID} --run-now" in home.pages[0][1]

    # The page's re-run command re-arms it.
    jobs_mod.rearm_oneshot(JOB_ID, jobs_mod._hermes_now().isoformat())
    assert _fire_if_due() == [JOB_ID]


def test_thread_ownership_loss_after_marking_still_replays_exactly_once(home):
    """P1 5291f0ebb63c: the marker releases fire_claim while the run's thread is still going.
    That thread then sees an ownership loss; its ledger write must not read as a terminal
    outcome that cancels the replay."""
    from unittest.mock import patch

    import cron.jobs as jobs_mod
    import cron.scheduler as sched
    from cron.executions import get_execution
    from cron.shutdown_replay import INTERRUPTED_ERROR

    _seed()
    assert jobs_mod.claim_job_for_fire(JOB_ID)
    job = _job()
    swept = []

    def run_job(*_a, **_k):
        # The drain expires while the agent is mid-run: the gateway's sweep runs, then the
        # agent's turn returns normally before the process exits.
        swept.extend(sched.mark_running_jobs_interrupted("drain expired", replay_marker=True))
        return True, "full output", "final response", None

    with patch("cron.scheduler.run_job", side_effect=run_job), \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver:
        assert sched.run_one_job(job) is True
    assert swept == [JOB_ID]
    deliver.assert_not_called()
    assert get_execution(job["execution_id"])["error"] == INTERRUPTED_ERROR
    assert _job()["replay_pending"]["run_id"] == job["execution_id"]

    _restart()
    assert _fire_if_due() == [JOB_ID]
    assert _fire_if_due() == []
    _finish_fired()
    _restart()
    assert _fire_if_due() == []
    assert home.pages == []


def test_ownership_lost_row_written_after_marking_is_not_a_terminal_outcome(home):
    """A run caught by the release before its body started records 'ownership lost'; written
    after marked_at that is the interruption, so the marker still replays once."""
    from cron import executions

    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    assert owner.swept == [JOB_ID], owner.err[-3000:]
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='failed', error=?, finished_at=? WHERE id=?",
            ("Fire claim ownership lost; stale result was discarded.",
             datetime.now(timezone.utc).astimezone().isoformat(), owner.run_id))
    _restart()
    assert _fire_if_due() == [JOB_ID]
    assert _fire_if_due() == []


def test_ownership_lost_row_from_before_marking_is_terminal(home):
    from cron import executions

    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    marked_at = datetime.fromisoformat(_job()["replay_pending"]["marked_at"])
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='failed', error=?, finished_at=? WHERE id=?",
            ("Fire claim ownership lost; stale result was discarded.",
             (marked_at - timedelta(seconds=5)).isoformat(), owner.run_id))
    _restart()
    assert _fire_if_due() == []
    assert _job()["state"] == "completed"


@pytest.mark.parametrize("ledger", ["missing", "running", "wedged", "unreadable"])
def test_marker_whose_run_cannot_be_proven_cut_off_is_retired_and_paged(home, ledger, monkeypatch):
    """P1 39cb8aa0809e: replay only on positive evidence the run was cut off by this stop.
    A missing row, a row still open, or a wedged-owner unknown proves nothing: page, retire."""
    from cron import executions

    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    assert owner.swept == [JOB_ID], owner.err[-3000:]
    with executions._transaction() as conn:
        if ledger == "missing":
            conn.execute("DELETE FROM executions WHERE id=?", (owner.run_id,))
        elif ledger == "running":
            # A live owner still holds it (e.g. a worker that outlived the gateway).
            conn.execute(
                "UPDATE executions SET status='running', error=NULL, process_id=?, pid=?, process_started_at=NULL WHERE id=?",
                (executions._PROCESS_ID, os.getpid(), owner.run_id))
        elif ledger == "unreadable":
            def unreadable(_run_id):
                raise OSError("ledger unavailable")

            monkeypatch.setattr(executions, "get_execution", unreadable)
        else:
            conn.execute(
                "UPDATE executions SET status='unknown', error=?, finished_at=? WHERE id=?",
                (executions._OWNER_WEDGED_REASON, datetime.now(timezone.utc).isoformat(),
                 owner.run_id))
    _restart()
    job = _job()
    assert "replay_pending" not in job
    assert job["enabled"] is False and job["state"] == "completed"
    assert _fire_if_due() == []
    assert [k for k, _ in home.pages] == [f"uncertain-{JOB_ID}"]
    assert f"hermes cron resume {JOB_ID} --run-now" in home.pages[0][1]


def test_sigkill_mid_run_is_not_replayed_and_pages_once(home):
    _seed()
    owner = _owner(home.path, "run", signal.SIGKILL)
    assert owner.swept is None  # no drain ran
    killed = _job()
    assert "replay_pending" not in killed
    assert f":{owner.pid}:" in killed["fire_claim"]["by"]  # the measured wedge shape

    _restart()
    released = _job()
    assert released["fire_claim"] is None and released.get("run_claim") is None
    assert "replay_pending" not in released
    assert _fire_if_due() == []
    assert len(home.pages) == 1
    key, text = home.pages[0]
    assert key == f"orphaned-{JOB_ID}"
    assert JOB_ID in text and "NOT replayed" in text

    _restart()
    assert _fire_if_due() == []
    assert len(home.pages) == 1


def test_script_runner_owned_job_is_never_marked(home):
    _seed(no_agent=True)
    _owner(home.path, "run", signal.SIGTERM)
    assert "replay_pending" not in _job()
    _restart()
    assert _fire_if_due() == []


def test_two_restarts_in_a_row_still_fire_exactly_once(home):
    _seed()
    owner = _owner(home.path, "run", signal.SIGTERM)
    assert owner.swept == [JOB_ID], owner.err[-3000:]
    _restart()  # consumes the marker; this boot dies before its first tick
    _restart()
    assert _fire_if_due() == [JOB_ID]
    _restart()  # restart while the replay is claimed
    assert _fire_if_due() == []
    _finish_fired()
    _restart()
    assert _fire_if_due() == []


def test_replay_interrupted_again_is_not_marked_a_second_time(home):
    """The one replay is spent: a second drain interrupt falls back to the plain mark."""
    import cron.jobs as jobs_mod

    _seed()
    _owner(home.path, "run", signal.SIGTERM)
    _restart()
    assert _job()[jobs_mod.RESTART_REQUEUE_COUNT_KEY] == 1
    from cron.shutdown_replay import mark_replay_pending

    assert _fire_if_due() == [JOB_ID]
    owner = _job()["fire_claim"]["by"]
    assert mark_replay_pending(JOB_ID, fire_owner=owner, run_id="x") is False
    assert "replay_pending" not in _job()


@pytest.mark.parametrize("delivered", [True, False])
def test_interrupted_run_ledger_error_says_whether_it_delivered(home, delivered):
    """The marker pass tells "already delivered" from "cut off" by this error text."""
    import cron.scheduler as sched
    from cron import shutdown_replay
    from cron.executions import create_execution, get_execution, mark_execution_running

    _seed()
    row = create_execution(JOB_ID, source="tick")
    mark_execution_running(row["id"])
    sched._finish_interrupted_run(_job(), row["id"], None, delivered=delivered)
    assert get_execution(row["id"])["error"] == (
        shutdown_replay.INTERRUPTED_AFTER_DELIVERY_ERROR if delivered
        else shutdown_replay.INTERRUPTED_ERROR)


def test_gateway_stop_sweep_requests_replay_markers(monkeypatch):
    """The drain-expired sweep is the only caller that asks for replay markers."""
    import cron.scheduler as sched
    from gateway.run_shutdown import GatewayShutdownMixin

    seen = {}

    def fake(reason, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(sched, "mark_running_jobs_interrupted", fake)
    GatewayShutdownMixin._stop_kill_tool_subprocesses("post-interrupt")
    assert seen.get("replay_marker") is True


def test_owner_gone_after_possible_delivery_is_uncertain(home):
    _seed()
    _owner(home.path, "ambiguous", signal.SIGTERM)
    _restart()
    assert _fire_if_due() == []
    assert [key for key, _ in home.pages] == [f"uncertain-{JOB_ID}"]


@pytest.mark.parametrize("change", ["recurring", "script"])
def test_obsolete_marker_does_not_retire_current_definition(home, change, monkeypatch):
    from cron import jobs as J

    _seed()
    _owner(home.path, "run", signal.SIGTERM)
    jobs = J.load_jobs()
    if change == "recurring":
        jobs[0]["schedule"] = {"kind": "cron", "expr": "0 * * * *"}
    else:
        jobs[0].update(no_agent=True, script="oneshot/x.sh")
    J.save_jobs(jobs)
    _shift_clock(monkeypatch, 7 * 3600)
    _restart()
    job = _job()
    assert "replay_pending" not in job
    assert job["enabled"] is True
    assert job["state"] == "scheduled"
    assert job["last_run_at"] is None


def test_external_provider_recovery_consumes_markers(home):
    from cron.scheduler_provider import CronScheduler

    _seed()
    _owner(home.path, "run", signal.SIGTERM)
    # Chronos calls the inherited recovery hook, not the builtin ticker start.
    CronScheduler.recover_interrupted(object())
    assert "replay_pending" not in _job()
    assert _fire_if_due() == [JOB_ID]


def test_interrupt_flag_without_persisted_mark_preserves_owner_outcome(home):
    from unittest.mock import patch
    from cron import jobs as J, scheduler as S
    from cron.executions import get_execution

    _seed()
    assert J.claim_job_for_fire(JOB_ID)
    job = _job()

    def deliver(*a, **kw):
        # Drain mark times out while delivery owns the fence: only the flag lands.
        with S._running_lock:
            S._interrupted_job_ids.add(S._inflight_key(JOB_ID))

    with patch("cron.scheduler.run_job", return_value=(True, "output", "response", None)), \
         patch("cron.scheduler._deliver_result", side_effect=deliver):
        assert S.run_one_job(job)
    assert _job()["last_status"] == "ok"
    assert get_execution(job["execution_id"])["status"] == "completed"
